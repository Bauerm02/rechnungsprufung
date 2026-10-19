"""Übersicht über Mailversand und Zustellnachweise.

Reine Einsicht plus ein ausdrücklich betätigter Statusabgleich - diese
Routen versenden selbst nichts."""

from __future__ import annotations

from datetime import timezone
from html import escape as h

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from mietinkasso.backoffice.views import csrf_feld

from mietinkasso.backoffice import dependencies as deps
from mietinkasso.backoffice.auth import _ctx, _current_session, _layout, _verify_csrf


#: Ohne eigenes Prefix - das gemeinsame `/backoffice`-Prefix wird GENAU
#: EINMAL in `backoffice/app.py` gesetzt.
router = APIRouter()


@router.get("/mailversand", response_class=HTMLResponse)
def mailversand_uebersicht(request: Request, session=Depends(_current_session)):
    ready = bool(deps._hv_mail.client and deps._settings.hv_mail_allowlist_bestaetigt)
    modes = [("Mahnungen", deps._settings.send_enabled),
             ("Indexanpassungen", deps._settings.indexautomatik_send_enabled),
             ("Vertragsende an Markus", deps._settings.vertragsende_erinnerung_send_enabled)]
    modes_html = " · ".join(f"{name}: {'aktiv' if ready and enabled else 'gesperrt'}" for name, enabled in modes)
    rows = []
    for row in deps._hv_mail.versanduebersicht(ctx=_ctx(session)):
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
    deps._hv_mail.status_abgleichen(ctx=_ctx(session))
    return RedirectResponse("/backoffice/mailversand", status_code=303)
