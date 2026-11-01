"""Real business services and HTTP protocol, exclusively synthetic in-memory data."""
import base64
import hashlib
import io
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from pypdf import PdfReader
from sqlalchemy import select

from mietinkasso.domain.enums import OPTyp, MahnStatus
from mietinkasso.domain.exceptions import VorgangIdKonfliktError
from mietinkasso.indexautomatik.bootstrap import bauen
from mietinkasso.indexautomatik.mailops_client import MailOpsClient, MailOpsErgebnis
from mietinkasso.indexautomatik.mailversand_service import HVMailversandService, zahlungsziel_fuer_mahnung
from mietinkasso.infrastructure.config import Settings
from mietinkasso.infrastructure.db.tables import (
    AuditEventTable, MahnFallTable, MahnkostenBuchungTable, MahnkostenGebuehrTable, MahnLaufTable,
)
from mietinkasso.mahnwesen import brief_sftp
from mietinkasso.mahnwesen.brief_rest import PostRestTransportadapter, RestKonfiguration
from mietinkasso.mahnwesen.brief_sftp import PostSftpTransportadapter, SftpKonfiguration
from tests.mietinkasso._brief_rest_test_helpers import STATUS, UPLOAD, VERARBEITEN, FakeEinfachBrief


@pytest.fixture
def hv(session_factory, basis_vertrag, tmp_path):
    token = tmp_path / "synthetic-token"
    token.write_text("SYNTHETIC-TOKEN", encoding="utf-8")
    state = {"status": "ANGENOMMEN", "calls": [], "sent": "2026-09-14T22:30:00Z"}
    def provider(request):
        state["calls"].append(request)
        payload = json.loads(request.content) if request.method == "POST" else None
        ref = payload["referenz"] if payload else request.url.params["referenz"]
        external = "JLBHV-" + hashlib.sha256(json.dumps(ref, ensure_ascii=False).encode()).hexdigest()
        return httpx.Response(200, json={"status": state["status"], "externe_referenz": external,
            "provider_referenz": "SYNTHETIC-SENT-1", "versendet_am": state["sent"] if state["status"] == "GESENDET" else None})
    client = MailOpsClient(token_file=token, transport_factory=lambda: httpx.MockTransport(provider))
    settings = Settings(owner_email="mb@jlb-immo.at", send_enabled=True, hv_mail_allowlist_bestaetigt=True,
        indexautomatik_send_enabled=True, vertragsende_erinnerung_send_enabled=True)
    bundle = bauen(session_factory, settings)
    return HVMailversandService(session_factory, bundle, settings, client=client), state




def _echter_beleg_pdf(text: str) -> bytes:
    """Ein TATSÄCHLICH mit `pypdf` lesbares, einseitiges synthetisches PDF
    (Codex-Abnahme 19.10.2026, dritte Korrektur: `importiere_anbieterbeleg`
    prüft jetzt lesbare Seitenzahl/Unverschlüsseltheit, nicht mehr nur die
    `%PDF-`-Kopfzeile). Unterschiedlicher `text` ergibt unterschiedliche
    Bytes/Hashes für "abweichender Beleg"-Tests."""

    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.cell(0, 10, text)
    return bytes(pdf.output())


def _seed_debt(hv, contract, account, ctx, day=date(2026, 9, 13)):
    service, _ = hv
    policy = service.policy_repo.anlegen(stufe1_tage_nach_faelligkeit=7,
        stufe2_mindesttage_nach_stufe1_versand=14, zinsen_prozent=Decimal("0"), gebuehr_cent=0, status="ENTWURF")
    service.policy_repo.freigeben(policy.id)
    service.op_service.buchen(ctx=ctx, konto=account, typ=OPTyp.SOLL, betrag_cent=83000,
        belegdatum=date(2026, 9, 1), buchungsdatum=date(2026, 9, 1), faelligkeit=date(2026, 9, 5),
        beleg_referenz="SYNTHETIC September")
    service.bank_repo.upsert_bank_konto(id="SYNTHETIC-BANK", gesellschaft_id=contract.gesellschaft_id,
        iban="SYNTHETIC-NOT-A-REAL-IBAN", bezeichnung="Testkonto")
    service.bank_service.bestaetige_bankvollstaendigkeit(bank_konto_id="SYNTHETIC-BANK",
        bestaetigt_bis=day, bestaetigt_von="SYNTHETIC-TEST")


def test_mahnung_acceptance_then_actual_sending_get_only_and_stage2_clock(hv, basis_vertrag, admin_ctx, session_factory):
    service, state = hv
    contract, account = basis_vertrag
    _seed_debt(hv, contract, account, admin_ctx)
    result = service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))
    assert result["gesendet"] == 0 and result["geplant"] == 1
    row = service.mahn_repo.list_fuer_vertrag(contract.id)[0]
    assert row.status == "UNSICHER" and row.gesendet_am is None
    body = json.loads(state["calls"][0].content)
    assert body["art"] == "MAHNUNG" and "830,00 EUR" in body["text"]
    assert body["empfaenger_email"] == "mieter@example.at"
    assert len(state["calls"]) == 1
    service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 14))
    assert len(state["calls"]) == 1  # no repeated POST for an uncertain case
    state["status"] = "GESENDET"
    assert service.status_abgleichen(ctx=admin_ctx) == 1
    assert [r.method for r in state["calls"]] == ["POST", "GET"]
    sent = service.mahn_repo.get(row.id)
    assert sent.status == "GESENDET" and sent.gesendet_am.isoformat() == "2026-09-14T22:30:00"
    # Der Status-Abgleich vervollständigt nicht nur das Mitglied, sondern
    # konsistent AUCH die Gruppe selbst (unabhängige Rückprüfung Codex
    # 14.09.2026: "Mitgliedsnachweis UND Kostenabschluss ... konsistent
    # fertigstellen") - beide werden nachweislich GESENDET, kein
    # erneuter Versand.
    gruppe = service.mahnlauf_repo.list_fuer_vertrag(contract.id)[0]
    assert gruppe.status == "GESENDET"
    assert service.status_abgleichen(ctx=admin_ctx) == 0
    with session_factory() as s:
        evidence = list(s.execute(select(AuditEventTable).where(AuditEventTable.aktion == "MAILVERSAND_BESTAETIGT")).scalars())
    # ZWEI Audit-Einträge - einer für die Gruppe (`mahnlaeufe`), einer für
    # das Mitglied (`mahn_faelle`) - beide belegen denselben tatsächlichen
    # Versand, keiner davon ein zweiter/erfundener.
    assert len(evidence) == 2
    assert {e.entity_typ for e in evidence} == {"mahnlaeufe", "mahn_faelle"}
    assert all(e.payload["zugang_bestaetigt"] is False for e in evidence)
    shown = service.versanduebersicht(ctx=admin_ctx)
    assert len(shown) == 1 and shown[0]["status"] == "GESENDET"
    assert shown[0]["provider_referenz"] == "SYNTHETIC-SENT-1"
    from mietinkasso.auth.service import AuthContext
    from mietinkasso.domain.enums import Rolle
    outsider = AuthContext(user_id="OTHER", rolle=Rolle.ADMIN, gesellschaft_ids={"OTHER"})
    assert service.versanduebersicht(ctx=outsider) == []
    service.bank_service.bestaetige_bankvollstaendigkeit(bank_konto_id="SYNTHETIC-BANK",
        bestaetigt_bis=date(2026, 9, 28), bestaetigt_von="SYNTHETIC-TEST")
    # Actual sending was 15 September in Vienna, not the 13 September dispatch attempt.
    too_early = service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 28))
    assert too_early["geplant"] == 0 and len(state["calls"]) == 2
    state["sent"] = "2026-09-29T08:00:00Z"
    service.bank_service.bestaetige_bankvollstaendigkeit(bank_konto_id="SYNTHETIC-BANK",
        bestaetigt_bis=date(2026, 9, 29), bestaetigt_von="SYNTHETIC-TEST")
    assert service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 29))["gesendet"] == 1
    assert service.mahn_repo.list_fuer_vertrag(contract.id)[0].stufe == 2
    assert len(state["calls"]) == 3
    service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 30))
    assert len(state["calls"]) == 3


def test_konfiguriertes_zahlungsziel_erscheint_im_mahntext_mit_periode_ursprung_und_rest(hv, basis_vertrag, admin_ctx):
    """Abnahme-Folgefix (Codex 20.10.2026): "Brief/Mailentwurf enthält
    noch keine geprüfte Ziel-IBAN/Kontoinhaber/Referenz und keine
    vollständige Periode/Position/Ursprungsbetrag/angerechnete
    Zahlungen/Rest-Tabelle". Eine teilweise angerechnete Zahlung macht
    Ursprungsbetrag/Rest im Mailtext sichtbar; die aus der tatsächlichen,
    eindeutigen Gesellschafts-Bankkontobindung aufgelöste Kontoverbindung
    erscheint konkret statt des vagen Hinweistextes (Auftrag HV-20261020-
    MAHN-FORTSETZUNG - keine globale Settings-IBAN mehr)."""

    service, state = hv
    contract, account = basis_vertrag
    _seed_debt(hv, contract, account, admin_ctx)
    # Dieselbe Bankkonto-ID wie `_seed_debt` - EIN upsert auf dieselbe
    # Zeile, keine zweite, sonst wäre die Bindung mehrdeutig.
    service.bank_repo.upsert_bank_konto(
        id="SYNTHETIC-BANK", gesellschaft_id=contract.gesellschaft_id,
        iban="AT611904300234573201", bezeichnung="Testkonto Zahlungsziel",
    )
    # Teilzahlung auf die Septemberforderung - Ursprungsbetrag 830,00 EUR,
    # davon 200,00 EUR bereits angerechnet, Rest 630,00 EUR.
    service.op_service.buchen(
        ctx=admin_ctx, konto=account, typ=OPTyp.ZAHLUNG, betrag_cent=20000,
        belegdatum=date(2026, 9, 10), buchungsdatum=date(2026, 9, 10), faelligkeit=None,
        beleg_referenz="Teilzahlung September",
    )
    result = service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))
    assert result["gesendet"] == 0 and result["geplant"] == 1
    body = json.loads(state["calls"][0].content)
    text = body["text"]
    assert "AT611904300234573201" in text
    assert "7D Immobilien GmbH" in text
    # Nachtrag Markus 23.10.2026 (HV-20261023-MAHNUNG-EINFACH): die alte
    # Prosa-Zeile "ursprünglich/bereits angerechnet/Rest" ist durch die
    # kurze chronologische Tabelle ersetzt - Ursprungsbetrag (830,00),
    # die angerechnete Zahlung (-200,00) und der daraus laufende Rest
    # (630,00) erscheinen jetzt als eigene Zeilen/Spalten DIESER Tabelle,
    # keine zweite, parallele Aufstellung.
    assert "830,00 EUR" in text
    assert "-200,00 EUR" in text
    assert "630,00 EUR" in text
    assert "auf das Ihnen für dieses Mietverhältnis bekannt gegebene Konto" not in text


def test_email_mahntext_ist_kurz_eine_tabelle_ohne_alte_forderungsprosa(hv, basis_vertrag, admin_ctx):
    """Auftrag HV-20261023-MAHNUNG-EINFACH, jetzt fertiggestellt für den
    E-Mail-Kanal: kurze Einleitung, GENAU EIN chronologisches Journal mit
    "Gesamt offen", konkrete Zahlungsaufforderung/Zahlungskonto, kurzer
    Rechtsfolgetext, fester Abschluss "JLB/Die Hausverwaltung" - die
    vormalige, separat formulierte Forderungsprosa ("Hauptforderung
    gesamt", "Gesamtbetrag (Hauptforderung inkl. ...)", der nachgestellte
    "Kontoauszug (Stand vor dieser Mahnung)"-Block) ist vollständig
    entfernt, keine zweite parallele Aufstellung."""

    service, state = hv
    contract, account = basis_vertrag
    _seed_debt(hv, contract, account, admin_ctx)

    result = service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))
    assert result["geplant"] == 1
    text = json.loads(state["calls"][0].content)["text"]

    assert "Hauptforderung gesamt" not in text
    assert "Gesamtbetrag (Hauptforderung" not in text
    assert "Kontoauszug (Stand vor dieser Mahnung)" not in text
    assert "Gesamt offen" in text
    assert "Ausgangssaldo" in text or "Mietvorschreibung" in text  # chronologisches Journal vorhanden
    assert text.rstrip().endswith("Mit freundlichen Grüßen\nJLB\nDie Hausverwaltung")


@pytest.mark.parametrize("receipt", [None, MailOpsErgebnis("ANGENOMMEN", "TEST", "TEST")])
def test_noop_or_accepted_callback_cannot_complete_dunning(receipt, hv, basis_vertrag, admin_ctx):
    service, state = hv
    contract, account = basis_vertrag
    _seed_debt(hv, contract, account, admin_ctx)
    planned = service.mahn_service.plane_alle_offenen_forderungen(ctx=admin_ctx, vertrag=contract, konto=account,
        policy=service.policy_repo.aktuelle_freigegebene(), heute=date(2026, 9, 13), bank_bestaetigt_bis=date(2026, 9, 13))[0]
    result = service.mahn_service.versenden(ctx=admin_ctx, mahnfall_id=planned.mahnfall_id,
        heute=date(2026, 9, 13), bank_bestaetigt_bis=date(2026, 9, 13), ungeklaerte_eingaenge_vorhanden=False,
        send_enabled=True, versand_fn=lambda _: receipt)
    assert result.status == "UNSICHER" and service.mahn_repo.get(planned.mahnfall_id).gesendet_am is None
    with pytest.raises(ValueError):
        service.mahn_service.manuell_abklaeren(mahnfall_id=planned.mahnfall_id, neuer_status=MahnStatus.GESENDET)


def test_owner_notice_waits_for_actual_receipt_and_never_goes_to_tenant(hv, basis_vertrag, admin_ctx):
    service, state = hv
    contract, _ = basis_vertrag
    st = service.bundle.stammdaten_repository
    st.upsert_vertrag(id=contract.id, einheit_id=contract.einheit_id, debitor_id=contract.debitor_id,
        gesellschaft_id=contract.gesellschaft_id, rechtsordnung=contract.rechtsordnung,
        gueltig_von=contract.gueltig_von, gueltig_bis=date(2026, 12, 31))
    service.bundle.vertragsende_service.plane_alle(ctx=admin_ctx, heute=date(2026, 9, 30))
    result = service.bundle.vertragsende_service.benachrichtige_faellige(
        heute=date(2026, 9, 30), send_enabled=True, versand_fn=service.owner_senden)
    assert result == []
    row = service.bundle.vertragsende_repository.liste_alle()[0]
    assert row.status == "UNKLAR" and row.benachrichtigt_am is None
    body = json.loads(state["calls"][0].content)
    assert body["art"] == "VERTRAGSENDE" and body["empfaenger_email"] == "mb@jlb-immo.at"
    state.update(status="GESENDET", sent="2026-09-30T11:42:12Z")
    assert service.status_abgleichen(ctx=admin_ctx) == 1
    assert service.bundle.vertragsende_repository.get(row.id).benachrichtigt_am.isoformat() == "2026-09-30T11:42:12"
    assert service.status_abgleichen(ctx=admin_ctx) == 0
    assert [r.method for r in state["calls"]] == ["POST", "GET"]


@pytest.mark.parametrize("flag", ["send_enabled", "hv_mail_allowlist_bestaetigt"])
def test_disabled_dunning_never_calls_provider(flag, hv, basis_vertrag, admin_ctx):
    service, state = hv
    contract, account = basis_vertrag
    _seed_debt(hv, contract, account, admin_ctx)
    setattr(service.settings, flag, False)
    assert service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))["gesendet"] == 0
    assert state["calls"] == []


def test_status_scope_blocks_other_company(hv, basis_vertrag, admin_ctx, ctx_factory):
    service, state = hv
    contract, account = basis_vertrag
    _seed_debt(hv, contract, account, admin_ctx)
    service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))
    assert service.status_abgleichen(ctx=ctx_factory("OTHER")) == 0
    assert len(state["calls"]) == 1


def _seed_index(service, contract, ctx):
    b = service.bundle
    b.stammdaten_repository.upsert_debitor(id=contract.debitor_id, name="Max Mustermieter",
        email="mieter@example.at", adresse="Mustergasse 1, 1010 Wien")
    b.stammdaten_repository.add_komponente(id="SYNTHETIC-HMZ", vertrag_id=contract.id,
        art="HMZ", bezeichnung="Hauptmietzins", betrag_cent=100000,
        indexierbar=True, gueltig_von=date(2024, 1, 1))
    profile = b.rechtsprofil_service.entwurf_anlegen(ctx=ctx, vertrag_id=contract.id,
        rechtsordnung="OESTERREICH_MRG_VOLL", ist_wohnungsnutzung=True,
        mrg_zinsbeschraenkung=False, mrg_zinsbeschraenkung_geprueft=True,
        foerderbindung=False, foerderbindung_geprueft=True, ist_altvertrag=False, ist_hauptmiete=True,
        mietzinsobergrenze_cent=None, mietzinsobergrenze_quellenbeleg=None, mietzinsobergrenze_gueltig_bis=None,
        bezugsjahr=2024, bezugsmonat=1, letzte_basis_war_jahresdurchschnitt=False,
        basis_komponenten_ids=["SYNTHETIC-HMZ"], vertraglich_zulaessiger_betrag_cent=200000,
        vertraglicher_quellenbeleg="SYNTHETIC Mietvertrag Punkt 5",
        vertraglicher_fruehestmoeglicher_termin=date(2026, 4, 1),
        vertrag_beleg_referenz="SYNTHETIC Mietvertrag", klausel_referenz="Punkt 5", erstellt_von="TEST")
    b.rechtsprofil_service.freigeben(profile.id, ctx=ctx, freigegeben_von="TEST")
    for year, value in ((2023, "100"), (2024, "102"), (2025, "104")):
        b.vpi_repository.jahreswert_erfassen(reihe="VPI20C18", jahr=year, wert=Decimal(value),
            quelle="SYNTHETIC Statistik", quelle_datum=date(year + 1, 2, 17), erfasst_von="TEST")
    result = b.index_automatik_service.monatslauf_fuer_vertrag(ctx=ctx, vertrag=contract,
        heute=date(2026, 4, 5), akteur="TEST")
    assert result.status == "ERHOEHUNG_ERZEUGT"
    return b.outbox_repository.get(result.erhoehungsschreiben_id)


# -- Mahnlauf-Bündelung (Rückprüfung 14.09.2026): ein Schreiben je Vertrag+Stufe --


