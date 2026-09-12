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


def test_cli_apply_mit_falschem_hash_erzeugt_keine_datei_kein_schema(tmp_path):
    """Codex-Rückprüfung: ein falscher Hash darf NICHT einmal ein Schema
    anlegen - vorher wird gar keine Verbindung zur Ziel-DB aufgebaut."""

    datei = tmp_path / "paket.json"
    datei.write_text(json.dumps(_beispielpaket()), encoding="utf-8")
    db_pfad = tmp_path / "produktiv.db"

    exit_code = _MODUL.main([
        "apply", "--datei", str(datei), "--database-url", f"sqlite:///{db_pfad}",
        "--bestaetige-hash", "falscher-hash", "--akteur", "cli-test",
    ])
    assert exit_code == 1
    assert not db_pfad.exists()  # kein neues Dateisystemobjekt, keine Schemaänderung


def test_cli_apply_mit_ungueltigem_paket_erzeugt_keine_datei_kein_schema(tmp_path):
    """Ein strukturell nicht anwendbares Paket (hier: Objekt 107) darf
    ebenfalls nicht einmal ein Schema an der Ziel-DB anlegen - die
    Vorprüfung läuft rein lesend/synthetisch, bevor `create_all_tables_fuer`
    überhaupt aufgerufen wird."""

    paket = _beispielpaket()
    paket["objekte"] = [{"id": "107", "gesellschaft_id": "JLB", "bezeichnung": "Sieben Dörfer"}]
    datei = tmp_path / "paket.json"
    datei.write_text(json.dumps(paket), encoding="utf-8")
    db_pfad = tmp_path / "produktiv.db"

    plan_exit = _MODUL.main(["plan", "--datei", str(datei), "--database-url", f"sqlite:///{db_pfad}"])
    assert plan_exit == 1
    assert not db_pfad.exists()

    # Korrekten Hash direkt über die geparste Datei berechnen (identisch
    # zu dem, was `main()` intern tut), um apply() mit einem PASSENDEN
    # Hash aufzurufen und so gezielt den "ungültiges Paket trotz
    # richtigem Hash"-Fall zu prüfen.
    from mietinkasso.intake.parser import parse_json_paket

    aktueller_hash = _MODUL.paket_hash(parse_json_paket(datei.read_text(encoding="utf-8")))

    apply_exit = _MODUL.main([
        "apply", "--datei", str(datei), "--database-url", f"sqlite:///{db_pfad}",
        "--bestaetige-hash", aktueller_hash, "--akteur", "cli-test",
    ])
    assert apply_exit == 1
    assert not db_pfad.exists()  # weiterhin kein Schema angelegt


def test_cli_plan_gegen_nicht_vorhandene_db_legt_keine_datei_an(tmp_path, capsys):
    """`plan` gegen eine noch nicht existierende Ziel-DB plant strukturell
    gegen eine synthetische In-Memory-Leerdatenbank - alle Zeilen NEU,
    aber es entsteht KEIN neues Dateisystemobjekt."""

    datei = tmp_path / "paket.json"
    datei.write_text(json.dumps(_beispielpaket()), encoding="utf-8")
    db_pfad = tmp_path / "noch-nicht-vorhanden.db"
    assert not db_pfad.exists()

    exit_code = _MODUL.main(["plan", "--datei", str(datei), "--database-url", f"sqlite:///{db_pfad}"])
    ausgabe = capsys.readouterr().out
    assert exit_code == 0
    assert "Gesamtstatus: ANWENDBAR" in ausgabe
    assert not db_pfad.exists()


def test_cli_plan_gegen_bestehende_db_ist_wirklich_read_only(tmp_path, capsys):
    """Nach einem erfolgreichen `apply` darf ein erneuter `plan`-Lauf
    gegen dieselbe (jetzt existierende) Datei weder Inhalt noch Schema
    verändern - echte Read-Only-Verbindung (SQLite `mode=ro`), nicht nur
    "es wird schon niemand committen"."""

    datei = tmp_path / "paket.json"
    datei.write_text(json.dumps(_beispielpaket()), encoding="utf-8")
    db_pfad = tmp_path / "produktiv.db"

    plan1 = _MODUL.main(["plan", "--datei", str(datei), "--database-url", f"sqlite:///{db_pfad}"])
    assert plan1 == 0
    ausgabe = capsys.readouterr().out
    paket_hash_wert = next(z for z in ausgabe.splitlines() if z.startswith("Paket-Hash:")).split(": ", 1)[1]
    apply_exit = _MODUL.main([
        "apply", "--datei", str(datei), "--database-url", f"sqlite:///{db_pfad}",
        "--bestaetige-hash", paket_hash_wert, "--akteur", "cli-test",
    ])
    assert apply_exit == 0
    capsys.readouterr()

    vor_groesse = db_pfad.stat().st_size
    vor_mtime = db_pfad.stat().st_mtime_ns

    plan2 = _MODUL.main(["plan", "--datei", str(datei), "--database-url", f"sqlite:///{db_pfad}"])
    ausgabe2 = capsys.readouterr().out
    assert plan2 == 0
    assert "Neu: 0" in ausgabe2  # alles bereits identisch vorhanden -> UNVERAENDERT

    assert db_pfad.stat().st_size == vor_groesse
    assert db_pfad.stat().st_mtime_ns == vor_mtime  # Datei wurde nicht angefasst

    # Die Read-Only-Verbindung lehnt einen Schreibversuch auch direkt ab.
    with pytest.raises(Exception):
        session_factory = _MODUL._lesende_session_factory(f"sqlite:///{db_pfad}")
        with session_factory() as session:
            from mietinkasso.infrastructure.db.tables import GesellschaftTable

            session.add(GesellschaftTable(id="SOLLTE-SCHEITERN", name="x"))
            session.commit()


def test_cli_lehnt_repo_internen_datenbankpfad_mit_exitcode_2_ab(tmp_path, capsys):
    datei = tmp_path / "paket.json"
    datei.write_text(json.dumps(_beispielpaket()), encoding="utf-8")
    innerhalb_repo = _MODUL._REPO_ROOT / "data" / "sollte-nicht-entstehen.db"

    exit_code = _MODUL.main(["plan", "--datei", str(datei), "--database-url", f"sqlite:///{innerhalb_repo}"])
    fehler = capsys.readouterr().err
    assert exit_code == 2
    assert "INNERHALB dieses Repositories" in fehler
    assert not innerhalb_repo.exists()
