from __future__ import annotations

import pytest

from mietinkasso.variableabrechnung.csv_import import (
    VariableAbrechnungImportNichtAnwendbarError,
    erstelle_plan,
    parse_csv,
    plan_hash,
    wende_an,
)
from mietinkasso.variableabrechnung.repository import VariableAbrechnungRepository
from mietinkasso.variableabrechnung.service import VariableAbrechnungService

_HEADER = (
    "einheit_id,art,leistungsmonat,belegdatum,quelle_referenz,status,"
    "berichteter_betrag,berichteter_betragsart,unser_netto_anteil,"
    "tatsaechlicher_zahlungseingang,vermietete_einheiten,vermietete_flaeche_qm,"
    "aenderungsgrund,import_id"
)


def _zeile(
    einheit_id="601-KURZ1", art="KURZZEITVERMIETUNG", leistungsmonat="2026-08", belegdatum="2026-09-05",
    quelle_referenz="Betreiberreport August", status="ENTWURF", berichteter_betrag="", berichteter_betragsart="",
    unser_netto_anteil="", zahlungseingang="", einheiten="", flaeche="", aenderungsgrund="", import_id="",
) -> str:
    return ",".join(
        [
            einheit_id, art, leistungsmonat, belegdatum, quelle_referenz, status, berichteter_betrag,
            berichteter_betragsart, unser_netto_anteil, zahlungseingang, einheiten, flaeche, aenderungsgrund,
            import_id,
        ]
    )


@pytest.fixture
def repo(session_factory) -> VariableAbrechnungRepository:
    return VariableAbrechnungRepository(session_factory)


@pytest.fixture
def service(repo, stammdaten_repo) -> VariableAbrechnungService:
    return VariableAbrechnungService(repo, stammdaten_repo)


@pytest.fixture
def kurzzeit_einheit(stammdaten_repo):
    stammdaten_repo.upsert_gesellschaft(id="7DI", name="7D Immobilien GmbH")
    stammdaten_repo.upsert_objekt(id="601", gesellschaft_id="7DI", bezeichnung="Am Corso")
    stammdaten_repo.upsert_einheit(id="601-KURZ1", objekt_id="601", bezeichnung="Kurzzeit 1", nutzungsstatus="KURZZEITVERMIETUNG")
    return "601-KURZ1"


def test_plan_neue_zeile(kurzzeit_einheit, repo):
    text = _HEADER + "\n" + _zeile() + "\n"
    plan = erstelle_plan(parse_csv(text), repository=repo)
    assert [b.status for b in plan.befunde] == ["NEU"]


def test_plan_unbekannte_einheit(kurzzeit_einheit, repo):
    text = _HEADER + "\n" + _zeile(einheit_id="UNBEKANNT") + "\n"
    plan = erstelle_plan(parse_csv(text), repository=repo)
    assert plan.befunde[0].status == "KONFLIKT"


def test_plan_gesperrtes_objekt(kurzzeit_einheit, repo, stammdaten_repo):
    stammdaten_repo.upsert_objekt(id="601", gesellschaft_id="7DI", bezeichnung="Am Corso", ausgeschlossen=True)
    text = _HEADER + "\n" + _zeile() + "\n"
    plan = erstelle_plan(parse_csv(text), repository=repo)
    assert plan.befunde[0].status == "GESPERRT"


def test_plan_dublette_innerhalb_der_datei(kurzzeit_einheit, repo):
    text = _HEADER + "\n" + _zeile(import_id="A") + "\n" + _zeile(import_id="B") + "\n"
    plan = erstelle_plan(parse_csv(text), repository=repo)
    assert [b.status for b in plan.befunde] == ["NEU", "KONFLIKT"]


def test_plan_unveraendert_nach_identischem_reimport(admin_ctx, kurzzeit_einheit, repo, service):
    text = _HEADER + "\n" + _zeile() + "\n"
    zeilen = parse_csv(text)
    ergebnis = wende_an(
        zeilen, ctx=admin_ctx, bestaetigter_hash=plan_hash(zeilen), korrekturen_bestaetigt=False,
        service=service, repository=repo, akteur="markus",
    )
    assert ergebnis.anzahl_neu == 1
    plan = erstelle_plan(parse_csv(text), repository=repo)
    assert [b.status for b in plan.befunde] == ["UNVERAENDERT"]