def _bank_bestaetigen(service, contract, bis):
    service.bank_repo.upsert_bank_konto(id="SYNTHETIC-BANK", gesellschaft_id=contract.gesellschaft_id,
        iban="SYNTHETIC-NOT-A-REAL-IBAN", bezeichnung="Testkonto")
    service.bank_service.bestaetige_bankvollstaendigkeit(bank_konto_id="SYNTHETIC-BANK",
        bestaetigt_bis=bis, bestaetigt_von="SYNTHETIC-TEST")


def _seed_zwei_komponenten(hv, contract, account, ctx, *, faelligkeit=date(2026, 9, 5)):
    service, _ = hv
    belegdatum = faelligkeit.replace(day=1)
    policy = service.policy_repo.anlegen(stufe1_tage_nach_faelligkeit=7,
        stufe2_mindesttage_nach_stufe1_versand=14, zinsen_prozent=Decimal("0"), gebuehr_cent=0, status="ENTWURF")
    service.policy_repo.freigeben(policy.id)
    service.op_service.buchen(ctx=ctx, konto=account, typ=OPTyp.SOLL, betrag_cent=60_000,
        belegdatum=belegdatum, buchungsdatum=belegdatum, faelligkeit=faelligkeit,
        leistungsperiode=faelligkeit.strftime("%Y-%m"), beleg_referenz="SYNTHETIC HMZ")
    service.op_service.buchen(ctx=ctx, konto=account, typ=OPTyp.SOLL, betrag_cent=15_000,
        belegdatum=belegdatum, buchungsdatum=belegdatum, faelligkeit=faelligkeit,
        leistungsperiode=faelligkeit.strftime("%Y-%m"), beleg_referenz="SYNTHETIC BK")


def test_mahnlauf_buendelt_zwei_komponenten_derselben_periode_zu_einem_schreiben(hv, basis_vertrag, admin_ctx):
    """Abnahmekriterium 14.09.2026: HMZ + BK derselben Vorschreibung
    dürfen NICHT zu zwei separaten Mahnschreiben führen - genau EIN
    tatsächlicher Versand je Vertrag und Mahnstufe, auch wenn zwei
    MahnFälle (je OP-Zeile) geplant werden."""

    service, state = hv
    contract, account = basis_vertrag
    state["status"] = "GESENDET"
    _seed_zwei_komponenten(hv, contract, account, admin_ctx)
    _bank_bestaetigen(service, contract, date(2026, 9, 13))

    result = service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))

    assert result["geplant"] == 2  # zwei MahnFälle geplant (je OP-Zeile) ...
    assert len(state["calls"]) == 1  # ... aber GENAU EIN tatsächlicher Versand
    assert result["gesendet"] == 1
    body = json.loads(state["calls"][0].content)
    assert "750,00 EUR" in body["text"]  # 600 + 150 EUR kombiniert

    faelle = service.mahn_repo.list_fuer_vertrag(contract.id)
    assert len(faelle) == 2
    assert all(f.status == "GESENDET" for f in faelle)
    # Derselbe tatsächliche Versandzeitpunkt für BEIDE MahnFälle - EIN Brief.
    assert len({f.gesendet_am for f in faelle}) == 1


def test_mahnlauf_zwei_gleichzeitige_aufrufe_fuer_verschiedene_gruppenmitglieder_senden_nur_einmal(
    hv, basis_vertrag, admin_ctx,
):
    """Synthetische Regression für konkurrierende/mehrfache
    Versandverarbeitung: zwei (hier sequenziell simulierte, aber je für
    ein ANDERES Gruppenmitglied aufgerufene) Dispatch-Versuche dürfen
    NIEMALS zwei E-Mails auslösen - beide lösen dieselbe Gruppe/denselben
    führenden Fall auf und konkurrieren um dessen atomaren Claim."""

    service, state = hv
    contract, account = basis_vertrag
    state["status"] = "GESENDET"
    _seed_zwei_komponenten(hv, contract, account, admin_ctx)
    _bank_bestaetigen(service, contract, date(2026, 9, 13))
    policy = service.policy_repo.aktuelle_freigegebene()
    geplant = service.mahn_service.plane_alle_offenen_forderungen(
        ctx=admin_ctx, vertrag=contract, konto=account, policy=policy, heute=date(2026, 9, 13),
        bank_bestaetigt_bis=date(2026, 9, 13),
    )
    assert len(geplant) == 2
    id_a, id_b = geplant[0].mahnfall_id, geplant[1].mahnfall_id

    # Erster Dispatch-Versuch (für Mitglied A) verarbeitet die GANZE Gruppe
    # (inkl. B) in einem Rutsch und sendet EINMAL tatsächlich.
    ergebnis_a = service.mahnung_senden(ctx=admin_ctx, row_id=id_a, heute=date(2026, 9, 13))
    # Ein zweiter, für das ANDERE Gruppenmitglied gestarteter Dispatch-
    # Versuch (z. B. ein zweiter, gleichzeitig laufender Worker) findet
    # dieselbe Forderung bereits verarbeitet vor - kein zweiter Versand.
    ergebnis_b = service.mahnung_senden(ctx=admin_ctx, row_id=id_b, heute=date(2026, 9, 13))

    assert len(state["calls"]) == 1  # trotz zweier Dispatch-Aufrufe nur EIN tatsächlicher Versand
    assert ergebnis_a.status == "GESENDET"
    assert ergebnis_b.status == "BEREITS_VERARBEITET"
    assert service.mahn_repo.get(id_a).status == "GESENDET"
    assert service.mahn_repo.get(id_b).status == "GESENDET"


def test_mahnlauf_wiederholte_stufe_sendet_je_stufe_wieder_genau_ein_schreiben(hv, basis_vertrag, admin_ctx):
    """Wiederholte Mahnstufen: Stufe 1 und die spätere Stufe 2 lösen JEDE
    für sich genau EIN Schreiben aus (kein kumulativer Zähler über
    Stufen hinweg, keine Doppelverarbeitung einer bereits gesendeten
    Stufe)."""

    service, state = hv
    contract, account = basis_vertrag
    state["status"] = "GESENDET"
    _seed_zwei_komponenten(hv, contract, account, admin_ctx)
    _bank_bestaetigen(service, contract, date(2026, 9, 13))

    stufe1 = service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))
    assert stufe1["gesendet"] == 1
    assert len(state["calls"]) == 1

    # Erneuter Lauf am selben Tag: nichts Neues zu tun (bereits GESENDET).
    service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))
    assert len(state["calls"]) == 1

    # Stufe 2 verlangt einen Mindestabstand seit dem TATSÄCHLICHEN
    # Stufe-1-Versandzeitpunkt (hier die feste synthetische Testuhrzeit
    # 2026-09-14T22:30 UTC = 2026-09-15 in Wien) plus Zahlungsfrist - wie im
    # bestehenden Referenztest ist der 28.9. noch zu früh, der 29.9. reicht.
    _bank_bestaetigen(service, contract, date(2026, 9, 28))
    zu_frueh = service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 28))
    assert zu_frueh["gesendet"] == 0
    assert len(state["calls"]) == 1

    _bank_bestaetigen(service, contract, date(2026, 9, 29))
    stufe2 = service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 29))
    assert stufe2["gesendet"] == 1
    assert len(state["calls"]) == 2  # ein zweites, eigenständiges Schreiben für Stufe 2

    faelle = service.mahn_repo.list_fuer_vertrag(contract.id)
    assert sorted(f.stufe for f in faelle) == [1, 1, 2, 2]
    assert all(f.status == "GESENDET" for f in faelle)


def test_mahnkosten_text_stimmt_exakt_mit_gebuchten_zusatzpositionen_ueberein(hv, basis_vertrag, admin_ctx):
    """Abnahmekriterium 14.09.2026: der im TATSÄCHLICH gesendeten Text
    ausgewiesene Zins-/Kostenbetrag muss exakt dem danach gebuchten
    Zusatzbetrag entsprechen - beide stammen aus derselben, EINMAL
    berechneten Vorschau (siehe `kosten_service.py`-Moduldoc)."""

    service, state = hv
    contract, account = basis_vertrag
    state["status"] = "GESENDET"
    _seed_zwei_komponenten(hv, contract, account, admin_ctx, faelligkeit=date(2026, 1, 5))
    _bank_bestaetigen(service, contract, date(2026, 3, 1))
    profil = service.mahnkosten_repo.zinsprofil_anlegen(
        vertrag_id=contract.id, ist_b2b=False, vertragsdatum=None, erstellt_von="test",
    )
    service.mahnkosten_repo.zinsprofil_freigeben(profil.id, freigegeben_von="test")

    result = service.mahnlauf(ctx=admin_ctx, heute=date(2026, 3, 1))
    assert result["gesendet"] == 1
    body = json.loads(state["calls"][0].content)

    from mietinkasso.infrastructure.db.tables import MahnkostenBuchungTable
    with service.sf() as db:
        buchung = db.execute(select(MahnkostenBuchungTable).where(
            MahnkostenBuchungTable.vertrag_id == contract.id)).scalars().one()
    zinsen_text = f"{buchung.zinsen_cent/100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    assert buchung.zinsen_cent > 0
    assert f"{zinsen_text} EUR" in body["text"]


def test_absturz_zwischen_bestaetigtem_versand_und_kostenbuchung_wird_anhand_snapshot_nachgeholt(
    hv, basis_vertrag, admin_ctx, monkeypatch,
):
    """Auftrag Markus 14.09.2026: "Recovery nach bestätigtem Versand mit
    eingefrorenem Kosten-/Inhaltssnapshot". Simuliert einen Absturz GENAU
    zwischen dem bestätigten GESENDET-Übergang (Gruppe+Mitglieder, samt
    dabei atomar eingefrorenem Kosten-/Inhaltssnapshot) und der
    eigentlichen Kostenbuchung, indem `MahnkostenService.buche_vorschau`
    beim ERSTEN Aufruf eine Exception wirft. Der Versand selbst bleibt
    dabei UNVERÄNDERT korrekt bestätigt (kein Doppelversand-Risiko); die
    fehlende Kostenbuchung wird über die tägliche Recovery-Routine
    NACHTRÄGLICH exakt anhand des eingefrorenen Snapshots nachgeholt -
    NIE anhand einer frisch neu berechneten, potenziell abweichenden
    Vorschau."""

    service, state = hv
    contract, account = basis_vertrag
    state["status"] = "GESENDET"
    _seed_zwei_komponenten(hv, contract, account, admin_ctx, faelligkeit=date(2026, 1, 5))
    _bank_bestaetigen(service, contract, date(2026, 3, 1))
    profil = service.mahnkosten_repo.zinsprofil_anlegen(
        vertrag_id=contract.id, ist_b2b=False, vertragsdatum=None, erstellt_von="test",
    )
    service.mahnkosten_repo.zinsprofil_freigeben(profil.id, freigegeben_von="test")

    original_buche_vorschau = service.mahnkosten_service.buche_vorschau
    aufrufe = {"anzahl": 0}

    def kaputte_buchung(*args, **kwargs):
        aufrufe["anzahl"] += 1
        if aufrufe["anzahl"] == 1:
            raise RuntimeError("Simulierter Absturz NACH bestätigtem Versand, VOR der Kostenbuchung.")
        return original_buche_vorschau(*args, **kwargs)

    monkeypatch.setattr(service.mahnkosten_service, "buche_vorschau", kaputte_buchung)
    with pytest.raises(RuntimeError):
        service.mahnlauf(ctx=admin_ctx, heute=date(2026, 3, 1))
    monkeypatch.undo()

    # Der Versand selbst ist trotz des simulierten Absturzes UNVERÄNDERT
    # korrekt bestätigt - kein halb-gesendeter/unklarer Zustand.
    with service.sf() as db:
        mahnlauf = db.execute(select(MahnLaufTable).where(MahnLaufTable.vertrag_id == contract.id)).scalars().one()
        mahnfaelle = list(db.execute(select(MahnFallTable).where(MahnFallTable.vertrag_id == contract.id)).scalars())
    assert mahnlauf.status == "GESENDET"
    assert all(f.status == MahnStatus.GESENDET.value for f in mahnfaelle)
    # Der Kosten-/Inhaltssnapshot wurde bereits ATOMAR mit dem
    # GESENDET-Übergang eingefroren - aber die Buchung selbst ist noch
    # NICHT als abgeschlossen markiert (genau die Recovery-Lücke).
    assert mahnlauf.mahnkosten_snapshot_json is not None
    assert mahnlauf.mahnkosten_verarbeitet_am is None

    with service.sf() as db:
        keine_buchung = list(db.execute(select(MahnkostenBuchungTable).where(
            MahnkostenBuchungTable.vertrag_id == contract.id)).scalars())
    assert keine_buchung == []  # tatsächlich noch nichts gebucht

    # Recovery: bucht anhand des eingefrorenen Snapshots nach, ohne den
    # Versand erneut anzustoßen (kein zweiter Mailversand, `state["calls"]`
    # bleibt bei genau einem Aufruf).
    aufrufe_vor_recovery = len(state["calls"])
    nachgeholt = service.mahn_service.vervollstaendige_gesendete_mahnlaeufe(
        ctx=admin_ctx, akteur="recovery-test",
    )
    assert len(nachgeholt) == 1
    assert len(state["calls"]) == aufrufe_vor_recovery  # kein erneuter Versand

    with service.sf() as db:
        buchung = db.execute(select(MahnkostenBuchungTable).where(
            MahnkostenBuchungTable.vertrag_id == contract.id)).scalars().one()
        mahnlauf_nach = db.get(MahnLaufTable, mahnlauf.id)
    assert buchung.zinsen_cent > 0
    assert buchung.versandnachweis_referenz == "mahnungslauf:" + mahnlauf.outbox_key
    assert mahnlauf_nach.mahnkosten_verarbeitet_am is not None

    # Ein zweiter Recovery-Durchlauf findet nichts mehr offen und bucht
    # NICHT ein zweites Mal (keine doppelte Kostenposition).
    zweiter_durchlauf = service.mahn_service.vervollstaendige_gesendete_mahnlaeufe(
        ctx=admin_ctx, akteur="recovery-test",
    )
    assert zweiter_durchlauf == []
    with service.sf() as db:
        buchungen_gesamt = list(db.execute(select(MahnkostenBuchungTable).where(
            MahnkostenBuchungTable.vertrag_id == contract.id)).scalars())
    assert len(buchungen_gesamt) == 1


def test_absturz_mitten_in_mitgliederschleife_wird_ueber_gruppenquittung_nachgezogen(
    hv, basis_vertrag, admin_ctx, monkeypatch,
):
    """Unabhängige Abnahme eb7b8a7, echter Bug: der bestätigte GESENDET-
    Übergang der GRUPPE selbst wird atomar committet, aber ein Absturz
    UNMITTELBAR DANACH - mitten in der Mitgliederschleife, bevor auch
    nur EIN Mitglied nachgezogen ist - ließ das Mitglied dauerhaft
    GEBUENDELT statt GESENDET zurück. Das blockiert insbesondere Stufe 2
    (die eine tatsächlich GESENDETE Stufe 1 voraussetzt) und den
    Zugangsbeleg für dieses Mitglied, obwohl der Versand selbst längst
    bestätigt war. Die Recovery MUSS Mitgliedsnachweis UND
    Kostenabschluss konsistent anhand der TATSÄCHLICHEN, bereits
    persistierten Gruppenquittung nachziehen - OHNE erneut zu senden."""

    service, state = hv
    contract, account = basis_vertrag
    state["status"] = "GESENDET"
    _seed_debt(hv, contract, account, admin_ctx)

    import mietinkasso.mahnwesen.service as mahn_service_module
    original_versand_belegen = mahn_service_module.versand_belegen

    def kaputtes_versand_belegen(session_factory, table, row_id, **kwargs):
        if table is MahnFallTable:
            raise RuntimeError(
                "Simulierter Absturz NACH bestätigtem Gruppen-GESENDET+Snapshot, "
                "WÄHREND der Mitgliederschleife (vor jedem einzelnen Mitgliedsnachweis)."
            )
        return original_versand_belegen(session_factory, table, row_id, **kwargs)

    monkeypatch.setattr(mahn_service_module, "versand_belegen", kaputtes_versand_belegen)
    with pytest.raises(RuntimeError):
        service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))
    monkeypatch.undo()

    mahnlauf = service.mahnlauf_repo.list_fuer_vertrag(contract.id)[0]
    mahnfall = service.mahn_repo.list_fuer_vertrag(contract.id)[0]
    assert mahnlauf.status == "GESENDET"  # Gruppe selbst bereits korrekt bestätigt
    assert mahnfall.status == "GEBUENDELT"  # Mitglied hängt noch fest - genau die Lücke
    assert len(state["calls"]) == 1

    nachgeholt = service.mahn_service.vervollstaendige_gesendete_mahnlaeufe(ctx=admin_ctx, akteur="recovery-test")
    assert len(nachgeholt) == 1

    mahnfall_danach = service.mahn_repo.get(mahnfall.id)
    assert mahnfall_danach.status == MahnStatus.GESENDET.value
    assert mahnfall_danach.gesendet_am is not None
    # KEIN zweiter Mailversand - die Recovery hat den bereits bestätigten
    # Beleg übernommen, nicht erneut gesendet.
    assert len(state["calls"]) == 1

    with service.sf() as db:
        evidence = list(db.execute(select(AuditEventTable).where(
            AuditEventTable.aktion == "MAILVERSAND_BESTAETIGT",
            AuditEventTable.entity_typ == "mahn_faelle",
        )).scalars())
    assert len(evidence) == 1

    # Stufe 2 ist jetzt tatsächlich erreichbar - vorher wäre sie an der
    # "Stufe 2 verlangt eine erfolgreich gesendete Stufe 1"-Prüfung
    # gescheitert, weil das Mitglied nie als GESENDET erkannt worden wäre.
    service.bank_service.bestaetige_bankvollstaendigkeit(bank_konto_id="SYNTHETIC-BANK",
        bestaetigt_bis=date(2026, 9, 29), bestaetigt_von="SYNTHETIC-TEST")
    state["sent"] = "2026-09-29T08:00:00Z"
    ergebnis_stufe2 = service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 29))
    assert ergebnis_stufe2["gesendet"] == 1
    assert service.mahn_repo.list_fuer_vertrag(contract.id)[0].stufe == 2


