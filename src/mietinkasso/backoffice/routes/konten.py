"""Mietkonto: Auszug, Nachbuchung, Storno/Korrektur, Eröffnungsimport
und Vorschreibungsentwurf/Sollstellung.

Alles, was direkt an einem Mietkonto bzw. dessen Soll-/Habenbuchungen
hängt - inklusive der zweistufigen Eröffnungs-CSV (Vorschau, dann
serverseitig neu validierte, atomare Verbuchung) und der von der
Sollstellung getrennten Vorschreibungsvorschau."""

from __future__ import annotations

from datetime import date, datetime, timezone
from html import escape as h

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from mietinkasso.auth.service import require_gesellschaft_access
from mietinkasso.backoffice.kontoblatt_render import render_kontoblatt_html
from mietinkasso.backoffice.views import csrf_feld, eur, flash_error, flash_ok, parse_eur_betrag, sperrgrund_label
from mietinkasso.domain.enums import OPTyp
from mietinkasso.domain.exceptions import MietinkassoError
from mietinkasso.index.service import UNTERSTUETZTE_BERECHNUNGSPROFILE
from mietinkasso.mahnwesen.brief_pdf import Absender as _BriefAbsender
from mietinkasso.op.eroeffnung_import import importiere_eroeffnung_csv_atomar, parse_eroeffnung_csv
from mietinkasso.op.kontoauszug_pdf import erzeuge_kontoauszug_pdf
from mietinkasso.op.kontoblatt import lade_kontoblatt
from mietinkasso.vorschreibung.service import faelligkeitsdatum

from mietinkasso.backoffice import dependencies as deps
from mietinkasso.backoffice.auth import _ctx, _current_session, _fehlerseite, _layout, _objekt_fuer_vertrag_gesperrt, _verify_csrf


#: Ohne eigenes Prefix - das gemeinsame `/backoffice`-Prefix wird GENAU
#: EINMAL in `backoffice/app.py` gesetzt.
router = APIRouter()


# -- Kontoauszug --------------------------------------------------------------


