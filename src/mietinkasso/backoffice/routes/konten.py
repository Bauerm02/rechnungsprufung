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
from fastapi.responses import HTMLResponse, RedirectResponse

from mietinkasso.backoffice.views import csrf_feld, eur, flash_error, flash_ok, parse_eur_betrag, sperrgrund_label
from mietinkasso.domain.enums import OPTyp
from mietinkasso.domain.exceptions import MietinkassoError
from mietinkasso.index.service import UNTERSTUETZTE_BERECHNUNGSPROFILE
from mietinkasso.op.eroeffnung_import import importiere_eroeffnung_csv_atomar, parse_eroeffnung_csv
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
    einheit = deps._stammdaten_repo.get_einheit(vertrag.einheit_id) if vertrag else None
    debitor = deps._stammdaten_repo.get_debitor(konto.debitor_id)
    gesperrt = _objekt_fuer_vertrag_gesperrt(konto.vertrag_id)
    positionen = deps._op_service.list_alle_positionen(konto_id)
    saldo = deps._op_service.berechne_saldo(konto_id)
    aktive_sperren = deps._stammdaten_repo.aktive_sperren(vertrag.id) if vertrag is not None else []

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

    return _layout(request, session, f"Kontoauszug {konto_id}", banner + stammdaten_karte + sperren_karte + op_tabelle + links)


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
