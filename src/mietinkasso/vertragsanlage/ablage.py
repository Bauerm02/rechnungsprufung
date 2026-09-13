"""Private Original-Ablage für hochgeladene Vertrags-PDFs (Auftrag
HV-20260913-VERTRAGSANLAGE). AUSSERHALB des Repos, niemals in Git oder als
DB-BLOB - siehe `infrastructure/config.py::vertragsanlage_upload_verzeichnis`.
Der vom Client behauptete Original-Dateiname wird NIE als Pfadbestandteil
verwendet (Path-Traversal/Sonderzeichen sind damit strukturell
ausgeschlossen, nicht nur gefiltert) - der einzige Dateiname ist der aus
dem tatsächlichen Byteinhalt abgeleitete SHA256-Hex-Digest."""

from __future__ import annotations

import os
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path


class UploadAbgelehntError(Exception):
    """Datei wurde aus Sicherheits-/Größengründen NICHT gespeichert -
    keine Original-PDF-Bytes wurden dabei irgendwo abgelegt."""


@dataclass(frozen=True)
class GespeichertesDokument:
    ablage_id: str  # SHA256-Hex - einziger Dateiname UND einzige zulässige Referenz von außen
    groesse_bytes: int


def _verzeichnis(konfiguriertes_verzeichnis: str | None) -> Path:
    if not konfiguriertes_verzeichnis:
        raise UploadAbgelehntError(
            "Kein privates Ablageverzeichnis konfiguriert "
            "(MIETINKASSO_VERTRAGSANLAGE_UPLOAD_VERZEICHNIS) - Upload bleibt blockiert, "
            "es wurde nichts gespeichert."
        )
    pfad = Path(konfiguriertes_verzeichnis)
    pfad.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(pfad, 0o700)
    except OSError:
        pass  # z. B. Windows-Testumgebung ohne POSIX-Berechtigungen
    return pfad


def _ablage_pfad(verzeichnis: Path, ablage_id: str) -> Path:
    return verzeichnis / f"{ablage_id}.pdf"


def speichern(rohbytes: bytes, *, konfiguriertes_verzeichnis: str | None, max_bytes: int) -> GespeichertesDokument:
    """Validiert Größe/Typ VOR dem Schreiben und speichert dann unter
    einem aus dem Inhalt abgeleiteten Namen. Ein zweiter Upload derselben
    Bytes überschreibt nur sich selbst (identischer Hash) - kein
    Duplikat, keine wachsende Ablage bei wiederholtem identischem
    Upload."""

    if not rohbytes:
        raise UploadAbgelehntError("Datei ist leer.")
    if len(rohbytes) > max_bytes:
        raise UploadAbgelehntError(
            f"Datei überschreitet die zulässige Größe ({max_bytes // (1024 * 1024)} MB) - nichts wurde gespeichert."
        )
    if not rohbytes.startswith(b"%PDF-"):
        raise UploadAbgelehntError("Datei ist kein PDF (fehlende %PDF-Signatur) - nichts wurde gespeichert.")

    ablage_id = sha256(rohbytes).hexdigest()
    verzeichnis = _verzeichnis(konfiguriertes_verzeichnis)
    ziel = _ablage_pfad(verzeichnis, ablage_id)
    if not ziel.exists():
        temp = verzeichnis / f".{ablage_id}.tmp"
        temp.write_bytes(rohbytes)
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        temp.replace(ziel)
    return GespeichertesDokument(ablage_id=ablage_id, groesse_bytes=len(rohbytes))


def lesen(ablage_id: str, *, konfiguriertes_verzeichnis: str | None) -> bytes:
    """Liest ein zuvor gespeichertes Dokument NUR über seine bereits
    validierte `ablage_id` - niemals über einen vom Client sonst wie
    übergebenen Pfad. `ablage_id` wird hier ERNEUT strikt als 64-stelliges
    Hex validiert (defense in depth, auch wenn der Aufrufer sie schon
    geprüft haben sollte), damit sie unter keinen Umständen als
    Pfadbestandteil außerhalb des Ablageverzeichnisses interpretiert
    werden kann."""

    if len(ablage_id) != 64 or any(c not in "0123456789abcdef" for c in ablage_id):
        raise UploadAbgelehntError("Ungültige Ablage-Referenz.")
    verzeichnis = _verzeichnis(konfiguriertes_verzeichnis)
    ziel = _ablage_pfad(verzeichnis, ablage_id)
    if not ziel.exists():
        raise UploadAbgelehntError("Dokument nicht (mehr) vorhanden - bitte erneut hochladen.")
    return ziel.read_bytes()
