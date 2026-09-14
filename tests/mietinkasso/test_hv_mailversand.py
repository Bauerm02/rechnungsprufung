"""Real business services and HTTP protocol, exclusively synthetic in-memory data."""
import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from mietinkasso.domain.enums import OPTyp, MahnStatus
from mietinkasso.indexautomatik.bootstrap import bauen
from mietinkasso.indexautomatik.mailops_client import MailOpsClient, MailOpsErgebnis
from mietinkasso.indexautomatik.mailversand_service import HVMailversandService
from mietinkasso.infrastructure.config import Settings
from mietinkasso.infrastructure.db.tables import (
    AuditEventTable, MahnFallTable, MahnkostenBuchungTable, MahnkostenGebuehrTable, MahnLaufTable,
)


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
