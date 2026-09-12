"""Tests für `infrastructure/readiness.py::datenbank_lesend_erreichbar`
(von `api/app.py::/ready` benutzt, Auftrag 12.09., Paket A: "Health/
Readiness prueft DB lesend"). Bewusst GETRENNT von `api/app.py`
importiert (kein FastAPI/Settings-Seiteneffekt) - jedes andere
Testmodul, das `mietinkasso.api.app` selbst importiert, muss das
NACH `test_backoffice.py` tun (siehe dessen Moduldocstring); dieses
Modul umgeht das Problem, indem es die Logik aus einem eigenen,
seiteneffektfreien Modul testet."""

from __future__ import annotations

from sqlalchemy.orm import sessionmaker

from mietinkasso.infrastructure.readiness import datenbank_lesend_erreichbar


def test_erreichbare_db_liefert_true(session_factory):
    assert datenbank_lesend_erreichbar(session_factory) is True


def test_nicht_erreichbare_db_liefert_false_statt_exception():
    """Eine `session_factory`, die beim Verbindungsaufbau/Ausführen
    scheitert, darf niemals eine Exception nach außen durchreichen -
    `/ready` muss daraus sauber 503 machen können, nicht 500."""

    def _kaputte_session_factory():
        raise RuntimeError("Verbindung fehlgeschlagen (simuliert)")

    assert datenbank_lesend_erreichbar(_kaputte_session_factory) is False


def test_nicht_existierende_sqlite_datei_liefert_false(tmp_path):
    from mietinkasso.infrastructure.db.session import build_engine
    from sqlalchemy.orm import Session

    # Eine Read-Only-Verbindung auf eine nicht existierende Datei -
    # `SELECT 1` selbst braucht kein Schema, muss also grundsätzlich
    # funktionieren, sobald die Datei da ist; ohne Datei/Schreibrecht
    # schlägt der Verbindungsaufbau fehl.
    kaputter_pfad = tmp_path / "unterordner-existiert-nicht" / "db.sqlite"
    engine = build_engine(f"sqlite:///file:{kaputter_pfad}?mode=ro&uri=true")
    factory = sessionmaker(bind=engine, future=True, expire_on_commit=False, class_=Session)
    assert datenbank_lesend_erreichbar(factory) is False
