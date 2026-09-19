"""Laufender Indexautomatik-Betrieb: Outbox, Monatsläufe, Soll-Umsetzung,
VPI-Pflege und Vertragsende-Erinnerungen.

Der eigentliche Monats-/Tageslauf läuft NICHT über HTTP (siehe
`scripts/indexautomatik_*.py`) - hier gibt es nur Einsicht, Freigabe und
manuelle Pflege."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from html import escape as h

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from mietinkasso.auth.service import require_schreibrecht
from mietinkasso.backoffice.views import csrf_feld, eur, flash_ok, option
from mietinkasso.domain.enums import ZUGANGSFORMEN_ALLE as _ZUGANGSFORMEN_ALLE
from mietinkasso.domain.exceptions import MietinkassoError
from mietinkasso.indexautomatik.zeit import heute_wien
from mietinkasso.infrastructure.db.tables import VpiMonatswertTable

from mietinkasso.backoffice import dependencies as deps
from mietinkasso.backoffice.auth import _ctx, _current_session, _fehlerseite, _layout, _verify_csrf


#: Ohne eigenes Prefix - das gemeinsame `/backoffice`-Prefix wird GENAU
#: EINMAL in `backoffice/app.py` gesetzt.
router = APIRouter()


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
    schreiben = deps._indexautomatik.outbox_repository.liste_alle()
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

    laeufe = deps._indexautomatik.lauf_repository.liste_alle()
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
    if deps._hv_mail.client is None:
        return _fehlerseite(
            session, "Indexautomatik-Outbox",
            "Kein Versandweg eingerichtet - Versand ist strukturell gesperrt. Es wird niemals ein "
            "Test-Versand im Produktivbetrieb durchgeführt.",
            "/backoffice/indexautomatik/outbox",
        )
    try:
        ergebnis = deps._hv_mail.index_senden(ctx=_ctx(session), row_id=erhoehungsschreiben_id, heute=heute)
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
        deps._indexautomatik.outbox_service.zugang_bestaetigen(
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
        for o in deps._indexautomatik.outbox_repository.liste_alle()
        if o.status in _SOLL_UMSETZUNG_STATUS
        and (vertrag := deps._stammdaten_repo.get_vertrag(o.vertrag_id)) is not None
        and ctx.has_zugriff(vertrag.gesellschaft_id)
    ]
    zeilen = "".join(_soll_umsetzung_zeile_html(o) for o in alle) or (
        '<tr><td colspan=6 class="muted">Kein Fall mit fälliger/bereits umgesetzter Soll-Umsetzung.</td></tr>'
    )
    hinweis = (
        ""
        if deps._settings.indexautomatik_soll_umsetzung_enabled
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
        vorschau = deps._indexautomatik.soll_umsetzung_service.vorschau(
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
        ergebnis = deps._indexautomatik.soll_umsetzung_service.umsetzen(
            ctx=_ctx(session), erhoehungsschreiben_id=erhoehungsschreiben_id, heute=heute_wien(),
            akteur=session.user_id, soll_umsetzung_enabled=deps._settings.indexautomatik_soll_umsetzung_enabled,
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Indexautomatik-Soll-Umsetzung", str(exc), "/backoffice/indexautomatik/soll-umsetzung")
    inhalt = flash_ok(f"Soll-Umsetzung: {ergebnis.status}" + (f" — {', '.join(ergebnis.gruende)}" if ergebnis.gruende else "")) + (
        '<p><a href="/backoffice/indexautomatik/soll-umsetzung">&larr; zurück</a></p>'
    )
    return _layout(request, session, "Indexautomatik-Soll-Umsetzung", inhalt)


@router.get("/indexautomatik/vpi", response_class=HTMLResponse)
def indexautomatik_vpi(request: Request, session=Depends(_current_session)) -> HTMLResponse:
    werte = deps._indexautomatik.vpi_repository.jahreswert_liste()
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
    <div class="card"><h2>Veröffentlichung eines vorhandenen Monatswerts belegen</h2>
      <p>Für publikationsabhängige Vertragsfristen. Der importierte VPI-Wert bleibt unverändert.</p>
      <form method="post" action="/backoffice/indexautomatik/vpi/veroeffentlichung">
        {csrf_feld(session.csrf_token)}
        <label>Reihe</label><select name="reihe"><option>VPI20C18</option><option>VPI15C18</option><option>VPI00</option><option>VPI96</option></select>
        <label>VPI-Monat</label><input type="month" name="monat" required>
        <label>Amtliches Veröffentlichungsdatum</label><input type="date" name="veroeffentlicht_am" required>
        <label>Beleg der Veröffentlichung</label><input name="quelle" required placeholder="Statistik Austria: Link und Fundstelle">
        <button type="submit">Veröffentlichungsbeleg speichern</button>
      </form></div>
    """
    return _layout(request, session, "VPI-Werte", inhalt)


