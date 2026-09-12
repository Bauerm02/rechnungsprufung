"""SQLite-Sicherung für den Produktivbetrieb (Auftrag 12.09., Paket A).

Nutzt die SQLite Online Backup API (`sqlite3.Connection.backup`) statt
eines simplen Dateikopiervorgangs - das liefert einen konsistenten
Snapshot auch dann, wenn die Quelle gerade mitten in einer
Schreibtransaktion ist (ein `shutil.copy` könnte in diesem Moment eine
strukturell kaputte Kopie ziehen). Jeder Sicherungslauf besteht aus
VIER Schritten, die ALLE bestehen müssen, damit ein Backup als
erfolgreich gilt:

1. Online-Backup der Quelle in eine neue, zeitgestempelte Zieldatei.
2. Integritätsprüfung DES NEUEN BACKUPS (`PRAGMA integrity_check`).
3. Wiederherstellungsprobe: das Backup in eine GETRENNTE temporäre
   Kopie duplizieren, DIESE prüfen, dann löschen - beweist, dass sich
   aus der Backup-Datei tatsächlich eine eigenständige Datenbank lesen
   lässt, ohne jemals die echte Quelle oder das Backup selbst
   anzufassen.
4. Aufbewahrung durchsetzen (alte, selbst erzeugte Backups jenseits der
   konfigurierten Frist löschen - NIE eine fremde/unbekannte Datei im
   Verzeichnis).

Die echte Produktions-DB wird an KEINER Stelle beschrieben oder
gelöscht; nur lesend geöffnet (SQLite-URI `mode=ro`)."""

from __future__ import annotations

import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mietinkasso.domain.exceptions import MietinkassoError

_DATEINAME_MUSTER = re.compile(r"^mietinkasso-(?P<ts>\d{8}T\d{6}Z)\.db$")
_ZEITSTEMPEL_FORMAT = "%Y%m%dT%H%M%SZ"


class BackupFehlerError(MietinkassoError):
    """Sicherung/Integritätsprüfung/Wiederherstellungsprobe ist
    fehlgeschlagen - ein als erfolgreich gemeldetes Backup hat garantiert
    ALLE Schritte bestanden, es gibt keinen Teilerfolg."""


@dataclass(frozen=True)
class BackupErgebnis:
    backup_pfad: Path
    groesse_bytes: int
    geloeschte_alte_backups: tuple[Path, ...]


def sqlite_pfad_aus_url(database_url: str) -> Path:
    """Extrahiert den Dateipfad aus einer `sqlite:///...`-URL. Wirft für
    Nicht-SQLite-URLs (z. B. PostgreSQL) oder `:memory:` - für beide ist
    dieses Dateibasierte Verfahren nicht anwendbar (eine Server-DB hat
    ihr eigenes Backup-Verfahren, z. B. `pg_dump`)."""

    if not database_url.startswith("sqlite:///"):
        raise BackupFehlerError(
            f"Sicherung unterstützt nur Datei-SQLite (sqlite:///...), erhalten: '{database_url}'. "
            "Für PostgreSQL/andere DBs gilt ein eigenes, DB-seitiges Backup-Verfahren (z. B. pg_dump)."
        )
    pfad_text = database_url.removeprefix("sqlite:///")
    if pfad_text in ("", ":memory:") or database_url.endswith(":memory:"):
        raise BackupFehlerError("Sicherung einer In-Memory-Datenbank ist nicht möglich/sinnvoll.")
    return Path(pfad_text)


def _pruefe_integritaet(db_pfad: Path) -> bool:
    """Öffnet NUR LESEND (SQLite-URI `mode=ro`) und führt `PRAGMA
    integrity_check` aus - `True` nur beim EXAKTEN Ergebnis `[('ok',)]`.
    Jede `sqlite3.DatabaseError` (z. B. "file is not a database", eine
    korrumpierte oder nicht öffenbare Datei) zählt als NICHT bestanden,
    wird aber nie unbehandelt nach außen durchgereicht."""

    try:
        verbindung = sqlite3.connect(f"file:{db_pfad}?mode=ro", uri=True)
        try:
            zeilen = verbindung.execute("PRAGMA integrity_check").fetchall()
            return len(zeilen) == 1 and zeilen[0][0] == "ok"
        finally:
            verbindung.close()
    except sqlite3.DatabaseError:
        return False


