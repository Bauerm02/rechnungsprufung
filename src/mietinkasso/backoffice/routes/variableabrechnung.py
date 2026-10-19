"""Variable Monatsabrechnung (Kurzzeitvermietung/Selfstorage), die daraus
abgeleitete Netto-Monatsübersicht und die Netto-Mietanteil-Freigabe je
Vertragskomponente.

Die Komponenten-Freigabe steht hier, weil nur sie darüber entscheidet,
welcher Komponentenbetrag in die Nettomieterlös-Monatsübersicht
einfließt."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from html import escape as h
import json
from pathlib import Path, PureWindowsPath
from sqlalchemy.engine import make_url

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from mietinkasso.auth.service import require_gesellschaft_access
from mietinkasso.backoffice.views import csrf_feld, eur, flash_error, flash_ok, option, parse_eur_betrag
from mietinkasso.domain.exceptions import MietinkassoError
from mietinkasso.variableabrechnung.csv_import import (
    VariableAbrechnungImportNichtAnwendbarError,
    erstelle_plan as _variable_abrechnung_erstelle_plan,
    parse_csv as _variable_abrechnung_parse_csv,
    wende_an as _variable_abrechnung_wende_an,
)
from mietinkasso.variableabrechnung.dashboard import berechne_monatsuebersicht

from mietinkasso.backoffice import dependencies as deps
from mietinkasso.backoffice.auth import _ctx, _current_session, _fehlerseite, _layout, _verify_csrf


#: Ohne eigenes Prefix - das gemeinsame `/backoffice`-Prefix wird GENAU
#: EINMAL in `backoffice/app.py` gesetzt.
router = APIRouter()


# -- Variable Monatsabrechnung (KURZZEITVERMIETUNG/SELFSTORAGE) --------------
# Auftrag 13.09., HV-20260913-DASHBOARD.


def _parse_optionalen_betrag(text: str | None) -> int | None:
    if not (text or "").strip():
        return None
    return parse_eur_betrag(text)


def _variable_abrechnung_einheiten_optionen(ausgewaehlt: str | None = None) -> str:
    optionen = ['<option value="">-- Einheit wählen --</option>']
    for objekt in deps._stammdaten_repo.list_objekte():
        for einheit in deps._stammdaten_repo.list_einheiten_fuer_objekt(objekt.id):
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
        f"<td>{eur(z.tatsaechlicher_zahlungseingang_cent) if z.tatsaechlicher_zahlungseingang_cent is not None else 'Nicht zugeordnet'}</td>"
        f"<td>{h(z.quelle_referenz)}</td>"
        f"<td><a href=\"/backoffice/variable-abrechnung/{z.id}/korrigieren\">Korrigieren</a> | "
        f"<a href=\"/backoffice/variable-abrechnung/versionen?einheit_id={h(z.einheit_id)}&art={h(z.art)}&monat={h(z.leistungsmonat)}\">Versionen</a></td>"
        "</tr>"
    )


@router.get("/variable-abrechnung", response_class=HTMLResponse)
def variable_abrechnung_liste(request: Request, monat: str | None = None, session=Depends(_current_session)) -> HTMLResponse:
    zeilen = deps._variableabrechnung.service.liste_aktuelle(ctx=_ctx(session), leistungsmonat=monat or None)
    zeilen_html = "".join(_variable_abrechnung_zeile_html(z) for z in zeilen) or (
        '<tr><td colspan=10 class="muted">Keine Monatsabrechnung vorhanden.</td></tr>'
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
        <a href="/backoffice/selfstorage-pruefung">Selfstorage: automatische Excel-Prüfung</a> &nbsp;|&nbsp;
        <a href="/backoffice/dashboard/monatsuebersicht">Monatsübersicht (Nettomieterlös)</a>
      </p>
      <table>
        <tr><th>Einheit</th><th>Art</th><th>Monat</th><th>Status</th><th>Version</th>
            <th>Unser Nettoanteil</th><th>Gemeldeter Betrag</th><th>Zahlungseingang</th><th>Quelle</th><th>Aktion</th></tr>
        {zeilen_html}
      </table>
    </div>
    """
    return _layout(request, session, "Variable Monatsabrechnung", inhalt)


