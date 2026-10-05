"""Bankquellen je Objekt: Übersicht, Bindung, Widerruf (Auftrag
HV-20261005-BANKQUELLENBINDUNG, docs/hausverwaltung/BANKQUELLENBINDUNG.md).

Nur geschützte Routen mit Sitzung + CSRF. Keine vorbefüllte Zuordnung,
kein vorausgewähltes Bankkonto, keine Zugangsdaten - nur nicht geheime
Anbieter-Kennungen. Jede Änderung braucht einen Nachweis und den
angezeigten Stand (`erwarteter_stand`); die Fachprüfung, das Audit und
die Transaktion liegen vollständig in `bank/quellenbindung.py`."""

from __future__ import annotations

from html import escape as h

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from mietinkasso.backoffice import dependencies as deps
from mietinkasso.backoffice.auth import _ctx, _current_session, _fehlerseite, _layout, _verify_csrf
from mietinkasso.backoffice.views import csrf_feld, flash_ok, option
from mietinkasso.bank.quellenbindung import ObjektQuellenStatus
from mietinkasso.domain.exceptions import MietinkassoError

#: Ohne eigenes Prefix - `/backoffice` setzt `backoffice/app.py`.
router = APIRouter()

_STATUS_HTML = {
    "NICHT_KONFIGURIERT": '<span class="badge badge-muted">nicht konfiguriert</span>'
    '<p class="muted">Noch keine feste Zuordnung. Das Konto wird beim manuellen Import gewählt.</p>',
    "AKTIV": '<span class="badge badge-ok">aktiv gebunden</span>',
    "WIDERRUFEN": '<span class="badge badge-error">widerrufen &ndash; gesperrt</span>'
    '<p class="muted">Kein Import/keine Zuordnung über diese Bindung, kein Rückfall auf andere Konten.</p>',
    "UNGUELTIG": '<span class="badge badge-error">ungültig &ndash; gesperrt</span>',
}


def _bindungs_formular(session, status: ObjektQuellenStatus) -> str:
    bank_optionen = "".join(
        option(bk.id, f"{bk.bezeichnung} ({bk.id})")
        for bk in deps._bank_repo.list_bank_konten(gesellschaft_id=status.gesellschaft_id)
    )
    return f"""
    <details><summary>{'Neu binden' if status.revision else 'Bankquelle binden'}</summary>
      <form method="post" action="/backoffice/bank/quellen/binden">
        {csrf_feld(session.csrf_token)}
        <input type="hidden" name="objekt_id" value="{h(status.objekt_id)}">
        <input type="hidden" name="gesellschaft_id" value="{h(status.gesellschaft_id)}">
        <input type="hidden" name="erwarteter_stand" value="{h(status.token)}">
        <label>Anbieter (Kennung, kein Passwort)</label><input type="text" name="anbieter" required>
        <label>Zugangsreferenz (z. B. Teilnehmer-/Kunden-ID, kein Schlüssel)</label><input type="text" name="zugang_ref" required>
        <label>Anbieter-Kontoreferenz</label><input type="text" name="konto_ref" required>
        <label>Bankkonto</label>
        <select name="bank_konto_id" required><option value="">-- wählen --</option>{bank_optionen}</select>
        <label>Eigene IBAN des Bankkontos (zur Bestätigung)</label><input type="text" name="iban" required>
        <label>Kontorolle</label>
        <select name="kontorolle" required><option value="">-- wählen --</option><option value="MIETE">MIETE (Mieteingangskonto)</option></select>
        <label>Nachweis (Beleg-/Vorgangsreferenz)</label><input type="text" name="nachweis_ref" required>
        <button type="submit">Binden</button>
      </form>
    </details>"""


def _widerrufs_formular(session, status: ObjektQuellenStatus) -> str:
    return f"""
    <details><summary>Widerrufen</summary>
      <form method="post" action="/backoffice/bank/quellen/widerrufen">
        {csrf_feld(session.csrf_token)}
        <input type="hidden" name="objekt_id" value="{h(status.objekt_id)}">
        <input type="hidden" name="erwarteter_stand" value="{h(status.token)}">
        <label>Nachweis (Grund/Beleg)</label><input type="text" name="nachweis_ref" required>
        <button type="submit">Bindung widerrufen (Objekt bleibt gesperrt)</button>
      </form>
    </details>"""


