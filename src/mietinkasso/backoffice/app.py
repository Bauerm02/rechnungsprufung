"""Bedienbares Backoffice für den Mietinkasso-Piloten (lokal, synthetische
Demodaten, EIN Operator/Login, deterministisch - keinerlei KI-Aufruf zur
Laufzeit).

Deckt genau die sechs beauftragten Arbeitsabläufe ab:

1. Gesellschaft/Objekt wählen -> Mietkontenübersicht + Kontoauszug.
2. Eröffnungssalden-CSV: Vorschau -> ausdrückliche Bestätigung -> atomarer
   Import (`op/eroeffnung_import.py::importiere_eroeffnung_csv_atomar`).
3. Nachbuchung (SOLL/GUTSCHRIFT) und Korrektur/Storno, mit Vorgangs-ID
   (`import_id`, serverseitige Idempotenz) und Audit-Log.
4. Bankdatei-Import (CSV/CAMT) mit Vorschau, danach getrennt: Zuordnung je
   Transaktion (automatisch vorgeschlagen ODER manuell), sowie
   Bankvollständigkeits-Bestätigung.
5. Vorschreibungsentwurf mit Netto/USt/Brutto-Aufschlüsselung je
   Bestandteil, wirksamer Indexversion und Sperren für historische/
   leerstehende Fälle - getrennt von der eigentlichen Sollstellung
   (Freigabe).
6. Mahnvorschau: reine Planungsansicht (keine Versandfunktion). Bank-
   Vollständigkeit/ungeklärte Eingänge werden IMMER serverseitig aus den
   persistierten Bankdaten abgeleitet, nie aus Formularfeldern übernommen.

Sicherheitsmodell: "closed by default" wie `api/app.py` - ohne
konfigurierten `MIETINKASSO_BACKOFFICE_PASSWORD_HASH` antwortet der
gesamte Router mit 503. Session-Cookie trägt nur eine opake, zufällige
ID (kein JWT, kein API-Token); Zustand lebt ausschließlich serverseitig
(`backoffice/security.py::SessionStore`, EIN Prozess, kein
Mehrbenutzer-Onlinebetrieb). Jede POST-Route verlangt ein gültiges
`csrf_token`-Formularfeld gegen die Session.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from html import escape as h
from urllib.parse import urlparse

from fastapi import APIRouter, Cookie, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from mietinkasso.audit.service import AuditService
from mietinkasso.auth.service import AuthContext
from mietinkasso.backoffice.security import LoginRateLimiter, SessionStore, pruefe_passwort
from mietinkasso.backoffice.views import csrf_feld, eur, flash_error, flash_ok, ist_bekannte_demo_umgebung, option, parse_eur_betrag, seite
from mietinkasso.bank.importer import (
    CamtKontoMismatchError,
    CamtMehrteiligeBuchungError,
    CamtUnvollstaendigError,
    CsvSpaltenMapping,
    parse_camt053,
    parse_csv,
)
from mietinkasso.bank.repository import BankRepository
from mietinkasso.bank.service import BankImportService
from mietinkasso.domain.enums import OPTyp, Rolle
from mietinkasso.domain.exceptions import MietinkassoError
from mietinkasso.index.repository import IndexRepository
from mietinkasso.index.service import UNTERSTUETZTE_BERECHNUNGSPROFILE, IndexService
from mietinkasso.infrastructure.config import get_settings
from mietinkasso.infrastructure.db.session import build_session_factory
from mietinkasso.mahnwesen.repository import MahnFallRepository, MahnPolicyRepository
from mietinkasso.mahnwesen.service import MahnwesenService
from mietinkasso.op.eroeffnung_import import importiere_eroeffnung_csv_atomar, parse_eroeffnung_csv
from mietinkasso.op.repository import OPRepository
from mietinkasso.op.service import OPService
from mietinkasso.stammdaten.repository import StammdatenRepository
from mietinkasso.vertragspruefung.repository import IndexPruefbedarfRepository, VertragPruefungRepository
from mietinkasso.vertragspruefung.service import VertragspruefungService
from mietinkasso.vorschreibung.repository import VorschreibungRepository
from mietinkasso.vorschreibung.service import VorschreibungService, faelligkeitsdatum

router = APIRouter(prefix="/backoffice", tags=["backoffice"])

_settings = get_settings()
# Für Banner-/Titel-Anzeige (views.py::betriebsmodus_banner) UND für die
# Autozuordnungs-Routensperre unten - dieselbe Klassifizierung an BEIDEN
# Stellen (views.py::ist_bekannte_demo_umgebung), damit sie nie
# auseinanderlaufen können. Codex-Rückprüfung (Paket A): NUR bekannte
# Demo-Umgebungen (development/test/ci) gelten als sicher synthetisch -
# jede andere (production, staging, ein Zwischenschritt wie
# "local_realdata_staged") wird als Echtbetrieb behandelt, auch wenn sie
# nicht exakt "production" heißt.
_DEMO_UMGEBUNG = ist_bekannte_demo_umgebung(_settings.environment)
_session_factory = build_session_factory(_settings.database_url)
_stammdaten_repo = StammdatenRepository(_session_factory)
_op_repo = OPRepository(_session_factory)
_op_service = OPService(_op_repo, _stammdaten_repo)
_bank_repo = BankRepository(_session_factory)
_bank_service = BankImportService(_bank_repo, _stammdaten_repo, _op_service)
_vorschreibung_repo = VorschreibungRepository(_session_factory)
_vorschreibung_service = VorschreibungService(_vorschreibung_repo, _stammdaten_repo, _op_service)
_mahn_fall_repo = MahnFallRepository(_session_factory)
_mahn_policy_repo = MahnPolicyRepository(_session_factory)
_mahn_service = MahnwesenService(_mahn_fall_repo, _stammdaten_repo, _op_service, _mahn_policy_repo, bank_stand_max_age_days=_settings.bank_stand_max_age_days)
_index_repo = IndexRepository(_session_factory)
_index_service = IndexService(_index_repo, _stammdaten_repo)
_vertragspruefung_repo = VertragPruefungRepository(_session_factory)
_index_pruefbedarf_repo = IndexPruefbedarfRepository(_session_factory)
_vertragspruefung_service = VertragspruefungService(_vertragspruefung_repo, _index_pruefbedarf_repo, _stammdaten_repo)
_audit_service = AuditService(_session_factory)

_sessions = SessionStore(ttl_sekunden=_settings.backoffice_session_ttl_minuten * 60)
# 5 Fehlversuche innerhalb von 5 Minuten -> 5 Minuten GLOBALE Sperre (siehe
# LoginRateLimiter-Docstring: kein Vertrauen in Proxy-Header, ein Operator).
_login_rate_limiter = LoginRateLimiter(max_versuche=5, fenster_sekunden=300, sperre_sekunden=300)

# `__Host-`-Präfix nur zulässig/sinnvoll mit Secure-Attribut (siehe unten
# `secure=_settings.backoffice_cookie_secure`), ohne Domain-Attribut (wird
# hier nie gesetzt) und mit Path=/ (Default von `Response.set_cookie`) -
# alle drei Bedingungen sind bereits erfüllt, sobald Cookies sicher sind.
# Das Präfix lässt den Browser das Cookie zusätzlich ablehnen, falls es
# jemals versucht würde, es unsicher (HTTP) oder mit einer abweichenden
# Domain zu setzen.
_COOKIE_NAME = "__Host-mietinkasso_biz_session" if _settings.backoffice_cookie_secure else "mietinkasso_biz_session"


def _require_enabled() -> None:
    if not _settings.backoffice_password_hash:
        raise HTTPException(
            status_code=503,
            detail="MIETINKASSO_BACKOFFICE_PASSWORD_HASH ist nicht konfiguriert; das Backoffice bleibt "
            "geschlossen (closed by default), bis ein lokaler Login eingerichtet ist.",
        )


def _redirect_to_login() -> HTTPException:
    return HTTPException(status_code=303, headers={"Location": "/backoffice/login"})


def _current_session(session_cookie: str | None = Cookie(default=None, alias=_COOKIE_NAME)):
    _require_enabled()
    session = _sessions.holen(session_cookie)
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
    if not _sessions.csrf_gueltig(session, csrf_token):
        raise HTTPException(status_code=403, detail="Ungültiges oder fehlendes csrf_token.")


def _layout(request: Request, session, titel: str, inhalt: str) -> HTMLResponse:
    return HTMLResponse(seite(
        titel=titel, inhalt=inhalt, user_id=session.user_id if session else None,
        csrf_token=session.csrf_token if session else None,
        environment=_settings.environment, send_enabled=_settings.send_enabled,
    ))


def _fehlerseite(session, titel: str, meldung: str, zurueck_href: str = "/backoffice/") -> HTMLResponse:
    inhalt = flash_error(meldung) + f'<p><a href="{h(zurueck_href)}">&larr; zurück</a></p>'
    return HTMLResponse(seite(
        titel=titel, inhalt=inhalt, user_id=session.user_id, csrf_token=session.csrf_token,
        environment=_settings.environment, send_enabled=_settings.send_enabled,
    ), status_code=400)


def _ist_objekt_gesperrt(objekt_id: str) -> bool:
    objekt = _stammdaten_repo.get_objekt(objekt_id)
    return objekt is not None and objekt.ausgeschlossen


def _objekt_fuer_vertrag_gesperrt(vertrag_id: str) -> bool:
    try:
        return _stammdaten_repo.objekt_fuer_vertrag(vertrag_id).ausgeschlossen
    except ValueError:
        return False


# -- Login/Logout -----------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
def login_formular(request: Request, fehler: str | None = None) -> HTMLResponse:
    _require_enabled()
    fehler_html = flash_error(fehler) if fehler else ""
    inhalt = f"""
    {fehler_html}
    <div class="card" style="max-width:380px;margin:2rem auto;">
      <h1>Anmeldung</h1>
      <form method="post" action="/backoffice/login">
        <label>Benutzername</label>
        <input type="text" name="username" required autofocus>
        <label>Passwort</label>
        <input type="password" name="password" required>
        <button type="submit">Anmelden</button>
      </form>
    </div>"""
    return HTMLResponse(seite(titel="Anmeldung", inhalt=inhalt, environment=_settings.environment, send_enabled=_settings.send_enabled))


@router.post("/login")
def login_absenden(request: Request, username: str = Form(...), password: str = Form(...)) -> RedirectResponse:
    _require_enabled()
    _pruefe_login_origin(request)
    if _login_rate_limiter.gesperrt():
        return RedirectResponse(
            url="/backoffice/login?fehler=Zu+viele+Fehlversuche.+Bitte+in+einigen+Minuten+erneut+versuchen.",
            status_code=303,
        )
    gueltig = username == _settings.backoffice_user and pruefe_passwort(password, _settings.backoffice_password_hash)
    if not gueltig:
        _login_rate_limiter.fehlversuch_melden()
        return RedirectResponse(url="/backoffice/login?fehler=Benutzername+oder+Passwort+falsch.", status_code=303)
    _login_rate_limiter.erfolgreich_angemeldet()
    session_id, _csrf = _sessions.erstellen(username)
    response = RedirectResponse(url="/backoffice/", status_code=303)
    response.set_cookie(
        _COOKIE_NAME, session_id, httponly=True, samesite="lax", secure=_settings.backoffice_cookie_secure,
        max_age=_settings.backoffice_session_ttl_minuten * 60,
    )
    return response


@router.post("/logout")
def logout(csrf_token: str = Form(...), session_cookie: str | None = Cookie(default=None, alias=_COOKIE_NAME)) -> RedirectResponse:
    session = _sessions.holen(session_cookie)
    if session is not None:
        _verify_csrf(session, csrf_token)
        _sessions.loeschen(session_cookie)
    response = RedirectResponse(url="/backoffice/login", status_code=303)
    response.delete_cookie(_COOKIE_NAME)
    return response


# -- Dashboard: Gesellschaft/Objekt -> Mietkontenübersicht -------------------


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, objekt_id: str | None = None, session=Depends(_current_session)) -> HTMLResponse:
    gesellschaften = _stammdaten_repo.list_gesellschaften()
    options = ['<option value="">-- Objekt wählen --</option>']
    for gesellschaft in gesellschaften:
        objekte = _stammdaten_repo.list_objekte(gesellschaft_id=gesellschaft.id)
        if not objekte:
            continue
        options.append(f'<optgroup label="{h(gesellschaft.name)}">')
        for objekt in objekte:
            label = objekt.bezeichnung + (" [GESPERRT]" if objekt.ausgeschlossen else "")
            options.append(option(objekt.id, label, selected=(objekt.id == objekt_id)))
        options.append("</optgroup>")

    auswahl_form = f"""
    <div class="card">
      <form method="get" action="/backoffice/">
        <label>Objekt</label>
        <select name="objekt_id" onchange="this.form.submit()">{''.join(options)}</select>
        <noscript><button type="submit">Anzeigen</button></noscript>
      </form>
    </div>"""

    tabelle = ""
    if objekt_id:
        objekt = _stammdaten_repo.get_objekt(objekt_id)
        if objekt is None:
            tabelle = flash_error(f"Unbekanntes Objekt {objekt_id}.")
        else:
            gesperrt = objekt.ausgeschlossen
            banner = (
                flash_error(
                    f"Objekt {objekt.id} ({objekt.bezeichnung}) ist von der Pilotphase ausgeschlossen - "
                    "nur Ansicht, KEINE Buchung/Vorschreibung/Mahnung möglich."
                )
                if gesperrt
                else ""
            )
            vertraege = _stammdaten_repo.list_vertraege_fuer_objekt(objekt_id)
            zeilen = []
            for vertrag in vertraege:
                einheit = _stammdaten_repo.get_einheit(vertrag.einheit_id)
                debitor = _stammdaten_repo.get_debitor(vertrag.debitor_id)
                konto = _stammdaten_repo.get_konto_by_vertrag(vertrag.id)
                saldo = _op_service.berechne_saldo(konto.id) if konto else None
                historisch = vertrag.gueltig_bis is not None and vertrag.gueltig_bis < date.today()
                status_tags = []
                if historisch:
                    status_tags.append('<span class="warn">historisch</span>')
                if einheit and einheit.nutzungsstatus == "LEERSTAND":
                    status_tags.append('<span class="warn">Leerstand</span>')
                konto_link = f'<a href="/backoffice/konto/{h(konto.id)}">{h(konto.id)}</a>' if konto else "-"
                zeilen.append(
                    f"<tr class='{'gesperrt-row' if gesperrt else ''}'>"
                    f"<td>{h(vertrag.id)}</td><td>{h(einheit.bezeichnung) if einheit else '-'}</td>"
                    f"<td>{h(einheit.nutzungsstatus) if einheit else '-'}</td>"
                    f"<td>{h(debitor.name) if debitor else '-'}</td>"
                    f"<td>{konto_link}</td>"
                    f"<td>{eur(saldo.saldo_cent) if saldo else '-'}</td>"
                    f"<td>{eur(saldo.faelliger_unstrittiger_rest_cent) if saldo else '-'}</td>"
                    f"<td>{' '.join(status_tags)}</td>"
                    "</tr>"
                )
            einheiten_ohne_vertrag = [
                einheit
                for einheit in _stammdaten_repo.list_einheiten_fuer_objekt(objekt_id)
                if einheit.id not in {v.einheit_id for v in vertraege}
            ]
            bestand_zeilen = "".join(
                f"<tr><td>{h(einheit.id)}</td><td>{h(einheit.bezeichnung)}</td><td>{h(einheit.nutzungsstatus)}</td></tr>"
                for einheit in einheiten_ohne_vertrag
            )
            bestand_tabelle = ""
            if einheiten_ohne_vertrag:
                bestand_tabelle = f"""
                <h2>Einheiten ohne aktiven Vertrag — {h(objekt.bezeichnung)} ({h(objekt.id)})</h2>
                <p class="muted">Nutzungsstatus wird eingespielt/gepflegt, unabhängig davon, ob eine
                   Mietforderung besteht (z. B. Leerstand, Kurzzeitvermietung, Selfstorage,
                   Eigennutzung) - "Leerstand" bedeutet hier den erfassten Status, nicht das
                   Fehlen eines Vertrags per Namens-/Nullsaldo-Vermutung.</p>
                <table>
                  <tr><th>Einheit</th><th>Bezeichnung</th><th>Nutzungsstatus</th></tr>
                  {bestand_zeilen}
                </table>"""

            tabelle = banner + f"""
            <h2>Mietkontenübersicht — {h(objekt.bezeichnung)} ({h(objekt.id)})</h2>
            <table>
              <tr><th>Vertrag</th><th>Einheit</th><th>Nutzungsstatus</th><th>Debitor</th><th>Konto</th>
                  <th>Saldo</th><th>fälliger unstrittiger Rest</th><th>Hinweise</th></tr>
              {''.join(zeilen) if zeilen else '<tr><td colspan=8 class="muted">Keine Verträge.</td></tr>'}
            </table>
            {bestand_tabelle}"""

    return _layout(request, session, "Dashboard", auswahl_form + tabelle)


# -- Kontoauszug --------------------------------------------------------------


def _op_zeile_html(position) -> str:
    faelligkeit_html = position.faelligkeit.isoformat() if position.faelligkeit else '<span class="muted">unbekannt</span>'
    status_html = '<span class="ok">AKTIV</span>' if position.status == "AKTIV" else '<span class="muted">STORNIERT</span>'
    aktion_html = f'<a href="/backoffice/op/{position.id}/korrigieren">Stornieren/Korrigieren</a>' if position.status == "AKTIV" else ""
    return (
        "<tr>"
        f"<td>#{position.id}</td><td>{h(position.typ)}</td>"
        f"<td>{eur(position.betrag_cent)}</td>"
        f"<td>{position.belegdatum.isoformat()}</td>"
        f"<td>{faelligkeit_html}</td>"
        f"<td>{status_html}</td>"
        f"<td>{h(position.beleg_referenz or '')}</td>"
        f"<td>{h(position.aenderungsgrund or '')}</td>"
        f"<td>{aktion_html}</td>"
        "</tr>"
    )


@router.get("/konto/{konto_id}", response_class=HTMLResponse)
def kontoauszug(request: Request, konto_id: str, session=Depends(_current_session)) -> HTMLResponse:
    konto = _stammdaten_repo.get_konto(konto_id)
    if konto is None:
        return _fehlerseite(session, "Kontoauszug", f"Unbekanntes Konto {konto_id}.")
    vertrag = _stammdaten_repo.get_vertrag(konto.vertrag_id)
    einheit = _stammdaten_repo.get_einheit(vertrag.einheit_id) if vertrag else None
    debitor = _stammdaten_repo.get_debitor(konto.debitor_id)
    gesperrt = _objekt_fuer_vertrag_gesperrt(konto.vertrag_id)
    positionen = _op_service.list_alle_positionen(konto_id)
    saldo = _op_service.berechne_saldo(konto_id)
    aktive_sperren = _stammdaten_repo.aktive_sperren(vertrag.id) if vertrag is not None else []

    banner = flash_error("Objekt ist von der Pilotphase ausgeschlossen - nur Ansicht.") if gesperrt else ""
    aktion = "" if gesperrt else f'<p><a href="/backoffice/konto/{h(konto_id)}/buchen">+ Nachbuchung (SOLL/GUTSCHRIFT)</a></p>'

    stammdaten_karte = f"""
    <div class="card">
      <h2>Stammdaten (getrennt geführt)</h2>
      <table>
        <tr><th>Vertrag</th><td>{h(vertrag.id)} — {'<span class="warn">UNGEKLAERT</span>' if vertrag.rechtsordnung == 'UNGEKLAERT' else h(vertrag.rechtsordnung)}, gültig {vertrag.gueltig_von.isoformat()}
            bis {vertrag.gueltig_bis.isoformat() if vertrag.gueltig_bis else 'unbefristet'}
            {'<br><span class="warn">Rechtsordnung ungeklärt — Mahnung/Index/Sollstellung gesperrt.</span>' if vertrag.rechtsordnung == 'UNGEKLAERT' else ''}</td></tr>
        <tr><th>Einheit</th><td>{h(einheit.bezeichnung) if einheit else '-'} — Nutzungsstatus:
            <strong>{h(einheit.nutzungsstatus) if einheit else '-'}</strong></td></tr>
        <tr><th>Debitor</th><td>{h(debitor.name) if debitor else '-'} ({h(debitor.email) if debitor and debitor.email else 'keine E-Mail hinterlegt'})</td></tr>
        <tr><th>Eröffnungsmodus</th><td>{h(konto.eroeffnung_modus or '-')}
            {'zum ' + konto.eroeffnung_stichtag.isoformat() if konto.eroeffnung_stichtag else ''}</td></tr>
      </table>
    </div>"""

    sperren_zeilen = "".join(
        f"<tr><td>{h(s.grund)}</td><td>{s.gesetzt_am.isoformat() if s.gesetzt_am else ''}</td>"
        f"<td>{h(s.kommentar or '')}</td></tr>"
        for s in aktive_sperren
    )
    sperren_karte = ""
    if aktive_sperren:
        sperren_karte = f"""
    <div class="card">
      <h2 class="error">Aktive Sperre(n) — blockiert Mahnung</h2>
      <table>
        <tr><th>Grund</th><th>Gesetzt am</th><th>Kommentar</th></tr>
        {sperren_zeilen}
      </table>
    </div>"""

    zeilen = "".join(_op_zeile_html(p) for p in positionen)
    op_tabelle = f"""
    <div class="card">
      <h2>Kontoauszug — {h(konto_id)}</h2>
      <p>Saldo: <strong>{eur(saldo.saldo_cent)}</strong> &nbsp;|&nbsp; fälliger unstrittiger Rest:
         <strong>{eur(saldo.faelliger_unstrittiger_rest_cent)}</strong></p>
      {aktion}
      <table>
        <tr><th>#</th><th>Typ</th><th>Betrag</th><th>Belegdatum</th><th>Fälligkeit</th><th>Status</th>
            <th>Beleg-Referenz</th><th>Änderungsgrund</th><th></th></tr>
        {zeilen if positionen else '<tr><td colspan=9 class="muted">Keine Buchungen.</td></tr>'}
      </table>
      <p class="muted">Zeilen ohne bekannte Fälligkeit werden nie automatisch gemahnt (siehe Mahnvorschau).</p>
    </div>"""

    links = ""
    if not gesperrt and vertrag is not None:
        links = f"""
        <p>
          <a href="/backoffice/vertrag/{h(vertrag.id)}/vorschreibung">Vorschreibungsentwurf</a> &nbsp;|&nbsp;
          <a href="/backoffice/vertrag/{h(vertrag.id)}/mahnvorschau">Mahnvorschau</a> &nbsp;|&nbsp;
          <a href="/backoffice/vertrag/{h(vertrag.id)}/pruefung">Vertragsprüfung</a>
        </p>"""

    return _layout(request, session, f"Kontoauszug {konto_id}", banner + stammdaten_karte + sperren_karte + op_tabelle + links)


# -- Nachbuchung ----------------------------------------------------------------


@router.get("/konto/{konto_id}/buchen", response_class=HTMLResponse)
def nachbuchung_formular(request: Request, konto_id: str, session=Depends(_current_session)) -> HTMLResponse:
    konto = _stammdaten_repo.get_konto(konto_id)
    if konto is None:
        return _fehlerseite(session, "Nachbuchung", f"Unbekanntes Konto {konto_id}.")
    if _objekt_fuer_vertrag_gesperrt(konto.vertrag_id):
        return _fehlerseite(session, "Nachbuchung", "Objekt ist gesperrt; keine Buchung möglich.", f"/backoffice/konto/{konto_id}")
    inhalt = f"""
    <div class="card" style="max-width:520px;">
      <h1>Nachbuchung — Konto {h(konto_id)}</h1>
      <form method="post" action="/backoffice/konto/{h(konto_id)}/buchen">
        {csrf_feld(session.csrf_token)}
        <label>Typ</label>
        <select name="typ" required>
          <option value="SOLL">SOLL (Nachbelastung)</option>
          <option value="GUTSCHRIFT">GUTSCHRIFT</option>
        </select>
        <label>Betrag (EUR)</label>
        <input type="text" name="betrag" placeholder="z. B. 123,45" required>
        <label>Belegdatum</label>
        <input type="date" name="belegdatum" value="{date.today().isoformat()}" required>
        <label>Fälligkeit (leer = unbekannt, wird nie automatisch gemahnt)</label>
        <input type="date" name="faelligkeit">
        <label>Beleg-Referenz</label>
        <input type="text" name="beleg_referenz" required>
        <label>Grund/Begründung</label>
        <input type="text" name="grund" required>
        <label>Vorgangs-ID (eindeutig; ein Retry mit derselben ID ist ein sicherer No-Op)</label>
        <input type="text" name="vorgangs_id" required>
        <button type="submit">Buchen</button>
      </form>
    </div>"""
    return _layout(request, session, "Nachbuchung", inhalt)


@router.post("/konto/{konto_id}/buchen")
def nachbuchung_absenden(
    request: Request,
    konto_id: str,
    csrf_token: str = Form(...),
    typ: str = Form(...),
    betrag: str = Form(...),
    belegdatum: date = Form(...),
    faelligkeit: str | None = Form(None),
    beleg_referenz: str = Form(...),
    grund: str = Form(...),
    vorgangs_id: str = Form(...),
    session=Depends(_current_session),
):
    _verify_csrf(session, csrf_token)
    konto = _stammdaten_repo.get_konto(konto_id)
    if konto is None:
        return _fehlerseite(session, "Nachbuchung", f"Unbekanntes Konto {konto_id}.")
    try:
        op_typ = OPTyp(typ)
        if op_typ not in (OPTyp.SOLL, OPTyp.GUTSCHRIFT):
            raise ValueError("Nur SOLL/GUTSCHRIFT sind über die manuelle Nachbuchung zulässig.")
        betrag_cent = parse_eur_betrag(betrag)
        faelligkeit_datum = date.fromisoformat(faelligkeit) if faelligkeit else None
        position = _op_service.buchen(
            ctx=_ctx(session), konto=konto, typ=op_typ, betrag_cent=betrag_cent, belegdatum=belegdatum,
            buchungsdatum=date.today(), faelligkeit=faelligkeit_datum, beleg_referenz=beleg_referenz,
            aenderungsgrund=grund, import_id=f"BACKOFFICE-BUCHUNG-{vorgangs_id}", quelle_system="backoffice",
        )
        _audit_service.log(
            entity_typ="op_position", entity_id=str(position.id), aktion="nachbuchung", akteur=session.user_id,
            payload={"konto_id": konto_id, "typ": typ, "betrag_cent": betrag_cent, "vorgangs_id": vorgangs_id, "grund": grund},
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Nachbuchung", str(exc), f"/backoffice/konto/{konto_id}/buchen")
    return RedirectResponse(url=f"/backoffice/konto/{konto_id}?gebucht=1", status_code=303)


# -- Korrektur/Storno -----------------------------------------------------------


@router.get("/op/{op_id}/korrigieren", response_class=HTMLResponse)
def korrektur_formular(request: Request, op_id: int, session=Depends(_current_session)) -> HTMLResponse:
    position = _op_service.get_position(op_id)
    if position is None:
        return _fehlerseite(session, "Korrektur", f"Unbekannte OP-Position {op_id}.")
    konto = _stammdaten_repo.get_konto(position.konto_id)
    if konto is not None and _objekt_fuer_vertrag_gesperrt(konto.vertrag_id):
        return _fehlerseite(session, "Korrektur", "Objekt ist gesperrt; keine Korrektur möglich.", f"/backoffice/konto/{position.konto_id}")
    vorschlag_vorgang_id = f"KORREKTUR-{op_id}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    inhalt = f"""
    <div class="card" style="max-width:520px;">
      <h1>Stornieren/Korrigieren — OP #{op_id}</h1>
      <table>
        <tr><th>Typ</th><td>{h(position.typ)}</td></tr>
        <tr><th>Betrag</th><td>{eur(position.betrag_cent)}</td></tr>
        <tr><th>Belegdatum</th><td>{position.belegdatum.isoformat()}</td></tr>
        <tr><th>Beleg-Referenz</th><td>{h(position.beleg_referenz or '')}</td></tr>
      </table>
      <form method="post" action="/backoffice/op/{op_id}/korrigieren">
        {csrf_feld(session.csrf_token)}
        <label>Grund (Pflicht)</label>
        <input type="text" name="grund" required>
        <label>Neuer Betrag (EUR, leer = reines Storno ohne Ersatzbuchung)</label>
        <input type="text" name="neuer_betrag" placeholder="z. B. 100,00">
        <label>Neue Fälligkeit (leer = unverändert übernehmen)</label>
        <input type="date" name="neue_faelligkeit">
        <label>Vorgangs-ID (eindeutig; eine doppelte Formularbestätigung mit derselben ID bleibt ein sicherer No-Op)</label>
        <input type="text" name="vorgangs_id" value="{h(vorschlag_vorgang_id)}" required>
        <button type="submit">Bestätigen</button>
      </form>
    </div>"""
    return _layout(request, session, "Korrektur", inhalt)


@router.post("/op/{op_id}/korrigieren")
def korrektur_absenden(
    request: Request,
    op_id: int,
    csrf_token: str = Form(...),
    grund: str = Form(...),
    neuer_betrag: str | None = Form(None),
    neue_faelligkeit: str | None = Form(None),
    vorgangs_id: str = Form(...),
    session=Depends(_current_session),
):
    _verify_csrf(session, csrf_token)
    position = _op_service.get_position(op_id)
    if position is None:
        return _fehlerseite(session, "Korrektur", f"Unbekannte OP-Position {op_id}.")
    konto = _stammdaten_repo.get_konto(position.konto_id)
    if konto is None:
        return _fehlerseite(session, "Korrektur", f"Konto {position.konto_id} nicht gefunden.")
    try:
        neuer_betrag_cent = parse_eur_betrag(neuer_betrag) if neuer_betrag and neuer_betrag.strip() else None
        neue_faelligkeit_datum = date.fromisoformat(neue_faelligkeit) if neue_faelligkeit else None
        ergebnis = _op_service.storniere_und_korrigiere(
            ctx=_ctx(session), konto=konto, original_id=op_id, aenderungsgrund=grund,
            neuer_betrag_cent=neuer_betrag_cent, neue_faelligkeit=neue_faelligkeit_datum, vorgang_id=vorgangs_id,
        )
        _audit_service.log(
            entity_typ="op_position", entity_id=str(op_id), aktion="storno_korrektur", akteur=session.user_id,
            payload={
                "grund": grund, "neuer_betrag_cent": neuer_betrag_cent, "vorgangs_id": vorgangs_id,
                "ersatz_id": ergebnis.id if ergebnis else None,
            },
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Korrektur", str(exc), f"/backoffice/op/{op_id}/korrigieren")
    return RedirectResponse(url=f"/backoffice/konto/{position.konto_id}?korrigiert=1", status_code=303)


# -- Eröffnungssalden CSV: Vorschau -> Bestätigung -> atomarer Import -----------


def _eroeffnung_zeilen_pruefen(text: str) -> tuple[list[dict], bool]:
    """Reine Lesevorschau (kein Schreibzugriff): parst und bewertet jede
    Zeile gegen den PERSISTIERTEN Stand. `alles_ok` ist nur True, wenn
    JEDE Zeile blockierfrei ist - erst dann darf überhaupt verbucht
    werden (keine Teilbuchung bei späterer Fehlerzeile)."""

    ergebnisse = []
    alles_ok = True
    try:
        zeilen = parse_eroeffnung_csv(text)
    except Exception as exc:  # Parsing-Fehler (Format, Datum, Betrag, ...)
        return [{"fehler": f"Datei nicht lesbar: {exc}"}], False
    if not zeilen:
        return [{"fehler": "Datei enthält keine Zeilen."}], False
    for zeile in zeilen:
        eintrag: dict = {
            "konto_id": zeile.konto_id, "modus": zeile.modus, "betrag_cent": zeile.betrag_cent,
            "stichtag": zeile.stichtag, "import_id": zeile.import_id, "hinweise": [], "blockiert": False,
        }
        konto = _stammdaten_repo.get_konto(zeile.konto_id)
        if konto is None:
            eintrag["hinweise"].append("UNBEKANNTES KONTO")
            eintrag["blockiert"] = True
            ergebnisse.append(eintrag)
            alles_ok = False
            continue
        if _objekt_fuer_vertrag_gesperrt(konto.vertrag_id):
            eintrag["hinweise"].append("OBJEKT GESPERRT (Pilotausschluss)")
            eintrag["blockiert"] = True
        if konto.eroeffnung_modus is not None and konto.eroeffnung_modus != zeile.modus:
            eintrag["hinweise"].append(f"KONFLIKT: Konto bereits mit Modus {konto.eroeffnung_modus} eröffnet")
            eintrag["blockiert"] = True
        if zeile.modus == "GESAMTSALDO":
            bestehende = _op_service.bestehende_eroeffnung(konto.id)
            if bestehende is not None:
                if bestehende.betrag_cent == zeile.betrag_cent and bestehende.belegdatum == zeile.stichtag:
                    eintrag["hinweise"].append("Replay (bereits gebucht, wirkungslos)")
                else:
                    eintrag["hinweise"].append(
                        f"KONFLIKT: bereits mit {bestehende.betrag_cent} Cent zum {bestehende.belegdatum} eröffnet"
                    )
                    eintrag["blockiert"] = True
        elif zeile.modus != "EINZEL_OP":
            eintrag["hinweise"].append(f"Unbekannter Modus '{zeile.modus}'")
            eintrag["blockiert"] = True
        if zeile.betrag_cent < 0:
            eintrag["hinweise"].append("Guthaben (negativer Saldo)")
        if eintrag["blockiert"]:
            alles_ok = False
        ergebnisse.append(eintrag)
    return ergebnisse, alles_ok


@router.get("/eroeffnung", response_class=HTMLResponse)
def eroeffnung_formular(request: Request, session=Depends(_current_session)) -> HTMLResponse:
    inhalt = f"""
    <div class="card" style="max-width:640px;">
      <h1>Eröffnungssalden-Import</h1>
      <p class="muted">Erwartete Spalten: <code>konto_id,modus,betrag,stichtag,import_id,beleg_referenz</code>
         (modus = GESAMTSALDO oder EINZEL_OP). Die gesamte Datei wird als EINE Transaktion geprüft und verbucht.</p>
      <form method="post" action="/backoffice/eroeffnung/vorschau" enctype="multipart/form-data">
        {csrf_feld(session.csrf_token)}
        <label>CSV-Datei</label>
        <input type="file" name="datei" accept=".csv,text/csv" required>
        <button type="submit">Vorschau anzeigen</button>
      </form>
    </div>"""
    return _layout(request, session, "Eröffnungssalden-Import", inhalt)


@router.post("/eroeffnung/vorschau", response_class=HTMLResponse)
async def eroeffnung_vorschau(request: Request, datei: UploadFile = File(...), csrf_token: str = Form(...), session=Depends(_current_session)) -> HTMLResponse:
    _verify_csrf(session, csrf_token)
    rohbytes = await datei.read()
    text = rohbytes.decode("utf-8-sig")
    zeilen, alles_ok = _eroeffnung_zeilen_pruefen(text)

    zeilen_html = []
    for z in zeilen:
        if "fehler" in z:
            zeilen_html.append(f'<tr class="gesperrt-row"><td colspan=6>{h(z["fehler"])}</td></tr>')
            continue
        klasse = "gesperrt-row" if z["blockiert"] else ""
        hinweise_html = "; ".join(h(x) for x in z["hinweise"]) or '<span class="ok">ok</span>'
        zeilen_html.append(
            f"<tr class='{klasse}'><td>{h(z['konto_id'])}</td><td>{h(z['modus'])}</td>"
            f"<td>{eur(z['betrag_cent'])}</td><td>{z['stichtag'].isoformat()}</td>"
            f"<td>{h(z['import_id'])}</td><td>{hinweise_html}</td></tr>"
        )

    bestaetigen = ""
    if alles_ok:
        bestaetigen = f"""
        <form method="post" action="/backoffice/eroeffnung/verbuchen">
          {csrf_feld(session.csrf_token)}
          <textarea name="datei_inhalt" hidden>{h(text)}</textarea>
          <button type="submit">Jetzt atomar verbuchen ({len(zeilen)} Zeile(n))</button>
        </form>"""
    else:
        bestaetigen = flash_error("Datei enthält blockierende Zeilen (siehe oben, rot markiert). Erst korrigieren und erneut hochladen - es wird NICHTS verbucht.")

    inhalt = f"""
    <div class="card">
      <h1>Vorschau — Eröffnungssalden</h1>
      <table>
        <tr><th>Konto</th><th>Modus</th><th>Betrag</th><th>Stichtag</th><th>Import-ID</th><th>Hinweise</th></tr>
        {''.join(zeilen_html)}
      </table>
      {bestaetigen}
      <p><a href="/backoffice/eroeffnung">&larr; andere Datei wählen</a></p>
    </div>"""
    return _layout(request, session, "Vorschau Eröffnungssalden", inhalt)


@router.post("/eroeffnung/verbuchen", response_class=HTMLResponse)
def eroeffnung_verbuchen(request: Request, datei_inhalt: str = Form(...), csrf_token: str = Form(...), session=Depends(_current_session)) -> HTMLResponse:
    _verify_csrf(session, csrf_token)
    # Serverseitige Re-Validierung des resubmitteten Inhalts - eine
    # clientseitig behauptete "alles ok"-Vorschau wird NIE blind
    # übernommen; erst hier, unmittelbar vor dem Verbuchen, entscheidet
    # der frisch berechnete Stand.
    zeilen, alles_ok = _eroeffnung_zeilen_pruefen(datei_inhalt)
    if not alles_ok:
        return _fehlerseite(
            session, "Eröffnungsimport",
            "Die Datei hat sich seit der Vorschau geändert oder enthält blockierende Zeilen; nichts wurde verbucht.",
            "/backoffice/eroeffnung",
        )
    konten_je_id = {z["konto_id"]: _stammdaten_repo.get_konto(z["konto_id"]) for z in zeilen if "konto_id" in z}
    try:
        ergebnisse = importiere_eroeffnung_csv_atomar(
            ctx=_ctx(session), op_service=_op_service, text=datei_inhalt, konten_je_id=konten_je_id,
            akteur=session.user_id, session_factory=_session_factory,
        )
        _audit_service.log(
            entity_typ="eroeffnung_import", entity_id=f"{len(ergebnisse)}-zeilen", aktion="atomar_verbucht",
            akteur=session.user_id, payload={"anzahl": len(ergebnisse), "konten": sorted(konten_je_id)},
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Eröffnungsimport", f"Import abgebrochen, NICHTS wurde verbucht: {exc}", "/backoffice/eroeffnung")
    inhalt = flash_ok(f"{len(ergebnisse)} Eröffnungszeile(n) atomar verbucht.") + '<p><a href="/backoffice/">&larr; zum Dashboard</a></p>'
    return _layout(request, session, "Eröffnungsimport erfolgreich", inhalt)


# -- Bankimport + Zuordnung ------------------------------------------------------


@router.get("/bank", response_class=HTMLResponse)
def bank_formular(request: Request, session=Depends(_current_session)) -> HTMLResponse:
    bank_konten = _bank_repo.list_bank_konten()
    options = "".join(option(bk.id, f"{bk.bezeichnung} ({bk.gesellschaft_id}, {bk.iban})") for bk in bank_konten)
    inhalt = f"""
    <div class="card" style="max-width:640px;">
      <h1>Bankdatei-Import</h1>
      <form method="post" action="/backoffice/bank/vorschau" enctype="multipart/form-data">
        {csrf_feld(session.csrf_token)}
        <label>Bankkonto</label>
        <select name="bank_konto_id" required>{options}</select>
        <label>Format</label>
        <select name="format" required>
          <option value="CSV">CSV</option>
          <option value="CAMT">CAMT.053 (XML)</option>
        </select>
        <label>Datei</label>
        <input type="file" name="datei" required>
        <fieldset>
          <legend>CSV-Spaltennamen (nur relevant bei Format CSV)</legend>
          <label>Betrag-Spalte</label><input type="text" name="spalte_betrag" value="betrag">
          <label>Datum-Spalte</label><input type="text" name="spalte_datum" value="datum">
          <label>Referenz-Spalte</label><input type="text" name="spalte_referenz" value="referenz">
          <label>Eindeutige-Referenz-Spalte (optional)</label><input type="text" name="spalte_eindeutig" value="">
          <label>Dezimaltrennzeichen</label>
          <select name="dezimaltrennzeichen"><option value=".">Punkt (1234.56)</option><option value=",">Komma (1234,56)</option></select>
        </fieldset>
        <button type="submit">Vorschau anzeigen</button>
      </form>
    </div>
    <p><a href="/backoffice/bank/unzugeordnet">Offene Zuordnungen ansehen</a> &nbsp;|&nbsp;
       <a href="/backoffice/bank/vollstaendigkeit">Bankvollständigkeit bestätigen</a></p>"""
    return _layout(request, session, "Bankdatei-Import", inhalt)


def _bank_rohdaten_parsen(*, format_: str, inhalt_bytes: bytes, mapping_felder: dict, bank_konto_iban: str) -> list:
    if format_ == "CAMT":
        return parse_camt053(inhalt_bytes, erwartete_iban=bank_konto_iban)
    mapping = CsvSpaltenMapping(
        betrag=mapping_felder["spalte_betrag"], buchungsdatum=mapping_felder["spalte_datum"],
        referenz=mapping_felder["spalte_referenz"] or None,
        eindeutige_referenz=mapping_felder["spalte_eindeutig"] or None,
        dezimaltrennzeichen=mapping_felder["dezimaltrennzeichen"],
    )
    return parse_csv(inhalt_bytes.decode("utf-8-sig"), mapping)


@router.post("/bank/vorschau", response_class=HTMLResponse)
async def bank_vorschau(
    request: Request,
    bank_konto_id: str = Form(...),
    format: str = Form(...),
    datei: UploadFile = File(...),
    spalte_betrag: str = Form("betrag"),
    spalte_datum: str = Form("datum"),
    spalte_referenz: str = Form("referenz"),
    spalte_eindeutig: str = Form(""),
    dezimaltrennzeichen: str = Form("."),
    csrf_token: str = Form(...),
    session=Depends(_current_session),
) -> HTMLResponse:
    _verify_csrf(session, csrf_token)
    bank_konto = _bank_repo.get_bank_konto(bank_konto_id)
    if bank_konto is None:
        return _fehlerseite(session, "Bankvorschau", f"Unbekanntes Bankkonto {bank_konto_id}.", "/backoffice/bank")
    inhalt_bytes = await datei.read()
    mapping_felder = {
        "spalte_betrag": spalte_betrag, "spalte_datum": spalte_datum, "spalte_referenz": spalte_referenz,
        "spalte_eindeutig": spalte_eindeutig, "dezimaltrennzeichen": dezimaltrennzeichen,
    }
    try:
        rohdaten = _bank_rohdaten_parsen(
            format_=format, inhalt_bytes=inhalt_bytes, mapping_felder=mapping_felder, bank_konto_iban=bank_konto.iban,
        )
    except (MietinkassoError, ValueError, CamtUnvollstaendigError, CamtMehrteiligeBuchungError, CamtKontoMismatchError, KeyError) as exc:
        return _fehlerseite(session, "Bankvorschau", f"Datei nicht importierbar: {exc}", "/backoffice/bank")
    if not rohdaten:
        return _fehlerseite(session, "Bankvorschau", "Datei enthält keine Zeilen.", "/backoffice/bank")

    zeitraum_von = min(r.buchungsdatum for r in rohdaten)
    zeitraum_bis = max(r.buchungsdatum for r in rohdaten)
    vorschlaege = _bank_service.vorschau_zuordnungsvorschlaege(rohdaten)

    zeilen_html = []
    unklare = 0
    for eintrag in vorschlaege:
        roh = eintrag.roh
        unklar = eintrag.vorgeschlagenes_konto_id is None and roh.betrag_cent > 0
        if unklar:
            unklare += 1
        klasse = "gesperrt-row" if unklar else ""
        zeilen_html.append(
            f"<tr class='{klasse}'>"
            f"<td>{eur(roh.betrag_cent)}</td><td>{roh.buchungsdatum.isoformat()}</td>"
            f"<td>{h(roh.referenz or '')}</td><td>{'ja' if roh.native_id else 'nein'}</td>"
            f"<td>{h(eintrag.vorgeschlagenes_konto_id or '-')}</td><td>{h(eintrag.grund)}</td></tr>"
        )

    import base64

    versteckte_datei = base64.b64encode(inhalt_bytes).decode("ascii")
    inhalt = f"""
    <div class="card">
      <h1>Vorschau — Bankdatei</h1>
      <table>
        <tr><th>Bankkonto</th><td>{h(bank_konto.bezeichnung)} ({h(bank_konto.gesellschaft_id)}, {h(bank_konto.iban)})</td></tr>
        <tr><th>Zeitraum</th><td>{zeitraum_von.isoformat()} bis {zeitraum_bis.isoformat()}</td></tr>
        <tr><th>Zeilen</th><td>{len(rohdaten)} (davon {unklare} ohne eindeutigen Zuordnungsvorschlag)</td></tr>
      </table>
      <table>
        <tr><th>Betrag</th><th>Datum</th><th>Referenz</th><th>eindeutige ID?</th><th>Vorschlag Konto</th><th>Begründung</th></tr>
        {''.join(zeilen_html)}
      </table>
      <p class="muted">Dieser Import legt nur die Bankbewegungen ab; die eigentliche Zuordnung gegen ein
         Mietkonto erfolgt danach als separater, ausdrücklicher Schritt je Transaktion.</p>
      <form method="post" action="/backoffice/bank/importieren">
        {csrf_feld(session.csrf_token)}
        <input type="hidden" name="bank_konto_id" value="{h(bank_konto_id)}">
        <input type="hidden" name="format" value="{h(format)}">
        <input type="hidden" name="inhalt_b64" value="{versteckte_datei}">
        <input type="hidden" name="spalte_betrag" value="{h(spalte_betrag)}">
        <input type="hidden" name="spalte_datum" value="{h(spalte_datum)}">
        <input type="hidden" name="spalte_referenz" value="{h(spalte_referenz)}">
        <input type="hidden" name="spalte_eindeutig" value="{h(spalte_eindeutig)}">
        <input type="hidden" name="dezimaltrennzeichen" value="{h(dezimaltrennzeichen)}">
        <button type="submit">Datei importieren (Bankbewegungen ablegen)</button>
      </form>
      <p><a href="/backoffice/bank">&larr; andere Datei wählen</a></p>
    </div>"""
    return _layout(request, session, "Vorschau Bankdatei", inhalt)


@router.post("/bank/importieren", response_class=HTMLResponse)
def bank_importieren(
    request: Request,
    bank_konto_id: str = Form(...),
    format: str = Form(...),
    inhalt_b64: str = Form(...),
    spalte_betrag: str = Form("betrag"),
    spalte_datum: str = Form("datum"),
    spalte_referenz: str = Form("referenz"),
    spalte_eindeutig: str = Form(""),
    dezimaltrennzeichen: str = Form("."),
    csrf_token: str = Form(...),
    session=Depends(_current_session),
) -> HTMLResponse:
    _verify_csrf(session, csrf_token)
    import base64

    bank_konto = _bank_repo.get_bank_konto(bank_konto_id)
    if bank_konto is None:
        return _fehlerseite(session, "Bankimport", f"Unbekanntes Bankkonto {bank_konto_id}.", "/backoffice/bank")
    inhalt_bytes = base64.b64decode(inhalt_b64)
    try:
        if format == "CAMT":
            transaktionen = _bank_service.importiere_camt053(ctx=_ctx(session), bank_konto=bank_konto, xml_bytes=inhalt_bytes)
        else:
            mapping = CsvSpaltenMapping(
                betrag=spalte_betrag, buchungsdatum=spalte_datum, referenz=spalte_referenz or None,
                eindeutige_referenz=spalte_eindeutig or None, dezimaltrennzeichen=dezimaltrennzeichen,
            )
            transaktionen = _bank_service.importiere_csv(
                ctx=_ctx(session), bank_konto=bank_konto, text=inhalt_bytes.decode("utf-8-sig"), mapping=mapping,
            )
        _audit_service.log(
            entity_typ="bank_import", entity_id=bank_konto_id, aktion="importiert", akteur=session.user_id,
            payload={"anzahl": len(transaktionen), "format": format},
        )
    except (MietinkassoError, ValueError, CamtUnvollstaendigError, CamtMehrteiligeBuchungError, CamtKontoMismatchError) as exc:
        return _fehlerseite(session, "Bankimport", f"Import abgebrochen, NICHTS wurde übernommen: {exc}", "/backoffice/bank")
    inhalt = (
        flash_ok(f"{len(transaktionen)} Bankbewegung(en) importiert (noch nicht zugeordnet).")
        + f'<p><a href="/backoffice/bank/unzugeordnet?bank_konto_id={h(bank_konto_id)}">Jetzt zuordnen &rarr;</a></p>'
    )
    return _layout(request, session, "Bankimport erfolgreich", inhalt)


@router.get("/bank/unzugeordnet", response_class=HTMLResponse)
def bank_unzugeordnet(request: Request, bank_konto_id: str | None = None, session=Depends(_current_session)) -> HTMLResponse:
    bank_konten = _bank_repo.list_bank_konten()
    options = "".join(option(bk.id, bk.bezeichnung, selected=(bk.id == bank_konto_id)) for bk in bank_konten)
    auswahl = f"""
    <form method="get" action="/backoffice/bank/unzugeordnet">
      <label>Bankkonto</label>
      <select name="bank_konto_id" onchange="this.form.submit()">
        <option value="">-- wählen --</option>{options}
      </select>
      <noscript><button type="submit">Anzeigen</button></noscript>
    </form>"""

    tabelle = ""
    if bank_konto_id:
        transaktionen = _bank_repo.list_unzugeordnet(bank_konto_id)
        gesellschaft_id = _bank_repo.get_bank_konto(bank_konto_id).gesellschaft_id
        objekte = _stammdaten_repo.list_objekte(gesellschaft_id=gesellschaft_id)
        konten_optionen = []
        for objekt in objekte:
            if objekt.ausgeschlossen:
                continue
            for vertrag in _stammdaten_repo.list_vertraege_fuer_objekt(objekt.id):
                konto = _stammdaten_repo.get_konto_by_vertrag(vertrag.id)
                if konto:
                    konten_optionen.append(option(konto.id, f"{konto.id} ({objekt.bezeichnung})"))
        konten_select = "".join(konten_optionen)

        zeilen = []
        for tx in transaktionen:
            zugeordnet = _bank_repo.zugeordneter_betrag(tx.id)
            rest = tx.betrag_cent - zugeordnet
            vorschlag_form = ""
            # Automatische Zuordnung ist nutzerseitig zurückgestellt (bis
            # EBS/EBICS) - außerhalb bekannter Demo-Umgebungen weder
            # Vorschlagstext noch Schaltfläche anzeigen. Die Anzeige allein
            # wäre KEIN Schutz - die POST-Route selbst verweigert die
            # Ausführung ebenfalls (siehe bank_automatisch_zuordnen unten).
            vorschlag_grund_html = ""
            if _DEMO_UMGEBUNG:
                vorschlag_konto, vorschlag_grund = _bank_service.schlage_konto_vor(tx)
                vorschlag_grund_html = h(vorschlag_grund)
                if vorschlag_konto is not None:
                    vorschlag_form = f"""
                    <form method="post" action="/backoffice/bank/{tx.id}/automatisch-zuordnen" class="inline">
                      {csrf_feld(session.csrf_token)}
                      <button type="submit">Vorschlag übernehmen ({h(vorschlag_konto.id)})</button>
                    </form>"""
            else:
                vorschlag_grund_html = '<span class="muted">Automatische Zuordnung zurückgestellt (EBS/EBICS ausstehend).</span>'
            zeilen.append(f"""
            <tr>
              <td>#{tx.id}</td><td>{eur(tx.betrag_cent)}</td><td>{tx.buchungsdatum.isoformat()}</td>
              <td>{h(tx.referenz or '')}</td><td>{eur(rest)} offen</td>
              <td>{vorschlag_grund_html}{vorschlag_form}</td>
              <td>
                <form method="post" action="/backoffice/bank/{tx.id}/manuell-zuordnen">
                  {csrf_feld(session.csrf_token)}
                  <select name="konto_id" required><option value="">Konto wählen</option>{konten_select}</select>
                  <input type="text" name="betrag" placeholder="Betrag EUR" value="{eur(rest).split()[0]}" required>
                  <input type="text" name="vorgangs_id" placeholder="Vorgangs-ID" value="MANUELL-{tx.id}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}" required>
                  <button type="submit">Manuell zuordnen</button>
                </form>
                <a href="/backoffice/bank/{tx.id}/verknuepfen">Mit bestehender Zahlung verknüpfen</a>
              </td>
            </tr>""")
        tabelle = f"""
        <table>
          <tr><th>#</th><th>Betrag</th><th>Datum</th><th>Referenz</th><th>Offen</th><th>Vorschlag</th><th>Manuell</th></tr>
          {''.join(zeilen) if zeilen else '<tr><td colspan=7 class="muted">Keine unzugeordneten Transaktionen.</td></tr>'}
        </table>"""

    inhalt = f'<div class="card"><h1>Offene Zuordnungen</h1>{auswahl}{tabelle}</div>'
    return _layout(request, session, "Offene Zuordnungen", inhalt)


@router.post("/bank/{transaktion_id}/automatisch-zuordnen")
def bank_automatisch_zuordnen(request: Request, transaktion_id: int, csrf_token: str = Form(...), session=Depends(_current_session)):
    _verify_csrf(session, csrf_token)
    if not _DEMO_UMGEBUNG:
        # Nutzerseitig zurückgestellt (bis EBS/EBICS) - die Route führt in
        # jeder NICHT bekannten Demo-Umgebung NICHTS aus, unabhängig davon,
        # ob im UI eine Schaltfläche dafür sichtbar war (die Anzeige allein
        # wäre kein Schutz gegen einen direkten POST).
        raise HTTPException(
            status_code=403,
            detail="Automatische Bankzuordnung ist zurückgestellt (EBS/EBICS ausstehend) und in dieser "
            "Umgebung deaktiviert.",
        )
    transaktion = _bank_repo.get_transaktion(transaktion_id)
    if transaktion is None:
        return _fehlerseite(session, "Zuordnung", f"Unbekannte Transaktion {transaktion_id}.", "/backoffice/bank/unzugeordnet")
    try:
        ergebnis = _bank_service.automatisch_zuordnen(ctx=_ctx(session), transaktion=transaktion)
        if not ergebnis.zugeordnet:
            return _fehlerseite(session, "Zuordnung", ergebnis.grund, f"/backoffice/bank/unzugeordnet?bank_konto_id={transaktion.bank_konto_id}")
        _audit_service.log(
            entity_typ="zuordnung", entity_id=str(ergebnis.zuordnung_id), aktion="automatisch_zugeordnet",
            akteur=session.user_id, payload={"transaktion_id": transaktion_id, "op_position_id": ergebnis.op_position_id},
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Zuordnung", str(exc), f"/backoffice/bank/unzugeordnet?bank_konto_id={transaktion.bank_konto_id}")
    return RedirectResponse(url=f"/backoffice/bank/unzugeordnet?bank_konto_id={transaktion.bank_konto_id}&zugeordnet=1", status_code=303)


@router.post("/bank/{transaktion_id}/manuell-zuordnen")
def bank_manuell_zuordnen(
    request: Request,
    transaktion_id: int,
    konto_id: str = Form(...),
    betrag: str = Form(...),
    vorgangs_id: str = Form(...),
    csrf_token: str = Form(...),
    session=Depends(_current_session),
):
    _verify_csrf(session, csrf_token)
    transaktion = _bank_repo.get_transaktion(transaktion_id)
    if transaktion is None:
        return _fehlerseite(session, "Zuordnung", f"Unbekannte Transaktion {transaktion_id}.", "/backoffice/bank/unzugeordnet")
    konto = _stammdaten_repo.get_konto(konto_id)
    if konto is None:
        return _fehlerseite(session, "Zuordnung", f"Unbekanntes Konto {konto_id}.", f"/backoffice/bank/unzugeordnet?bank_konto_id={transaktion.bank_konto_id}")
    try:
        betrag_cent = parse_eur_betrag(betrag)
        zuordnung = _bank_service.zuordnen_manuell(
            ctx=_ctx(session), transaktion=transaktion, konto=konto, betrag_cent=betrag_cent,
            beleg_referenz=f"Manuelle Zuordnung durch {session.user_id}", vorgang_id=vorgangs_id,
        )
        _audit_service.log(
            entity_typ="zuordnung", entity_id=str(zuordnung.id), aktion="manuell_zugeordnet", akteur=session.user_id,
            payload={"transaktion_id": transaktion_id, "konto_id": konto_id, "betrag_cent": betrag_cent, "vorgangs_id": vorgangs_id},
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Zuordnung", str(exc), f"/backoffice/bank/unzugeordnet?bank_konto_id={transaktion.bank_konto_id}")
    return RedirectResponse(url=f"/backoffice/bank/unzugeordnet?bank_konto_id={transaktion.bank_konto_id}&zugeordnet=1", status_code=303)


@router.get("/bank/vollstaendigkeit", response_class=HTMLResponse)
def bank_vollstaendigkeit_formular(request: Request, session=Depends(_current_session)) -> HTMLResponse:
    bank_konten = _bank_repo.list_bank_konten()
    zeilen = []
    for bk in bank_konten:
        bestaetigt_bis = _bank_service.bankvollstaendigkeit_bestaetigt_bis(bk.id)
        letzte_zeile_alter = _bank_service.bankstand_alter_tage(bk.id)
        zeilen.append(f"""
        <tr>
          <td>{h(bk.id)} ({h(bk.bezeichnung)})</td>
          <td>{bestaetigt_bis.isoformat() if bestaetigt_bis else '<span class="warn">nie bestätigt</span>'}</td>
          <td>{f'{letzte_zeile_alter} Tage' if letzte_zeile_alter is not None else '-'} <span class="muted">(nur Datumshinweis, kein Vollständigkeitsnachweis)</span></td>
          <td>
            <form method="post" action="/backoffice/bank/{h(bk.id)}/vollstaendigkeit-bestaetigen">
              {csrf_feld(session.csrf_token)}
              <input type="date" name="bestaetigt_bis" value="{date.today().isoformat()}" required>
              <button type="submit">Bestätigen: Import lückenlos bis Datum</button>
            </form>
          </td>
        </tr>""")
    inhalt = f"""
    <div class="card">
      <h1>Bankvollständigkeit</h1>
      <p class="muted">Nur eine ausdrückliche menschliche Bestätigung zählt fürs Mahnwesen - das bloße
         Vorhandensein einer aktuellen Zeile beweist keine Exportvollständigkeit.</p>
      <table><tr><th>Bankkonto</th><th>Zuletzt bestätigt bis</th><th>Letzte importierte Zeile</th><th>Neu bestätigen</th></tr>
        {''.join(zeilen)}
      </table>
    </div>"""
    return _layout(request, session, "Bankvollständigkeit", inhalt)


@router.post("/bank/{bank_konto_id}/vollstaendigkeit-bestaetigen")
def bank_vollstaendigkeit_bestaetigen(request: Request, bank_konto_id: str, bestaetigt_bis: date = Form(...), csrf_token: str = Form(...), session=Depends(_current_session)):
    _verify_csrf(session, csrf_token)
    _bank_service.bestaetige_bankvollstaendigkeit(bank_konto_id=bank_konto_id, bestaetigt_bis=bestaetigt_bis, bestaetigt_von=session.user_id)
    _audit_service.log(
        entity_typ="bank_vollstaendigkeit", entity_id=bank_konto_id, aktion="bestaetigt", akteur=session.user_id,
        payload={"bestaetigt_bis": bestaetigt_bis.isoformat()},
    )
    return RedirectResponse(url="/backoffice/bank/vollstaendigkeit?bestaetigt=1", status_code=303)


# -- Vorschreibungsentwurf --------------------------------------------------------


@router.get("/vertrag/{vertrag_id}/vorschreibung", response_class=HTMLResponse)
def vorschreibung_formular(request: Request, vertrag_id: str, monat: str | None = None, session=Depends(_current_session)) -> HTMLResponse:
    vertrag = _stammdaten_repo.get_vertrag(vertrag_id)
    if vertrag is None:
        return _fehlerseite(session, "Vorschreibung", f"Unbekannter Vertrag {vertrag_id}.")
    gesperrt = _objekt_fuer_vertrag_gesperrt(vertrag_id)
    form = f"""
    <form method="get" action="/backoffice/vertrag/{h(vertrag_id)}/vorschreibung">
      <label>Monat (YYYY-MM)</label>
      <input type="month" name="monat" value="{h(monat or '')}" required>
      <button type="submit">Vorschau anzeigen/aktualisieren</button>
    </form>"""
    inhalt = f'<div class="card"><h1>Vorschreibungsentwurf — {h(vertrag_id)}</h1>{form}</div>'
    if gesperrt:
        inhalt += flash_error("Objekt ist gesperrt; keine Vorschreibung möglich.")
        return _layout(request, session, "Vorschreibung", inhalt)

    einheit = _stammdaten_repo.get_einheit(vertrag.einheit_id)
    historisch = vertrag.gueltig_bis is not None and monat and vertrag.gueltig_bis < faelligkeitsdatum(monat, 28)
    leerstand = einheit is not None and einheit.nutzungsstatus == "LEERSTAND"

    if monat:
        try:
            ergebnis = _vorschreibung_service.entwurf_erstellen(ctx=_ctx(session), vertrag=vertrag, monat=monat)
            aufschluesselung = _vorschreibung_service.aufschluesselung(ergebnis.vorschreibung_id)
        except (MietinkassoError, ValueError) as exc:
            return _layout(request, session, "Vorschreibung", inhalt + flash_error(str(exc)))

        klausel = _index_repo.freigegebene_klausel(vertrag_id)
        if klausel is None:
            index_info = "keine freigegebene IndexKlausel"
        else:
            profil_status = "" if klausel.berechnungsprofil in UNTERSTUETZTE_BERECHNUNGSPROFILE else ' <span class="warn">GESPERRT (nicht implementiert)</span>'
            index_info = f"Version {klausel.version}, Profil {h(klausel.berechnungsprofil)}{profil_status}"

        warnungen = []
        if historisch:
            warnungen.append("Vertrag ist zum gewählten Monat bereits historisch (gueltig_bis überschritten).")
        if leerstand:
            warnungen.append("Einheit steht laut Nutzungsstatus LEERSTAND.")
        warn_html = flash_error(" / ".join(warnungen)) if warnungen else ""

        pos_zeilen = "".join(
            f"<tr><td>{h(p.kategorie)}</td><td>{h(p.bezeichnung)}</td><td>{eur(p.netto_cent)}</td>"
            f"<td>{eur(p.ust_cent)} ({p.ust_satz_promille/1000:.1f}%)</td><td>{eur(p.brutto_cent)}</td></tr>"
            for p in aufschluesselung.positionen
        )
        freigabe_form = ""
        if not warnungen:
            freigabe_form = f"""
            <form method="post" action="/backoffice/vertrag/{h(vertrag_id)}/vorschreibung/sollstellen">
              {csrf_feld(session.csrf_token)}
              <input type="hidden" name="monat" value="{h(monat)}">
              <button type="submit">Freigeben (Sollstellen — bucht {eur(aufschluesselung.summe_brutto_cent)} als SOLL)</button>
            </form>"""
        else:
            freigabe_form = flash_error("Freigabe gesperrt (siehe Warnung oben) - Historische/leerstehende Fälle werden nicht automatisch aktiviert.")

        inhalt += f"""
        <div class="card">
          <h2>Vorschau {h(monat)} — Status {h(ergebnis.status)}</h2>
          <p>Wirksame Indexversion: {index_info}</p>
          {warn_html}
          <table>
            <tr><th>Kategorie</th><th>Bezeichnung</th><th>Netto</th><th>USt</th><th>Brutto</th></tr>
            {pos_zeilen}
            <tr><th colspan=2>Summe</th><th>{eur(aufschluesselung.summe_netto_cent)}</th>
                <th>{eur(aufschluesselung.summe_ust_cent)}</th><th>{eur(aufschluesselung.summe_brutto_cent)}</th></tr>
          </table>
          {freigabe_form}
        </div>"""
    return _layout(request, session, "Vorschreibung", inhalt)


@router.post("/vertrag/{vertrag_id}/vorschreibung/sollstellen")
def vorschreibung_sollstellen(request: Request, vertrag_id: str, monat: str = Form(...), csrf_token: str = Form(...), session=Depends(_current_session)):
    _verify_csrf(session, csrf_token)
    vertrag = _stammdaten_repo.get_vertrag(vertrag_id)
    if vertrag is None:
        return _fehlerseite(session, "Vorschreibung", f"Unbekannter Vertrag {vertrag_id}.")
    einheit = _stammdaten_repo.get_einheit(vertrag.einheit_id)
    if vertrag.gueltig_bis is not None and vertrag.gueltig_bis < faelligkeitsdatum(monat, 28):
        return _fehlerseite(session, "Vorschreibung", "Vertrag ist historisch; Sollstellung wird nicht automatisch aktiviert.", f"/backoffice/vertrag/{vertrag_id}/vorschreibung?monat={monat}")
    if einheit is not None and einheit.nutzungsstatus == "LEERSTAND":
        return _fehlerseite(session, "Vorschreibung", "Einheit steht als LEERSTAND; Sollstellung wird nicht automatisch aktiviert.", f"/backoffice/vertrag/{vertrag_id}/vorschreibung?monat={monat}")
    konto = _stammdaten_repo.get_konto_by_vertrag(vertrag_id)
    if konto is None:
        return _fehlerseite(session, "Vorschreibung", "Kein Konto für diesen Vertrag vorhanden.")
    try:
        ergebnis = _vorschreibung_service.sollstellen(ctx=_ctx(session), vertrag=vertrag, konto=konto, monat=monat)
        _audit_service.log(
            entity_typ="vorschreibung", entity_id=str(ergebnis.vorschreibung_id), aktion="sollgestellt",
            akteur=session.user_id, payload={"vertrag_id": vertrag_id, "monat": monat, "summe_cent": ergebnis.summe_cent},
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Vorschreibung", str(exc), f"/backoffice/vertrag/{vertrag_id}/vorschreibung?monat={monat}")
    return RedirectResponse(url=f"/backoffice/konto/{konto.id}?sollgestellt=1", status_code=303)


# -- Mahnvorschau (nur Entwürfe, kein Versand) ------------------------------------


def _bank_freigabe_ableiten(gesellschaft_id: str, vertrag_id: str) -> tuple[date | None, bool]:
    """Leitet `bank_bestaetigt_bis`/`ungeklaerte_eingaenge_vorhanden`
    AUSSCHLIESSLICH aus persistierten, serverseitigen Bankdaten ab - die
    Oberfläche darf hierfür NIEMALS ein Formularfeld entgegennehmen
    (sonst könnte eine Vorschau eine Mahnung freischalten, die die echten
    Bankdaten nicht hergeben)."""

    bank_konten = _bank_repo.list_bank_konten(gesellschaft_id=gesellschaft_id)
    if not bank_konten:
        return None, False
    bestaetigungen = [_bank_service.bankvollstaendigkeit_bestaetigt_bis(bk.id) for bk in bank_konten]
    if any(b is None for b in bestaetigungen):
        bank_bestaetigt_bis = None
    else:
        bank_bestaetigt_bis = min(bestaetigungen)
    ungeklaert = any(
        _bank_service.hat_ungeklaerte_relevante_eingaenge(bank_konto_id=bk.id, vertrag_id=vertrag_id) for bk in bank_konten
    )
    return bank_bestaetigt_bis, ungeklaert


@router.get("/vertrag/{vertrag_id}/mahnvorschau", response_class=HTMLResponse)
def mahnvorschau(request: Request, vertrag_id: str, heute: str | None = None, session=Depends(_current_session)) -> HTMLResponse:
    vertrag = _stammdaten_repo.get_vertrag(vertrag_id)
    if vertrag is None:
        return _fehlerseite(session, "Mahnvorschau", f"Unbekannter Vertrag {vertrag_id}.")
    if _objekt_fuer_vertrag_gesperrt(vertrag_id):
        return _fehlerseite(session, "Mahnvorschau", "Objekt ist gesperrt; keine Mahnung möglich.")
    konto = _stammdaten_repo.get_konto_by_vertrag(vertrag_id)
    if konto is None:
        return _fehlerseite(session, "Mahnvorschau", "Kein Konto für diesen Vertrag vorhanden.")
    heute_datum = date.fromisoformat(heute) if heute else date.today()
    policy = _mahn_policy_repo.aktuelle_freigegebene()

    form = f"""
    <form method="get" action="/backoffice/vertrag/{h(vertrag_id)}/mahnvorschau">
      <label>Heute (Simulationsdatum)</label>
      <input type="date" name="heute" value="{heute_datum.isoformat()}">
      <button type="submit">Neu berechnen</button>
    </form>"""
    inhalt = f'<div class="card"><h1>Mahnvorschau — {h(vertrag_id)}</h1>{form}</div>'

    if policy is None:
        inhalt += flash_error("Keine freigegebene MahnPolicy vorhanden; es kann nichts geplant werden.")
        return _layout(request, session, "Mahnvorschau", inhalt)

    bank_bestaetigt_bis, ungeklaert = _bank_freigabe_ableiten(vertrag.gesellschaft_id, vertrag_id)
    forderungen = _op_service.offene_forderungen(konto.id, heute=heute_datum)
    zeilen = []
    for forderung in forderungen:
        ergebnis = _mahn_service.plane_forderung(
            ctx=_ctx(session), vertrag=vertrag, konto=konto, forderung=forderung, policy=policy, heute=heute_datum,
            bank_bestaetigt_bis=bank_bestaetigt_bis, ungeklaerte_eingaenge_vorhanden=ungeklaert,
        )
        sende_check = ""
        if ergebnis.status == "GEPLANT" and ergebnis.mahnfall_id is not None:
            sende_check = f"""
            <form method="post" action="/backoffice/mahnfall/{ergebnis.mahnfall_id}/sendebereitschaft" class="inline">
              {csrf_feld(session.csrf_token)}
              <button type="submit" class="secondary">Sendebereitschaft prüfen (kein Versand)</button>
            </form>"""
        zeilen.append(f"""
        <tr>
          <td>OP #{forderung.op_position_id}</td><td>{h(forderung.art)}</td><td>{eur(forderung.rest_cent)}</td>
          <td>{forderung.faelligkeit.isoformat() if forderung.faelligkeit else 'unbekannt'}</td>
          <td>{h(ergebnis.status)}</td><td>{h(ergebnis.grund)}</td><td>{sende_check}</td>
        </tr>""")

    bank_status = (
        f"Bank bestätigt bis {bank_bestaetigt_bis.isoformat()}" if bank_bestaetigt_bis else '<span class="warn">keine ausreichend aktuelle Bankbestätigung</span>'
    ) + (" | <span class='warn'>ungeklärte Eingänge vorhanden</span>" if ungeklaert else "")
    inhalt += f"""
    <div class="card">
      <p class="muted">Sperrgründe transparent, serverseitig aus persistierten Daten abgeleitet: {bank_status}</p>
      <table>
        <tr><th>Forderung</th><th>Art</th><th>Rest</th><th>Fälligkeit</th><th>Status</th><th>Grund</th><th></th></tr>
        {''.join(zeilen) if zeilen else '<tr><td colspan=7 class="muted">Keine offenen Forderungen.</td></tr>'}
      </table>
      <p class="muted">Pilot: ausschließlich Entwürfe/Planung. Kein Senden-Button löst einen echten Mailversand aus.</p>
    </div>"""
    return _layout(request, session, "Mahnvorschau", inhalt)


@router.post("/mahnfall/{mahnfall_id}/sendebereitschaft", response_class=HTMLResponse)
def mahnfall_sendebereitschaft(request: Request, mahnfall_id: int, csrf_token: str = Form(...), session=Depends(_current_session)) -> HTMLResponse:
    _verify_csrf(session, csrf_token)
    mahnfall = _mahn_fall_repo.get(mahnfall_id)
    if mahnfall is None:
        return _fehlerseite(session, "Mahnvorschau", f"Unbekannter Mahnfall {mahnfall_id}.")
    bank_bestaetigt_bis, ungeklaert = _bank_freigabe_ableiten(mahnfall.gesellschaft_id, mahnfall.vertrag_id)
    ergebnis = _mahn_service.versenden(
        ctx=_ctx(session), mahnfall_id=mahnfall_id, heute=date.today(), bank_bestaetigt_bis=bank_bestaetigt_bis,
        ungeklaerte_eingaenge_vorhanden=ungeklaert, send_enabled=False, versand_fn=lambda *_: None,
    )
    inhalt = flash_ok(f"Sendebereitschaft (KEIN echter Versand): {ergebnis.status} — {ergebnis.grund}")
    inhalt += f'<p><a href="/backoffice/vertrag/{h(mahnfall.vertrag_id)}/mahnvorschau">&larr; zurück</a></p>'
    return _layout(request, session, "Sendebereitschaft", inhalt)


# -- Mahnstufen-Konfiguration (genau 2 Stufen, HV-20260912-ECHTBETRIEB) ------


def _policy_zeile_html(policy) -> str:
    aktion = ""
    if policy.status == "ENTWURF":
        aktion = f"""
        <form method="post" action="/backoffice/mahnwesen/policy/{policy.id}/freigeben" class="inline">
          {{csrf}}
          <button type="submit" class="secondary">Freigeben</button>
        </form>"""
    return f"""
    <tr>
      <td>{policy.version}</td>
      <td>{policy.stufe1_tage_nach_faelligkeit} Tage</td>
      <td>{policy.stufe2_mindesttage_nach_stufe1_versand} Tage</td>
      <td>{policy.zinsen_prozent}%</td>
      <td>{policy.gebuehr_cent} Cent</td>
      <td>{h(policy.status)}</td>
      <td>{policy.freigegeben_am.isoformat() if policy.freigegeben_am else '-'}</td>
      <td>{aktion}</td>
    </tr>"""


@router.get("/mahnwesen/policy", response_class=HTMLResponse)
def mahnpolicy_uebersicht(request: Request, session=Depends(_current_session)) -> HTMLResponse:
    aktuelle = _mahn_policy_repo.aktuelle_freigegebene()
    alle = _mahn_policy_repo.alle()

    aktuelle_html = (
        f"""<div class="card">
          <p class="ok">Aktuell freigegeben: Version {aktuelle.version} — Stufe 1 nach {aktuelle.stufe1_tage_nach_faelligkeit}
          Tagen, Stufe 2 frühestens {aktuelle.stufe2_mindesttage_nach_stufe1_versand} Tage nach tatsächlich
          versandter Stufe 1 UND erst nach deren vertraglicher Zahlungsfrist (serverseitig als Maximum
          erzwungen). Zinsen/Gebühren: {aktuelle.zinsen_prozent}% / {aktuelle.gebuehr_cent} Cent.</p>
        </div>"""
        if aktuelle is not None
        else flash_error("Keine freigegebene MahnPolicy vorhanden — es kann derzeit NICHTS automatisch gemahnt werden.")
    )

    zeilen_html = "".join(_policy_zeile_html(p).replace("{csrf}", csrf_feld(session.csrf_token)) for p in alle)

    inhalt = f"""
    <div class="card" style="max-width:820px;">
      <h1>Mahnstufen-Konfiguration</h1>
      <p class="muted">Genau zwei automatische Mahnstufen (Fachregel 7) — keine dritte Stufe, kein
         Inkasso/RA, Zinsen/Gebühren in diesem Auftrag fest auf 0 (nicht editierbar). Bestehende Sperren
         (RA/Ratenplan/Insolvenz/ungeklärter Eingang/unklarer Eröffnungssaldo/manuelle Sperre) sowie die
         BESTÄTIGTE Bankvollständigkeit gelten unverändert und werden hier NICHT umgangen.
         <code>SEND_ENABLED=false</code> bleibt Standard — diese Seite ändert daran nichts, sie plant nur
         Fristen, sie versendet nichts.</p>
    </div>
    {aktuelle_html}
    <div class="card">
      <h2>Neue Version vorschlagen</h2>
      <form method="post" action="/backoffice/mahnwesen/policy/anlegen">
        {csrf_feld(session.csrf_token)}
        <label>Stufe 1: Tage nach belegter Fälligkeit</label>
        <input type="number" name="stufe1_tage_nach_faelligkeit" min="1" value="{_settings.mahn_stufe1_tage_nach_faelligkeit}" required>
        <label>Stufe 2: Mindesttage nach tatsächlich versandter Stufe 1 (zusätzlich wird serverseitig
               IMMER auch die vertragliche Zahlungsfrist abgewartet — das Maximum beider Werte gilt)</label>
        <input type="number" name="stufe2_mindesttage_nach_stufe1_versand" min="1" value="{_settings.mahn_stufe2_mindesttage_nach_stufe1}" required>
        <p class="muted">Zinsen: 0% · Gebühr: 0 Cent (in diesem Auftrag fest, nicht editierbar).</p>
        <button type="submit">Als Entwurf anlegen</button>
      </form>
    </div>
    <div class="card">
      <h2>Versionen</h2>
      <table>
        <tr><th>Version</th><th>Stufe 1</th><th>Stufe 2</th><th>Zinsen</th><th>Gebühr</th><th>Status</th><th>Freigegeben am</th><th></th></tr>
        {zeilen_html or '<tr><td colspan=8 class="muted">Noch keine Policy angelegt.</td></tr>'}
      </table>
    </div>"""
    return _layout(request, session, "Mahnstufen-Konfiguration", inhalt)


@router.post("/mahnwesen/policy/anlegen", response_class=HTMLResponse)
def mahnpolicy_anlegen(
    request: Request,
    stufe1_tage_nach_faelligkeit: int = Form(...),
    stufe2_mindesttage_nach_stufe1_versand: int = Form(...),
    csrf_token: str = Form(...),
    session=Depends(_current_session),
) -> HTMLResponse:
    _verify_csrf(session, csrf_token)
    if stufe1_tage_nach_faelligkeit < 1 or stufe2_mindesttage_nach_stufe1_versand < 1:
        return _fehlerseite(session, "Mahnstufen-Konfiguration", "Beide Werte müssen mindestens 1 Tag betragen.", "/backoffice/mahnwesen/policy")
    policy = _mahn_policy_repo.anlegen(
        stufe1_tage_nach_faelligkeit=stufe1_tage_nach_faelligkeit,
        stufe2_mindesttage_nach_stufe1_versand=stufe2_mindesttage_nach_stufe1_versand,
        # Fest auf 0 - dieser Auftrag verlangt ausdrücklich "keine Zinsen/Gebühren"; kein Formularfeld,
        # das versehentlich (oder absichtlich via rohem POST) einen anderen Wert setzen könnte.
        zinsen_prozent=Decimal("0"),
        gebuehr_cent=0,
        status="ENTWURF",
    )
    _audit_service.log(
        entity_typ="mahn_policy", entity_id=str(policy.id), aktion="angelegt", akteur=session.user_id,
        payload={"version": policy.version, "stufe1_tage": stufe1_tage_nach_faelligkeit, "stufe2_mindesttage": stufe2_mindesttage_nach_stufe1_versand},
    )
    inhalt = flash_ok(f"Policy-Version {policy.version} als ENTWURF angelegt — muss noch freigegeben werden, bevor sie wirkt.")
    inhalt += '<p><a href="/backoffice/mahnwesen/policy">&larr; zurück</a></p>'
    return _layout(request, session, "Mahnstufen-Konfiguration", inhalt)


@router.post("/mahnwesen/policy/{policy_id}/freigeben", response_class=HTMLResponse)
def mahnpolicy_freigeben(request: Request, policy_id: int, csrf_token: str = Form(...), session=Depends(_current_session)) -> HTMLResponse:
    _verify_csrf(session, csrf_token)
    try:
        policy = _mahn_policy_repo.freigeben(policy_id)
    except ValueError as exc:
        return _fehlerseite(session, "Mahnstufen-Konfiguration", str(exc), "/backoffice/mahnwesen/policy")
    _audit_service.log(entity_typ="mahn_policy", entity_id=str(policy.id), aktion="freigegeben", akteur=session.user_id, payload={"version": policy.version})
    inhalt = flash_ok(f"Policy-Version {policy.version} freigegeben.")
    inhalt += '<p><a href="/backoffice/mahnwesen/policy">&larr; zurück</a></p>'
    return _layout(request, session, "Mahnstufen-Konfiguration", inhalt)


# -- Verknüpfung mit bestehender Zahlung (kein neuer Zahlungseintrag) --------------


@router.get("/bank/{transaktion_id}/verknuepfen", response_class=HTMLResponse)
def bank_verknuepfen_formular(request: Request, transaktion_id: int, session=Depends(_current_session)) -> HTMLResponse:
    transaktion = _bank_repo.get_transaktion(transaktion_id)
    if transaktion is None:
        return _fehlerseite(session, "Verknüpfung", f"Unbekannte Transaktion {transaktion_id}.", "/backoffice/bank/unzugeordnet")
    rest = transaktion.betrag_cent - _bank_repo.zugeordneter_betrag(transaktion_id)
    vorgang_vorschlag = f"VERKNUEPFT-{transaktion_id}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    inhalt = f"""
    <div class="card" style="max-width:560px;">
      <h1>Mit bestehender Zahlung verknüpfen — Transaktion #{transaktion_id}</h1>
      <p>Betrag: {eur(transaktion.betrag_cent)} &nbsp;|&nbsp; noch nicht zugeordnet: {eur(rest)}</p>
      <p class="muted">Verlinkt diese Rohtransaktion mit einer BEREITS gebuchten ZAHLUNG-Position (z. B.
         eine vor dem Bankfeed manuell erfasste Zahlung), OHNE einen zweiten Zahlungseintrag zu erzeugen -
         damit bestehende Mieterkonto-Buchungen bei diesem Rohbankimport nicht doppelt gutgeschrieben
         werden. Die OP-ID der bestehenden Zahlung steht im Kontoauszug des betroffenen Kontos
         (Spalte &quot;#&quot;).</p>
      <form method="post" action="/backoffice/bank/{transaktion_id}/verknuepfen">
        {csrf_feld(session.csrf_token)}
        <label>Bestehende ZAHLUNG-OP-ID</label>
        <input type="number" name="op_position_id" required>
        <label>Konto-ID (zur Bestätigung/Mandantenprüfung)</label>
        <input type="text" name="konto_id" required>
        <label>Verknüpfungsbetrag (EUR)</label>
        <input type="text" name="betrag" value="{eur(rest).split()[0]}" required>
        <label>Vorgangs-ID (eindeutig; ein Retry mit derselben ID ist ein sicherer No-Op)</label>
        <input type="text" name="vorgangs_id" value="{vorgang_vorschlag}" required>
        <button type="submit">Verknüpfen</button>
      </form>
    </div>"""
    return _layout(request, session, "Verknüpfung", inhalt)


@router.post("/bank/{transaktion_id}/verknuepfen")
def bank_verknuepfen(
    request: Request,
    transaktion_id: int,
    op_position_id: int = Form(...),
    konto_id: str = Form(...),
    betrag: str = Form(...),
    vorgangs_id: str = Form(...),
    csrf_token: str = Form(...),
    session=Depends(_current_session),
):
    _verify_csrf(session, csrf_token)
    transaktion = _bank_repo.get_transaktion(transaktion_id)
    if transaktion is None:
        return _fehlerseite(session, "Verknüpfung", f"Unbekannte Transaktion {transaktion_id}.", "/backoffice/bank/unzugeordnet")
    konto = _stammdaten_repo.get_konto(konto_id)
    if konto is None:
        return _fehlerseite(session, "Verknüpfung", f"Unbekanntes Konto {konto_id}.", f"/backoffice/bank/{transaktion_id}/verknuepfen")
    op_position = _op_service.get_position(op_position_id)
    if op_position is None:
        return _fehlerseite(session, "Verknüpfung", f"Unbekannte OP-Position {op_position_id}.", f"/backoffice/bank/{transaktion_id}/verknuepfen")
    try:
        betrag_cent = parse_eur_betrag(betrag)
        zuordnung = _bank_service.verknuepfe_mit_bestehender_zahlung(
            ctx=_ctx(session), transaktion=transaktion, op_position=op_position, konto=konto,
            betrag_cent=betrag_cent, vorgang_id=vorgangs_id,
        )
        _audit_service.log(
            entity_typ="zuordnung", entity_id=str(zuordnung.id), aktion="mit_bestehender_zahlung_verknuepft",
            akteur=session.user_id,
            payload={"transaktion_id": transaktion_id, "op_position_id": op_position_id, "betrag_cent": betrag_cent, "vorgangs_id": vorgangs_id},
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Verknüpfung", str(exc), f"/backoffice/bank/unzugeordnet?bank_konto_id={transaktion.bank_konto_id}")
    return RedirectResponse(url=f"/backoffice/bank/unzugeordnet?bank_konto_id={transaktion.bank_konto_id}&zugeordnet=1", status_code=303)


# -- Vertragsprüfung (Auftrag 12.09., Paket B) -------------------------------------


_RECHTSORDNUNGEN_FUER_AUSWAHL = [
    "OESTERREICH_MRG_VOLL", "OESTERREICH_MRG_TEIL", "OESTERREICH_MRG_FREI",
    "OESTERREICH_WGG", "OESTERREICH_GEWERBE", "DEUTSCHLAND", "UNGEKLAERT",
]


def _pruefung_zeile_html(p) -> str:
    return (
        "<tr>"
        f"<td>{p.version}</td><td>{h(p.rechtsordnung)}</td><td>{h(p.fachstatus)}</td>"
        f"<td>{h(p.quellenbeleg_referenz)}</td><td>{h(p.kommentar or '')}</td>"
        f"<td>{h(p.erstellt_von)}</td><td>{p.erstellt_am.isoformat() if p.erstellt_am else ''}</td>"
        "</tr>"
    )


def _sperre_zeile_html(s, *, vertrag_id: str, csrf_token: str) -> str:
    return f"""
    <tr>
      <td>{h(s.grund)}</td><td>{s.gesetzt_am.isoformat() if s.gesetzt_am else ''}</td><td>{h(s.kommentar or '')}</td>
      <td>
        <form method="post" action="/backoffice/vertrag/{h(vertrag_id)}/sperre/{s.id}/aufheben">
          {csrf_feld(csrf_token)}
          <input type="text" name="begruendung" placeholder="Begründung/Beleg (Pflicht)" required>
          <button type="submit" class="secondary">Aufheben</button>
        </form>
      </td>
    </tr>"""


def _index_pruefbedarf_zeile_html(e) -> str:
    return (
        "<tr>"
        f"<td>{h(e.rechtsordnung or '-')}</td><td>{h(e.basis_reihe or '-')}</td>"
        f"<td>{h(str(e.basis_wert)) if e.basis_wert is not None else '-'}</td><td>{h(e.basis_monat or '-')}</td>"
        f"<td>{h(e.kommentar or '')}</td><td>{h(e.erstellt_von)}</td>"
        f"<td>{e.erstellt_am.isoformat() if e.erstellt_am else ''}</td>"
        "</tr>"
    )


@router.get("/vertrag/{vertrag_id}/pruefung", response_class=HTMLResponse)
def vertragspruefung_uebersicht(request: Request, vertrag_id: str, session=Depends(_current_session)) -> HTMLResponse:
    vertrag = _stammdaten_repo.get_vertrag(vertrag_id)
    if vertrag is None:
        return _fehlerseite(session, "Vertragsprüfung", f"Unbekannter Vertrag {vertrag_id}.")
    if _objekt_fuer_vertrag_gesperrt(vertrag_id):
        return _fehlerseite(session, "Vertragsprüfung", "Objekt ist gesperrt; keine Prüfung möglich.")

    rechtsordnung_optionen = "".join(
        option(r, r, selected=(r == vertrag.rechtsordnung)) for r in _RECHTSORDNUNGEN_FUER_AUSWAHL
    )
    historie = _vertragspruefung_service.liste_pruefungen(vertrag_id)
    historie_html = "".join(_pruefung_zeile_html(p) for p in historie) or '<tr><td colspan=7 class="muted">Noch keine Prüfung erfasst.</td></tr>'

    aktive_sperren = _stammdaten_repo.aktive_sperren(vertrag_id)
    sperren_html = "".join(_sperre_zeile_html(s, vertrag_id=vertrag_id, csrf_token=session.csrf_token) for s in aktive_sperren)
    sperren_html = sperren_html or '<tr><td colspan=4 class="muted">Keine aktiven Sperren.</td></tr>'

    pruefbedarf = _vertragspruefung_service.liste_index_pruefbedarf(vertrag_id)
    pruefbedarf_html = "".join(_index_pruefbedarf_zeile_html(e) for e in pruefbedarf) or '<tr><td colspan=7 class="muted">Noch kein Prüfbedarf gespeichert.</td></tr>'

    inhalt = f"""
    <div class="card">
      <h1>Vertragsprüfung — {h(vertrag_id)}</h1>
      <p>Aktuelle Rechtsordnung (wirksam): <strong>{h(vertrag.rechtsordnung)}</strong></p>
    </div>

    <div class="card">
      <h2>Neue Prüfung erfassen</h2>
      <p class="muted">Jede Prüfung ist eine neue, unveränderliche Version. Nur Fachstatus GEPRUEFT schreibt
         die Rechtsordnung tatsächlich auf den Vertrag zurück (Freigabe) - ein ENTWURF bleibt sichtbar,
         aber wirkungslos. Ohne Quellenbeleg-Referenz wird nichts gespeichert (keine beleglose
         Klassifizierung).</p>
      <form method="post" action="/backoffice/vertrag/{h(vertrag_id)}/pruefung/anlegen">
        {csrf_feld(session.csrf_token)}
        <label>Rechtsprofil (Rechtsordnung)</label>
        <select name="rechtsordnung" required>{rechtsordnung_optionen}</select>
        <label>Fachstatus</label>
        <select name="fachstatus" required>
          <option value="ENTWURF">ENTWURF (nur speichern, keine Freigabe)</option>
          <option value="GEPRUEFT">GEPRUEFT (schreibt die Rechtsordnung auf den Vertrag zurück)</option>
        </select>
        <label>Quellenbeleg-Referenz (Pflicht)</label>
        <input type="text" name="quellenbeleg_referenz" placeholder="z. B. Mietvertrag-2024.pdf, S. 3" required>
        <label>Kommentar</label>
        <input type="text" name="kommentar">
        <button type="submit">Speichern</button>
      </form>
      <h3>Prüfhistorie</h3>
      <table>
        <tr><th>Version</th><th>Rechtsordnung</th><th>Fachstatus</th><th>Quellenbeleg</th><th>Kommentar</th><th>Von</th><th>Am</th></tr>
        {historie_html}
      </table>
    </div>

    <div class="card">
      <h2>Aktive Sperren</h2>
      <p class="muted">Jede Aufhebung ist einzeln und braucht eine Begründung - kein Sammel-/Automatik-Pfad,
         auch nicht für RATENPLAN/RECHTSANWALT.</p>
      <table>
        <tr><th>Grund</th><th>Gesetzt am</th><th>Kommentar</th><th>Aktion</th></tr>
        {sperren_html}
      </table>
    </div>

    <div class="card">
      <h2>Index-Prüfbedarf (Entwurf, ohne Freigabe)</h2>
      <p class="muted">Auch unvollständige Indexangaben sind hier speicherbar - alle Felder optional,
         keine Verbindung zu einer freigebbaren Indexklausel; eine echte Klausel entsteht weiterhin nur
         über den bestehenden Index-Weg.</p>
      <form method="post" action="/backoffice/vertrag/{h(vertrag_id)}/index-pruefbedarf/anlegen">
        {csrf_feld(session.csrf_token)}
        <label>Rechtsordnung (optional)</label>
        <select name="rechtsordnung"><option value="">-- keine Angabe --</option>{rechtsordnung_optionen}</select>
        <label>Basisreihe (optional)</label>
        <input type="text" name="basis_reihe">
        <label>Basiswert (optional)</label>
        <input type="text" name="basis_wert">
        <label>Basismonat (optional, JJJJ-MM)</label>
        <input type="text" name="basis_monat">
        <label>Kommentar</label>
        <input type="text" name="kommentar">
        <button type="submit">Als Prüfbedarf speichern</button>
      </form>
      <table>
        <tr><th>Rechtsordnung</th><th>Basisreihe</th><th>Basiswert</th><th>Basismonat</th><th>Kommentar</th><th>Von</th><th>Am</th></tr>
        {pruefbedarf_html}
      </table>
    </div>
    <p><a href="/backoffice/konto/{h(_stammdaten_repo.get_konto_by_vertrag(vertrag_id).id) if _stammdaten_repo.get_konto_by_vertrag(vertrag_id) else ''}">&larr; zurück zum Kontoauszug</a></p>
    """
    return _layout(request, session, "Vertragsprüfung", inhalt)


@router.post("/vertrag/{vertrag_id}/pruefung/anlegen")
def vertragspruefung_anlegen(
    request: Request,
    vertrag_id: str,
    rechtsordnung: str = Form(...),
    fachstatus: str = Form(...),
    quellenbeleg_referenz: str = Form(...),
    kommentar: str = Form(""),
    csrf_token: str = Form(...),
    session=Depends(_current_session),
):
    _verify_csrf(session, csrf_token)
    vertrag = _stammdaten_repo.get_vertrag(vertrag_id)
    if vertrag is None:
        return _fehlerseite(session, "Vertragsprüfung", f"Unbekannter Vertrag {vertrag_id}.")
    try:
        pruefung = _vertragspruefung_service.pruefung_anlegen(
            ctx=_ctx(session), vertrag=vertrag, rechtsordnung=rechtsordnung, fachstatus=fachstatus,
            quellenbeleg_referenz=quellenbeleg_referenz, kommentar=kommentar or None, akteur=session.user_id,
        )
        _audit_service.log(
            entity_typ="vertrag_pruefung", entity_id=f"{vertrag_id}:{pruefung.version}", aktion=fachstatus,
            akteur=session.user_id,
            payload={"rechtsordnung": rechtsordnung, "quellenbeleg_referenz": quellenbeleg_referenz, "kommentar": kommentar},
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Vertragsprüfung", str(exc), f"/backoffice/vertrag/{vertrag_id}/pruefung")
    return RedirectResponse(url=f"/backoffice/vertrag/{vertrag_id}/pruefung", status_code=303)


@router.post("/vertrag/{vertrag_id}/sperre/{sperre_id}/aufheben")
def vertragspruefung_sperre_aufheben(
    request: Request,
    vertrag_id: str,
    sperre_id: int,
    begruendung: str = Form(...),
    csrf_token: str = Form(...),
    session=Depends(_current_session),
):
    _verify_csrf(session, csrf_token)
    vertrag = _stammdaten_repo.get_vertrag(vertrag_id)
    if vertrag is None:
        return _fehlerseite(session, "Vertragsprüfung", f"Unbekannter Vertrag {vertrag_id}.")
    try:
        sperre = _vertragspruefung_service.sperre_aufheben(
            ctx=_ctx(session), vertrag=vertrag, sperre_id=sperre_id, begruendung=begruendung, akteur=session.user_id,
        )
        _audit_service.log(
            entity_typ="sperre", entity_id=str(sperre_id), aktion="aufgehoben", akteur=session.user_id,
            payload={"vertrag_id": vertrag_id, "grund": sperre.grund, "begruendung": begruendung},
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Vertragsprüfung", str(exc), f"/backoffice/vertrag/{vertrag_id}/pruefung")
    return RedirectResponse(url=f"/backoffice/vertrag/{vertrag_id}/pruefung", status_code=303)


@router.post("/vertrag/{vertrag_id}/index-pruefbedarf/anlegen")
def vertragspruefung_index_pruefbedarf_anlegen(
    request: Request,
    vertrag_id: str,
    rechtsordnung: str = Form(""),
    basis_reihe: str = Form(""),
    basis_wert: str = Form(""),
    basis_monat: str = Form(""),
    kommentar: str = Form(""),
    csrf_token: str = Form(...),
    session=Depends(_current_session),
):
    _verify_csrf(session, csrf_token)
    vertrag = _stammdaten_repo.get_vertrag(vertrag_id)
    if vertrag is None:
        return _fehlerseite(session, "Vertragsprüfung", f"Unbekannter Vertrag {vertrag_id}.")
    # Basiswert ist ein reiner Indexwert (kein Geldbetrag) - eigene, tolerante
    # Umwandlung statt der EUR-Betragsvalidierung; leer/ungültig -> None
    # (bewusst unvollständig speicherbar, kein Fehler).
    basis_wert_decimal = None
    if (basis_wert or "").strip():
        try:
            basis_wert_decimal = Decimal(basis_wert.strip().replace(",", "."))
        except InvalidOperation:
            basis_wert_decimal = None
    try:
        _vertragspruefung_service.index_pruefbedarf_speichern(
            ctx=_ctx(session), vertrag=vertrag, rechtsordnung=rechtsordnung or None,
            basis_reihe=basis_reihe or None, basis_wert=basis_wert_decimal, basis_monat=basis_monat or None,
            kommentar=kommentar or None, akteur=session.user_id,
        )
        _audit_service.log(
            entity_typ="index_pruefbedarf", entity_id=vertrag_id, aktion="angelegt", akteur=session.user_id,
            payload={"rechtsordnung": rechtsordnung, "basis_reihe": basis_reihe, "basis_monat": basis_monat},
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Vertragsprüfung", str(exc), f"/backoffice/vertrag/{vertrag_id}/pruefung")
    return RedirectResponse(url=f"/backoffice/vertrag/{vertrag_id}/pruefung", status_code=303)
