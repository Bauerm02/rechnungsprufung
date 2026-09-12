#!/usr/bin/env python3
"""CLI für den generischen Echtbetrieb-Intake (Auftrag
HV-20260912-ECHTBETRIEB). Siehe `docs/hausverwaltung/IMPORT_VERTRAG.md`
für den vollständigen Vertrag (Feldschema, Beispiel).

Zwei Unterbefehle:

    python scripts/intake_import.py plan \\
        --datei echtdaten.json --database-url sqlite:////pfad/ausserhalb/repo/produktiv.db

    python scripts/intake_import.py apply \\
        --datei echtdaten.json --database-url sqlite:////pfad/ausserhalb/repo/produktiv.db \\
        --bestaetige-hash <paket_hash aus dem plan-Lauf> --akteur "codex-intake"

`--verzeichnis` statt `--datei` liest ein CSV-Bündel (Dateien
`gesellschaften.csv`, `objekte.csv`, ... im angegebenen Verzeichnis,
siehe IMPORT_VERTRAG.md).

KEIN Datenbankimport ohne `--database-url`, KEIN Fallback auf die
Repo-eigene Demo-DB, KEINE `:memory:`-Datenbank, KEIN SQLite-Pfad
innerhalb dieses Repositories - dieses Skript ist für eine private,
ausdrücklich angegebene Produktionsdatenbank AUSSERHALB des Repos
gedacht. Es startet keinen Server, ruft keine Bank-/Mailfunktion auf und
importiert nie synthetische Demodaten (siehe `seed_synthetic_data.py`,
ein komplett getrennter Weg)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mietinkasso.domain.exceptions import MietinkassoError  # noqa: E402
from mietinkasso.infrastructure.db.session import build_session_factory  # noqa: E402
from mietinkasso.intake.apply import wende_an  # noqa: E402
from mietinkasso.intake.parser import CSV_BUENDEL_DATEINAMEN, IntakeFormatFehlerError, parse_csv_buendel, parse_json_paket  # noqa: E402
from mietinkasso.intake.planner import IntakePlan, erstelle_plan  # noqa: E402
from mietinkasso.op.repository import OPRepository  # noqa: E402
from mietinkasso.op.service import OPService  # noqa: E402
from mietinkasso.stammdaten.repository import StammdatenRepository  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _pruefe_database_url(database_url: str) -> None:
    if not database_url.startswith("sqlite:///"):
        return  # nicht-SQLite (z. B. PostgreSQL) liegt per Definition außerhalb des Repos
    pfad_text = database_url.removeprefix("sqlite:///")
    if pfad_text in ("", ":memory:") or database_url.endswith(":memory:"):
        raise ValueError(
            "--database-url darf keine In-Memory-Datenbank sein (kein persistenter Produktivstand). "
            "Eine ausdrücklich angegebene Datei- oder Server-DB ist Pflicht."
        )
    ziel = Path(pfad_text).resolve()
    if ziel == _REPO_ROOT or _REPO_ROOT in ziel.parents:
        raise ValueError(
            f"--database-url zeigt auf einen Pfad INNERHALB dieses Repositories ({ziel}). "
            "Verlangt ist eine ausdrücklich außerhalb des Repos liegende, private Datenbank - "
            "keine Vermischung mit dem synthetischen Demo-Seed."
        )


def _lade_paket(args: argparse.Namespace):
    if args.datei and args.verzeichnis:
        raise ValueError("Nur EINES von --datei/--verzeichnis angeben, nicht beides.")
    if args.datei:
        text = Path(args.datei).read_text(encoding="utf-8-sig")
        return parse_json_paket(text)
    if args.verzeichnis:
        basis = Path(args.verzeichnis)
        dateien = {}
        for name in CSV_BUENDEL_DATEINAMEN:
            pfad = basis / f"{name}.csv"
            if pfad.exists():
                dateien[name] = pfad.read_text(encoding="utf-8-sig")
        return parse_csv_buendel(quelle=str(basis), dateien=dateien)
    raise ValueError("Entweder --datei (JSON) oder --verzeichnis (CSV-Bündel) angeben.")


def _drucke_plan(plan: IntakePlan) -> None:
    print(f"Paket-Hash: {plan.paket_hash}")
    print(f"Zeilen gesamt: {len(plan.befunde)}  Neu: {len(plan.neu)}  Unverändert: {len(plan.unveraendert)}  "
          f"Konflikte: {len(plan.konflikte)}  Gesperrt: {len(plan.gesperrt)}")
    for hinweis in plan.hinweise:
        print(f"  HINWEIS: {hinweis}")
    if plan.konflikte:
        print("\nKonflikte:")
        for b in plan.konflikte:
            print(f"  {b.entitaet} {b.id}: {b.grund}")
    if plan.gesperrt:
        print("\nGesperrt:")
        for b in plan.gesperrt:
            print(f"  {b.entitaet} {b.id}: {b.grund}")
    print("\nNeu/Unverändert:")
    for b in (*plan.neu, *plan.unveraendert):
        print(f"  {b.entitaet} {b.id}: {b.status}")
    print(f"\nGesamtstatus: {'ANWENDBAR' if plan.anwendbar else 'NICHT ANWENDBAR'}")


def _baue_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    unterbefehle = parser.add_subparsers(dest="befehl", required=True)

    def _quelle_argumente(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--datei", type=Path, default=None, help="Pfad zur JSON-Paketdatei.")
        sub.add_argument("--verzeichnis", type=Path, default=None, help="Pfad zu einem CSV-Bündel-Verzeichnis.")
        sub.add_argument("--database-url", required=True, help="Ziel-DB, ausdrücklich außerhalb dieses Repos, kein :memory:.")

    plan_parser = unterbefehle.add_parser("plan", help="Rein lesender Dry-run: zeigt den Plan, schreibt nichts.")
    _quelle_argumente(plan_parser)

    apply_parser = unterbefehle.add_parser("apply", help="Schreibt die gesamte Datei atomar in EINER Transaktion.")
    _quelle_argumente(apply_parser)
    apply_parser.add_argument("--bestaetige-hash", required=True, dest="bestaetige_hash", help="paket_hash aus dem vorherigen plan()-Lauf.")
    apply_parser.add_argument("--akteur", required=True, help="Name/Kennung für das Audit-Log.")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _baue_parser().parse_args(argv)

    try:
        _pruefe_database_url(args.database_url)
    except ValueError as exc:
        print(f"Ungültige --database-url: {exc}", file=sys.stderr)
        return 2

    try:
        paket = _lade_paket(args)
    except (IntakeFormatFehlerError, ValueError, OSError) as exc:
        print(f"Datei nicht lesbar/ungültig: {exc}", file=sys.stderr)
        return 2

    create_all_tables_fuer(args.database_url)
    session_factory = build_session_factory(args.database_url)
    stammdaten_repo = StammdatenRepository(session_factory)
    op_service = OPService(OPRepository(session_factory), stammdaten_repo)

    if args.befehl == "plan":
        plan = erstelle_plan(paket, session_factory=session_factory)
        _drucke_plan(plan)
        return 0 if plan.anwendbar else 1

    # apply
    try:
        ergebnis = wende_an(
            paket, bestaetigter_hash=args.bestaetige_hash, stammdaten_repo=stammdaten_repo,
            op_service=op_service, session_factory=session_factory, akteur=args.akteur,
        )
    except (MietinkassoError, ValueError) as exc:
        print(f"Apply abgebrochen, NICHTS wurde eingespielt: {exc}", file=sys.stderr)
        return 1

    print(f"Erfolgreich eingespielt (Paket-Hash {ergebnis.paket_hash}):")
    print(f"  Gesellschaften: {ergebnis.anzahl_gesellschaften}  Objekte: {ergebnis.anzahl_objekte}  "
          f"Einheiten: {ergebnis.anzahl_einheiten}  Debitoren: {ergebnis.anzahl_debitoren}  "
          f"Verträge: {ergebnis.anzahl_vertraege}")
    print(f"  Eröffnungen: {ergebnis.anzahl_eroeffnungen}  Nachbuchungen: {ergebnis.anzahl_nachbuchungen}  "
          f"Eröffnungskorrekturen: {ergebnis.anzahl_eroeffnungskorrekturen}")
    print(f"  Sperren: {ergebnis.anzahl_sperren}  Komponenten: {ergebnis.anzahl_komponenten}")
    return 0


def create_all_tables_fuer(database_url: str) -> None:
    """Legt fehlende Tabellen an einer bereits geprüften externen DB an -
    keine Nutzung von `get_settings()`, damit das Skript unabhängig von
    der Prozessumgebung ausschließlich mit `--database-url` arbeitet."""

    from mietinkasso.infrastructure.db.base import Base
    from mietinkasso.infrastructure.db.session import build_engine

    engine = build_engine(database_url)
    Base.metadata.create_all(engine)


if __name__ == "__main__":
    raise SystemExit(main())
