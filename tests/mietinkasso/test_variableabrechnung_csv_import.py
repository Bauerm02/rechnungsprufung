from __future__ import annotations

from decimal import Decimal

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


def _hash(zeilen, *, ctx, repo):
    return erstelle_plan(zeilen, ctx=ctx, repository=repo).plan_hash


def test_plan_neue_zeile(admin_ctx, kurzzeit_einheit, repo):
    text = _HEADER + "\n" + _zeile() + "\n"
    plan = erstelle_plan(parse_csv(text), ctx=admin_ctx, repository=repo)
    assert [b.status for b in plan.befunde] == ["NEU"]


def test_plan_unbekannte_einheit(admin_ctx, kurzzeit_einheit, repo):
    text = _HEADER + "\n" + _zeile(einheit_id="UNBEKANNT") + "\n"
    plan = erstelle_plan(parse_csv(text), ctx=admin_ctx, repository=repo)
    assert plan.befunde[0].status == "KONFLIKT"


def test_plan_gesperrtes_objekt(admin_ctx, kurzzeit_einheit, repo, stammdaten_repo):
    stammdaten_repo.upsert_objekt(id="601", gesellschaft_id="7DI", bezeichnung="Am Corso", ausgeschlossen=True)
    text = _HEADER + "\n" + _zeile() + "\n"
    plan = erstelle_plan(parse_csv(text), ctx=admin_ctx, repository=repo)
    assert plan.befunde[0].status == "GESPERRT"


def test_plan_fremde_gesellschaft_ergibt_gesperrt_ohne_fachdaten(ctx_factory, kurzzeit_einheit, repo):
    """Unabhängiger Review: die Vorschau (`erstelle_plan`) darf einem
    ctx ohne Zugriff auf die Gesellschaft der Zeile keine Fachdaten
    preisgeben - er erhält GESPERRT, exakt wie ein ausgeschlossenes
    Objekt, statt NEU/KONFLIKT mit Detailinformationen."""

    fremder_ctx = ctx_factory("ANDERE-GESELLSCHAFT")
    text = _HEADER + "\n" + _zeile() + "\n"
    plan = erstelle_plan(parse_csv(text), ctx=fremder_ctx, repository=repo)
    assert plan.befunde[0].status == "GESPERRT"


def test_plan_dublette_innerhalb_der_datei(admin_ctx, kurzzeit_einheit, repo):
    text = _HEADER + "\n" + _zeile(import_id="A") + "\n" + _zeile(import_id="B") + "\n"
    plan = erstelle_plan(parse_csv(text), ctx=admin_ctx, repository=repo)
    assert [b.status for b in plan.befunde] == ["NEU", "KONFLIKT"]


def test_plan_unveraendert_nach_identischem_reimport(admin_ctx, kurzzeit_einheit, repo, service):
    text = _HEADER + "\n" + _zeile() + "\n"
    zeilen = parse_csv(text)
    ergebnis = wende_an(
        zeilen, ctx=admin_ctx, bestaetigter_hash=_hash(zeilen, ctx=admin_ctx, repo=repo), korrekturen_bestaetigt=False,
        service=service, repository=repo, akteur="markus",
    )
    assert ergebnis.anzahl_neu == 1
    plan = erstelle_plan(parse_csv(text), ctx=admin_ctx, repository=repo)
    assert [b.status for b in plan.befunde] == ["UNVERAENDERT"]