def _op_zeile_html(position) -> str:
    faelligkeit_html = position.faelligkeit.isoformat() if position.faelligkeit else '<span class="muted">unbekannt</span>'
    status_html = '<span class="ok">AKTIV</span>' if position.status == "AKTIV" else '<span class="muted">STORNIERT</span>'
    aktion_html = f'<a href="/backoffice/op/{position.id}/korrigieren">Stornieren/Korrigieren</a>' if position.status == "AKTIV" else ""
    if position.typ == OPTyp.RUECKLASTSCHRIFT.value and position.status == "AKTIV":
        aktion_html += (
            f' &nbsp;<a href="/backoffice/op/{position.id}/ruecklastschriftkosten">Rücklastschriftkosten erfassen</a>'
        )
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
    konto = deps._stammdaten_repo.get_konto(konto_id)
    if konto is None:
        return _fehlerseite(session, "Kontoauszug", f"Unbekanntes Konto {konto_id}.")
    vertrag = deps._stammdaten_repo.get_vertrag(konto.vertrag_id)
    if vertrag is None:
        return _fehlerseite(session, "Kontoauszug", "Stammdaten unvollständig.")
    # Codex-Abnahme 20.10.2026, P1: bislang prüfte NUR die PDF-Route den
    # Gesellschaftszugriff - diese HTML-Route las Debitor/Kosten/Saldo
    # bereits VOR jeder Zugriffsprüfung. Jetzt konsistent VOR jedem
    # weiteren Datenread auf BEIDEN Wegen geprüft (keine bloße
    # Ausblendung einzelner Kartenabschnitte für ein fremdes Konto -
    # eine fehlende Berechtigung bricht die gesamte Route ab).
    require_gesellschaft_access(_ctx(session), vertrag.gesellschaft_id)
    einheit = deps._stammdaten_repo.get_einheit(vertrag.einheit_id)
    debitor = deps._stammdaten_repo.get_debitor(konto.debitor_id)
    gesperrt = _objekt_fuer_vertrag_gesperrt(konto.vertrag_id)
    positionen = deps._op_service.list_alle_positionen(konto_id)
    saldo = deps._op_service.berechne_saldo(konto_id)
    aktive_sperren = deps._stammdaten_repo.aktive_sperren(vertrag.id)

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

    # Auftrag HV-20261020-KONTOBLATT-KOSTEN: dieselbe Fachregel wie in der
    # Mieterakte - ein von der Pilotphase ausgeschlossenes Objekt zeigt
    # KEINE Kosten-/Saldoaufschlüsselung (die bestehende, historisch
    # laxere reine OP-Tabelle unten bleibt dabei bewusst unverändert,
    # siehe `vertragsanlage.py::vertragsanlage_detail`-Kommentar - "das
    # ändert dieser Auftrag nicht").
    kontoblatt_karte = ""
    if not gesperrt and vertrag is not None:
        kontoblatt = lade_kontoblatt(
            konto_id=konto_id, vertrag_id=vertrag.id, op_service=deps._op_service,
            mahnkosten_repo=deps._hv_mail.mahnkosten_repo, ruecklastschriftkosten_repo=deps._ruecklastschriftkosten_repo,
            mahnlauf_repo=deps._hv_mail.mahnlauf_repo,
        )
        zinsabrechnung_link_html = f' &nbsp;|&nbsp; <a href="/backoffice/vertrag/{h(vertrag.id)}/zinsabrechnung">Zinsabrechnung</a>'
        kontoblatt_karte = render_kontoblatt_html(kontoblatt, konto_id=konto_id, zinsabrechnung_link_html=zinsabrechnung_link_html)

    sperren_zeilen = "".join(
        f"<tr><td>{h(sperrgrund_label(s.grund))} <span class='muted'>({h(s.grund)})</span></td>"
        f"<td>{s.gesetzt_am.isoformat() if s.gesetzt_am else ''}</td>"
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

    return _layout(
        request, session, f"Kontoauszug {konto_id}",
        banner + stammdaten_karte + sperren_karte + kontoblatt_karte + op_tabelle + links,
    )


@router.get("/konto/{konto_id}/kontoauszug.pdf")
def kontoauszug_pdf(konto_id: str, session=Depends(_current_session)):
    """Druckfähiger JLB-PDF-Kontoauszug (Auftrag HV-20261020-KONTOBLATT-
    KOSTEN) - GENAU dieselben Daten wie die Kostenübersicht oben
    (`lade_kontoblatt`), reiner GET: liest nur, bucht/sendet/verändert
    nichts. Für ein von der Pilotphase ausgeschlossenes Objekt (Objekt
    107) gibt es KEINEN PDF-Kontoauszug - dieselbe Fachregel wie überall
    sonst in diesem Modul."""

    konto = deps._stammdaten_repo.get_konto(konto_id)
    if konto is None:
        return _fehlerseite(session, "Kontoauszug", f"Unbekanntes Konto {konto_id}.")
    vertrag = deps._stammdaten_repo.get_vertrag(konto.vertrag_id)
    if vertrag is None:
        return _fehlerseite(session, "Kontoauszug", "Stammdaten unvollständig.")
    require_gesellschaft_access(_ctx(session), vertrag.gesellschaft_id)
    if _objekt_fuer_vertrag_gesperrt(konto.vertrag_id):
        return _fehlerseite(session, "Kontoauszug", "Objekt ist von der Pilotphase ausgeschlossen - kein PDF-Kontoauszug.")

    einheit = deps._stammdaten_repo.get_einheit(vertrag.einheit_id)
    objekt = deps._stammdaten_repo.objekt_fuer_vertrag(vertrag.id)
    debitor = deps._stammdaten_repo.get_debitor(konto.debitor_id)
    if einheit is None or objekt is None or debitor is None:
        return _fehlerseite(session, "Kontoauszug", "Stammdaten unvollständig.")

    kontoblatt = lade_kontoblatt(
        konto_id=konto_id, vertrag_id=vertrag.id, op_service=deps._op_service,
        mahnkosten_repo=deps._hv_mail.mahnkosten_repo, ruecklastschriftkosten_repo=deps._ruecklastschriftkosten_repo,
        mahnlauf_repo=deps._hv_mail.mahnlauf_repo,
    )
    absender = _BriefAbsender(
        name=deps._settings.brief_absender_name, adresse=deps._settings.brief_absender_adresse, fn=deps._settings.brief_absender_fn,
        uid=deps._settings.brief_absender_uid, telefon=deps._settings.brief_absender_telefon, website=deps._settings.brief_absender_website,
        email=deps._settings.brief_absender_email, farbe_anthrazit=deps._settings.brief_farbe_anthrazit, farbe_gold=deps._settings.brief_farbe_gold,
        logo_pfad=deps._settings.brief_logo_pfad, font_regular_pfad=deps._settings.brief_font_regular_pfad, font_bold_pfad=deps._settings.brief_font_bold_pfad,
        font_headline_pfad=deps._settings.brief_font_headline_pfad,
    )
    pdf_bytes = erzeuge_kontoauszug_pdf(
        absender=absender, objekt_bezeichnung=objekt.bezeichnung, einheit_bezeichnung=einheit.bezeichnung,
        debitor_name=debitor.name, vertrag_id=vertrag.id, konto_id=konto_id, heute=date.today(), kontoblatt=kontoblatt,
    )
    dateiname = f"Kontoauszug_{konto_id}_{date.today().isoformat()}.pdf"
    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{dateiname}"'},
    )


