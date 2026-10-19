"""Post-SFTP-Bulkversand für den EinfachBrief-Fensterkuvert-Mahnbrief
(Auftrag HV-20261019-VERSAND-KOSTEN-ABSCHLUSS, Teil 3) - konkreter,
minimaler Anschlusspunkt, KEINE behauptete Live-Anbindung (Codex
verifiziert diese Datei gegen den tatsächlichen Post-Server vor
Produktivbetrieb, siehe `docs/hausverwaltung/OFFENE_PUNKTE.md`).

Vorgabe Markus 19.10.2026 (offizieller Anbieterspezifikation): Host
`transfer.post-business-solutions.at`, Port 22, Zielpfad RELATIV
`bulk_tp/` (Codex-Abnahme 19.10.2026: das Anbieteroriginal verlangt
einen relativen Pfad - ein absoluter `/bulk_tp/` hätte bei einem
chroot-/Home-Verzeichnis-basierten SFTP-Konto am echten Server
fälschlich außerhalb des zulässigen Bereichs gesucht), ein PDF/A je
Brief. Dateiname: ein 15-stelliger, 1-basiert positionierter
Parameterblock (Position 3 Farbmodus, 4 Duplex, 5 Tempo, 7 Produkt, 9
E-Brief - IMMER 0, kein E-Brief-Versuch - alle übrigen Positionen fest
"0") + Bindestrich + der permanent in der DB reservierte, opake
Sende-Schlüssel (UUID, siehe `mahnbrief_archiv.py::archivieren`) +
".pdf" - siehe `SendeParameter`/`baue_dateiname`.

Geschlossen per Default (`konfiguration_aus_settings`): ohne
VOLLSTÄNDIGE Konfiguration (Host+Username+Passwort+gepinnter
Hostkey-Fingerprint, ausschließlich aus `Settings`/privaten
Serverdateien - NIE ein hier hinterlegter echter Wert) findet
GARANTIERT kein Netzzugriff statt. Das Passwort wird NIE als Klartext-
Settings-Wert gehalten, sondern aus einer privaten Datei gelesen
(`brief_sftp_password_datei`, analog `hv_mail_token_file`) - taucht
dadurch nie in einer `.env`/einem Prozess-Environment-Dump auf. Der
gepinnte Hostkey wird VOR jeder Authentifizierung geprüft
(`PostSftpHostkeyFehlerError` bei Abweichung). Remote-exklusives UND
SCHREIBENDES Anlegen (`"wxb"` - Codex-Abnahme 19.10.2026: `"xb"` allein
setzt in paramikos `SFTPClient.open`-Flag-Übersetzung kein Schreib-Flag,
die Datei würde nie tatsächlich beschrieben) - existiert die Datei
bereits, ist das ein `PostSftpDateiKonfliktError`, NIE ein stiller
Erfolg und NIE ein automatischer Neuversuch unter neuem Namen. Jede
sonstige Verbindungsstörung (einschließlich eines fehlschlagenden
Verbindungsaufbaus selbst) ist IMMER ungewiss
(`TransportFehlerUngewissError`, kein automatischer Retry - derselbe
Grundsatz wie `indexautomatik/transport.py`). Verbindungsaufbau UND
Authentifizierung laufen mit demselben begrenzten Socket-Timeout
(`_VERBINDUNGSTIMEOUT_SEKUNDEN`).

Ein erfolgreicher Upload ist AUSSCHLIESSLICH ein ÜBERMITTELT-Nachweis
gegenüber dem SFTP-Server - KEIN Postaufgabe-/Zustellnachweis.
**Kritisch (Codex-Abnahme 19.10.2026, zentraler Befund):** auch wenn
`BriefSftpErgebnis.status == "GESENDET"` (= dieselbe Form wie
`MailOpsErgebnis`/`VersandBestaetigung` trägt), darf dieses Ergebnis
NIEMALS direkt als `versand_fn`-Rückgabe an
`MahnwesenService.versende_mahnlauf` durchgereicht werden - dessen
`nachweis_daten`/`versand_belegen`-Mechanismus würde daraus SOFORT
einen abgeschlossenen Versand samt Kostenbuchung ableiten. Der
tatsächliche Dispatch-Code (`indexautomatik/mailversand_service.py::
HVMailversandService._sende_brief`) verwendet dieses Ergebnis deshalb
NUR, um den Archivstatus auf `UEBERMITTELT` zu setzen, und gibt dem
Aufrufer explizit `None` zurück - der Mahnlauf bleibt dadurch UNSICHER
(keine Kostenbuchung, kein automatischer Neuversand), bis ein
authentischer Anbieterbeleg (`mahnbrief_archiv.py::
importiere_anbieterbeleg` + Vervollständigung) vorliegt.

Alle definitiven (nicht-ungewissen) Fehler dieses Moduls sind bewusst
`ValueError`-Subklassen - `MahnwesenService.versende_mahnlauf` fängt
GENAU diese Klasse ab und blockiert den Mahnlauf kontrolliert, statt
den gesamten nächtlichen Versandlauf abstürzen zu lassen. `_sende_brief`
behandelt `PostSftpDateiKonfliktError` davon abweichend NICHT als
BLOCKIERT-fähig (siehe dortige Docstring) - ein Dateikonflikt unter
einem permanent reservierten, nie wiederverwendeten Schlüssel könnte
bedeuten, dass ein früherer, unbestätigter Versuch bereits erfolgreich
war."""