def test_codex_interleaved_disjoint_groups_do_not_announce_same_period_fee_twice(hv, basis_vertrag, admin_ctx):
    """Unabhängige Abnahme eb7b8a7, echter Bug (ohne Threads
    reproduziert, verschachtelte Aufrufe statt echter Nebenläufigkeit):
    zwei disjunkte OP-Komponenten DERSELBEN Vorschreibungsperiode landen
    in zwei VERSCHIEDENEN Mahnlauf-Gruppen. Gruppe A wird geplant/
    gebündelt und claimt den Versand; WÄHREND A "in Versand" ist
    (innerhalb ihres eigenen `versand_fn`), wird die GENUIN andere,
    disjunkte Forderung B DERSELBEN Periode gebucht, geplant, gebündelt
    und TATSÄCHLICH gesendet - verschachtelt, nicht threaded. Ohne die
    frühe Reservierung (VOR Text-/Kostenfreeze und Providerkontakt)
    hätten BEIDE Gruppen unabhängig voneinander dieselbe, noch
    unbestätigte §458-Pauschale in ihrem jeweiligen Brief-/Mailtext
    angekündigt, obwohl sie nur EINMAL gebucht werden darf."""

    service, state = hv
    contract, account = basis_vertrag
    state["status"] = "GESENDET"
    policy = service.policy_repo.anlegen(stufe1_tage_nach_faelligkeit=7,
        stufe2_mindesttage_nach_stufe1_versand=14, zinsen_prozent=Decimal("0"), gebuehr_cent=0, status="ENTWURF")
    policy = service.policy_repo.freigeben(policy.id)
    profil = service.mahnkosten_repo.zinsprofil_anlegen(
        vertrag_id=contract.id, ist_b2b=True, vertragsdatum=date(2020, 1, 1),
        mahngebuehr_kostenbasis_cent=4000, mahngebuehr_kostenbasis_beleg="Portokosten-Nachweis", erstellt_von="test",
    )
    service.mahnkosten_repo.zinsprofil_freigeben(profil.id, freigegeben_von="test")
    service.bank_repo.upsert_bank_konto(id="SYNTHETIC-BANK", gesellschaft_id=contract.gesellschaft_id,
        iban="SYNTHETIC-NOT-A-REAL-IBAN", bezeichnung="Testkonto")
    heute = date(2026, 4, 20)
    service.bank_service.bestaetige_bankvollstaendigkeit(bank_konto_id="SYNTHETIC-BANK",
        bestaetigt_bis=heute, bestaetigt_von="SYNTHETIC-TEST")

    def _receipt(ref):
        return MailOpsErgebnis("GESENDET", ref, "SYNTHETIC-SENT", datetime(2026, 4, 20, 10, 0, tzinfo=timezone.utc))

    op_a = service.op_service.buchen(
        ctx=admin_ctx, konto=account, typ=OPTyp.SOLL, betrag_cent=60_000,
        belegdatum=date(2026, 4, 1), buchungsdatum=date(2026, 4, 1),
        faelligkeit=date(2026, 4, 5), leistungsperiode="2026-04", beleg_referenz="Komponente A",
    )
    forderung_a = next(f for f in service.op_service.offene_forderungen(account.id, heute=heute) if f.op_position_id == op_a.id)
    geplant_a = service.mahn_service.plane_forderung(
        ctx=admin_ctx, vertrag=contract, konto=account, forderung=forderung_a, policy=policy, heute=heute,
        bank_bestaetigt_bis=heute, ungeklaerte_eingaenge_vorhanden=False,
    )
    assert geplant_a.status == "GEPLANT"
    mahnlauf_a = service.mahn_service.plane_mahnlauf(
        ctx=admin_ctx, vertrag=contract, konto=account, stufe=1, heute=heute, bank_bestaetigt_bis=heute,
    )
    assert mahnlauf_a is not None

    ergebnis_b_slot: dict = {}

    def versand_fn_a(_mitglieder):
        # WÄHREND A "in Versand" ist: B wird gebucht, geplant, gebündelt
        # und TATSÄCHLICH gesendet - verschachtelt, ohne Threads.
        op_b = service.op_service.buchen(
            ctx=admin_ctx, konto=account, typ=OPTyp.SOLL, betrag_cent=15_000,
            belegdatum=date(2026, 4, 1), buchungsdatum=date(2026, 4, 1),
            faelligkeit=date(2026, 4, 6), leistungsperiode="2026-04", beleg_referenz="Komponente B",
        )
        forderung_b = next(f for f in service.op_service.offene_forderungen(account.id, heute=heute) if f.op_position_id == op_b.id)
        geplant_b = service.mahn_service.plane_forderung(
            ctx=admin_ctx, vertrag=contract, konto=account, forderung=forderung_b, policy=policy, heute=heute,
            bank_bestaetigt_bis=heute, ungeklaerte_eingaenge_vorhanden=False,
        )
        assert geplant_b.status == "GEPLANT"
        mahnlauf_b = service.mahn_service.plane_mahnlauf(
            ctx=admin_ctx, vertrag=contract, konto=account, stufe=1, heute=heute, bank_bestaetigt_bis=heute,
        )
        assert mahnlauf_b is not None
        assert mahnlauf_b.id != mahnlauf_a.id  # genuin disjunkte, ANDERE Gruppe

        ergebnis_b = service.mahn_service.versende_mahnlauf(
            ctx=admin_ctx, mahnlauf_id=mahnlauf_b.id, heute=heute, bank_bestaetigt_bis=heute,
            ungeklaerte_eingaenge_vorhanden=False, send_enabled=True,
            versand_fn=lambda _m: _receipt("mahnungslauf:" + mahnlauf_b.outbox_key),
            mahnkosten_vorschau_slot=ergebnis_b_slot,
        )
        assert ergebnis_b.status == "GESENDET"
        return _receipt("mahnungslauf:" + mahnlauf_a.outbox_key)

    ergebnis_a_slot: dict = {}
    ergebnis_a = service.mahn_service.versende_mahnlauf(
        ctx=admin_ctx, mahnlauf_id=mahnlauf_a.id, heute=heute, bank_bestaetigt_bis=heute,
        ungeklaerte_eingaenge_vorhanden=False, send_enabled=True,
        versand_fn=versand_fn_a, mahnkosten_vorschau_slot=ergebnis_a_slot,
    )
    assert ergebnis_a.status == "GESENDET"

    vorschau_a = ergebnis_a_slot.get("vorschau")
    vorschau_b = ergebnis_b_slot.get("vorschau")
    angekuendigt = [(v.gebuehr_cent or 0) for v in (vorschau_a, vorschau_b) if v is not None]
    # Der eigentliche Bug: BEIDE Vorschauen hätten unabhängig voneinander
    # 4000 Cent angekündigt. Nach dem Fix darf die SUMME der tatsächlich
    # in den (bereits versendeten) Texten angekündigten Pauschalen 4000
    # (einmal) nicht übersteigen.
    assert sum(angekuendigt) <= 4000

    with service.sf() as db:
        gebuehren = list(db.execute(select(MahnkostenGebuehrTable).where(
            MahnkostenGebuehrTable.vertrag_id == contract.id)).scalars())
    assert len(gebuehren) == 1  # GENAU eine Zeile für "PERIODE:2026-04"
    assert sum(g.betrag_cent for g in gebuehren) <= 4000


def test_index_real_mail_protocol_acceptance_is_not_sending_or_rent_change(hv, basis_vertrag, admin_ctx):
    service, state = hv
    contract, _ = basis_vertrag
    row = _seed_index(service, contract, admin_ctx)
    result = service.index_senden(ctx=admin_ctx, row_id=row.id, heute=date(2026, 4, 5))
    assert result.status == "UNKLAR"
    pending = service.bundle.outbox_repository.get(row.id)
    assert pending.versendet_am is None and pending.zugang_bestaetigt_am is None
    body = json.loads(state["calls"][0].content)
    assert body["art"] == "INDEX" and body["freigabe_referenz"].startswith("RECHTSPROFIL:")
    assert service.bundle.stammdaten_repository.get_komponente("SYNTHETIC-HMZ").betrag_cent == 100000
    service.index_senden(ctx=admin_ctx, row_id=row.id, heute=date(2026, 4, 6))
    assert len(state["calls"]) == 1
    state.update(status="GESENDET", sent="2026-04-06T11:12:13Z")
    assert service.status_abgleichen(ctx=admin_ctx) == 1
    confirmed = service.bundle.outbox_repository.get(row.id)
    assert confirmed.versendet_am.isoformat() == "2026-04-06T11:12:13"
    assert confirmed.zugang_bestaetigt_am is None and confirmed.status == "GESENDET"
    assert [r.method for r in state["calls"]] == ["POST", "GET"]


class _FakeSftpVerbindung:
    """Synthetisches Test-Double - niemals echter Netzwerkzugriff (siehe
    `test_brief_sftp.py` für denselben Grundsatz)."""

    def __init__(self, fingerprint: str):
        self._fingerprint = fingerprint
        self.geschriebene_dateien: dict[str, bytes] = {}

    def hostkey_fingerprint(self) -> str:
        return self._fingerprint

    def put_exclusive(self, remote_pfad: str, data: bytes) -> None:
        self.geschriebene_dateien[remote_pfad] = data

    def close(self) -> None:
        pass


def _richte_brief_kanal_ein(service, contract, account):
    regel = service.kanalregel_repo.anlegen(stufe1_kanal="BRIEF", stufe2_kanal="BRIEF", erstellt_von="test")
    service.kanalregel_repo.freigeben(regel.id, freigegeben_von="test")
    profil = service.brief_anbieterprofil_repo.anlegen(
        anbieter_name="EinfachBrief (Test)", quelle_beleg="Testtarif", erstellt_von="test",
    )
    service.brief_anbieterprofil_repo.freigeben(profil.id, freigegeben_von="test")
    service.bundle.stammdaten_repository.upsert_debitor(
        id=account.debitor_id, name="Max Mustermieter", email=None,
        adresse="Musterstraße 1, 1010 Wien", postadresse_geprueft=True,
    )

    fingerprint = "SHA256:test-fingerprint"
    fake_verbindung = _FakeSftpVerbindung(fingerprint)
    konfiguration = SftpKonfiguration(
        host="transfer.post-business-solutions.at", port=22, pfad="bulk_tp/",
        username="jlb-test", password="synthetisches-test-passwort", host_key_fingerprint=fingerprint,
    )
    # Dieselbe Injektionsstelle wie in `test_brief_sftp.py` - niemals ein
    # echter Netzwerkzugriff, auch wenn `brief_transport_verfuegbar` hier
    # für den Test manuell auf `True` gesetzt wird (produktiv bleibt das
    # nur dann wahr, wenn Codex echte Settings-Werte konfiguriert).
    service.brief_sftp_adapter = PostSftpTransportadapter(konfiguration, verbindung_factory=lambda k: fake_verbindung)
    service.mahn_service._brief_transport_verfuegbar = True
    return fake_verbindung


def test_brief_upload_uebermittelt_aber_laesst_gebuehren_mahnstatus_offen(hv, basis_vertrag, admin_ctx, session_factory):
    """Zentraler Codex-Befund 19.10.2026: ein erfolgreicher SFTP-Upload
    ist AUSSCHLIESSLICH ein ÜBERMITTELT-Nachweis gegenüber dem Server -
    der Mahnlauf/Mahnfall bleibt UNSICHER, KEINE Kostenbuchung, KEIN
    GESENDET-Übergang, bis ein authentischer Anbieterbeleg vorliegt."""

    service, _ = hv
    contract, account = basis_vertrag
    _seed_debt(hv, contract, account, admin_ctx)
    fake_verbindung = _richte_brief_kanal_ein(service, contract, account)

    service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))

    row = service.mahn_repo.list_fuer_vertrag(contract.id)[0]
    assert row.status == "UNSICHER"  # NICHT GESENDET

    with session_factory() as s:
        mahnlauf_row = s.execute(select(MahnLaufTable).where(MahnLaufTable.vertrag_id == contract.id)).scalars().one()
    assert mahnlauf_row.kanal == "BRIEF" and mahnlauf_row.status == "UNSICHER"
    assert mahnlauf_row.mahnkosten_verarbeitet_am is None

    archiv = service.mahnbrief_archiv_repo.fuer_mahnlauf(
        vertrag_id=contract.id, stufe=1, mahnlauf_schluessel=mahnlauf_row.outbox_key,
    )
    assert archiv is not None
    assert archiv.pdf_bytes[:5] == b"%PDF-"
    assert archiv.status == "UEBERMITTELT"  # Transport-Nachweis, KEIN Versandabschluss
    assert archiv.status_beleg_referenz is not None
    assert archiv.sende_schluessel  # permanent reserviert

    dateiname = brief_sftp.baue_dateiname(sende_schluessel=archiv.sende_schluessel)
    assert fake_verbindung.geschriebene_dateien == {f"bulk_tp/{dateiname}": archiv.pdf_bytes}

    # Keine Kostenbuchung/kein Versandbeleg - der generische EMAIL-Audit-
    # Mechanismus wurde für diesen Mahnfall NIE ausgelöst.
    with session_factory() as s:
        evidence = list(s.execute(select(AuditEventTable).where(
            AuditEventTable.aktion == "MAILVERSAND_BESTAETIGT", AuditEventTable.entity_typ == "mahn_faelle",
        )).scalars())
    assert evidence == []


def test_echter_beleg_bucht_exakt_den_freigegebenen_kostensnapshot_einmal(hv, basis_vertrag, admin_ctx, session_factory):
    """Belegt tatsächliche Zahlenrichtigkeit statt nur Zählergleichheit
    (Codex-Abnahme 19.10.2026): ein freigegebenes 20-EUR-Gebührenprofil
    PLUS ein vereinbarter, geprüfter Zinssatz liefern einen POSITIVEN
    Zinsdelta UND eine Gebühr von exakt 2000 Cent - der Upload selbst
    bucht NICHTS, der echte Beleg bucht GENAU EINMAL exakt diesen
    Snapshot, ein identischer zweiter Import bucht nichts mehr (kein
    zweiter Upload, keine zweite Ledger-Zeile). Ausschließlich
    synthetische Vertrags-/Kostendaten, keine Produktivbuchung."""

    service, _ = hv
    contract, account = basis_vertrag
    heute = date.today()
    _seed_debt(hv, contract, account, admin_ctx, day=heute)

    profil = service.mahnkosten_repo.zinsprofil_anlegen(
        # ist_b2b=True ist Pflicht für die §458-UGB-Mahngebühr (sonst bleibt
        # gebuehr_cent trotz gesetzter Kostenbasis None/0, siehe Hinweis-
        # text "§458 UGB gilt nur bei beiderseits unternehmensbezogenem
        # Geschäft").
        vertrag_id=contract.id, ist_b2b=True, vertragsdatum=date(2020, 1, 1),
        vereinbarter_zinssatz_prozent=Decimal("5.0"), vereinbarung_geprueft=True,
        vereinbarung_beleg="SYNTHETISCHER Mietvertrag Pkt. 12 (Verzugszinsen)",
        mahngebuehr_kostenbasis_cent=2000, mahngebuehr_kostenbasis_beleg="SYNTHETISCHER Portokosten-Nachweis",
        erstellt_von="codex-test",
    )
    service.mahnkosten_repo.zinsprofil_freigeben(profil.id, freigegeben_von="codex-test")

    _richte_brief_kanal_ein(service, contract, account)
    service.mahnlauf(ctx=admin_ctx, heute=heute)

    row = service.mahn_repo.list_fuer_vertrag(contract.id)[0]
    assert row.status == "UNSICHER"  # Upload selbst bucht NICHTS

    with session_factory() as s:
        mahnlauf_row = s.execute(select(MahnLaufTable).where(MahnLaufTable.vertrag_id == contract.id)).scalars().one()
        gebuehren_vor_beleg = list(s.execute(select(MahnkostenBuchungTable).where(
            MahnkostenBuchungTable.vertrag_id == contract.id)).scalars())
    assert gebuehren_vor_beleg == []  # kein Buchungsversuch durch den bloßen Upload

    archiv = service.mahnbrief_archiv_repo.fuer_mahnlauf(
        vertrag_id=contract.id, stufe=1, mahnlauf_schluessel=mahnlauf_row.outbox_key,
    )
    beleg_pdf = _echter_beleg_pdf("Original-Postaufgabebeleg")

    service.vervollstaendige_mahnlauf_aus_anbieterbeleg(
        ctx=admin_ctx, archiv_id=archiv.id, anbieterbeleg_pdf_bytes=beleg_pdf,
        anbieterbeleg_referenz="PORTAL-AUFTRAG-ECHT-20EUR", postaufgabe_datum=heute,
        importiert_von="codex-test", heute=heute,
    )

    with session_factory() as s:
        buchungen = list(s.execute(select(MahnkostenBuchungTable).where(
            MahnkostenBuchungTable.vertrag_id == contract.id)).scalars())
    assert len(buchungen) == 1
    buchung = buchungen[0]
    assert buchung.gebuehr_cent == 2000  # exakt der freigegebene 20-EUR-Snapshot
    assert buchung.zinsen_cent > 0  # echtes positives Zinsdelta, keine bloße 0-Platzhalterzahl

    # Identischer Import: bucht NICHTS mehr - kein zweiter SFTP-Upload,
    # keine zweite Ledger-Zeile.
    service.vervollstaendige_mahnlauf_aus_anbieterbeleg(
        ctx=admin_ctx, archiv_id=archiv.id, anbieterbeleg_pdf_bytes=beleg_pdf,
        anbieterbeleg_referenz="PORTAL-AUFTRAG-ECHT-20EUR", postaufgabe_datum=heute,
        importiert_von="codex-test", heute=heute,
    )
    with session_factory() as s:
        buchungen_erneut = list(s.execute(select(MahnkostenBuchungTable).where(
            MahnkostenBuchungTable.vertrag_id == contract.id)).scalars())
    assert len(buchungen_erneut) == 1
    assert buchungen_erneut[0].id == buchung.id
    assert buchungen_erneut[0].gebuehr_cent == 2000
    assert buchungen_erneut[0].zinsen_cent == buchung.zinsen_cent