# -- Zinsabrechnung: Preview -> Bestätigen (Auftrag HV-20261022-EIN-KONTOBLATT) --


def _zinsabrechnung_vorschau_html(vorschau, *, vertrag_id: str, konto_id: str, session) -> str:
    satz_text = f"{vorschau.zinssatz_prozent} % p.a." if vorschau.zinssatz_prozent is not None else "siehe Zinssegmente (Halbjahreswechsel/Basiszinssatz)"
    zeitraum_text = (
        f"{vorschau.zins_von.isoformat()} – {vorschau.zins_bis.isoformat()}"
        if vorschau.zins_von and vorschau.zins_bis else "kein verzinsbarer Zeitraum"
    )
    hinweis_ungeklaert = (
        '<p class="warn">Mindestens ein Zeitsegment hat eine (noch) ungeklärte Zinsbasis - für dieses '
        "Segment wird KEIN Zins angesetzt, bis die Basis geklärt/belegt ist.</p>"
    ) if vorschau.zins_teilweise_ungeklaert else ""
    gebuehr_hinweis = ""
    if vorschau.gebuehr_cent or vorschau.mahnpauschale_cent:
        gebuehr_hinweis = (
            '<p class="warn">Diese Vorschau würde bei einem tatsächlichen Mahnversand zusätzlich Mahnspesen/'
            "-pauschale auslösen - die Zinsabrechnung selbst bucht davon NICHTS, Gebühren/Pauschale bleiben "
            "ausschließlich an einen tatsächlichen, nachgewiesenen Mahnversand gebunden.</p>"
        )
    kein_delta_hinweis = (
        '<p class="ok">Kein neues Zinsdelta seit der letzten Buchung - eine Bestätigung wäre ein reiner No-Op.</p>'
    ) if vorschau.neue_zinsen_delta_cent <= 0 else ""
    bestaetigen_form = f"""
    <form method="post" action="/backoffice/vertrag/{h(vertrag_id)}/zinsabrechnung/bestaetigen">
      {csrf_feld(session.csrf_token)}
      <button type="submit">Zinsabrechnung jetzt verbuchen (nur Delta, keine Gebühren)</button>
    </form>"""
    return f"""
    <div class="card" style="max-width:720px;">
      <h1>Zinsabrechnung — Vertrag {h(vertrag_id)}</h1>
      <p class="muted">Reine Vorschau (GET, bucht nichts). Eine Buchung erfolgt ausschließlich über "Bestätigen"
         unten (POST) - niemals automatisch.</p>
      <table>
        <tr><th>Satz</th><td>{h(satz_text)}</td></tr>
        <tr><th>Zeitraum</th><td>{h(zeitraum_text)}</td></tr>
        <tr><th>Verzinste Kapitalbasis</th><td>{eur(vorschau.hauptforderung_cent)}</td></tr>
        <tr><th>Bereits gebucht (gesamt, alle Stufen/Abrechnungen)</th><td>{eur(vorschau.bereits_gebuchte_zinsen_cent)}</td></tr>
        <tr><th>Neu zu buchendes Delta</th><td><strong>{eur(vorschau.neue_zinsen_delta_cent)}</strong></td></tr>
      </table>
      {hinweis_ungeklaert}
      {gebuehr_hinweis}
      {kein_delta_hinweis}
      {bestaetigen_form if vorschau.neue_zinsen_delta_cent > 0 else ''}
      <p><a href="/backoffice/konto/{h(konto_id)}">&larr; zurück zum Kontoauszug</a></p>
    </div>"""