from __future__ import annotations

import base64
import hashlib
import io
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol

from pypdf import PdfReader

from mietinkasso.domain.exceptions import TransportFehlerUngewissError

MAX_SIMPLEX_SEITEN = 14
_BULK_PFAD_DEFAULT = "bulk_tp/"
_VERBINDUNGSTIMEOUT_SEKUNDEN = 20
_SENDE_SCHLUESSEL_MUSTER = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


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
    Anlegen), niemals automatisch unter neuem Namen erneut versuchen.
    **Aufrufer-Hinweis:** dies ist NICHT dasselbe wie "von vornherein
    nichts gesendet" - der Aufruf-Code darf hieraus NIE ein
    BLOCKIERT/Freigabe-zum-Neuversand ableiten (siehe Moduldoc)."""


@dataclass(frozen=True)
class SendeParameter:
    """1-basierte Ziffernpositionen im 15-stelligen Post-Parameterblock
    (offiziell bestätigte Vorgabe): Position 3 Farbmodus (0=Schwarz-
    Weiß), 4 Duplex (0=Simplex), 5 Tempo (0=PRIO), 7 Produkt (1=Inland
    Einschreiben Einwurf), 9 E-Brief; alle übrigen Positionen fest 0.
    `ebrief` ist ZWINGEND 0 (Codex-Abnahme 19.10.2026: kein E-Brief-
    Versuch, kein alternativer Zustellkanal ohne eigene Freigabe) -
    jeder andere Wert wird kontrolliert abgelehnt, nicht stillschweigend
    übernommen."""

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
        if self.ebrief != 0:
            raise ValueError("SendeParameter.ebrief ist zwingend 0 - kein E-Brief-Versuch ohne eigene Freigabe.")

    def parameterblock(self) -> str:
        ziffern = ["0"] * 15
        ziffern[2] = str(self.farbmodus)  # Position 3
        ziffern[3] = str(self.duplex)  # Position 4
        ziffern[4] = str(self.tempo)  # Position 5
        ziffern[6] = str(self.produkt)  # Position 7
        ziffern[8] = str(self.ebrief)  # Position 9
        return "".join(ziffern)


def baue_dateiname(*, sende_schluessel: str, parameter: SendeParameter | None = None) -> str:
    """Opaquer, dauerhaft eindeutiger Dateiname: 15-stelliger
    Parameterblock, Bindestrich, der PERMANENT in der DB reservierte,
    niemals wiederverwendete `sende_schluessel` (siehe
    `mahnbrief_archiv.py::archivieren` - eine unveränderte Wiederholung
    behält denselben Schlüssel und damit denselben Dateinamen), `.pdf`.
    Validiert ein sicheres, dateinamenstaugliches Format (kein Pfad-
    Trennzeichen, keine Steuerzeichen) statt jeden beliebigen String
    ungeprüft in einen Remote-Pfad einzusetzen."""

    if not isinstance(sende_schluessel, str) or not _SENDE_SCHLUESSEL_MUSTER.fullmatch(sende_schluessel):
        raise ValueError(
            f"sende_schluessel {sende_schluessel!r} ist kein gültiger, bereits reservierter Sende-Schlüssel."
        )
    block = (parameter or SendeParameter()).parameterblock()
    return f"{block}-{sende_schluessel}.pdf"


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
    (Host, Username, Passwort-DATEI, gepinnter Hostkey-Fingerprint)
    explizit konfiguriert sind UND die Passwortdatei tatsächlich lesbar
    ist - Credentials kommen AUSSCHLIESSLICH aus privaten
    Serverdateien/Umgebungsvariablen außerhalb dieses Repos (Codex),
    niemals aus einem hier hinterlegten Default. Das Passwort wird NIE
    als Settings-Klartextwert gehalten (siehe Moduldoc)."""

    host = (settings.brief_sftp_host or "").strip()
    username = (settings.brief_sftp_username or "").strip()
    fingerprint = (settings.brief_sftp_host_key_fingerprint or "").strip()
    passwort_datei = (settings.brief_sftp_password_datei or "").strip()
    if not host or not username or not fingerprint or not passwort_datei:
        return None
    try:
        from pathlib import Path

        password = Path(passwort_datei).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not password:
        return None
    return SftpKonfiguration(
        host=host, port=settings.brief_sftp_port, pfad=settings.brief_sftp_pfad or _BULK_PFAD_DEFAULT,
        username=username, password=password, host_key_fingerprint=fingerprint,
    )


