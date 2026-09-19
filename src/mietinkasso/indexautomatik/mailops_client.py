"""Private JLB MailOps client; no business decisions and no automatic retries.

The ledger owns approval, recipient checks and state transitions. MailOps owns
the fixed sender, original JLB signature and SentItems evidence. A successful
HTTP request or ANGENOMMEN response is never evidence of sending or receipt.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import httpx

from mietinkasso.domain.exceptions import TransportFehlerUngewissError

_STATUSES = frozenset({"ANGENOMMEN", "GESENDET", "UNKLAR", "IN_BEARBEITUNG",
                      "DEAKTIVIERT", "NICHT_GEFUNDEN", "FEHLER"})
# "INDEX_MONATSBERICHT" (Auftrag HV-20260919-INDEX-MONATSBERICHT): Codex
# ergänzt als alleiniger Writer des getrennten MailOps-Repositories
# denselben Kind serverseitig - siehe dortige Transportpräzisierung
# ("harte Provider-Empfängergrenze mb@jlb-immo.at, unveränderte
# JLB-Signatur/Dedupe"). Der App-Client muss GENAU diesen Kind verwenden.
_KINDS = frozenset({"INDEX", "MAHNUNG", "VERTRAGSENDE", "INDEX_MONATSBERICHT"})


def _reference(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 160 or any(c in value for c in "\r\n\0"):
        raise ValueError("Ungültige MailOps-Vorgangsreferenz.")


def _expected_external_reference(value: str) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "JLBHV-" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class MailOpsAuftrag:
    referenz: str
    art: str
    empfaenger_email: str
    empfaenger_name: str
    betreff: str
    text: str
    freigabe_referenz: str

    def payload(self) -> dict[str, str]:
        values = asdict(self)
        if not all(isinstance(v, str) for v in values.values()):
            raise ValueError("MailOps-Auftrag enthält ungültige Felder.")
        _reference(self.referenz)
        if self.art not in _KINDS:
            raise ValueError("Unbekannte MailOps-Nachrichtenart.")
        for value in (self.freigabe_referenz, self.betreff):
            if not value.strip() or len(value) > 240 or any(c in value for c in "\r\n\0"):
                raise ValueError("MailOps-Freigabereferenz oder Betreff fehlt/ist ungültig.")
        if len(self.empfaenger_email) > 254 or not re.fullmatch(r"[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+", self.empfaenger_email):
            raise ValueError("Genau eine gültige MailOps-Empfängeradresse ist erforderlich.")
        if self.art == "VERTRAGSENDE" and self.empfaenger_email.lower() != "mb@jlb-immo.at":
            raise ValueError("Vertragsendehinweise gehen zuerst ausschließlich an Markus.")
        # Codex-Betriebsdetail: Ownerkonfig muss denselben Empfänger
        # erzwingen wie die Provider-Empfängergrenze im getrennten
        # MailOps-Repository - "Alle Empfänger serverseitig strikt aus
        # Ownerkonfig, nie aus Request-/Vertragsdaten" (identisches
        # Defense-in-Depth-Muster wie VERTRAGSENDE oben).
        if self.art == "INDEX_MONATSBERICHT" and self.empfaenger_email.lower() != "mb@jlb-immo.at":
            raise ValueError("Der Index-Monatsbericht geht ausschließlich an Markus.")
        if not self.text.strip() or len(self.text) > 20000 or "\0" in self.text:
            raise ValueError("MailOps-Nachrichtentext fehlt oder ist zu lang.")
        if re.search(r"\b(?:undefined|null)\b|\{\{.+?\}\}", self.text, re.I):
            raise ValueError("Unaufgelöster Platzhalter im MailOps-Nachrichtentext.")
        if len(json.dumps(values, ensure_ascii=False).encode()) > 32768:
            raise ValueError("MailOps-Auftrag überschreitet die zulässige Größe.")
        return values


@dataclass(frozen=True)
class MailOpsErgebnis:
    status: str
    externe_referenz: str | None = None
    provider_referenz: str | None = None
    versendet_am: datetime | None = None

    @property
    def versand_bestaetigt(self) -> bool:
        return self.status == "GESENDET" and self.versendet_am is not None


class MailOpsClient:
    def __init__(self, *, socket_path: str = "/run/jlb-hv-mail/mailops-hv.sock",
                 token_file: str | Path = "/run/secrets/hv-mail-token",
                 timeout_sekunden: float = 10,
                 transport_factory: Callable[[], httpx.BaseTransport] | None = None):
        if not socket_path or not math.isfinite(timeout_sekunden) or not 0 < timeout_sekunden <= 60:
            raise ValueError("Ungültige MailOps-Verbindungskonfiguration.")
        self._socket_path = socket_path
        self._token_file = Path(token_file)
        self._timeout = timeout_sekunden
        self._transport_factory = transport_factory

    def _token(self) -> str:
        try:
            token = self._token_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            raise ValueError("Privater MailOps-Zugangsschlüssel ist nicht lesbar.") from None
        if not token or len(token) > 4096 or any(c.isspace() for c in token) or "\0" in token:
            raise ValueError("Privater MailOps-Zugangsschlüssel ist ungültig.")
        return token

    def senden(self, auftrag: MailOpsAuftrag) -> MailOpsErgebnis:
        return self._request("POST", "/v1/send", referenz=auftrag.referenz, payload=auftrag.payload())

    def status_abfragen(self, referenz: str) -> MailOpsErgebnis:
        """Read back even an uncertain operation; never issues a POST."""
        _reference(referenz)
        return self._request("GET", "/v1/status", referenz=referenz)

    def _request(self, method: str, path: str, *, referenz: str,
                 payload: dict | None = None) -> MailOpsErgebnis:
        token = self._token()  # Local configuration failure, before any provider call.
        transport = self._transport_factory() if self._transport_factory else httpx.HTTPTransport(
            uds=self._socket_path, retries=0)
        try:
            with httpx.Client(transport=transport, base_url="http://mailops", timeout=self._timeout,
                              follow_redirects=False, trust_env=False) as client:
                response = client.request(method, path,
                    params={"referenz": referenz} if method == "GET" else None,
                    json=payload if method == "POST" else None,
                    headers={"Authorization": "Bearer " + token})
        except Exception:
            # Do not leak tokens, request bodies, recipient data or provider details.
            raise TransportFehlerUngewissError(
                "MailOps-Verbindungsfehler: Status prüfen; nicht automatisch erneut senden.") from None
        if response.status_code != 200 or len(response.content) > 16384:
            raise TransportFehlerUngewissError(
                "MailOps-Antwort nicht bestätigt: Status prüfen; nicht automatisch erneut senden.")
        try:
            data = response.json()
            if not isinstance(data, dict) or data.get("status") not in _STATUSES:
                raise ValueError()
            status = data["status"]
            external = data.get("externe_referenz")
            provider = data.get("provider_referenz") or None
            if provider is not None and (not isinstance(provider, str) or len(provider) > 2048):
                raise ValueError()
            if status in {"DEAKTIVIERT", "NICHT_GEFUNDEN"}:
                if external not in (None, "") or data.get("versendet_am") or provider:
                    raise ValueError()
                return MailOpsErgebnis(status=status)
            if external != _expected_external_reference(referenz):
                raise ValueError()  # A proof for another operation cannot complete this one.
            sent_at = None
            if status == "GESENDET":
                raw = data.get("versendet_am")
                if not isinstance(raw, str) or not provider:
                    raise ValueError()
                sent_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if sent_at.tzinfo is None or sent_at.utcoffset() is None:
                    raise ValueError()
            elif data.get("versendet_am") is not None:
                raise ValueError()
            return MailOpsErgebnis(status, external, provider, sent_at)
        except (ValueError, TypeError, KeyError):
            raise TransportFehlerUngewissError(
                "MailOps-Versandnachweis fehlt oder passt nicht zum Vorgang; Statusklärung erforderlich.") from None