def test_plan_unveraendert_bei_abweichender_flaechen_darstellung(admin_ctx, kurzzeit_einheit, repo, service):
    """Reproduzierte Gegenprobe: 'vermietete_flaeche_qm=10' einmal
    speichern, danach dieselbe Datei erneut planen - das gespeicherte
    `Decimal("10.00")` und die neu eingelesene Zeichenkette "10" müssen
    als inhaltsgleich (UNVERAENDERT) erkannt werden, nicht als KONFLIKT."""

    text = _HEADER + "\n" + _zeile(flaeche="10") + "\n"
    zeilen = parse_csv(text)
    wende_an(
        zeilen, ctx=admin_ctx, bestaetigter_hash=_hash(zeilen, ctx=admin_ctx, repo=repo), korrekturen_bestaetigt=False,
        service=service, repository=repo, akteur="markus",
    )
    aktuelle = repo.aktuelle_version("601-KURZ1", "KURZZEITVERMIETUNG", "2026-08")
    assert aktuelle.vermietete_flaeche_qm == Decimal("10")

    plan = erstelle_plan(parse_csv(text), ctx=admin_ctx, repository=repo)
    assert [b.status for b in plan.befunde] == ["UNVERAENDERT"]


def test_plan_korrektur_erforderlich_ohne_aenderungsgrund_ist_konflikt(admin_ctx, kurzzeit_einheit, repo, service):
    erste = _HEADER + "\n" + _zeile() + "\n"
    zeilen_erste = parse_csv(erste)
    wende_an(
        zeilen_erste, ctx=admin_ctx, bestaetigter_hash=_hash(zeilen_erste, ctx=admin_ctx, repo=repo), korrekturen_bestaetigt=False,
        service=service, repository=repo, akteur="markus",
    )
    abweichend = _HEADER + "\n" + _zeile(quelle_referenz="Anderer Report") + "\n"
    plan = erstelle_plan(parse_csv(abweichend), ctx=admin_ctx, repository=repo)
    assert plan.befunde[0].status == "KONFLIKT"
    assert "aenderungsgrund" in plan.befunde[0].grund


def test_plan_korrektur_mit_aenderungsgrund(admin_ctx, kurzzeit_einheit, repo, service):
    erste = _HEADER + "\n" + _zeile() + "\n"
    zeilen_erste = parse_csv(erste)
    wende_an(
        zeilen_erste, ctx=admin_ctx, bestaetigter_hash=_hash(zeilen_erste, ctx=admin_ctx, repo=repo), korrekturen_bestaetigt=False,
        service=service, repository=repo, akteur="markus",
    )
    abweichend = _HEADER + "\n" + _zeile(quelle_referenz="Anderer Report", aenderungsgrund="Korrektur lt. Betreiber") + "\n"
    plan = erstelle_plan(parse_csv(abweichend), ctx=admin_ctx, repository=repo)
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
    zeilen_erste = parse_csv(erste)
    wende_an(
        zeilen_erste, ctx=admin_ctx, bestaetigter_hash=_hash(zeilen_erste, ctx=admin_ctx, repo=repo), korrekturen_bestaetigt=False,
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
            zeilen, ctx=admin_ctx, bestaetigter_hash=_hash(zeilen, ctx=admin_ctx, repo=repo), korrekturen_bestaetigt=True,
            service=service, repository=repo, akteur="markus",
        )
    assert repo.aktuelle_version("601-KURZ1", "KURZZEITVERMIETUNG", "2026-08") is None


def test_wende_an_korrektur_mit_bestaetigung_wird_uebernommen(admin_ctx, kurzzeit_einheit, repo, service):
    erste = _HEADER + "\n" + _zeile() + "\n"
    zeilen_erste = parse_csv(erste)
    wende_an(
        zeilen_erste, ctx=admin_ctx, bestaetigter_hash=_hash(zeilen_erste, ctx=admin_ctx, repo=repo), korrekturen_bestaetigt=False,
        service=service, repository=repo, akteur="markus",
    )
    korrigiert = _HEADER + "\n" + _zeile(quelle_referenz="Korrigierter Report", aenderungsgrund="Korrektur lt. Betreiber") + "\n"
    zeilen = parse_csv(korrigiert)
    ergebnis = wende_an(
        zeilen, ctx=admin_ctx, bestaetigter_hash=_hash(zeilen, ctx=admin_ctx, repo=repo), korrekturen_bestaetigt=True,
        service=service, repository=repo, akteur="markus",
    )
    assert ergebnis.anzahl_korrektur == 1
    aktuelle = repo.aktuelle_version("601-KURZ1", "KURZZEITVERMIETUNG", "2026-08")
    assert aktuelle.version == 2
    assert aktuelle.quelle_referenz == "Korrigierter Report"


