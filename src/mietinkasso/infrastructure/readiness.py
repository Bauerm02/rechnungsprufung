"""Reine, von FastAPI/`api/app.py` unabhängige Readiness-Prüfung (Auftrag
12.09., Paket A/B: "Health/Readiness prueft DB lesend").

Bewusst NICHT in `api/app.py` selbst definiert: dieses Modul importiert
KEIN FastAPI und löst beim Import KEINE Seiteneffekte aus (kein
`get_settings()`, kein `build_session_factory()`, kein
`pruefe_produktionskonfiguration()`) - `api/app.py` führt genau diese
Seiteneffekte beim ERSTEN Import einmalig aus, und mehrere Testmodule,
die unabhängig voneinander etwas aus `api.app` importieren, würden sich
sonst gegenseitig die Konfiguration/den Session-Factory "unterschieben"
(Python führt ein bereits importiertes Modul kein zweites Mal aus).

Codex-Rückprüfung (Paket A, unabhängig geprüft): ein bloßes `SELECT 1`
braucht KEIN Schema und öffnet bei SQLite eine ganz normale
Schreibverbindung - gegen eine fehlende/gelöschte Datei würde das
STILLSCHWEIGEND eine neue, leere Datenbankdatei anlegen und
"bereit" melden, obwohl die eigentliche Produktions-DB fehlt. Deshalb
prüft diese Funktion für Datei-SQLite ZUERST (ohne jede Verbindung),
ob die Datei überhaupt existiert, und liest DANACH tatsächlich aus den
erwarteten Kerntabellen (Schema-Vorhandensein, nicht nur "der Prozess
kann irgendeine SQL-Anweisung ausführen")."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

#: Repräsentative, immer vorhandene Kerntabellen (siehe
#: infrastructure/db/tables.py) - ihre Lesbarkeit beweist, dass das
#: Schema tatsächlich angelegt wurde, nicht nur, dass "irgendeine"
#: (ggf. gerade erst automatisch angelegte, leere) SQLite-Datei existiert.
_ERWARTETE_KERNTABELLEN = ("gesellschaften", "vertraege", "konten", "op_positionen")


def _sqlite_datei_fehlt(engine) -> bool:
    """`True` nur für eine Datei-SQLite-URL, deren Datei nicht existiert -
    liest NUR die bereits geparste `engine.url` (löst KEINE
    Verbindung/kein `connect()` aus, kann also selbst keine Datei anlegen).
    Für `:memory:` oder Nicht-SQLite (z. B. PostgreSQL) immer `False` -
    dort ist die Frage "existiert die Datei" nicht sinnvoll/zuständig."""

    url = engine.url
    if url.get_backend_name() != "sqlite":
        return False
    pfad_text = url.database
    if not pfad_text or pfad_text == ":memory:":
        return False
    return not Path(pfad_text).exists()


def datenbank_lesend_erreichbar(session_factory) -> bool:
    """`True` nur, wenn (a) eine ggf. dateibasierte SQLite-Quelle bereits
    existiert (wird NIE selbst angelegt) UND (b) die erwarteten
    Kerntabellen tatsächlich lesbar sind (Schema vorhanden). `False` bei
    JEDER Störung - nie eine Exception durchreichen, damit `/ready`
    daraus sauber 503 statt 500 macht."""

    try:
        with session_factory() as session:
            engine = session.get_bind()  # löst noch KEINE Verbindung aus
            if _sqlite_datei_fehlt(engine):
                return False
            for tabelle in _ERWARTETE_KERNTABELLEN:
                session.execute(text(f"SELECT 1 FROM {tabelle} LIMIT 1"))
        return True
    except Exception:  # noqa: BLE001 - jede DB-Störung bedeutet "nicht bereit", nie ein 500
        return False
