"""Login-/Session-/CSRF-/Scope- und Layout-Helfer des Backoffice.

`_current_session` ist die FastAPI-Abhängigkeit, die jede geschützte
Route über `Depends(_current_session)` verwendet - es muss prozessweit
GENAU EIN Funktionsobjekt sein, sonst entstehen mehrere unabhängige
Abhängigkeitsbäume. Deshalb importieren die Routenmodule diese Funktion
von hier und definieren sie nie selbst.

Abhängigkeiten werden ausdrücklich über `dependencies` gelesen; dieses
Modul importiert bewusst kein Routenmodul und nicht `app` (keine
Zyklen)."""

from __future__ import annotations

from html import escape as h
from urllib.parse import urlparse

from fastapi import Cookie, HTTPException, Request
from fastapi.responses import HTMLResponse

from mietinkasso.auth.service import AuthContext
from mietinkasso.backoffice.views import flash_error, seite
from mietinkasso.domain.enums import Rolle

from mietinkasso.backoffice import dependencies as deps


def _require_enabled() -> None:
    if not deps._settings.backoffice_password_hash:
        raise HTTPException(
            status_code=503,
            detail="MIETINKASSO_BACKOFFICE_PASSWORD_HASH ist nicht konfiguriert; das Backoffice bleibt "
            "geschlossen (closed by default), bis ein lokaler Login eingerichtet ist.",
        )


def _redirect_to_login() -> HTTPException:
    return HTTPException(status_code=303, headers={"Location": "/backoffice/login"})


def _current_session(session_cookie: str | None = Cookie(default=None, alias=deps._COOKIE_NAME)):
    _require_enabled()
    session = deps._sessions.holen(session_cookie)
    if session is None:
        raise _redirect_to_login()
    return session


def _pruefe_login_origin(request: Request) -> None:
    """Der Login-POST hat (bewusst) noch KEIN sitzungsgebundenes
    CSRF-Token - das entsteht erst NACH erfolgreicher Anmeldung
    (`SessionStore.erstellen`). Die Standardverteidigung gegen
    Login-CSRF (ein Angreifer verleitet den Browser des Opfers, sich in
    eine vom Angreifer kontrollierte Sitzung einzuloggen) ist eine
    Origin-Prüfung: der `Origin`-Header (ersatzweise `Referer`) muss zum
    eigenen `Host`-Header dieser Anfrage passen. Fehlen beide, wird die
    Anfrage ABGELEHNT statt stillschweigend durchgelassen - ein echter
    Browser sendet bei einem Formular-POST praktisch immer mindestens
    einen der beiden."""

    eigener_host = request.headers.get("host")
    kandidat = request.headers.get("origin") or request.headers.get("referer")
    if not eigener_host or not kandidat or urlparse(kandidat).netloc != eigener_host:
        raise HTTPException(status_code=403, detail="Anfrage von unerwartetem Origin abgelehnt.")


def _ctx(session) -> AuthContext:
    """EIN Operator, volle Sicht (ADMIN) - kein Mehrbenutzer-/Rollen-
    Management über HTTP in diesem Pilotmodul. Der Objekt-107-Ausschluss
    gilt unabhängig davon serverseitig für JEDE Rolle, ADMIN eingeschlossen
    (siehe stammdaten/repository.py::pruefe_vertrag_nicht_ausgeschlossen)."""

    return AuthContext(user_id=session.user_id, rolle=Rolle.ADMIN, gesellschaft_ids=None)


def _verify_csrf(session, csrf_token: str) -> None:
    if not deps._sessions.csrf_gueltig(session, csrf_token):
        raise HTTPException(status_code=403, detail="Ungültiges oder fehlendes csrf_token.")


def _layout(request: Request, session, titel: str, inhalt: str) -> HTMLResponse:
    return HTMLResponse(seite(
        titel=titel, inhalt=inhalt, user_id=session.user_id if session else None,
        csrf_token=session.csrf_token if session else None,
        environment=deps._settings.environment, send_enabled=deps._settings.send_enabled,
        aktueller_pfad=request.url.path,
    ))


def _fehlerseite(session, titel: str, meldung: str, zurueck_href: str = "/backoffice/") -> HTMLResponse:
    inhalt = flash_error(meldung) + f'<p><a href="{h(zurueck_href)}">&larr; zurück</a></p>'
    return HTMLResponse(seite(
        titel=titel, inhalt=inhalt, user_id=session.user_id, csrf_token=session.csrf_token,
        environment=deps._settings.environment, send_enabled=deps._settings.send_enabled,
    ), status_code=400)


def _ist_objekt_gesperrt(objekt_id: str) -> bool:
    objekt = deps._stammdaten_repo.get_objekt(objekt_id)
    return objekt is not None and objekt.ausgeschlossen


def _objekt_fuer_vertrag_gesperrt(vertrag_id: str) -> bool:
    try:
        return deps._stammdaten_repo.objekt_fuer_vertrag(vertrag_id).ausgeschlossen
    except ValueError:
        return False
