"""Tests für das eigenständige CLI-Preview-Skript
`scripts/george_business_preview.py`.

Reiner Aufruf über die `main()`-Funktion (kein Subprozess nötig) - prüft
Exit-Codes, Ausgabe und dass keine Datei außer der übergebenen gelesen und
keine neue Datei angelegt wird."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "george_business_preview.py"
_SPEC = importlib.util.spec_from_file_location("george_business_preview_cli", _SCRIPT_PATH)
_MODUL = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MODUL)

from mietinkasso.bank.george_business_csv import ERWARTETE_SPALTEN  # noqa: E402


def _schreibe_csv(pfad: Path, zeilen: list[dict[str, str]]) -> None:
    kopf = ",".join(f'"{spalte}"' for spalte in ERWARTETE_SPALTEN)
    daten = [",".join(f'"{zeile.get(spalte, "")}"' for spalte in ERWARTETE_SPALTEN) for zeile in zeilen]
    text = "\r\n".join([kopf, *daten]) + "\r\n"
    pfad.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))


def _basiszeile(**overrides: str) -> dict[str, str]:
    basis = {spalte: "" for spalte in ERWARTETE_SPALTEN}
    basis.update(
        {
            "Eigene IBAN": "AT611000000000601001",
            "Eigener Kontoname": "Testkonto",
            "Buchungsdatum": "15.01.2026",
            "Betrag": "-45,50",
            "Währung": "EUR",
        }
    )
    basis.update(overrides)
    return basis


def test_cli_gibt_bericht_aus_und_liefert_exitcode_0(tmp_path, capsys):
    datei = tmp_path / "export.csv"
    _schreibe_csv(datei, [_basiszeile(**{"Enthaltene Überweisung ID": "REF-CLI-001"})])

    exit_code = _MODUL.main(
        [
            "--datei", str(datei),
            "--konto", "AT611000000000601001",
            "--von", "01.01.2026",
            "--bis", "31.01.2026",
        ]
    )
    ausgabe = capsys.readouterr().out

    assert exit_code == 0
    assert "Kandidaten (1)" in ausgabe
    assert "45,50" in ausgabe
    assert "HINWEIS" in ausgabe
    assert "Gesamtstatus: VOLLSTÄNDIG" in ausgabe


def test_cli_lehnt_xlsx_mit_exitcode_2_ab(tmp_path, capsys):
    datei = tmp_path / "export.xlsx"
    datei.write_bytes(b"PK\x03\x04" + b"\x00" * 30)

    exit_code = _MODUL.main(
        [
            "--datei", str(datei),
            "--konto", "AT611000000000601001",
            "--von", "01.01.2026",
            "--bis", "31.01.2026",
        ]
    )
    fehler = capsys.readouterr().err

    assert exit_code == 2
    assert "Format abgelehnt" in fehler


def test_cli_erzeugt_keine_zusaetzlichen_dateien(tmp_path):
    datei = tmp_path / "export.csv"
    _schreibe_csv(datei, [_basiszeile()])
    vor_dem_lauf = set(tmp_path.iterdir())

    _MODUL.main(
        [
            "--datei", str(datei),
            "--konto", "AT611000000000601001",
            "--von", "01.01.2026",
            "--bis", "31.01.2026",
        ]
    )

    nach_dem_lauf = set(tmp_path.iterdir())
    assert vor_dem_lauf == nach_dem_lauf


def test_cli_mit_saldenkontrolle_zeigt_abweichung_und_liefert_exitcode_ungleich_0(tmp_path, capsys):
    datei = tmp_path / "export.csv"
    _schreibe_csv(datei, [_basiszeile(Betrag="100,00")])

    exit_code = _MODUL.main(
        [
            "--datei", str(datei),
            "--konto", "AT611000000000601001",
            "--von", "01.01.2026",
            "--bis", "31.01.2026",
            "--anfangssaldo", "0,00",
            "--endsaldo", "50,00",
        ]
    )
    ausgabe = capsys.readouterr().out

    assert exit_code != 0
    assert "ABWEICHUNG" in ausgabe
    assert "Gesamtstatus: UNVOLLSTÄNDIG" in ausgabe


def test_cli_liefert_exitcode_ungleich_0_bei_pruefffaellen(tmp_path, capsys):
    datei = tmp_path / "export.csv"
    _schreibe_csv(
        datei,
        [
            _basiszeile(Betrag="-10,00", **{"Enthaltene Überweisung ID": "DUP"}),
            _basiszeile(Betrag="-20,00", **{"Enthaltene Überweisung ID": "DUP"}),
        ],
    )

    exit_code = _MODUL.main(
        [
            "--datei", str(datei),
            "--konto", "AT611000000000601001",
            "--von", "01.01.2026",
            "--bis", "31.01.2026",
        ]
    )
    ausgabe = capsys.readouterr().out

    assert exit_code != 0
    assert "Prüffälle" in ausgabe
    assert "Gesamtstatus: UNVOLLSTÄNDIG" in ausgabe


def test_cli_zeigt_saldenkontrolle_nicht_als_ok_wenn_trotz_passender_summe_pruefffaelle_vorhanden_sind(tmp_path, capsys):
    """Befund 4 (2. Runde): eine rein rechnerisch aufgehende
    Saldenkontrolle (0=0, weil beide Zeilen als Dubletten ausgeschlossen
    wurden) darf nie als schlichtes 'OK' erscheinen, solange die Datei
    Prüffälle enthält."""

    datei = tmp_path / "export.csv"
    _schreibe_csv(
        datei,
        [
            _basiszeile(Betrag="-10,00", **{"Enthaltene Überweisung ID": "DUP"}),
            _basiszeile(Betrag="-20,00", **{"Enthaltene Überweisung ID": "DUP"}),
        ],
    )

    exit_code = _MODUL.main(
        [
            "--datei", str(datei),
            "--konto", "AT611000000000601001",
            "--von", "01.01.2026",
            "--bis", "31.01.2026",
            "--anfangssaldo", "0,00",
            "--endsaldo", "0,00",
        ]
    )
    ausgabe = capsys.readouterr().out

    assert exit_code != 0
    assert "rechnerisch OK, aber Datei UNVOLLSTÄNDIG" in ausgabe
    assert "Gesamtstatus: UNVOLLSTÄNDIG" in ausgabe


def test_cli_ungueltiges_datumsargument_wird_von_argparse_abgelehnt(tmp_path):
    datei = tmp_path / "export.csv"
    _schreibe_csv(datei, [_basiszeile()])

    with pytest.raises(SystemExit):
        _MODUL.main(
            [
                "--datei", str(datei),
                "--konto", "AT611000000000601001",
                "--von", "2026-01-01",
                "--bis", "31.01.2026",
            ]
        )