@router.post("/indexautomatik/vpi/veroeffentlichung")
def vpi_veroeffentlichung_belegen(
    request: Request, reihe: str = Form(...), monat: str = Form(...), veroeffentlicht_am: str = Form(...),
    quelle: str = Form(...), csrf_token: str = Form(...), session=Depends(_current_session),
):
    _verify_csrf(session, csrf_token)
    require_schreibrecht(_ctx(session))
    try:
        periode = date.fromisoformat(monat + "-01")
        datum = date.fromisoformat(veroeffentlicht_am)
        if not quelle.strip() or len(quelle.strip()) > 256 or datum > heute_wien():
            raise ValueError("Ein erfolgtes Veröffentlichungsdatum und ein Quellenbeleg bis 256 Zeichen sind erforderlich.")
        with deps._session_factory() as db:
            row = db.execute(select(VpiMonatswertTable).where(
                VpiMonatswertTable.reihe == reihe, VpiMonatswertTable.jahr == periode.year,
                VpiMonatswertTable.monat == periode.month,
            )).scalar_one_or_none()
            if row is None or row.finalitaet != "ENDGUELTIG":
                raise ValueError("Kein entsprechender endgültiger Monatswert vorhanden.")
            row.veroeffentlicht_am = datum
            row.veroeffentlichung_quelle = quelle.strip()
            from mietinkasso.infrastructure.db.tables import AuditEventTable
            db.add(AuditEventTable(entity_typ="vpi_monatswert", entity_id=str(row.id),
                aktion="VEROEFFENTLICHUNG_BELEGT", akteur=session.user_id,
                payload={"reihe": reihe, "monat": monat, "datum": datum.isoformat(), "quelle": quelle.strip()}))
            db.commit()
    except ValueError as exc:
        return _fehlerseite(session, "VPI-Veröffentlichung", str(exc), "/backoffice/indexautomatik/vpi")
    return RedirectResponse("/backoffice/indexautomatik/vpi", status_code=303)


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
        deps._indexautomatik.vpi_repository.jahreswert_erfassen(
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
    erinnerungen = deps._indexautomatik.vertragsende_repository.liste_alle()
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
        deps._indexautomatik.vertragsende_service.entscheiden(
            ctx=_ctx(session), erinnerung_id=erinnerung_id, entscheidung=entscheidung, entschieden_von=session.user_id,
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Vertragsende-Erinnerungen", str(exc), "/backoffice/indexautomatik/vertragsende")
    return RedirectResponse(url="/backoffice/indexautomatik/vertragsende", status_code=303)


@router.post("/indexautomatik/vertragsende/{erinnerung_id}/mieterentwurf")
def indexautomatik_vertragsende_mieterentwurf(request: Request, erinnerung_id: int, csrf_token: str = Form(...), session=Depends(_current_session)):
    _verify_csrf(session, csrf_token)
    try:
        deps._indexautomatik.vertragsende_service.mieterentwurf_erzeugen(ctx=_ctx(session), erinnerung_id=erinnerung_id)
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Vertragsende-Erinnerungen", str(exc), "/backoffice/indexautomatik/vertragsende")
    return RedirectResponse(url="/backoffice/indexautomatik/vertragsende", status_code=303)
