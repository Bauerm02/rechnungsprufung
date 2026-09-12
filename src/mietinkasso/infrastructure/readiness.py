"""Reine, von FastAPI/`api/app.py` unabhängige Readiness-Prüfung (Auftrag
12.09., Paket A: "Health/Readiness prueft DB lesend").

Bewusst NICHT in `api/app.py` selbst definiert: dieses Modul importiert
KEIN FastAPI und löst beim Import KEINE Seiteneffekte aus (kein
`get_settings()`, kein `build_session_factory()`, kein
`pruefe_produktionskonfiguration()`) - `api/app.py` führt genau diese
Seiteneffekte beim ERSTEN Import einmalig aus, und mehrere Testmodule,
die unabhängig voneinander etwas aus `api.app` importieren, würden sich
sonst gegenseitig die Konfiguration/den Session-Factory "unterschieben"
(Python führt ein bereits importiertes Modul kein zweites Mal aus)."""

from __future__ import annotations

from sqlalchemy import select


def datenbank_lesend_erreichbar(session_factory) -> bool:
    """`SELECT 1` über die übergebene `session_factory` - `True` nur bei
    tatsächlichem Erfolg, `False` bei JEDER Störung (nie eine Exception
    durchreichen, damit `/ready` daraus sauber 503 statt 500 macht)."""

    try:
        with session_factory() as session:
            session.execute(select(1))
        return True
    except Exception:  # noqa: BLE001 - jede DB-Störung bedeutet "nicht bereit", nie ein 500
        return False