@router.get("/vertrag/{vertrag_id}/zinsabrechnung", response_class=HTMLResponse)
def zinsabrechnung_vorschau_route(request: Request, vertrag_id: str, session=Depends(_current_session)) -> HTMLResponse:
    """Reine Lesevorschau (GET, mutationsfrei) einer eigenständigen
    Zinsabrechnung - zeigt Satz/Zeitraum/Kapital/bereits gebucht/Delta,
    bucht NICHTS (siehe `MahnkostenService.zinsabrechnung_vorschau`)."""

    vertrag = deps._stammdaten_repo.get_vertrag(vertrag_id)
    if vertrag is None:
        return _fehlerseite(session, "Zinsabrechnung", f"Unbekannter Vertrag {vertrag_id}.")
    require_gesellschaft_access(_ctx(session), vertrag.gesellschaft_id)
    if _objekt_fuer_vertrag_gesperrt(vertrag_id):
        return _fehlerseite(session, "Zinsabrechnung", "Objekt ist von der Pilotphase ausgeschlossen - keine Zinsabrechnung.")
    konto = deps._stammdaten_repo.get_konto_by_vertrag(vertrag_id)
    if konto is None:
        return _fehlerseite(session, "Zinsabrechnung", "Kein Konto für diesen Vertrag gefunden.")

    vorschau = deps._hv_mail.mahnkosten_service.zinsabrechnung_vorschau(vertrag_id=vertrag_id, heute=date.today())
    if vorschau is None:
        return _fehlerseite(session, "Zinsabrechnung", "Keine Vorschau möglich (Kontodaten unvollständig).", f"/backoffice/konto/{konto.id}")
    inhalt = _zinsabrechnung_vorschau_html(vorschau, vertrag_id=vertrag_id, konto_id=konto.id, session=session)
    return _layout(request, session, "Zinsabrechnung", inhalt)


@router.post("/vertrag/{vertrag_id}/zinsabrechnung/bestaetigen")
def zinsabrechnung_bestaetigen(
    request: Request, vertrag_id: str, csrf_token: str = Form(...), session=Depends(_current_session),
):
    """Einzige Buchungsstelle einer eigenständigen Zinsabrechnung -
    AUSSCHLIESSLICH per POST erreichbar (nie über GET/PDF). Bucht NIE
    Gebühren/Mahnpauschale (siehe `MahnkostenService.
    buche_zinsabrechnung`), NUR das tatsächlich neue Zinsdelta."""

    _verify_csrf(session, csrf_token)
    vertrag = deps._stammdaten_repo.get_vertrag(vertrag_id)
    if vertrag is None:
        return _fehlerseite(session, "Zinsabrechnung", f"Unbekannter Vertrag {vertrag_id}.")
    require_gesellschaft_access(_ctx(session), vertrag.gesellschaft_id)
    if _objekt_fuer_vertrag_gesperrt(vertrag_id):
        return _fehlerseite(session, "Zinsabrechnung", "Objekt ist von der Pilotphase ausgeschlossen - keine Zinsabrechnung.")
    konto = deps._stammdaten_repo.get_konto_by_vertrag(vertrag_id)
    if konto is None:
        return _fehlerseite(session, "Zinsabrechnung", "Kein Konto für diesen Vertrag gefunden.")

    try:
        ledger = deps._hv_mail.mahnkosten_service.buche_zinsabrechnung(
            ctx=_ctx(session), vertrag_id=vertrag_id, heute=date.today(), akteur=session.user_id,
        )
        if ledger is not None:
            deps._audit_service.log(
                entity_typ="mahnkosten_buchung", entity_id=str(ledger.id), akteur=session.user_id,
                aktion="ZINSABRECHNUNG", payload={"vertrag_id": vertrag_id},
            )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Zinsabrechnung", str(exc), f"/backoffice/vertrag/{vertrag_id}/zinsabrechnung")
    return RedirectResponse(url=f"/backoffice/konto/{konto.id}?zinsabrechnung=1", status_code=303)