def test_echter_anbieterbeleg_vervollstaendigt_mahnlauf_genau_einmal(hv, basis_vertrag, admin_ctx, session_factory):
    """`archiv.archiviert_am` ist ein ECHTER Wanduhr-Zeitstempel
    (`server_default=func.now()`) - `heute`/`postaufgabe_datum` werden
    deshalb bewusst am tatsächlichen `date.today()` verankert (nicht an
    den sonst in diesem Modul üblichen, weit zurückliegenden
    September-2026-Werten), damit der "Postaufgabedatum nicht vor
    Archivierung"-Schutz nicht fälschlich anschlägt."""

    service, _ = hv
    contract, account = basis_vertrag
    heute = date.today()
    _seed_debt(hv, contract, account, admin_ctx, day=heute)
    _richte_brief_kanal_ein(service, contract, account)
    service.mahnlauf(ctx=admin_ctx, heute=heute)

    with session_factory() as s:
        mahnlauf_row = s.execute(select(MahnLaufTable).where(MahnLaufTable.vertrag_id == contract.id)).scalars().one()
    archiv = service.mahnbrief_archiv_repo.fuer_mahnlauf(
        vertrag_id=contract.id, stufe=1, mahnlauf_schluessel=mahnlauf_row.outbox_key,
    )
    beleg_pdf = _echter_beleg_pdf("Original-Postaufgabebeleg aus dem Post-Portal")

    service.vervollstaendige_mahnlauf_aus_anbieterbeleg(
        ctx=admin_ctx, archiv_id=archiv.id, anbieterbeleg_pdf_bytes=beleg_pdf,
        anbieterbeleg_referenz="PORTAL-AUFTRAG-4711", postaufgabe_datum=heute,
        importiert_von="codex-test", heute=heute,
    )

    row = service.mahn_repo.list_fuer_vertrag(contract.id)[0]
    assert row.status == "GESENDET"
    with session_factory() as s:
        mahnlauf_row = s.execute(select(MahnLaufTable).where(MahnLaufTable.id == mahnlauf_row.id)).scalars().one()
    assert mahnlauf_row.status == "GESENDET"
    assert mahnlauf_row.gesendet_am.date().isoformat() == heute.isoformat()  # ECHTES Postaufgabedatum, nicht der Uploadtag
    assert mahnlauf_row.mahnkosten_verarbeitet_am is not None

    with session_factory() as s:
        gebuehren = list(s.execute(select(MahnkostenGebuehrTable).where(
            MahnkostenGebuehrTable.vertrag_id == contract.id)).scalars())
        evidence = list(s.execute(select(AuditEventTable).where(
            AuditEventTable.aktion == "MAILVERSAND_BESTAETIGT")).scalars())
    assert all(e.payload["zugang_bestaetigt"] is False for e in evidence)  # kein erfundener Zustellnachweis

    # Idempotent: ein zweiter Import mit IDENTISCHEM Beleg vervollständigt
    # genau einmal - keine zweite Buchung, keine zweite Audit-Zeile.
    service.vervollstaendige_mahnlauf_aus_anbieterbeleg(
        ctx=admin_ctx, archiv_id=archiv.id, anbieterbeleg_pdf_bytes=beleg_pdf,
        anbieterbeleg_referenz="PORTAL-AUFTRAG-4711", postaufgabe_datum=heute,
        importiert_von="codex-test", heute=heute,
    )
    with session_factory() as s:
        evidence_erneut = list(s.execute(select(AuditEventTable).where(
            AuditEventTable.aktion == "MAILVERSAND_BESTAETIGT")).scalars())
        gebuehren_erneut = list(s.execute(select(MahnkostenGebuehrTable).where(
            MahnkostenGebuehrTable.vertrag_id == contract.id)).scalars())
    assert len(evidence_erneut) == len(evidence)
    assert len(gebuehren_erneut) == len(gebuehren)


def test_fehlender_oder_vertauschter_beleg_bleibt_offen_kein_fehlerhafter_abschluss(hv, basis_vertrag, admin_ctx, session_factory):
    service, _ = hv
    contract, account = basis_vertrag
    heute = date.today()
    _seed_debt(hv, contract, account, admin_ctx, day=heute)
    _richte_brief_kanal_ein(service, contract, account)
    service.mahnlauf(ctx=admin_ctx, heute=heute)

    with session_factory() as s:
        mahnlauf_row = s.execute(select(MahnLaufTable).where(MahnLaufTable.vertrag_id == contract.id)).scalars().one()
    archiv = service.mahnbrief_archiv_repo.fuer_mahnlauf(
        vertrag_id=contract.id, stufe=1, mahnlauf_schluessel=mahnlauf_row.outbox_key,
    )

    # Referenztext allein ist KEIN Beleg - kein PDF mitgegeben.
    with pytest.raises(ValueError):
        service.mahnbrief_archiv_repo.importiere_anbieterbeleg(
            archiv_id=archiv.id, anbieterbeleg_pdf_bytes=b"", anbieterbeleg_referenz="PORTAL-AUFTRAG-1",
            postaufgabe_datum=heute, importiert_von="codex-test",
        )

    service.vervollstaendige_mahnlauf_aus_anbieterbeleg(
        ctx=admin_ctx, archiv_id=archiv.id, anbieterbeleg_pdf_bytes=_echter_beleg_pdf("echter Beleg"),
        anbieterbeleg_referenz="PORTAL-AUFTRAG-ECHT", postaufgabe_datum=heute,
        importiert_von="codex-test", heute=heute,
    )
    # Ein vertauschter/abweichender Beleg für DIESELBE Zeile wird abgelehnt,
    # der bereits bestätigte Mahnlauf bleibt unangetastet GESENDET.
    with pytest.raises(VorgangIdKonfliktError):
        service.vervollstaendige_mahnlauf_aus_anbieterbeleg(
            ctx=admin_ctx, archiv_id=archiv.id, anbieterbeleg_pdf_bytes=_echter_beleg_pdf("ANDERER Beleg"),
            anbieterbeleg_referenz="PORTAL-AUFTRAG-ANDERS", postaufgabe_datum=heute,
            importiert_von="codex-test", heute=heute,
        )
    row = service.mahn_repo.list_fuer_vertrag(contract.id)[0]
    assert row.status == "GESENDET"
    with session_factory() as s:
        mahnlauf_row = s.execute(select(MahnLaufTable).where(MahnLaufTable.id == mahnlauf_row.id)).scalars().one()
    assert mahnlauf_row.gesendet_am.date().isoformat() == heute.isoformat()  # unverändert, keine Überschreibung


def test_vervollstaendigung_lehnt_zukuenftiges_postaufgabedatum_ab(hv, basis_vertrag, admin_ctx, session_factory):
    service, _ = hv
    contract, account = basis_vertrag
    heute = date.today()
    _seed_debt(hv, contract, account, admin_ctx, day=heute)
    _richte_brief_kanal_ein(service, contract, account)
    service.mahnlauf(ctx=admin_ctx, heute=heute)

    with session_factory() as s:
        mahnlauf_row = s.execute(select(MahnLaufTable).where(MahnLaufTable.vertrag_id == contract.id)).scalars().one()
    archiv = service.mahnbrief_archiv_repo.fuer_mahnlauf(
        vertrag_id=contract.id, stufe=1, mahnlauf_schluessel=mahnlauf_row.outbox_key,
    )
    with pytest.raises(ValueError):
        service.vervollstaendige_mahnlauf_aus_anbieterbeleg(
            ctx=admin_ctx, archiv_id=archiv.id, anbieterbeleg_pdf_bytes=b"%PDF-1.4 Beleg",
            anbieterbeleg_referenz="PORTAL-ZUKUNFT", postaufgabe_datum=heute + timedelta(days=1),
            importiert_von="codex-test", heute=heute,
        )
    row = service.mahn_repo.list_fuer_vertrag(contract.id)[0]
    assert row.status == "UNSICHER"  # unverändert, kein Abschluss


def test_vervollstaendigung_lehnt_postaufgabedatum_vor_archivierung_ab(hv, basis_vertrag, admin_ctx, session_factory):
    service, _ = hv
    contract, account = basis_vertrag
    heute = date.today()
    _seed_debt(hv, contract, account, admin_ctx, day=heute)
    _richte_brief_kanal_ein(service, contract, account)
    service.mahnlauf(ctx=admin_ctx, heute=heute)

    with session_factory() as s:
        mahnlauf_row = s.execute(select(MahnLaufTable).where(MahnLaufTable.vertrag_id == contract.id)).scalars().one()
    archiv = service.mahnbrief_archiv_repo.fuer_mahnlauf(
        vertrag_id=contract.id, stufe=1, mahnlauf_schluessel=mahnlauf_row.outbox_key,
    )
    with pytest.raises(ValueError):
        service.vervollstaendige_mahnlauf_aus_anbieterbeleg(
            ctx=admin_ctx, archiv_id=archiv.id, anbieterbeleg_pdf_bytes=b"%PDF-1.4 Beleg",
            anbieterbeleg_referenz="PORTAL-VERGANGENHEIT", postaufgabe_datum=heute - timedelta(days=2),
            importiert_von="codex-test", heute=heute,
        )
    row = service.mahn_repo.list_fuer_vertrag(contract.id)[0]
    assert row.status == "UNSICHER"  # unverändert, kein Abschluss


def test_fehler_nach_upload_fuehrt_zu_unsicher_nie_zu_blockiert_oder_retry(hv, basis_vertrag, admin_ctx, session_factory):
    """Codex-Abnahme 19.10.2026: ein Fehler NACH möglichem Upload (hier:
    eine fehlgeschlagene Archivstatus-Aktualisierung) darf NIE als
    BLOCKIERT durchgereicht werden - das würde die Mitglieder für einen
    zweiten, unabhängigen Versandversuch freigeben."""

    service, _ = hv
    contract, account = basis_vertrag
    _seed_debt(hv, contract, account, admin_ctx)
    _richte_brief_kanal_ein(service, contract, account)

    orig = service.mahnbrief_archiv_repo.aktualisiere_status

    def _schlaegt_fehl(*args, **kwargs):
        raise RuntimeError("Fake-DB-Fehler NACH dem Upload (Test).")

    service.mahnbrief_archiv_repo.aktualisiere_status = _schlaegt_fehl
    try:
        service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))
    finally:
        service.mahnbrief_archiv_repo.aktualisiere_status = orig

    row = service.mahn_repo.list_fuer_vertrag(contract.id)[0]
    assert row.status == "UNSICHER"  # NIE "BLOCKIERT" trotz Fehler nach dem Upload
    with session_factory() as s:
        mahnlauf_row = s.execute(select(MahnLaufTable).where(MahnLaufTable.vertrag_id == contract.id)).scalars().one()
    assert mahnlauf_row.status == "UNSICHER"

    # Die Bytes wurden trotzdem tatsächlich übertragen (Archivzeile existiert).
    archiv = service.mahnbrief_archiv_repo.fuer_mahnlauf(
        vertrag_id=contract.id, stufe=1, mahnlauf_schluessel=mahnlauf_row.outbox_key,
    )
    assert archiv is not None
    assert archiv.pdf_bytes[:5] == b"%PDF-"


# -- Auftrag HV-20261020-MAHN-FORTSETZUNG: Zahlungsziel aus geprüfter ------
# -- Vertrag/Objekt/Gesellschaft-Bindung (Task 36), tatsächlich belegter ---
# -- Versand an alle bestätigten Mitmieter ----------------------------------


def _zweite_gesellschaft_vertrag(service, *, gesellschaft_id="JLB", gesellschaft_name="JLB Projects GmbH"):
    """Baut eine ZWEITE, von `basis_vertrag` ("7DI") unabhängige
    Gesellschaft samt Objekt/Einheit/Debitor/Vertrag/Konto auf - für Tests,
    die belegen müssen, dass das Zahlungsziel NIE zwischen zwei
    Gesellschaften vermischt wird."""

    st = service.bundle.stammdaten_repository
    st.upsert_gesellschaft(id=gesellschaft_id, name=gesellschaft_name)
    st.upsert_objekt(id=f"{gesellschaft_id}-OBJ", gesellschaft_id=gesellschaft_id, bezeichnung="Zweites Objekt")
    st.upsert_einheit(
        id=f"{gesellschaft_id}-TOP1", objekt_id=f"{gesellschaft_id}-OBJ", bezeichnung="Top 1",
        nutzungsstatus="DAUERVERMIETUNG",
    )
    st.upsert_debitor(id=f"DEB-{gesellschaft_id}", name="Zweite Mieterin", email="zweite@example.at")
    st.upsert_vertrag(
        id=f"V-{gesellschaft_id}-1", einheit_id=f"{gesellschaft_id}-TOP1", debitor_id=f"DEB-{gesellschaft_id}",
        gesellschaft_id=gesellschaft_id, rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    vertrag = st.get_vertrag(f"V-{gesellschaft_id}-1")
    konto = st.get_or_create_konto(vertrag=vertrag)
    return vertrag, konto


def test_zahlungsziel_je_gesellschaft_eindeutig_keine_vermischung_kein_raten(hv, basis_vertrag):
    """Task 36 (core): Zahlungsziel kommt je Gesellschaft aus deren
    EIGENER, eindeutiger Bankkontobindung - niemals aus einer globalen
    Einstellung (die gibt es nicht mehr), niemals geraten/vermischt. Eine
    fehlende ODER mehrdeutige Bindung bleibt explizit None/None."""

    service, _ = hv
    contract_a, _account_a = basis_vertrag
    contract_b, _account_b = _zweite_gesellschaft_vertrag(service)
    st = service.bundle.stammdaten_repository

    service.bank_repo.upsert_bank_konto(
        id="BANK-A", gesellschaft_id=contract_a.gesellschaft_id, iban="AT000000000000000001", bezeichnung="Konto A")
    service.bank_repo.upsert_bank_konto(
        id="BANK-B", gesellschaft_id=contract_b.gesellschaft_id, iban="AT000000000000000002", bezeichnung="Konto B")

    ziel_a = zahlungsziel_fuer_mahnung(
        service.bank_repo, st, gesellschaft_id=contract_a.gesellschaft_id, vertrag_id=contract_a.id, stufe=1)
    ziel_b = zahlungsziel_fuer_mahnung(
        service.bank_repo, st, gesellschaft_id=contract_b.gesellschaft_id, vertrag_id=contract_b.id, stufe=1)
    assert ziel_a.iban == "AT000000000000000001" and ziel_a.kontoinhaber == "7D Immobilien GmbH"
    assert ziel_b.iban == "AT000000000000000002" and ziel_b.kontoinhaber == "JLB Projects GmbH"

    # Fehlende Bindung (keine Gesellschaft/kein Konto) bleibt None/None.
    ziel_unbekannt = zahlungsziel_fuer_mahnung(
        service.bank_repo, st, gesellschaft_id="UNBEKANNT", vertrag_id="V-X", stufe=1)
    assert ziel_unbekannt.iban is None and ziel_unbekannt.kontoinhaber is None

    # Eine MEHRDEUTIGE Bindung (zwei Konten derselben Gesellschaft) bleibt
    # ebenso None/None - nie eine geratene Wahl zwischen beiden.
    service.bank_repo.upsert_bank_konto(
        id="BANK-A-ZWEITES", gesellschaft_id=contract_a.gesellschaft_id, iban="AT000000000000000099",
        bezeichnung="Zweites Konto A")
    ziel_a_mehrdeutig = zahlungsziel_fuer_mahnung(
        service.bank_repo, st, gesellschaft_id=contract_a.gesellschaft_id, vertrag_id=contract_a.id, stufe=1)
    assert ziel_a_mehrdeutig.iban is None and ziel_a_mehrdeutig.kontoinhaber is None


def test_mahnlauf_stoppt_tatsaechlichen_versand_bei_mehrdeutigem_zahlungsziel(hv, basis_vertrag, admin_ctx):
    """Konkreter Abnahmehinweis (Markus 21.10.2026) zu Task 36: eine
    fehlende/mehrdeutige Zahlungszielbindung darf NICHT nur eine Warnung
    plus unspezifischem Kundentext sein - der tatsächliche Versand muss
    dafür als gezielter Prüffall STOPPEN (`blockiert`, nicht
    `gesendet`), damit weder ein falsches noch ein unbrauchbares
    Zahlungsziel gemahnt wird. Erst eine ausdrücklich bestätigte
    Vertragsbindung (nicht "die erste Zeile"/"nach Bezeichnung") löst
    die Mehrdeutigkeit auf und lässt den Versand tatsächlich zu."""

    service, state = hv
    contract, account = basis_vertrag
    _seed_debt(hv, contract, account, admin_ctx)

    # Ein ZWEITES Bankkonto derselben Gesellschaft macht die Bindung
    # mehrdeutig - absichtlich mit einer Bezeichnung, die bei einer
    # naiven "erste Zeile"/"nach Bezeichnung"-Heuristik eher träfe als
    # "SYNTHETIC-BANK", um eine solche Heuristik sicher aufzudecken.
    service.bank_repo.upsert_bank_konto(
        id="AAA-ZUERST", gesellschaft_id=contract.gesellschaft_id, iban="AT000000000000000077", bezeichnung="Hauptkonto")

    ergebnis = service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))
    assert ergebnis["gesendet"] == 0
    assert ergebnis["blockiert"] == 1
    assert len(state["calls"]) == 0  # kein einziger Providerkontakt - kein falsches/unbrauchbares Zahlungsziel gemahnt

    # Transienter Prüffall: NICHT einmal ein MahnFallTable-Eintrag wird
    # angelegt (wie bei jedem anderen `_pruefe_forderung_planbar`-
    # BLOCKIERT-Grund) - kein dauerhaft toter, manuell zu entsperrender
    # Zustand, sobald die Bindung bestätigt ist, wird einfach neu geplant.
    assert service.mahn_repo.list_fuer_vertrag(contract.id) == []

    # Eine ausdrücklich bestätigte Bindung an das ZWEITE Konto (NICHT
    # das zuerst angelegte "SYNTHETIC-BANK", NICHT das nach Bezeichnung
    # "passendere" "Hauptkonto") löst die Mehrdeutigkeit auf.
    service.zahlungsziel_bindung_repo.bestaetigen(
        vertrag_id=contract.id, bank_konto_id="AAA-ZUERST",
        quellenbeleg_referenz="Mietvertrag Anhang 3", bestaetigt_von="codex-test",
    )
    # Das zweite, jetzt gebundene Konto braucht ebenfalls eine aktuelle
    # Bankbestätigung (`bank_freigabe_ableiten` nimmt das Minimum über
    # ALLE Konten der Gesellschaft).
    service.bank_service.bestaetige_bankvollstaendigkeit(bank_konto_id="AAA-ZUERST", bestaetigt_bis=date(2026, 9, 14), bestaetigt_von="SYNTHETIC-TEST")
    service.bank_service.bestaetige_bankvollstaendigkeit(bank_konto_id="SYNTHETIC-BANK", bestaetigt_bis=date(2026, 9, 14), bestaetigt_von="SYNTHETIC-TEST")
    state["status"] = "GESENDET"
    ergebnis2 = service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 14))
    assert ergebnis2["gesendet"] == 1
    body = json.loads(state["calls"][0].content)
    assert "AT000000000000000077" in body["text"]
    assert "SYNTHETIC-NOT-A-REAL-IBAN" not in body["text"]


