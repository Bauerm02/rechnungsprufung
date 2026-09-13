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

import json
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from html import escape as h
from urllib.parse import urlparse

from fastapi import APIRouter, Cookie, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from mietinkasso.audit.service import AuditService
from mietinkasso.auth.service import AuthContext, require_gesellschaft_access
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
from mietinkasso.domain.enums import ZUGANGSFORMEN_ALLE as _ZUGANGSFORMEN_ALLE
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
from mietinkasso.mieweg_vorschau.repository import MieWegVorschauRepository
from mietinkasso.mieweg_vorschau.service import MieWegVorschauService, VpiWert
from mietinkasso.indexautomatik.bootstrap import bauen as _indexautomatik_bauen
from mietinkasso.indexautomatik.mailversand_service import HVMailversandService, bank_freigabe_ableiten
from mietinkasso.indexautomatik.zeit import heute_wien
from mietinkasso.vertragspruefung.repository import IndexPruefbedarfRepository, VertragPruefungRepository
from mietinkasso.vertragspruefung.service import VertragspruefungService
from mietinkasso.vorschreibung.repository import VorschreibungRepository
from mietinkasso.vorschreibung.service import VorschreibungService, faelligkeitsdatum
from mietinkasso.variableabrechnung.bootstrap import bauen as _variableabrechnung_bauen
from mietinkasso.variableabrechnung.csv_import import (
    VariableAbrechnungImportNichtAnwendbarError,
    erstelle_plan as _variable_abrechnung_erstelle_plan,
    parse_csv as _variable_abrechnung_parse_csv,
    plan_hash as _variable_abrechnung_plan_hash,
    wende_an as _variable_abrechnung_wende_an,
)
from mietinkasso.variableabrechnung.dashboard import berechne_monatsuebersicht
from mietinkasso.rueckstaende.service import berechne_rueckstandsuebersicht

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
_mieweg_vorschau_repo = MieWegVorschauRepository(_session_factory)
_mieweg_vorschau_service = MieWegVorschauService(_mieweg_vorschau_repo, _stammdaten_repo)
_audit_service = AuditService(_session_factory)
_indexautomatik = _indexautomatik_bauen(_session_factory, _settings)
_hv_mail = HVMailversandService(_session_factory, _indexautomatik, _settings)
_variableabrechnung = _variableabrechnung_bauen(_session_factory, _stammdaten_repo)

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


def _rueckstaende_objekt_filter_form(uebersicht, *, action: str = "/backoffice/") -> str:
    options = [option("", "Alle Objekte", selected=(uebersicht.objekt_filter is None))]
    je_gesellschaft: dict[str, list] = {}
    for o in uebersicht.objekt_optionen:
        je_gesellschaft.setdefault(o.gesellschaft_name, []).append(o)
    for gesellschaft_name, objekte in je_gesellschaft.items():
        options.append(f'<optgroup label="{h(gesellschaft_name)}">')
        for o in objekte:
            options.append(option(o.id, o.bezeichnung, selected=(o.id == uebersicht.objekt_filter)))
        options.append("</optgroup>")
    return f"""
    <div class="card">
      <form method="get" action="{h(action)}">
        <label>Objekt</label>
        <select name="objekt_id" onchange="this.form.submit()">{''.join(options)}</select>
        <noscript><button type="submit">Anzeigen</button></noscript>
      </form>
    </div>"""


def _rueckstaende_kpi_html(k) -> str:
    def _kpi(label: str, cent: int) -> str:
        return f'<div class="kpi"><span class="zahl">{eur(cent)}</span><span class="kpi-label">{h(label)}</span></div>'

    return f"""
    <div class="kpi-grid">
      {_kpi("Summe positiver Kontostände", k.summe_positiver_kontostaende_cent)}
      {_kpi("Guthaben gesamt (nicht verrechnet)", k.summe_guthaben_cent)}
      {_kpi("Fällig/überfällig (bekanntes Datum)", k.ueberfaellig_cent)}
      {_kpi("Noch nicht fällig", k.nicht_faellig_cent)}
      {_kpi("Fälligkeit unbekannt", k.faelligkeit_unbekannt_cent)}
    </div>"""


def _rueckstaende_mietkonto_zeile_html(z) -> str:
    status_tags = []
    if z.historisch:
        status_tags.append('<span class="badge badge-warn">historisch</span>')
    if z.nutzungsstatus == "LEERSTAND":
        status_tags.append('<span class="badge badge-warn">Leerstand</span>')
    if z.sperrgruende:
        status_tags.append(f'<span class="badge badge-error">Sperre: {h(", ".join(z.sperrgruende))}</span>')
    if z.mahnfaelle_anzahl:
        status_tags.append(f'<span class="badge badge-muted">{z.mahnfaelle_anzahl} Mahnfall(e), siehe Tabelle unten</span>')
    konto_link = f'<a href="/backoffice/konto/{h(z.konto_id)}">{h(z.konto_id)}</a>' if z.konto_id else "-"
    mahnvorschau_link = (
        f'<a href="/backoffice/vertrag/{h(z.vertrag_id)}/mahnvorschau">Mahnvorschau</a>' if z.konto_id else ""
    )
    # Abweichung Kontostand <-> Summe der Einzelpositionen NIE
    # verschweigen (z. B. eine Korrekturbuchung ohne eigene offene
    # Position) - nur bei Gleichstand "–" zeigen, sonst deutlich als
    # Betrag mit Vorzeichen.
    if z.abweichung_saldo_zu_positionen_cent:
        abweichung_html = f'<span class="badge badge-warn">{eur(z.abweichung_saldo_zu_positionen_cent)}</span>'
    elif z.abweichung_saldo_zu_positionen_cent == 0:
        abweichung_html = "–"
    else:
        abweichung_html = "-"
    return (
        f"<tr class='{'gesperrt-row' if z.sperrgruende else ''}'>"
        f"<td>{h(z.objekt_bezeichnung)}</td>"
        f"<td>{h(z.vertrag_id)}</td><td>{h(z.einheit_bezeichnung)}</td>"
        f"<td>{h(z.nutzungsstatus)}</td>"
        f"<td>{h(z.debitor_name)}</td>"
        f"<td>{konto_link}</td>"
        f"<td>{eur(z.saldo_cent) if z.saldo_cent is not None else '-'}</td>"
        f"<td>{eur(z.faelliger_unstrittiger_rest_cent) if z.faelliger_unstrittiger_rest_cent is not None else '-'}</td>"
        f"<td>{eur(z.positionen_faelliger_rest_cent) if z.positionen_faelliger_rest_cent is not None else '-'}</td>"
        f"<td>{eur(z.positionen_rest_gesamt_cent) if z.positionen_rest_gesamt_cent is not None else '-'}</td>"
        f"<td>{abweichung_html}</td>"
        f"<td>{' '.join(status_tags)}</td>"
        f"<td>{mahnvorschau_link}</td>"
        "</tr>"
    )


_FAELLIGKEITSKLASSE_BADGE = {
    "UEBERFAELLIG": '<span class="badge badge-error">fällig/überfällig</span>',
    "NICHT_FAELLIG": '<span class="badge badge-muted">noch nicht fällig</span>',
    "UNBEKANNT": '<span class="badge badge-warn">Fälligkeit unbekannt</span>',
}


def _rueckstaende_position_zeile_html(p) -> str:
    # `faelligkeit_bekannt` ist die maßgebliche Angabe - ein trotzdem
    # gespeichertes Datum bei faelligkeit_bekannt=False (inkonsistente
    # Altdaten) darf NIE wie ein bestätigtes Datum aussehen.
    faelligkeit_html = (
        p.faelligkeit.isoformat() if (p.faelligkeit_bekannt and p.faelligkeit) else '<span class="muted">unbekannt</span>'
    )
    return (
        "<tr>"
        f"<td>{h(p.objekt_bezeichnung)}</td>"
        f"<td>{h(p.vertrag_id)}</td><td>{h(p.debitor_name)}</td>"
        f"<td>#{p.op_position_id}</td>"
        f"<td>{h(p.beleg_referenz)}</td>"
        f"<td>{h(p.art)}</td>"
        f"<td>{h(p.leistungsperiode or '')}</td>"
        f"<td>{p.belegdatum.isoformat()}</td>"
        f"<td>{faelligkeit_html}</td>"
        f"<td>{eur(p.rest_cent)}</td>"
        f"<td>{_FAELLIGKEITSKLASSE_BADGE.get(p.faelligkeitsklasse, '')}</td>"
        f"<td><a href='/backoffice/konto/{h(p.konto_id)}'>Konto</a></td>"
        "</tr>"
    )