@router.get("/bank/quellen", response_class=HTMLResponse)
def bankquellen_uebersicht(request: Request, ok: str | None = None, session=Depends(_current_session)) -> HTMLResponse:
    zeilen = []
    for status in deps._bank_service.quellenbindung.uebersicht(ctx=_ctx(session)):
        if status.revision is None:
            quelle_html = "-"
        else:
            quelle_html = (
                f"{h(status.anbieter or '-')} / {h(status.zugang_ref or '-')} / {h(status.konto_ref or '-')}<br>"
                f"Rolle {h(status.kontorolle or '-')} &middot; Bankkonto {h(status.bank_konto_id or '-')} "
                f"({h(status.iban_maskiert or '-')})<br>"
                f'<span class="muted">Revision {status.revision} &middot; Nachweis {h(status.nachweis_ref or "-")} '
                f"&middot; {h(status.akteur or '-')} "
                f"{status.zeitpunkt.strftime('%Y-%m-%d %H:%M') + ' UTC' if status.zeitpunkt else ''}</span>"
            )
        hinweis = f'<p class="warn">{h(status.hinweis)}</p>' if status.hinweis else ""
        aktionen = _bindungs_formular(session, status)
        if status.status in ("AKTIV", "UNGUELTIG"):
            aktionen += _widerrufs_formular(session, status)
        zeilen.append(
            f"<tr><td>{h(status.objekt_id)} {h(status.objekt_bezeichnung)}<br>"
            f'<span class="muted">{h(status.gesellschaft_id)}</span></td>'
            f"<td>{_STATUS_HTML[status.status]}{hinweis}</td><td>{quelle_html}</td><td>{aktionen}</td></tr>"
        )
    meldung = flash_ok("Bankquellenbindung gespeichert.") if ok else ""
    inhalt = f"""
    {meldung}
    <div class="card">
      <h1>Bankquellen je Objekt</h1>
      <p class="muted">Bindet eine Anbieter-Kontokennung an GENAU ein Bankkonto mit Rolle MIETE und an das Objekt.
         Danach sind Import und Zahlungszuordnung für Mieter dieses Objekts nur über dieses Bankkonto möglich.
         Es wird keine Bankverbindung hergestellt und kein Konto vorgeschlagen; ausgeschlossene Objekte erscheinen
         nicht. Ein Widerruf sperrt das Objekt, bis es ausdrücklich neu gebunden wird.</p>
      <div class="tabelle-scroll"><table>
        <tr><th>Objekt</th><th>Status</th><th>Bankquelle</th><th>Aktion</th></tr>
        {''.join(zeilen) or '<tr><td colspan="4" class="muted">Keine zugänglichen Objekte.</td></tr>'}
      </table></div>
      <p><a href="/backoffice/bank">&larr; Bankdatei-Import</a></p>
    </div>"""
    return _layout(request, session, "Bankquellen", inhalt)


@router.post("/bank/quellen/binden")
def bankquellen_binden(
    request: Request,
    objekt_id: str = Form(...),
    gesellschaft_id: str = Form(...),
    erwarteter_stand: str = Form(...),
    anbieter: str = Form(...),
    zugang_ref: str = Form(...),
    konto_ref: str = Form(...),
    bank_konto_id: str = Form(...),
    iban: str = Form(...),
    kontorolle: str = Form(...),
    nachweis_ref: str = Form(...),
    csrf_token: str = Form(...),
    session=Depends(_current_session),
):
    _verify_csrf(session, csrf_token)
    try:
        deps._bank_service.quellenbindung.binden(
            ctx=_ctx(session), objekt_id=objekt_id, anbieter=anbieter, zugang_ref=zugang_ref, konto_ref=konto_ref,
            bank_konto_id=bank_konto_id, gesellschaft_id=gesellschaft_id, iban=iban, kontorolle=kontorolle,
            nachweis_ref=nachweis_ref, erwarteter_stand=erwarteter_stand,
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Bankquellen", f"Nicht gespeichert: {exc}", "/backoffice/bank/quellen")
    return RedirectResponse(url="/backoffice/bank/quellen?ok=1", status_code=303)


@router.post("/bank/quellen/widerrufen")
def bankquellen_widerrufen(
    request: Request,
    objekt_id: str = Form(...),
    erwarteter_stand: str = Form(...),
    nachweis_ref: str = Form(...),
    csrf_token: str = Form(...),
    session=Depends(_current_session),
):
    _verify_csrf(session, csrf_token)
    try:
        deps._bank_service.quellenbindung.widerrufen(
            ctx=_ctx(session), objekt_id=objekt_id, nachweis_ref=nachweis_ref, erwarteter_stand=erwarteter_stand,
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Bankquellen", f"Nicht widerrufen: {exc}", "/backoffice/bank/quellen")
    return RedirectResponse(url="/backoffice/bank/quellen?ok=1", status_code=303)
