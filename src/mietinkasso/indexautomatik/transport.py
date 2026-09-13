"""Generischer Transportadapter für den Versand von Erhöhungsschreiben
(Auftrag 13.09.) - EIN dokumentierter Request-/Response-Vertrag statt
eines erfundenen Live-Anbieters. Die tatsächliche JLB-Mailstrecke
(HTML-Signatur mit Original-Logo, bestehende Betriebsanbindung)
verdrahtet Codex serverseitig gegen genau diesen Vertrag; dieses Modul
behauptet an keiner Stelle, bereits produktiv angebunden zu sein.

Kritische Fachregel (Fachlicher Nachtrag 13.09.): ein technisch
ANGENOMMENER Versand (HTTP 200 / `status == "ANGENOMMEN"`) ist NIE
automatisch ein bestätigter ZUGANG im Sinn des § 16 Abs 9 MRG - dieser
Adapter liefert ausschließlich die Versandbestätigung
(`VersandBestaetigung`), niemals einen Zugangsnachweis. Der Zugang wird
separat, ausdrücklich und mit Formangabe im Backoffice bestätigt
(siehe `outbox_service.py::zugang_bestaetigen`)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from mietinkasso.domain.exceptions import TransportFehlerUngewissError


@dataclass(frozen=True)
class VersandAuftrag:
    referenz: str  # idempotenzschluessel - MUSS bei jedem Retry identisch sein
    empfaenger_name: str
    empfaenger_adresse: str
    empfaenger_email: str | None
    betreff: str
    text: str


@dataclass(frozen=True)
class VersandBestaetigung:
    externe_referenz: str
    status: str  # ausschließlich "ANGENOMMEN" - kein Zugangsnachweis


class Transportadapter(Protocol):
    def senden(self, auftrag: VersandAuftrag) -> VersandBestaetigung: ...


class HttpTransportadapter:
    """Generischer HTTP-Transport mit striktem Request-/Response-Vertrag:
    POST `{referenz, empfaenger_name, empfaenger_adresse, empfaenger_email,
    betreff, text}` als JSON an `endpoint_url`; erwartet HTTP 200 mit
    `{"status": "ANGENOMMEN", "externe_referenz": "..."}`. JEDE Abweichung
    (Timeout, Verbindungsfehler, anderer Statuscode, fehlendes/falsches
    Feld) wird zu `TransportFehlerUngewissError` - nie stillschweigend als
    Erfolg oder als endgültiger Fehlschlag interpretiert, damit der
    Aufrufer (siehe `outbox_service.py`) niemals blind erneut sendet.

    `http_post` ist injizierbar (Standard: `httpx.post`), damit Tests ohne
    echten Netzwerkzugriff eine kontrollierte Response simulieren können,
    ohne den kompletten Adapter durch ein Fake zu ersetzen."""

    def __init__(self, *, endpoint_url: str, api_key: str, timeout_sekunden: float = 10.0, http_post=None):
        self._endpoint_url = endpoint_url
        self._api_key = api_key
        self._timeout_sekunden = timeout_sekunden
        if http_post is None:
            import httpx

            http_post = httpx.post
        self._http_post = http_post

    def senden(self, auftrag: VersandAuftrag) -> VersandBestaetigung:
        payload = {
            "referenz": auftrag.referenz,
            "empfaenger_name": auftrag.empfaenger_name,
            "empfaenger_adresse": auftrag.empfaenger_adresse,
            "empfaenger_email": auftrag.empfaenger_email,
            "betreff": auftrag.betreff,
            "text": auftrag.text,
        }
        # Fehlermeldungen bleiben ABSICHTLICH kategorisch statt die rohe
        # Provider-Antwort/Exception zu zitieren: eine echte Fehlerantwort
        # kann Tokens/interne Details enthalten, und `fehlergrund` landet
        # sichtbar im Backoffice (`ErhoehungsschreibenTable.fehlergrund`).
        try:
            response = self._http_post(
                self._endpoint_url,
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self._timeout_sekunden,
            )
        except Exception as exc:  # Timeout/ConnectError/... - jede Transportstörung ist ungewiss.
            raise TransportFehlerUngewissError(
                f"Transportfehler beim Versand von {auftrag.referenz!r} ({type(exc).__name__}) - "
                "möglicherweise dennoch angenommen, kein automatischer Retry."
            ) from exc

        if response.status_code != 200:
            raise TransportFehlerUngewissError(
                f"Unerwarteter Statuscode {response.status_code} beim Versand von {auftrag.referenz!r} - "
                "möglicherweise dennoch angenommen, kein automatischer Retry."
            )
        try:
            data = response.json()
        except Exception as exc:
            raise TransportFehlerUngewissError(
                f"Antwort für {auftrag.referenz!r} ist kein gültiges JSON ({type(exc).__name__})."
            ) from exc
        if not isinstance(data, dict) or data.get("status") != "ANGENOMMEN" or not data.get("externe_referenz"):
            raise TransportFehlerUngewissError(
                f"Unerwartete Antwortstruktur für {auftrag.referenz!r} - der vereinbarte Vertrag verlangt "
                "status='ANGENOMMEN' und eine externe_referenz (Antwortinhalt wird bewusst nicht "
                "protokolliert, könnte sensible Provider-Details enthalten)."
            )
        return VersandBestaetigung(externe_referenz=str(data["externe_referenz"]), status="ANGENOMMEN")


class FakeTransportadapter:
    """Isoliertes Test-Double - behauptet AUSDRÜCKLICH KEINE echte
    Live-Integration (siehe Modul-Docstring). `verhalten="TIMEOUT"`
    simuliert eine ungewisse Transportstörung, `verhalten="ANGENOMMEN"`
    (Default) eine erfolgreiche Annahme. Zeichnet jeden Aufruf in
    `aufrufe` auf, damit Tests Idempotenz/Retry-Verhalten prüfen
    können."""

    def __init__(self, *, verhalten: str = "ANGENOMMEN"):
        self._verhalten = verhalten
        self.aufrufe: list[VersandAuftrag] = []

    def senden(self, auftrag: VersandAuftrag) -> VersandBestaetigung:
        self.aufrufe.append(auftrag)
        if self._verhalten == "TIMEOUT":
            raise TransportFehlerUngewissError(f"Fake-Timeout für {auftrag.referenz!r} (Test).")
        return VersandBestaetigung(externe_referenz=f"FAKE-{len(self.aufrufe)}", status="ANGENOMMEN")