def _rueckstaende_mahnfall_zeile_html(m) -> str:
    return (
        "<tr>"
        f"<td>{h(m.objekt_bezeichnung)}</td><td>{h(m.vertrag_id)}</td>"
        f"<td>#{m.forderung_op_position_id}</td>"
        f"<td>{m.stufe}</td><td>{h(m.status)}</td>"
        f"<td>{eur(m.betrag_cent)}</td>"
        f"<td>{m.geplant_am.date().isoformat()}</td>"
        "</tr>"
    )


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, objekt_id: str | None = None, session=Depends(_current_session)) -> HTMLResponse:
    """Zentrale Rückstandsübersicht - Standard "Alle Objekte" (nur
    erlaubte, nicht ausgeschlossene), mit gemeinsamem Objektfilter, der
    Summen/Mietkontentabelle/Einzelpositionen/Mahnsperren-Anzeige
    identisch mitfiltert (Auftrag 13.09.2026, HV-20260913-RUECKSTAENDE:
    "Aktuell ist /backoffice/ ohne Objekt leer" - alle vier Ansichten
    stammen jetzt aus GENAU EINER Berechnung,
    `rueckstaende.service.berechne_rueckstandsuebersicht`, REIN LESEND,
    keine Mahnplanung als GET-Seiteneffekt)."""

    ctx = _ctx(session)
    try:
        uebersicht = berechne_rueckstandsuebersicht(
            ctx=ctx, objekt_id=objekt_id or None, stammdaten_repository=_stammdaten_repo, op_service=_op_service,
            mahn_fall_repository=_mahn_fall_repo,
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Rückstandsübersicht", str(exc), "/backoffice/")

    auswahl_form = _rueckstaende_objekt_filter_form(uebersicht)
    kpi_html = _rueckstaende_kpi_html(uebersicht.kennzahlen)

    if uebersicht.objekt_filter is None:
        titel_zusatz = "Alle Objekte"
    else:
        gefiltertes_objekt = next((o for o in uebersicht.objekt_optionen if o.id == uebersicht.objekt_filter), None)
        titel_zusatz = (
            f"{h(gefiltertes_objekt.bezeichnung)} ({h(gefiltertes_objekt.id)})"
            if gefiltertes_objekt is not None else h(uebersicht.objekt_filter)
        )
    mietkonten_html = "".join(_rueckstaende_mietkonto_zeile_html(z) for z in uebersicht.mietkonten)
    mietkonten_tabelle = f"""
    <div class="card">
      <h2>Mietkontenübersicht — {titel_zusatz}</h2>
      <p class="muted">"Kontostand" = Eröffnung + Vorschreibungen − Zahlungen/Gutschriften (positiv: offener
         Betrag; negativ: Guthaben). Zwei getrennte Berechnungen desselben Kontos stehen nebeneinander:
         "Fällig (Kontoberechnung)" ist die bestehende Kontostand-Rechnung, "Fällig (Positionen)"/
         "Rest gesamt (Positionen)" ist die Summe der einzelnen offenen Posten weiter unten - beide können
         voneinander abweichen (z. B. bei einer Korrekturbuchung ohne eigene Einzelposition), die Spalte
         "Abweichung" zeigt das dann als Betrag statt es zu verstecken. Eine unbekannte Fälligkeit ist
         NICHT automatisch strittig, und eine bekannte Fälligkeit ist KEINE Mahnfreigabe - eine aktive
         Sperre (Spalte "Hinweise") blockiert unabhängig davon; Mahnfälle stammen aus zuvor bereits
         geplanten Fällen (Tabelle weiter unten), diese Übersicht plant selbst keine neuen.</p>
      <div class="tabelle-scroll">
      <table>
        <tr><th>Objekt</th><th>Vertrag</th><th>Einheit</th><th>Nutzungsstatus</th><th>Mieter</th><th>Konto</th>
            <th>Kontostand (offen/Guthaben)</th><th>Fällig (Kontoberechnung)</th><th>Fällig (Positionen)</th>
            <th>Rest gesamt (Positionen)</th><th>Abweichung</th><th>Hinweise</th><th></th></tr>
        {mietkonten_html or '<tr><td colspan=13 class="muted">Keine Verträge.</td></tr>'}
      </table>
      </div>
    </div>"""

    positionen_html = "".join(_rueckstaende_position_zeile_html(p) for p in uebersicht.offene_positionen)
    positionen_tabelle = f"""
    <div class="card">
      <h2>Offene Einzelpositionen — {titel_zusatz}</h2>
      <p class="muted">Jede Zeile ist ein einzelner offener Posten (nicht der Kontosaldo) - eine Zahlung
         wird zuerst der ältesten offenen Position zugeordnet, gezeigt wird nur der danach verbleibende
         Rest. OP-Nr. und Beleg identifizieren die zugrunde liegende Buchung eindeutig, auch wenn mehrere
         Positionen ähnlich aussehen.</p>
      <div class="tabelle-scroll">
      <table>
        <tr><th>Objekt</th><th>Vertrag</th><th>Mieter</th><th>OP-Nr.</th><th>Beleg</th><th>Art</th>
            <th>Zeitraum</th><th>Belegdatum</th><th>Fälligkeit</th><th>Rest</th><th>Status</th><th></th></tr>
        {positionen_html or '<tr><td colspan=12 class="muted">Keine offenen Positionen.</td></tr>'}
      </table>
      </div>
    </div>"""

    mahnfaelle_html = "".join(_rueckstaende_mahnfall_zeile_html(m) for m in uebersicht.mahnfaelle)
    mahnfaelle_tabelle = f"""
    <div class="card">
      <h2>Mahnfälle — {titel_zusatz}</h2>
      <p class="muted">Alle bereits geplanten Mahnfälle je Forderung, nicht nur der zuletzt angelegte - so
         bleiben auch ältere Stufen/Forderungen nachvollziehbar. Der Fallbetrag ist der ursprünglich
         festgehaltene Betrag zum Planungszeitpunkt und fließt in KEINE Summe oben ein. Diese Übersicht
         plant selbst keine neuen Mahnfälle.</p>
      <div class="tabelle-scroll">
      <table>
        <tr><th>Objekt</th><th>Vertrag</th><th>OP-Nr.</th><th>Stufe</th><th>Status</th><th>Fallbetrag</th><th>Geplant am</th></tr>
        {mahnfaelle_html or '<tr><td colspan=7 class="muted">Keine Mahnfälle.</td></tr>'}
      </table>
      </div>
    </div>"""

    bestand_html = "".join(
        f"<tr><td>{h(e.objekt_bezeichnung)}</td><td>{h(e.einheit_id)}</td><td>{h(e.einheit_bezeichnung)}</td>"
        f"<td>{h(e.nutzungsstatus)}</td></tr>"
        for e in uebersicht.einheiten_ohne_konto
    )
    bestand_tabelle = f"""
    <div class="card">
      <h2>Einheiten ohne Mietkonto — {titel_zusatz}</h2>
      <p class="muted">Nutzungsstatus wird eingespielt/gepflegt, unabhängig davon, ob eine Mietforderung
         besteht (z. B. Leerstand, Kurzzeitvermietung, Selfstorage, Eigennutzung) - das ist BESTAND, kein
         erfundener Nullsaldo/Rückstand.</p>
      <div class="tabelle-scroll">
      <table>
        <tr><th>Objekt</th><th>Einheit</th><th>Bezeichnung</th><th>Nutzungsstatus</th></tr>
        {bestand_html or '<tr><td colspan=4 class="muted">Keine Einheiten ohne Mietkonto.</td></tr>'}
      </table>
      </div>
    </div>"""

    return _layout(
        request, session, "Rückstandsübersicht",
        auswahl_form + kpi_html + mietkonten_tabelle + positionen_tabelle + mahnfaelle_tabelle + bestand_tabelle,
    )


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


def _rechenweg_html(positionen) -> str:
    """Reine Darstellung des bestehenden `OPService.berechne_saldo`-
    Ergebnisses als Formel - ändert/berechnet NICHTS selbst, sondern
    gruppiert dieselben `positionen` (aus `OPSaldo.positionen`, also
    identisch mit dem tatsächlich verwendeten Rechenweg) nur nach Typ
    für die Anzeige (Auftrag 13.09., HV-20260913-DASHBOARD: "Rechenweg
    Eröffnung + Vorschreibungen - Zahlungen/Gutschriften zeigen")."""

    summen = {"EROEFFNUNG": 0, "SOLL": 0, "RUECKLASTSCHRIFT": 0, "GUTSCHRIFT": 0, "ZAHLUNG": 0, "KORREKTUR": 0}
    for p in positionen:
        if p.typ in summen:
            summen[p.typ] += p.betrag_cent
    vorschreibungen = summen["SOLL"] + summen["RUECKLASTSCHRIFT"]
    formel = (
        f"Eröffnung {eur(summen['EROEFFNUNG'])} + Vorschreibungen/Nachbelastungen {eur(vorschreibungen)} "
        f"− Zahlungen {eur(summen['ZAHLUNG'])} − Gutschriften {eur(summen['GUTSCHRIFT'])}"
    )
    if summen["KORREKTUR"]:
        formel += f" + Korrekturen {eur(summen['KORREKTUR'])}"
    return formel


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
      <p class="muted">Eine aktive Sperre blockiert die Mahnung UNABHÄNGIG davon, ob ein Teil des
         Kontostands eine bekannte, bereits verstrichene Fälligkeit hat - "bekannte Fälligkeit" ist
         keine Mahnfreigabe.</p>
    </div>"""

    zeilen = "".join(_op_zeile_html(p) for p in positionen)
    op_tabelle = f"""
    <div class="card">
      <h2>Kontoauszug — {h(konto_id)}</h2>
      <p>Kontostand (offen/Guthaben): <strong>{eur(saldo.saldo_cent)}</strong> &nbsp;|&nbsp;
         Davon mit bekannter Fälligkeit: <strong>{eur(saldo.faelliger_unstrittiger_rest_cent)}</strong></p>
      <p class="muted">Rechenweg: {_rechenweg_html(saldo.positionen)}</p>
      {aktion}
      <table>
        <tr><th>#</th><th>Typ</th><th>Betrag</th><th>Belegdatum</th><th>Fälligkeit</th><th>Status</th>
            <th>Beleg-Referenz</th><th>Änderungsgrund</th><th></th></tr>
        {zeilen if positionen else '<tr><td colspan=9 class="muted">Keine Buchungen.</td></tr>'}
      </table>
      <p class="muted">Zeilen ohne bekannte Fälligkeit werden nie automatisch gemahnt (siehe Mahnvorschau) -
         eine UNBEKANNTE Fälligkeit gilt dabei NICHT automatisch als strittig, sie ist lediglich (noch)
         nicht Teil von "Davon mit bekannter Fälligkeit".</p>
    </div>"""

    links = ""
    if not gesperrt and vertrag is not None:
        links = f"""
        <p>
          <a href="/backoffice/vertrag/{h(vertrag.id)}/vorschreibung">Vorschreibungsentwurf</a> &nbsp;|&nbsp;
          <a href="/backoffice/vertrag/{h(vertrag.id)}/mahnvorschau">Mahnvorschau</a> &nbsp;|&nbsp;
          <a href="/backoffice/vertrag/{h(vertrag.id)}/pruefung">Vertragsprüfung</a> &nbsp;|&nbsp;
          <a href="/backoffice/vertrag/{h(vertrag.id)}/mieweg-vorschau">MieWeG-Vorschau</a> &nbsp;|&nbsp;
          <a href="/backoffice/vertrag/{h(vertrag.id)}/komponenten-freigabe">Netto-Mietanteil-Freigabe</a>
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

    return bank_freigabe_ableiten(_bank_repo, _bank_service, gesellschaft_id, vertrag_id)


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
            if _settings.send_enabled and _settings.hv_mail_allowlist_bestaetigt and _hv_mail.client:
                sende_check += f"""<form method="post" action="/backoffice/mahnfall/{ergebnis.mahnfall_id}/versenden" class="inline">
                  {csrf_feld(session.csrf_token)}
                  <button type="submit">Mahnung senden</button>
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
      <p class="muted">Die Vorschau versendet keine Nachricht. Der separate Versand prüft unmittelbar davor den aktuellen Bank- und Forderungsstand.</p>
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


@router.post("/mahnfall/{mahnfall_id}/versenden", response_class=HTMLResponse)
def mahnfall_versenden(request: Request, mahnfall_id: int, csrf_token: str = Form(...), session=Depends(_current_session)):
    _verify_csrf(session, csrf_token)
    try:
        result = _hv_mail.mahnung_senden(ctx=_ctx(session), row_id=mahnfall_id, heute=heute_wien())
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Mahnversand", str(exc), "/backoffice/mailversand")
    return _layout(request, session, "Mahnversand", flash_ok(f"{result.status} — {result.grund}") +
        '<p><a href="/backoffice/mailversand">Versandnachweise ansehen</a></p>')


@router.get("/mailversand", response_class=HTMLResponse)
def mailversand_uebersicht(request: Request, session=Depends(_current_session)):
    ready = bool(_hv_mail.client and _settings.hv_mail_allowlist_bestaetigt)
    modes = [("Mahnungen", _settings.send_enabled),
             ("Indexanpassungen", _settings.indexautomatik_send_enabled),
             ("Vertragsende an Markus", _settings.vertragsende_erinnerung_send_enabled)]
    modes_html = " · ".join(f"{name}: {'aktiv' if ready and enabled else 'gesperrt'}" for name, enabled in modes)
    rows = []
    for row in _hv_mail.versanduebersicht(ctx=_ctx(session)):
        sent = row["versendet_am"]
        if sent and sent.tzinfo is None:
            sent = sent.replace(tzinfo=timezone.utc)
        if sent:
            from zoneinfo import ZoneInfo
            sent_text = sent.astimezone(ZoneInfo("Europe/Vienna")).strftime("%d.%m.%Y %H:%M")
        else:
            sent_text = "Noch nicht belegt"
        evidence = h(row["nachweis"]) if row["nachweis"] else "—"
        if row["provider_referenz"]:
            evidence += f'<details><summary>Microsoft-Nachweis</summary>{h(row["provider_referenz"])}</details>'
        rows.append(f'<tr><td>{h(row["art"])}</td><td>{h(row["objekt"])}, {h(row["einheit"])}</td>'
            f'<td>{h(row["status"])}</td><td>{sent_text}</td><td>{evidence}</td></tr>')
    content = f'''<div class="card"><h1>Mailversand und Nachweise</h1>
      <p>Absender: hausverwaltung@jlb-immo.at · Originale JLB-Signatur</p>
      <p>{modes_html}</p>
      <p>Ein belegter Versand bestätigt noch keinen rechtlich ausreichenden Zugang beim Mieter.
      Unklare Versandfälle werden abgefragt und nicht automatisch erneut versendet.</p>
      <form method="post" action="/backoffice/mailversand/status-abgleichen">
        {csrf_feld(session.csrf_token)}<button type="submit">Offene Versandnachweise abfragen</button>
      </form></div><div class="card"><table>
      <tr><th>Art</th><th>Objekt / Einheit</th><th>Status</th><th>Versandzeit Wien</th><th>Nachweis</th></tr>
      {''.join(rows) if rows else '<tr><td colspan="5">Noch keine Versandvorgänge vorhanden.</td></tr>'}
      </table></div>'''
    return _layout(request, session, "Mailversand", content)


@router.post("/mailversand/status-abgleichen")
def mailversand_status_abgleichen(request: Request, csrf_token: str = Form(...), session=Depends(_current_session)):
    _verify_csrf(session, csrf_token)
    _hv_mail.status_abgleichen(ctx=_ctx(session))
    return RedirectResponse("/backoffice/mailversand", status_code=303)


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


# -- MieWeG-2026-Berechnungsvorschau (Auftrag 12.09., Paket C) ---------------------
#
# AUSDRÜCKLICH nur eine Berechnungsvorschau: keine Route hier löst eine
# Vorschreibung, Freigabe oder einen Versand aus - siehe
# `mieweg_vorschau/service.py`.


def _parse_vpi_zeilen(rohtext: str) -> dict[int, VpiWert]:
    """Format je Zeile: `JAHR;WERT;QUELLE;DATUM` - bewusst eine einfache
    Textform statt dynamischer JS-Zeilen, um die Bedienseite klein zu
    halten. Leere Zeilen werden übersprungen; eine fehlerhafte Zeile
    wird NICHT still ignoriert, sondern als ValueError gemeldet (kein
    erfundener VPI-Wert)."""

    ergebnis: dict[int, VpiWert] = {}
    for zeilennummer, zeile in enumerate(rohtext.splitlines(), start=1):
        zeile = zeile.strip()
        if not zeile:
            continue
        teile = [teil.strip() for teil in zeile.split(";")]
        if len(teile) != 4:
            raise ValueError(
                f"VPI-Zeile {zeilennummer} ('{zeile}') hat nicht das Format JAHR;WERT;QUELLE;DATUM."
            )
        jahr_text, wert_text, quelle, datum = teile
        try:
            jahr = int(jahr_text)
            Decimal(wert_text.replace(",", "."))  # nur Gültigkeit prüfen, Service parst erneut
        except (ValueError, InvalidOperation) as exc:
            raise ValueError(f"VPI-Zeile {zeilennummer} ('{zeile}') hat kein gültiges Jahr/keinen gültigen Wert.") from exc
        ergebnis[jahr] = VpiWert(wert=wert_text.replace(",", "."), quelle=quelle, datum=datum)
    return ergebnis


def _mieweg_vorschau_zeile_html(v) -> str:
    ergebnis = json.loads(v.ergebnis_json)
    hoechstbetrag = (
        eur(v.massgeblicher_hoechstbetrag_cent) if v.massgeblicher_hoechstbetrag_cent is not None else "-"
    )
    gesetzliche_grenze = (
        eur(ergebnis["gesetzliche_hoechstgrenze_cent"]) if ergebnis.get("gesetzliche_hoechstgrenze_cent") is not None else "-"
    )
    vertragsspur = (
        eur(ergebnis["vertraglich_zulaessiger_betrag_cent"])
        if ergebnis.get("vertraglich_zulaessiger_betrag_cent") is not None
        else "-"
    )
    aktuell_verrechnet = (
        eur(ergebnis["aktuell_verrechneter_betrag_cent"])
        if ergebnis.get("aktuell_verrechneter_betrag_cent") is not None
        else "-"
    )
    ausfuehrbare_erhoehung = (
        eur(ergebnis["ausfuehrbare_erhoehung_cent"]) if ergebnis.get("ausfuehrbare_erhoehung_cent") is not None else "-"
    )
    termin = v.fruehester_termin.isoformat() if v.fruehester_termin else "-"
    offene_nachweise = ergebnis.get("offene_nachweise") or []
    blockiert_grund = ergebnis.get("blockiert_grund")
    status_html = (
        f'<span class="error">{h(blockiert_grund)}</span>'
        if blockiert_grund
        else ("<span class=\"ok\">vollständig</span>" if v.vollstaendig else '<span class="warn">Prüfbedarf</span>')
    )
    nachweise_html = "<br>".join(h(n) for n in offene_nachweise) or "-"
    return (
        "<tr>"
        f"<td>{v.version}</td><td>{h(v.rechtsordnung)}</td><td>{v.ziel_bewertungsjahr or '-'}</td>"
        f"<td>{gesetzliche_grenze}</td><td>{vertragsspur}</td><td>{hoechstbetrag}</td>"
        f"<td>{aktuell_verrechnet}</td><td>{ausfuehrbare_erhoehung}</td><td>{h(termin)}</td>"
        f"<td>{status_html}</td><td>{nachweise_html}</td>"
        f"<td>{h(v.erstellt_von)}</td><td>{v.erstellt_am.isoformat() if v.erstellt_am else ''}</td>"
        "</tr>"
    )


@router.get("/vertrag/{vertrag_id}/mieweg-vorschau", response_class=HTMLResponse)
def mieweg_vorschau_uebersicht(request: Request, vertrag_id: str, session=Depends(_current_session)) -> HTMLResponse:
    vertrag = _stammdaten_repo.get_vertrag(vertrag_id)
    if vertrag is None:
        return _fehlerseite(session, "MieWeG-Vorschau", f"Unbekannter Vertrag {vertrag_id}.")
    if _objekt_fuer_vertrag_gesperrt(vertrag_id):
        return _fehlerseite(session, "MieWeG-Vorschau", "Objekt ist gesperrt; keine Vorschau möglich.")

    rechtsordnung_optionen = "".join(
        option(r, r, selected=(r == vertrag.rechtsordnung)) for r in _RECHTSORDNUNGEN_FUER_AUSWAHL
    )
    komponenten = _stammdaten_repo.list_aktive_komponenten(vertrag_id, date.today())
    indexierbare = [k for k in komponenten if k.indexierbar]
    if indexierbare:
        komponenten_html = "".join(
            f'<label class="muted"><input type="checkbox" name="basis_komponenten_ids" value="{h(k.id)}"> '
            f"{h(k.bezeichnung)} ({eur(k.betrag_cent)})</label><br>"
            for k in indexierbare
        )
    else:
        komponenten_html = (
            '<p class="muted">Keine als indexierbar markierte Komponente vorhanden - der Basisbetrag '
            "unten wird dann ohne Komponentenreferenz erfasst.</p>"
        )
    nicht_indexierbare = [k for k in komponenten if not k.indexierbar]
    if nicht_indexierbare:
        komponenten_html += (
            '<p class="muted">Nicht indexierbar (nie automatisch einbezogen): '
            + ", ".join(h(k.bezeichnung) for k in nicht_indexierbare) + "</p>"
        )

    historie = _mieweg_vorschau_service.liste_fuer_vertrag(vertrag_id)
    historie_html = "".join(_mieweg_vorschau_zeile_html(v) for v in historie) or (
        '<tr><td colspan=11 class="muted">Noch keine Vorschau erstellt.</td></tr>'
    )

    inhalt = f"""
    <div class="card">
      <h1>MieWeG-2026-Berechnungsvorschau — {h(vertrag_id)}</h1>
      <p class="muted">AUSDRÜCKLICH eine reine Berechnungsvorschau - keine Vorschreibung, keine Freigabe,
         kein Versand. Jede Vorschau ist eine neue, unveränderliche Version. Aktuelle Rechtsordnung
         (wirksam laut Vertragsprüfung): <strong>{h(vertrag.rechtsordnung)}</strong>.</p>
    </div>

    <div class="card">
      <h2>Neue Vorschau erstellen</h2>
      <form method="post" action="/backoffice/vertrag/{h(vertrag_id)}/mieweg-vorschau/erstellen">
        {csrf_feld(session.csrf_token)}
        <fieldset>
          <legend>Rechtsprofil (explizit, keine automatische Einstufung)</legend>
          <label>Rechtsordnung</label>
          <select name="rechtsordnung" required>{rechtsordnung_optionen}</select>
          <label><input type="checkbox" name="ist_wohnungsnutzung" value="1"> Wohnungsnutzung bestätigt
            (MieWeG §1 Abs1 gilt nur für Wohnungen - ohne Bestätigung kein Wohnungsrechner-Fall, auch
            nicht bei MRG-Vollanwendung/-Teilanwendung eines Geschäftsraums)</label><br>
          <label><input type="checkbox" name="mrg_zinsbeschraenkung" value="1"> MRG-Zinsbeschränkung
            (Richtwert-/Kategoriemiete) - nur bei MRG-Vollanwendung, aktiviert den Übergangsdeckel
            2025 (1%)/2026 (2%)</label><br>
          <label><input type="checkbox" name="ist_altvertrag" value="1"> Altvertrag (Bezugsmonat statt
            Abschlussdatum, erste Modellbewertung frühestens April 2026)</label>
        </fieldset>
        <fieldset>
          <legend>Gesetzliche Spur - Eingaben</legend>
          <p class="muted">Bei Altvertrag: Bezugsjahr/-monat des tatsächlich ZULETZT verwendeten
             Indexwertes, NICHT das Mietbeginn-/Erhöhungsschreiben-Datum.</p>
          <label>Bezugsjahr (Abschlussjahr bzw. Jahr des zuletzt verwendeten Indexwertes)</label>
          <input type="number" name="bezugsjahr" min="1990" max="2100">
          <label>Bezugsmonat (1-12)</label>
          <input type="number" name="bezugsmonat" min="1" max="12">
          <label><input type="checkbox" name="letzte_basis_war_jahresdurchschnitt" value="1"> Die
            bisherige Basis war selbst ein Jahresdurchschnitt (statt eines Einzelmonats) - wird dann wie
            Bezugsmonat Dezember behandelt</label>
          <label>Ziel-Bewertungsjahr (1. April dieses Jahres)</label>
          <input type="number" name="ziel_bewertungsjahr" min="1990" max="2100">
          <label>Basisbetrag (EUR, BRUTTO, letzter unveränderter Betrag)</label>
          <input type="text" name="basis_betrag" placeholder="Betrag EUR">
          <label>Einbezogene, explizit indexierte Komponenten</label>
          {komponenten_html}
          <label>VPI-2020-Jahresdurchschnittswerte (eine Zeile je Jahr: JAHR;WERT;QUELLE;DATUM)</label>
          <textarea name="vpi_zeilen" rows="4" placeholder="2023;100.0;Statistik Austria VPI 2020;2026-01-15"></textarea>
        </fieldset>
        <fieldset>
          <legend>Vertragsspur - manuell geprüfter, vertraglich zulässiger Betrag</legend>
          <p class="muted">Wird NICHT aus den VPI-Daten hergeleitet - die individuelle Vertragsklausel
             bleibt Fachprüfung. Ohne Betrag bleibt diese Spur offen (Prüfbedarf).</p>
          <label>Vertraglich zulässiger Betrag (EUR, BRUTTO)</label>
          <input type="text" name="vertraglicher_betrag" placeholder="Betrag EUR">
          <label>Quellenbeleg (Pflicht, sobald ein Betrag erfasst wird)</label>
          <input type="text" name="vertraglicher_quellenbeleg" placeholder="z. B. Mietvertrag-2024.pdf, Wertsicherungsklausel">
          <label>Vertraglich frühestmöglicher Termin</label>
          <input type="date" name="vertraglicher_termin">
        </fieldset>
        <fieldset>
          <legend>Aktuell verrechneter Betrag - GETRENNT vom historischen Basisbetrag</legend>
          <p class="muted">Der historische Basisbetrag oben ist NICHT automatisch der heute tatsächlich
             verrechnete Betrag - zwischen dem historischen Bezugszeitpunkt und heute können bereits
             (teilweise) Erhöhungen umgesetzt worden sein. Ohne diesen Vergleichswert (mit Datum/Beleg)
             wird KEINE ausführbare Erhöhung ausgewiesen, nur die gesetzliche/vertragliche Obergrenze -
             sonst würde ein bereits verrechneter Teil ein zweites Mal aufgeschlagen.</p>
          <label>Aktuell verrechneter Betrag (EUR, BRUTTO - dieselbe Grundlage wie alle Beträge oben)</label>
          <input type="text" name="aktuell_verrechneter_betrag" placeholder="Betrag EUR">
          <label>Quellenbeleg (Pflicht, sobald ein Betrag erfasst wird)</label>
          <input type="text" name="aktuell_verrechnet_quellenbeleg" placeholder="z. B. Vorschreibung 2026-03.pdf">
          <label>Stichtag des aktuell verrechneten Betrags</label>
          <input type="date" name="aktuell_verrechnet_stichtag">
        </fieldset>
        <fieldset>
          <legend>Nachweise</legend>
          <label>Zustellnachweis-Referenz (ohne diese bleibt jedes Ergebnis reine Vorschau)</label>
          <input type="text" name="zustellnachweis_referenz">
          <label>Kommentar</label>
          <input type="text" name="kommentar">
        </fieldset>
        <button type="submit">Vorschau berechnen und speichern</button>
      </form>
    </div>

    <div class="card">
      <h2>Historie</h2>
      <table>
        <tr>
          <th>Version</th><th>Rechtsordnung</th><th>Ziel-Jahr</th><th>Gesetzliche Grenze</th>
          <th>Vertragsspur</th><th>Maßgeblich</th><th>Aktuell verrechnet</th><th>Ausführbare Erhöhung</th>
          <th>Frühester Termin</th><th>Status</th>
          <th>Offene Nachweise</th><th>Von</th><th>Am</th>
        </tr>
        {historie_html}
      </table>
    </div>
    <p><a href="/backoffice/vertrag/{h(vertrag_id)}/pruefung">&larr; zur Vertragsprüfung</a></p>
    """
    return _layout(request, session, "MieWeG-Vorschau", inhalt)


@router.post("/vertrag/{vertrag_id}/mieweg-vorschau/erstellen")
def mieweg_vorschau_erstellen(
    request: Request,
    vertrag_id: str,
    rechtsordnung: str = Form(...),
    ist_wohnungsnutzung: str = Form(""),
    mrg_zinsbeschraenkung: str = Form(""),
    ist_altvertrag: str = Form(""),
    bezugsjahr: str = Form(""),
    bezugsmonat: str = Form(""),
    letzte_basis_war_jahresdurchschnitt: str = Form(""),
    ziel_bewertungsjahr: str = Form(""),
    basis_betrag: str = Form(""),
    basis_komponenten_ids: list[str] = Form([]),
    vpi_zeilen: str = Form(""),
    vertraglicher_betrag: str = Form(""),
    vertraglicher_quellenbeleg: str = Form(""),
    vertraglicher_termin: str = Form(""),
    aktuell_verrechneter_betrag: str = Form(""),
    aktuell_verrechnet_quellenbeleg: str = Form(""),
    aktuell_verrechnet_stichtag: str = Form(""),
    zustellnachweis_referenz: str = Form(""),
    kommentar: str = Form(""),
    csrf_token: str = Form(...),
    session=Depends(_current_session),
):
    _verify_csrf(session, csrf_token)
    vertrag = _stammdaten_repo.get_vertrag(vertrag_id)
    if vertrag is None:
        return _fehlerseite(session, "MieWeG-Vorschau", f"Unbekannter Vertrag {vertrag_id}.")
    try:
        basis_betrag_cent = parse_eur_betrag(basis_betrag) if (basis_betrag or "").strip() else None
        vertraglich_zulaessiger_betrag_cent = (
            parse_eur_betrag(vertraglicher_betrag) if (vertraglicher_betrag or "").strip() else None
        )
        vpi_jahresdurchschnitte = _parse_vpi_zeilen(vpi_zeilen)
        vertraglicher_fruehestmoeglicher_termin = (
            date.fromisoformat(vertraglicher_termin) if (vertraglicher_termin or "").strip() else None
        )
        aktuell_verrechneter_betrag_cent = (
            parse_eur_betrag(aktuell_verrechneter_betrag) if (aktuell_verrechneter_betrag or "").strip() else None
        )
        aktuell_verrechnet_stichtag_datum = (
            date.fromisoformat(aktuell_verrechnet_stichtag) if (aktuell_verrechnet_stichtag or "").strip() else None
        )
        _mieweg_vorschau_service.vorschau_erstellen(
            ctx=_ctx(session), vertrag=vertrag, rechtsordnung=rechtsordnung,
            ist_wohnungsnutzung=bool(ist_wohnungsnutzung),
            mrg_zinsbeschraenkung=bool(mrg_zinsbeschraenkung), ist_altvertrag=bool(ist_altvertrag),
            bezugsjahr=int(bezugsjahr) if (bezugsjahr or "").strip() else None,
            bezugsmonat=int(bezugsmonat) if (bezugsmonat or "").strip() else None,
            letzte_basis_war_jahresdurchschnitt=bool(letzte_basis_war_jahresdurchschnitt),
            ziel_bewertungsjahr=int(ziel_bewertungsjahr) if (ziel_bewertungsjahr or "").strip() else None,
            basis_betrag_cent=basis_betrag_cent, basis_komponenten_ids=basis_komponenten_ids,
            vpi_jahresdurchschnitte=vpi_jahresdurchschnitte,
            vertraglich_zulaessiger_betrag_cent=vertraglich_zulaessiger_betrag_cent,
            vertraglicher_quellenbeleg=vertraglicher_quellenbeleg or None,
            vertraglicher_fruehestmoeglicher_termin=vertraglicher_fruehestmoeglicher_termin,
            aktuell_verrechneter_betrag_cent=aktuell_verrechneter_betrag_cent,
            aktuell_verrechnet_quellenbeleg=aktuell_verrechnet_quellenbeleg or None,
            aktuell_verrechnet_stichtag=aktuell_verrechnet_stichtag_datum,
            zustellnachweis_referenz=zustellnachweis_referenz or None, kommentar=kommentar or None,
            akteur=session.user_id,
        )
        _audit_service.log(
            entity_typ="mieweg_vorschau", entity_id=vertrag_id, aktion="erstellt", akteur=session.user_id,
            payload={"rechtsordnung": rechtsordnung, "ziel_bewertungsjahr": ziel_bewertungsjahr},
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "MieWeG-Vorschau", str(exc), f"/backoffice/vertrag/{vertrag_id}/mieweg-vorschau")
    return RedirectResponse(url=f"/backoffice/vertrag/{vertrag_id}/mieweg-vorschau", status_code=303)


# -- Indexautomatik (Auftrag 13.09., HV-20260913-INDEXAUTOMATIK) -------------------
#
# Rechtsprofil-Freigabe, Monatslauf-Übersicht, Erhöhungsschreiben-Outbox,
# VPI-Werte-Pflege und Vertragsende-Erinnerungen. Der eigentliche
# Monats-/Tageslauf läuft NICHT über HTTP (siehe scripts/indexautomatik_*.py) -
# dieser Abschnitt ist ausschließlich Einsicht/Freigabe/manuelle Pflege.


def _rechtsprofil_zeile_html(p) -> str:
    def ja_nein(wert, geprueft=True):
        return "ungeklärt" if not geprueft or wert is None else "Ja" if wert else "Nein"
    aktion = ""
    if p.status == "ENTWURF":
        aktion = f"""<form method="post" action="/backoffice/indexautomatik/rechtsprofil/{p.id}/freigeben" class="inline">
          {{csrf}}<button type="submit" class="secondary">Freigeben</button></form>"""
    return (
        "<tr>"
        f"<td>{p.version}</td><td>{h(p.rechtsordnung)}</td><td>{ja_nein(p.ist_wohnungsnutzung)}</td>"
        f"<td>{ja_nein(p.ist_hauptmiete)}</td><td>{ja_nein(p.mrg_zinsbeschraenkung, p.mrg_zinsbeschraenkung_geprueft)}</td>"
        f"<td>{ja_nein(p.foerderbindung, p.foerderbindung_geprueft)}</td>"
        f"<td>{p.bezugsjahr or '-'}-{p.bezugsmonat or '-'}</td>"
        f"<td>{eur(p.vertraglich_zulaessiger_betrag_cent) if p.vertraglich_zulaessiger_betrag_cent is not None else '-'}</td>"
        f"<td>{p.vertragsklausel_id or '-'}</td><td>{h(p.status)}</td><td>{h(p.freigegeben_von or '-')}</td>"
        f"<td>{aktion}</td></tr>"
    )


@router.get("/vertrag/{vertrag_id}/rechtsprofil", response_class=HTMLResponse)
def rechtsprofil_uebersicht(request: Request, vertrag_id: str, session=Depends(_current_session)) -> HTMLResponse:
    vertrag = _stammdaten_repo.get_vertrag(vertrag_id)
    if vertrag is None:
        return _fehlerseite(session, "Rechtsprofil", f"Unbekannter Vertrag {vertrag_id}.")
    if _objekt_fuer_vertrag_gesperrt(vertrag_id):
        return _fehlerseite(session, "Rechtsprofil", "Objekt ist gesperrt; kein Rechtsprofil möglich.")

    rechtsordnung_optionen = "".join(
        option(r, r, selected=(r == vertrag.rechtsordnung)) for r in _RECHTSORDNUNGEN_FUER_AUSWAHL
    )
    komponenten = _stammdaten_repo.list_aktive_komponenten(vertrag_id, date.today())
    komponenten_html = "".join(
        f'<label class="muted"><input type="checkbox" name="basis_komponenten_ids" value="{h(k.id)}"> '
        f"{h(k.bezeichnung)} ({eur(k.betrag_cent)}, {h(k.art)})</label><br>"
        for k in komponenten if k.indexierbar
    ) or '<p class="muted">Keine als indexierbar markierte Komponente vorhanden.</p>'
    klauseln = _indexautomatik.index_repository.freigegebene_klausel(vertrag_id)
    klausel_hinweis = (
        f'<p class="muted">Freigegebene IndexKlausel: #{klauseln.id} (Basis {klauseln.basis_reihe}={klauseln.basis_wert}, '
        f"Bezugsmonat {klauseln.basis_monat})</p>"
        if klauseln else '<p class="muted">Keine freigegebene IndexKlausel für diesen Vertrag vorhanden (siehe Index-Modul).</p>'
    )
    historie = _indexautomatik.rechtsprofil_service.liste_fuer_vertrag(vertrag_id)
    historie_html = "".join(_rechtsprofil_zeile_html(p).replace("{csrf}", csrf_feld(session.csrf_token)) for p in historie) or (
        '<tr><td colspan=12 class="muted">Noch kein Rechtsprofil erfasst.</td></tr>'
    )

    inhalt = f"""
    <div class="card">
      <h1>Rechtsprofil (Indexautomatik) — {h(vertrag_id)}</h1>
      <p class="muted">Nur GENAU EIN freigegebenes Rechtsprofil treibt die monatliche Indexautomatik an.
         Änderungen an Vertrag/Komponenten/Klausel nach der Freigabe entwerten sie automatisch - eine
         neue Freigabe ist dann erforderlich.</p>
    </div>
    <div class="card">
      <h2>Neues Rechtsprofil (Entwurf)</h2>
      {klausel_hinweis}
      <form method="post" action="/backoffice/vertrag/{h(vertrag_id)}/rechtsprofil/erstellen">
        {csrf_feld(session.csrf_token)}
        <fieldset>
          <legend>Rechtsklassifikation</legend>
          <label>Rechtsordnung</label>
          <select name="rechtsordnung" required>{rechtsordnung_optionen}</select>
          <label><input type="checkbox" name="ist_wohnungsnutzung" value="1"> Wohnungsnutzung bestätigt</label><br>
          <label>Haupt-/Untermiete</label>
          <select name="ist_hauptmiete"><option value="">ungeklärt</option><option value="1">Hauptmiete (geprüft)</option>
            <option value="0">Untermiete (geprüft)</option></select>
          <label>MRG-Zinsbeschränkung</label>
          <select name="mrg_zinsbeschraenkung"><option value="">ungeklärt</option>
            <option value="1">Ja (geprüft)</option><option value="0">Nein (geprüft)</option></select><br>
          <label><input type="checkbox" name="ist_altvertrag" value="1"> Altvertrag</label>
        </fieldset>
        <fieldset>
          <legend>Förderbindung/Mietzinsobergrenze (BRUTTO, wirkt unabhängig von foerderbindung als Kappung)</legend>
          <label>Förderbindung</label>
          <select name="foerderbindung"><option value="">ungeklärt</option>
            <option value="1">Ja (geprüft)</option><option value="0">Nein (geprüft)</option></select><br>
          <label>Mietzinsobergrenze (EUR, BRUTTO)</label>
          <input type="text" name="mietzinsobergrenze">
          <label>Quellenbeleg</label>
          <input type="text" name="mietzinsobergrenze_quellenbeleg">
          <label>Gültig bis</label>
          <input type="date" name="mietzinsobergrenze_gueltig_bis">
        </fieldset>
        <fieldset>
          <legend>Letzte tatsächlich verwendete Indexbasis</legend>
          <label>Bezugsjahr</label><input type="number" name="bezugsjahr" min="1990" max="2100">
          <label>Bezugsmonat (1-12)</label><input type="number" name="bezugsmonat" min="1" max="12">
          <label><input type="checkbox" name="letzte_basis_war_jahresdurchschnitt" value="1"> War Jahresdurchschnitt</label>
          <label>VPI-Reihe (gesetzliche MieWeG-Spur, fix)</label>
          <select name="vpi_reihe">{option("VPI20C18","VPI20C18",selected=True)}</select>
          <p class="muted">Nur VPI20C18 (amtlich aktuelle Reihe) - eine abweichende vertragliche Reihe
             gehört als eigene IndexKlausel unten erfasst.</p>
          <label>Referenzierte Basis-Komponenten</label>
          {komponenten_html}
        </fieldset>
        <fieldset>
          <legend>Vertragliche Spur - GENAU EINE der beiden Varianten</legend>
          <label>Statischer vertraglich zulässiger Betrag (EUR, BRUTTO)</label>
          <input type="text" name="vertraglicher_betrag">
          <label>Quellenbeleg</label>
          <input type="text" name="vertraglicher_quellenbeleg">
          <label>Vertraglich frühestmöglicher Termin</label>
          <input type="date" name="vertraglicher_termin">
          <p class="muted">ODER: ID einer freigegebenen IndexKlausel (dynamisch berechnete Spur, siehe oben)</p>
          <label>Vertragsklausel-ID</label>
          <input type="number" name="vertragsklausel_id">
        </fieldset>
        <fieldset>
          <legend>Belege</legend>
          <label>Vertragsbeleg-Referenz (Pflicht)</label>
          <input type="text" name="vertrag_beleg_referenz" required>
          <label>Klauselreferenz</label>
          <input type="text" name="klausel_referenz">
        </fieldset>
        <button type="submit">Rechtsprofil als Entwurf speichern</button>
      </form>
    </div>
    <div class="card">
      <h2>Historie</h2>
      <table>
        <tr><th>Version</th><th>Rechtsordnung</th><th>Wohnung</th><th>Hauptmiete</th><th>MRG-Zinsbeschränkung</th><th>Förderbindung</th><th>Bezug</th>
          <th>Vertragl. Betrag</th><th>Klausel-ID</th><th>Status</th><th>Freigegeben von</th><th>Aktion</th></tr>
        {historie_html}
      </table>
    </div>
    """
    return _layout(request, session, "Rechtsprofil", inhalt)


@router.post("/vertrag/{vertrag_id}/rechtsprofil/erstellen")
def rechtsprofil_erstellen(
    request: Request,
    vertrag_id: str,
    rechtsordnung: str = Form(...),
    ist_wohnungsnutzung: str = Form(""),
    ist_hauptmiete: str = Form(""),
    mrg_zinsbeschraenkung: str = Form(""),
    ist_altvertrag: str = Form(""),
    foerderbindung: str = Form(""),
    mietzinsobergrenze: str = Form(""),
    mietzinsobergrenze_quellenbeleg: str = Form(""),
    mietzinsobergrenze_gueltig_bis: str = Form(""),
    bezugsjahr: str = Form(""),
    bezugsmonat: str = Form(""),
    letzte_basis_war_jahresdurchschnitt: str = Form(""),
    vpi_reihe: str = Form("VPI20C18"),
    basis_komponenten_ids: list[str] = Form([]),
    vertraglicher_betrag: str = Form(""),
    vertraglicher_quellenbeleg: str = Form(""),
    vertraglicher_termin: str = Form(""),
    vertragsklausel_id: str = Form(""),
    vertrag_beleg_referenz: str = Form(...),
    klausel_referenz: str = Form(""),
    csrf_token: str = Form(...),
    session=Depends(_current_session),
):
    _verify_csrf(session, csrf_token)
    try:
        _indexautomatik.rechtsprofil_service.entwurf_anlegen(
            ctx=_ctx(session), vertrag_id=vertrag_id, rechtsordnung=rechtsordnung,
            ist_wohnungsnutzung=bool(ist_wohnungsnutzung),
            ist_hauptmiete=(None if ist_hauptmiete == "" else bool(int(ist_hauptmiete))),
            # Tri-State wie `ist_hauptmiete`: "" = ungeklärt (Wert bleibt
            # False, `_geprueft` bleibt False - eine Freigabe verlangt
            # `_geprueft=True`, siehe rechtsprofil.py); "0"/"1" = geprüfte
            # Angabe.
            mrg_zinsbeschraenkung=bool(int(mrg_zinsbeschraenkung)) if mrg_zinsbeschraenkung != "" else False,
            mrg_zinsbeschraenkung_geprueft=mrg_zinsbeschraenkung != "",
            ist_altvertrag=bool(ist_altvertrag),
            foerderbindung=bool(int(foerderbindung)) if foerderbindung != "" else False,
            foerderbindung_geprueft=foerderbindung != "",
            mietzinsobergrenze_cent=parse_eur_betrag(mietzinsobergrenze) if mietzinsobergrenze.strip() else None,
            mietzinsobergrenze_quellenbeleg=mietzinsobergrenze_quellenbeleg or None,
            mietzinsobergrenze_gueltig_bis=(
                date.fromisoformat(mietzinsobergrenze_gueltig_bis) if mietzinsobergrenze_gueltig_bis.strip() else None
            ),
            bezugsjahr=int(bezugsjahr) if bezugsjahr.strip() else None,
            bezugsmonat=int(bezugsmonat) if bezugsmonat.strip() else None,
            letzte_basis_war_jahresdurchschnitt=bool(letzte_basis_war_jahresdurchschnitt),
            basis_komponenten_ids=basis_komponenten_ids, vpi_reihe=vpi_reihe,
            vertraglich_zulaessiger_betrag_cent=parse_eur_betrag(vertraglicher_betrag) if vertraglicher_betrag.strip() else None,
            vertraglicher_quellenbeleg=vertraglicher_quellenbeleg or None,
            vertraglicher_fruehestmoeglicher_termin=(
                date.fromisoformat(vertraglicher_termin) if vertraglicher_termin.strip() else None
            ),
            vertragsklausel_id=int(vertragsklausel_id) if vertragsklausel_id.strip() else None,
            vertrag_beleg_referenz=vertrag_beleg_referenz, klausel_referenz=klausel_referenz or None,
            erstellt_von=session.user_id,
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Rechtsprofil", str(exc), f"/backoffice/vertrag/{vertrag_id}/rechtsprofil")
    return RedirectResponse(url=f"/backoffice/vertrag/{vertrag_id}/rechtsprofil", status_code=303)


@router.post("/indexautomatik/rechtsprofil/{rechtsprofil_id}/freigeben")
def rechtsprofil_freigeben(request: Request, rechtsprofil_id: int, csrf_token: str = Form(...), session=Depends(_current_session)):
    _verify_csrf(session, csrf_token)
    profil = _indexautomatik.rechtsprofil_repository.get(rechtsprofil_id)
    if profil is None:
        return _fehlerseite(session, "Rechtsprofil", f"Unbekanntes Rechtsprofil {rechtsprofil_id}.")
    try:
        _indexautomatik.rechtsprofil_service.freigeben(rechtsprofil_id, ctx=_ctx(session), freigegeben_von=session.user_id)
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Rechtsprofil", str(exc), f"/backoffice/vertrag/{profil.vertrag_id}/rechtsprofil")
    return RedirectResponse(url=f"/backoffice/vertrag/{profil.vertrag_id}/rechtsprofil", status_code=303)


def _outbox_zeile_html(o) -> str:
    aktion = ""
    if o.status == "BEREIT":
        aktion = f"""<form method="post" action="/backoffice/indexautomatik/outbox/{o.id}/versenden" class="inline">
          {{csrf}}<button type="submit" class="secondary">Versenden</button></form>"""
    elif o.status == "GESENDET":
        aktion = f"""<form method="post" action="/backoffice/indexautomatik/outbox/{o.id}/zugang-bestaetigen" class="inline">
          {{csrf}}
          <select name="zugangsform">{"".join(option(f, f) for f in sorted(_ZUGANGSFORMEN_ALLE))}</select>
          <input type="date" name="zugang_datum" required>
          <input type="text" name="zugang_beleg" placeholder="Belegreferenz" required>
          <button type="submit" class="secondary">Zugang bestätigen</button></form>"""
    gruende = "<br>".join(h(g) for g in (o.blockiert_gruende or [])) or "-"
    schreiben_html = (
        f"<details><summary>Text anzeigen</summary><pre>{h(o.schreiben_text or '(kein Text)')}</pre></details>"
    )
    return (
        "<tr>"
        f"<td>{h(o.vertrag_id)}</td><td>{o.ziel_bewertungsjahr or '-'}</td><td>{o.index_anpassung_id or '-'}</td>"
        f"<td>{eur(o.erhoehung_cent)}</td><td>{o.massgeblicher_termin.isoformat()}</td><td>{h(o.status)}</td>"
        f"<td>{gruende}</td><td>{o.zahlungspflicht_ab.isoformat() if o.zahlungspflicht_ab else '-'}</td>"
        f"<td>{schreiben_html}</td><td>{aktion}</td></tr>"
    )


@router.get("/indexautomatik/outbox", response_class=HTMLResponse)
def indexautomatik_outbox(request: Request, session=Depends(_current_session)) -> HTMLResponse:
    schreiben = _indexautomatik.outbox_repository.liste_alle()
    zeilen = "".join(_outbox_zeile_html(o).replace("{csrf}", csrf_feld(session.csrf_token)) for o in schreiben) or (
        '<tr><td colspan=10 class="muted">Noch kein Erhöhungsschreiben vorhanden.</td></tr>'
    )
    inhalt = f"""
    <div class="card">
      <h1>Erhöhungsschreiben-Outbox</h1>
      <p class="muted">Realer Versand ist erst nach zwei getrennten internen Freigaben (Versand generell
         aktiviert UND die Mailbox-Freischaltung bestätigt) sowie mit einem eingerichteten Versandweg
         möglich - ohne das bleibt "Versenden" eine reine Vorschau/Sperre, es wird niemals ein
         Test-Versand als echt ausgegeben.</p>
      <table>
        <tr><th>Vertrag</th><th>Ziel-Jahr</th><th>IndexAnpassung</th><th>Erhöhung</th><th>Termin</th>
          <th>Status</th><th>Gründe</th><th>Zahlungspflicht ab</th><th>Schreiben</th><th>Aktion</th></tr>
        {zeilen}
      </table>
    </div>
    """
    return _layout(request, session, "Indexautomatik-Outbox", inhalt)


def _lauf_zeile_html(l) -> str:
    gruende = "<br>".join(h(g) for g in (l.blockiert_gruende or [])) or "-"
    return (
        "<tr>"
        f"<td>{h(l.vertrag_id)}</td><td>{h(l.periode)}</td><td>{h(l.status)}</td><td>{gruende}</td>"
        f"<td>{l.erhoehungsschreiben_id or '-'}</td></tr>"
    )


@router.get("/indexautomatik/laeufe", response_class=HTMLResponse)
def indexautomatik_laeufe(request: Request, session=Depends(_current_session)) -> HTMLResponse:
    """Ergänzende Abnahmepunkte (Endprüfung): "Portal braucht die
    internen BLOCKIERT-Gründe der Monatsläufe sichtbar (nicht nur leere
    Outbox)" - ein Fall, der wegen fehlender/unklarer Daten gar nicht
    erst bis zur Outbox kommt, war bisher nur in der Datenbank sichtbar,
    nicht im Backoffice."""

    laeufe = _indexautomatik.lauf_repository.liste_alle()
    zeilen = "".join(_lauf_zeile_html(l) for l in laeufe) or (
        '<tr><td colspan=5 class="muted">Noch kein Monatslauf durchgeführt.</td></tr>'
    )
    inhalt = f"""
    <div class="card">
      <h1>Indexautomatik-Monatsläufe</h1>
      <p class="muted">Jeder Vertrag/Monat, den der Monatslauf geprüft hat - inklusive der Fälle, die
         schon vor einem Erhöhungsschreiben blockiert wurden (z. B. fehlende Belege, ungeprüfte
         Haupt-/Untermiete, fehlender amtlicher Indexwert).</p>
      <table>
        <tr><th>Vertrag</th><th>Periode</th><th>Status</th><th>Gründe</th><th>Erhöhungsschreiben</th></tr>
        {zeilen}
      </table>
    </div>
    """
    return _layout(request, session, "Indexautomatik-Monatsläufe", inhalt)


@router.post("/indexautomatik/outbox/{erhoehungsschreiben_id}/versenden")
def indexautomatik_outbox_versenden(request: Request, erhoehungsschreiben_id: int, csrf_token: str = Form(...), session=Depends(_current_session)):
    _verify_csrf(session, csrf_token)
    heute = heute_wien()
    if _hv_mail.client is None:
        return _fehlerseite(
            session, "Indexautomatik-Outbox",
            "Kein Versandweg eingerichtet - Versand ist strukturell gesperrt. Es wird niemals ein "
            "Test-Versand im Produktivbetrieb durchgeführt.",
            "/backoffice/indexautomatik/outbox",
        )
    try:
        ergebnis = _hv_mail.index_senden(ctx=_ctx(session), row_id=erhoehungsschreiben_id, heute=heute)
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Indexautomatik-Outbox", str(exc), "/backoffice/indexautomatik/outbox")
    inhalt = flash_ok(f"Versand: {ergebnis.status} — {ergebnis.grund}") + (
        '<p><a href="/backoffice/indexautomatik/outbox">&larr; zurück</a></p>'
    )
    return _layout(request, session, "Indexautomatik-Outbox", inhalt)


@router.post("/indexautomatik/outbox/{erhoehungsschreiben_id}/zugang-bestaetigen")
def indexautomatik_outbox_zugang_bestaetigen(
    request: Request,
    erhoehungsschreiben_id: int,
    zugangsform: str = Form(...),
    zugang_datum: str = Form(...),
    zugang_beleg: str = Form(...),
    csrf_token: str = Form(...),
    session=Depends(_current_session),
):
    _verify_csrf(session, csrf_token)
    try:
        _indexautomatik.outbox_service.zugang_bestaetigen(
            ctx=_ctx(session), erhoehungsschreiben_id=erhoehungsschreiben_id, heute=heute_wien(),
            zugang_datum=date.fromisoformat(zugang_datum), zugangsform=zugangsform, zugang_beleg=zugang_beleg,
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Indexautomatik-Outbox", str(exc), "/backoffice/indexautomatik/outbox")
    return RedirectResponse(url="/backoffice/indexautomatik/outbox", status_code=303)


#: Auftrag HV-20260913-VERSAND-SOLL: nur diese drei Status gehören
#: überhaupt in diese Ansicht - alle vorherigen Zustände (ENTWURF bis
#: ZUGANG_BESTAETIGT) laufen weiterhin ausschließlich über die
#: bestehende Outbox-Seite oben.
_SOLL_UMSETZUNG_STATUS = ("SOLL_UMSETZUNG_OFFEN", "SOLL_UMSETZUNG_BLOCKIERT", "SOLL_UMGESETZT")


def _soll_umsetzung_zeile_html(o) -> str:
    aktion = ""
    if o.status in ("SOLL_UMSETZUNG_OFFEN", "SOLL_UMSETZUNG_BLOCKIERT"):
        aktion = f'<a href="/backoffice/indexautomatik/soll-umsetzung/{o.id}">Prüfen/Umsetzen</a>'
    gruende = "<br>".join(h(g) for g in (o.blockiert_gruende or [])) or "-"
    return (
        "<tr>"
        f"<td>{h(o.vertrag_id)}</td><td>{eur(o.erhoehung_cent)}</td>"
        f"<td>{o.zahlungspflicht_ab.isoformat() if o.zahlungspflicht_ab else '-'}</td>"
        f"<td>{h(o.status)}</td><td>{gruende}</td><td>{aktion}</td></tr>"
    )


@router.get("/indexautomatik/soll-umsetzung", response_class=HTMLResponse)
def indexautomatik_soll_umsetzung(request: Request, session=Depends(_current_session)) -> HTMLResponse:
    """Verständliche Übersicht für den bisher letzten fehlenden Schritt
    der Indexautomatik-Pipeline (SOLL_UMSETZUNG_OFFEN -> tatsächliche
    Komponenten-/Rechtsprofiländerung, siehe umsetzung_service.py). GET
    ist rein lesend - keine Statusänderung, kein Claim."""

    # Codex-Rückprüfung (499c36f): "listet ungefiltert alle Gesellschaften
    # statt ctx-Scope" - `_ctx(session)` ist im aktuellen Ein-Operator-
    # Pilotmodul zwar immer ADMIN/gesellschaft_ids=None (siehe `_ctx`-
    # Docstring), aber der Filter gehört trotzdem hierher, konsistent mit
    # jeder anderen Listenroute über mehrere Verträge
    # (`indexautomatik/service.py::monatslauf_alle`,
    # `vertragsende_service.py::plane_alle`) - kein stillschweigend
    # ausgelassener Scope-Check, der bei einer künftigen Mehrbenutzer-
    # Rolle sofort zur Datenlücke würde.
    ctx = _ctx(session)
    alle = [
        o
        for o in _indexautomatik.outbox_repository.liste_alle()
        if o.status in _SOLL_UMSETZUNG_STATUS
        and (vertrag := _stammdaten_repo.get_vertrag(o.vertrag_id)) is not None
        and ctx.has_zugriff(vertrag.gesellschaft_id)
    ]
    zeilen = "".join(_soll_umsetzung_zeile_html(o) for o in alle) or (
        '<tr><td colspan=6 class="muted">Kein Fall mit fälliger/bereits umgesetzter Soll-Umsetzung.</td></tr>'
    )
    hinweis = (
        ""
        if _settings.indexautomatik_soll_umsetzung_enabled
        else '<p class="muted">Soll-Umsetzung ist konfigurationsseitig deaktiviert '
        "(indexautomatik_soll_umsetzung_enabled=false) - \"Jetzt umsetzen\" bleibt bis dahin wirkungslos.</p>"
    )
    inhalt = f"""
    <div class="card">
      <h1>Indexautomatik-Soll-Umsetzung</h1>
      <p class="muted">Ein zugegangenes, wirksam gewordenes Erhöhungsschreiben wird hier tatsächlich in
         Vertragskomponenten und Rechtsprofil-Basis übernommen - erst NACH bestätigtem Zugang und
         erreichter Zahlungspflicht, nie vorher.</p>
      {hinweis}
      <table>
        <tr><th>Vertrag</th><th>Erhöhung</th><th>Wirksam ab</th><th>Status</th><th>Gründe</th><th>Aktion</th></tr>
        {zeilen}
      </table>
    </div>
    """
    return _layout(request, session, "Indexautomatik-Soll-Umsetzung", inhalt)


@router.get("/indexautomatik/soll-umsetzung/{erhoehungsschreiben_id}", response_class=HTMLResponse)
def indexautomatik_soll_umsetzung_detail(
    request: Request, erhoehungsschreiben_id: int, session=Depends(_current_session)
) -> HTMLResponse:
    """Vorschau Soll alt/neu ab Datum bzw. konkreter Blockiergrund - rein
    lesend (`umsetzung_service.vorschau`), keine Statusänderung."""

    try:
        vorschau = _indexautomatik.soll_umsetzung_service.vorschau(
            ctx=_ctx(session), erhoehungsschreiben_id=erhoehungsschreiben_id, heute=heute_wien(),
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Indexautomatik-Soll-Umsetzung", str(exc), "/backoffice/indexautomatik/soll-umsetzung")

    if vorschau["blockiert"]:
        stand_html = (
            "<p><strong>Blockiert - Gründe:</strong></p><ul>"
            + "".join(f"<li>{h(g)}</li>" for g in vorschau["gruende"])
            + "</ul>"
        )
    else:
        zeilen_soll = "".join(
            f"<tr><td>{h(eintrag['komponente_id'])}</td><td>{eur(eintrag['alter_betrag_cent'])}</td>"
            f"<td>{eur(eintrag['neuer_betrag_cent'])}</td><td>{h(vorschau['wirksam_ab'])}</td></tr>"
            for eintrag in vorschau["eintraege"]
        )
        stand_html = f"""
        <table>
          <tr><th>Komponente</th><th>Alter Betrag</th><th>Neuer Betrag</th><th>Wirksam ab</th></tr>
          {zeilen_soll}
        </table>
        """
    aktion_html = ""
    if vorschau["status"] in ("SOLL_UMSETZUNG_OFFEN", "SOLL_UMSETZUNG_BLOCKIERT"):
        aktion_html = f"""
        <form method="post" action="/backoffice/indexautomatik/soll-umsetzung/{erhoehungsschreiben_id}/umsetzen">
          {csrf_feld(session.csrf_token)}
          <button type="submit">Jetzt umsetzen</button>
        </form>
        """
    inhalt = f"""
    <div class="card">
      <h1>Soll-Umsetzung Vertrag {h(vorschau['vertrag_id'])}</h1>
      <p class="muted">Status: {h(vorschau['status'])} — Zahlungspflicht ab: {h(vorschau['zahlungspflicht_ab'] or '-')}</p>
      {stand_html}
      {aktion_html}
      <p><a href="/backoffice/indexautomatik/soll-umsetzung">&larr; zurück</a></p>
    </div>
    """
    return _layout(request, session, "Indexautomatik-Soll-Umsetzung", inhalt)


@router.post("/indexautomatik/soll-umsetzung/{erhoehungsschreiben_id}/umsetzen")
def indexautomatik_soll_umsetzung_umsetzen(
    request: Request, erhoehungsschreiben_id: int, csrf_token: str = Form(...), session=Depends(_current_session)
):
    _verify_csrf(session, csrf_token)
    try:
        ergebnis = _indexautomatik.soll_umsetzung_service.umsetzen(
            ctx=_ctx(session), erhoehungsschreiben_id=erhoehungsschreiben_id, heute=heute_wien(),
            akteur=session.user_id, soll_umsetzung_enabled=_settings.indexautomatik_soll_umsetzung_enabled,
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Indexautomatik-Soll-Umsetzung", str(exc), "/backoffice/indexautomatik/soll-umsetzung")
    inhalt = flash_ok(f"Soll-Umsetzung: {ergebnis.status}" + (f" — {', '.join(ergebnis.gruende)}" if ergebnis.gruende else "")) + (
        '<p><a href="/backoffice/indexautomatik/soll-umsetzung">&larr; zurück</a></p>'
    )
    return _layout(request, session, "Indexautomatik-Soll-Umsetzung", inhalt)


@router.get("/indexautomatik/vpi", response_class=HTMLResponse)
def indexautomatik_vpi(request: Request, session=Depends(_current_session)) -> HTMLResponse:
    werte = _indexautomatik.vpi_repository.jahreswert_liste()
    zeilen = "".join(
        f"<tr><td>{h(w.reihe)}</td><td>{w.jahr}</td><td>{w.wert}</td><td>{h(w.finalitaet)}</td>"
        f"<td>{h(w.quelle)}</td><td>{h(w.erfasst_von)}</td></tr>"
        for w in werte
    ) or '<tr><td colspan=6 class="muted">Noch kein Jahreswert erfasst.</td></tr>'
    inhalt = f"""
    <div class="card">
      <h1>VPI-Jahresdurchschnittswerte (manueller Override)</h1>
      <p class="muted">Der amtliche Monatswerte-Import läuft über die eingerichtete tägliche Pflege -
         hier nur ein manuell belegter Jahresdurchschnitt-Override mit Publikationsbeleg.</p>
      <form method="post" action="/backoffice/indexautomatik/vpi/erfassen">
        {csrf_feld(session.csrf_token)}
        <label>Reihe</label>
        <select name="reihe">{option("VPI20C18","VPI20C18",selected=True)}{option("VPI15C18","VPI15C18")}{option("VPI00","VPI00")}{option("VPI96","VPI96")}</select>
        <label>Jahr</label><input type="number" name="jahr" min="1990" max="2100" required>
        <label>Wert</label><input type="text" name="wert" required>
        <label>Quelle</label><input type="text" name="quelle" required placeholder="z. B. Statistik Austria Pressemitteilung">
        <label>Quelldatum</label><input type="date" name="quelle_datum" required>
        <button type="submit">Jahreswert erfassen</button>
      </form>
    </div>
    <div class="card">
      <h2>Erfasste Jahreswerte</h2>
      <table><tr><th>Reihe</th><th>Jahr</th><th>Wert</th><th>Finalität</th><th>Quelle</th><th>Von</th></tr>{zeilen}</table>
    </div>
    """
    return _layout(request, session, "VPI-Werte", inhalt)


@router.post("/indexautomatik/vpi/erfassen")
def indexautomatik_vpi_erfassen(
    request: Request,
    reihe: str = Form(...),
    jahr: int = Form(...),
    wert: str = Form(...),
    quelle: str = Form(...),
    quelle_datum: str = Form(...),
    csrf_token: str = Form(...),
    session=Depends(_current_session),
):
    _verify_csrf(session, csrf_token)
    try:
        _indexautomatik.vpi_repository.jahreswert_erfassen(
            reihe=reihe, jahr=jahr, wert=Decimal(wert.replace(",", ".")), quelle=quelle,
            quelle_datum=date.fromisoformat(quelle_datum), erfasst_von=session.user_id,
        )
    except (InvalidOperation, ValueError) as exc:
        return _fehlerseite(session, "VPI-Werte", f"Ungültiger Wert: {exc}", "/backoffice/indexautomatik/vpi")
    return RedirectResponse(url="/backoffice/indexautomatik/vpi", status_code=303)


def _vertragsende_zeile_html(e) -> str:
    aktion = ""
    if e.status in ("OFFEN", "BENACHRICHTIGT", "UNKLAR"):
        aktion = f"""<form method="post" action="/backoffice/indexautomatik/vertragsende/{e.id}/entscheiden" class="inline">
          {{csrf}}
          <select name="entscheidung">{"".join(option(v, v) for v in ("VERLAENGERN_PRUEFEN","NICHT_VERLAENGERN_PRUEFEN","RUECKFRAGE"))}</select>
          <button type="submit" class="secondary">Entscheiden</button></form>"""
    elif e.status == "ENTSCHIEDEN" and not e.mieterentwurf_text:
        aktion = f"""<form method="post" action="/backoffice/indexautomatik/vertragsende/{e.id}/mieterentwurf" class="inline">
          {{csrf}}<button type="submit" class="secondary">Mieterentwurf erzeugen</button></form>"""
    entwurf_hinweis = (
        f"<br><details><summary>Entwurf anzeigen (nur intern, nie automatisch versendet)</summary>"
        f"<pre>{h(e.mieterentwurf_text)}</pre></details>"
        if e.mieterentwurf_text else ""
    )
    return (
        "<tr>"
        f"<td>{h(e.vertrag_id)}</td><td>{e.end_datum.isoformat()}</td><td>{e.faellig_am.isoformat()}</td>"
        f"<td>{h(e.status)}</td><td>{h(e.entscheidung or '-')}{entwurf_hinweis}</td><td>{aktion}</td></tr>"
    )


@router.get("/indexautomatik/vertragsende", response_class=HTMLResponse)
def indexautomatik_vertragsende(request: Request, session=Depends(_current_session)) -> HTMLResponse:
    erinnerungen = _indexautomatik.vertragsende_repository.liste_alle()
    zeilen = "".join(_vertragsende_zeile_html(e).replace("{csrf}", csrf_feld(session.csrf_token)) for e in erinnerungen) or (
        '<tr><td colspan=6 class="muted">Noch keine Vertragsende-Erinnerung geplant.</td></tr>'
    )
    inhalt = f"""
    <div class="card">
      <h1>Vertragsende-Erinnerungen</h1>
      <p class="muted">Empfänger ist ausschließlich der intern konfigurierte Eigentümer - nie der Mieter.
         Der Mieter wird ERST nach einer hier gespeicherten Entscheidung überhaupt adressiert, und dann
         höchstens über einen manuell zu prüfenden Entwurf.</p>
      <table>
        <tr><th>Vertrag</th><th>Ende</th><th>Fällig am</th><th>Status</th><th>Entscheidung</th><th>Aktion</th></tr>
        {zeilen}
      </table>
    </div>
    """
    return _layout(request, session, "Vertragsende-Erinnerungen", inhalt)


@router.post("/indexautomatik/vertragsende/{erinnerung_id}/entscheiden")
def indexautomatik_vertragsende_entscheiden(
    request: Request, erinnerung_id: int, entscheidung: str = Form(...), csrf_token: str = Form(...),
    session=Depends(_current_session),
):
    _verify_csrf(session, csrf_token)
    try:
        _indexautomatik.vertragsende_service.entscheiden(
            ctx=_ctx(session), erinnerung_id=erinnerung_id, entscheidung=entscheidung, entschieden_von=session.user_id,
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Vertragsende-Erinnerungen", str(exc), "/backoffice/indexautomatik/vertragsende")
    return RedirectResponse(url="/backoffice/indexautomatik/vertragsende", status_code=303)


@router.post("/indexautomatik/vertragsende/{erinnerung_id}/mieterentwurf")
def indexautomatik_vertragsende_mieterentwurf(request: Request, erinnerung_id: int, csrf_token: str = Form(...), session=Depends(_current_session)):
    _verify_csrf(session, csrf_token)
    try:
        _indexautomatik.vertragsende_service.mieterentwurf_erzeugen(ctx=_ctx(session), erinnerung_id=erinnerung_id)
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Vertragsende-Erinnerungen", str(exc), "/backoffice/indexautomatik/vertragsende")
    return RedirectResponse(url="/backoffice/indexautomatik/vertragsende", status_code=303)


# -- Variable Monatsabrechnung (KURZZEITVERMIETUNG/SELFSTORAGE) --------------
# Auftrag 13.09., HV-20260913-DASHBOARD.


def _parse_optionalen_betrag(text: str | None) -> int | None:
    if not (text or "").strip():
        return None
    return parse_eur_betrag(text)


def _variable_abrechnung_einheiten_optionen(ausgewaehlt: str | None = None) -> str:
    optionen = ['<option value="">-- Einheit wählen --</option>']
    for objekt in _stammdaten_repo.list_objekte():
        for einheit in _stammdaten_repo.list_einheiten_fuer_objekt(objekt.id):
            if einheit.nutzungsstatus not in ("KURZZEITVERMIETUNG", "SELFSTORAGE"):
                continue
            label = f"{einheit.id} — {einheit.bezeichnung} ({objekt.bezeichnung}, {einheit.nutzungsstatus})"
            optionen.append(option(einheit.id, label, selected=(einheit.id == ausgewaehlt)))
    return "".join(optionen)


def _variable_abrechnung_zeile_html(z) -> str:
    netto_anteil_html = eur(z.unser_netto_anteil_cent) if z.unser_netto_anteil_cent is not None else '<span class="muted">unbekannt</span>'
    return (
        "<tr>"
        f"<td>{h(z.einheit_id)}</td><td>{h(z.art)}</td><td>{h(z.leistungsmonat)}</td>"
        f"<td>{h(z.status)}</td><td>v{z.version}</td>"
        f"<td>{netto_anteil_html}</td>"
        f"<td>{eur(z.berichteter_betrag_cent) if z.berichteter_betrag_cent is not None else '-'} "
        f"{h(z.berichteter_betragsart or '')}</td>"
        f"<td>{h(z.quelle_referenz)}</td>"
        f"<td><a href=\"/backoffice/variable-abrechnung/{z.id}/korrigieren\">Korrigieren</a> | "
        f"<a href=\"/backoffice/variable-abrechnung/versionen?einheit_id={h(z.einheit_id)}&art={h(z.art)}&monat={h(z.leistungsmonat)}\">Versionen</a></td>"
        "</tr>"
    )


@router.get("/variable-abrechnung", response_class=HTMLResponse)
def variable_abrechnung_liste(request: Request, monat: str | None = None, session=Depends(_current_session)) -> HTMLResponse:
    zeilen = _variableabrechnung.service.liste_aktuelle(ctx=_ctx(session), leistungsmonat=monat or None)
    zeilen_html = "".join(_variable_abrechnung_zeile_html(z) for z in zeilen) or (
        '<tr><td colspan=9 class="muted">Keine Monatsabrechnung vorhanden.</td></tr>'
    )
    inhalt = f"""
    <div class="card">
      <h1>Variable Monatsabrechnung — KURZZEITVERMIETUNG/SELFSTORAGE</h1>
      <p class="muted">Nur Status BESTAETIGT (mit erfasstem, geprüftem Nettoanteil) fließt in eine
         Erlössumme ein - ein ENTWURF bleibt sichtbar, aber unbestätigt. Kostenfelder sind rein
         erläuternd; die tatsächliche Überweisung an den Eigentümer ist NICHT dasselbe wie der
         Nettomieterlös.</p>
      <form method="get" action="/backoffice/variable-abrechnung">
        <label>Leistungsmonat (YYYY-MM)</label>
        <input type="text" name="monat" value="{h(monat or '')}" placeholder="2026-08">
        <button type="submit">Filtern</button>
      </form>
      <p>
        <a href="/backoffice/variable-abrechnung/erfassen">+ Neu erfassen</a> &nbsp;|&nbsp;
        <a href="/backoffice/variable-abrechnung/import">CSV-Import</a> &nbsp;|&nbsp;
        <a href="/backoffice/dashboard/monatsuebersicht">Monatsübersicht (Nettomieterlös)</a>
      </p>
      <table>
        <tr><th>Einheit</th><th>Art</th><th>Monat</th><th>Status</th><th>Version</th>
            <th>Unser Nettoanteil</th><th>Gemeldeter Betrag</th><th>Quelle</th><th>Aktion</th></tr>
        {zeilen_html}
      </table>
    </div>
    """
    return _layout(request, session, "Variable Monatsabrechnung", inhalt)


def _variable_abrechnung_formularfelder(*, einheit_id: str | None = None, vorbelegung=None) -> str:
    art_optionen = "".join(
        option(a, a, selected=(vorbelegung.art == a if vorbelegung else False)) for a in ("KURZZEITVERMIETUNG", "SELFSTORAGE")
    )
    status_optionen = "".join(
        option(s, s, selected=(vorbelegung.status == s if vorbelegung else s == "ENTWURF")) for s in ("ENTWURF", "BESTAETIGT")
    )
    betragsart_optionen = "".join(
        option(b, b, selected=(vorbelegung.berichteter_betragsart == b if vorbelegung else False)) for b in ("", "BRUTTO", "NETTO", "UNGEKLAERT")
    )

    def _feldwert(name: str) -> str:
        if vorbelegung is None:
            return ""
        wert = getattr(vorbelegung, name)
        return "" if wert is None else str(wert)

    return f"""
        <fieldset>
          <legend>Einheit/Art/Monat</legend>
          <label>Einheit</label>
          <select name="einheit_id" required {'disabled' if vorbelegung else ''}>{_variable_abrechnung_einheiten_optionen(einheit_id or (vorbelegung.einheit_id if vorbelegung else None))}</select>
          {f'<input type="hidden" name="einheit_id" value="{h(vorbelegung.einheit_id)}">' if vorbelegung else ''}
          <label>Art</label>
          <select name="art" required {'disabled' if vorbelegung else ''}>{art_optionen}</select>
          {f'<input type="hidden" name="art" value="{h(vorbelegung.art)}">' if vorbelegung else ''}
          <label>Leistungsmonat (YYYY-MM)</label>
          <input type="text" name="leistungsmonat" value="{h(vorbelegung.leistungsmonat) if vorbelegung else ''}" placeholder="2026-08" required {'readonly' if vorbelegung else ''}>
        </fieldset>
        <fieldset>
          <legend>Beleg/Status</legend>
          <label>Belegdatum</label>
          <input type="date" name="belegdatum" value="{_feldwert('belegdatum')}" required>
          <label>Quellenreferenz (Pflicht)</label>
          <input type="text" name="quelle_referenz" value="{h(_feldwert('quelle_referenz'))}" required>
          <label>Quellen-Hash (optional)</label>
          <input type="text" name="quelle_hash" value="{h(_feldwert('quelle_hash'))}">
          <label>Status</label>
          <select name="status">{status_optionen}</select>
        </fieldset>
        <fieldset>
          <legend>Beträge - berichteter Ursprungsbetrag (KEINE automatische USt-Umrechnung)</legend>
          <label>Berichteter Betrag (EUR)</label>
          <input type="text" name="berichteter_betrag">
          <label>Betragsart</label>
          <select name="berichteter_betragsart">{betragsart_optionen}</select>
          <label>Unser Nettoanteil (EUR, MASSGEBLICH - Pflicht für Status BESTAETIGT)</label>
          <input type="text" name="unser_netto_anteil">
        </fieldset>
        <fieldset>
          <legend>Kostenfelder (REIN ERLÄUFTERND - bereits im Nettoanteil enthalten, wird nicht nochmals abgezogen)</legend>
          <label>Betriebskosten-Hinweis (EUR)</label>
          <input type="text" name="betriebskosten_hinweis">
          <label>Reinigungskosten-Hinweis (EUR)</label>
          <input type="text" name="reinigungskosten_hinweis">
          <label>Verwaltungskosten-Hinweis (EUR)</label>
          <input type="text" name="verwaltungskosten_hinweis">
          <label>Tatsächlicher Zahlungseingang (EUR, NICHT gleich Nettomieterlös)</label>
          <input type="text" name="tatsaechlicher_zahlungseingang">
        </fieldset>
        <fieldset>
          <legend>Belegung (optional)</legend>
          <label>Vermietete Einheiten</label>
          <input type="number" name="vermietete_einheiten" min="0">
          <label>Vermietete Fläche (qm)</label>
          <input type="text" name="vermietete_flaeche_qm">
        </fieldset>
    """


@router.get("/variable-abrechnung/erfassen", response_class=HTMLResponse)
def variable_abrechnung_erfassen_formular(request: Request, session=Depends(_current_session)) -> HTMLResponse:
    inhalt = f"""
    <div class="card" style="max-width:640px;">
      <h1>Variable Monatsabrechnung — neu erfassen</h1>
      <form method="post" action="/backoffice/variable-abrechnung/erfassen">
        {csrf_feld(session.csrf_token)}
        {_variable_abrechnung_formularfelder()}
        <button type="submit">Erfassen</button>
      </form>
    </div>"""
    return _layout(request, session, "Variable Monatsabrechnung erfassen", inhalt)


@router.post("/variable-abrechnung/erfassen")
def variable_abrechnung_erfassen(
    request: Request,
    einheit_id: str = Form(...),
    art: str = Form(...),
    leistungsmonat: str = Form(...),
    belegdatum: str = Form(...),
    quelle_referenz: str = Form(...),
    quelle_hash: str = Form(""),
    status: str = Form("ENTWURF"),
    berichteter_betrag: str = Form(""),
    berichteter_betragsart: str = Form(""),
    unser_netto_anteil: str = Form(""),
    betriebskosten_hinweis: str = Form(""),
    reinigungskosten_hinweis: str = Form(""),
    verwaltungskosten_hinweis: str = Form(""),
    tatsaechlicher_zahlungseingang: str = Form(""),
    vermietete_einheiten: str = Form(""),
    vermietete_flaeche_qm: str = Form(""),
    csrf_token: str = Form(...),
    session=Depends(_current_session),
):
    _verify_csrf(session, csrf_token)
    try:
        _variableabrechnung.service.erfassen(
            ctx=_ctx(session), einheit_id=einheit_id, art=art, leistungsmonat=leistungsmonat,
            belegdatum=date.fromisoformat(belegdatum), quelle_referenz=quelle_referenz,
            quelle_hash=quelle_hash or None, status=status,
            berichteter_betrag_cent=_parse_optionalen_betrag(berichteter_betrag),
            berichteter_betragsart=berichteter_betragsart or None,
            unser_netto_anteil_cent=_parse_optionalen_betrag(unser_netto_anteil),
            betriebskosten_hinweis_cent=_parse_optionalen_betrag(betriebskosten_hinweis),
            reinigungskosten_hinweis_cent=_parse_optionalen_betrag(reinigungskosten_hinweis),
            verwaltungskosten_hinweis_cent=_parse_optionalen_betrag(verwaltungskosten_hinweis),
            tatsaechlicher_zahlungseingang_cent=_parse_optionalen_betrag(tatsaechlicher_zahlungseingang),
            vermietete_einheiten=int(vermietete_einheiten) if vermietete_einheiten.strip() else None,
            vermietete_flaeche_qm=Decimal(vermietete_flaeche_qm.replace(",", ".")) if vermietete_flaeche_qm.strip() else None,
            erstellt_von=session.user_id,
        )
    except (MietinkassoError, ValueError, InvalidOperation) as exc:
        return _fehlerseite(session, "Variable Monatsabrechnung", str(exc), "/backoffice/variable-abrechnung/erfassen")
    return RedirectResponse(url="/backoffice/variable-abrechnung", status_code=303)


@router.get("/variable-abrechnung/{id}/korrigieren", response_class=HTMLResponse)
def variable_abrechnung_korrigieren_formular(request: Request, id: int, session=Depends(_current_session)) -> HTMLResponse:
    zeile = _variableabrechnung.repository.get(id)
    if zeile is None:
        return _fehlerseite(session, "Variable Monatsabrechnung", f"Unbekannte Zeile {id}.", "/backoffice/variable-abrechnung")
    # Unabhängiger Review: "Korrektur-GET" las bislang direkt über das
    # Repository ohne jede ctx-/Scopeprüfung - eine LESEZUGRIFF-Rolle
    # oder ein fremdgesellschafts-gebundener ctx konnte so Fachdaten
    # (Beträge, Quelle, Änderungsgrund) EINER FREMDEN Gesellschaft über
    # das Korrekturformular einsehen. Ein nicht abgefangener
    # `CrossTenantError`/`ObjektAusgeschlossenError` hier würde als
    # rohe 500-Antwort statt einer verständlichen Ablehnung enden -
    # deshalb wie an anderen Stellen dieser Route in `_fehlerseite`
    # übersetzt.
    try:
        objekt = _stammdaten_repo.objekt_fuer_einheit(zeile.einheit_id)
        require_gesellschaft_access(_ctx(session), objekt.gesellschaft_id)
        _stammdaten_repo.pruefe_einheit_nicht_ausgeschlossen(zeile.einheit_id)
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Variable Monatsabrechnung", str(exc), "/backoffice/variable-abrechnung")
    inhalt = f"""
    <div class="card" style="max-width:640px;">
      <h1>Korrektur — {h(zeile.einheit_id)} / {h(zeile.art)} / {h(zeile.leistungsmonat)} (aktuell v{zeile.version})</h1>
      <form method="post" action="/backoffice/variable-abrechnung/{zeile.id}/korrigieren">
        {csrf_feld(session.csrf_token)}
        {_variable_abrechnung_formularfelder(vorbelegung=zeile)}
        <label>Änderungsgrund (Pflicht)</label>
        <input type="text" name="aenderungsgrund" required>
        <button type="submit">Als neue Version speichern</button>
      </form>
    </div>"""
    return _layout(request, session, "Variable Monatsabrechnung korrigieren", inhalt)


@router.post("/variable-abrechnung/{id}/korrigieren")
def variable_abrechnung_korrigieren(
    request: Request,
    id: int,
    belegdatum: str = Form(...),
    quelle_referenz: str = Form(...),
    quelle_hash: str = Form(""),
    status: str = Form("ENTWURF"),
    berichteter_betrag: str = Form(""),
    berichteter_betragsart: str = Form(""),
    unser_netto_anteil: str = Form(""),
    betriebskosten_hinweis: str = Form(""),
    reinigungskosten_hinweis: str = Form(""),
    verwaltungskosten_hinweis: str = Form(""),
    tatsaechlicher_zahlungseingang: str = Form(""),
    vermietete_einheiten: str = Form(""),
    vermietete_flaeche_qm: str = Form(""),
    aenderungsgrund: str = Form(...),
    csrf_token: str = Form(...),
    session=Depends(_current_session),
):
    _verify_csrf(session, csrf_token)
    try:
        _variableabrechnung.service.korrigieren(
            ctx=_ctx(session), ausgehend_von_id=id, aenderungsgrund=aenderungsgrund,
            belegdatum=date.fromisoformat(belegdatum), quelle_referenz=quelle_referenz,
            quelle_hash=quelle_hash or None, status=status,
            berichteter_betrag_cent=_parse_optionalen_betrag(berichteter_betrag),
            berichteter_betragsart=berichteter_betragsart or None,
            unser_netto_anteil_cent=_parse_optionalen_betrag(unser_netto_anteil),
            betriebskosten_hinweis_cent=_parse_optionalen_betrag(betriebskosten_hinweis),
            reinigungskosten_hinweis_cent=_parse_optionalen_betrag(reinigungskosten_hinweis),
            verwaltungskosten_hinweis_cent=_parse_optionalen_betrag(verwaltungskosten_hinweis),
            tatsaechlicher_zahlungseingang_cent=_parse_optionalen_betrag(tatsaechlicher_zahlungseingang),
            vermietete_einheiten=int(vermietete_einheiten) if vermietete_einheiten.strip() else None,
            vermietete_flaeche_qm=Decimal(vermietete_flaeche_qm.replace(",", ".")) if vermietete_flaeche_qm.strip() else None,
            erstellt_von=session.user_id,
        )
    except (MietinkassoError, ValueError, InvalidOperation) as exc:
        return _fehlerseite(session, "Variable Monatsabrechnung", str(exc), f"/backoffice/variable-abrechnung/{id}/korrigieren")
    return RedirectResponse(url="/backoffice/variable-abrechnung", status_code=303)


@router.get("/variable-abrechnung/versionen", response_class=HTMLResponse)
def variable_abrechnung_versionen(
    request: Request, einheit_id: str, art: str, monat: str, session=Depends(_current_session)
) -> HTMLResponse:
    try:
        versionen = _variableabrechnung.service.liste_versionen(ctx=_ctx(session), einheit_id=einheit_id, art=art, leistungsmonat=monat)
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Versionshistorie", str(exc), "/backoffice/variable-abrechnung")
    zeilen = "".join(
        "<tr>"
        f"<td>v{z.version}</td><td>{h(z.status)}</td><td>{'AKTUELL' if z.ist_aktuell else h(str(z.ist_aktuell))}</td>"
        f"<td>{eur(z.unser_netto_anteil_cent) if z.unser_netto_anteil_cent is not None else '-'}</td>"
        f"<td>{h(z.quelle_referenz)}</td><td>{h(z.aenderungsgrund or '')}</td>"
        f"<td>{h(z.erstellt_von)}</td><td>{z.erstellt_am.isoformat()}</td>"
        "</tr>"
        for z in versionen
    ) or '<tr><td colspan=8 class="muted">Keine Versionen.</td></tr>'
    inhalt = f"""
    <div class="card">
      <h1>Versionshistorie — {h(einheit_id)} / {h(art)} / {h(monat)}</h1>
      <table>
        <tr><th>Version</th><th>Status</th><th>Aktuell</th><th>Nettoanteil</th><th>Quelle</th>
            <th>Änderungsgrund</th><th>Erstellt von</th><th>Erstellt am</th></tr>
        {zeilen}
      </table>
      <p><a href="/backoffice/variable-abrechnung">&larr; zurück</a></p>
    </div>"""
    return _layout(request, session, "Versionshistorie", inhalt)


@router.get("/variable-abrechnung/import", response_class=HTMLResponse)
def variable_abrechnung_import_formular(request: Request, session=Depends(_current_session)) -> HTMLResponse:
    inhalt = f"""
    <div class="card" style="max-width:720px;">
      <h1>Variable Monatsabrechnung — CSV-Import</h1>
      <p class="muted">Erwartete Spalten: <code>einheit_id,art,leistungsmonat,belegdatum,quelle_referenz,status,
         berichteter_betrag,berichteter_betragsart,unser_netto_anteil,tatsaechlicher_zahlungseingang,
         vermietete_einheiten,vermietete_flaeche_qm,aenderungsgrund,import_id</code> (Beträge als EUR,
         Datum YYYY-MM-DD, Monat YYYY-MM). Genau eine aktuelle Version je Einheit/Art/Monat - eine Zeile,
         die von einer bestehenden aktuellen Version abweicht, benötigt einen <code>aenderungsgrund</code>
         und wird nur nach expliziter Bestätigung als neue Version übernommen.</p>
      <form method="post" action="/backoffice/variable-abrechnung/import/vorschau" enctype="multipart/form-data">
        {csrf_feld(session.csrf_token)}
        <label>CSV-Datei</label>
        <input type="file" name="datei" accept=".csv,text/csv" required>
        <button type="submit">Vorschau anzeigen</button>
      </form>
    </div>"""
    return _layout(request, session, "Variable Monatsabrechnung — Import", inhalt)


@router.post("/variable-abrechnung/import/vorschau", response_class=HTMLResponse)
async def variable_abrechnung_import_vorschau(
    request: Request, datei: UploadFile = File(...), csrf_token: str = Form(...), session=Depends(_current_session)
) -> HTMLResponse:
    _verify_csrf(session, csrf_token)
    rohbytes = await datei.read()
    text = rohbytes.decode("utf-8-sig")
    try:
        zeilen = _variable_abrechnung_parse_csv(text)
    except (KeyError, ValueError) as exc:
        return _fehlerseite(session, "Variable Monatsabrechnung — Import", f"Datei kann nicht gelesen werden: {exc}", "/backoffice/variable-abrechnung/import")
    plan = _variable_abrechnung_erstelle_plan(zeilen, ctx=_ctx(session), repository=_variableabrechnung.repository)

    def _zeile_klasse(b) -> str:
        return "gesperrt-row" if b.status in ("GESPERRT", "KONFLIKT") else ""

    zeilen_html = "".join(
        f"<tr class='{_zeile_klasse(b)}'>"
        f"<td>{b.zeilennummer}</td><td>{h(b.einheit_id)}</td><td>{h(b.art)}</td><td>{h(b.leistungsmonat)}</td>"
        f"<td>{h(b.status)}</td><td>{h(b.grund or '')}</td></tr>"
        for b in plan.befunde
    )
    anwendbar_ohne_bestaetigung = plan.anwendbar(korrekturen_bestaetigt=False)
    anwendbar_mit_bestaetigung = plan.anwendbar(korrekturen_bestaetigt=True)

    if not anwendbar_mit_bestaetigung:
        bestaetigen = flash_error("Datei enthält gesperrte/konfliktbehaftete Zeilen (siehe oben) - es wird NICHTS eingespielt, bitte korrigieren und erneut hochladen.")
    else:
        korrektur_hinweis = (
            '<label><input type="checkbox" name="korrekturen_bestaetigt" value="1" required> '
            "Ich bestätige die oben markierten KORREKTUR-Zeilen bewusst als neue Version.</label><br>"
            if plan.korrektur else ""
        )
        bestaetigen = f"""
        <form method="post" action="/backoffice/variable-abrechnung/import/uebernehmen">
          {csrf_feld(session.csrf_token)}
          <textarea name="datei_inhalt" hidden>{h(text)}</textarea>
          <input type="hidden" name="plan_hash" value="{h(plan.plan_hash)}">
          {korrektur_hinweis}
          <button type="submit">Jetzt übernehmen ({len(zeilen)} Zeile(n): {len(plan.neu)} neu,
            {len(plan.unveraendert)} unverändert, {len(plan.korrektur)} Korrektur)</button>
        </form>"""

    inhalt = f"""
    <div class="card">
      <h1>Vorschau — Variable Monatsabrechnung</h1>
      <table>
        <tr><th>Zeile</th><th>Einheit</th><th>Art</th><th>Monat</th><th>Status</th><th>Grund</th></tr>
        {zeilen_html}
      </table>
      {bestaetigen}
      <p><a href="/backoffice/variable-abrechnung/import">&larr; andere Datei wählen</a></p>
    </div>"""
    return _layout(request, session, "Vorschau — Variable Monatsabrechnung", inhalt)


@router.post("/variable-abrechnung/import/uebernehmen", response_class=HTMLResponse)
def variable_abrechnung_import_uebernehmen(
    request: Request,
    datei_inhalt: str = Form(...),
    plan_hash: str = Form(...),
    korrekturen_bestaetigt: str = Form(""),
    csrf_token: str = Form(...),
    session=Depends(_current_session),
) -> HTMLResponse:
    _verify_csrf(session, csrf_token)
    zeilen = _variable_abrechnung_parse_csv(datei_inhalt)
    try:
        ergebnis = _variable_abrechnung_wende_an(
            zeilen, ctx=_ctx(session), bestaetigter_hash=plan_hash, korrekturen_bestaetigt=bool(korrekturen_bestaetigt),
            service=_variableabrechnung.service, repository=_variableabrechnung.repository, akteur=session.user_id,
        )
    except (MietinkassoError, ValueError, VariableAbrechnungImportNichtAnwendbarError) as exc:
        return _fehlerseite(session, "Variable Monatsabrechnung — Import", f"Import abgebrochen, NICHTS wurde eingespielt: {exc}", "/backoffice/variable-abrechnung/import")
    inhalt = flash_ok(
        f"{ergebnis.anzahl_neu} neu, {ergebnis.anzahl_unveraendert} unverändert, "
        f"{ergebnis.anzahl_korrektur} korrigiert."
    ) + '<p><a href="/backoffice/variable-abrechnung">&larr; zur Übersicht</a></p>'
    return _layout(request, session, "Import erfolgreich", inhalt)


@router.get("/dashboard/monatsuebersicht", response_class=HTMLResponse)
def dashboard_monatsuebersicht(request: Request, monat: str | None = None, session=Depends(_current_session)) -> HTMLResponse:
    heute = date.today()
    gewaehlter_monat = monat or f"{heute.year:04d}-{heute.month:02d}"
    try:
        uebersicht = berechne_monatsuebersicht(
            ctx=_ctx(session), leistungsmonat=gewaehlter_monat, stammdaten_repository=_stammdaten_repo,
            variable_service=_variableabrechnung.service,
            komponenten_freigabe_service=_variableabrechnung.komponenten_freigabe_service,
        )
    except ValueError as exc:
        return _fehlerseite(session, "Monatsübersicht", f"Ungültiger Monat '{gewaehlter_monat}': {exc}", "/backoffice/dashboard/monatsuebersicht")

    datenluecken_html = "".join(f"<li>{h(g)}</li>" for g in uebersicht.datenluecken) or "<li class='ok'>Keine Datenlücken erkannt.</li>"
    vollstaendigkeits_hinweis = (
        '<p class="ok">Keine offenen Datenlücken für diesen Monat.</p>'
        if uebersicht.vollstaendig
        else f'<p class="warn">{len(uebersicht.datenluecken)} Datenlücke(n) - die Summe unten ist deshalb NICHT als vollständig zu verstehen.</p>'
    )
    inhalt = f"""
    <div class="card">
      <h1>Monatsübersicht — Nettomieterlös laut Vorschreibung und Monatsabrechnungen</h1>
      <form method="get" action="/backoffice/dashboard/monatsuebersicht">
        <label>Monat (YYYY-MM)</label>
        <input type="text" name="monat" value="{h(gewaehlter_monat)}" placeholder="2026-08">
        <button type="submit">Anzeigen</button>
      </form>
      <table>
        <tr><th>Dauermiete-Soll (netto, ohne BK/HK/USt)</th><td>{eur(uebersicht.dauermiete_soll_netto_cent)}</td></tr>
        <tr><th>Kurzzeitvermietung — Nettoanteil (bestätigt)</th><td>{eur(uebersicht.kurzzeit_netto_anteil_cent)}</td></tr>
        <tr><th>Selfstorage — Nettoanteil (bestätigt)</th><td>{eur(uebersicht.selfstorage_netto_anteil_cent)}</td></tr>
        <tr><th><strong>Nettomieterlös laut Vorschreibung und Monatsabrechnungen</strong></th>
            <td><strong>{eur(uebersicht.nettomieterloes_cent)}</strong></td></tr>
      </table>
      {vollstaendigkeits_hinweis}
      <p class="muted">Diese Summe ist NIEMALS ein Bank-Ist (tatsächlicher Zahlungseingang) - sie beruht
         ausschließlich auf Dauermiete-Vorschreibungsbasis und BESTÄTIGTEN Monatsabrechnungen.</p>
      <h2>Datenlücken</h2>
      <ul>{datenluecken_html}</ul>
      <p><a href="/backoffice/variable-abrechnung">&larr; zur variablen Monatsabrechnung</a></p>
    </div>"""
    return _layout(request, session, "Monatsübersicht", inhalt)


# -- Netto-Mietanteil-Freigabe je Vertragskomponente -------------------------
# Auftrag 13.09., unabhängiger Review: Art/USt-Satz allein sind kein Beleg
# für einen tatsächlich NETTO gespeicherten Komponentenbetrag - erst eine
# hier explizit erfasste, belegte Freigabe zählt für die Monatsübersicht.


@router.get("/vertrag/{vertrag_id}/komponenten-freigabe", response_class=HTMLResponse)
def komponenten_freigabe_formular(request: Request, vertrag_id: str, session=Depends(_current_session)) -> HTMLResponse:
    vertrag = _stammdaten_repo.get_vertrag(vertrag_id)
    if vertrag is None:
        return _fehlerseite(session, "Netto-Mietanteil-Freigabe", f"Unbekannter Vertrag {vertrag_id}.")
    # Unabhängiger Review: dieses GET las Vertrag/Komponenten bislang
    # ohne jede ctx-/Scopeprüfung - ein fremdgesellschafts-gebundener
    # ctx konnte so Komponentenbeträge/Freigabestatus einer FREMDEN
    # Gesellschaft einsehen.
    try:
        require_gesellschaft_access(_ctx(session), vertrag.gesellschaft_id)
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Netto-Mietanteil-Freigabe", str(exc), "/backoffice/")
    komponenten = _stammdaten_repo.list_aktive_komponenten(vertrag_id, date.today())
    zeilen = []
    for k in komponenten:
        freigaben = _variableabrechnung.komponenten_freigabe_service.liste_fuer_komponente(ctx=_ctx(session), komponente_id=k.id)
        aktuelle_freigabe = next((f for f in freigaben if f.status == "FREIGEGEBEN"), None)
        freigabe_html = (
            f"AKTIV: {eur(aktuelle_freigabe.bestaetigter_netto_betrag_cent)} ab {aktuelle_freigabe.gueltig_von.isoformat()}"
            + (f" bis {aktuelle_freigabe.gueltig_bis.isoformat()}" if aktuelle_freigabe.gueltig_bis else " (unbefristet)")
            + (" [ENTWERTET - Komponente seither geändert]" if aktuelle_freigabe and not _variableabrechnung.komponenten_freigabe_service.ist_noch_gueltig(aktuelle_freigabe) else "")
            if aktuelle_freigabe else '<span class="muted">keine aktive Freigabe - Datenlücke in der Monatsübersicht</span>'
        )
        zeilen.append(f"""
        <tr>
          <td>{h(k.id)}</td><td>{h(k.art)}</td><td>{h(k.bezeichnung)}</td>
          <td>{eur(k.betrag_cent)} (gespeichert, Basis ungeprüft)</td>
          <td>{freigabe_html}</td>
          <td>
            <form method="post" action="/backoffice/vertrag/{h(vertrag_id)}/komponenten-freigabe/{h(k.id)}/freigeben" class="inline">
              {csrf_feld(session.csrf_token)}
              <input type="text" name="bestaetigter_netto_betrag" placeholder="Netto-Betrag EUR" required style="width:8em;">
              <input type="text" name="quellenbeleg_referenz" placeholder="Quellenbeleg" required style="width:10em;">
              <input type="date" name="gueltig_von" required>
              <input type="date" name="gueltig_bis" placeholder="optional">
              <input type="text" name="aenderungsgrund" placeholder="Änderungsgrund (nur bei Überschneidung mit bestehender Freigabe nötig)" style="width:16em;">
              <button type="submit" class="secondary">Freigeben</button>
            </form>
          </td>
        </tr>""")
    inhalt = f"""
    <div class="card">
      <h1>Netto-Mietanteil-Freigabe — Vertrag {h(vertrag_id)}</h1>
      <p class="muted">Der gespeicherte Komponentenbetrag ist historisch teils BRUTTO erfasst (auch bei
         HMZ/Küche/Parkplatz) - Art und USt-Satz allein sind kein Beleg. Nur ein hier explizit
         bestätigter, belegter Netto-Betrag fließt in die Nettomieterlös-Monatsübersicht ein. Eine
         Freigabe entwertet sich automatisch, sobald sich die zugrunde liegende Komponente ändert.
         `VertragsKomponenteTable`/OP-Buchungen bleiben davon unberührt.</p>
      <table>
        <tr><th>Komponente</th><th>Art</th><th>Bezeichnung</th><th>Gespeicherter Betrag</th>
            <th>Freigabestatus</th><th>Neu freigeben</th></tr>
        {''.join(zeilen) if zeilen else '<tr><td colspan=6 class="muted">Keine aktiven Komponenten.</td></tr>'}
      </table>
      <p><a href="/backoffice/konto/{h(_stammdaten_repo.get_konto_by_vertrag(vertrag_id).id) if _stammdaten_repo.get_konto_by_vertrag(vertrag_id) else ''}">&larr; zurück zum Kontoauszug</a></p>
    </div>"""
    return _layout(request, session, "Netto-Mietanteil-Freigabe", inhalt)


@router.post("/vertrag/{vertrag_id}/komponenten-freigabe/{komponente_id}/freigeben")
def komponenten_freigabe_erstellen(
    request: Request,
    vertrag_id: str,
    komponente_id: str,
    bestaetigter_netto_betrag: str = Form(...),
    quellenbeleg_referenz: str = Form(...),
    gueltig_von: str = Form(...),
    gueltig_bis: str = Form(""),
    aenderungsgrund: str = Form(""),
    csrf_token: str = Form(...),
    session=Depends(_current_session),
):
    _verify_csrf(session, csrf_token)
    # Unabhängiger Review: der Pfad behauptet über `vertrag_id`, zu
    # welchem Vertrag `komponente_id` gehört, ohne das je zu prüfen -
    # `service.freigeben` leitet den tatsächlichen Vertrag/Scope zwar
    # SELBST korrekt über `komponente.vertrag_id` ab (kein Sicherheits-
    # loch), aber eine manipulierte/veraltete `vertrag_id` im Pfad würde
    # sonst unbemerkt eine Komponente EINES ANDEREN Vertrags freigeben
    # und den Nutzer nach dem falschen Formular zurückleiten.
    komponente = _stammdaten_repo.get_komponente(komponente_id)
    if komponente is None or komponente.vertrag_id != vertrag_id:
        return _fehlerseite(
            session, "Netto-Mietanteil-Freigabe",
            f"Komponente {komponente_id} gehört nicht zu Vertrag {vertrag_id}.",
            f"/backoffice/vertrag/{vertrag_id}/komponenten-freigabe",
        )
    try:
        _variableabrechnung.komponenten_freigabe_service.freigeben(
            ctx=_ctx(session), komponente_id=komponente_id,
            bestaetigter_netto_betrag_cent=parse_eur_betrag(bestaetigter_netto_betrag),
            quellenbeleg_referenz=quellenbeleg_referenz, gueltig_von=date.fromisoformat(gueltig_von),
            gueltig_bis=date.fromisoformat(gueltig_bis) if gueltig_bis.strip() else None,
            freigegeben_von=session.user_id, aenderungsgrund=aenderungsgrund or None,
        )
    except (MietinkassoError, ValueError, InvalidOperation) as exc:
        return _fehlerseite(session, "Netto-Mietanteil-Freigabe", str(exc), f"/backoffice/vertrag/{vertrag_id}/komponenten-freigabe")
    return RedirectResponse(url=f"/backoffice/vertrag/{vertrag_id}/komponenten-freigabe", status_code=303)