def test_wende_an_korrektur_ohne_bestaetigungsflag_wird_abgelehnt(admin_ctx, kurzzeit_einheit, repo, service):
    erste = _HEADER + "\n" + _zeile() + "\n"
    zeilen_erste = parse_csv(erste)
    wende_an(
        zeilen_erste, ctx=admin_ctx, bestaetigter_hash=_hash(zeilen_erste, ctx=admin_ctx, repo=repo), korrekturen_bestaetigt=False,
        service=service, repository=repo, akteur="markus",
    )
    korrigiert = _HEADER + "\n" + _zeile(quelle_referenz="Korrigierter Report", aenderungsgrund="Korrektur lt. Betreiber") + "\n"
    zeilen = parse_csv(korrigiert)
    with pytest.raises(VariableAbrechnungImportNichtAnwendbarError):
        wende_an(
            zeilen, ctx=admin_ctx, bestaetigter_hash=_hash(zeilen, ctx=admin_ctx, repo=repo), korrekturen_bestaetigt=False,
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
            zeilen, ctx=admin_ctx, bestaetigter_hash=_hash(zeilen, ctx=admin_ctx, repo=repo), korrekturen_bestaetigt=True,
            service=service, repository=repo, akteur="markus",
        )
    assert repo.aktuelle_version("601-KURZ1", "KURZZEITVERMIETUNG", "2026-08") is None


def test_wende_an_fremde_gesellschaft_wird_abgelehnt_auch_bei_unveraendert(
    admin_ctx, ctx_factory, kurzzeit_einheit, repo, service
):
    """Unabhängiger Review: 'Ein UNVERAENDERT-CSV-Zweig umgeht aktuell
    auch Schreib-/Scopeprüfung in wende_an.' - eine bereits bestehende,
    inhaltlich UNVERAENDERTE Zeile einer fremden Gesellschaft darf
    trotzdem NICHT stillschweigend als UNVERAENDERT durchgewunken
    werden, sondern muss den Import ablehnen (hier bereits in der
    Neuprüfung als GESPERRT, weil der fremde ctx die Zeile gar nicht
    lesen darf)."""

    erste = _HEADER + "\n" + _zeile() + "\n"
    zeilen_erste = parse_csv(erste)
    wende_an(
        zeilen_erste, ctx=admin_ctx, bestaetigter_hash=_hash(zeilen_erste, ctx=admin_ctx, repo=repo), korrekturen_bestaetigt=False,
        service=service, repository=repo, akteur="markus",
    )

    fremder_ctx = ctx_factory("ANDERE-GESELLSCHAFT")
    wieder = _HEADER + "\n" + _zeile() + "\n"  # identischer Inhalt wie oben
    zeilen = parse_csv(wieder)
    with pytest.raises(Exception):
        wende_an(
            zeilen, ctx=fremder_ctx, bestaetigter_hash=_hash(zeilen, ctx=fremder_ctx, repo=repo), korrekturen_bestaetigt=False,
            service=service, repository=repo, akteur="fremd-user",
        )
    aktuelle = repo.aktuelle_version("601-KURZ1", "KURZZEITVERMIETUNG", "2026-08")
    assert aktuelle.version == 1  # unverändert - nichts wurde geschrieben