def test_nachsendung_verweigert_bei_mehrdeutigem_zahlungsziel(hv_mit_tracking, basis_vertrag, admin_ctx):
    """Dasselbe Versand-Gate gilt auch für die sichere Nachsende-Aktion
    (Task 2) - eine Nachsendung mit mehrdeutigem Zahlungsziel wird
    abgelehnt, nicht mit einem unbrauchbaren Zahlungsziel ausgeliefert."""

    service, state = hv_mit_tracking
    contract, account = basis_vertrag
    policy = service.policy_repo.anlegen(stufe1_tage_nach_faelligkeit=7,
        stufe2_mindesttage_nach_stufe1_versand=14, zinsen_prozent=Decimal("0"), gebuehr_cent=0, status="ENTWURF")
    service.policy_repo.freigeben(policy.id)
    service.op_service.buchen(ctx=admin_ctx, konto=account, typ=OPTyp.SOLL, betrag_cent=50_000,
        belegdatum=date(2026, 9, 1), buchungsdatum=date(2026, 9, 1), faelligkeit=date(2026, 9, 5), beleg_referenz="Sept")
    service.bank_repo.upsert_bank_konto(
        id="BANK-1", gesellschaft_id=contract.gesellschaft_id, iban="AT01", bezeichnung="Konto 1")
    service.bank_service.bestaetige_bankvollstaendigkeit(bank_konto_id="BANK-1", bestaetigt_bis=date(2026, 9, 13), bestaetigt_von="TEST")
    m = service.mitmieter_repo.anlegen(
        vertrag_id=contract.id, name="Mitmieterin", email="mitmieterin@example.at", adresse=None,
        erstellt_von="test", rolle="MITMIETER", zustand="BESTAETIGT")

    state["fehlschlagen_beim_ersten_versuch"] = {"mitmieterin@example.at"}
    service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))
    mahnlauf = service.mahnlauf_repo.list_fuer_vertrag(contract.id)[0]

    # Erst JETZT, nach dem erfolgreichen Erstversand, wird die Bindung
    # mehrdeutig (zweites Bankkonto taucht nachträglich auf).
    service.bank_repo.upsert_bank_konto(
        id="BANK-2", gesellschaft_id=contract.gesellschaft_id, iban="AT02", bezeichnung="Konto 2")

    with pytest.raises(Exception) as exc_info:
        service.nachsenden_einzelempfaenger(ctx=admin_ctx, mahnlauf_id=mahnlauf.id, mitmieter_id=m.id, heute=date(2026, 9, 14))
    from mietinkasso.domain.exceptions import ZahlungszielUngeklaertError
    assert isinstance(exc_info.value, ZahlungszielUngeklaertError)


def test_mahnlauf_zwei_gesellschaften_email_verwendet_jeweils_eigene_iban(hv, basis_vertrag, admin_ctx):
    """Task 36: zwei Gesellschaften mit abweichenden IBANs - der
    tatsächlich gesendete E-Mail-Text jeder Gesellschaft nennt
    AUSSCHLIESSLICH ihre EIGENE IBAN, nie die der anderen."""

    service, state = hv
    contract_a, account_a = basis_vertrag
    contract_b, account_b = _zweite_gesellschaft_vertrag(service)
    policy = service.policy_repo.anlegen(stufe1_tage_nach_faelligkeit=7,
        stufe2_mindesttage_nach_stufe1_versand=14, zinsen_prozent=Decimal("0"), gebuehr_cent=0, status="ENTWURF")
    service.policy_repo.freigeben(policy.id)

    service.op_service.buchen(ctx=admin_ctx, konto=account_a, typ=OPTyp.SOLL, betrag_cent=50_000,
        belegdatum=date(2026, 9, 1), buchungsdatum=date(2026, 9, 1), faelligkeit=date(2026, 9, 5), beleg_referenz="A Sept")
    service.op_service.buchen(ctx=admin_ctx, konto=account_b, typ=OPTyp.SOLL, betrag_cent=70_000,
        belegdatum=date(2026, 9, 1), buchungsdatum=date(2026, 9, 1), faelligkeit=date(2026, 9, 5), beleg_referenz="B Sept")

    service.bank_repo.upsert_bank_konto(
        id="BANK-A", gesellschaft_id=contract_a.gesellschaft_id, iban="AT000000000000000001", bezeichnung="Konto A")
    service.bank_repo.upsert_bank_konto(
        id="BANK-B", gesellschaft_id=contract_b.gesellschaft_id, iban="AT000000000000000002", bezeichnung="Konto B")
    service.bank_service.bestaetige_bankvollstaendigkeit(bank_konto_id="BANK-A", bestaetigt_bis=date(2026, 9, 13), bestaetigt_von="TEST")
    service.bank_service.bestaetige_bankvollstaendigkeit(bank_konto_id="BANK-B", bestaetigt_bis=date(2026, 9, 13), bestaetigt_von="TEST")

    result = service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))
    assert result["geplant"] == 2
    assert len(state["calls"]) == 2

    texte = [json.loads(c.content)["text"] for c in state["calls"]]
    text_a = next(t for t in texte if "500,00 EUR" in t)
    text_b = next(t for t in texte if "700,00 EUR" in t)
    assert "AT000000000000000001" in text_a and "AT000000000000000002" not in text_a
    assert "AT000000000000000002" in text_b and "AT000000000000000001" not in text_b


def test_mahnlauf_zwei_gesellschaften_brief_verwendet_jeweils_eigene_iban(hv, basis_vertrag, admin_ctx):
    """Task 36: dieselbe Zwei-Gesellschaften-Garantie auch für den
    Briefkanal - das archivierte PDF jeder Gesellschaft nennt nur ihre
    EIGENE IBAN."""

    service, _state = hv
    contract_a, account_a = basis_vertrag
    contract_b, account_b = _zweite_gesellschaft_vertrag(service)
    _seed_debt(hv, contract_a, account_a, admin_ctx)
    service.op_service.buchen(ctx=admin_ctx, konto=account_b, typ=OPTyp.SOLL, betrag_cent=70_000,
        belegdatum=date(2026, 9, 1), buchungsdatum=date(2026, 9, 1), faelligkeit=date(2026, 9, 5), beleg_referenz="B Sept")
    service.bank_repo.upsert_bank_konto(
        id="BANK-B", gesellschaft_id=contract_b.gesellschaft_id, iban="AT000000000000000002", bezeichnung="Konto B")
    service.bank_service.bestaetige_bankvollstaendigkeit(bank_konto_id="BANK-B", bestaetigt_bis=date(2026, 9, 13), bestaetigt_von="TEST")

    _richte_brief_kanal_ein(service, contract_a, account_a)
    service.bundle.stammdaten_repository.upsert_debitor(
        id=account_b.debitor_id, name="Zweite Mieterin", email=None,
        adresse="Zweitstraße 2, 2020 Wien", postadresse_geprueft=True,
    )

    service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))

    mahnlauf_a = service.mahnlauf_repo.list_fuer_vertrag(contract_a.id)[0]
    mahnlauf_b = service.mahnlauf_repo.list_fuer_vertrag(contract_b.id)[0]
    archiv_a = service.mahnbrief_archiv_repo.fuer_mahnlauf(vertrag_id=contract_a.id, stufe=1, mahnlauf_schluessel=mahnlauf_a.outbox_key)
    archiv_b = service.mahnbrief_archiv_repo.fuer_mahnlauf(vertrag_id=contract_b.id, stufe=1, mahnlauf_schluessel=mahnlauf_b.outbox_key)
    assert archiv_a is not None and archiv_b is not None
    text_a = " ".join(p.extract_text() for p in PdfReader(io.BytesIO(archiv_a.pdf_bytes)).pages)
    text_b = " ".join(p.extract_text() for p in PdfReader(io.BytesIO(archiv_b.pdf_bytes)).pages)
    assert "SYNTHETIC-NOT-A-REAL-IBAN" in text_a
    assert "AT000000000000000002" in text_b
    assert "AT000000000000000002" not in text_a
    assert "SYNTHETIC-NOT-A-REAL-IBAN" not in text_b


def test_ohne_bestaetigte_mitmieter_bleibt_einzelempfaenger_versand_unveraendert(hv, basis_vertrag, admin_ctx):
    """Regression: OHNE bestätigte Mitmieter bleibt der Versand
    unverändert zum bisherigen Einempfänger-Pfad - GENAU EIN
    Provideraufruf, GENAU EIN Empfänger."""

    service, state = hv
    contract, account = basis_vertrag
    _seed_debt(hv, contract, account, admin_ctx)
    result = service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))
    assert result["geplant"] == 1
    assert len(state["calls"]) == 1
    body = json.loads(state["calls"][0].content)
    assert body["empfaenger_email"] == "mieter@example.at"


def test_bestaetigte_mitmieter_bekommen_eigene_kopie_teilausfall_ohne_doppelversand(hv, basis_vertrag, admin_ctx):
    """Auftrag HV-20261020-MAHN-FORTSETZUNG: tatsächlich belegter Versand
    an ALLE bestätigten Mitmieter - ein fehlschlagender Kopieversand
    blockiert weder den Hauptversand noch einen ANDEREN Mitmieter (jede
    Person wird GENAU EINMAL versucht), und ein erneuter Lauf für dieselbe,
    bereits versandversuchte Gruppe löst KEINEN zweiten Kopieversand aus."""

    service, state = hv
    contract, account = basis_vertrag
    state["status"] = "GESENDET"
    _seed_debt(hv, contract, account, admin_ctx)

    m_ok = service.mitmieter_repo.anlegen(
        vertrag_id=contract.id, name="Mitmieterin A", email="mitmieterin-a@example.at", adresse=None,
        erstellt_von="test", rolle="MITMIETER", zustand="BESTAETIGT")
    m_fail = service.mitmieter_repo.anlegen(
        vertrag_id=contract.id, name="Mitmieter B", email="mitmieter-b@example.at", adresse=None,
        erstellt_von="test", rolle="MITMIETER", zustand="BESTAETIGT")
    # Kein Versand an eine noch ungeklärte Angabe oder an VERTRETER (nie
    # automatisch mitadressiert, siehe `list_bestaetigte_mitmieter`-Docstring).
    service.mitmieter_repo.anlegen(
        vertrag_id=contract.id, name="Ungeklärt", email="ungeklaert@example.at", adresse=None,
        erstellt_von="test", rolle="MITMIETER", zustand="AUSGELESEN")
    service.mitmieter_repo.anlegen(
        vertrag_id=contract.id, name="Vertreterin", email="vertreterin@example.at", adresse=None,
        erstellt_von="test", rolle="VERTRETER", zustand="BESTAETIGT")

    original_senden = service.client.senden

    def gezielt_fehlschlagend(auftrag):
        if auftrag.empfaenger_email == "mitmieter-b@example.at":
            raise RuntimeError("Simulierter Providerfehler NUR für diesen Empfänger (Test).")
        return original_senden(auftrag)

    service.client.senden = gezielt_fehlschlagend
    result = service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))
    assert result["geplant"] == 1

    empfaenger = {json.loads(c.content)["empfaenger_email"] for c in state["calls"]}
    assert empfaenger == {"mieter@example.at", "mitmieterin-a@example.at"}
    assert len(state["calls"]) == 2  # Hauptversand + GENAU EINE erfolgreiche Mitmieter-Kopie

    mahnlauf = service.mahnlauf_repo.list_fuer_vertrag(contract.id)[0]
    zugang_eintraege = service.empfaenger_zugang_repo.fuer_mahnlauf(mahnlauf.id)
    eintrag_a = next(e for e in zugang_eintraege if e.mitmieter_id == m_ok.id)
    eintrag_b = next(e for e in zugang_eintraege if e.mitmieter_id == m_fail.id)
    assert eintrag_a.versand_belegt_am is not None and eintrag_a.versand_referenz
    assert eintrag_b.versand_belegt_am is None  # Fehlversuch bleibt sichtbar, kein erfundener Beleg

    # Ein erneuter Lauf für dieselbe (bereits versandversuchte, UNSICHERE)
    # Gruppe darf NIEMALS eine zweite Kopie an Mitmieterin A auslösen -
    # auch nicht, nachdem der simulierte Providerfehler behoben ist.
    service.client.senden = original_senden
    service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 14))
    assert len(state["calls"]) == 2


def test_mitmieter_kopiennachweis_bleibt_durabel_auch_bei_absturz_nach_dem_versand(
    hv, basis_vertrag, admin_ctx, monkeypatch,
):
    """Nachprüfung (Markus 20.10.2026, synthetischer Outcome-Test): das
    Mitmieter-Versandbookkeeping darf NICHT erst in einem Schritt NACH
    `versende_mahnlauf()` geschrieben werden (dort wäre es bei einem
    Absturz/einer Exception ZWISCHEN bestätigtem Versand und Kosten-
    buchung verloren, obwohl die Kopie tatsächlich bereits beim Provider
    angenommen wurde) - es wird jetzt UNMITTELBAR nach dem einzelnen
    Mitmieter-Sendeversuch INNERHALB von `versand_fn` selbst eingefroren,
    bevor überhaupt etwas anderes passieren kann. Reproduziert exakt
    denselben Absturzpunkt wie `test_absturz_zwischen_bestaetigtem_versand_
    und_kostenbuchung_wird_anhand_snapshot_nachgeholt`, diesmal mit einem
    bestätigten Mitmieter."""

    service, state = hv
    contract, account = basis_vertrag
    state["status"] = "GESENDET"
    _seed_zwei_komponenten(hv, contract, account, admin_ctx, faelligkeit=date(2026, 1, 5))
    _bank_bestaetigen(service, contract, date(2026, 3, 1))
    profil = service.mahnkosten_repo.zinsprofil_anlegen(
        vertrag_id=contract.id, ist_b2b=False, vertragsdatum=None, erstellt_von="test",
    )
    service.mahnkosten_repo.zinsprofil_freigeben(profil.id, freigegeben_von="test")
    mitmieter = service.mitmieter_repo.anlegen(
        vertrag_id=contract.id, name="Mitmieterin A", email="mitmieterin-a@example.at", adresse=None,
        erstellt_von="test", rolle="MITMIETER", zustand="BESTAETIGT")

    original_buche_vorschau = service.mahnkosten_service.buche_vorschau
    aufrufe = {"anzahl": 0}

    def kaputte_buchung(*args, **kwargs):
        aufrufe["anzahl"] += 1
        if aufrufe["anzahl"] == 1:
            raise RuntimeError("Simulierter Absturz NACH bestätigtem Versand (inkl. Mitmieter-Kopie), VOR der Kostenbuchung.")
        return original_buche_vorschau(*args, **kwargs)

    monkeypatch.setattr(service.mahnkosten_service, "buche_vorschau", kaputte_buchung)
    with pytest.raises(RuntimeError):
        service.mahnlauf(ctx=admin_ctx, heute=date(2026, 3, 1))
    monkeypatch.undo()

    # Beide Nachrichten (Hauptversand + Mitmieter-Kopie) wurden TATSÄCHLICH
    # an den Provider gesendet, BEVOR der simulierte Absturz eintrat.
    empfaenger = {json.loads(c.content)["empfaenger_email"] for c in state["calls"]}
    assert empfaenger == {"mieter@example.at", "mitmieterin-a@example.at"}
    anzahl_nach_absturz = len(state["calls"])

    mahnlauf = service.mahnlauf_repo.list_fuer_vertrag(contract.id)[0]
    eintrag = next(e for e in service.empfaenger_zugang_repo.fuer_mahnlauf(mahnlauf.id) if e.mitmieter_id == mitmieter.id)
    # ENTSCHEIDEND: trotz des Absturzes (NACH dem Versand, VOR der
    # Kostenbuchung) ist der Mitmieter-Kopiennachweis bereits durabel
    # vorhanden - er wurde NICHT erst nachträglich nach einem
    # erfolgreichen `versende_mahnlauf()`-Rückgabewert geschrieben.
    assert eintrag.versand_belegt_am is not None
    assert eintrag.versand_referenz

    # Die reguläre Recovery (holt NUR die Kostenbuchung nach, sendet NIE
    # erneut) ändert daran nichts und löst KEINEN zweiten Kopieversand aus.
    service.mahn_service.vervollstaendige_gesendete_mahnlaeufe(ctx=admin_ctx, akteur="recovery-test")
    assert len(state["calls"]) == anzahl_nach_absturz
    eintrag_nach_recovery = next(
        e for e in service.empfaenger_zugang_repo.fuer_mahnlauf(mahnlauf.id) if e.mitmieter_id == mitmieter.id
    )
    assert eintrag_nach_recovery.versand_belegt_am == eintrag.versand_belegt_am
    assert eintrag_nach_recovery.versand_referenz == eintrag.versand_referenz


# -- Auftrag HV-20261021-ABSCHLUSS: Briefsendung an ALLE bestätigten -------
# -- Mitmieter (eigenes PDF, Anschrift, Sendeschlüssel, Anbieterbeleg) ------


def test_brief_erreicht_alle_bestaetigten_mitmieter_mit_eigenem_pdf_und_sendeschluessel(
    hv, basis_vertrag, admin_ctx,
):
    """Task 1 (HV-20261021-ABSCHLUSS): JEDER aktive, bestätigte Mitmieter
    mit geprüfter Postadresse bekommt ein EIGENES Mahnbrief-PDF unter
    einem EIGENEN, permanenten Sendeschlüssel - ein Vertreter, eine noch
    ungeklärte Angabe und ein bestätigter Mitmieter OHNE geprüfte
    Postadresse werden NIE automatisch mitadressiert."""

    service, _state = hv
    contract, account = basis_vertrag
    _seed_debt(hv, contract, account, admin_ctx)
    fake_verbindung = _richte_brief_kanal_ein(service, contract, account)

    m_ok = service.mitmieter_repo.anlegen(
        vertrag_id=contract.id, name="Mitmieterin A", email=None, adresse="Nebenstraße 2, 1010 Wien",
        erstellt_von="test", rolle="MITMIETER", zustand="BESTAETIGT")
    service.mitmieter_repo.setze_postadresse_geprueft(m_ok.id, geprueft=True)
    m_ohne_geprueft = service.mitmieter_repo.anlegen(
        vertrag_id=contract.id, name="Mitmieter ungeprüft", email=None, adresse="Ungeprüfte Gasse 3, 1010 Wien",
        erstellt_von="test", rolle="MITMIETER", zustand="BESTAETIGT")
    service.mitmieter_repo.anlegen(
        vertrag_id=contract.id, name="Ungeklärt", email=None, adresse="Irrelevant 4, 1010 Wien",
        erstellt_von="test", rolle="MITMIETER", zustand="AUSGELESEN")
    m_vertreter = service.mitmieter_repo.anlegen(
        vertrag_id=contract.id, name="Vertreter", email=None, adresse="Kanzleigasse 5, 1010 Wien",
        erstellt_von="test", rolle="VERTRETER", zustand="BESTAETIGT")
    service.mitmieter_repo.setze_postadresse_geprueft(m_vertreter.id, geprueft=True)

    service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))

    mahnlauf = service.mahnlauf_repo.list_fuer_vertrag(contract.id)[0]
    eintraege = {e.mitmieter_id: e for e in service.empfaenger_zugang_repo.fuer_mahnlauf(mahnlauf.id)}

    eintrag_a = eintraege[m_ok.id]
    assert eintrag_a.brief_status == "UEBERMITTELT"
    assert eintrag_a.brief_pdf_bytes[:5] == b"%PDF-"
    assert eintrag_a.brief_sende_schluessel

    eintrag_ohne_geprueft = eintraege[m_ohne_geprueft.id]
    assert eintrag_ohne_geprueft.brief_status is None  # keine geprüfte Postadresse -> kein Versand

    assert m_vertreter.id not in eintraege  # Vertreter ist nie Teil der erwarteten Empfänger überhaupt

    archiv_hauptbrief = service.mahnbrief_archiv_repo.fuer_mahnlauf(
        vertrag_id=contract.id, stufe=1, mahnlauf_schluessel=mahnlauf.outbox_key,
    )
    dateiname_haupt = brief_sftp.baue_dateiname(sende_schluessel=archiv_hauptbrief.sende_schluessel)
    dateiname_mitmieter = brief_sftp.baue_dateiname(sende_schluessel=eintrag_a.brief_sende_schluessel)
    assert dateiname_haupt != dateiname_mitmieter  # zwei EIGENE Sendeschlüssel/Dateinamen
    assert set(fake_verbindung.geschriebene_dateien) == {f"bulk_tp/{dateiname_haupt}", f"bulk_tp/{dateiname_mitmieter}"}
    assert fake_verbindung.geschriebene_dateien[f"bulk_tp/{dateiname_mitmieter}"] == eintrag_a.brief_pdf_bytes


