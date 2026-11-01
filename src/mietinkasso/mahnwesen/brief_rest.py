"""EinfachBrief-REST-Transportoption der BESTEHENDEN Briefstrecke - KEIN
zweiter Sender, kein eigenes Journal, kein eigener Job: Aufrufer bleibt
ausschließlich `indexautomatik/mailversand_service.py::HVMailversandService`
(`_sende_brief`/`_nachsenden_brief`) samt aller dortigen Gates. KEINE
behauptete Live-Anbindung: dieser Code hat nie einen echten Endpoint
gesehen (Quellen: OpenAPI v1.5.0 produktiv/test, Anbieter-Anleitung
"Einfach Brief REST API"; bei Widerspruch gilt die OpenAPI, daher
`registeredMailType` statt des alten `registeredMail` der PDF-Beispiele).

Geschlossen per Default: `konfiguration_aus_settings` liefert nur bei
expliziter Auswahl `brief_transport == "rest"` UND vollständig gültigen
Werten eine Konfiguration; es gibt KEIN stilles Ausweichen zwischen SFTP
und REST (`transport_auswahl`). Der API-Schlüssel kommt ausschließlich
aus einer privaten Datei, steht nie in Settings/Env und erscheint weder
in `repr` noch in einer Fehlermeldung.

Ablauf für GENAU EINEN Einzelbrief, ausschließlich dokumentierte Methoden:

1. `POST /sendAndPollStatus/v1/uploadToShoppingCart` - das archivierte PDF
   (Base64 im dokumentierten Feld `payload`), `printOnly=true`,
   `foreignId` = permanenter Sende-Schlüssel. Kein `receiver`: die
   Stammdaten kennen weder getrennte Namens- noch Adressfelder, eine
   Zerlegung wäre geraten; die Adresse steht im Adressblock des PDF
   (`skipAddressCheck=false`, siehe OpenAPI-Beschreibung des Felds).
2. `GET /sendAndPollStatus/v1/getDeliveryStatus/{shoppingCartId}` - die
   EINE Lieferung mit dieser `foreignId` wird gegen alle bestellten
   Parameter, die lokale Seitenzahl und die Preisobergrenze geprüft;
   andere Lieferungen im Warenkorb bleiben unberührt.
3. `POST /sendAndPollStatus/v1/processDeliveriesFromShoppingCart` - nur
   mit dieser EINEN `deliveryId`, nie der ganze Warenkorb.

`removeDeliveriesFromShoppingCart` (kann per `removeAll` fremde
Lieferungen löschen), `processDelivery` (kein Prüfpunkt vor der
Verarbeitung) und `viewShoppingCart` werden bewusst nicht verwendet.

Bewusst gesperrte Schritte, weil die Quellen die Semantik nicht belegen:
- `registeredMailType` hat KEINEN Default. Ob das bisherige Produkt
  "Inland Einschreiben Einwurf" (`brief_sftp.SendeParameter.produkt=1`)
  dem Wert `Einschreiben` oder `Einschreiben Einfach` entspricht, steht in
  keiner Quelle - ohne ausdrückliche Festlegung bleibt REST nicht verfügbar.
- `printJobStatusType`/`trackingNumber`/`orderDetails` werden NICHT als
  Nachweis ausgewertet; die Bedeutung ihrer Werte ist nicht dokumentiert.
  Eine Quittung belegt höchstens Annahme/Verarbeitung, nie Postaufgabe
  oder Zugang: `status` ist deshalb wie bei SFTP `"UEBERMITTELT"`, der
  Mahnlauf bleibt UNSICHER bis zum geprüften Anbieterbeleg.

Dokumentiert sind ausschließlich HTTP-200-Antworten. Ab dem ersten
Request ist deshalb JEDE Abweichung (Timeout, anderer Status, Redirect,
ungültige Antwort, Infektion, Warnung, Fehler, falsche IDs)
`TransportFehlerUngewissError` - nie BLOCKIERT, nie ein Neuversuch. Der
Anbieter dokumentiert keine Idempotenz für `foreignId`; anders als das
exklusive SFTP-Anlegen schützt also nichts remote vor einer Dublette.
`BriefRestVersandClaim` reserviert den Sende-Schlüssel deshalb dauerhaft
VOR dem ersten Request: jeder Schlüssel bekommt höchstens einen Versuch,
auch nach Absturz oder Timeout. Ein gestoppter Lauf kann eine
unverarbeitete Lieferung im Warenkorb zurücklassen; sie wird nur manuell
im Portal geklärt."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import re
import stat
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable

import httpx
from pypdf import PdfReader
from sqlalchemy import select

from mietinkasso.domain.exceptions import TransportFehlerUngewissError
from mietinkasso.infrastructure.db.sqlite_write_lock import schreibgesperrte_session
from mietinkasso.infrastructure.db.tables import AuditEventTable
from mietinkasso.mahnwesen.brief_sftp import (
    _SENDE_SCHLUESSEL_MUSTER, MAX_SIMPLEX_SEITEN, PostSendevoraussetzungError,
)

TRANSPORT_SFTP = "sftp"
TRANSPORT_REST = "rest"

_BASIS_URLS = frozenset({"https://www.einfach-brief.at/be", "https://abn-www.einfach-brief.at/be_t"})
_PFAD_UPLOAD = "/sendAndPollStatus/v1/uploadToShoppingCart"
_PFAD_STATUS = "/sendAndPollStatus/v1/getDeliveryStatus/"
_PFAD_VERARBEITEN = "/sendAndPollStatus/v1/processDeliveriesFromShoppingCart"

# Rückmeldewerte laut `DeliveryPayloadResult.registerMailType`/
# `DocumentPayloadResult.registerMailType`, inklusive der Schreibweise
# "Einshreiben Einfach" aus der OpenAPI selbst. `NONE` ist bewusst nicht
# bestellbar: das wäre eine Herabstufung gegenüber dem freigegebenen Produkt.
_EINSCHREIBEN_RUECKMELDUNG = {
    "Einschreiben": frozenset({"EINSCHREIBEN", "Einschreiben"}),
    "Einschreiben Einfach": frozenset({"EINSCHREIBEN_EINFACH", "Einshreiben Einfach"}),
}

_CONNECT_SEKUNDEN = 10.0
_READ_SEKUNDEN = 30.0
_WRITE_SEKUNDEN = 30.0
_POOL_SEKUNDEN = 5.0
_GESAMT_SEKUNDEN = 90.0
_MAX_ANTWORT_BYTES = 4 * 1024 * 1024
_MAX_PDF_BYTES = 15 * 1024 * 1024
_MAX_SCHLUESSEL_LAENGE = 4096
# POSIX-Rechtebits sagen unter Windows nichts aus.
_POSIX_RECHTE_PRUEFEN = os.name == "posix"
_ANBIETER_ID_MUSTER = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_CLAIM_ENTITY_TYP = "brief_rest_versand"
_CLAIM_AKTION = "BRIEF_REST_VERSAND_CLAIM"


class PostRestBereitsVersuchtError(TransportFehlerUngewissError):
    """Für diesen Sende-Schlüssel existiert bereits ein dauerhafter Claim -
    ein früherer Versuch kann angekommen sein, daher kein weiterer Request."""


@dataclass(frozen=True)
class RestKonfiguration:
    basis_url: str
    registered_mail_type: str
    preisobergrenze: Decimal
    api_key: str = field(repr=False)


@dataclass(frozen=True)
class BriefRestErgebnis:
    """Dieselben Felder wie `brief_sftp.BriefSftpErgebnis`; `status` ist
    IMMER `"UEBERMITTELT"`, damit `nachweis_daten` es nie als Versand wertet."""

    status: str
    externe_referenz: str
    provider_referenz: str
    versendet_am: datetime


def transport_auswahl(settings) -> str | None:
    """Liefert den explizit gewählten Transport - ein unbekannter Wert
    wählt KEINEN Transport, statt auf SFTP oder REST auszuweichen."""

    auswahl = settings.brief_transport
    return auswahl if auswahl in (TRANSPORT_SFTP, TRANSPORT_REST) else None


def _dateirechte_privat(modus: int) -> bool:
    """Eine reguläre Datei ohne Zugriff für "andere" und ohne Schreibrecht
    der Gruppe; Gruppen-Lesen bleibt für Secret-Mounts (0440) zulässig."""

    return stat.S_ISREG(modus) and not modus & (stat.S_IRWXO | stat.S_IWGRP)


def _lies_api_schluessel(pfad: str) -> str | None:
    if not pfad:
        return None
    datei = Path(pfad)
    try:
        if _POSIX_RECHTE_PRUEFEN and not _dateirechte_privat(datei.lstat().st_mode):
            return None
        rohdaten = datei.read_bytes()
    except OSError:
        return None
    if len(rohdaten) > _MAX_SCHLUESSEL_LAENGE:
        return None
    try:
        schluessel = rohdaten.decode("ascii").strip()
    except UnicodeDecodeError:
        return None
    if not schluessel or any(not 0x21 <= ord(zeichen) <= 0x7E for zeichen in schluessel):
        return None
    return schluessel


def konfiguration_aus_settings(settings) -> RestKonfiguration | None:
    if transport_auswahl(settings) != TRANSPORT_REST:
        return None
    basis_url = settings.brief_rest_basis_url
    if basis_url not in _BASIS_URLS:
        return None
    registered_mail_type = settings.brief_rest_registered_mail_type
    if registered_mail_type not in _EINSCHREIBEN_RUECKMELDUNG:
        return None
    preisobergrenze = settings.brief_rest_preisobergrenze
    if preisobergrenze is None or not preisobergrenze.is_finite() or preisobergrenze <= 0:
        return None
    api_key = _lies_api_schluessel((settings.brief_rest_api_key_datei or "").strip())
    if api_key is None:
        return None
    return RestKonfiguration(
        basis_url=basis_url, registered_mail_type=registered_mail_type,
        preisobergrenze=preisobergrenze, api_key=api_key,
    )


class BriefRestVersandClaim:
    """Dauerhafter "höchstens ein Versuch je Sende-Schlüssel"-Claim in der
    vorhandenen `AuditEventTable` (keine Migration). Nur unter SQLite
    atomar (`schreibgesperrte_session`, Produktivbetrieb); für andere
    Datenbanken fehlt ein Unique-Schutz, dort wird daher abgelehnt."""

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    def beanspruche(self, *, sende_schluessel: str, pdf_sha256: str, erstellt_von: str) -> bool:
        with schreibgesperrte_session(self._session_factory) as session:
            if session.get_bind().dialect.name != "sqlite":
                raise ValueError("REST-Versandclaim ist nur unter SQLite atomar - kein Versand.")
            bestehend = session.execute(select(AuditEventTable.id).where(
                AuditEventTable.entity_typ == _CLAIM_ENTITY_TYP,
                AuditEventTable.entity_id == sende_schluessel,
                AuditEventTable.aktion == _CLAIM_AKTION,
            )).first()
            if bestehend is not None:
                session.rollback()
                return False
            session.add(AuditEventTable(
                entity_typ=_CLAIM_ENTITY_TYP, entity_id=sende_schluessel, aktion=_CLAIM_AKTION,
                akteur=erstellt_von, payload={"pdf_sha256": pdf_sha256},
            ))
            session.commit()
            return True


def _ungewiss(grund: str) -> TransportFehlerUngewissError:
    # Nie Antwortinhalt, Header oder PDF-Daten in die Meldung übernehmen.
    return TransportFehlerUngewissError(
        f"EinfachBrief-REST: {grund} - möglicherweise angenommen, Portal prüfen; kein automatischer Neuversuch."
    )


def _anbieter_id(wert: object, bezeichnung: str) -> str:
    if not isinstance(wert, str) or not _ANBIETER_ID_MUSTER.fullmatch(wert):
        raise _ungewiss(f"ungültige {bezeichnung} in der Antwort")
    return wert


def _ohne_eintraege(wert: object) -> bool:
    return wert is None or wert == []


def _preis(wert: object) -> Decimal | None:
    if isinstance(wert, bool) or not isinstance(wert, (int, float)):
        return None
    if isinstance(wert, float) and not math.isfinite(wert):
        return None
    try:
        return Decimal(str(wert))
    except InvalidOperation:
        return None


class PostRestTransportadapter:
    """`transport_factory` ist injizierbar (Standard: echter
    `httpx.HTTPTransport` mit `retries=0` und TLS-Prüfung), damit Tests über
    `httpx.MockTransport` ohne Netz laufen."""

    def __init__(
        self, konfiguration: RestKonfiguration, *,
        transport_factory: Callable[[], httpx.BaseTransport] | None = None,
        gesamt_sekunden: float = _GESAMT_SEKUNDEN,
    ) -> None:
        self._konfiguration = konfiguration
        self._transport_factory = transport_factory or (lambda: httpx.HTTPTransport(verify=True, retries=0))
        self._gesamt_sekunden = gesamt_sekunden

    def pruefe_vorab(self, *, pdf_bytes: bytes, pdf_sha256: str, sende_schluessel: str) -> int:
        """Rein lokale Prüfung VOR Claim und Netzkontakt (`ValueError` =
        sicher nichts gesendet); liefert die lokale Seitenzahl."""

        if not isinstance(sende_schluessel, str) or not _SENDE_SCHLUESSEL_MUSTER.fullmatch(sende_schluessel):
            raise ValueError("Kein gültiger, bereits reservierter Sende-Schlüssel.")
        if hashlib.sha256(pdf_bytes).hexdigest() != pdf_sha256:
            raise PostSendevoraussetzungError("PDF-Bytes stimmen nicht mit dem archivierten Hash überein.")
        if pdf_bytes[:5] != b"%PDF-" or len(pdf_bytes) > _MAX_PDF_BYTES:
            raise PostSendevoraussetzungError("Keine zulässige PDF-Datei - Versand abgelehnt.")
        try:
            seiten = len(PdfReader(io.BytesIO(pdf_bytes)).pages)
        except Exception as exc:
            raise PostSendevoraussetzungError("PDF ist nicht lesbar - Versand abgelehnt.") from exc
        if not 1 <= seiten <= MAX_SIMPLEX_SEITEN:
            raise PostSendevoraussetzungError(f"{seiten} Seiten liegen außerhalb des Simplex-Limits.")
        return seiten

    def sende(self, *, pdf_bytes: bytes, pdf_sha256: str, sende_schluessel: str) -> BriefRestErgebnis:
        """Darf nur NACH erfolgreichem `BriefRestVersandClaim.beanspruche`
        aufgerufen werden. Einziger Aufrufer: `HVMailversandService`."""

        seiten = self.pruefe_vorab(pdf_bytes=pdf_bytes, pdf_sha256=pdf_sha256, sende_schluessel=sende_schluessel)
        frist = time.monotonic() + self._gesamt_sekunden
        name = f"{sende_schluessel}.pdf"

        warenkorb_id = self._lade_hoch(pdf_bytes=pdf_bytes, sende_schluessel=sende_schluessel, name=name, frist=frist)
        lieferung_id = self._pruefe_lieferung(
            warenkorb_id=warenkorb_id, sende_schluessel=sende_schluessel, seiten=seiten, frist=frist,
        )
        auftrag_id = self._verarbeite(lieferung_id=lieferung_id, frist=frist)

        return BriefRestErgebnis(
            status="UEBERMITTELT",
            externe_referenz=f"einfachbrief:{sende_schluessel}:order:{auftrag_id}:delivery:{lieferung_id}",
            provider_referenz=f"einfachbrief:{auftrag_id}",
            versendet_am=datetime.now(timezone.utc),
        )

    def _lade_hoch(self, *, pdf_bytes: bytes, sende_schluessel: str, name: str, frist: float) -> str:
        # Artikelparameter wie `brief_sftp.SendeParameter()`: Schwarz-Weiß,
        # Simplex, PRIO; `splitOn` wie in beiden Beispielen der Anleitung.
        daten = self._anfrage("POST", _PFAD_UPLOAD, frist=frist, nutzdaten={
            "pageColor": "nocolor",
            "sided": "single",
            "registeredMailType": self._konfiguration.registered_mail_type,
            "postalPriority": "PRIO",
            "shipmentType": "Einzelbrief",
            "printOnly": True,
            "skipAddressCheck": False,
            "includeAddressPage": False,
            "splitOn": 0,
            "documentPayloadList": [{
                "name": name,
                "mimeType": "application/pdf",
                "payload": base64.b64encode(pdf_bytes).decode("ascii"),
                "foreignId": sende_schluessel,
            }],
        })
        if (
            daten.get("success") is not True or daten.get("finished") is not True
            or daten.get("infected") is not False or daten.get("warningExist") is not False
            or not _ohne_eintraege(daten.get("errorFileList")) or not _ohne_eintraege(daten.get("warningFileList"))
            or daten.get("successFileList") not in (None, [], [name])
        ):
            raise _ungewiss("Upload nicht vorbehaltlos bestätigt, Verarbeitung nicht ausgelöst")
        return _anbieter_id(daten.get("shoppingCartId"), "shoppingCartId")

    def _pruefe_lieferung(self, *, warenkorb_id: str, sende_schluessel: str, seiten: int, frist: float) -> str:
        daten = self._anfrage("GET", _PFAD_STATUS + warenkorb_id, frist=frist)
        lieferungen = daten.get("validityResult")
        if daten.get("shoppingCartId") != warenkorb_id or not isinstance(lieferungen, list):
            raise _ungewiss("Warenkorbantwort passt nicht zum Upload, Verarbeitung nicht ausgelöst")

        treffer = []
        for lieferung in lieferungen:
            dokumente = lieferung.get("documentsResults") if isinstance(lieferung, dict) else None
            if isinstance(dokumente, list) and any(
                isinstance(d, dict) and d.get("foreignId") == sende_schluessel for d in dokumente
            ):
                treffer.append(lieferung)
        if len(treffer) != 1:
            raise _ungewiss("Lieferung mit diesem Sende-Schlüssel nicht eindeutig, Verarbeitung nicht ausgelöst")

        lieferung = treffer[0]
        dokumente = lieferung["documentsResults"]
        erlaubt = _EINSCHREIBEN_RUECKMELDUNG[self._konfiguration.registered_mail_type]
        dokument = dokumente[0]
        preis = _preis(lieferung.get("totalPrice"))
        if (
            len(dokumente) != 1
            or lieferung.get("valid") is not True or lieferung.get("hasWarning") is not False
            # E-Brief: Kanal und Produkt prüfen, `printOnly` allein reicht nicht.
            or lieferung.get("ebrief") is not False or lieferung.get("registerMailType") not in erlaubt
            or lieferung.get("shipmentType") != "Einzelbrief" or lieferung.get("postalPriority") != "PRIO"
            or lieferung.get("pageColor") != "nocolor" or lieferung.get("sided") != "single"
            or lieferung.get("numberOfDocuments") != 1 or lieferung.get("numberOfPages") != seiten
            or lieferung.get("attachmentExist") is not False or lieferung.get("paymentLocked") is not False
            or dokument.get("valid") is not True or dokument.get("numberOfPages") != seiten
            or dokument.get("registerMailType") not in erlaubt
            or not _ohne_eintraege(dokument.get("error")) or not _ohne_eintraege(dokument.get("warning"))
        ):
            raise _ungewiss("Lieferung weicht von der Bestellung ab, Verarbeitung nicht ausgelöst")
        if preis is None or preis <= 0 or preis > self._konfiguration.preisobergrenze:
            raise _ungewiss("Preis fehlt oder liegt über der Obergrenze, Verarbeitung nicht ausgelöst")
        return _anbieter_id(lieferung.get("deliveryId"), "deliveryId")

    def _verarbeite(self, *, lieferung_id: str, frist: float) -> str:
        daten = self._anfrage("POST", _PFAD_VERARBEITEN, frist=frist, nutzdaten={"deliveries": [lieferung_id]})
        if not _ohne_eintraege(daten.get("errorDescription")):
            raise _ungewiss("Verarbeitung meldet Fehler")
        return _anbieter_id(daten.get("orderId"), "orderId")

    def _anfrage(self, methode: str, pfad: str, *, frist: float, nutzdaten: dict | None = None) -> dict:
        """Genau EIN Request ohne Retry/Redirect. Die Gesamtfrist läuft im
        Aufruferthread, weil httpx-Timeouts je Einzeloperation gelten und
        weder DNS noch eine tröpfelnde Antwort begrenzen - derselbe
        Grundsatz wie `brief_sftp._ParamikoVerbindung._baue_sftp_kanal_mit_deadline`."""

        rest = frist - time.monotonic()
        if rest <= 0:
            raise _ungewiss("Gesamtfrist überschritten")

        ergebnis: dict = {}
        fertig = threading.Event()
        try:
            client = httpx.Client(
                transport=self._transport_factory(),
                timeout=httpx.Timeout(
                    connect=_CONNECT_SEKUNDEN, read=_READ_SEKUNDEN, write=_WRITE_SEKUNDEN, pool=_POOL_SEKUNDEN,
                ),
                follow_redirects=False, trust_env=False,
            )
        except Exception:
            raise _ungewiss("HTTP-Client nicht verfügbar") from None

        def _ausfuehren() -> None:
            try:
                with client.stream(
                    methode, self._konfiguration.basis_url + pfad, json=nutzdaten,
                    headers={"x-api-key": self._konfiguration.api_key, "Accept": "application/json"},
                ) as antwort:
                    inhalt = bytearray()
                    for block in antwort.iter_bytes():
                        inhalt.extend(block)
                        if len(inhalt) > _MAX_ANTWORT_BYTES:
                            raise ValueError("Antwort zu groß")
                ergebnis["status"], ergebnis["inhalt"] = antwort.status_code, bytes(inhalt)
            except BaseException:  # noqa: BLE001 - jeder Abbruch ist unten ungewiss
                ergebnis["abbruch"] = True
            finally:
                fertig.set()

        threading.Thread(target=_ausfuehren, daemon=True).start()
        beendet = fertig.wait(timeout=rest)
        try:
            client.close()
        except Exception:
            pass
        if not beendet:
            raise _ungewiss("Zeitgrenze überschritten")
        if ergebnis.get("abbruch"):
            raise _ungewiss("Verbindung gestört")
        if ergebnis["status"] != 200:
            raise _ungewiss(f"HTTP-Status {ergebnis['status']} statt 200")
        try:
            daten = json.loads(ergebnis["inhalt"])
        except ValueError:
            raise _ungewiss("Antwort ist kein gültiges JSON") from None
        if not isinstance(daten, dict):
            raise _ungewiss("Antwort hat kein gültiges Format")
        return daten
