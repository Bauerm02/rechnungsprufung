from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from mietinkasso.indexautomatik.repository import VpiRepository
from mietinkasso.indexautomatik.vpi_import import (
    VpiImportFehlerError,
    VpiSpaltenzuordnung,
    importiere_monatswerte_csv,
)


@pytest.fixture
def vpi_repo(session_factory) -> VpiRepository:
    return VpiRepository(session_factory)


_SPALTEN = VpiSpaltenzuordnung(jahr_spalte="jahr", monat_spalte="monat", wert_spalte="wert", status_spalte="status")


def _schreibe_csv(pfad, zeilen: list[str]) -> str:
    datei = pfad / "vpi.csv"
    datei.write_text("jahr;monat;wert;status\n" + "\n".join(zeilen), encoding="utf-8")
    return str(datei)


def test_import_blockiert_gesamte_datei_bei_kaputter_zeile(tmp_path, vpi_repo):
    pfad = _schreibe_csv(tmp_path, ["2024;1;100,0;ENDGUELTIG", "2024;X;101,0;ENDGUELTIG"])
    with pytest.raises(VpiImportFehlerError):
        importiere_monatswerte_csv(
            pfad, reihe="VPI20C18", spalten=_SPALTEN, repository=vpi_repo, importiert_von="test",
            abgerufen_am=datetime(2026, 2, 17, tzinfo=timezone.utc),
        )
    assert vpi_repo.monatswerte_liste("VPI20C18", 2024) == []


def test_jahresdurchschnitt_erfordert_alle_12_monate_endgueltig(tmp_path, vpi_repo):
    zeilen = [f"2024;{m};100,0;ENDGUELTIG" for m in range(1, 12)]
    zeilen.append("2024;12;100,0;VORLAEUFIG")  # Dezember noch nicht final
    pfad = _schreibe_csv(tmp_path, zeilen)
    importiere_monatswerte_csv(
        pfad, reihe="VPI20C18", spalten=_SPALTEN, repository=vpi_repo, importiert_von="test",
        abgerufen_am=datetime(2025, 1, 20, tzinfo=timezone.utc),
    )
    assert vpi_repo.jahresdurchschnitt("VPI20C18", 2024) is None


def test_jahresdurchschnitt_wird_berechnet_wenn_alle_12_monate_endgueltig(tmp_path, vpi_repo):
    zeilen = [f"2024;{m};100,0;ENDGUELTIG" for m in range(1, 13)]
    pfad = _schreibe_csv(tmp_path, zeilen)
    importiere_monatswerte_csv(
        pfad, reihe="VPI20C18", spalten=_SPALTEN, repository=vpi_repo, importiert_von="test",
        abgerufen_am=datetime(2025, 2, 17, tzinfo=timezone.utc),
    )
    assert vpi_repo.jahresdurchschnitt("VPI20C18", 2024) == Decimal("100.0")


def test_manueller_jahreswert_override_hat_vorrang(tmp_path, vpi_repo):
    vpi_repo.jahreswert_erfassen(
        reihe="VPI20C18", jahr=2024, wert=Decimal("105.5"), quelle="Statistik Austria Pressemitteilung",
        quelle_datum=date(2025, 2, 17), erfasst_von="markus",
    )
    assert vpi_repo.jahresdurchschnitt("VPI20C18", 2024) == Decimal("105.5")


def test_quelle_hash_und_abrufzeit_werden_gespeichert(tmp_path, vpi_repo):
    pfad = _schreibe_csv(tmp_path, [f"2024;{m};100,0;ENDGUELTIG" for m in range(1, 13)])
    abrufzeit = datetime(2025, 2, 17, 9, 0, tzinfo=timezone.utc)
    importiere_monatswerte_csv(
        pfad, reihe="VPI20C18", spalten=_SPALTEN, repository=vpi_repo, importiert_von="test", abgerufen_am=abrufzeit
    )
    monatswerte = vpi_repo.monatswerte_liste("VPI20C18", 2024)
    assert all(m.quelle_hash for m in monatswerte)
    # SQLite liefert naive datetimes zurück (Wanduhrzeit bleibt erhalten,
    # tzinfo geht verloren) - konsistent mit anderen DateTime(timezone=True)
    # Spalten in diesem Repository.
    assert all(m.abgerufen_am.replace(tzinfo=timezone.utc) == abrufzeit for m in monatswerte)


def test_reihe_und_jahr_sind_unabhaengig(tmp_path, vpi_repo):
    pfad = _schreibe_csv(tmp_path, [f"2024;{m};100,0;ENDGUELTIG" for m in range(1, 13)])
    importiere_monatswerte_csv(
        pfad, reihe="VPI15C18", spalten=_SPALTEN, repository=vpi_repo, importiert_von="test",
        abgerufen_am=datetime(2025, 2, 17, tzinfo=timezone.utc),
    )
    assert vpi_repo.jahresdurchschnitt("VPI20C18", 2024) is None
    assert vpi_repo.jahresdurchschnitt("VPI15C18", 2024) == Decimal("100.0")
