#!/usr/bin/env python3
"""CLI für die SQLite-Sicherung des Mietinkasso-Moduls (Auftrag 12.09.,
Paket A). Siehe `docs/hausverwaltung/DEPLOYMENT_HETZNER.md` Abschnitt 7
und `src/mietinkasso/infrastructure/backup.py` für die vier Schritte
(Online-Backup, Integritätsprüfung, Wiederherstellungsprobe,
Aufbewahrung).

    python scripts/backup_sqlite.py \\
        --database-url sqlite:////pfad/ausserhalb/repo/produktiv.db \\
        --backup-verzeichnis /pfad/ausserhalb/repo/backups \\
        --aufbewahrung-tage 30

Genau EIN Backupjob (siehe Timer-/Service-Vorlagen unter
`docs/hausverwaltung/deploy/`) - kein Fallback auf die Repo-eigene
Demo-DB, kein `:memory:`, kein Pfad innerhalb dieses Repositories für
Quelle ODER Backup-Verzeichnis. Fasst die echte Produktions-DB an
KEINER Stelle schreibend an."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mietinkasso.infrastructure.backup import BackupFehlerError, sichern, sqlite_pfad_aus_url  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _pruefe_ausserhalb_repo(pfad: Path, *, bezeichnung: str) -> None:
    ziel = pfad.resolve()
    if ziel == _REPO_ROOT or _REPO_ROOT in ziel.parents:
        raise ValueError(
            f"{bezeichnung} '{ziel}' liegt INNERHALB dieses Repositories. Verlangt ist ein ausdrücklich "
            "außerhalb des Repos liegender Pfad - keine Vermischung mit dem synthetischen Demo-Seed."
        )


def _baue_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", required=True, help="Quelle, z. B. sqlite:////pfad/ausserhalb/repo/produktiv.db")
    parser.add_argument("--backup-verzeichnis", required=True, type=Path, help="Zielverzeichnis, ausdrücklich außerhalb dieses Repos.")
    parser.add_argument("--aufbewahrung-tage", type=int, default=30, help="Backups älter als dies werden gelöscht (default: 30).")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _baue_parser().parse_args(argv)

    try:
        quelle_pfad = sqlite_pfad_aus_url(args.database_url)
        _pruefe_ausserhalb_repo(quelle_pfad, bezeichnung="--database-url")
        _pruefe_ausserhalb_repo(args.backup_verzeichnis, bezeichnung="--backup-verzeichnis")
    except (BackupFehlerError, ValueError) as exc:
        print(f"Ungültige Argumente: {exc}", file=sys.stderr)
        return 2

    try:
        ergebnis = sichern(
            database_url=args.database_url,
            backup_verzeichnis=args.backup_verzeichnis,
            aufbewahrung_tage=args.aufbewahrung_tage,
        )
    except BackupFehlerError as exc:
        print(f"Backup fehlgeschlagen: {exc}", file=sys.stderr)
        return 1

    print(f"Backup erfolgreich: {ergebnis.backup_pfad} ({ergebnis.groesse_bytes} Bytes)")
    print("Integritätsprüfung: OK  |  Wiederherstellungsprobe (getrennte temp-DB): OK")
    if ergebnis.geloeschte_alte_backups:
        print(f"Aufbewahrung: {len(ergebnis.geloeschte_alte_backups)} alte Backup(s) gelöscht.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
