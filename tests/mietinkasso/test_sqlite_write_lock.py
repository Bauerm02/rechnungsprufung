"""Tests für `infrastructure/db/sqlite_write_lock.py::schreibgesperrte_session`
(Codex-Rückprüfung Paket B: `with_for_update` ist unter SQLite ein
Kein-Op - kein echtes Zeilen-Locking).

Bewusst als EIGENE, deterministische Primitiv-Tests statt allein über
eine Geschäftslogik-Methode (`BankImportService.verknuepfe_mit_
bestehender_zahlung`) zu prüfen: ein reiner Business-Logik-Test mit
zwei nebenläufigen Threads kann eine echte Interleaving-Race nicht
zuverlässig erzwingen (abhängig vom OS-Thread-Scheduling/GIL -
gelegentlich laufen beide Threads durch schnellen Python-Code hindurch
"zufällig" sequenziell). Diese Tests halten die erste Transaktion
stattdessen über ein `threading.Event` kontrolliert offen und prüfen
direkt und zeitlich beweisbar, dass eine zweite gleichzeitige
`schreibgesperrte_session` unter Datei-SQLite tatsächlich blockiert,
bis die erste committet/zurückrollt."""

from __future__ import annotations

import threading
import time

from sqlalchemy import text

from mietinkasso.infrastructure.db.base import Base
from mietinkasso.infrastructure.db.session import build_engine, build_session_factory
from mietinkasso.infrastructure.db.sqlite_write_lock import schreibgesperrte_session


def _datei_session_factory(tmp_path, name: str = "lock.db"):
    pfad = tmp_path / name
    engine = build_engine(f"sqlite:///{pfad}")
    Base.metadata.create_all(engine)
    engine.dispose()
    return build_session_factory(f"sqlite:///{pfad}")


def test_zweite_schreibgesperrte_session_wartet_auf_die_erste_bei_datei_sqlite(tmp_path):
    factory = _datei_session_factory(tmp_path)

    a_ist_drin = threading.Event()
    a_darf_verlassen = threading.Event()
    zeiten: dict[str, float] = {}

    def _thread_a():
        with schreibgesperrte_session(factory) as session:
            session.execute(text("SELECT 1"))  # löst BEGIN IMMEDIATE tatsächlich aus
            a_ist_drin.set()
            a_darf_verlassen.wait(timeout=5)
            zeiten["a_committet_um"] = time.monotonic()
            session.commit()

    def _thread_b():
        assert a_ist_drin.wait(timeout=5), "Thread A ist nicht rechtzeitig in seine kritische Sektion gekommen"
        with schreibgesperrte_session(factory) as session:
            session.execute(text("SELECT 1"))  # muss blockieren, bis A committet
            zeiten["b_betritt_um"] = time.monotonic()
            session.commit()

    thread_a = threading.Thread(target=_thread_a)
    thread_b = threading.Thread(target=_thread_b)
    thread_a.start()
    assert a_ist_drin.wait(timeout=5)
    thread_b.start()

    # B muss jetzt (A hält die Sperre bewusst noch) blockiert sein - kurz
    # abwarten und sicherstellen, dass B noch NICHT durchgekommen ist.
    time.sleep(0.3)
    assert "b_betritt_um" not in zeiten, "B durfte nicht eintreten, solange A die Sperre noch hält"

    a_darf_verlassen.set()
    thread_a.join(timeout=5)
    thread_b.join(timeout=5)
    assert not thread_a.is_alive()
    assert not thread_b.is_alive()

    assert "b_betritt_um" in zeiten
    assert zeiten["b_betritt_um"] >= zeiten["a_committet_um"]


def test_schreibgesperrte_session_fuer_memory_db_ist_gewoehnliche_session():
    """Für `:memory:` gibt es keine Datei, gegen die zwei Verbindungen um
    einen echten Schreib-Lock konkurrieren könnten (ein `StaticPool`
    teilt ohnehin eine einzige Verbindung) - `schreibgesperrte_session`
    liefert dort unverändert eine gewöhnliche Session aus der
    übergebenen `session_factory`, baut keine zusätzliche Engine auf."""

    from sqlalchemy.orm import Session, sessionmaker

    engine = build_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, future=True, expire_on_commit=False, class_=Session)

    with schreibgesperrte_session(factory) as session:
        assert session.get_bind() is engine
