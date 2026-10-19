"""Post-SFTP-Bulkversand für den EinfachBrief-Fensterkuvert-Mahnbrief
(Auftrag HV-20261019-VERSAND-KOSTEN-ABSCHLUSS, Teil 3) - konkreter,
minimaler Anschlusspunkt, KEINE behauptete Live-Anbindung (Codex
verifiziert diese Datei gegen den tatsächlichen Post-Server vor
Produktivbetrieb, siehe `docs/hausverwaltung/OFFENE_PUNKTE.md`).

Vorgabe Markus 19.10.2026 (offizieller Anbieterspezifikation, keine
weitere Recherche nötig): Host `transfer.post-business-solutions.at`,
Port 22, Zielpfad `/bulk_tp/`, ein PDF/A je Brief. Dateiname: ein
15-stelliger, 1-basiert positionierter Parameterblock (Position 3
Farbmodus, 4 Duplex, 5 Tempo, 7 Produkt, 9 E-Brief-Versuch, alle übrigen
Positionen fest "0") + Bindestrich + die dauerhaft reservierte,
niemals wiederverwendete Archiv-Zeilen-ID + ".pdf" - siehe
`SendeParameter`/`baue_dateiname`.

Geschlossen per Default (`konfiguration_aus_settings`): ohne
VOLLSTÄNDIGE Konfiguration (Host+Username+Passwort+gepinnter
Hostkey-Fingerprint, ausschließlich aus `Settings`/Umgebungsvariablen -
NIE ein hier hinterlegter echter Wert) findet GARANTIERT kein
Netzzugriff statt. Der gepinnte Hostkey wird VOR jeder Authentifizierung
geprüft (`PostSftpHostkeyFehlerError` bei Abweichung). Remote-exklusives
Anlegen (niemals Überschreiben) - existiert die Datei bereits, ist das
ein `PostSftpDateiKonfliktError`, NIE ein stiller Erfolg und NIE ein
automatischer Neuversuch unter neuem Namen. Jede sonstige
Verbindungsstörung ist IMMER ungewiss (`TransportFehlerUngewissError`,
kein automatischer Retry - derselbe Grundsatz wie
`indexautomatik/transport.py`). Ein erfolgreicher Upload ist
AUSSCHLIESSLICH ein ÜBERMITTELT-Nachweis gegenüber dem SFTP-Server -
KEIN Postaufgabe-/Zustellnachweis (siehe `BriefSftpErgebnis`-Docstring
und `mahnbrief_archiv.py` für die honeste Statusabbildung).

Alle definitiven (nicht-ungewissen) Fehler dieses Moduls sind bewusst
`ValueError`-Subklassen - `MahnwesenService.versende_mahnlauf` fängt
GENAU diese Klasse ab und blockiert den Mahnlauf kontrolliert, statt
den gesamten nächtlichen Versandlauf abstürzen zu lassen."""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol

from pypdf import PdfReader

from mietinkasso.domain.exceptions import TransportFehlerUngewissError

MAX_SIMPLEX_SEITEN = 14
_BULK_PFAD_DEFAULT = "/bulk_tp/"


class PostSftpKonfigurationFehltError(ValueError):
    """Transport ist NICHT eingerichtet (fehlende Credentials/Hostkey) -
    es findet GARANTIERT kein Netzzugriff statt."""


class PostSftpHostkeyFehlerError(ValueError):
    """Der tatsächliche Hostkey des Servers stimmt NICHT mit dem
    konfigurierten, gepinnten Fingerprint überein - Verbindung wird VOR
    jeder Authentifizierung abgebrochen."""


class PostSendevoraussetzungError(ValueError):
    """Die PDF-Datei erfüllt eine feste Versandvoraussetzung nicht (kein
    PDF-Header, zu viele Simplexseiten) - kontrolliert abgelehnt statt
    eine ungültige Datei hochzuladen."""


class PostSftpDateiKonfliktError(ValueError):
    """Unter der permanent reservierten Dateireferenz liegt am Server
    bereits eine Datei - niemals überschreiben (remote exklusives
    Anlegen), niemals automatisch unter neuem Namen erneut versuchen."""