def _remote_pfad(pfad: str, dateiname: str) -> str:
    """Baut den Remote-Pfad RELATIV zum SFTP-Login-Verzeichnis (kein
    führender `/`) - `pfad` selbst darf mit oder ohne Schrägstriche
    konfiguriert sein, das Ergebnis trägt nie einen doppelten oder
    führenden Schrägstrich."""

    bereinigt = pfad.strip("/")
    return f"{bereinigt}/{dateiname}" if bereinigt else dateiname


@dataclass(frozen=True)
class BriefSftpErgebnis:
    """Dieselbe Form wie `indexautomatik.mailops_client.MailOpsErgebnis`/
    `indexautomatik.transport.VersandBestaetigung` - rein strukturell.
    `status` ist IMMER "GESENDET" (= an den Post-SFTP-Server
    ÜBERMITTELT, NIE ein Zustellungs-/Postaufgabenachweis) - **dieses
    Objekt darf TROTZDEM niemals direkt als `versand_fn`-Rückgabe an
    `MahnwesenService.versende_mahnlauf` weitergereicht werden** (siehe
    Moduldoc, zentraler Codex-Befund 19.10.2026)."""

    status: str
    externe_referenz: str  # der permanent reservierte Dateiname
    provider_referenz: str
    versendet_am: datetime


class _SftpVerbindung(Protocol):
    def hostkey_fingerprint(self) -> str: ...
    def put_exclusive(self, remote_pfad: str, data: bytes) -> None: ...
    def close(self) -> None: ...


class _ParamikoVerbindung:
    """Echte paramiko-SFTP-Verbindung - wird in dieser Entwicklungs-
    umgebung NIE mit ihrem Standard-Verbindungsaufbau aufgerufen (kein
    echter Host/Credentials vorhanden, siehe `konfiguration_aus_
    settings`). `transport_factory`/`sftp_client_cls` sind injizierbar,
    damit ein gezielter Test die TATSÄCHLICHEN Open-Flags/den
    TATSÄCHLICHEN Verbindungsaufbau dieser Klasse mit einem Fake
    `Transport`/`SFTPClient` prüft, ohne echtes Netz (siehe
    `test_brief_sftp.py`). Codex verifiziert den Standardpfad gegen den
    echten Post-Server vor Produktivbetrieb."""

    def __init__(
        self, konfiguration: SftpKonfiguration, *,
        transport_factory: Callable[[], object] | None = None, sftp_client_cls: type | None = None,
    ) -> None:
        import paramiko

        self._konfiguration = konfiguration
        self._sftp_client_cls = sftp_client_cls or paramiko.SFTPClient
        if transport_factory is not None:
            self._transport = transport_factory()
        else:
            sock = socket.create_connection(
                (konfiguration.host, konfiguration.port), timeout=_VERBINDUNGSTIMEOUT_SEKUNDEN
            )
            self._transport = paramiko.Transport(sock)
            self._transport.banner_timeout = _VERBINDUNGSTIMEOUT_SEKUNDEN
            self._transport.start_client(timeout=_VERBINDUNGSTIMEOUT_SEKUNDEN)
        self._sftp = None

    def hostkey_fingerprint(self) -> str:
        schluessel = self._transport.get_remote_server_key()
        digest = hashlib.sha256(schluessel.asbytes()).digest()
        return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")

    def put_exclusive(self, remote_pfad: str, data: bytes) -> None:
        if self._sftp is None:
            self._transport.auth_password(self._konfiguration.username, self._konfiguration.password)
            self._sftp = self._sftp_client_cls.from_transport(self._transport)
        # "wxb" (nicht "xb" allein - Codex-Abnahme 19.10.2026): paramikos
        # `SFTPClient.open`-Modusübersetzung setzt bei einem bloßen "xb"
        # KEIN Schreib-Flag, die Datei würde exklusiv angelegt, aber nie
        # tatsächlich beschrieben.
        with self._sftp.open(remote_pfad, "wxb") as datei:
            datei.write(data)

    def close(self) -> None:
        if self._sftp is not None:
            self._sftp.close()
        self._transport.close()


def _echte_verbindung(konfiguration: SftpKonfiguration) -> _SftpVerbindung:
    return _ParamikoVerbindung(konfiguration)


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
        remote_pfad = _remote_pfad(self._konfiguration.pfad, dateiname)

        verbindung: _SftpVerbindung | None = None
        try:
            # Der Verbindungsaufbau selbst (Factory-Aufruf) ist bereits
            # Teil des Netzkontakts - ein Fehler hier (Verbindungsaufbau/
            # Kanalverhandlung) ist ebenso ungewiss wie ein späterer
            # Schreibfehler, NIE ein sauberes "nichts gesendet".
            verbindung = self._verbindung_factory(self._konfiguration)
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
            if verbindung is not None:
                try:
                    verbindung.close()
                except Exception:
                    pass

        return BriefSftpErgebnis(
            status="GESENDET", externe_referenz=dateiname,
            provider_referenz=f"sftp:{self._konfiguration.host}:{remote_pfad}",
            versendet_am=datetime.now(timezone.utc),
        )
