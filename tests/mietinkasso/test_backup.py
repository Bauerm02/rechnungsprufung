"""Tests für die SQLite-Sicherung (Auftrag 12.09., Paket A:
`src/mietinkasso/infrastructure/backup.py`). Ausschließlich synthetische,
in `tmp_path` erzeugte Test-Datenbanken - es wird nie eine echte
Datenbank gelöscht/ersetzt."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mietinkasso.infrastructure.backup import (
    BackupFehlerError,
    _pruefe_integritaet,
    _wiederherstellungsprobe,
    sichern,
    sqlite_pfad_aus_url,
)
from mietinkasso.infrastructure.db.base import Base
from mietinkasso.infrastructure.db.session import build_engine


def _synthetische_quelle(pfad: Path) -> None:
    """Legt eine echte, minimal befüllte SQLite-Datei an - schema-
    identisch zur echten Anwendung, aber rein synthetischer Inhalt."""

    engine = build_engine(f"sqlite:///{pfad}")
    Base.metadata.create_all(engine)
    from mietinkasso.stammdaten.repository import StammdatenRepository
    from sqlalchemy.orm import Session, sessionmaker

    session_factory = sessionmaker(bind=engine, future=True, expire_on_commit=False, class_=Session)
    StammdatenRepository(session_factory).upsert_gesellschaft(id="TEST", name="Test GmbH")
    engine.dispose()


def test_sqlite_pfad_aus_url_lehnt_memory_und_nicht_sqlite_ab():
    with pytest.raises(BackupFehlerError, match="In-Memory"):
        sqlite_pfad_aus_url("sqlite:///:memory:")
    with pytest.raises(BackupFehlerError, match="nur Datei-SQLite"):
        sqlite_pfad_aus_url("postgresql://user:pass@localhost/produktiv")


def test_sichern_erzeugt_gueltiges_backup(tmp_path):
    quelle = tmp_path / "quelle.db"
    _synthetische_quelle(quelle)
    backup_dir = tmp_path / "backups"

    ergebnis = sichern(database_url=f"sqlite:///{quelle}", backup_verzeichnis=backup_dir, aufbewahrung_tage=30)

    assert ergebnis.backup_pfad.exists()
    assert ergebnis.backup_pfad.parent == backup_dir
    assert ergebnis.groesse_bytes > 0
    assert ergebnis.geloeschte_alte_backups == ()
    # Das Backup ist eine eigenständige, vollständige Kopie mit den echten Daten.
    verbindung = sqlite3.connect(ergebnis.backup_pfad)
    zeilen = verbindung.execute("SELECT id, name FROM gesellschaften").fetchall()
    verbindung.close()
    assert zeilen == [("TEST", "Test GmbH")]


def test_sichern_veraendert_die_quelle_nie(tmp_path):
    quelle = tmp_path / "quelle.db"
    _synthetische_quelle(quelle)
    inhalt_vorher = quelle.read_bytes()
    mtime_vorher = quelle.stat().st_mtime_ns

    sichern(database_url=f"sqlite:///{quelle}", backup_verzeichnis=tmp_path / "backups", aufbewahrung_tage=30)

    assert quelle.read_bytes() == inhalt_vorher
    assert quelle.stat().st_mtime_ns == mtime_vorher


def test_sichern_lehnt_fehlende_quelle_ab_ohne_seiteneffekte(tmp_path):
    quelle = tmp_path / "existiert-nicht.db"
    backup_dir = tmp_path / "backups"

    with pytest.raises(BackupFehlerError, match="existiert nicht"):
        sichern(database_url=f"sqlite:///{quelle}", backup_verzeichnis=backup_dir, aufbewahrung_tage=30)

    assert not backup_dir.exists()  # kein Verzeichnis angelegt, wenn die Quelle fehlt


def test_sichern_lehnt_ungueltige_aufbewahrung_ab(tmp_path):
    quelle = tmp_path / "quelle.db"
    _synthetische_quelle(quelle)
    with pytest.raises(BackupFehlerError, match="aufbewahrung_tage"):
        sichern(database_url=f"sqlite:///{quelle}", backup_verzeichnis=tmp_path / "backups", aufbewahrung_tage=0)


def test_aufbewahrung_loescht_nur_alte_eigene_backups(tmp_path):
    quelle = tmp_path / "quelle.db"
    _synthetische_quelle(quelle)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    jetzt = datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)
    alt_pfad = backup_dir / "mietinkasso-20260101T000000Z.db"
    alt_pfad.write_bytes(b"alt")
    fremde_datei = backup_dir / "nicht-von-uns.db"
    fremde_datei.write_bytes(b"fremd")

    ergebnis = sichern(
        database_url=f"sqlite:///{quelle}", backup_verzeichnis=backup_dir, aufbewahrung_tage=30, zeitstempel=jetzt,
    )

    assert not alt_pfad.exists()  # älter als 30 Tage -> gelöscht
    assert alt_pfad in ergebnis.geloeschte_alte_backups
    assert fremde_datei.exists()  # entspricht nicht dem eigenen Namensschema -> NIE angefasst
    assert ergebnis.backup_pfad.exists()


def test_aufbewahrung_behaelt_juengere_backups(tmp_path):
    quelle = tmp_path / "quelle.db"
    _synthetische_quelle(quelle)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    jetzt = datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)
    juenger_pfad = backup_dir / "mietinkasso-20260919T000000Z.db"
    juenger_pfad.write_bytes(b"juenger")

    sichern(database_url=f"sqlite:///{quelle}", backup_verzeichnis=backup_dir, aufbewahrung_tage=30, zeitstempel=jetzt)

    assert juenger_pfad.exists()


def test_wiederholte_sicherung_am_selben_zeitstempel_ist_konflikt_kein_ueberschreiben(tmp_path):
    quelle = tmp_path / "quelle.db"
    _synthetische_quelle(quelle)
    backup_dir = tmp_path / "backups"
    zeitstempel = datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)

    sichern(database_url=f"sqlite:///{quelle}", backup_verzeichnis=backup_dir, aufbewahrung_tage=30, zeitstempel=zeitstempel)
    with pytest.raises(BackupFehlerError, match="existiert bereits"):
        sichern(database_url=f"sqlite:///{quelle}", backup_verzeichnis=backup_dir, aufbewahrung_tage=30, zeitstempel=zeitstempel)


def test_pruefe_integritaet_erkennt_korrumpierte_datei(tmp_path):
    kaputte_datei = tmp_path / "kaputt.db"
    kaputte_datei.write_bytes(b"das ist keine sqlite-datenbank")
    assert _pruefe_integritaet(kaputte_datei) is False


def test_pruefe_integritaet_erkennt_gueltige_datei(tmp_path):
    gueltige_datei = tmp_path / "gueltig.db"
    _synthetische_quelle(gueltige_datei)
    assert _pruefe_integritaet(gueltige_datei) is True


def test_wiederherstellungsprobe_erkennt_korrumpiertes_backup(tmp_path):
    kaputte_datei = tmp_path / "kaputt.db"
    kaputte_datei.write_bytes(b"das ist keine sqlite-datenbank")
    assert _wiederherstellungsprobe(kaputte_datei) is False


def test_wiederherstellungsprobe_ruehrt_original_nicht_an(tmp_path):
    original = tmp_path / "original.db"
    _synthetische_quelle(original)
    inhalt_vorher = original.read_bytes()

    assert _wiederherstellungsprobe(original) is True
    assert original.read_bytes() == inhalt_vorher  # Probe lief gegen eine Kopie, nicht gegen das Original
