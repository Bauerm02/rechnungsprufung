"""Synthetischer EinfachBrief-Provider für `httpx.MockTransport` - niemals
ein echter Netzwerkzugriff, weder auf den Produktiv- noch den Testendpoint."""

from __future__ import annotations

import base64
import io
import json

import httpx
from pypdf import PdfReader

UPLOAD = "uploadToShoppingCart"
STATUS = "getDeliveryStatus"
VERARBEITEN = "processDeliveriesFromShoppingCart"


class FakeEinfachBrief:
    """Beantwortet nur die drei verwendeten Methoden; jeder andere Pfad
    (`removeDeliveries...`, `processDelivery`, ...) lässt den Test scheitern.
    `upload`/`lieferung`/`dokument`/`warenkorb`/`verarbeitung` überschreiben
    einzelne Antwortfelder; `stoerung[methode]` liefert eine Exception oder
    `httpx.Response` (optional als Funktion des Requests)."""

    def __init__(self, *, fremde_lieferung=False, preis=4.8):
        self.anfragen: list[httpx.Request] = []
        self.upload: dict = {}
        self.lieferung: dict = {}
        self.dokument: dict = {}
        self.warenkorb: dict = {}
        self.verarbeitung: dict = {}
        self.stoerung: dict = {}
        self.fremde_lieferung = fremde_lieferung
        self.preis = preis
        self.hochgeladen: list[dict] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._antworte)

    def methoden(self) -> list[str]:
        return [anfrage.url.path.split("/v1/")[1].split("/")[0] for anfrage in self.anfragen]

    def bodies(self, methode: str) -> list[dict]:
        return [
            json.loads(a.content) for a in self.anfragen
            if a.url.path.split("/v1/")[1].split("/")[0] == methode
        ]

    def _antworte(self, request: httpx.Request) -> httpx.Response:
        self.anfragen.append(request)
        methode = request.url.path.split("/v1/")[1].split("/")[0]
        if methode in self.stoerung:
            stoerung = self.stoerung[methode]
            if callable(stoerung):
                stoerung = stoerung(request)
            if isinstance(stoerung, Exception):
                raise stoerung
            if stoerung is not None:
                return stoerung
        if methode == UPLOAD:
            return self._upload(request)
        if methode == STATUS:
            return self._status(request)
        if methode == VERARBEITEN:
            return httpx.Response(200, json={"orderId": "ORDER-1", "errorDescription": [], "totalPrice": self.preis, **self.verarbeitung})
        raise AssertionError(f"Nicht vorgesehene Methode aufgerufen: {request.url.path}")

    def _upload(self, request: httpx.Request) -> httpx.Response:
        dokument = json.loads(request.content)["documentPayloadList"][0]
        self.hochgeladen.append(dokument)
        return httpx.Response(200, json={
            "success": True, "finished": True, "infected": False, "warningExist": False,
            "errorFileList": [], "warningFileList": [], "successFileList": [dokument["name"]],
            "shoppingCartId": f"CART-{len(self.hochgeladen)}", **self.upload,
        })

    def _status(self, request: httpx.Request) -> httpx.Response:
        warenkorb_id = request.url.path.rsplit("/", 1)[1]
        dokument = self.hochgeladen[int(warenkorb_id.split("-")[1]) - 1]
        seiten = len(PdfReader(io.BytesIO(base64.b64decode(dokument["payload"]))).pages)
        lieferungen = []
        if self.fremde_lieferung:
            lieferungen.append({
                "deliveryId": "FREMD-1", "valid": False,
                "documentsResults": [{"foreignId": "fremder-schluessel", "valid": False}],
            })
        lieferungen.append({
            "deliveryId": "DELIVERY-1", "valid": True, "hasWarning": False, "ebrief": False,
            "registerMailType": "EINSCHREIBEN", "shipmentType": "Einzelbrief", "postalPriority": "PRIO",
            "pageColor": "nocolor", "sided": "single", "numberOfDocuments": 1, "numberOfPages": seiten,
            "attachmentExist": False, "paymentLocked": False, "totalPrice": self.preis,
            "documentsResults": [{
                "foreignId": dokument["foreignId"], "valid": True, "numberOfPages": seiten,
                "registerMailType": "EINSCHREIBEN", "error": [], "warning": [], **self.dokument,
            }],
            **self.lieferung,
        })
        return httpx.Response(200, json={
            "shoppingCartId": warenkorb_id, "valid": True, "hasWarning": False, "validityResult": lieferungen,
            **self.warenkorb,
        })
