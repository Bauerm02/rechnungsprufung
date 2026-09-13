from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from mietinkasso.indexautomatik.repository import VpiRepository
from mietinkasso.indexautomatik.vpi_import import VpiImportFehlerError, importiere_ogd_csv

_HEADER = "C-VPIZR-0;C-VPICOICOP18_5-0;F-VPIMZBM;sonstige_ogd_spalte\n"
#: Öffentliche amtliche Beispielwerte, wörtlich aus dem unabhängigen
#: Zwischenreview 34c6fdd (verifiziertes reales OGD-Schema).
_ECHTE_BEISPIELZEILEN = [
    "VPIZR-2024;VPICOICOP18-0;123,80000;x",
    "VPIZR-2025;VPICOICOP18-0;128,20000;x",
    "VPIZR-202606;VPICOICOP18-0;132,20000;x",
    "VPIZR-202607;VPICOICOP18-0;132,20000;x",
]


@pytest.fixture
def vpi_repo(session_factory) -> VpiRepository:
    return VpiRepository(session_factory)


def _schreibe_csv(pfad, zeilen: list[str]) -> str:
    datei = pfad / "vpi.csv"
    datei.write_text(_HEADER + "\n".join(zeilen), encoding="utf-8-sig")
    return str(datei)


def test_import_echter_ogd_beispieldaten(tmp_path, vpi_repo):
    pfad = _schreibe_csv(tmp_path, _ECHTE_BEISPIELZEILEN)
    ergebnis = importiere_ogd_csv(
        pfad, reihe="VPI20C18", repository=vpi_repo, importiert_von="markus",
        abgerufen_am=datetime(2026, 8, 3, tzinfo=timezone.utc),
    )
    assert ergebnis.jahreszeilen == 2
    assert ergebnis.monatszeilen == 2
    assert ergebnis.endgueltige_monatszeilen == 1
    assert ergebnis.vorlaeufige_monatszeilen == 1


def test_letzter_monat_ist_vorlaeufig_fruehere_monate_endgueltig(tmp_path, vpi_repo):
    pfad = _schreibe_csv(tmp_path, _ECHTE_BEISPIELZEILEN)
    importiere_ogd_csv(
        pfad, reihe="VPI20C18", repository=vpi_repo, importiert_von="markus",
        abgerufen_am=datetime(2026, 8, 3, tzinfo=timezone.utc),
    )
    monatswerte = {(m.jahr, m.monat): m.finalitaet for m in vpi_repo.monatswerte_liste("VPI20C18", 2026)}
    assert monatswerte[(2026, 6)] == "ENDGUELTIG"
    assert monatswerte[(2026, 7)] == "VORLAEUFIG"


def test_amtlicher_jahresdurchschnitt_wird_direkt_uebernommen_nicht_selbst_gemittelt(tmp_path, vpi_repo):
    """"Jahresdurchschnitt 2025 amtlich 128,2 nutzen, NICHT ungerundeten
    selbstgemittelten Monatswert" - der importierte amtliche Jahreswert
    hat Vorrang vor jeder Eigenmittelung aus Monatswerten."""

    pfad = _schreibe_csv(tmp_path, _ECHTE_BEISPIELZEILEN)
    importiere_ogd_csv(
        pfad, reihe="VPI20C18", repository=vpi_repo, importiert_von="markus",
        abgerufen_am=datetime(2026, 8, 3, tzinfo=timezone.utc),
    )
    assert vpi_repo.jahresdurchschnitt("VPI20C18", 2025) == Decimal("128.20000")


def test_teilindex_zeilen_werden_gefiltert_nicht_importiert(tmp_path, vpi_repo):
    zeilen = list(_ECHTE_BEISPIELZEILEN) + ["VPIZR-202607;IRGENDEIN_TEILINDEX;999,00000;x"]
    pfad = _schreibe_csv(tmp_path, zeilen)
    ergebnis = importiere_ogd_csv(
        pfad, reihe="VPI20C18", repository=vpi_repo, importiert_von="markus",
        abgerufen_am=datetime(2026, 8, 3, tzinfo=timezone.utc),
    )
    assert ergebnis.monatszeilen == 2  # Teilindex-Zeile nicht mitgezählt


def test_doppelte_periode_blockiert_gesamten_import(tmp_path, vpi_repo):
    zeilen = list(_ECHTE_BEISPIELZEILEN) + ["VPIZR-202607;VPICOICOP18-0;140,00000;x"]
    pfad = _schreibe_csv(tmp_path, zeilen)
    with pytest.raises(VpiImportFehlerError):
        importiere_ogd_csv(
            pfad, reihe="VPI20C18", repository=vpi_repo, importiert_von="markus",
            abgerufen_am=datetime(2026, 8, 3, tzinfo=timezone.utc),
        )
    assert vpi_repo.monatswerte_liste("VPI20C18", 2026) == []


