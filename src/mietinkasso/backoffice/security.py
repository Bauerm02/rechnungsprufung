"""Passwort-Hashing und In-Memory-Session-/CSRF-Verwaltung für das
Backoffice-Pilotmodul.

Bewusst EIN Prozess, EIN Login (siehe `infrastructure/config.py`): kein
verteilter Session-Store, keine Mehrbenutzer-/Rollenverwaltung über HTTP -
das ist eine spätere Inbetriebnahme (siehe OFFENE_PUNKTE.md). Die
Session-ID im Cookie ist ein rein opakes, zufälliges Token (kein
signiertes JWT/Cookie, kein API-Token) - der Zustand (welcher User, CSRF-
Token, Ablauf) lebt ausschließlich serverseitig in diesem Prozess.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sys
import time
from dataclasses import dataclass, field

_PBKDF2_ALGORITHMUS = "sha256"
_PBKDF2_ITERATIONEN = 390_000


def hash_passwort(passwort: str) -> str:
    """Erzeugt einen speicherbaren Passwort-Hash (PBKDF2-HMAC-SHA256, 16
    Byte Zufalls-Salt). Nie das Klartext-Passwort speichern/loggen."""

    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(_PBKDF2_ALGORITHMUS, passwort.encode("utf-8"), salt, _PBKDF2_ITERATIONEN)
    return f"pbkdf2_{_PBKDF2_ALGORITHMUS}${_PBKDF2_ITERATIONEN}${salt.hex()}${digest.hex()}"


def pruefe_passwort(passwort: str, gespeicherter_hash: str) -> bool:
    """Zeitkonstanter Vergleich (`hmac.compare_digest`) gegen einen mit
    `hash_passwort` erzeugten Hash. Gibt bei jedem Parsing-/Formatfehler
    `False` zurück statt eine Exception zu werfen (ein falsch
    konfigurierter Hash darf niemals versehentlich als "kein Passwort
    nötig" durchgehen)."""

    try:
        algorithmus, iterationen_text, salt_hex, digest_hex = gespeicherter_hash.split("$")
        algorithmus = algorithmus.removeprefix("pbkdf2_")
        iterationen = int(iterationen_text)
        salt = bytes.fromhex(salt_hex)
        erwarteter_digest = bytes.fromhex(digest_hex)
    except (ValueError, AttributeError):
        return False
    berechneter_digest = hashlib.pbkdf2_hmac(algorithmus, passwort.encode("utf-8"), salt, iterationen)
    return hmac.compare_digest(berechneter_digest, erwarteter_digest)


@dataclass
class BackofficeSession:
    user_id: str
    csrf_token: str
    erstellt_um: float = field(default_factory=time.monotonic)
    zuletzt_aktiv_um: float = field(default_factory=time.monotonic)


class SessionStore:
    """Rein prozessinterner Session-Speicher (dict) - überlebt keinen
    Neustart und ist NICHT für mehrere Worker-Prozesse geeignet. Für den
    lokalen Ein-Prozess-Pilotbetrieb (ein `uvicorn`-Worker, kein
    Mehrbenutzer-Onlinebetrieb) bewusst so einfach gehalten; ein echter
    Mehrbenutzerbetrieb braucht einen externen Session-Store (Redis/DB) -
    siehe OFFENE_PUNKTE.md."""

    def __init__(self, *, ttl_sekunden: int):
        self._sessions: dict[str, BackofficeSession] = {}
        self._ttl_sekunden = ttl_sekunden

    def erstellen(self, user_id: str) -> tuple[str, str]:
        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        self._sessions[session_id] = BackofficeSession(user_id=user_id, csrf_token=csrf_token)
        return session_id, csrf_token

    def holen(self, session_id: str | None) -> BackofficeSession | None:
        if not session_id:
            return None
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if time.monotonic() - session.zuletzt_aktiv_um > self._ttl_sekunden:
            del self._sessions[session_id]
            return None
        session.zuletzt_aktiv_um = time.monotonic()
        return session

    def loeschen(self, session_id: str | None) -> None:
        if session_id and session_id in self._sessions:
            del self._sessions[session_id]

    def csrf_gueltig(self, session: BackofficeSession, token: str | None) -> bool:
        return bool(token) and hmac.compare_digest(session.csrf_token, token)


class LoginRateLimiter:
    """Kurzzeitige, GLOBALE Sperre nach zu vielen Fehlversuchen - bewusst
    NICHT pro Client-IP: eine IP-basierte Begrenzung müsste entweder
    Proxy-Headern wie `X-Forwarded-For` vertrauen (vom Client
    fälschbar, wenn der vorgeschaltete Reverse-Proxy sie nicht
    zuverlässig überschreibt) oder eine verlässliche Kenntnis der
    Proxy-Topologie voraussetzen, die dieses Modul nicht hat. Für den
    Ein-Operator-Pilotbetrieb ist eine einzige globale Sperre nach
    wiederholten Fehlversuchen ausreichend und braucht kein Vertrauen in
    Client-Header. Rein prozessintern (wie `SessionStore`), überlebt
    keinen Neustart."""

    def __init__(self, *, max_versuche: int, fenster_sekunden: float, sperre_sekunden: float):
        self._max_versuche = max_versuche
        self._fenster_sekunden = fenster_sekunden
        self._sperre_sekunden = sperre_sekunden
        self._fehlversuche: list[float] = []
        self._gesperrt_bis: float | None = None

    def gesperrt(self) -> bool:
        jetzt = time.monotonic()
        if self._gesperrt_bis is not None:
            if jetzt < self._gesperrt_bis:
                return True
            self._gesperrt_bis = None
            self._fehlversuche.clear()
        return False

    def fehlversuch_melden(self) -> None:
        jetzt = time.monotonic()
        self._fehlversuche = [t for t in self._fehlversuche if jetzt - t <= self._fenster_sekunden]
        self._fehlversuche.append(jetzt)
        if len(self._fehlversuche) >= self._max_versuche:
            self._gesperrt_bis = jetzt + self._sperre_sekunden
            self._fehlversuche.clear()

    def erfolgreich_angemeldet(self) -> None:
        self._fehlversuche.clear()
        self._gesperrt_bis = None


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Verwendung: python -m mietinkasso.backoffice.security <passwort>")
        raise SystemExit(1)
    print(hash_passwort(sys.argv[1]))