def test_plan_korrektur_erforderlich_ohne_aenderungsgrund_ist_konflikt(admin_ctx, kurzzeit_einheit, repo, service):
    erste = _HEADER + "\n" + _zeile() + "\n"
    wende_an(
        parse_csv(erste), ctx=admin_ctx, bestaetigter_hash=plan_hash(parse_csv(erste)), korrekturen_bestaetigt=False,
        service=service, repository=repo, akteur="markus",
    )
    abweichend = _HEADER + "\n" + _zeile(quelle_referenz="Anderer Report") + "\n"
    plan = erstelle_plan(parse_csv(abweichend), repository=repo)
    assert plan.befunde[0].status == "KONFLIKT"
    assert "aenderungsgrund" in plan.befunde[0].grund


def test_plan_korrektur_mit_aenderungsgrund(admin_ctx, kurzzeit_einheit, repo, service):
    erste = _HEADER + "\n" + _zeile() + "\n"
    wende_an(
        parse_csv(erste), ctx=admin_ctx, bestaetigter_hash=plan_hash(parse_csv(erste)), korrekturen_bestaetigt=False,
        service=service, repository=repo, akteur="markus",
    )
    abweichend = _HEADER + "\n" + _zeile(quelle_referenz="Anderer Report", aenderungsgrund="Korrektur lt. Betreiber") + "\n"
    plan = erstelle_plan(parse_csv(abweichend), repository=repo)
    assert plan.befunde[0].status == "KORREKTUR"


def test_wende_an_veralteter_hash_wird_abgelehnt(admin_ctx, kurzzeit_einheit, repo, service):
    text = _HEADER + "\n" + _zeile() + "\n"
    with pytest.raises(ValueError):
        wende_an(
            parse_csv(text), ctx=admin_ctx, bestaetigter_hash="veraltet", korrekturen_bestaetigt=False,
            service=service, repository=repo, akteur="markus",
        )


def test_wende_an_korrektur_ohne_bestaetigung_wird_komplett_abgelehnt(admin_ctx, kurzzeit_einheit, repo, service):
    """Atomarität: eine Datei mit einer NEUEN Zeile UND einer
    korrekturbedürftigen Zeile darf OHNE explizite Bestätigung GAR
    NICHTS schreiben - auch nicht die unproblematische NEU-Zeile."""

    erste = _HEADER + "\n" + _zeile(einheit_id="601-KURZ1", leistungsmonat="2026-07") + "\n"
    wende_an(
        parse_csv(erste), ctx=admin_ctx, bestaetigter_hash=plan_hash(parse_csv(erste)), korrekturen_bestaetigt=False,
        service=service, repository=repo, akteur="markus",
    )

    gemischt = (
        _HEADER + "\n"
        + _zeile(einheit_id="601-KURZ1", leistungsmonat="2026-08") + "\n"  # NEU
        + _zeile(einheit_id="601-KURZ1", leistungsmonat="2026-07", quelle_referenz="Korrigierter Report", aenderungsgrund="") + "\n"  # KORREKTUR ohne Grund -> KONFLIKT
    )
    zeilen = parse_csv(gemischt)
    with pytest.raises(Exception):
        wende_an(
            zeilen, ctx=admin_ctx, bestaetigter_hash=plan_hash(zeilen), korrekturen_bestaetigt=True,
            service=service, repository=repo, akteur="markus",
        )
    assert repo.aktuelle_version("601-KURZ1", "KURZZEITVERMIETUNG", "2026-08") is None


