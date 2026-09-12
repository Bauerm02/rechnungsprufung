"""Tests für `scripts/intake_import.py` - v. a. die
Datenbank-Pfadprüfung (kein Import gegen Repo-interne/In-Memory-DB) und
Exit-Codes. Rein synthetische Testdaten, kein Serverstart."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "intake_import.py"
_SPEC = importlib.util.spec_from_file_location("intake_import_cli", _SCRIPT_PATH)
_MODUL = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MODUL)


def _beispielpaket() -> dict:
    return {
        "quelle": "cli-test",
        "gesellschaften": [{"id": "JLB", "name": "JLB Projects GmbH"}],
        "objekte": [{"id": "601", "gesellschaft_id": "JLB", "bezeichnung": "Am Corso"}],
        "einheiten": [{"id": "601-T1", "objekt_id": "601", "bezeichnung": "Top 1", "nutzungsstatus": "DAUERVERMIETUNG"}],
        "debitoren": [{"id": "DEB-1", "name": "Erika Musterfrau", "email": "erika@example.at"}],
        "vertraege": [
            {"id": "V-1", "einheit_id": "601-T1", "debitor_id": "DEB-1", "gesellschaft_id": "JLB",
             "rechtsordnung": "OESTERREICH_MRG_VOLL", "gueltig_von": "2020-01-01"}
        ],
    }


def test_pruefe_database_url_lehnt_repo_internen_pfad_ab():
    innerhalb_repo = _MODUL._REPO_ROOT / "data" / "produktiv.db"
    with pytest.raises(ValueError, match="INNERHALB dieses Repositories"):
        _MODUL._pruefe_database_url(f"sqlite:///{innerhalb_repo}")


def test_pruefe_database_url_lehnt_memory_ab():
    with pytest.raises(ValueError, match="In-Memory"):
        _MODUL._pruefe_database_url("sqlite:///:memory:")


def test_pruefe_database_url_akzeptiert_pfad_ausserhalb_repo(tmp_path):
    ziel = tmp_path / "produktiv.db"
    _MODUL._pruefe_database_url(f"sqlite:///{ziel}")  # darf nicht werfen


def test_pruefe_database_url_akzeptiert_nicht_sqlite_url():
    _MODUL._pruefe_database_url("postgresql://user:pass@localhost/produktiv")  # darf nicht werfen


def test_cli_plan_und_apply_end_to_end(tmp_path, capsys):
    datei = tmp_path / "paket.json"
    datei.write_text(json.dumps(_beispielpaket()), encoding="utf-8")
    db_pfad = tmp_path / "produktiv.db"

    exit_code = _MODUL.main(["plan", "--datei", str(datei), "--database-url", f"sqlite:///{db_pfad}"])
    ausgabe = capsys.readouterr().out
    assert exit_code == 0
    assert "Gesamtstatus: ANWENDBAR" in ausgabe
    hash_zeile = next(zeile for zeile in ausgabe.splitlines() if zeile.startswith("Paket-Hash:"))
    paket_hash = hash_zeile.split(": ", 1)[1]

    exit_code = _MODUL.main([
        "apply", "--datei", str(datei), "--database-url", f"sqlite:///{db_pfad}",
        "--bestaetige-hash", paket_hash, "--akteur", "cli-test",
    ])
    ausgabe = capsys.readouterr().out
    assert exit_code == 0
    assert "Erfolgreich eingespielt" in ausgabe


def test_cli_apply_mit_falschem_hash_schreibt_nichts(tmp_path):
    datei = tmp_path / "paket.json"
    datei.write_text(json.dumps(_beispielpaket()), encoding="utf-8")
    db_pfad = tmp_path / "produktiv.db"

    exit_code = _MODUL.main([
        "apply", "--datei", str(datei), "--database-url", f"sqlite:///{db_pfad}",
        "--bestaetige-hash", "falscher-hash", "--akteur", "cli-test",
    ])
    assert exit_code == 1
    assert not db_pfad.exists() or db_pfad.stat().st_size == 0 or _keine_gesellschaft(db_pfad)


def _keine_gesellschaft(db_pfad: Path) -> bool:
    import sqlite3

    con = sqlite3.connect(db_pfad)
    try:
        cur = con.execute("SELECT COUNT(*) FROM gesellschaften")
        return cur.fetchone()[0] == 0
    except sqlite3.OperationalError:
        return True
    finally:
        con.close()


def test_cli_lehnt_repo_internen_datenbankpfad_mit_exitcode_2_ab(tmp_path, capsys):
    datei = tmp_path / "paket.json"
    datei.write_text(json.dumps(_beispielpaket()), encoding="utf-8")
    innerhalb_repo = _MODUL._REPO_ROOT / "data" / "sollte-nicht-entstehen.db"

    exit_code = _MODUL.main(["plan", "--datei", str(datei), "--database-url", f"sqlite:///{innerhalb_repo}"])
    fehler = capsys.readouterr().err
    assert exit_code == 2
    assert "INNERHALB dieses Repositories" in fehler
    assert not innerhalb_repo.exists()
