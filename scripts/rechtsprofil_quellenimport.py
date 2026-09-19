#!/usr/bin/env python3
"""CLI für den generischen Rechtsprofil-/Quellenfakten-Import (Auftrag
HV-20260919-INDEX-MONATSBERICHT, Umfang C). Siehe
`docs/hausverwaltung/IMPORT_RECHTSPROFIL_QUELLENFAKTEN.md` für das
vollständige JSON-Schema und ein Beispiel.

Zwei Unterbefehle:

    python scripts/rechtsprofil_quellenimport.py plan \\
        --datei quellenfakten.json --database-url sqlite:////pfad/ausserhalb/repo/produktiv.db

    python scripts/rechtsprofil_quellenimport.py apply \\
        --datei quellenfakten.json --database-url sqlite:////pfad/ausserhalb/repo/produktiv.db \\
        --bestaetige-hash <paket_hash aus dem plan-Lauf> --akteur "codex-quellenimport"

Schreibt AUSSCHLIESSLICH `RechtsprofilTable`-Zeilen mit `status=ENTWURF`
(nie eine Freigabe) und/oder rein informative `IndexQuellenFaktenTable`-
Zeilen - nichts an Soll/Bank/OP, keine Buchung, kein Versand, kein
Deployment. KEIN Datenbankimport ohne `--database-url`, KEIN Fallback auf
die Repo-eigene Demo-DB, KEINE `:memory:`-Datenbank, KEIN SQLite-Pfad
innerhalb dieses Repositories (identische Sicherheitsgrenze wie
`intake_import.py`)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mietinkasso.auth.service import AuthContext  # noqa: E402
from mietinkasso.domain.enums import Rolle  # noqa: E402
from mietinkasso.indexautomatik.rechtsprofil import RechtsprofilService  # noqa: E402
from mietinkasso.indexautomatik.rechtsprofil_import import (  # noqa: E402
    ImportPlan,
    RechtsprofilImportFehlerError,
    erstelle_plan,
    parse_json_paket,
    wende_an,
)
from mietinkasso.indexautomatik.repository import IndexQuellenFaktenRepository, RechtsprofilRepository  # noqa: E402
from mietinkasso.index.repository import IndexRepository  # noqa: E402
from mietinkasso.infrastructure.config import Settings  # noqa: E402
from mietinkasso.infrastructure.db.session import build_session_factory, create_all_tables  # noqa: E402
from mietinkasso.stammdaten.repository import StammdatenRepository  # noqa: E402

_ADMIN_CTX = AuthContext(user_id="rechtsprofil-quellenimport", rolle=Rolle.ADMIN, gesellschaft_ids=None)
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _pruefe_database_url(database_url: str) -> None:
    if not database_url.startswith("sqlite:///"):
        return
    pfad_text = database_url.removeprefix("sqlite:///")
    if pfad_text in ("", ":memory:") or database_url.endswith(":memory:"):
        raise ValueError(
            "--database-url darf keine In-Memory-Datenbank sein (kein persistenter Produktivstand). "
            "Eine ausdrücklich außerhalb des Repos liegende Datei-/Server-DB ist Pflicht."
        )
    ziel = Path(pfad_text).resolve()
    if ziel == _REPO_ROOT or _REPO_ROOT in ziel.parents:
        raise ValueError(
            f"--database-url zeigt auf einen Pfad INNERHALB dieses Repositories ({ziel}). Verlangt ist eine "
            "ausdrücklich außerhalb liegende, private Datenbank - keine Vermischung mit dem synthetischen Demo-Seed."
        )


def _drucke_plan(plan: ImportPlan) -> None:
    print(f"Paket-Hash: {plan.paket_hash}")
    print(f"Quelle: {plan.quelle}")
    print(
        f"Rechtsprofile: {len(plan.rechtsprofil_zeilen)} Zeilen, "
        f"{sum(1 for z in plan.rechtsprofil_zeilen if z.aktion == 'ANLEGEN')} neu, "
        f"{sum(1 for z in plan.rechtsprofil_zeilen if z.aktion != 'ANLEGEN')} unverändert übersprungen."
    )
    for zeile in plan.rechtsprofil_zeilen:
        print(f"  {zeile.vertrag_id}: {zeile.aktion}")
    print(
        f"Quellenfakten: {len(plan.quellen_fakten_zeilen)} Zeilen, "
        f"{sum(1 for z in plan.quellen_fakten_zeilen if z.aktion == 'ANLEGEN')} neu, "
        f"{sum(1 for z in plan.quellen_fakten_zeilen if z.aktion != 'ANLEGEN')} unverändert übersprungen."
    )
    for zeile in plan.quellen_fakten_zeilen:
        print(f"  {zeile.vertrag_id}: {zeile.aktion}")


def _baue_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    unterbefehle = parser.add_subparsers(dest="befehl", required=True)

    def _basis_argumente(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--datei", type=Path, required=True, help="Pfad zur JSON-Paketdatei.")
        sub.add_argument("--database-url", required=True, help="Ziel-DB, ausdrücklich außerhalb dieses Repos, kein :memory:.")

    plan_parser = unterbefehle.add_parser("plan", help="Rein lesender Dry-run: zeigt den Plan, schreibt nichts.")
    _basis_argumente(plan_parser)

    apply_parser = unterbefehle.add_parser("apply", help="Legt neue ENTWURF-/Quellenfakten-Zeilen an.")
    _basis_argumente(apply_parser)
    apply_parser.add_argument("--bestaetige-hash", required=True, dest="bestaetige_hash", help="paket_hash aus dem vorherigen plan-Lauf.")
    apply_parser.add_argument("--akteur", required=True, help="Name/Kennung für erstellt_von/Audit.")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _baue_parser().parse_args(argv)

    try:
        _pruefe_database_url(args.database_url)
    except ValueError as exc:
        print(f"Ungültige --database-url: {exc}", file=sys.stderr)
        return 2

    try:
        paket = parse_json_paket(Path(args.datei).read_text(encoding="utf-8-sig"))
    except (RechtsprofilImportFehlerError, OSError) as exc:
        print(f"Datei nicht lesbar/ungültig: {exc}", file=sys.stderr)
        return 2

    settings = Settings(database_url=args.database_url)
    create_all_tables(settings)
    session_factory = build_session_factory(settings.database_url)
    stammdaten_repository = StammdatenRepository(session_factory)
    rechtsprofil_repository = RechtsprofilRepository(session_factory)
    quellen_fakten_repository = IndexQuellenFaktenRepository(session_factory)
    rechtsprofil_service = RechtsprofilService(
        rechtsprofil_repository, stammdaten_repository, IndexRepository(session_factory)
    )

    try:
        plan = erstelle_plan(
            paket, stammdaten_repository=stammdaten_repository, rechtsprofil_repository=rechtsprofil_repository,
            quellen_fakten_repository=quellen_fakten_repository,
        )
    except RechtsprofilImportFehlerError as exc:
        print(f"Plan nicht möglich: {exc}", file=sys.stderr)
        return 1

    if args.befehl == "plan":
        _drucke_plan(plan)
        return 0

    try:
        ergebnis = wende_an(
            plan, bestaetige_hash=args.bestaetige_hash, ctx=_ADMIN_CTX, akteur=args.akteur,
            rechtsprofil_service=rechtsprofil_service, rechtsprofil_repository=rechtsprofil_repository,
            quellen_fakten_repository=quellen_fakten_repository,
        )
    except RechtsprofilImportFehlerError as exc:
        print(f"Apply abgelehnt: {exc}", file=sys.stderr)
        return 1

    print("Import abgeschlossen:")
    for schluessel, wert in ergebnis.items():
        print(f"  {schluessel}: {wert}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
