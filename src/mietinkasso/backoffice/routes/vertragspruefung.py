"""Vertragsprüfung: Rechtsordnungs-Versionen, Sperren, Index-Prüfbedarf.

Jede Prüfung ist eine neue, unveränderliche Version; nur Fachstatus
GEPRUEFT schreibt die Rechtsordnung auf den Vertrag zurück."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from html import escape as h

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from mietinkasso.backoffice.views import csrf_feld, option, sperrgrund_label
from mietinkasso.domain.exceptions import MietinkassoError

from mietinkasso.backoffice import dependencies as deps
from mietinkasso.backoffice.auth import _ctx, _current_session, _fehlerseite, _layout, _objekt_fuer_vertrag_gesperrt, _verify_csrf
from mietinkasso.backoffice.routes.shared import _RECHTSORDNUNGEN_FUER_AUSWAHL


#: Ohne eigenes Prefix - das gemeinsame `/backoffice`-Prefix wird GENAU
#: EINMAL in `backoffice/app.py` gesetzt.
router = APIRouter()


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
      <td>{h(sperrgrund_label(s.grund))} <span class="muted">({h(s.grund)})</span></td>
      <td>{s.gesetzt_am.isoformat() if s.gesetzt_am else ''}</td><td>{h(s.kommentar or '')}</td>
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
    vertrag = deps._stammdaten_repo.get_vertrag(vertrag_id)
    if vertrag is None:
        return _fehlerseite(session, "Vertragsprüfung", f"Unbekannter Vertrag {vertrag_id}.")
    if _objekt_fuer_vertrag_gesperrt(vertrag_id):
        return _fehlerseite(session, "Vertragsprüfung", "Objekt ist gesperrt; keine Prüfung möglich.")

    rechtsordnung_optionen = "".join(
        option(r, r, selected=(r == vertrag.rechtsordnung)) for r in _RECHTSORDNUNGEN_FUER_AUSWAHL
    )
    historie = deps._vertragspruefung_service.liste_pruefungen(vertrag_id)
    historie_html = "".join(_pruefung_zeile_html(p) for p in historie) or '<tr><td colspan=7 class="muted">Noch keine Prüfung erfasst.</td></tr>'

    aktive_sperren = deps._stammdaten_repo.aktive_sperren(vertrag_id)
    sperren_html = "".join(_sperre_zeile_html(s, vertrag_id=vertrag_id, csrf_token=session.csrf_token) for s in aktive_sperren)
    sperren_html = sperren_html or '<tr><td colspan=4 class="muted">Keine aktiven Sperren.</td></tr>'

    pruefbedarf = deps._vertragspruefung_service.liste_index_pruefbedarf(vertrag_id)
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
    <p><a href="/backoffice/konto/{h(deps._stammdaten_repo.get_konto_by_vertrag(vertrag_id).id) if deps._stammdaten_repo.get_konto_by_vertrag(vertrag_id) else ''}">&larr; zurück zum Kontoauszug</a></p>
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
    vertrag = deps._stammdaten_repo.get_vertrag(vertrag_id)
    if vertrag is None:
        return _fehlerseite(session, "Vertragsprüfung", f"Unbekannter Vertrag {vertrag_id}.")
    try:
        pruefung = deps._vertragspruefung_service.pruefung_anlegen(
            ctx=_ctx(session), vertrag=vertrag, rechtsordnung=rechtsordnung, fachstatus=fachstatus,
            quellenbeleg_referenz=quellenbeleg_referenz, kommentar=kommentar or None, akteur=session.user_id,
        )
        deps._audit_service.log(
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
    vertrag = deps._stammdaten_repo.get_vertrag(vertrag_id)
    if vertrag is None:
        return _fehlerseite(session, "Vertragsprüfung", f"Unbekannter Vertrag {vertrag_id}.")
    try:
        sperre = deps._vertragspruefung_service.sperre_aufheben(
            ctx=_ctx(session), vertrag=vertrag, sperre_id=sperre_id, begruendung=begruendung, akteur=session.user_id,
        )
        deps._audit_service.log(
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
    vertrag = deps._stammdaten_repo.get_vertrag(vertrag_id)
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
        deps._vertragspruefung_service.index_pruefbedarf_speichern(
            ctx=_ctx(session), vertrag=vertrag, rechtsordnung=rechtsordnung or None,
            basis_reihe=basis_reihe or None, basis_wert=basis_wert_decimal, basis_monat=basis_monat or None,
            kommentar=kommentar or None, akteur=session.user_id,
        )
        deps._audit_service.log(
            entity_typ="index_pruefbedarf", entity_id=vertrag_id, aktion="angelegt", akteur=session.user_id,
            payload={"rechtsordnung": rechtsordnung, "basis_reihe": basis_reihe, "basis_monat": basis_monat},
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Vertragsprüfung", str(exc), f"/backoffice/vertrag/{vertrag_id}/pruefung")
    return RedirectResponse(url=f"/backoffice/vertrag/{vertrag_id}/pruefung", status_code=303)