# -- Nachbuchung ----------------------------------------------------------------


@router.get("/konto/{konto_id}/buchen", response_class=HTMLResponse)
def nachbuchung_formular(request: Request, konto_id: str, session=Depends(_current_session)) -> HTMLResponse:
    konto = deps._stammdaten_repo.get_konto(konto_id)
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
    konto = deps._stammdaten_repo.get_konto(konto_id)
    if konto is None:
        return _fehlerseite(session, "Nachbuchung", f"Unbekanntes Konto {konto_id}.")
    try:
        op_typ = OPTyp(typ)
        if op_typ not in (OPTyp.SOLL, OPTyp.GUTSCHRIFT):
            raise ValueError("Nur SOLL/GUTSCHRIFT sind über die manuelle Nachbuchung zulässig.")
        betrag_cent = parse_eur_betrag(betrag)
        faelligkeit_datum = date.fromisoformat(faelligkeit) if faelligkeit else None
        position = deps._op_service.buchen(
            ctx=_ctx(session), konto=konto, typ=op_typ, betrag_cent=betrag_cent, belegdatum=belegdatum,
            buchungsdatum=date.today(), faelligkeit=faelligkeit_datum, beleg_referenz=beleg_referenz,
            aenderungsgrund=grund, import_id=f"BACKOFFICE-BUCHUNG-{vorgangs_id}", quelle_system="backoffice",
        )
        deps._audit_service.log(
            entity_typ="op_position", entity_id=str(position.id), aktion="nachbuchung", akteur=session.user_id,
            payload={"konto_id": konto_id, "typ": typ, "betrag_cent": betrag_cent, "vorgangs_id": vorgangs_id, "grund": grund},
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Nachbuchung", str(exc), f"/backoffice/konto/{konto_id}/buchen")
    return RedirectResponse(url=f"/backoffice/konto/{konto_id}?gebucht=1", status_code=303)


# -- Korrektur/Storno -----------------------------------------------------------


@router.get("/op/{op_id}/korrigieren", response_class=HTMLResponse)
def korrektur_formular(request: Request, op_id: int, session=Depends(_current_session)) -> HTMLResponse:
    position = deps._op_service.get_position(op_id)
    if position is None:
        return _fehlerseite(session, "Korrektur", f"Unbekannte OP-Position {op_id}.")
    konto = deps._stammdaten_repo.get_konto(position.konto_id)
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
    position = deps._op_service.get_position(op_id)
    if position is None:
        return _fehlerseite(session, "Korrektur", f"Unbekannte OP-Position {op_id}.")
    konto = deps._stammdaten_repo.get_konto(position.konto_id)
    if konto is None:
        return _fehlerseite(session, "Korrektur", f"Konto {position.konto_id} nicht gefunden.")
    try:
        neuer_betrag_cent = parse_eur_betrag(neuer_betrag) if neuer_betrag and neuer_betrag.strip() else None
        neue_faelligkeit_datum = date.fromisoformat(neue_faelligkeit) if neue_faelligkeit else None
        ergebnis = deps._op_service.storniere_und_korrigiere(
            ctx=_ctx(session), konto=konto, original_id=op_id, aenderungsgrund=grund,
            neuer_betrag_cent=neuer_betrag_cent, neue_faelligkeit=neue_faelligkeit_datum, vorgang_id=vorgangs_id,
        )
        deps._audit_service.log(
            entity_typ="op_position", entity_id=str(op_id), aktion="storno_korrektur", akteur=session.user_id,
            payload={
                "grund": grund, "neuer_betrag_cent": neuer_betrag_cent, "vorgangs_id": vorgangs_id,
                "ersatz_id": ergebnis.id if ergebnis else None,
            },
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Korrektur", str(exc), f"/backoffice/op/{op_id}/korrigieren")
    return RedirectResponse(url=f"/backoffice/konto/{position.konto_id}?korrigiert=1", status_code=303)


# -- Rücklastschriftkosten (Auftrag HV-20261019-MAHNKOSTEN) -----------------


@router.get("/op/{op_id}/ruecklastschriftkosten", response_class=HTMLResponse)
def ruecklastschriftkosten_formular(request: Request, op_id: int, session=Depends(_current_session)) -> HTMLResponse:
    position = deps._op_service.get_position(op_id)
    if position is None:
        return _fehlerseite(session, "Rücklastschriftkosten", f"Unbekannte OP-Position {op_id}.")
    if position.typ != OPTyp.RUECKLASTSCHRIFT.value:
        return _fehlerseite(session, "Rücklastschriftkosten", f"OP #{op_id} ist keine Rücklastschrift (Typ {h(position.typ)}).")
    konto = deps._stammdaten_repo.get_konto(position.konto_id)
    if konto is None:
        return _fehlerseite(session, "Rücklastschriftkosten", f"Konto {position.konto_id} nicht gefunden.")
    vertrag = deps._stammdaten_repo.get_vertrag(konto.vertrag_id)
    if vertrag is not None:
        require_gesellschaft_access(_ctx(session), vertrag.gesellschaft_id)

    bestehend = deps._ruecklastschriftkosten_repo.fuer_ruecklastschrift(op_id)
    if bestehend is not None:
        zurechenbar_text = (
            "Ja - als Forderung gebucht" if bestehend.dem_zahlungspflichtigen_zurechenbar
            else "Nein - rein dokumentarisch, KEINE Forderung gebucht"
        )
        inhalt = f"""
        <div class="card" style="max-width:560px;">
          <h1>Rücklastschriftkosten — OP #{op_id}</h1>
          <p class="ok">Bereits erfasst{' am ' + bestehend.gebucht_am.strftime('%d.%m.%Y %H:%M') if bestehend.gebucht_am else ''}
             von {h(bestehend.erstellt_von)}.</p>
          <table>
            <tr><th>Originalbankentgelt</th><td>{eur(bestehend.bankentgelt_cent)}</td></tr>
            <tr><th>Rückgabeursache</th><td>{h(bestehend.rueckgabeursache)}</td></tr>
            <tr><th>Dem Zahlungspflichtigen zurechenbar</th><td>{h(zurechenbar_text)}</td></tr>
            <tr><th>Begründung Zurechenbarkeit</th><td>{h(bestehend.zurechenbarkeit_begruendung)}</td></tr>
            <tr><th>Beleg-Referenz</th><td>{h(bestehend.beleg_referenz)}</td></tr>
          </table>
          <p class="muted">Eine Rücklastschrift-OP darf nur EINMAL erfasst werden (keine Doppelbuchung) -
             diese Erfassung ist abgeschlossen.</p>
          <p><a href="/backoffice/konto/{h(konto.id)}">&larr; zurück zum Kontoauszug</a></p>
        </div>"""
        return _layout(request, session, "Rücklastschriftkosten", inhalt)

    inhalt = f"""
    <div class="card" style="max-width:560px;">
      <h1>Rücklastschriftkosten erfassen — OP #{op_id}</h1>
      <table>
        <tr><th>Rücklastschrift-Betrag</th><td>{eur(position.betrag_cent)}</td></tr>
        <tr><th>Belegdatum</th><td>{position.belegdatum.isoformat()}</td></tr>
        <tr><th>Beleg-Referenz</th><td>{h(position.beleg_referenz or '')}</td></tr>
      </table>
      <form method="post" action="/backoffice/op/{op_id}/ruecklastschriftkosten/erfassen">
        {csrf_feld(session.csrf_token)}
        <label>Tatsächliches Originalbankentgelt in EUR (Pflicht - NUR der echte Bankbeleg, kein Pauschalbetrag)</label>
        <input name="bankentgelt_cent" required placeholder="z. B. 12,50">
        <label>Rückgabeursache (Pflicht, laut Bankbeleg)</label>
        <input name="rueckgabeursache" required placeholder="z. B. Mangels Deckung">
        <label><input type="checkbox" name="dem_zahlungspflichtigen_zurechenbar" value="1">
          Dem Zahlungspflichtigen zurechenbar (z. B. KEIN Bank-/SEPA-Fehler) - nur dann wird eine Forderung gebucht</label>
        <label>Begründung Zurechenbarkeit (Pflicht - bei einem Bankfehler ausdrücklich NICHT zurechenbar)</label>
        <textarea name="zurechenbarkeit_begruendung" rows="3" required></textarea>
        <label>Beleg-Referenz (Pflicht, z. B. Bankauszugs-/Belegnummer)</label>
        <input name="beleg_referenz" required>
        <button type="submit">Erfassen</button>
      </form>
      <p class="muted">Nur das tatsächlich belegte Bankentgelt wird gebucht - kein geschätzter/pauschaler Betrag.
         Ohne Zurechenbarkeit bleibt die Erfassung rein dokumentarisch (0 EUR Forderung).</p>
    </div>"""
    return _layout(request, session, "Rücklastschriftkosten", inhalt)


@router.post("/op/{op_id}/ruecklastschriftkosten/erfassen")
async def ruecklastschriftkosten_absenden(request: Request, op_id: int, session=Depends(_current_session)):
    form = await request.form()
    _verify_csrf(session, str(form.get("csrf_token", "")))
    position = deps._op_service.get_position(op_id)
    if position is None:
        return _fehlerseite(session, "Rücklastschriftkosten", f"Unbekannte OP-Position {op_id}.")
    konto = deps._stammdaten_repo.get_konto(position.konto_id)
    if konto is None:
        return _fehlerseite(session, "Rücklastschriftkosten", f"Konto {position.konto_id} nicht gefunden.")
    vertrag = deps._stammdaten_repo.get_vertrag(konto.vertrag_id)
    if vertrag is None:
        return _fehlerseite(session, "Rücklastschriftkosten", "Kein Vertrag für dieses Konto gefunden.")

    try:
        bankentgelt_cent = parse_eur_betrag(str(form.get("bankentgelt_cent", "")).strip())
        ergebnis = deps._ruecklastschriftkosten_service.erfasse_und_buche(
            ctx=_ctx(session), ruecklastschrift_op_position=position, vertrag_id=vertrag.id, konto=konto,
            bankentgelt_cent=bankentgelt_cent,
            rueckgabeursache=str(form.get("rueckgabeursache", "")).strip(),
            dem_zahlungspflichtigen_zurechenbar=str(form.get("dem_zahlungspflichtigen_zurechenbar", "")) == "1",
            zurechenbarkeit_begruendung=str(form.get("zurechenbarkeit_begruendung", "")).strip(),
            beleg_referenz=str(form.get("beleg_referenz", "")).strip(),
            heute=date.today(), akteur=session.user_id,
        )
        deps._audit_service.log(
            entity_typ="ruecklastschriftkosten", entity_id=str(ergebnis.id), akteur=session.user_id,
            aktion="ERFASST", payload={"op_position_id": op_id, "bankentgelt_cent": bankentgelt_cent},
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Rücklastschriftkosten", str(exc), f"/backoffice/op/{op_id}/ruecklastschriftkosten")
    return RedirectResponse(url=f"/backoffice/konto/{konto.id}?ruecklastschriftkosten_erfasst=1", status_code=303)


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
        konto = deps._stammdaten_repo.get_konto(zeile.konto_id)
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
            bestehende = deps._op_service.bestehende_eroeffnung(konto.id)
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
    konten_je_id = {z["konto_id"]: deps._stammdaten_repo.get_konto(z["konto_id"]) for z in zeilen if "konto_id" in z}
    try:
        ergebnisse = importiere_eroeffnung_csv_atomar(
            ctx=_ctx(session), op_service=deps._op_service, text=datei_inhalt, konten_je_id=konten_je_id,
            akteur=session.user_id, session_factory=deps._session_factory,
        )
        deps._audit_service.log(
            entity_typ="eroeffnung_import", entity_id=f"{len(ergebnisse)}-zeilen", aktion="atomar_verbucht",
            akteur=session.user_id, payload={"anzahl": len(ergebnisse), "konten": sorted(konten_je_id)},
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Eröffnungsimport", f"Import abgebrochen, NICHTS wurde verbucht: {exc}", "/backoffice/eroeffnung")
    inhalt = flash_ok(f"{len(ergebnisse)} Eröffnungszeile(n) atomar verbucht.") + '<p><a href="/backoffice/">&larr; zum Dashboard</a></p>'
    return _layout(request, session, "Eröffnungsimport erfolgreich", inhalt)


# -- Vorschreibungsentwurf --------------------------------------------------------


@router.get("/vertrag/{vertrag_id}/vorschreibung", response_class=HTMLResponse)
def vorschreibung_formular(request: Request, vertrag_id: str, monat: str | None = None, session=Depends(_current_session)) -> HTMLResponse:
    vertrag = deps._stammdaten_repo.get_vertrag(vertrag_id)
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

    einheit = deps._stammdaten_repo.get_einheit(vertrag.einheit_id)
    historisch = vertrag.gueltig_bis is not None and monat and vertrag.gueltig_bis < faelligkeitsdatum(monat, 28)
    leerstand = einheit is not None and einheit.nutzungsstatus == "LEERSTAND"

    if monat:
        try:
            ergebnis = deps._vorschreibung_service.entwurf_erstellen(ctx=_ctx(session), vertrag=vertrag, monat=monat)
            aufschluesselung = deps._vorschreibung_service.aufschluesselung(ergebnis.vorschreibung_id)
        except (MietinkassoError, ValueError) as exc:
            return _layout(request, session, "Vorschreibung", inhalt + flash_error(str(exc)))

        klausel = deps._index_repo.freigegebene_klausel(vertrag_id)
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
    vertrag = deps._stammdaten_repo.get_vertrag(vertrag_id)
    if vertrag is None:
        return _fehlerseite(session, "Vorschreibung", f"Unbekannter Vertrag {vertrag_id}.")
    einheit = deps._stammdaten_repo.get_einheit(vertrag.einheit_id)
    if vertrag.gueltig_bis is not None and vertrag.gueltig_bis < faelligkeitsdatum(monat, 28):
        return _fehlerseite(session, "Vorschreibung", "Vertrag ist historisch; Sollstellung wird nicht automatisch aktiviert.", f"/backoffice/vertrag/{vertrag_id}/vorschreibung?monat={monat}")
    if einheit is not None and einheit.nutzungsstatus == "LEERSTAND":
        return _fehlerseite(session, "Vorschreibung", "Einheit steht als LEERSTAND; Sollstellung wird nicht automatisch aktiviert.", f"/backoffice/vertrag/{vertrag_id}/vorschreibung?monat={monat}")
    konto = deps._stammdaten_repo.get_konto_by_vertrag(vertrag_id)
    if konto is None:
        return _fehlerseite(session, "Vorschreibung", "Kein Konto für diesen Vertrag vorhanden.")
    try:
        ergebnis = deps._vorschreibung_service.sollstellen(ctx=_ctx(session), vertrag=vertrag, konto=konto, monat=monat)
        deps._audit_service.log(
            entity_typ="vorschreibung", entity_id=str(ergebnis.vorschreibung_id), aktion="sollgestellt",
            akteur=session.user_id, payload={"vertrag_id": vertrag_id, "monat": monat, "summe_cent": ergebnis.summe_cent},
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Vorschreibung", str(exc), f"/backoffice/vertrag/{vertrag_id}/vorschreibung?monat={monat}")
    return RedirectResponse(url=f"/backoffice/konto/{konto.id}?sollgestellt=1", status_code=303)
