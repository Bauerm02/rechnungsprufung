"""Echte Schreibserialisierung für kritische "lesen, dann entscheiden,
dann schreiben"-Abläufe unter Datei-SQLite (Codex-Rückprüfung Paket B).

`with_for_update=True` (z. B. `session.get(..., with_for_update=True)`)
ist unter SQLite ein KEIN-OP - der SQLite-Dialekt kennt keine echten
Zeilensperren und kompiliert `FOR UPDATE` schlicht weg. Eine Standard-
ORM-Transaktion nimmt ihren tatsächlichen Schreib-Lock (SQLite: RESERVED)
außerdem erst bei ihrem ERSTEN Schreibbefehl (`BEGIN` = DEFERRED), nicht
bei ihrem ersten Lesebefehl. Zwei ECHT gleichzeitige Verbindungen können
deshalb in einem Ablauf wie "Restbetrag lesen -> prüfen -> ggf.
schreiben" denselben, noch nicht committeten Zwischenstand lesen, bevor
eine von beiden schreibt (reproduziert mit zwei Datei-SQLite-
Verbindungen/Threads auf dieselbe Zahlung).

`schreibgesperrte_session` öffnet für genau einen solchen kritischen
Abschnitt eine Session, deren Transaktion unter Datei-SQLite bereits bei
ihrem ERSTEN Statement `BEGIN IMMEDIATE` ausführt - der Schreib-Lock wird
sofort genommen, eine zweite gleichzeitige kritische Sektion blockiert
(bis zum sqlite3-Busy-Timeout), statt denselben veralteten Zustand zu
lesen, und sieht nach dem Freiwerden den bereits committeten,
tatsächlich aktuellen Stand.

Bewusst eine EIGENE, separate Engine/Verbindung zur SELBEN Datei statt
einer globalen Änderung von `infrastructure/db/session.py::build_engine`
- jeder andere Codepfad (inkl. PostgreSQL-Betrieb, der weiterhin echte
`SELECT ... FOR UPDATE`-Zeilensperren nutzt) bleibt unverändert. Für
PostgreSQL und für `:memory:`-Datenbanken (in Tests ohnehin über einen
geteilten `StaticPool` eine einzige Verbindung) wird unverändert die
übergebene `session_factory` direkt verwendet - dort ist diese
Umschaltung weder nötig noch wirksam."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker


def _ist_datei_sqlite(engine) -> bool:
    if engine.dialect.name != "sqlite":
        return False
    pfad = engine.url.database
    return bool(pfad) and pfad != ":memory:"


def _neue_begin_immediate_engine(url):
    engine = create_engine(url, future=True, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pysqlite_eigenes_begin_abschalten(dbapi_connection, connection_record):  # noqa: ARG001
        # Ohne dies handhabt der pysqlite-Treiber Transaktionen selbst
        # (immer DEFERRED, erst beim ersten Schreibbefehl) und ignoriert
        # ein von uns gesendetes "BEGIN IMMEDIATE" oder lehnt es als
        # "cannot start a transaction within a transaction" ab.
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _begin_immediate(connection):
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


@contextmanager
def schreibgesperrte_session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """Liefert für die Dauer des `with`-Blocks eine Session, die unter
    Datei-SQLite ihren Schreib-Lock (`BEGIN IMMEDIATE`) bereits bei ihrem
    ERSTEN Statement nimmt - für jede andere Datenbank unverändert eine
    gewöhnliche Session aus `session_factory`. Committet/rollt NICHT
    selbst zurück (das bleibt Aufgabe des Aufrufers, wie bei jeder
    anderen Session in diesem Modul) - schließt die Session (und eine
    ggf. dafür aufgebaute eigene Engine) beim Verlassen des Blocks."""

    sonde = session_factory()
    engine = sonde.get_bind()
    sonde.close()

    if not _ist_datei_sqlite(engine):
        session = session_factory()
        try:
            yield session
        finally:
            session.close()
        return

    immediate_engine = _neue_begin_immediate_engine(engine.url)
    session = Session(bind=immediate_engine, future=True, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        immediate_engine.dispose()