@dataclass(frozen=True)
class SendeParameter:
    """1-basierte Ziffernpositionen im 15-stelligen Post-Parameterblock
    (offiziell bestätigte Vorgabe): Position 3 Farbmodus (0=Schwarz-
    Weiß), 4 Duplex (0=Simplex), 5 Tempo (0=PRIO), 7 Produkt (1=Inland
    Einschreiben Einwurf), 9 E-Brief (0=kein Versuch); alle übrigen
    Positionen fest 0. Defaults bilden den vereinbarten
    Standardversand für Mahnbriefe ab."""

    farbmodus: int = 0
    duplex: int = 0
    tempo: int = 0
    produkt: int = 1
    ebrief: int = 0

    def __post_init__(self) -> None:
        for feld, wert in (
            ("farbmodus", self.farbmodus), ("duplex", self.duplex), ("tempo", self.tempo),
            ("produkt", self.produkt), ("ebrief", self.ebrief),
        ):
            if not isinstance(wert, int) or isinstance(wert, bool) or not 0 <= wert <= 9:
                raise ValueError(f"SendeParameter.{feld} muss eine einzelne Ziffer (0-9) sein, nicht {wert!r}.")

    def parameterblock(self) -> str:
        ziffern = ["0"] * 15
        ziffern[2] = str(self.farbmodus)  # Position 3
        ziffern[3] = str(self.duplex)  # Position 4
        ziffern[4] = str(self.tempo)  # Position 5
        ziffern[6] = str(self.produkt)  # Position 7
        ziffern[8] = str(self.ebrief)  # Position 9
        return "".join(ziffern)


def baue_dateiname(*, archiv_id: int, parameter: SendeParameter | None = None) -> str:
    """Opaquer, dauerhaft eindeutiger Dateiname: 15-stelliger
    Parameterblock, Bindestrich, die DAUERHAFT reservierte, niemals
    wiederverwendete `MahnbriefArchivTable`-Zeilen-ID (12-stellig
    zero-padded) - die Eindeutigkeit kommt ausschließlich aus dieser
    autoincrement-ID, niemals aus Inhalt oder Zeitstempel."""

    if not isinstance(archiv_id, int) or isinstance(archiv_id, bool) or archiv_id <= 0:
        raise ValueError("archiv_id muss eine positive, bereits reservierte Archiv-Zeilen-ID sein.")
    if archiv_id > 10**12 - 1:
        raise ValueError(f"archiv_id {archiv_id} überschreitet die zwölfstellige Dateinamenskapazität.")
    block = (parameter or SendeParameter()).parameterblock()
    return f"{block}-{archiv_id:012d}.pdf"


def pruefe_sendevoraussetzungen(pdf_bytes: bytes, *, duplex: int) -> None:
    if pdf_bytes[:5] != b"%PDF-":
        raise PostSendevoraussetzungError("Keine PDF-Datei - Versand abgelehnt.")
    anzahl_seiten = len(PdfReader(io.BytesIO(pdf_bytes)).pages)
    if duplex == 0 and anzahl_seiten > MAX_SIMPLEX_SEITEN:
        raise PostSendevoraussetzungError(
            f"{anzahl_seiten} Seiten überschreiten das Simplex-Limit von {MAX_SIMPLEX_SEITEN} Seiten."
        )


@dataclass(frozen=True)
class SftpKonfiguration:
    host: str
    port: int
    pfad: str
    username: str
    password: str
    host_key_fingerprint: str


def konfiguration_aus_settings(settings) -> SftpKonfiguration | None:
    """Geschlossen, solange NICHT ALLE vier sicherheitsrelevanten Werte
    (Host, Username, Passwort, gepinnter Hostkey-Fingerprint) explizit
    konfiguriert sind - Credentials kommen AUSSCHLIESSLICH aus privaten
    Serverdateien/Umgebungsvariablen außerhalb dieses Repos (Codex),
    niemals aus einem hier hinterlegten Default."""

    host = (settings.brief_sftp_host or "").strip()
    username = (settings.brief_sftp_username or "").strip()
    password = settings.brief_sftp_password or ""
    fingerprint = (settings.brief_sftp_host_key_fingerprint or "").strip()
    if not host or not username or not password or not fingerprint:
        return None
    return SftpKonfiguration(
        host=host, port=settings.brief_sftp_port, pfad=settings.brief_sftp_pfad or _BULK_PFAD_DEFAULT,
        username=username, password=password, host_key_fingerprint=fingerprint,
    )


@dataclass(frozen=True)
class BriefSftpErgebnis:
    """Dieselbe Form wie `indexautomatik.mailops_client.MailOpsErgebnis`/
    `indexautomatik.transport.VersandBestaetigung`, damit
    `MahnwesenService.versende_mahnlauf`s bestehender `nachweis_daten`/
    `versand_belegen`-Mechanismus UNVERÄNDERT greift - kein separater
    Statuscode-Vertrag je Kanal. `status` ist IMMER "GESENDET" (= an den
    Post-SFTP-Server ÜBERMITTELT); jede andere Möglichkeit ist eine
    Exception, NIE ein erfundener Zustellungs-/Postaufgabenachweis."""

    status: str
    externe_referenz: str  # der dauerhaft reservierte Dateiname
    provider_referenz: str
    versendet_am: datetime


