#!/usr/bin/env python3
"""CLI für den monatlichen Indexautomatik-Lauf (Auftrag 13.09.,
HV-20260913-INDEXAUTOMATIK). Prüft JEDEN aktiven Vertrag genau einmal
pro Kalendermonat (`JobRunner`/`JobLockTable`, fachschluessel = die
Periode "YYYY-MM" - ein zweiter Aufruf im selben Monat, egal ob
Parallelstart oder Neustart nach Absturz, führt den Lauf NICHT erneut
aus) und erzeugt bei rechtlich ausführbarer Erhöhung ein
Erhöhungsschreiben (Status ENTWURF/BEREIT/BLOCKIERT je nach Vollständigkeit).

    python scripts/indexautomatik_monatslauf.py \\
        --database-url sqlite:////pfad/ausserhalb/repo/produktiv.db

Sendet NIE selbst (das übernimmt `indexautomatik_taegliche_pflege.py`
unter `SEND_ENABLED`/Allowlist-Kontrolle) - dieser Lauf berechnet nur
und legt die Outbox-Zeile an."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mietinkasso.auth.service import AuthContext  # noqa: E402
from mietinkasso.domain.enums import Rolle  # noqa: E402
from mietinkasso.indexautomatik.bootstrap import bauen  # noqa: E402
from mietinkasso.indexautomatik.zeit import heute_wien  # noqa: E402
from mietinkasso.infrastructure.config import Settings  # noqa: E402
from mietinkasso.infrastructure.db.session import build_session_factory, create_all_tables  # noqa: E402
from mietinkasso.jobs.runner import JobRunner  # noqa: E402

_ADMIN_CTX = AuthContext(user_id="indexautomatik-monatslauf", rolle=Rolle.ADMIN, gesellschaft_ids=None)


def _baue_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", required=True, help="z. B. sqlite:////pfad/ausserhalb/repo/produktiv.db")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _baue_parser().parse_args(argv)
    settings = Settings(database_url=args.database_url)
    create_all_tables(settings)
    session_factory = build_session_factory(settings.database_url)
    bundle = bauen(session_factory, settings)
    runner = JobRunner(session_factory)

    from datetime import datetime

    heute = heute_wien(datetime.now(ZoneInfo("Europe/Vienna")))
    periode = f"{heute.year:04d}-{heute.month:02d}"

    def _arbeit() -> dict:
        laeufe = bundle.index_automatik_service.monatslauf_alle(
            ctx=_ADMIN_CTX, heute=heute, akteur="indexautomatik-monatslauf"
        )
        zusammenfassung: dict[str, int] = {}
        for lauf in laeufe:
            zusammenfassung[lauf.status] = zusammenfassung.get(lauf.status, 0) + 1
        return {"periode": periode, "vertraege_geprueft": len(laeufe), "nach_status": zusammenfassung}

    ergebnis = runner.einmalig_ausfuehren(job_name="indexautomatik_monatslauf", fachschluessel=periode, fn=_arbeit)
    if ergebnis is None:
        print(f"Monatslauf für Periode {periode} lief bereits (oder läuft gerade) - kein Doppellauf.")
        return 0

    print(f"Monatslauf {periode}: {ergebnis['vertraege_geprueft']} Verträge geprüft.")
    for status, anzahl in sorted(ergebnis["nach_status"].items()):
        print(f"  {status}: {anzahl}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