def test_wende_an_korrektur_mit_bestaetigung_wird_uebernommen(admin_ctx, kurzzeit_einheit, repo, service):
    erste = _HEADER + "\n" + _zeile() + "\n"
    wende_an(
        parse_csv(erste), ctx=admin_ctx, bestaetigter_hash=plan_hash(parse_csv(erste)), korrekturen_bestaetigt=False,
        service=service, repository=repo, akteur="markus",
    )
    korrigiert = _HEADER + "\n" + _zeile(quelle_referenz="Korrigierter Report", aenderungsgrund="Korrektur lt. Betreiber") + "\n"
    zeilen = parse_csv(korrigiert)
    ergebnis = wende_an(
        zeilen, ctx=admin_ctx, bestaetigter_hash=plan_hash(zeilen), korrekturen_bestaetigt=True,
        service=service, repository=repo, akteur="markus",
    )
    assert ergebnis.anzahl_korrektur == 1
    aktuelle = repo.aktuelle_version("601-KURZ1", "KURZZEITVERMIETUNG", "2026-08")
    assert aktuelle.version == 2
    assert aktuelle.quelle_referenz == "Korrigierter Report"


def test_wende_an_korrektur_ohne_bestaetigungsflag_wird_abgelehnt(admin_ctx, kurzzeit_einheit, repo, service):
    erste = _HEADER + "\n" + _zeile() + "\n"
    wende_an(
        parse_csv(erste), ctx=admin_ctx, bestaetigter_hash=plan_hash(parse_csv(erste)), korrekturen_bestaetigt=False,
        service=service, repository=repo, akteur="markus",
    )
    korrigiert = _HEADER + "\n" + _zeile(quelle_referenz="Korrigierter Report", aenderungsgrund="Korrektur lt. Betreiber") + "\n"
    zeilen = parse_csv(korrigiert)
    with pytest.raises(VariableAbrechnungImportNichtAnwendbarError):
        wende_an(
            zeilen, ctx=admin_ctx, bestaetigter_hash=plan_hash(zeilen), korrekturen_bestaetigt=False,
            service=service, repository=repo, akteur="markus",
        )
    aktuelle = repo.aktuelle_version("601-KURZ1", "KURZZEITVERMIETUNG", "2026-08")
    assert aktuelle.version == 1  # unverändert - nichts wurde geschrieben


def test_wende_an_gesperrte_zeile_blockiert_gesamten_import(admin_ctx, kurzzeit_einheit, repo, service, stammdaten_repo):
    stammdaten_repo.upsert_objekt(id="602", gesellschaft_id="7DI", bezeichnung="Anderes Objekt", ausgeschlossen=True)
    stammdaten_repo.upsert_einheit(id="602-KURZ1", objekt_id="602", bezeichnung="Kurzzeit gesperrt", nutzungsstatus="KURZZEITVERMIETUNG")

    gemischt = (
        _HEADER + "\n"
        + _zeile(einheit_id="601-KURZ1") + "\n"  # NEU, unproblematisch
        + _zeile(einheit_id="602-KURZ1") + "\n"  # GESPERRT
    )
    zeilen = parse_csv(gemischt)
    with pytest.raises(VariableAbrechnungImportNichtAnwendbarError):
        wende_an(
            zeilen, ctx=admin_ctx, bestaetigter_hash=plan_hash(zeilen), korrekturen_bestaetigt=True,
            service=service, repository=repo, akteur="markus",
        )
    assert repo.aktuelle_version("601-KURZ1", "KURZZEITVERMIETUNG", "2026-08") is None


def test_synthetische_importvorlage_ist_gueltig():
    """Die im Repo mitgelieferte Vorlage (`importtemplates/
    variable_abrechnung.csv`, siehe README.md) muss sich mit `parse_csv`
    fehlerfrei einlesen lassen - stellt sicher, dass Vorlage und Parser
    nicht auseinanderlaufen."""

    from pathlib import Path

    pfad = Path(__file__).resolve().parents[2] / "src" / "mietinkasso" / "importtemplates" / "variable_abrechnung.csv"
    text = pfad.read_text(encoding="utf-8")
    zeilen = parse_csv(text)
    assert len(zeilen) == 3
    assert zeilen[0].art == "KURZZEITVERMIETUNG"
    assert zeilen[0].status == "ENTWURF"
    assert zeilen[1].art == "SELFSTORAGE"
    assert zeilen[1].unser_netto_anteil_cent == 86_000
    assert zeilen[2].aenderungsgrund == "Nettoanteil vom Betreiber final bestätigt"
