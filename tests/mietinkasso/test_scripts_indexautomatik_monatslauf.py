"""Regressionstests für scripts/indexautomatik_monatslauf.py (Auftrag
13.09., Betriebsverdrahtung): "scripts/indexautomatik_monatslauf.py
nutzt den geprüften Statistik-Austria-Client noch überhaupt nicht;
indexautomatik_vpi_automatischer_abruf ist ohne Wirkung. Bitte beim
aktivierten Flag die vier verifizierten amtlichen Reihen vor
Monatsprüfung ... aktualisieren, bei Fehler sichtbar sperren bzw. Job
scheitern lassen, niemals stille veraltete Werte."."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from mietinkasso.indexautomatik.repository import VpiRepository
from mietinkasso.indexautomatik.statistik_austria_client import VpiAbrufFehlerError
from mietinkasso.infrastructure.config import Settings


def _lade_script_modul():
    pfad = Path(__file__).resolve().parents[2] / "scripts" / "indexautomatik_monatslauf.py"
    spec = importlib.util.spec_from_file_location("indexautomatik_monatslauf_script", pfad)
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul


@pytest.fixture(scope="module")
def script_modul():
    return _lade_script_modul()


@pytest.fixture
def vpi_repo(session_factory) -> VpiRepository:
    return VpiRepository(session_factory)


def test_deaktivierter_automatischer_abruf_ist_ein_no_op(script_modul, vpi_repo):
    settings = Settings(database_url="sqlite:///:memory:", indexautomatik_vpi_automatischer_abruf=False)
    ergebnis = script_modul._aktualisiere_vpi_reihen(settings=settings, vpi_repository=vpi_repo)
    assert ergebnis == {}


def test_aktivierter_abruf_ohne_ablageverzeichnis_scheitert_sichtbar(script_modul, vpi_repo):
    settings = Settings(
        database_url="sqlite:///:memory:", indexautomatik_vpi_automatischer_abruf=True,
        indexautomatik_vpi_ablage_verzeichnis=None,
    )
    with pytest.raises(script_modul.VpiAktualisierungFehlgeschlagenError):
        script_modul._aktualisiere_vpi_reihen(settings=settings, vpi_repository=vpi_repo)


def test_fehlgeschlagener_abruf_bricht_ab_ohne_stillen_altwert(script_modul, vpi_repo, tmp_path, monkeypatch):
    """Schlägt der Abruf EINER Reihe fehl, wird der gesamte Vorgang
    abgebrochen (sichtbare Exception) statt stillschweigend mit
    bisherigen Werten weiterzumachen - und es wird nichts geschrieben."""

    class _KaputterClient:
        def __init__(self, *, ziel_verzeichnis):
            pass

        def abrufen(self, reihe):
            raise VpiAbrufFehlerError(f"Fake-Fehler für {reihe}")

    monkeypatch.setattr(script_modul, "StatistikAustriaClient", _KaputterClient)

    settings = Settings(
        database_url="sqlite:///:memory:", indexautomatik_vpi_automatischer_abruf=True,
        indexautomatik_vpi_ablage_verzeichnis=str(tmp_path),
    )
    with pytest.raises(script_modul.VpiAktualisierungFehlgeschlagenError):
        script_modul._aktualisiere_vpi_reihen(settings=settings, vpi_repository=vpi_repo)
    assert vpi_repo.jahreswert_liste() == []


def test_erfolgreicher_abruf_importiert_alle_vier_reihen(script_modul, vpi_repo, tmp_path, monkeypatch):
    def _csv_fuer(reihe: str) -> bytes:
        schema = script_modul._REIHEN_SCHEMA[reihe]
        dimension_spalte = schema["dimension_spalte"]
        gesamtindex_wert = schema["gesamtindex_wert"]
        zeilen = [
            f"C-VPIZR-0;{dimension_spalte};F-VPIMZBM",
            f"VPIZR-202406;{gesamtindex_wert};123,80000",
            f"VPIZR-202407;{gesamtindex_wert};124,10000",
        ]
        return ("\r\n".join(zeilen) + "\r\n").encode("utf-8-sig")

    class _FakeAbruf:
        def __init__(self, reihe, pfad, url):
            self.reihe = reihe
            self.pfad = pfad
            self.url = url
            from datetime import datetime, timezone

            self.abgerufen_am = datetime(2026, 9, 13, tzinfo=timezone.utc)

    class _FakeClient:
        def __init__(self, *, ziel_verzeichnis):
            self._ziel_verzeichnis = Path(ziel_verzeichnis)

        def abrufen(self, reihe):
            self._ziel_verzeichnis.mkdir(parents=True, exist_ok=True)
            pfad = self._ziel_verzeichnis / f"{reihe}.csv"
            pfad.write_bytes(_csv_fuer(reihe))
            return _FakeAbruf(reihe, pfad, f"https://example.invalid/{reihe}.csv")

    monkeypatch.setattr(script_modul, "StatistikAustriaClient", _FakeClient)

    settings = Settings(
        database_url="sqlite:///:memory:", indexautomatik_vpi_automatischer_abruf=True,
        indexautomatik_vpi_ablage_verzeichnis=str(tmp_path),
    )
    ergebnis = script_modul._aktualisiere_vpi_reihen(settings=settings, vpi_repository=vpi_repo)
    assert set(ergebnis) == {"VPI20C18", "VPI15C18", "VPI00", "VPI96"}
    assert all(anzahl == 2 for anzahl in ergebnis.values())
