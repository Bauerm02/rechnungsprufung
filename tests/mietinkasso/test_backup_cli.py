"""Tests für `scripts/backup_sqlite.py` (Auftrag 12.09., Paket A) -
Repo-Pfadschutz und Exit-Codes, analog zu `test_intake_cli.py`."""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "backup_sqlite.py"
_SPEC = importlib.util.spec_from_file_location("backup_sqlite_cli", _SCRIPT_PATH)
_MODUL = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MODUL)


def _synthetische_quelle(pfad: Path) -> None:
    from mietinkasso.infrastructure.db.base import Base
    from mietinkasso.infrastructure.db.session import build_engine

    engine = build_engine(f"sqlite:///{pfad}")
    Base.metadata.create_all(engine)
    engine.dispose()


def test_lehnt_datenbank_innerhalb_repo_ab(tmp_path, capsys):
    innerhalb_repo = _MODUL._REPO_ROOT / "data" / "produktiv.db"
    exit_code = _MODUL.main([
        "--database-url", f"sqlite:///{innerhalb_repo}", "--backup-verzeichnis", str(tmp_path / "backups"),
    ])
    fehler = capsys.readouterr().err
    assert exit_code == 2
    assert "INNERHALB dieses Repositories" in fehler


def test_lehnt_backup_verzeichnis_innerhalb_repo_ab(tmp_path, capsys):
    quelle = tmp_path / "quelle.db"
    _synthetische_quelle(quelle)
    innerhalb_repo = _MODUL._REPO_ROOT / "data" / "backups"
    exit_code = _MODUL.main([
        "--database-url", f"sqlite:///{quelle}", "--backup-verzeichnis", str(innerhalb_repo),
    ])
    fehler = capsys.readouterr().err
    assert exit_code == 2
    assert "INNERHALB dieses Repositories" in fehler
    assert not innerhalb_repo.exists()


def test_backup_end_to_end(tmp_path, capsys):
    quelle = tmp_path / "quelle.db"
    _synthetische_quelle(quelle)
    backup_dir = tmp_path / "backups"

    exit_code = _MODUL.main([
        "--database-url", f"sqlite:///{quelle}", "--backup-verzeichnis", str(backup_dir), "--aufbewahrung-tage", "30",
    ])
    ausgabe = capsys.readouterr().out
    assert exit_code == 0
    assert "Backup erfolgreich" in ausgabe
    assert "Integritätsprüfung: OK" in ausgabe

    backups = list(backup_dir.glob("mietinkasso-*.db"))
    assert len(backups) == 1
    verbindung = sqlite3.connect(backups[0])
    verbindung.execute("PRAGMA integrity_check").fetchall()
    verbindung.close()


def test_backup_fehlender_quelle_liefert_exitcode_1(tmp_path, capsys):
    quelle = tmp_path / "existiert-nicht.db"
    exit_code = _MODUL.main([
        "--database-url", f"sqlite:///{quelle}", "--backup-verzeichnis", str(tmp_path / "backups"),
    ])
    fehler = capsys.readouterr().err
    assert exit_code == 1
    assert "existiert nicht" in fehler
