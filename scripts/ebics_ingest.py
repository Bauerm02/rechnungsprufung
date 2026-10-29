#!/usr/bin/env python3
"""CLI für die kontrollierte Übernahme einer vorab abgeholten EBICS-
CAMT.053-Datei in den bestehenden SQLite-Bankimport (Auftrag
HV-20261029-EBICS-INTEGRATION). Siehe `src/mietinkasso/bank/ebics_ingest.py`
und `docs/hausverwaltung/EBICS_INGEST.md`.

    python scripts/ebics_ingest.py plan \\
        --database-url sqlite:////pfad/ausserhalb/repo/produktiv.db \\
        --bank-id BK-1 --erwartete-iban AT00... --datei auszug.xml \\
        --kontext-datei /pfad/ausserhalb/repo/operator.json

    python scripts/ebics_ingest.py apply ... --expected-plan-hash <plan_hash aus plan>

Der Operator-Kontext kommt aus einer konfigurierten JSON-Datei außerhalb
des Repos (`--kontext-datei` oder Umgebungsvariable
`MIETINKASSO_EBICS_KONTEXT`), nie aus freien Kommandozeilenwerten:

    {"user_id": "markus", "rolle": "BUCHHALTUNG", "gesellschaft_ids": ["JLB"]}

`plan` öffnet eine bestehende SQLite-Datei ausschließlich read-only
(`mode=ro`). `apply` schreibt nur bei identischem, unter der
Schreibsperre neu berechnetem Planhash. KEIN Bankabruf, KEIN Netzwerk,
KEINE OP-Buchung, KEINE Zuordnung, KEIN Schema-Anlegen, KEIN Versand.

Ausgabe: JSON auf stdout (Geld in Cent, Datum ISO). Exit-Codes:
0 = anwendbar/geschrieben und vollständig abgeglichen;
1 = anwendbar/geschrieben, aber mit zurückgehaltenen UNKLAR-Einträgen;
2 = nicht anwendbar (Konflikt) bzw. Apply verweigert (Planhash);
3 = Eingabe-, Format-, Konto- oder Berechtigungsfehler."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from mietinkasso.auth.service import AuthContext  # noqa: E402
from mietinkasso.bank.ebics_ingest import (  # noqa: E402
    DATUM_TOLERANZ_TAGE_STANDARD,
    EbicsPlanHashAbweichungError,
    EbicsPlanNichtAnwendbarError,
    apply_ebics_camt053,
    plan_ebics_camt053,
)
from mietinkasso.domain.enums import Rolle  # noqa: E402
from mietinkasso.infrastructure.db.session import build_engine, build_session_factory  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[1]
_KONTEXT_ENV = "MIETINKASSO_EBICS_KONTEXT"


class _EingabeFehler(Exception):
    pass


def _ausserhalb_repo(pfad: Path, was: str) -> Path:
    ziel = pfad.resolve()
    if ziel == _REPO_ROOT or _REPO_ROOT in ziel.parents:
        raise _EingabeFehler(f"{was} liegt INNERHALB dieses Repositories ({ziel}) - verlangt ist ein privater Pfad außerhalb.")
    return ziel


def _sqlite_datei(database_url: str) -> Path:
    if not database_url.startswith("sqlite:///"):
        raise _EingabeFehler("--database-url muss eine SQLite-Datei-URL sein (sqlite:///...).")
    pfad_text = database_url.removeprefix("sqlite:///")
    if pfad_text in ("", ":memory:") or "mode=" in pfad_text or "?" in pfad_text:
        raise _EingabeFehler("--database-url muss auf eine bestehende SQLite-Datei zeigen (kein :memory:, keine URI-Optionen).")
    pfad = _ausserhalb_repo(Path(pfad_text), "--database-url")
    if not pfad.is_file():
        raise _EingabeFehler(f"SQLite-Datei {pfad} existiert nicht - es wird kein Schema angelegt.")
    return pfad


def _lesende_session_factory(pfad: Path) -> sessionmaker[Session]:
    # Gleiche Read-only-URI-Form wie scripts/intake_import.py.
    engine = build_engine(f"sqlite:///file:{pfad}?mode=ro&uri=true")
    return sessionmaker(bind=engine, future=True, expire_on_commit=False, class_=Session)


def _lade_kontext(kontext_datei: str | None) -> AuthContext:
    quelle = kontext_datei or os.environ.get(_KONTEXT_ENV)
    if not quelle:
        raise _EingabeFehler(f"Kein Operator-Kontext konfiguriert (--kontext-datei oder {_KONTEXT_ENV}).")
    pfad = _ausserhalb_repo(Path(quelle), "Kontextdatei")
    try:
        daten = json.loads(pfad.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise _EingabeFehler(f"Kontextdatei nicht lesbar: {exc}") from exc
    if not isinstance(daten, dict):
        raise _EingabeFehler("Kontextdatei muss ein JSON-Objekt sein.")
    user_id = daten.get("user_id")
    rolle_text = daten.get("rolle")
    gesellschaften = daten.get("gesellschaft_ids")
    if not isinstance(user_id, str) or not user_id.strip():
        raise _EingabeFehler("Kontextdatei: user_id fehlt.")
    try:
        rolle = Rolle(rolle_text)
    except ValueError as exc:
        raise _EingabeFehler(f"Kontextdatei: unbekannte Rolle {rolle_text!r}.") from exc
    if (
        not isinstance(gesellschaften, list) or not gesellschaften
        or not all(isinstance(g, str) and g.strip() for g in gesellschaften)
    ):
        raise _EingabeFehler("Kontextdatei: gesellschaft_ids muss eine nicht-leere Liste ausdrücklicher IDs sein.")
    return AuthContext(user_id=user_id.strip(), rolle=rolle, gesellschaft_ids=frozenset(g.strip() for g in gesellschaften))


def _baue_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    unterbefehle = parser.add_subparsers(dest="befehl", required=True)

    def _gemeinsam(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--database-url", required=True, help="Bestehende SQLite-Datei außerhalb des Repos.")
        sub.add_argument("--bank-id", required=True, dest="bank_id", help="Explizite bank_konto_id.")
        sub.add_argument("--erwartete-iban", required=True, dest="erwartete_iban", help="IBAN laut Transport.")
        sub.add_argument("--datei", required=True, type=Path, help="CAMT.053-Datei des Transports.")
        sub.add_argument("--kontext-datei", default=None, dest="kontext_datei", help=f"Operator-Kontext (sonst {_KONTEXT_ENV}).")
        sub.add_argument("--datum-toleranz-tage", type=int, default=DATUM_TOLERANZ_TAGE_STANDARD, dest="toleranz")

    _gemeinsam(unterbefehle.add_parser("plan", help="Rein lesender Plan (SQLite mode=ro)."))
    apply_parser = unterbefehle.add_parser("apply", help="Schreibt NEU-Zeilen bei identischem Planhash.")
    _gemeinsam(apply_parser)
    apply_parser.add_argument("--expected-plan-hash", required=True, dest="plan_hash")
    return parser


def _ausgabe(daten: dict) -> None:
    print(json.dumps(daten, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    args = _baue_parser().parse_args(argv)
    try:
        db_pfad = _sqlite_datei(args.database_url)
        ctx = _lade_kontext(args.kontext_datei)
        camt_bytes = args.datei.read_bytes()
    except (_EingabeFehler, OSError) as exc:
        _ausgabe({"befehl": args.befehl, "fehler": type(exc).__name__, "meldung": str(exc)})
        return 3

    try:
        if args.befehl == "plan":
            plan = plan_ebics_camt053(
                session_factory=_lesende_session_factory(db_pfad), bank_konto_id=args.bank_id,
                erwartete_iban=args.erwartete_iban, camt_bytes=camt_bytes, ctx=ctx, datum_toleranz_tage=args.toleranz,
            )
            _ausgabe({"befehl": "plan", **plan.to_dict()})
            if not plan.anwendbar:
                return 2
            return 0 if plan.abgleich_vollstaendig else 1

        ergebnis = apply_ebics_camt053(
            session_factory=build_session_factory(f"sqlite:///{db_pfad}"), bank_konto_id=args.bank_id,
            erwartete_iban=args.erwartete_iban, camt_bytes=camt_bytes, ctx=ctx,
            erwarteter_plan_hash=args.plan_hash, datum_toleranz_tage=args.toleranz,
        )
        _ausgabe({"befehl": "apply", **ergebnis.to_dict()})
        return 0 if ergebnis.abgleich_vollstaendig else 1
    except (EbicsPlanHashAbweichungError, EbicsPlanNichtAnwendbarError) as exc:
        _ausgabe({"befehl": args.befehl, "fehler": type(exc).__name__, "meldung": str(exc), "geschrieben": 0})
        return 2
    except Exception as exc:  # noqa: BLE001 - jede Ablehnung als JSON, nichts geschrieben (atomar)
        _ausgabe({"befehl": args.befehl, "fehler": type(exc).__name__, "meldung": str(exc), "geschrieben": 0})
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