def test_mitmieter_brief_anbieterbeleg_import_ist_unabhaengig_vom_hauptbrief(hv, basis_vertrag, admin_ctx):
    """Task 1: ein Anbieterbeleg-Import für EINEN Mitmieter-Brief ist eine
    rein dokumentarische, eigene Evidenz - treibt NIE die Gruppen-
    Kostenbuchung/den GESENDET-Übergang (das bleibt ausschließlich dem
    Hauptdebitor-Anbieterbeleg vorbehalten, "keine doppelte
    Gebührenbuchung"). Ein Retry mit identischem Beleg ist ein No-Op, ein
    abweichender Beleg für dieselbe Referenz ein Konflikt."""

    service, _state = hv
    contract, account = basis_vertrag
    _seed_debt(hv, contract, account, admin_ctx)
    _richte_brief_kanal_ein(service, contract, account)

    mitmieter = service.mitmieter_repo.anlegen(
        vertrag_id=contract.id, name="Mitmieterin A", email=None, adresse="Nebenstraße 2, 1010 Wien",
        erstellt_von="test", rolle="MITMIETER", zustand="BESTAETIGT")
    service.mitmieter_repo.setze_postadresse_geprueft(mitmieter.id, geprueft=True)

    service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))
    mahnlauf = service.mahnlauf_repo.list_fuer_vertrag(contract.id)[0]
    eintrag = next(e for e in service.empfaenger_zugang_repo.fuer_mahnlauf(mahnlauf.id) if e.mitmieter_id == mitmieter.id)
    assert eintrag.brief_status == "UEBERMITTELT"
    assert eintrag.brief_anbieterbeleg_referenz is None  # noch kein echter Zustellnachweis

    beleg_pdf = _echter_beleg_pdf("Mitmieter-Anbieterbeleg")
    aktualisiert = service.empfaenger_zugang_repo.importiere_brief_anbieterbeleg(
        eintrag.id, anbieterbeleg_pdf_bytes=beleg_pdf, anbieterbeleg_referenz="POST-REF-MITMIETER-1",
        postaufgabe_datum=date(2026, 9, 13), importiert_von="codex-test",
    )
    assert aktualisiert.brief_anbieterbeleg_referenz == "POST-REF-MITMIETER-1"
    assert aktualisiert.brief_anbieterbeleg_pdf_bytes == beleg_pdf

    # Echter Retry mit identischem Beleg: No-Op.
    erneut = service.empfaenger_zugang_repo.importiere_brief_anbieterbeleg(
        eintrag.id, anbieterbeleg_pdf_bytes=beleg_pdf, anbieterbeleg_referenz="POST-REF-MITMIETER-1",
        postaufgabe_datum=date(2026, 9, 13), importiert_von="codex-test",
    )
    assert erneut.brief_anbieterbeleg_importiert_am == aktualisiert.brief_anbieterbeleg_importiert_am

    # Abweichender Beleg für dieselbe Referenz: Konflikt, kein stiller Erfolg.
    with pytest.raises(VorgangIdKonfliktError):
        service.empfaenger_zugang_repo.importiere_brief_anbieterbeleg(
            eintrag.id, anbieterbeleg_pdf_bytes=_echter_beleg_pdf("Abweichender Beleg"),
            anbieterbeleg_referenz="POST-REF-MITMIETER-1", postaufgabe_datum=date(2026, 9, 14),
            importiert_von="codex-test",
        )

    # Das Hauptdebitor-Mahnbrief-Archiv ist davon völlig unberührt -
    # bleibt weiterhin UNSICHER, keine Kostenbuchung ausgelöst.
    with service.sf() as db:
        from mietinkasso.infrastructure.db.tables import MahnFallTable as _MF
        mahnfall = db.execute(select(_MF).where(_MF.vertrag_id == contract.id)).scalars().first()
    assert mahnfall.status == "UNSICHER"


# -- Auftrag HV-20261021-ABSCHLUSS, Task 2: sichere Nachsende-Aktion -------
# -- (Status erst klären, nie erfolgreich Gesendete wiederholen, keine -----
# -- doppelte Gebührenbuchung) ----------------------------------------------


@pytest.fixture
def hv_mit_tracking(session_factory, basis_vertrag, tmp_path):
    """Wie `hv`, aber mit einem GET/POST-bewussten Mock-Provider, der
    TATSÄCHLICH nachhält, welche Referenzen je wirklich beim Provider
    angenommen wurden - nötig, um `status_abfragen` (NICHT_GEFUNDEN vs.
    GESENDET) realistisch zu testen ("ungewisser Providerstatus erst
    abklären"). `state["fehlschlagen_beim_ersten_versuch"]` simuliert
    einen echten Transportfehler NUR beim allerersten POST-Versuch einer
    Referenz für die genannten Empfänger-E-Mail-Adressen - ein späterer
    Versuch MIT DERSELBEN Referenz (Nachsendung) gelingt danach
    tatsächlich, genau wie bei einem echten, idempotenten Mailserver."""

    token = tmp_path / "synthetic-token"
    token.write_text("SYNTHETIC-TOKEN", encoding="utf-8")
    state = {"calls": [], "angenommen": set(), "bereits_versucht": set(), "fehlschlagen_beim_ersten_versuch": set()}

    def provider(request):
        state["calls"].append(request)
        payload = json.loads(request.content) if request.method == "POST" else None
        referenz = payload["referenz"] if payload else request.url.params["referenz"]
        external = "JLBHV-" + hashlib.sha256(json.dumps(referenz, ensure_ascii=False).encode()).hexdigest()
        if request.method == "POST":
            if referenz not in state["bereits_versucht"]:
                state["bereits_versucht"].add(referenz)
                if payload.get("empfaenger_email") in state["fehlschlagen_beim_ersten_versuch"]:
                    return httpx.Response(500)
            state["angenommen"].add(referenz)
            return httpx.Response(200, json={"status": "GESENDET", "externe_referenz": external,
                "provider_referenz": "SYNTHETIC-SENT-TRACK", "versendet_am": "2026-09-14T22:30:00Z"})
        if referenz in state["angenommen"]:
            return httpx.Response(200, json={"status": "GESENDET", "externe_referenz": external,
                "provider_referenz": "SYNTHETIC-SENT-TRACK", "versendet_am": "2026-09-14T22:30:00Z"})
        return httpx.Response(200, json={"status": "NICHT_GEFUNDEN"})

    client = MailOpsClient(token_file=token, transport_factory=lambda: httpx.MockTransport(provider))
    settings = Settings(owner_email="mb@jlb-immo.at", send_enabled=True, hv_mail_allowlist_bestaetigt=True,
        indexautomatik_send_enabled=True, vertragsende_erinnerung_send_enabled=True)
    bundle = bauen(session_factory, settings)
    return HVMailversandService(session_factory, bundle, settings, client=client), state


def test_nachsendung_email_fuer_nachweislich_fehlgeschlagenen_empfaenger_erst_status_klaeren(
    hv_mit_tracking, basis_vertrag, admin_ctx,
):
    """Task 2: der ERSTE Kopieversand an die Mitmieterin schlägt
    (simuliert) am Transport fehl - `nachsenden_einzelempfaenger` klärt
    ERST den Providerstatus (NICHT_GEFUNDEN - nachweislich nie
    angenommen), sendet DANN sicher nach, und ein erneuter Aufruf für
    dieselbe, jetzt belegte Person ist ein reiner No-Op (kein zweiter
    POST, "erfolgreich gesendete Empfänger nie wiederholen")."""

    service, state = hv_mit_tracking
    contract, account = basis_vertrag
    _seed_debt(hv_mit_tracking, contract, account, admin_ctx)
    mitmieter = service.mitmieter_repo.anlegen(
        vertrag_id=contract.id, name="Mitmieterin A", email="mitmieterin-a@example.at", adresse=None,
        erstellt_von="test", rolle="MITMIETER", zustand="BESTAETIGT")
    state["fehlschlagen_beim_ersten_versuch"].add("mitmieterin-a@example.at")

    service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))
    mahnlauf = service.mahnlauf_repo.list_fuer_vertrag(contract.id)[0]
    assert mahnlauf.status == "GESENDET"  # Hauptversand unberührt vom Mitmieter-Fehlschlag
    eintrag_vor = next(e for e in service.empfaenger_zugang_repo.fuer_mahnlauf(mahnlauf.id) if e.mitmieter_id == mitmieter.id)
    assert eintrag_vor.versand_belegt_am is None  # tatsächlich fehlgeschlagen, kein erfundener Beleg

    anzahl_vor_nachsendung = len(state["calls"])
    ergebnis = service.nachsenden_einzelempfaenger(ctx=admin_ctx, mahnlauf_id=mahnlauf.id, mitmieter_id=mitmieter.id, heute=date(2026, 9, 14))
    assert ergebnis == "GESENDET"
    # GENAU zwei neue Anfragen: die Status-Abklärung (GET) + der eine neue Sendeversuch (POST).
    assert len(state["calls"]) == anzahl_vor_nachsendung + 2
    eintrag_nach = next(e for e in service.empfaenger_zugang_repo.fuer_mahnlauf(mahnlauf.id) if e.mitmieter_id == mitmieter.id)
    assert eintrag_nach.versand_belegt_am is not None and eintrag_nach.versand_referenz

    # Ein erneuter Nachsende-Versuch für dieselbe, jetzt bereits belegte
    # Person löst KEINEN zweiten POST aus - reiner No-Op.
    anzahl_nach_erster_nachsendung = len(state["calls"])
    ergebnis_erneut = service.nachsenden_einzelempfaenger(ctx=admin_ctx, mahnlauf_id=mahnlauf.id, mitmieter_id=mitmieter.id, heute=date(2026, 9, 15))
    assert ergebnis_erneut == "BEREITS_GESENDET"
    assert len(state["calls"]) == anzahl_nach_erster_nachsendung  # kein weiterer Providerkontakt


def test_nachsendung_email_erkennt_bereits_tatsaechlich_gesendet_und_sendet_nicht_doppelt(
    hv_mit_tracking, basis_vertrag, admin_ctx,
):
    """Task 2: der Kopieversand wurde beim Provider TATSÄCHLICH
    angenommen, aber lokal (z. B. durch einen Verbindungsabbruch NACH
    Serverannahme) nie als belegt vermerkt. `nachsenden_einzelempfaenger`
    erkennt das über `status_abfragen` (Status GESENDET) und trägt es NUR
    nachträglich ein - sendet NIE ein zweites Mal."""

    service, state = hv_mit_tracking
    contract, account = basis_vertrag
    _seed_debt(hv_mit_tracking, contract, account, admin_ctx)
    mitmieter = service.mitmieter_repo.anlegen(
        vertrag_id=contract.id, name="Mitmieterin A", email="mitmieterin-a@example.at", adresse=None,
        erstellt_von="test", rolle="MITMIETER", zustand="BESTAETIGT")

    service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))
    mahnlauf = service.mahnlauf_repo.list_fuer_vertrag(contract.id)[0]
    eintrag_vor = next(e for e in service.empfaenger_zugang_repo.fuer_mahnlauf(mahnlauf.id) if e.mitmieter_id == mitmieter.id)
    assert eintrag_vor.versand_belegt_am is not None  # beim Erstversand bereits erfolgreich belegt

    # Simuliert den "lokal nie belegt, aber tatsächlich angenommen"-Fall:
    # Nachweis künstlich zurücksetzen, OHNE die Provider-Tracking-Menge
    # ("angenommen") zu verändern - `status_abfragen` sieht weiterhin GESENDET.
    with service.sf() as db:
        from mietinkasso.infrastructure.db.tables import MahnEmpfaengerZugangTable as _MEZ
        row = db.get(_MEZ, eintrag_vor.id)
        row.versand_belegt_am = None
        row.versand_referenz = None
        db.commit()

    anzahl_vor = len(state["calls"])
    ergebnis = service.nachsenden_einzelempfaenger(ctx=admin_ctx, mahnlauf_id=mahnlauf.id, mitmieter_id=mitmieter.id, heute=date(2026, 9, 14))
    assert ergebnis == "BEREITS_GESENDET"
    assert len(state["calls"]) == anzahl_vor + 1  # NUR die Status-Abfrage (GET), KEIN neuer POST
    eintrag_nach = next(e for e in service.empfaenger_zugang_repo.fuer_mahnlauf(mahnlauf.id) if e.mitmieter_id == mitmieter.id)
    assert eintrag_nach.versand_belegt_am is not None  # nachträglich korrekt belegt


def test_nachsendung_blockiert_solange_providerstatus_ungewiss(hv_mit_tracking, basis_vertrag, admin_ctx, monkeypatch):
    """Task 2: solange der Providerstatus selbst nicht eindeutig geklärt
    werden kann (Transportfehler bei der Abfrage), wird NIE automatisch
    nachgesendet."""

    from mietinkasso.domain.exceptions import TransportFehlerUngewissError

    service, state = hv_mit_tracking
    contract, account = basis_vertrag
    _seed_debt(hv_mit_tracking, contract, account, admin_ctx)
    mitmieter = service.mitmieter_repo.anlegen(
        vertrag_id=contract.id, name="Mitmieterin A", email="mitmieterin-a@example.at", adresse=None,
        erstellt_von="test", rolle="MITMIETER", zustand="BESTAETIGT")
    state["fehlschlagen_beim_ersten_versuch"].add("mitmieterin-a@example.at")
    service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))
    mahnlauf = service.mahnlauf_repo.list_fuer_vertrag(contract.id)[0]

    def kaputte_statusabfrage(referenz):
        raise TransportFehlerUngewissError("Simulierter Verbindungsfehler bei der Statusabfrage (Test).")

    monkeypatch.setattr(service.client, "status_abfragen", kaputte_statusabfrage)
    anzahl_vor = len(state["calls"])
    ergebnis = service.nachsenden_einzelempfaenger(ctx=admin_ctx, mahnlauf_id=mahnlauf.id, mitmieter_id=mitmieter.id, heute=date(2026, 9, 14))
    assert ergebnis == "UNKLAR"
    assert len(state["calls"]) == anzahl_vor  # keine echte HTTP-Anfrage mehr (Monkeypatch greift davor)
    eintrag = next(e for e in service.empfaenger_zugang_repo.fuer_mahnlauf(mahnlauf.id) if e.mitmieter_id == mitmieter.id)
    assert eintrag.versand_belegt_am is None  # weiterhin unbelegt, NIE geraten


def test_nachsendung_verweigert_fuer_unbekannten_mahnlauf(hv, basis_vertrag, admin_ctx):
    """Task 2: ein unbekannter Mahnlauf wird nie stillschweigend
    übersprungen - klarer Fehler statt eines erfundenen Ergebnisses."""

    service, _state = hv
    contract, _account = basis_vertrag
    mitmieter = service.mitmieter_repo.anlegen(
        vertrag_id=contract.id, name="Mitmieterin A", email="mitmieterin-a@example.at", adresse=None,
        erstellt_von="test", rolle="MITMIETER", zustand="BESTAETIGT")
    with pytest.raises(ValueError):
        service.nachsenden_einzelempfaenger(ctx=admin_ctx, mahnlauf_id=999999, mitmieter_id=mitmieter.id, heute=date(2026, 9, 14))


def test_nachsendung_verweigert_fuer_noch_nie_versandte_gruppe(hv, basis_vertrag, admin_ctx):
    """Task 2: eine Gruppe, die noch nie tatsächlich versandversucht
    wurde (GEPLANT), darf NIE über die Einzel-Nachsendung "nachgeholt"
    werden - das bleibt ausschließlich dem normalen Planen/Dispatchen
    vorbehalten."""

    service, _state = hv
    contract, _account = basis_vertrag
    mitmieter = service.mitmieter_repo.anlegen(
        vertrag_id=contract.id, name="Mitmieterin A", email="mitmieterin-a@example.at", adresse=None,
        erstellt_von="test", rolle="MITMIETER", zustand="BESTAETIGT")
    geplanter_lauf = service.mahnlauf_repo.get_or_create(
        outbox_key="test-noch-nie-versandt", vertrag_id=contract.id, gesellschaft_id=contract.gesellschaft_id,
        stufe=1, mitglieder_mahnfall_ids="[]",
    )
    assert geplanter_lauf.status == "GEPLANT"

    with pytest.raises(ValueError):
        service.nachsenden_einzelempfaenger(
            ctx=admin_ctx, mahnlauf_id=geplanter_lauf.id, mitmieter_id=mitmieter.id, heute=date(2026, 9, 14),
        )


