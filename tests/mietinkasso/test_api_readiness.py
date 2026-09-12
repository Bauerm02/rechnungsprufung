"""Tests für `infrastructure/readiness.py::datenbank_lesend_erreichbar`
(von `api/app.py::/ready` benutzt, Auftrag 12.09., Paket A/B: "Health/
Readiness prueft DB lesend"). Bewusst GETRENNT von `api/app.py`
importiert (kein FastAPI/Settings-Seiteneffekt) - jedes andere
Testmodul, das `mietinkasso.api.app` selbst importiert, muss das
NACH `test_backoffice.py` tun (siehe dessen Moduldocstring); dieses
Modul umgeht das Problem, indem es die Logik aus einem eigenen,
seiteneffektfreien Modul testet.

Codex-Rückprüfung (Paket A): eine fehlende SQLite-Datei darf NIE
stillschweigend neu angelegt werden, nur weil `/ready` sie anfasst -
und ein bloßes "SELECT 1" beweist kein vorhandenes Schema. Beide Fälle
sind hier explizit abgedeckt."""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.infrastructure.db.base import Base
from mietinkasso.infrastructure.db.session import build_engine, build_session_factory
from mietinkasso.infrastructure.readiness import datenbank_lesend_erreichbar


def test_erreichbare_db_mit_vollstaendigem_schema_liefert_true(session_factory):
    assert datenbank_lesend_erreichbar(session_factory) is True


def test_nicht_erreichbare_db_liefert_false_statt_exception():
    """Eine `session_factory`, die beim Verbindungsaufbau/Ausführen
    scheitert, darf niemals eine Exception nach außen durchreichen -
    `/ready` muss daraus sauber 503 machen können, nicht 500."""

    def _kaputte_session_factory():
        raise RuntimeError("Verbindung fehlgeschlagen (simuliert)")

    assert datenbank_lesend_erreichbar(_kaputte_session_factory) is False


def test_fehlende_sqlite_datei_liefert_false_und_erzeugt_keine_datei(tmp_path):
    """Codex-Rückprüfung: der frühere `SELECT 1`-Ansatz hätte gegen eine
    fehlende Datei stillschweigend eine neue, leere SQLite-Datei
    angelegt und 'bereit' gemeldet."""

    fehlender_pfad = tmp_path / "unterordner" / "produktiv.db"
    factory = build_session_factory(f"sqlite:///{fehlender_pfad}")

    assert datenbank_lesend_erreichbar(factory) is False
    assert not fehlender_pfad.exists()
    assert not fehlender_pfad.parent.exists()  # nicht einmal das Verzeichnis wurde angelegt


def test_vorhandene_datei_ohne_schema_liefert_false(tmp_path):
    """Eine existierende, aber leere (schemalose) SQLite-Datei ist KEIN
    gültiger Bereitschaftsnachweis - die Kerntabellen fehlen."""

    leere_datei = tmp_path / "leer.db"
    leere_datei.touch()
    factory = build_session_factory(f"sqlite:///{leere_datei}")

    assert datenbank_lesend_erreichbar(factory) is False


def test_vorhandene_datei_mit_vollstaendigem_schema_liefert_true(tmp_path):
    pfad = tmp_path / "vollstaendig.db"
    engine = build_engine(f"sqlite:///{pfad}")
    Base.metadata.create_all(engine)
    engine.dispose()

    factory = build_session_factory(f"sqlite:///{pfad}")
    assert datenbank_lesend_erreichbar(factory) is True


def test_memory_datenbank_wird_nicht_als_fehlende_datei_behandelt():
    """`:memory:` hat keine Datei, für die "existiert nicht" Sinn ergibt -
    mit vollständigem Schema muss die Prüfung trotzdem True liefern."""

    engine = build_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, future=True, expire_on_commit=False, class_=Session)
    assert datenbank_lesend_erreichbar(factory) is True
