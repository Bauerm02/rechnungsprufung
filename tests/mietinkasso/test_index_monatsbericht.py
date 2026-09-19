"""Auftrag HV-20260919-INDEX-MONATSBERICHT: synthetische Tests für den
Owner-Monatsbericht (Umfang A), den Owner-Mailversand (Umfang B) und den
Quellenimport (Umfang C).

Codex-Vorgabe: "Bitte keine Tests mit erfundener flacher Verteilung" - der
MOEGLICH-Fall lässt daher den ECHTEN `IndexautomatikService`-Monatslauf
laufen (wie in `test_indexautomatik_service.py`) statt eine
`komponenten_verteilung` von Hand zu erfinden; `_neue_gesamtvorschreibung_
aus_schreiben` wird damit gegen die reale `{"eintraege": [...]}`-Struktur
aus `outbox_service.py` geprüft."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from mietinkasso.domain.exceptions import TransportFehlerUngewissError
from mietinkasso.index.repository import IndexRepository
from mietinkasso.index.service import IndexService
from mietinkasso.indexautomatik.mailnachweis import nachweis_daten
from mietinkasso.indexautomatik.mailops_client import MailOpsErgebnis
from mietinkasso.indexautomatik.monatsbericht_service import (
    IndexMonatsberichtService,
    _naechste_gesetzliche_april_grenze,
    status_label,
)
from mietinkasso.indexautomatik.outbox_service import ErhoehungsschreibenOutboxService
from mietinkasso.indexautomatik.rechtsprofil import RechtsprofilService
from mietinkasso.indexautomatik.rechtsprofil_import import (
    RechtsprofilImportFehlerError,
    _quellen_fakten_felder_aus_zeile,
)
from mietinkasso.indexautomatik.repository import (
    ErhoehungsschreibenRepository,
    IndexautomatikLaufRepository,
    IndexMonatsberichtRepository,
    IndexQuellenFaktenRepository,
    RechtsprofilRepository,
    VpiRepository,
)
from mietinkasso.indexautomatik.service import IndexautomatikService
from mietinkasso.infrastructure.db.tables import IndexautomatikLaufTable, IndexQuellenFaktenTable
from mietinkasso.mieweg_vorschau.repository import MieWegVorschauRepository
from mietinkasso.mieweg_vorschau.service import MieWegVorschauService


@dataclass
class Bundle:
    monatsbericht_service: IndexMonatsberichtService
    monatsbericht_repo: IndexMonatsberichtRepository
    quellen_fakten_repo: IndexQuellenFaktenRepository
    rechtsprofil_repo: RechtsprofilRepository
    rechtsprofil_service: RechtsprofilService
    outbox_repo: ErhoehungsschreibenRepository
    vpi_repo: VpiRepository
    index_service: IndexautomatikService


@pytest.fixture
def bundle(session_factory, stammdaten_repo) -> Bundle:
    rechtsprofil_repo = RechtsprofilRepository(session_factory)
    index_repo = IndexRepository(session_factory)
    outbox_repo = ErhoehungsschreibenRepository(session_factory)
    lauf_repo = IndexautomatikLaufRepository(session_factory)
    vpi_repo = VpiRepository(session_factory)
    quellen_fakten_repo = IndexQuellenFaktenRepository(session_factory)
    monatsbericht_repo = IndexMonatsberichtRepository(session_factory)
    mieweg_repo = MieWegVorschauRepository(session_factory)

    rechtsprofil_service = RechtsprofilService(rechtsprofil_repo, stammdaten_repo, index_repo)
    mieweg_service = MieWegVorschauService(mieweg_repo, stammdaten_repo)
    index_service_pure = IndexService(index_repo, stammdaten_repo)
    outbox_service = ErhoehungsschreibenOutboxService(
        outbox_repo, stammdaten_repo, rechtsprofil_repo, rechtsprofil_service, jlb_signatur="JLB Projects GmbH"
    )
    index_service = IndexautomatikService(
        stammdaten_repository=stammdaten_repo, rechtsprofil_service=rechtsprofil_service,
        lauf_repository=lauf_repo, outbox_repository=outbox_repo, vpi_repository=vpi_repo,
        mieweg_service=mieweg_service, index_repository=index_repo, index_service=index_service_pure,
        outbox_service=outbox_service,
    )
    monatsbericht_service = IndexMonatsberichtService(
        session_factory=session_factory, repository=monatsbericht_repo, stammdaten_repository=stammdaten_repo,
        rechtsprofil_repository=rechtsprofil_repo, outbox_repository=outbox_repo,
        quellen_fakten_repository=quellen_fakten_repo, vpi_repository=vpi_repo,
        owner_email="mb@jlb-immo.at", backoffice_basis_url="https://verwaltung.jlb-immo.at",
    )
    return Bundle(
        monatsbericht_service, monatsbericht_repo, quellen_fakten_repo, rechtsprofil_repo, rechtsprofil_service,
        outbox_repo, vpi_repo, index_service,
    )


def _lauf(*, vertrag_id, periode, status, rechtsprofil_id=None, rechtsprofil_version=None,
          erhoehungsschreiben_id=None, blockiert_gruende=None) -> IndexautomatikLaufTable:
    return IndexautomatikLaufTable(
        vertrag_id=vertrag_id, periode=periode, status=status, rechtsprofil_id=rechtsprofil_id,
        rechtsprofil_version=rechtsprofil_version, erhoehungsschreiben_id=erhoehungsschreiben_id,
        blockiert_gruende=blockiert_gruende or [],
    )


def _weiterer_vertrag(stammdaten_repo, *, vertrag_id: str, einheit_id: str, gesellschaft_id="7DI"):
    stammdaten_repo.upsert_einheit(id=einheit_id, objekt_id="601", bezeichnung=einheit_id, nutzungsstatus="DAUERVERMIETUNG")
    stammdaten_repo.upsert_vertrag(
        id=vertrag_id, einheit_id=einheit_id, debitor_id="DEB-1001", gesellschaft_id=gesellschaft_id,
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    return stammdaten_repo.get_vertrag(vertrag_id)


def _mit_komponente(stammdaten_repo, vertrag, *, id, betrag_cent=100_000):
    stammdaten_repo.add_komponente(
        id=id, vertrag_id=vertrag.id, art="HMZ", bezeichnung="Hauptmietzins", betrag_cent=betrag_cent,
        indexierbar=True, gueltig_von=date(2024, 1, 1),
    )


def _freigegebenes_wohnungsprofil(admin_ctx, rechtsprofil_service, vertrag, komponenten_ids, **overrides):
    basis = dict(
        ctx=admin_ctx, vertrag_id=vertrag.id, rechtsordnung="OESTERREICH_MRG_VOLL", ist_wohnungsnutzung=True,
        mrg_zinsbeschraenkung=False, mrg_zinsbeschraenkung_geprueft=True, ist_altvertrag=False, ist_hauptmiete=True,
        foerderbindung=False, foerderbindung_geprueft=True,
        mietzinsobergrenze_cent=None, mietzinsobergrenze_quellenbeleg=None, mietzinsobergrenze_gueltig_bis=None,
        bezugsjahr=2024, bezugsmonat=1, letzte_basis_war_jahresdurchschnitt=False, basis_komponenten_ids=komponenten_ids,
        vertraglich_zulaessiger_betrag_cent=200_000, vertraglicher_quellenbeleg="Mietvertrag Punkt 5",
        vertraglicher_fruehestmoeglicher_termin=date(2026, 4, 1), vertrag_beleg_referenz=f"Mietvertrag {vertrag.id}",
        klausel_referenz="Punkt 5 Wertsicherung", erstellt_von="markus",
    )
    basis.update(overrides)
    profil = rechtsprofil_service.entwurf_anlegen(**basis)
    return rechtsprofil_service.freigeben(profil.id, ctx=admin_ctx, freigegeben_von="markus")


def _seed_vpi(vpi_repo, *, reihe="VPI20C18", jahre_werte: dict[int, str]):
    for jahr, wert in jahre_werte.items():
        vpi_repo.jahreswert_erfassen(
            reihe=reihe, jahr=jahr, wert=Decimal(wert), quelle="Statistik Austria (synthetisch)",
            quelle_datum=date(jahr + 1, 2, 17), erfasst_von="markus",
        )


# -- Umfang A: Statusmapping/Zusammenfassung/atomare Erzeugung ---------------


def test_status_mapping_noch_nicht_moeglich_und_pruefung_noetig(bundle, stammdaten_repo, basis_vertrag):
    vertrag, _konto = basis_vertrag
    v2 = _weiterer_vertrag(stammdaten_repo, vertrag_id="V-B", einheit_id="601-TB")
    v3 = _weiterer_vertrag(stammdaten_repo, vertrag_id="V-C", einheit_id="601-TC")

    laeufe = [
        _lauf(vertrag_id=vertrag.id, periode="2026-09", status="KEIN_ERHOEHUNGSBEDARF"),
        _lauf(vertrag_id=v2.id, periode="2026-09", status="BLOCKIERT", blockiert_gruende=["Fehlender Beleg."]),
        _lauf(vertrag_id=v3.id, periode="2026-09", status="SENKUNG_PRUEFBEDARF", blockiert_gruende=["Berechnete Senkung - Prüfung."]),
    ]
    bericht = bundle.monatsbericht_service.erstellen_fuer_periode(periode="2026-09", laeufe=laeufe, heute=date(2026, 9, 19))

    assert bericht.anzahl_vertraege == 3
    assert bericht.anzahl_moeglich == 0
    assert bericht.anzahl_noch_nicht_moeglich == 1
    assert bericht.anzahl_pruefung_noetig == 2

    zeilen = {z.vertrag_id: z for z in bundle.monatsbericht_repo.zeilen_fuer_periode("2026-09")}
    assert zeilen[vertrag.id].status == "NOCH_NICHT_MOEGLICH"
    assert zeilen[v2.id].status == "PRUEFUNG_NOETIG" and zeilen[v2.id].status_grund == "Fehlender Beleg."
    assert zeilen[v3.id].status == "PRUEFUNG_NOETIG" and "Senkung" in zeilen[v3.id].status_grund
    # Kein technischer Code als Statusanzeige - menschenlesbarer Text.
    assert status_label(zeilen[vertrag.id].status) == "Noch nicht möglich"
    assert status_label(zeilen[v2.id].status) == "Prüfung nötig"


def test_status_moeglich_ueber_echten_monatslauf_mit_realer_komponenten_verteilung(
    admin_ctx, bundle, stammdaten_repo, basis_vertrag,
):
    """Kein erfundener flacher `komponenten_verteilung`-Wert: der echte
    `IndexautomatikService`-Monatslauf erzeugt das Erhöhungsschreiben mit
    der tatsächlichen `{"eintraege": [...]}`-Liste aus `outbox_service.py`,
    genau die Struktur, die `_neue_gesamtvorschreibung_aus_schreiben`
    verarbeiten muss."""

    vertrag, _konto = basis_vertrag
    _mit_komponente(stammdaten_repo, vertrag, id="K-1", betrag_cent=100_000)
    _freigegebenes_wohnungsprofil(admin_ctx, bundle.rechtsprofil_service, vertrag, ["K-1"])
    _seed_vpi(bundle.vpi_repo, jahre_werte={2023: "100", 2024: "102", 2025: "104"})

    lauf = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 9, 13), akteur="test")
    assert lauf.status == "ERHOEHUNG_ERZEUGT"
    schreiben = bundle.outbox_repo.get(lauf.erhoehungsschreiben_id)
    assert "eintraege" in (schreiben.komponenten_verteilung or {})

    bericht = bundle.monatsbericht_service.erstellen_fuer_periode(periode="2026-09", laeufe=[lauf], heute=date(2026, 9, 19))
    assert bericht.anzahl_moeglich == 1
    zeile = bundle.monatsbericht_repo.zeilen_fuer_periode("2026-09")[0]
    assert zeile.status == "MOEGLICH"
    assert zeile.vorschlag_cent == schreiben.komponenten_verteilung["eintraege"][0]["neuer_betrag_cent"]
    assert zeile.differenz_cent == schreiben.erhoehung_cent


def test_bestaetigte_gesamtmiete_ohne_komponenten_wird_nie_zu_nullmiete(bundle, stammdaten_repo, basis_vertrag):
    """Codex-Beispiel Pietsch/IMG: bestätigter Gesamtbetrag OHNE jede
    Vertragskomponente - NIE eine Nullmiete, aber auch NIE eine geratene
    Aufteilung auf einen indexierbaren Anteil."""

    vertrag, _konto = basis_vertrag
    qf = IndexQuellenFaktenTable(
        vertrag_id=vertrag.id, version=1, quelle="Test", inhalt_hash="h1", erstellt_von="test",
        bestaetigte_gesamtmiete_cent=123_456, bestaetigte_gesamtmiete_quelle="Kontoauszug 09/2026",
    )
    bundle.quellen_fakten_repo.anlegen(qf)

    laeufe = [_lauf(vertrag_id=vertrag.id, periode="2026-09", status="BLOCKIERT", blockiert_gruende=["Kein gültiges, freigegebenes Rechtsprofil (fehlt, nie freigegeben, oder seit Freigabe entwertet)."])]
    bundle.monatsbericht_service.erstellen_fuer_periode(periode="2026-09", laeufe=laeufe, heute=date(2026, 9, 19))
    zeile = bundle.monatsbericht_repo.zeilen_fuer_periode("2026-09")[0]
    assert zeile.gesamtvorschreibung_cent == 123_456
    assert zeile.gesamtvorschreibung_quelle == "BESTAETIGT_OHNE_KOMPONENTEN"
    assert zeile.indexierbarer_mietanteil_cent is None


def test_doppellauf_erzeugt_keine_doppelten_zeilen(bundle, stammdaten_repo, basis_vertrag):
    vertrag, _konto = basis_vertrag
    laeufe = [_lauf(vertrag_id=vertrag.id, periode="2026-09", status="KEIN_ERHOEHUNGSBEDARF")]
    bundle.monatsbericht_service.erstellen_fuer_periode(periode="2026-09", laeufe=laeufe, heute=date(2026, 9, 19))
    bundle.monatsbericht_service.erstellen_fuer_periode(periode="2026-09", laeufe=laeufe, heute=date(2026, 9, 19))
    assert len(bundle.monatsbericht_repo.zeilen_fuer_periode("2026-09")) == 1


def test_gesendeter_bericht_wird_von_erneutem_lauf_nicht_ueberschrieben(bundle, stammdaten_repo, basis_vertrag):
    vertrag, _konto = basis_vertrag
    laeufe = [_lauf(vertrag_id=vertrag.id, periode="2026-09", status="KEIN_ERHOEHUNGSBEDARF")]
    bericht = bundle.monatsbericht_service.erstellen_fuer_periode(periode="2026-09", laeufe=laeufe, heute=date(2026, 9, 19))
    bundle.monatsbericht_repo.set_status(bericht.id, "GESENDET", versendet_am=datetime.now(timezone.utc))

    ergebnis = bundle.monatsbericht_service.erstellen_fuer_periode(periode="2026-09", laeufe=laeufe, heute=date(2026, 9, 19))
    assert ergebnis is None
    aktueller = bundle.monatsbericht_repo.get_by_periode("2026-09")
    assert aktueller.status == "GESENDET"


# -- VPI-Ausfall: sichtbarer Fehlerbericht statt stillem Jobabbruch ----------


def test_vpi_fehler_erzeugt_sichtbaren_bericht_und_wird_bei_erfolg_normalisiert(bundle, stammdaten_repo, basis_vertrag):
    """Schlussreview-Korrektur (2d45e27): `vpi_fehlergrund` ist ein vom
    Versandstatus (`status`) VOLLSTÄNDIG unabhängiges Feld - `status`
    bleibt der normale BEREIT-Lebenszyklus (identisch zu jeder anderen
    Owner-Outbox), NIE ein eigener "VPI_FEHLER"-Wert (der bei Claim/
    Versand sonst überschrieben würde)."""

    vertrag, _konto = basis_vertrag
    fehlerbericht = bundle.monatsbericht_service.markiere_vpi_fehler(
        periode="2026-09", fehlergrund="Statistik-Austria-Abruf fehlgeschlagen: Verbindungsfehler."
    )
    assert fehlerbericht.status == "BEREIT"
    assert "Verbindungsfehler" in fehlerbericht.vpi_fehlergrund
    assert bundle.monatsbericht_repo.zeilen_fuer_periode("2026-09") == []

    # Nach Behebung: ein normaler, erfolgreicher Lauf normalisiert den
    # fachlichen Fehlerstand (nicht den Versandstatus, der war nie
    # verändert).
    laeufe = [_lauf(vertrag_id=vertrag.id, periode="2026-09", status="KEIN_ERHOEHUNGSBEDARF")]
    erneut = bundle.monatsbericht_service.erstellen_fuer_periode(periode="2026-09", laeufe=laeufe, heute=date(2026, 9, 19))
    assert erneut is not None
    assert erneut.status == "BEREIT"
    assert erneut.vpi_fehlergrund is None
    assert len(bundle.monatsbericht_repo.zeilen_fuer_periode("2026-09")) == 1


def test_vpi_fehler_mailtext_erklaert_ausbleiben_statt_leerem_bericht(bundle):
    bericht = bundle.monatsbericht_service.markiere_vpi_fehler(periode="2026-09", fehlergrund="Netzwerkfehler beim OGD-Abruf.")
    text = bundle.monatsbericht_service._text_fuer_bericht("2026-09", bericht)
    assert "VPI-Abruf fehlgeschlagen" in text and "Netzwerkfehler beim OGD-Abruf." in text
    assert "veralteten" in text
    # Keine Behauptung einer automatischen Wiederholung (Codex: "UI-
    # Behauptung automatischer Wiederholung korrigieren") - der Text darf
    # nur die EXPLIZITE Verneinung ("KEINE automatische Wiederholung")
    # enthalten, nie eine Zusage wie "wird automatisch erzeugt".
    assert "wird automatisch" not in text and "automatisch erzeugt" not in text
    assert "KEINE" in text and "automatische Wiederholung" in text
    assert "technische Klärung" in text


def test_vpi_fehler_bleibt_nach_erfolgreichem_versand_sichtbar(bundle):
    """Regression zum zweiten Schlussreview-Befund: "VPI_FEHLER ist
    gerade zugleich Versandstatus. Nach claim->IN_VERSAND->GESENDET zeigt
    Portal keinen VPI-Fehler mehr ... Transportfehler überschreibt
    fehlergrund." Ein erfolgreicher Owner-Mailversand DARF den
    fachlichen VPI-Fehlerhinweis nicht löschen."""

    bericht = bundle.monatsbericht_service.markiere_vpi_fehler(periode="2026-08", fehlergrund="VPI-Import fehlgeschlagen.")
    versendet = bundle.monatsbericht_service.benachrichtige_faellige(
        heute=date(2026, 9, 19), send_enabled=True, send_ab_periode="2026-01",
        versand_fn=lambda a: MailOpsErgebnis("GESENDET", "REF-1", "PROVIDER-REF-1", datetime.now(timezone.utc)),
    )
    assert len(versendet) == 1
    aktualisiert = bundle.monatsbericht_repo.get(bericht.id)
    assert aktualisiert.status == "GESENDET"
    assert aktualisiert.vpi_fehlergrund == "VPI-Import fehlgeschlagen."
    # Der Mailtext für diesen Bericht bleibt der VPI-Fehlerhinweis, egal
    # dass der Versand selbst erfolgreich war.
    text = bundle.monatsbericht_service._text_fuer_bericht("2026-08", aktualisiert)
    assert "VPI-Abruf fehlgeschlagen" in text


def test_vpi_fehler_und_transportfehler_ueberschreiben_sich_nicht_gegenseitig(bundle):
    """`fehlergrund` (Transport-/Mailversand-Diagnose) und
    `vpi_fehlergrund` (fachlicher VPI-Ausfallgrund) sind getrennte
    Spalten - ein unsicherer Versandversuch darf den VPI-Grund nicht
    überschreiben, und umgekehrt bleibt eine spätere `markiere_vpi_fehler`
    für eine bereits eingefrorene (GESENDET/UNKLAR) Kopfzeile wirkungslos."""

    bundle.monatsbericht_service.markiere_vpi_fehler(periode="2026-08", fehlergrund="VPI-Import fehlgeschlagen.")

    def kaputter_versand(auftrag):
        raise TransportFehlerUngewissError("Verbindungsabbruch - Status ungewiss.")

    bundle.monatsbericht_service.benachrichtige_faellige(
        heute=date(2026, 9, 19), send_enabled=True, send_ab_periode="2026-01", versand_fn=kaputter_versand,
    )
    bericht = bundle.monatsbericht_repo.get_by_periode("2026-08")
    assert bericht.status == "UNKLAR"
    assert bericht.fehlergrund == "Verbindungsabbruch - Status ungewiss."
    assert bericht.vpi_fehlergrund == "VPI-Import fehlgeschlagen."


# -- Umfang B: Aktivierungsperiode/Zukunftsschutz/Claim-Recovery ------------


def test_ohne_aktivierungsperiode_wird_nie_automatisch_versendet(bundle, stammdaten_repo, basis_vertrag):
    vertrag, _konto = basis_vertrag
    laeufe = [_lauf(vertrag_id=vertrag.id, periode="2026-09", status="KEIN_ERHOEHUNGSBEDARF")]
    bundle.monatsbericht_service.erstellen_fuer_periode(periode="2026-09", laeufe=laeufe, heute=date(2026, 9, 19))

    versendet = bundle.monatsbericht_service.benachrichtige_faellige(
        heute=date(2026, 9, 19), send_enabled=True, send_ab_periode=None, versand_fn=lambda a: None,
    )
    assert versendet == []
    assert bundle.monatsbericht_repo.get_by_periode("2026-09").status == "BEREIT"


def test_periode_vor_aktivierung_bleibt_nur_portalvorschau(bundle, stammdaten_repo, basis_vertrag):
    vertrag, _konto = basis_vertrag
    laeufe = [_lauf(vertrag_id=vertrag.id, periode="2026-09", status="KEIN_ERHOEHUNGSBEDARF")]
    bundle.monatsbericht_service.erstellen_fuer_periode(periode="2026-09", laeufe=laeufe, heute=date(2026, 9, 19))

    versendet = bundle.monatsbericht_service.benachrichtige_faellige(
        heute=date(2026, 10, 2), send_enabled=True, send_ab_periode="2026-10", versand_fn=lambda a: None,
    )
    assert versendet == []
    assert bundle.monatsbericht_repo.get_by_periode("2026-09").status == "BEREIT"


def test_zukuenftige_periode_wird_trotz_aktivierung_nie_vorab_versendet(bundle, stammdaten_repo, basis_vertrag):
    vertrag, _konto = basis_vertrag
    laeufe = [_lauf(vertrag_id=vertrag.id, periode="2026-10", status="KEIN_ERHOEHUNGSBEDARF")]
    bundle.monatsbericht_service.erstellen_fuer_periode(periode="2026-10", laeufe=laeufe, heute=date(2026, 9, 19))

    # send_ab liegt VOR der Periode, "heute" ist aber noch September - eine
    # bereits (versehentlich) erzeugte Oktober-Kopfzeile darf trotzdem nie
    # vor Erreichen des tatsächlichen Kalendermonats versendet werden.
    versendet = bundle.monatsbericht_service.benachrichtige_faellige(
        heute=date(2026, 9, 19), send_enabled=True, send_ab_periode="2026-01", versand_fn=lambda a: None,
    )
    assert versendet == []


def test_periode_ab_aktivierung_und_im_aktuellen_monat_wird_versendet(bundle, stammdaten_repo, basis_vertrag):
    vertrag, _konto = basis_vertrag
    laeufe = [_lauf(vertrag_id=vertrag.id, periode="2026-10", status="KEIN_ERHOEHUNGSBEDARF")]
    bundle.monatsbericht_service.erstellen_fuer_periode(periode="2026-10", laeufe=laeufe, heute=date(2026, 10, 2))

    aufrufe = []

    def versand_fn(auftrag):
        aufrufe.append(auftrag)
        return MailOpsErgebnis("GESENDET", "REF-OK", "PROVIDER-REF-OK", datetime.now(timezone.utc))

    versendet = bundle.monatsbericht_service.benachrichtige_faellige(
        heute=date(2026, 10, 2), send_enabled=True, send_ab_periode="2026-10", versand_fn=versand_fn,
    )
    assert len(versendet) == 1
    assert len(aufrufe) == 1 and aufrufe[0]["idempotenzschluessel"] == "index_monatsbericht:2026-10"


def test_unsicherer_versand_wird_nie_automatisch_erneut_versucht(bundle, stammdaten_repo, basis_vertrag):
    vertrag, _konto = basis_vertrag
    laeufe = [_lauf(vertrag_id=vertrag.id, periode="2026-10", status="KEIN_ERHOEHUNGSBEDARF")]
    bundle.monatsbericht_service.erstellen_fuer_periode(periode="2026-10", laeufe=laeufe, heute=date(2026, 10, 2))

    def versand_fn(auftrag):
        raise TransportFehlerUngewissError("Verbindungsabbruch - Status ungewiss.")

    versendet = bundle.monatsbericht_service.benachrichtige_faellige(
        heute=date(2026, 10, 2), send_enabled=True, send_ab_periode="2026-10", versand_fn=versand_fn,
    )
    assert versendet == []
    bericht = bundle.monatsbericht_repo.get_by_periode("2026-10")
    assert bericht.status == "UNKLAR"

    # Ein weiterer Lauf darf NICHT automatisch (ohne Recovery/Freigabe)
    # aus UNKLAR erneut versendet werden.
    versendet_zwei = bundle.monatsbericht_service.benachrichtige_faellige(
        heute=date(2026, 10, 3), send_enabled=True, send_ab_periode="2026-10", versand_fn=lambda a: (_ for _ in ()).throw(AssertionError("darf nicht erneut aufgerufen werden")),
    )
    assert versendet_zwei == []


def test_ohne_owner_email_wird_nie_versendet(bundle, stammdaten_repo, basis_vertrag):
    bundle.monatsbericht_service._owner_email = None
    vertrag, _konto = basis_vertrag
    laeufe = [_lauf(vertrag_id=vertrag.id, periode="2026-10", status="KEIN_ERHOEHUNGSBEDARF")]
    bundle.monatsbericht_service.erstellen_fuer_periode(periode="2026-10", laeufe=laeufe, heute=date(2026, 10, 2))
    versendet = bundle.monatsbericht_service.benachrichtige_faellige(
        heute=date(2026, 10, 2), send_enabled=True, send_ab_periode="2026-10", versand_fn=lambda a: None,
    )
    assert versendet == []


def test_verwaiste_in_versand_werden_nach_absturz_auf_unklar_gesetzt(bundle, stammdaten_repo, basis_vertrag):
    vertrag, _konto = basis_vertrag
    laeufe = [_lauf(vertrag_id=vertrag.id, periode="2026-10", status="KEIN_ERHOEHUNGSBEDARF")]
    bericht = bundle.monatsbericht_service.erstellen_fuer_periode(periode="2026-10", laeufe=laeufe, heute=date(2026, 10, 2))
    bundle.monatsbericht_repo.claim_fuer_versand(bericht.id, jetzt=datetime.now(timezone.utc) - timedelta(minutes=30))

    verwaiste = bundle.monatsbericht_service.markiere_verwaiste_als_unklar()
    assert len(verwaiste) == 1 and verwaiste[0].id == bericht.id
    assert bundle.monatsbericht_repo.get(bericht.id).status == "UNKLAR"


def test_absoluter_mail_link_zeigt_auf_konfigurierte_domain(bundle, stammdaten_repo, basis_vertrag):
    vertrag, _konto = basis_vertrag
    laeufe = [_lauf(vertrag_id=vertrag.id, periode="2026-09", status="KEIN_ERHOEHUNGSBEDARF")]
    bericht = bundle.monatsbericht_service.erstellen_fuer_periode(periode="2026-09", laeufe=laeufe, heute=date(2026, 9, 19))
    text = bundle.monatsbericht_service._text_fuer_bericht("2026-09", bericht)
    assert "https://verwaltung.jlb-immo.at/backoffice/indexautomatik/monatsbericht/2026-09" in text
    # Verständlicher Statusname, kein technischer Code, auch im Mailtext.
    assert "NOCH_NICHT_MOEGLICH" not in text
    assert "Noch nicht möglich" in text


# -- Gewerbe-Rechenvorschlag: fail-closed über `ist_wohnungsnutzung` --------


def _volle_quellenfakten(vertrag_id: str, **overrides) -> IndexQuellenFaktenTable:
    # `aktueller_indexbetrag_cent == urspruenglicher_indexbetrag_cent`
    # entspricht genau Codex' "drei freigabefreien Quellenvorschauen ...
    # Original=aktuellerTeil=Gesamt (keine bisherigen Erhöhungen)" -
    # in diesem Fall verhält sich die korrigierte Delta-Berechnung
    # identisch zur ursprünglichen (fälschlich gegen die Basis
    # gerechneten) Fassung.
    basis = dict(
        vertrag_id=vertrag_id, version=1, quelle="Test", inhalt_hash="h1", ist_wohnungsnutzung=False, erstellt_von="test",
        urspruengliche_klauselbasis_reihe="VPI20C18", urspruengliche_klauselbasis_monat="2024-01",
        urspruengliche_klauselbasis_wert="130.0", urspruenglicher_indexbetrag_cent=50_000,
        aktueller_indexbetrag_cent=50_000,
        betrag_basisbindung_belegt=True, schwelle_prozent="3", schwelle_inklusive=False,
        bestaetigte_gesamtmiete_cent=200_000, bestaetigte_gesamtmiete_quelle="Kontoauszug",
    )
    basis.update(overrides)
    return IndexQuellenFaktenTable(**basis)


@pytest.mark.parametrize("ist_wohnungsnutzung", [None, True])
def test_gewerbevorschau_bleibt_gesperrt_ohne_explizite_verifizierte_ablehnung(
    bundle, stammdaten_repo, basis_vertrag, ist_wohnungsnutzung,
):
    """Fail-closed (analog `RechtsprofilTable.ist_hauptmiete`): nur ein
    explizites `False` darf die Gewerbevorschau rechnen - `None`
    (ungeklärt) sperrt GENAUSO wie `True` (tatsächliche Wohnung)."""

    vertrag, _konto = basis_vertrag
    qf = _volle_quellenfakten(vertrag.id, ist_wohnungsnutzung=ist_wohnungsnutzung, pruefhinweis="Bitte Rechtsprofil anlegen.")
    bundle.quellen_fakten_repo.anlegen(qf)
    _seed_vpi(bundle.vpi_repo, jahre_werte={2023: "128", 2024: "130"})
    bundle.vpi_repo.monatswert_erfassen(
        reihe="VPI20C18", jahr=2026, monat=6, wert=Decimal("140.0"), finalitaet="ENDGUELTIG",
        quelle_datei="test.csv", quelle_zeile=1, quelle_hash="h", abgerufen_am=datetime(2026, 7, 1, tzinfo=timezone.utc),
        importiert_von="test",
    )
    laeufe = [_lauf(vertrag_id=vertrag.id, periode="2026-09", status="BLOCKIERT", blockiert_gruende=["Kein gültiges, freigegebenes Rechtsprofil (fehlt, nie freigegeben, oder seit Freigabe entwertet)."])]
    bundle.monatsbericht_service.erstellen_fuer_periode(periode="2026-09", laeufe=laeufe, heute=date(2026, 9, 19))
    zeile = bundle.monatsbericht_repo.zeilen_fuer_periode("2026-09")[0]
    assert zeile.vorschlag_cent is None
    assert zeile.status_grund == "Bitte Rechtsprofil anlegen."
    assert "Rechenvorschlag" not in zeile.status_grund


def test_gewerbevorschau_zeigt_rohe_und_wirksame_veraenderung_getrennt(bundle, stammdaten_repo, basis_vertrag):
    vertrag, _konto = basis_vertrag
    qf = _volle_quellenfakten(vertrag.id)
    bundle.quellen_fakten_repo.anlegen(qf)
    bundle.vpi_repo.monatswert_erfassen(
        reihe="VPI20C18", jahr=2026, monat=6, wert=Decimal("133.9"), finalitaet="ENDGUELTIG",
        quelle_datei="test.csv", quelle_zeile=1, quelle_hash="h", abgerufen_am=datetime(2026, 7, 1, tzinfo=timezone.utc),
        importiert_von="test",
    )
    laeufe = [_lauf(vertrag_id=vertrag.id, periode="2026-09", status="BLOCKIERT", blockiert_gruende=["Kein gültiges, freigegebenes Rechtsprofil (fehlt, nie freigegeben, oder seit Freigabe entwertet)."])]
    bundle.monatsbericht_service.erstellen_fuer_periode(periode="2026-09", laeufe=laeufe, heute=date(2026, 9, 19))
    zeile = bundle.monatsbericht_repo.zeilen_fuer_periode("2026-09")[0]

    # Rohveränderung (133.9/130.0 - 1) ~ 3.0% liegt UNTER/AN der 3%-Schwelle
    # (exklusive) - wirksame Veränderung bleibt daher 0, die tatsächliche
    # Rohveränderung bleibt trotzdem sichtbar im Text (nicht als "0%"
    # kaschiert).
    assert "Rechenvorschlag" in zeile.status_grund
    assert "Rohveränderung" in zeile.status_grund and "wirksame Veränderung" in zeile.status_grund
    assert "2026-06" in zeile.status_grund
    # Ohne bestätigte Gesamtmiete-Trennung: hier IST eine bestätigte
    # Gesamtmiete hinterlegt, also ist vorschlag_cent gesetzt.
    assert zeile.vorschlag_cent is not None
    assert zeile.differenz_cent is not None


def test_gewerbevorschau_ohne_bestaetigte_gesamtmiete_liefert_nur_isolierten_indexanteil(bundle, stammdaten_repo, basis_vertrag):
    vertrag, _konto = basis_vertrag
    qf = _volle_quellenfakten(vertrag.id, bestaetigte_gesamtmiete_cent=None, bestaetigte_gesamtmiete_quelle=None)
    bundle.quellen_fakten_repo.anlegen(qf)
    bundle.vpi_repo.monatswert_erfassen(
        reihe="VPI20C18", jahr=2026, monat=6, wert=Decimal("140.0"), finalitaet="ENDGUELTIG",
        quelle_datei="test.csv", quelle_zeile=1, quelle_hash="h", abgerufen_am=datetime(2026, 7, 1, tzinfo=timezone.utc),
        importiert_von="test",
    )
    laeufe = [_lauf(vertrag_id=vertrag.id, periode="2026-09", status="BLOCKIERT", blockiert_gruende=["Kein gültiges, freigegebenes Rechtsprofil (fehlt, nie freigegeben, oder seit Freigabe entwertet)."])]
    bundle.monatsbericht_service.erstellen_fuer_periode(periode="2026-09", laeufe=laeufe, heute=date(2026, 9, 19))
    zeile = bundle.monatsbericht_repo.zeilen_fuer_periode("2026-09")[0]
    assert zeile.vorschlag_cent is None
    assert zeile.differenz_cent is not None
    assert "Keine bestätigte aktuelle Gesamtmiete" in zeile.status_grund


# -- Schlussreview-Korrektur (2d45e27, Punkt 40(5)): Delta gegen den
# tatsächlich AKTUELLEN Indexanteil, nie gegen die ursprüngliche Basis --


def test_gewerbevorschau_rechnet_delta_gegen_aktuellen_nicht_gegen_urspruenglichen_anteil(
    bundle, stammdaten_repo, basis_vertrag,
):
    """Codex' Beispiel: original 50000 EUR-Cent @ VPI 130, HEUTE bereits
    55000 EUR-Cent (frühere Teilerhöhung) plus 20000 EUR-Cent BK = 75000
    Gesamt. Neuer Indexanteil (kumulierte Veränderung seit Basis) = 60000.
    Erwartetes Delta = 60000 - 55000 = 5000, neues Gesamt = 80000 - NICHT
    85000 (der alte, doppelt zählende Fehler: 75000 + (60000-50000))."""

    vertrag, _konto = basis_vertrag
    qf = _volle_quellenfakten(
        vertrag.id, aktueller_indexbetrag_cent=55_000, bestaetigte_gesamtmiete_cent=75_000,
        bestaetigte_gesamtmiete_quelle="Kontoauszug September 2026",
    )
    bundle.quellen_fakten_repo.anlegen(qf)
    # 130 * 1,2 = 156 ergibt exakt die im Beispiel vorausgesetzten 60000
    # (50000 * 1,2) als neuen Indexanteil.
    bundle.vpi_repo.monatswert_erfassen(
        reihe="VPI20C18", jahr=2026, monat=6, wert=Decimal("156.0"), finalitaet="ENDGUELTIG",
        quelle_datei="test.csv", quelle_zeile=1, quelle_hash="h", abgerufen_am=datetime(2026, 7, 1, tzinfo=timezone.utc),
        importiert_von="test",
    )
    laeufe = [_lauf(vertrag_id=vertrag.id, periode="2026-09", status="BLOCKIERT", blockiert_gruende=["Kein gültiges, freigegebenes Rechtsprofil (fehlt, nie freigegeben, oder seit Freigabe entwertet)."])]
    bundle.monatsbericht_service.erstellen_fuer_periode(periode="2026-09", laeufe=laeufe, heute=date(2026, 9, 19))
    zeile = bundle.monatsbericht_repo.zeilen_fuer_periode("2026-09")[0]
    assert zeile.differenz_cent == 5_000
    assert zeile.vorschlag_cent == 80_000
    assert zeile.vorschlag_cent != 85_000


def test_gewerbevorschau_unveraenderter_anteil_ergibt_dasselbe_wie_zuvor(bundle, stammdaten_repo, basis_vertrag):
    """Codex' zweites Beispiel: OHNE zwischenzeitliche Erhöhung (aktueller
    Anteil == ursprünglicher Anteil == 50000) bleibt das Ergebnis
    identisch zur (für diesen Sonderfall bereits vorher korrekten)
    alten Rechnung: 200000 + (60000-50000) = 210000."""

    vertrag, _konto = basis_vertrag
    qf = _volle_quellenfakten(vertrag.id, aktueller_indexbetrag_cent=50_000, bestaetigte_gesamtmiete_cent=200_000)
    bundle.quellen_fakten_repo.anlegen(qf)
    bundle.vpi_repo.monatswert_erfassen(
        reihe="VPI20C18", jahr=2026, monat=6, wert=Decimal("156.0"), finalitaet="ENDGUELTIG",
        quelle_datei="test.csv", quelle_zeile=1, quelle_hash="h", abgerufen_am=datetime(2026, 7, 1, tzinfo=timezone.utc),
        importiert_von="test",
    )
    laeufe = [_lauf(vertrag_id=vertrag.id, periode="2026-09", status="BLOCKIERT", blockiert_gruende=["Kein gültiges, freigegebenes Rechtsprofil (fehlt, nie freigegeben, oder seit Freigabe entwertet)."])]
    bundle.monatsbericht_service.erstellen_fuer_periode(periode="2026-09", laeufe=laeufe, heute=date(2026, 9, 19))
    zeile = bundle.monatsbericht_repo.zeilen_fuer_periode("2026-09")[0]
    assert zeile.differenz_cent == 10_000
    assert zeile.vorschlag_cent == 210_000


def test_gewerbevorschau_unter_schwelle_laesst_aktuellen_anteil_unveraendert(bundle, stammdaten_repo, basis_vertrag):
    """"Bei Unter-Schwelle darf keine fiktive Rücknahme bereits
    enthaltener Erhöhung entstehen: dann aktuellenTeil unverändert
    lassen." - selbst wenn der aktuelle Anteil (55000) HÖHER ist als
    eine Hochrechnung der ursprünglichen Basis (was ohne diese Regel
    einen negativen/rückwirkenden Delta ergäbe), bleibt das Delta bei
    unterschrittener Schwelle exakt 0."""

    vertrag, _konto = basis_vertrag
    qf = _volle_quellenfakten(vertrag.id, aktueller_indexbetrag_cent=55_000, bestaetigte_gesamtmiete_cent=75_000)
    bundle.quellen_fakten_repo.anlegen(qf)
    # Rohe Veränderung 130 -> 131 ist 0,77% - klar unter der 3%-Schwelle.
    bundle.vpi_repo.monatswert_erfassen(
        reihe="VPI20C18", jahr=2026, monat=6, wert=Decimal("131.0"), finalitaet="ENDGUELTIG",
        quelle_datei="test.csv", quelle_zeile=1, quelle_hash="h", abgerufen_am=datetime(2026, 7, 1, tzinfo=timezone.utc),
        importiert_von="test",
    )
    laeufe = [_lauf(vertrag_id=vertrag.id, periode="2026-09", status="BLOCKIERT", blockiert_gruende=["Kein gültiges, freigegebenes Rechtsprofil (fehlt, nie freigegeben, oder seit Freigabe entwertet)."])]
    bundle.monatsbericht_service.erstellen_fuer_periode(periode="2026-09", laeufe=laeufe, heute=date(2026, 9, 19))
    zeile = bundle.monatsbericht_repo.zeilen_fuer_periode("2026-09")[0]
    assert zeile.differenz_cent == 0
    assert zeile.vorschlag_cent == 75_000


def test_gewerbevorschau_fehlender_aktueller_anteil_ist_fail_closed(bundle, stammdaten_repo, basis_vertrag):
    """"Vorschlag nur bei belegtem aktuellem Teilbetrag ... Fehlt er,
    fail-closed kein Gesamtvorschlag." - bei überschrittener Schwelle
    OHNE `aktueller_indexbetrag_cent` bleibt sowohl `differenz_cent` als
    auch `vorschlag_cent` `None`, obwohl die Prozentinformation weiterhin
    als reine Information gezeigt wird."""

    vertrag, _konto = basis_vertrag
    qf = _volle_quellenfakten(vertrag.id, aktueller_indexbetrag_cent=None, bestaetigte_gesamtmiete_cent=75_000)
    bundle.quellen_fakten_repo.anlegen(qf)
    bundle.vpi_repo.monatswert_erfassen(
        reihe="VPI20C18", jahr=2026, monat=6, wert=Decimal("156.0"), finalitaet="ENDGUELTIG",
        quelle_datei="test.csv", quelle_zeile=1, quelle_hash="h", abgerufen_am=datetime(2026, 7, 1, tzinfo=timezone.utc),
        importiert_von="test",
    )
    laeufe = [_lauf(vertrag_id=vertrag.id, periode="2026-09", status="BLOCKIERT", blockiert_gruende=["Kein gültiges, freigegebenes Rechtsprofil (fehlt, nie freigegeben, oder seit Freigabe entwertet)."])]
    bundle.monatsbericht_service.erstellen_fuer_periode(periode="2026-09", laeufe=laeufe, heute=date(2026, 9, 19))
    zeile = bundle.monatsbericht_repo.zeilen_fuer_periode("2026-09")[0]
    assert zeile.differenz_cent is None
    assert zeile.vorschlag_cent is None
    assert "Kein belegter aktueller Indexanteil" in zeile.status_grund
    assert "Schwelle überschritten" in zeile.status_grund


def test_gewerbevorschau_zeigt_normale_dezimaldarstellung_statt_wissenschaftlicher_notation(
    bundle, stammdaten_repo, basis_vertrag,
):
    """Schlussreview-Präzisierung: eine privat importierte Basis wie
    "1.3E+2" darf NICHT als wissenschaftliche Notation im Anzeigetext
    landen, und Prozentwerte dürfen NICHT mit bis zu 28 Nachkommastellen
    erscheinen - nur die ANZEIGE wird gerundet, die Rechnung selbst bleibt
    unverändert (siehe die exakten Delta-Werte in den Tests oben)."""

    vertrag, _konto = basis_vertrag
    qf = _volle_quellenfakten(
        vertrag.id, urspruengliche_klauselbasis_wert="1.3E+2", aktueller_indexbetrag_cent=50_000,
        bestaetigte_gesamtmiete_cent=200_000,
    )
    bundle.quellen_fakten_repo.anlegen(qf)
    bundle.vpi_repo.monatswert_erfassen(
        reihe="VPI20C18", jahr=2026, monat=6, wert=Decimal("133.9"), finalitaet="ENDGUELTIG",
        quelle_datei="test.csv", quelle_zeile=1, quelle_hash="h", abgerufen_am=datetime(2026, 7, 1, tzinfo=timezone.utc),
        importiert_von="test",
    )
    laeufe = [_lauf(vertrag_id=vertrag.id, periode="2026-09", status="BLOCKIERT", blockiert_gruende=["Kein gültiges, freigegebenes Rechtsprofil (fehlt, nie freigegeben, oder seit Freigabe entwertet)."])]
    bundle.monatsbericht_service.erstellen_fuer_periode(periode="2026-09", laeufe=laeufe, heute=date(2026, 9, 19))
    zeile = bundle.monatsbericht_repo.zeilen_fuer_periode("2026-09")[0]
    assert "E+" not in zeile.status_grund and "e+" not in zeile.status_grund
    assert "130,0" in zeile.status_grund
    # Kein Rohveränderungswert mit mehr als 4 Nachkommastellen.
    import re

    for prozent_text in re.findall(r"(\d+,\d+)%", zeile.status_grund):
        nachkomma = prozent_text.split(",")[1]
        assert len(nachkomma) == 4, zeile.status_grund


# -- Jahreswechsel: dynamische Aprilgrenze -----------------------------------


def test_naechste_gesetzliche_april_grenze_ist_dynamisch_ueber_jahreswechsel():
    assert _naechste_gesetzliche_april_grenze(date(2026, 9, 19)) == date(2027, 4, 1)
    assert _naechste_gesetzliche_april_grenze(date(2027, 4, 1)) == date(2028, 4, 1)
    assert _naechste_gesetzliche_april_grenze(date(2027, 3, 31)) == date(2027, 4, 1)


# -- Umfang C: Import-Validierung (Typen/Decimal/Basis>0/Bool-Strenge) -------


def test_import_lehnt_string_false_fuer_wohnungsflag_ab():
    with pytest.raises(RechtsprofilImportFehlerError, match="ist_wohnungsnutzung"):
        _quellen_fakten_felder_aus_zeile({"vertrag_id": "V-1", "ist_wohnungsnutzung": "false"}, 0)


def test_import_akzeptiert_explizites_bool_fuer_wohnungsflag():
    felder = _quellen_fakten_felder_aus_zeile({"vertrag_id": "V-1", "ist_wohnungsnutzung": False}, 0)
    assert felder["ist_wohnungsnutzung"] is False


def test_import_lehnt_nicht_positive_klauselbasis_ab():
    for wert in (0, -1, "0"):
        with pytest.raises(RechtsprofilImportFehlerError, match="urspruengliche_klauselbasis.wert"):
            _quellen_fakten_felder_aus_zeile(
                {"vertrag_id": "V-1", "urspruengliche_klauselbasis": {"reihe": "VPI20C18", "monat": "2024-01", "wert": wert}}, 0
            )


def test_import_lehnt_nan_und_infinity_ab():
    for wert in (float("nan"), float("inf"), "nan"):
        with pytest.raises(RechtsprofilImportFehlerError, match="endlich"):
            _quellen_fakten_felder_aus_zeile({"vertrag_id": "V-1", "schwelle_prozent": wert}, 0)


def test_import_lehnt_bool_fuer_cent_felder_ab():
    with pytest.raises(RechtsprofilImportFehlerError, match="urspruenglicher_indexbetrag_cent"):
        _quellen_fakten_felder_aus_zeile({"vertrag_id": "V-1", "urspruenglicher_indexbetrag_cent": True}, 0)


def test_import_lehnt_float_fuer_cent_felder_ab():
    with pytest.raises(RechtsprofilImportFehlerError, match="bestaetigte_gesamtmiete_cent"):
        _quellen_fakten_felder_aus_zeile({"vertrag_id": "V-1", "bestaetigte_gesamtmiete_cent": 123.45}, 0)


def test_import_quellen_fakten_batch_ist_atomar(bundle):
    """Codex: "Bei reinem quellen_fakten-Batch wenigstens atomar
    schreiben; keine halben 17 Datensätze" - ein Fehler mitten im Batch
    (hier: doppelte (vertrag_id, version)) darf KEINE einzige Zeile des
    Batches in der Datenbank hinterlassen."""

    gueltig = IndexQuellenFaktenTable(vertrag_id="V-1", version=1, quelle="Test", inhalt_hash="h1", erstellt_von="test")
    kollidierend = IndexQuellenFaktenTable(vertrag_id="V-1", version=1, quelle="Test", inhalt_hash="h2", erstellt_von="test")
    with pytest.raises(Exception):
        bundle.quellen_fakten_repo.anlegen_batch([gueltig, kollidierend])
    assert bundle.quellen_fakten_repo.liste_fuer_vertrag("V-1") == []


def test_import_unveraenderter_reimport_erzeugt_keine_neue_version(bundle):
    zeile = {"vertrag_id": "V-1", "ist_wohnungsnutzung": False}
    felder = _quellen_fakten_felder_aus_zeile(zeile, 0)
    from mietinkasso.op.service import compute_content_hash

    inhalt_hash = compute_content_hash(felder)
    assert bundle.quellen_fakten_repo.existiert_inhalt_bereits("V-1", inhalt_hash) is False
    bundle.quellen_fakten_repo.anlegen(IndexQuellenFaktenTable(vertrag_id="V-1", version=1, quelle="Test", inhalt_hash=inhalt_hash, erstellt_von="test"))
    assert bundle.quellen_fakten_repo.existiert_inhalt_bereits("V-1", inhalt_hash) is True