class _SftpVerbindung(Protocol):
    def hostkey_fingerprint(self) -> str: ...
    def put_exclusive(self, remote_pfad: str, data: bytes) -> None: ...
    def close(self) -> None: ...


def _echte_verbindung(konfiguration: SftpKonfiguration) -> _SftpVerbindung:
    """Echte paramiko-Verbindung - wird in dieser Entwicklungsumgebung
    NIE aufgerufen (kein echter Host/Credentials vorhanden, siehe
    `konfiguration_aus_settings`); Tests injizieren ausschließlich ein
    Fake über `verbindung_factory`. Codex verifiziert diese Funktion
    gegen den tatsächlichen Post-Server vor Produktivbetrieb (siehe
    `docs/hausverwaltung/OFFENE_PUNKTE.md`)."""

    import paramiko

    class _ParamikoVerbindung:
        def __init__(self) -> None:
            self._transport = paramiko.Transport((konfiguration.host, konfiguration.port))
            self._transport.start_client(timeout=20)
            self._sftp = None

        def hostkey_fingerprint(self) -> str:
            import base64
            import hashlib

            schluessel = self._transport.get_remote_server_key()
            digest = hashlib.sha256(schluessel.asbytes()).digest()
            return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")

        def put_exclusive(self, remote_pfad: str, data: bytes) -> None:
            if self._sftp is None:
                self._transport.auth_password(konfiguration.username, konfiguration.password)
                self._sftp = paramiko.SFTPClient.from_transport(self._transport)
            with self._sftp.open(remote_pfad, "xb") as datei:
                datei.write(data)

        def close(self) -> None:
            if self._sftp is not None:
                self._sftp.close()
            self._transport.close()

    return _ParamikoVerbindung()


class PostSftpTransportadapter:
    """Minimaler, konkreter Anschlusspunkt für den EinfachBrief-SFTP-
    Bulkversand - siehe Moduldoc für die vollständige
    Sicherheits-/Ehrlichkeitsvorgabe. `verbindung_factory` ist
    injizierbar (Standard: `_echte_verbindung`), damit Tests ohne
    echten Netzwerkzugriff ein kontrolliertes Fake verwenden - derselbe
    etablierte Grundsatz wie `indexautomatik/transport.py::
    HttpTransportadapter`."""

    def __init__(
        self, konfiguration: SftpKonfiguration | None, *,
        verbindung_factory: Callable[[SftpKonfiguration], _SftpVerbindung] | None = None,
    ) -> None:
        self._konfiguration = konfiguration
        self._verbindung_factory = verbindung_factory or _echte_verbindung

    def sende(
        self, *, pdf_bytes: bytes, dateiname: str, parameter: SendeParameter | None = None,
    ) -> BriefSftpErgebnis:
        if self._konfiguration is None:
            raise PostSftpKonfigurationFehltError(
                "Kein vollständiges SFTP-Transportprofil konfiguriert - kein Netzzugriff."
            )
        parameter = parameter or SendeParameter()
        pruefe_sendevoraussetzungen(pdf_bytes, duplex=parameter.duplex)
        remote_pfad = self._konfiguration.pfad.rstrip("/") + "/" + dateiname

        verbindung = self._verbindung_factory(self._konfiguration)
        try:
            tatsaechlicher_fingerprint = verbindung.hostkey_fingerprint()
            if tatsaechlicher_fingerprint != self._konfiguration.host_key_fingerprint:
                raise PostSftpHostkeyFehlerError(
                    f"Hostkey-Fingerprint stimmt nicht überein (erwartet "
                    f"{self._konfiguration.host_key_fingerprint!r}, tatsächlich {tatsaechlicher_fingerprint!r}) - "
                    "Verbindung abgebrochen, keine Authentifizierung."
                )
            try:
                verbindung.put_exclusive(remote_pfad, pdf_bytes)
            except FileExistsError as exc:
                raise PostSftpDateiKonfliktError(
                    f"Unter der reservierten Dateireferenz {dateiname!r} liegt am Server bereits eine Datei - "
                    "kein Überschreiben, kein automatischer Neuversuch."
                ) from exc
        except (PostSftpHostkeyFehlerError, PostSftpDateiKonfliktError, PostSendevoraussetzungError):
            raise
        except Exception as exc:
            raise TransportFehlerUngewissError(
                f"SFTP-Transportfehler beim Versand von {dateiname!r} ({type(exc).__name__}) - "
                "möglicherweise dennoch übertragen, kein automatischer Retry."
            ) from exc
        finally:
            try:
                verbindung.close()
            except Exception:
                pass

        return BriefSftpErgebnis(
            status="GESENDET", externe_referenz=dateiname,
            provider_referenz=f"sftp:{self._konfiguration.host}:{remote_pfad}",
            versendet_am=datetime.now(timezone.utc),
        )