def test_nachsendung_brief_fuer_nachweislich_nie_archivierten_empfaenger(hv, basis_vertrag, admin_ctx):
    """Task 2 (Briefkanal): eine Mitmieterin, deren Brief beim Erstversand
    NIE archiviert werden konnte (simulierter Fehler), bekommt über die
    Nachsendung ihr Schreiben alsdann tatsächlich erzeugt/archiviert/
    übertragen - mit demselben eingefrorenen Kostensnapshot, keiner
    zweiten Gebührenbuchung."""

    service, _state = hv
    contract, account = basis_vertrag
    _seed_debt(hv, contract, account, admin_ctx)
    fake_verbindung = _richte_brief_kanal_ein(service, contract, account)
    mitmieter = service.mitmieter_repo.anlegen(
        vertrag_id=contract.id, name="Mitmieterin A", email=None, adresse="Nebenstraße 2, 1010 Wien",
        erstellt_von="test", rolle="MITMIETER", zustand="BESTAETIGT")
    service.mitmieter_repo.setze_postadresse_geprueft(mitmieter.id, geprueft=True)

    # Simuliert einen Fehlschlag NUR beim Mitmieter-Brief (Hauptbrief
    # bleibt unberührt) durch gezieltes Monkeypatching der Archivierung
    # für GENAU diesen einen Aufruf.
    original_archiviere_brief = service.empfaenger_zugang_repo.archiviere_brief
    aufrufe = {"anzahl": 0}

    def kaputte_archivierung(*args, **kwargs):
        aufrufe["anzahl"] += 1
        if aufrufe["anzahl"] == 1:
            raise RuntimeError("Simulierter Fehler bei der Mitmieter-Brief-Archivierung (Test).")
        return original_archiviere_brief(*args, **kwargs)

    service.empfaenger_zugang_repo.archiviere_brief = kaputte_archivierung
    try:
        service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))
    finally:
        service.empfaenger_zugang_repo.archiviere_brief = original_archiviere_brief

    mahnlauf = service.mahnlauf_repo.list_fuer_vertrag(contract.id)[0]
    eintrag_vor = next(e for e in service.empfaenger_zugang_repo.fuer_mahnlauf(mahnlauf.id) if e.mitmieter_id == mitmieter.id)
    assert eintrag_vor.brief_status is None  # nachweislich nie archiviert

    ergebnis = service.nachsenden_einzelempfaenger(ctx=admin_ctx, mahnlauf_id=mahnlauf.id, mitmieter_id=mitmieter.id, heute=date(2026, 9, 14))
    assert ergebnis == "GESENDET"
    eintrag_nach = next(e for e in service.empfaenger_zugang_repo.fuer_mahnlauf(mahnlauf.id) if e.mitmieter_id == mitmieter.id)
    assert eintrag_nach.brief_status == "UEBERMITTELT"
    assert eintrag_nach.brief_pdf_bytes[:5] == b"%PDF-"
    dateiname = brief_sftp.baue_dateiname(sende_schluessel=eintrag_nach.brief_sende_schluessel)
    assert f"bulk_tp/{dateiname}" in fake_verbindung.geschriebene_dateien

    # Keine zweite Gebührenbuchung ausgelöst - die Nachsendung bucht
    # nichts, der Kostensnapshot bleibt unverändert an den Hauptlauf gebunden.
    with service.sf() as db:
        from mietinkasso.infrastructure.db.tables import MahnkostenBuchungTable as _MKB
        buchungen = list(db.execute(select(_MKB).where(_MKB.vertrag_id == contract.id)).scalars())
    assert buchungen == []  # Hauptlauf bleibt UNSICHER (kein Anbieterbeleg), keine Buchung von irgendwoher

    # Erneute Nachsendung für den jetzt bereits übermittelten Brief: No-Op.
    ergebnis_erneut = service.nachsenden_einzelempfaenger(ctx=admin_ctx, mahnlauf_id=mahnlauf.id, mitmieter_id=mitmieter.id, heute=date(2026, 9, 15))
    assert ergebnis_erneut == "BEREITS_GESENDET"


def test_nachsendung_brief_enthaelt_kanonischen_kontoauszug_in_tatsaechlich_uebertragenen_bytes(hv, basis_vertrag, admin_ctx):
    """Konkreter Paket-Test gegen die TATSÄCHLICH im Fake-SFTP übertragenen
    Bytes (nicht nur gegen den Merge-Helfer isoliert, Nachtrag Markus
    22.10.2026): derselbe Repro wie `test_nachsendung_brief_fuer_
    nachweislich_nie_archivierten_empfaenger` - eine Mitmieterin, deren
    Brief beim Erstversand NIE archiviert werden konnte, bekommt ihr
    Schreiben über die Nachsendung tatsächlich erzeugt/archiviert/
    übertragen. Bisher (Fund dieser Runde) war dieses SPÄT nachgesendete
    PDF nur 1 Seite OHNE den kanonischen Kontoauszug/chronologisches
    Journal - anders als der Erstversand-Hauptbrief. Prüft die ECHTEN,
    im Fake-SFTP angekommenen Bytes (`fake_verbindung.geschriebene_
    dateien`), kein echter Netzkontakt."""

    service, _state = hv
    contract, account = basis_vertrag
    _seed_debt(hv, contract, account, admin_ctx)
    fake_verbindung = _richte_brief_kanal_ein(service, contract, account)
    mitmieter = service.mitmieter_repo.anlegen(
        vertrag_id=contract.id, name="Mitmieterin Kontoauszug", email=None, adresse="Nebenstraße 3, 1010 Wien",
        erstellt_von="test", rolle="MITMIETER", zustand="BESTAETIGT")
    service.mitmieter_repo.setze_postadresse_geprueft(mitmieter.id, geprueft=True)

    original_archiviere_brief = service.empfaenger_zugang_repo.archiviere_brief
    aufrufe = {"anzahl": 0}

    def kaputte_archivierung(*args, **kwargs):
        aufrufe["anzahl"] += 1
        if aufrufe["anzahl"] == 1:
            raise RuntimeError("Simulierter Fehler bei der Mitmieter-Brief-Archivierung (Test).")
        return original_archiviere_brief(*args, **kwargs)

    service.empfaenger_zugang_repo.archiviere_brief = kaputte_archivierung
    try:
        service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))
    finally:
        service.empfaenger_zugang_repo.archiviere_brief = original_archiviere_brief

    mahnlauf = service.mahnlauf_repo.list_fuer_vertrag(contract.id)[0]
    ergebnis = service.nachsenden_einzelempfaenger(ctx=admin_ctx, mahnlauf_id=mahnlauf.id, mitmieter_id=mitmieter.id, heute=date(2026, 9, 14))
    assert ergebnis == "GESENDET"

    eintrag_nach = next(e for e in service.empfaenger_zugang_repo.fuer_mahnlauf(mahnlauf.id) if e.mitmieter_id == mitmieter.id)
    dateiname = brief_sftp.baue_dateiname(sende_schluessel=eintrag_nach.brief_sende_schluessel)
    uebertragene_bytes = fake_verbindung.geschriebene_dateien[f"bulk_tp/{dateiname}"]
    assert uebertragene_bytes == eintrag_nach.brief_pdf_bytes  # archivierte und tatsächlich übertragene Bytes sind identisch

    seiten = PdfReader(io.BytesIO(uebertragene_bytes)).pages
    assert len(seiten) > 1, "Nachgesendetes Mitmieter-PDF muss den kanonischen Kontoauszug als weitere Seite(n) enthalten."
    text = " ".join(p.extract_text() for p in seiten)
    # Nachtrag Markus 23.10.2026 (HV-20261023-MAHNUNG-EINFACH): die
    # kundenverständliche Überschrift "Ihre Kontoaufstellung" ersetzt das
    # frühere technische "Kontoblatt — chronologisches Journal" - sowohl
    # auf der ersten Briefseite selbst als auch im angehängten Kontoauszug.
    assert "Ihre Kontoaufstellung" in text


def test_nachsendung_brief_defekter_kontoauszug_blockiert_vor_archivierung_und_sftp(hv, basis_vertrag, admin_ctx, monkeypatch):
    """Fix5 gilt identisch auf dem Nachsendeweg: ein defekter Kontoauszug-
    Anhang muss VOR jedem Providerkontakt (Archivierung/SFTP) blockieren,
    nie stillschweigend ohne Pflichtbeilage weiterlaufen. Kein echter
    Netzkontakt - reine Code-/Regressionsprüfung."""

    import mietinkasso.indexautomatik.mailversand_service as mailversand_modul

    service, _state = hv
    contract, account = basis_vertrag
    _seed_debt(hv, contract, account, admin_ctx)
    fake_verbindung = _richte_brief_kanal_ein(service, contract, account)
    mitmieter = service.mitmieter_repo.anlegen(
        vertrag_id=contract.id, name="Mitmieterin Defekt", email=None, adresse="Nebenstraße 4, 1010 Wien",
        erstellt_von="test", rolle="MITMIETER", zustand="BESTAETIGT")
    service.mitmieter_repo.setze_postadresse_geprueft(mitmieter.id, geprueft=True)

    original_archiviere_brief = service.empfaenger_zugang_repo.archiviere_brief
    aufrufe = {"anzahl": 0}

    def kaputte_archivierung(*args, **kwargs):
        aufrufe["anzahl"] += 1
        if aufrufe["anzahl"] == 1:
            raise RuntimeError("Simulierter Fehler bei der Mitmieter-Brief-Archivierung (Test).")
        return original_archiviere_brief(*args, **kwargs)

    service.empfaenger_zugang_repo.archiviere_brief = kaputte_archivierung
    try:
        service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))
    finally:
        service.empfaenger_zugang_repo.archiviere_brief = original_archiviere_brief

    mahnlauf = service.mahnlauf_repo.list_fuer_vertrag(contract.id)[0]
    eintrag_vor = next(e for e in service.empfaenger_zugang_repo.fuer_mahnlauf(mahnlauf.id) if e.mitmieter_id == mitmieter.id)
    dateien_vor = dict(fake_verbindung.geschriebene_dateien)

    def kaputter_kontoauszug(**kwargs):
        return b"DAS-IST-KEIN-PDF"

    monkeypatch.setattr(mailversand_modul, "erzeuge_kontoauszug_pdf", kaputter_kontoauszug)

    with pytest.raises(ValueError):
        service.nachsenden_einzelempfaenger(ctx=admin_ctx, mahnlauf_id=mahnlauf.id, mitmieter_id=mitmieter.id, heute=date(2026, 9, 14))

    eintrag_nach = next(e for e in service.empfaenger_zugang_repo.fuer_mahnlauf(mahnlauf.id) if e.mitmieter_id == mitmieter.id)
    assert eintrag_nach.brief_status is None  # nicht archiviert
    assert eintrag_nach.brief_sende_schluessel == eintrag_vor.brief_sende_schluessel  # unverändert (weiterhin None)
    assert fake_verbindung.geschriebene_dateien == dateien_vor  # kein SFTP-Kontakt


# -- Regression (Codex-Abnahme 21.10.2026, b6e773e): die Nachsendung --------
# -- gatete einen TATSÄCHLICH NEUEN Providerkontakt bisher an KEINER -------
# -- der Erstversand-Bedingungen (SEND_ENABLED/Allowlist, aktuelle ----------
# -- Bankbestätigung) - repariert für EMAIL UND BRIEF. ----------------------


def test_nachsendung_email_sendet_nicht_erneut_wenn_send_enabled_inzwischen_deaktiviert(
    hv_mit_tracking, basis_vertrag, admin_ctx,
):
    """Konkreter Abnahmefund (Codex 21.10.2026): der erste Kopieversand
    schlägt fehl, der Hauptversand bleibt GESENDET. Wird `SEND_ENABLED`
    (hier: `settings.send_enabled`) VOR der Nachsendung deaktiviert, darf
    `nachsenden_einzelempfaenger` KEINEN neuen POST auslösen - genau
    dieselbe Bedingung, die der Erstversand vor jedem tatsächlichen
    Providerkontakt prüft. Die reine Statusabklärung (GET) bleibt davon
    unberührt, löst aber KEINE Sendung aus."""

    service, state = hv_mit_tracking
    contract, account = basis_vertrag
    _seed_debt(hv_mit_tracking, contract, account, admin_ctx)
    mitmieter = service.mitmieter_repo.anlegen(
        vertrag_id=contract.id, name="Mitmieterin A", email="mitmieterin-a@example.at", adresse=None,
        erstellt_von="test", rolle="MITMIETER", zustand="BESTAETIGT")
    state["fehlschlagen_beim_ersten_versuch"].add("mitmieterin-a@example.at")

    service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))
    mahnlauf = service.mahnlauf_repo.list_fuer_vertrag(contract.id)[0]
    assert mahnlauf.status == "GESENDET"
    eintrag_vor = next(e for e in service.empfaenger_zugang_repo.fuer_mahnlauf(mahnlauf.id) if e.mitmieter_id == mitmieter.id)
    assert eintrag_vor.versand_belegt_am is None

    service.settings.send_enabled = False
    anzahl_vor = len(state["calls"])
    ergebnis = service.nachsenden_einzelempfaenger(ctx=admin_ctx, mahnlauf_id=mahnlauf.id, mitmieter_id=mitmieter.id, heute=date(2026, 9, 14))
    assert ergebnis == "BLOCKIERT"
    # GENAU EIN neuer Aufruf - die reine Statusabklärung (GET), die laut
    # Spezifikation auch bei deaktiviertem Versand erlaubt bleibt
    # ("Statusklärung ohne neue Sendung getrennt möglich"). KEIN neuer
    # POST (der wäre der zweite neue Call).
    assert len(state["calls"]) == anzahl_vor + 1
    assert state["calls"][-1].method == "GET"
    eintrag_nach = next(e for e in service.empfaenger_zugang_repo.fuer_mahnlauf(mahnlauf.id) if e.mitmieter_id == mitmieter.id)
    assert eintrag_nach.versand_belegt_am is None  # weiterhin unbelegt, kein erfundener Beleg

    # Wird SEND_ENABLED wieder aktiviert, funktioniert die Nachsendung
    # unverändert wie zuvor (keine dauerhafte Blockade durch das Gate).
    service.settings.send_enabled = True
    ergebnis2 = service.nachsenden_einzelempfaenger(ctx=admin_ctx, mahnlauf_id=mahnlauf.id, mitmieter_id=mitmieter.id, heute=date(2026, 9, 14))
    assert ergebnis2 == "GESENDET"


def test_nachsendung_email_sendet_nicht_erneut_bei_veralteter_bankbestaetigung(
    hv_mit_tracking, basis_vertrag, admin_ctx,
):
    """Konkreter Abnahmefund (Codex 21.10.2026): dieselbe Garantie für
    die Bankbestätigung - bleibt sie seit dem Erstversand unverändert
    (hier: nur bis 13.09.2026 bestätigt) und wird die Nachsendung weit
    später (21.10.2026, lange über `bank_stand_max_age_days` hinaus)
    versucht, darf das KEINEN neuen POST auslösen."""

    service, state = hv_mit_tracking
    contract, account = basis_vertrag
    _seed_debt(hv_mit_tracking, contract, account, admin_ctx)  # Bankbestätigung NUR bis 13.09.2026 (Default von _seed_debt)
    mitmieter = service.mitmieter_repo.anlegen(
        vertrag_id=contract.id, name="Mitmieterin A", email="mitmieterin-a@example.at", adresse=None,
        erstellt_von="test", rolle="MITMIETER", zustand="BESTAETIGT")
    state["fehlschlagen_beim_ersten_versuch"].add("mitmieterin-a@example.at")

    service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))
    mahnlauf = service.mahnlauf_repo.list_fuer_vertrag(contract.id)[0]
    assert mahnlauf.status == "GESENDET"

    anzahl_vor = len(state["calls"])
    # Die Bankbestätigung wurde seitdem NIE erneuert - ein Nachsende-
    # Versuch weit später (21.10.2026) muss dieselbe Veraltetheitsprüfung
    # treffen wie ein echter Erstversand an diesem Tag.
    ergebnis = service.nachsenden_einzelempfaenger(ctx=admin_ctx, mahnlauf_id=mahnlauf.id, mitmieter_id=mitmieter.id, heute=date(2026, 10, 21))
    assert ergebnis == "BLOCKIERT"
    assert len(state["calls"]) == anzahl_vor + 1  # NUR die Statusabklärung (GET), KEIN neuer POST
    eintrag_nach = next(e for e in service.empfaenger_zugang_repo.fuer_mahnlauf(mahnlauf.id) if e.mitmieter_id == mitmieter.id)
    assert eintrag_nach.versand_belegt_am is None

    # Eine frische Bankbestätigung macht die Nachsendung wieder möglich.
    service.bank_service.bestaetige_bankvollstaendigkeit(bank_konto_id="SYNTHETIC-BANK", bestaetigt_bis=date(2026, 10, 21), bestaetigt_von="SYNTHETIC-TEST")
    ergebnis2 = service.nachsenden_einzelempfaenger(ctx=admin_ctx, mahnlauf_id=mahnlauf.id, mitmieter_id=mitmieter.id, heute=date(2026, 10, 21))
    assert ergebnis2 == "GESENDET"


def test_nachsendung_brief_sendet_nicht_erneut_bei_veralteter_bankbestaetigung(hv, basis_vertrag, admin_ctx):
    """Dieselbe Garantie auch für den Briefkanal - der SFTP-Upload ist
    dort der EINZIGE tatsächliche Transportkontakt (keine separate
    Statusabfrage), das Gate muss also unmittelbar davor greifen."""

    service, _state = hv
    contract, account = basis_vertrag
    _seed_debt(hv, contract, account, admin_ctx)  # Bankbestätigung NUR bis 13.09.2026
    fake_verbindung = _richte_brief_kanal_ein(service, contract, account)
    mitmieter = service.mitmieter_repo.anlegen(
        vertrag_id=contract.id, name="Mitmieterin A", email=None, adresse="Nebenstraße 2, 1010 Wien",
        erstellt_von="test", rolle="MITMIETER", zustand="BESTAETIGT")
    service.mitmieter_repo.setze_postadresse_geprueft(mitmieter.id, geprueft=True)

    original_archiviere_brief = service.empfaenger_zugang_repo.archiviere_brief
    aufrufe = {"anzahl": 0}

    def kaputte_archivierung(*args, **kwargs):
        aufrufe["anzahl"] += 1
        if aufrufe["anzahl"] == 1:
            raise RuntimeError("Simulierter Fehler bei der Mitmieter-Brief-Archivierung (Test).")
        return original_archiviere_brief(*args, **kwargs)

    service.empfaenger_zugang_repo.archiviere_brief = kaputte_archivierung
    try:
        service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))
    finally:
        service.empfaenger_zugang_repo.archiviere_brief = original_archiviere_brief

    mahnlauf = service.mahnlauf_repo.list_fuer_vertrag(contract.id)[0]
    eintrag_vor = next(e for e in service.empfaenger_zugang_repo.fuer_mahnlauf(mahnlauf.id) if e.mitmieter_id == mitmieter.id)
    assert eintrag_vor.brief_status is None

    # Der Hauptbrief wurde beim Erstversand bereits erfolgreich
    # hochgeladen (eigener Sendeschlüssel) - das gehört NICHT zur
    # Nachsendung und bleibt als Vergleichsbasis unberührt stehen.
    dateien_vor_nachsendung = set(fake_verbindung.geschriebene_dateien)
    ergebnis = service.nachsenden_einzelempfaenger(ctx=admin_ctx, mahnlauf_id=mahnlauf.id, mitmieter_id=mitmieter.id, heute=date(2026, 10, 21))
    assert ergebnis == "BLOCKIERT"
    assert set(fake_verbindung.geschriebene_dateien) == dateien_vor_nachsendung  # KEIN neuer SFTP-Upload für die Nachsendung selbst
    eintrag_nach = next(e for e in service.empfaenger_zugang_repo.fuer_mahnlauf(mahnlauf.id) if e.mitmieter_id == mitmieter.id)
    # Das lokale Archivieren (PDF erzeugen/in der DB ablegen, KEIN
    # Netzkontakt) läuft weiterhin - NUR der tatsächliche SFTP-Upload ist
    # gesperrt, siehe `geschriebene_dateien` oben.
    assert eintrag_nach.brief_status == "VORBEREITET"