def _wiederherstellungsprobe(backup_pfad: Path) -> bool:
    """Kopiert das Backup in eine GETRENNTE temporäre Datei (niemals die
    echte Produktions-DB oder das Original-Backup selbst) und prüft
    DIESE Kopie erneut - beweist, dass sich aus dem Backup tatsächlich
    eine eigenständige, unabhängige Datenbank wiederherstellen lässt."""

    with tempfile.TemporaryDirectory(prefix="mietinkasso-restore-probe-") as tmp_dir:
        probe_pfad = Path(tmp_dir) / "restore-probe.db"
        shutil.copy2(backup_pfad, probe_pfad)
        if not _pruefe_integritaet(probe_pfad):
            return False
        verbindung = sqlite3.connect(f"file:{probe_pfad}?mode=ro", uri=True)
        try:
            # Muss lesbar sein, ohne dass eine bestimmte Tabelle
            # existieren MUSS (ein Backup direkt nach Schema-Anlage,
            # noch ohne Daten, ist ebenfalls ein gültiges Backup).
            verbindung.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            return True
        except sqlite3.DatabaseError:
            return False
        finally:
            verbindung.close()


def _alte_backups_loeschen(backup_verzeichnis: Path, *, aufbewahrung_tage: int, jetzt: datetime) -> tuple[Path, ...]:
    """Löscht NUR Dateien, die exakt dem eigenen Namensschema
    entsprechen (`mietinkasso-<UTC-Zeitstempel>.db`) und deren im
    DATEINAMEN kodiertes Erstellungsdatum älter als `aufbewahrung_tage`
    ist - nie eine fremde/unbekannte Datei im selben Verzeichnis, und
    nie basierend auf der (durch Kopieren/Backups verfälschbaren)
    Dateisystem-mtime."""

    grenze = jetzt - timedelta(days=aufbewahrung_tage)
    geloescht = []
    for pfad in sorted(backup_verzeichnis.glob("mietinkasso-*.db")):
        treffer = _DATEINAME_MUSTER.match(pfad.name)
        if treffer is None:
            continue
        zeitpunkt = datetime.strptime(treffer.group("ts"), _ZEITSTEMPEL_FORMAT).replace(tzinfo=timezone.utc)
        if zeitpunkt < grenze:
            pfad.unlink()
            geloescht.append(pfad)
    return tuple(geloescht)


def sichern(
    *,
    database_url: str,
    backup_verzeichnis: Path,
    aufbewahrung_tage: int,
    zeitstempel: datetime | None = None,
) -> BackupErgebnis:
    """Führt EINEN vollständigen Sicherungslauf durch (siehe Modul-
    Docstring für die vier Schritte). Schlägt IRGENDEIN Schritt fehl,
    wird `BackupFehlerError` geworfen und NICHTS als Erfolg gemeldet -
    ein bereits geschriebenes, aber als fehlerhaft erkanntes Backup
    bleibt zur Fehleranalyse liegen (wird nicht automatisch gelöscht),
    zählt aber nicht als gültiges Backup."""

    if aufbewahrung_tage < 1:
        raise BackupFehlerError(f"aufbewahrung_tage muss mindestens 1 sein (erhalten: {aufbewahrung_tage}).")

    quelle_pfad = sqlite_pfad_aus_url(database_url)
    if not quelle_pfad.exists():
        raise BackupFehlerError(f"Quell-Datenbank '{quelle_pfad}' existiert nicht.")

    backup_verzeichnis.mkdir(parents=True, exist_ok=True)
    zeitstempel = zeitstempel or datetime.now(timezone.utc)
    dateiname = f"mietinkasso-{zeitstempel.strftime(_ZEITSTEMPEL_FORMAT)}.db"
    ziel_pfad = backup_verzeichnis / dateiname
    if ziel_pfad.exists():
        raise BackupFehlerError(f"Backup-Ziel '{ziel_pfad}' existiert bereits (Zeitstempelkollision).")

    quelle_verbindung = sqlite3.connect(f"file:{quelle_pfad}?mode=ro", uri=True)
    try:
        ziel_verbindung = sqlite3.connect(ziel_pfad)
        try:
            quelle_verbindung.backup(ziel_verbindung)
        finally:
            ziel_verbindung.close()
    finally:
        quelle_verbindung.close()

    if not _pruefe_integritaet(ziel_pfad):
        raise BackupFehlerError(f"Integritätsprüfung des neuen Backups '{ziel_pfad}' fehlgeschlagen.")
    if not _wiederherstellungsprobe(ziel_pfad):
        raise BackupFehlerError(f"Wiederherstellungsprobe für '{ziel_pfad}' fehlgeschlagen.")

    geloescht = _alte_backups_loeschen(backup_verzeichnis, aufbewahrung_tage=aufbewahrung_tage, jetzt=zeitstempel)

    return BackupErgebnis(
        backup_pfad=ziel_pfad,
        groesse_bytes=ziel_pfad.stat().st_size,
        geloeschte_alte_backups=geloescht,
    )