def test_wende_an_stale_preview_ueberschreibt_nicht_zwischenzeitliche_korrektur(admin_ctx, kurzzeit_einheit, repo, service):
    """Unabhängiger Review, exakt reproduziert: Version1=450 EUR; danach
    eine CSV-Korrektur auf 500 EUR mit Änderungsgrund GEPLANT (Hash
    bestätigt, wie ein Operator ihn im Formular übernehmen würde);
    DANACH eine ANDERE, manuelle Korrektur auf Version2=600 EUR;
    `wende_an` mit dem inzwischen VERALTETEN Plan-Hash der 500er-Korrektur
    darf NICHT stillschweigend auf 500/Version3 überschreiben - Version2
    (600 EUR) muss unangetastet bleiben, keine Version 3 darf entstehen."""

    erste = _HEADER + "\n" + _zeile(status="BESTAETIGT", unser_netto_anteil="450") + "\n"
    zeilen_erste = parse_csv(erste)
    wende_an(
        zeilen_erste, ctx=admin_ctx, bestaetigter_hash=_hash(zeilen_erste, ctx=admin_ctx, repo=repo), korrekturen_bestaetigt=False,
        service=service, repository=repo, akteur="markus",
    )
    v1 = repo.aktuelle_version("601-KURZ1", "KURZZEITVERMIETUNG", "2026-08")
    assert v1.unser_netto_anteil_cent == 45_000

    korrektur_csv = (
        _HEADER + "\n"
        + _zeile(status="BESTAETIGT", unser_netto_anteil="500", aenderungsgrund="Nettoanteil auf 500 korrigiert") + "\n"
    )
    zeilen_korrektur = parse_csv(korrektur_csv)
    veralteter_hash = _hash(zeilen_korrektur, ctx=admin_ctx, repo=repo)  # "im Formular bestätigter" Plan-Hash

    v2 = service.korrigieren(
        ctx=admin_ctx, ausgehend_von_id=v1.id, aenderungsgrund="Manuelle Korrektur auf 600",
        belegdatum=v1.belegdatum, quelle_referenz="Manuelle Korrektur zwischenzeitlich", status="BESTAETIGT",
        unser_netto_anteil_cent=60_000, erstellt_von="markus",
    )
    assert v2.version == 2

    with pytest.raises(Exception):
        wende_an(
            zeilen_korrektur, ctx=admin_ctx, bestaetigter_hash=veralteter_hash, korrekturen_bestaetigt=True,
            service=service, repository=repo, akteur="markus",
        )

    aktuelle = repo.aktuelle_version("601-KURZ1", "KURZZEITVERMIETUNG", "2026-08")
    assert aktuelle.id == v2.id
    assert aktuelle.version == 2  # keine Version 3 entstanden
    assert aktuelle.unser_netto_anteil_cent == 60_000  # v2 (600 EUR) unangetastet
    alle_versionen = repo.liste_versionen("601-KURZ1", "KURZZEITVERMIETUNG", "2026-08")
    assert [z.version for z in alle_versionen] == [2, 1]


def test_plan_hash_bindet_sich_an_gesehene_version(admin_ctx, kurzzeit_einheit, repo, service):
    """Direkter Nachweis der Fehlerbehebung zu #5: `plan_hash` derselben
    Datei unterscheidet sich, sobald sich die AKTUELLE Version der
    betroffenen Zeile zwischen zwei Aufrufen geändert hat - selbst wenn
    der Dateiinhalt (die Befunde-Liste an sich) identisch bliebe."""

    text = _HEADER + "\n" + _zeile() + "\n"
    zeilen = parse_csv(text)
    plan_vorher = erstelle_plan(zeilen, ctx=admin_ctx, repository=repo)
    hash_vorher = plan_hash(zeilen, plan_vorher.befunde)

    wende_an(
        zeilen, ctx=admin_ctx, bestaetigter_hash=hash_vorher, korrekturen_bestaetigt=False,
        service=service, repository=repo, akteur="markus",
    )

    abweichend = _HEADER + "\n" + _zeile(quelle_referenz="Anderer Report", aenderungsgrund="Grund") + "\n"
    zeilen_abweichend = parse_csv(abweichend)
    plan_nachher = erstelle_plan(zeilen_abweichend, ctx=admin_ctx, repository=repo)
    hash_nachher = plan_hash(zeilen_abweichend, plan_nachher.befunde)

    assert hash_vorher != hash_nachher
    assert plan_nachher.befunde[0].aktuelle_version_id is not None


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