@router.get('/selfstorage-pruefung', response_class=HTMLResponse)
def selfstorage_pruefung(request: Request, session=Depends(_current_session)):
    """Read-only results from the sole local workbook checker. Never posts amounts."""
    database = make_url(deps._settings.database_url).database
    path = Path(database or '.').parent / 'selfstorage-pruefung.json'
    if not path.is_file():
        return _layout(request, session, 'Selfstorage-Prüfung', '<div class="card"><h1>Selfstorage-Prüfung</h1><p>Noch kein Prüflauf vorhanden.</p></div>')
    data = json.loads(path.read_text(encoding='utf-8'))
    obj = deps._stammdaten_repo.objekt_fuer_einheit(data['unit'])
    require_gesellschaft_access(_ctx(session), obj.gesellschaft_id)
    deps._stammdaten_repo.pruefe_einheit_nicht_ausgeschlossen(data['unit'])
    rows = []
    labels = {'GERECHNET':'Rechnerisch geprüft', 'MONAT_ZU_BESTAETIGEN':'Abrechnungsmonat bestätigen', 'PRUEFUNG_ERFORDERLICH':'Formel oder Quelldaten prüfen'}
    for row in data.get('reports', []):
        amount = eur(row['payout_cent']) if row.get('payout_cent') is not None else 'Nicht berechnet'
        rows.append(f"<tr><td>{h(PureWindowsPath(row['source']).name)}</td><td>{h(row.get('period') or 'Noch nicht bestätigt')}</td><td>{amount}</td><td>{h(labels.get(row['status'],row['status']))}</td><td>{h(row.get('error',''))}</td></tr>")
    body = f'''<div class="card"><h1>Selfstorage: Excel-Prüfung</h1>
    <p>Letzter Lauf: {h(data.get('checked_at','unbekannt'))}. Quelle erreichbar: {'Ja' if data.get('source_available') else 'Nein'}.</p>
    <p>Neue Monatsdateien werden nachgerechnet. Fehlende Abrechnungsmonate und geänderte Formeln bleiben zur Prüfung offen.
    Eine rechnerisch passende Auszahlung bestätigt weder den Nettoertrag noch einen Bankeingang. Es werden keine Beträge aus dem Vormonat übernommen.</p>
    <table><tr><th>Datei</th><th>Monat</th><th>Auszahlung laut Berechnung</th><th>Prüfung</th><th>Grund</th></tr>{''.join(rows)}</table>
    <p><a href="/backoffice/variable-abrechnung">Zur Monatsabrechnung und den zugeordneten Zahlungen</a></p></div>'''
    return _layout(request, session, 'Selfstorage-Prüfung', body)


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
        deps._variableabrechnung.service.erfassen(
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
    zeile = deps._variableabrechnung.repository.get(id)
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
        objekt = deps._stammdaten_repo.objekt_fuer_einheit(zeile.einheit_id)
        require_gesellschaft_access(_ctx(session), objekt.gesellschaft_id)
        deps._stammdaten_repo.pruefe_einheit_nicht_ausgeschlossen(zeile.einheit_id)
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
        deps._variableabrechnung.service.korrigieren(
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
        versionen = deps._variableabrechnung.service.liste_versionen(ctx=_ctx(session), einheit_id=einheit_id, art=art, leistungsmonat=monat)
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
    plan = _variable_abrechnung_erstelle_plan(zeilen, ctx=_ctx(session), repository=deps._variableabrechnung.repository)

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
            service=deps._variableabrechnung.service, repository=deps._variableabrechnung.repository, akteur=session.user_id,
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
            ctx=_ctx(session), leistungsmonat=gewaehlter_monat, stammdaten_repository=deps._stammdaten_repo,
            variable_service=deps._variableabrechnung.service,
            komponenten_freigabe_service=deps._variableabrechnung.komponenten_freigabe_service,
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
    vertrag = deps._stammdaten_repo.get_vertrag(vertrag_id)
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
    komponenten = deps._stammdaten_repo.list_aktive_komponenten(vertrag_id, date.today())
    zeilen = []
    for k in komponenten:
        freigaben = deps._variableabrechnung.komponenten_freigabe_service.liste_fuer_komponente(ctx=_ctx(session), komponente_id=k.id)
        aktuelle_freigabe = next((f for f in freigaben if f.status == "FREIGEGEBEN"), None)
        freigabe_html = (
            f"AKTIV: {eur(aktuelle_freigabe.bestaetigter_netto_betrag_cent)} ab {aktuelle_freigabe.gueltig_von.isoformat()}"
            + (f" bis {aktuelle_freigabe.gueltig_bis.isoformat()}" if aktuelle_freigabe.gueltig_bis else " (unbefristet)")
            + (" [ENTWERTET - Komponente seither geändert]" if aktuelle_freigabe and not deps._variableabrechnung.komponenten_freigabe_service.ist_noch_gueltig(aktuelle_freigabe) else "")
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
      <p><a href="/backoffice/konto/{h(deps._stammdaten_repo.get_konto_by_vertrag(vertrag_id).id) if deps._stammdaten_repo.get_konto_by_vertrag(vertrag_id) else ''}">&larr; zurück zum Kontoauszug</a></p>
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
    komponente = deps._stammdaten_repo.get_komponente(komponente_id)
    if komponente is None or komponente.vertrag_id != vertrag_id:
        return _fehlerseite(
            session, "Netto-Mietanteil-Freigabe",
            f"Komponente {komponente_id} gehört nicht zu Vertrag {vertrag_id}.",
            f"/backoffice/vertrag/{vertrag_id}/komponenten-freigabe",
        )
    try:
        deps._variableabrechnung.komponenten_freigabe_service.freigeben(
            ctx=_ctx(session), komponente_id=komponente_id,
            bestaetigter_netto_betrag_cent=parse_eur_betrag(bestaetigter_netto_betrag),
            quellenbeleg_referenz=quellenbeleg_referenz, gueltig_von=date.fromisoformat(gueltig_von),
            gueltig_bis=date.fromisoformat(gueltig_bis) if gueltig_bis.strip() else None,
            freigegeben_von=session.user_id, aenderungsgrund=aenderungsgrund or None,
        )
    except (MietinkassoError, ValueError, InvalidOperation) as exc:
        return _fehlerseite(session, "Netto-Mietanteil-Freigabe", str(exc), f"/backoffice/vertrag/{vertrag_id}/komponenten-freigabe")
    return RedirectResponse(url=f"/backoffice/vertrag/{vertrag_id}/komponenten-freigabe", status_code=303)