# -- REST-Transportoption (`mahnwesen/brief_rest.py`), rein synthetisch ---


def _richte_rest_brief_kanal_ein(service, contract, account, *, fake=None):
    """Dieselbe Kanal-/Profil-/Adressvorbereitung wie SFTP; als Transport
    läuft ausschließlich ein `httpx.MockTransport`, nie ein echter Endpoint."""

    _richte_brief_kanal_ein(service, contract, account)
    fake = fake or FakeEinfachBrief()
    service.brief_sftp_adapter = None
    service.brief_rest_adapter = PostRestTransportadapter(RestKonfiguration(
        basis_url="https://abn-www.einfach-brief.at/be_t", registered_mail_type="Einschreiben",
        preisobergrenze=Decimal("10"), api_key="SYNTHETIC-KEY",
    ), transport_factory=fake.transport)
    return fake


def _rest_claims(session_factory):
    with session_factory() as s:
        return list(s.execute(select(AuditEventTable).where(AuditEventTable.aktion == "BRIEF_REST_VERSAND_CLAIM")).scalars())


def _lauf_und_archiv(service, contract):
    mahnlauf = service.mahnlauf_repo.list_fuer_vertrag(contract.id)[0]
    archiv = service.mahnbrief_archiv_repo.fuer_mahnlauf(
        vertrag_id=contract.id, stufe=1, mahnlauf_schluessel=mahnlauf.outbox_key,
    )
    return mahnlauf, archiv


def _rest_mitmieter(service, contract):
    mitmieter = service.mitmieter_repo.anlegen(
        vertrag_id=contract.id, name="Mitmieterin A", email=None, adresse="Nebenstraße 2, 1010 Wien",
        erstellt_von="test", rolle="MITMIETER", zustand="BESTAETIGT")
    service.mitmieter_repo.setze_postadresse_geprueft(mitmieter.id, geprueft=True)
    return mitmieter


def test_rest_brief_uebermittelt_dieselben_bytes_und_laesst_mahnstatus_offen(hv, basis_vertrag, admin_ctx, session_factory):
    service, _ = hv
    contract, account = basis_vertrag
    _seed_debt(hv, contract, account, admin_ctx)
    fake = _richte_rest_brief_kanal_ein(service, contract, account)

    service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))

    mahnlauf, archiv = _lauf_und_archiv(service, contract)
    assert fake.methoden() == [UPLOAD, STATUS, VERARBEITEN]
    dokument = fake.bodies(UPLOAD)[0]["documentPayloadList"][0]
    assert base64.b64decode(dokument["payload"]) == archiv.pdf_bytes
    assert dokument["foreignId"] == archiv.sende_schluessel
    assert fake.bodies(UPLOAD)[0]["printOnly"] is True
    # Quittung = höchstens Annahme: UEBERMITTELT/UNSICHER, keine Buchung, kein GESENDET.
    assert archiv.status == "UEBERMITTELT"
    assert archiv.status_beleg_referenz == f"einfachbrief:{archiv.sende_schluessel}:order:ORDER-1:delivery:DELIVERY-1"
    assert archiv.anbieterbeleg_referenz is None
    assert mahnlauf.status == "UNSICHER" and mahnlauf.mahnkosten_verarbeitet_am is None
    assert service.mahn_repo.list_fuer_vertrag(contract.id)[0].status == "UNSICHER"
    with session_factory() as s:
        assert list(s.execute(select(MahnkostenBuchungTable)).scalars()) == []
        assert list(s.execute(select(AuditEventTable).where(AuditEventTable.aktion == "MAILVERSAND_BESTAETIGT")).scalars()) == []
    assert [c.entity_id for c in _rest_claims(session_factory)] == [archiv.sende_schluessel]

    service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 14))
    assert len(fake.anfragen) == 3  # kein zweiter Versuch im Folgelauf


def test_rest_anbieterbeleg_bleibt_der_einzige_abschluss(hv, basis_vertrag, admin_ctx):
    service, _ = hv
    contract, account = basis_vertrag
    heute = date.today()
    _seed_debt(hv, contract, account, admin_ctx, day=heute)
    fake = _richte_rest_brief_kanal_ein(service, contract, account)
    service.mahnlauf(ctx=admin_ctx, heute=heute)
    mahnlauf, archiv = _lauf_und_archiv(service, contract)
    assert mahnlauf.status == "UNSICHER"

    service.vervollstaendige_mahnlauf_aus_anbieterbeleg(
        ctx=admin_ctx, archiv_id=archiv.id, anbieterbeleg_pdf_bytes=_echter_beleg_pdf("Synthetischer Beleg"),
        anbieterbeleg_referenz="SYNTHETIC-BELEG-1", postaufgabe_datum=heute, importiert_von="test", heute=heute,
    )
    assert service.mahnlauf_repo.get(mahnlauf.id).status == "GESENDET"
    assert len(fake.anfragen) == 3  # der Abschluss sendet nie erneut


@pytest.mark.parametrize("methode,erwartet", [
    (UPLOAD, [UPLOAD]), (STATUS, [UPLOAD, STATUS]), (VERARBEITEN, [UPLOAD, STATUS, VERARBEITEN]),
])
def test_rest_timeout_bleibt_unsicher_ohne_replay(hv, basis_vertrag, admin_ctx, session_factory, methode, erwartet):
    service, _ = hv
    contract, account = basis_vertrag
    _seed_debt(hv, contract, account, admin_ctx)
    fake = _richte_rest_brief_kanal_ein(service, contract, account)
    fake.stoerung[methode] = httpx.ReadTimeout("synthetisch")

    service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))

    mahnlauf, archiv = _lauf_und_archiv(service, contract)
    assert mahnlauf.status == "UNSICHER" and archiv.status == "VORBEREITET"
    assert service.mahn_repo.list_fuer_vertrag(contract.id)[0].status == "UNSICHER"
    assert fake.methoden() == erwartet
    assert len(_rest_claims(session_factory)) == 1

    del fake.stoerung[methode]
    service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 14))
    assert fake.methoden() == erwartet


@pytest.mark.parametrize("ziel,feld,wert", [
    ("upload", "infected", True), ("upload", "warningExist", True), ("upload", "errorFileList", ["x.pdf"]),
    ("lieferung", "ebrief", True), ("dokument", "foreignId", "falsch"),
])
def test_rest_anbieterablehnung_wird_nie_blockiert_und_nie_neu_geplant(hv, basis_vertrag, admin_ctx, ziel, feld, wert):
    service, _ = hv
    contract, account = basis_vertrag
    _seed_debt(hv, contract, account, admin_ctx)
    fake = _richte_rest_brief_kanal_ein(service, contract, account)
    getattr(fake, ziel)[feld] = wert

    service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))

    mahnlauf, archiv = _lauf_und_archiv(service, contract)
    assert mahnlauf.status == "UNSICHER" and archiv.status == "VORBEREITET"
    assert VERARBEITEN not in fake.methoden()
    anzahl = len(fake.anfragen)
    service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 14))
    assert len(fake.anfragen) == anzahl


def test_rest_lokaler_pdf_fehler_blockiert_ohne_claim_und_ohne_request(hv, basis_vertrag, admin_ctx, session_factory):
    service, _ = hv
    contract, account = basis_vertrag
    _seed_debt(hv, contract, account, admin_ctx)
    fake = _richte_rest_brief_kanal_ein(service, contract, account)

    def _abgelehnt(**_kwargs):
        raise brief_sftp.PostSendevoraussetzungError("Synthetisch abgelehnt.")

    service.brief_rest_adapter.pruefe_vorab = _abgelehnt
    service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))

    assert fake.anfragen == [] and _rest_claims(session_factory) == []
    assert service.mahnlauf_repo.list_fuer_vertrag(contract.id)[0].status not in ("UNSICHER", "GESENDET", "IN_VERSAND")


def test_rest_nachsendung_nach_ungewissem_versuch_sendet_nie_erneut(hv, basis_vertrag, admin_ctx):
    """Kern der REST-Abweichung: SFTP erkennt eine Dublette remote über das
    exklusive Anlegen, REST nicht - der dauerhafte Claim verhindert hier,
    dass eine Nachsendung denselben Brief ein zweites Mal hochlädt."""

    service, _ = hv
    contract, account = basis_vertrag
    _seed_debt(hv, contract, account, admin_ctx, day=date(2026, 9, 14))
    fake = _richte_rest_brief_kanal_ein(service, contract, account)
    mitmieter = _rest_mitmieter(service, contract)
    uploads = {"anzahl": 0}

    def _zweiter_upload_haengt(_request):
        uploads["anzahl"] += 1
        return httpx.ReadTimeout("synthetisch") if uploads["anzahl"] == 2 else None

    fake.stoerung[UPLOAD] = _zweiter_upload_haengt
    service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))

    mahnlauf = service.mahnlauf_repo.list_fuer_vertrag(contract.id)[0]
    eintrag = next(e for e in service.empfaenger_zugang_repo.fuer_mahnlauf(mahnlauf.id) if e.mitmieter_id == mitmieter.id)
    assert eintrag.brief_status == "VORBEREITET" and eintrag.brief_sende_schluessel
    anzahl = len(fake.anfragen)

    del fake.stoerung[UPLOAD]
    for _ in range(2):
        ergebnis = service.nachsenden_einzelempfaenger(
            ctx=admin_ctx, mahnlauf_id=mahnlauf.id, mitmieter_id=mitmieter.id, heute=date(2026, 9, 14),
        )
        assert ergebnis == "UNKLAR"
    assert len(fake.anfragen) == anzahl


def test_rest_absturz_nach_claim_sendet_bei_nachsendung_nie(hv, basis_vertrag, admin_ctx):
    service, _ = hv
    contract, account = basis_vertrag
    _seed_debt(hv, contract, account, admin_ctx, day=date(2026, 9, 14))
    fake = _richte_rest_brief_kanal_ein(service, contract, account)
    mitmieter = _rest_mitmieter(service, contract)
    original_sende = service.brief_rest_adapter.sende
    aufrufe = {"anzahl": 0}

    def _absturz_beim_mitmieter(**kwargs):
        aufrufe["anzahl"] += 1
        if aufrufe["anzahl"] == 2:
            raise RuntimeError("Synthetischer Absturz nach dem Claim.")
        return original_sende(**kwargs)

    service.brief_rest_adapter.sende = _absturz_beim_mitmieter
    service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))
    service.brief_rest_adapter.sende = original_sende
    anzahl = len(fake.anfragen)

    mahnlauf = service.mahnlauf_repo.list_fuer_vertrag(contract.id)[0]
    ergebnis = service.nachsenden_einzelempfaenger(
        ctx=admin_ctx, mahnlauf_id=mahnlauf.id, mitmieter_id=mitmieter.id, heute=date(2026, 9, 14),
    )
    assert ergebnis == "UNKLAR" and len(fake.anfragen) == anzahl


def test_rest_mitmieter_bekommt_eigene_bytes_und_eigenen_schluessel(hv, basis_vertrag, admin_ctx):
    service, _ = hv
    contract, account = basis_vertrag
    _seed_debt(hv, contract, account, admin_ctx)
    fake = _richte_rest_brief_kanal_ein(service, contract, account)
    mitmieter = _rest_mitmieter(service, contract)

    service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))

    mahnlauf, archiv = _lauf_und_archiv(service, contract)
    eintrag = next(e for e in service.empfaenger_zugang_repo.fuer_mahnlauf(mahnlauf.id) if e.mitmieter_id == mitmieter.id)
    assert [d["foreignId"] for d in fake.hochgeladen] == [archiv.sende_schluessel, eintrag.brief_sende_schluessel]
    assert base64.b64decode(fake.hochgeladen[1]["payload"]) == eintrag.brief_pdf_bytes
    assert eintrag.brief_status == "UEBERMITTELT"
    assert fake.methoden().count(VERARBEITEN) == 2  # je Brief genau eine Lieferung, nie gesammelt
    assert all(len(body["deliveries"]) == 1 for body in fake.bodies(VERARBEITEN))
    # Bereits übermittelt: Nachsendung bleibt ein No-Op ohne Request.
    anzahl = len(fake.anfragen)
    assert service.nachsenden_einzelempfaenger(
        ctx=admin_ctx, mahnlauf_id=mahnlauf.id, mitmieter_id=mitmieter.id, heute=date(2026, 9, 13),
    ) == "BEREITS_GESENDET"
    assert len(fake.anfragen) == anzahl


def test_rest_nachsendung_haelt_das_frische_versandgate(hv, basis_vertrag, admin_ctx, session_factory):
    service, _ = hv
    contract, account = basis_vertrag
    _seed_debt(hv, contract, account, admin_ctx)  # Bankbestätigung NUR bis 13.09.2026
    fake = _richte_rest_brief_kanal_ein(service, contract, account)
    mitmieter = _rest_mitmieter(service, contract)
    original = service.empfaenger_zugang_repo.archiviere_brief

    def _kaputt(*_args, **_kwargs):
        raise RuntimeError("Synthetischer Archivierungsfehler.")

    service.empfaenger_zugang_repo.archiviere_brief = _kaputt
    service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))
    service.empfaenger_zugang_repo.archiviere_brief = original
    anzahl, claims = len(fake.anfragen), len(_rest_claims(session_factory))

    mahnlauf = service.mahnlauf_repo.list_fuer_vertrag(contract.id)[0]
    assert service.nachsenden_einzelempfaenger(
        ctx=admin_ctx, mahnlauf_id=mahnlauf.id, mitmieter_id=mitmieter.id, heute=date(2026, 10, 21),
    ) == "BLOCKIERT"
    assert len(fake.anfragen) == anzahl and len(_rest_claims(session_factory)) == claims


def test_rest_send_enabled_aus_macht_keinen_request(hv, basis_vertrag, admin_ctx, session_factory):
    service, _ = hv
    contract, account = basis_vertrag
    _seed_debt(hv, contract, account, admin_ctx)
    fake = _richte_rest_brief_kanal_ein(service, contract, account)
    service.settings.send_enabled = False
    service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))
    assert fake.anfragen == [] and _rest_claims(session_factory) == []


def test_rest_veraltete_bankbestaetigung_macht_keinen_request(hv, basis_vertrag, admin_ctx, session_factory):
    service, _ = hv
    contract, account = basis_vertrag
    _seed_debt(hv, contract, account, admin_ctx, day=date(2026, 9, 1))
    fake = _richte_rest_brief_kanal_ein(service, contract, account)
    service.mahnlauf(ctx=admin_ctx, heute=date(2026, 12, 13))
    assert fake.anfragen == [] and _rest_claims(session_factory) == []


def test_rest_ohne_freigegebenes_anbieterprofil_macht_keinen_request(hv, basis_vertrag, admin_ctx, session_factory, monkeypatch):
    service, _ = hv
    contract, account = basis_vertrag
    _seed_debt(hv, contract, account, admin_ctx)
    fake = _richte_rest_brief_kanal_ein(service, contract, account)
    monkeypatch.setattr(service.brief_anbieterprofil_repo, "aktuelle_freigegebene", lambda *a, **k: None)
    service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))
    assert fake.anfragen == [] and _rest_claims(session_factory) == []


def test_rest_ohne_geprueften_empfaenger_macht_keinen_request(hv, basis_vertrag, admin_ctx, session_factory):
    service, _ = hv
    contract, account = basis_vertrag
    _seed_debt(hv, contract, account, admin_ctx)
    fake = _richte_rest_brief_kanal_ein(service, contract, account)
    service.bundle.stammdaten_repository.upsert_debitor(
        id=account.debitor_id, name="Max Mustermieter", email=None,
        adresse="Musterstraße 1, 1010 Wien", postadresse_geprueft=False,
    )
    service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))
    assert fake.anfragen == [] and _rest_claims(session_factory) == []


def _transport_service(session_factory, tmp_path, **overrides):
    key = tmp_path / "post-api-key"
    key.write_text("SYNTHETIC-KEY", encoding="utf-8")
    sftp_user, sftp_pass = tmp_path / "sftp-user", tmp_path / "sftp-pass"
    sftp_user.write_text("synthetic", encoding="utf-8")
    sftp_pass.write_text("synthetic", encoding="utf-8")
    werte = dict(
        owner_email="mb@jlb-immo.at",
        brief_sftp_host="transfer.post-business-solutions.at", brief_sftp_username_datei=str(sftp_user),
        brief_sftp_password_datei=str(sftp_pass), brief_sftp_host_key_fingerprint="SHA256:synthetic",
        brief_rest_basis_url="https://abn-www.einfach-brief.at/be_t", brief_rest_api_key_datei=str(key),
        brief_rest_registered_mail_type="Einschreiben", brief_rest_preisobergrenze=Decimal("10"),
    )
    werte.update(overrides)
    settings = Settings(_env_file=None, **werte)
    return HVMailversandService(session_factory, bauen(session_factory, settings), settings)


def test_transport_ohne_auswahl_bleibt_unveraendert_sftp(session_factory, tmp_path):
    service = _transport_service(session_factory, tmp_path)
    assert isinstance(service.brief_sftp_adapter, PostSftpTransportadapter)
    assert service.brief_rest_adapter is None
    assert service.mahn_service._brief_transport_verfuegbar is True


def test_transport_rest_explizit_und_vollstaendig_schliesst_sftp_aus(session_factory, tmp_path):
    service = _transport_service(session_factory, tmp_path, brief_transport="rest")
    assert isinstance(service.brief_rest_adapter, PostRestTransportadapter)
    assert service.brief_sftp_adapter is None
    assert service.mahn_service._brief_transport_verfuegbar is True


@pytest.mark.parametrize("overrides", [
    {"brief_transport": "rest", "brief_rest_api_key_datei": None},
    {"brief_transport": "rest", "brief_rest_registered_mail_type": None},
    {"brief_transport": "rest", "brief_rest_preisobergrenze": None},
    {"brief_transport": "rest", "brief_rest_basis_url": "https://example.invalid/be"},
    {"brief_transport": "REST"},
    {"brief_transport": ""},
])
def test_transport_ohne_gueltige_auswahl_faellt_nie_auf_sftp_zurueck(session_factory, tmp_path, overrides):
    service = _transport_service(session_factory, tmp_path, **overrides)
    assert service.brief_sftp_adapter is None and service.brief_rest_adapter is None
    assert service.mahn_service._brief_transport_verfuegbar is False


def test_transport_sftp_ausgewaehlt_aber_unvollstaendig_faellt_nie_auf_rest_zurueck(session_factory, tmp_path):
    service = _transport_service(session_factory, tmp_path, brief_sftp_host=None)
    assert service.brief_sftp_adapter is None and service.brief_rest_adapter is None
    assert service.mahn_service._brief_transport_verfuegbar is False