def test_unplausibler_wert_blockiert_gesamten_import(tmp_path, vpi_repo):
    zeilen = list(_ECHTE_BEISPIELZEILEN) + ["VPIZR-202608;VPICOICOP18-0;-5,0;x"]
    pfad = _schreibe_csv(tmp_path, zeilen)
    with pytest.raises(VpiImportFehlerError):
        importiere_ogd_csv(
            pfad, reihe="VPI20C18", repository=vpi_repo, importiert_von="markus",
            abgerufen_am=datetime(2026, 8, 3, tzinfo=timezone.utc),
        )
    assert vpi_repo.monatswerte_liste("VPI20C18", 2026) == []


def test_fehlendes_erwartetes_schema_wird_abgelehnt(tmp_path, vpi_repo):
    datei = tmp_path / "falsch.csv"
    datei.write_text("Jahr;Monat;Wert\n2024;1;100\n", encoding="utf-8-sig")
    with pytest.raises(VpiImportFehlerError):
        importiere_ogd_csv(
            str(datei), reihe="VPI20C18", repository=vpi_repo, importiert_von="markus",
            abgerufen_am=datetime(2026, 8, 3, tzinfo=timezone.utc),
        )


def test_import_ist_atomar_bei_fehler_in_spaeterer_zeile(tmp_path, vpi_repo):
    """Zwischenreview 34c6fdd: "die aktuelle Schleife commit je Zeile ist
    trotz Docstring nicht atomar" - ein Fehler DARF keine Teilmenge der
    Zeilen bereits geschrieben haben."""

    zeilen = list(_ECHTE_BEISPIELZEILEN) + ["VPIZR-KAPUTT;VPICOICOP18-0;100,0;x"]
    pfad = _schreibe_csv(tmp_path, zeilen)
    with pytest.raises(VpiImportFehlerError):
        importiere_ogd_csv(
            pfad, reihe="VPI20C18", repository=vpi_repo, importiert_von="markus",
            abgerufen_am=datetime(2026, 8, 3, tzinfo=timezone.utc),
        )
    assert vpi_repo.monatswerte_liste("VPI20C18", 2026) == []
    assert vpi_repo.jahresdurchschnitt("VPI20C18", 2024) is None
    assert vpi_repo.jahresdurchschnitt("VPI20C18", 2025) is None


def test_quelle_hash_und_abrufzeit_und_url_werden_gespeichert(tmp_path, vpi_repo):
    pfad = _schreibe_csv(tmp_path, _ECHTE_BEISPIELZEILEN)
    abrufzeit = datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc)
    importiere_ogd_csv(pfad, reihe="VPI20C18", repository=vpi_repo, importiert_von="markus", abgerufen_am=abrufzeit)

    monatswerte = vpi_repo.monatswerte_liste("VPI20C18", 2026)
    assert all(m.quelle_hash for m in monatswerte)
    assert all(m.abgerufen_am.replace(tzinfo=timezone.utc) == abrufzeit for m in monatswerte)

    jahreswerte = vpi_repo.jahreswert_liste("VPI20C18")
    assert all("OGD_vpi20c18" in j.quelle for j in jahreswerte)


def test_verschiedene_reihen_sind_unabhaengig(tmp_path, vpi_repo):
    pfad = _schreibe_csv(tmp_path, _ECHTE_BEISPIELZEILEN)
    importiere_ogd_csv(
        pfad, reihe="VPI15C18", repository=vpi_repo, importiert_von="markus",
        abgerufen_am=datetime(2026, 8, 3, tzinfo=timezone.utc),
    )
    assert vpi_repo.jahresdurchschnitt("VPI20C18", 2025) is None
    assert vpi_repo.jahresdurchschnitt("VPI15C18", 2025) == Decimal("128.20000")


def test_manueller_jahreswert_override_bleibt_moeglich(vpi_repo):
    vpi_repo.jahreswert_erfassen(
        reihe="VPI20C18", jahr=2027, wert=Decimal("130.0"), quelle="Statistik Austria Pressemitteilung",
        quelle_datum=date(2028, 2, 17), erfasst_von="markus",
    )
    assert vpi_repo.jahresdurchschnitt("VPI20C18", 2027) == Decimal("130.0")
