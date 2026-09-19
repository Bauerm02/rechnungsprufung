"""Vertragsbezogene Indexgrundlagen: Indexklauseln und Rechtsprofile.

Beides läuft über Entwurf -> ausdrückliche Freigabe; nur GENAU EIN
freigegebenes Rechtsprofil treibt die monatliche Indexautomatik an."""

from __future__ import annotations

from datetime import date
from html import escape as h

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from mietinkasso.auth.service import require_gesellschaft_access
from mietinkasso.backoffice.indexklausel_form import klausel_form_werte, klausel_formular
from mietinkasso.backoffice.views import csrf_feld, eur, option, parse_eur_betrag
from mietinkasso.domain.exceptions import MietinkassoError
from mietinkasso.indexautomatik.zeit import heute_wien
from mietinkasso.infrastructure.db.tables import IndexKlauselTable

from mietinkasso.backoffice import dependencies as deps
from mietinkasso.backoffice.auth import _ctx, _current_session, _fehlerseite, _layout, _objekt_fuer_vertrag_gesperrt, _verify_csrf
from mietinkasso.backoffice.routes.shared import _RECHTSORDNUNGEN_FUER_AUSWAHL


#: Ohne eigenes Prefix - das gemeinsame `/backoffice`-Prefix wird GENAU
#: EINMAL in `backoffice/app.py` gesetzt.
router = APIRouter()


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


def _historische_belege_aus_form(ids, betraege, daten, quellen, erlaubte_ids):
    if not (len(ids) == len(betraege) == len(daten) == len(quellen)):
        raise ValueError("Unvollständige Belegfelder; bitte das Formular neu laden.")
    belege = {}
    for kid, betrag, datum, quelle in zip(ids, betraege, daten, quellen):
        if not any((betrag.strip(), datum.strip(), quelle.strip())):
            continue
        if kid not in erlaubte_ids or kid in belege:
            raise ValueError("Der Basisbeleg muss zu einer ausgewählten Mietkomponente gehören.")
        if not all((betrag.strip(), datum.strip(), quelle.strip())):
            raise ValueError("Jeder Basisbeleg braucht Betrag, Belegdatum und Quelle.")
        cent = parse_eur_betrag(betrag)
        if cent <= 0:
            raise ValueError("Der belegte Basisbetrag muss positiv sein.")
        belege[kid] = {"betrag_cent": cent, "datum": date.fromisoformat(datum).isoformat(),
                       "quellenbeleg": quelle.strip()}
    return belege


@router.get("/vertrag/{vertrag_id}/indexklauseln", response_class=HTMLResponse)
def indexklauseln_uebersicht(request: Request, vertrag_id: str, session=Depends(_current_session)):
    vertrag = deps._stammdaten_repo.get_vertrag(vertrag_id)
    if vertrag is None or _objekt_fuer_vertrag_gesperrt(vertrag_id):
        return _fehlerseite(session, "Indexklauseln", "Vertrag nicht verfügbar.")
    require_gesellschaft_access(_ctx(session), vertrag.gesellschaft_id)
    komponenten = deps._stammdaten_repo.list_aktive_komponenten(vertrag_id, heute_wien())
    with deps._session_factory() as db:
        klauseln = list(db.execute(select(IndexKlauselTable).where(IndexKlauselTable.vertrag_id == vertrag_id)
                                  .order_by(IndexKlauselTable.version.desc())).scalars())
    return _layout(request, session, "Vertragliche Indexklauseln",
                   klausel_formular(vertrag, komponenten, klauseln, session.csrf_token))


@router.post("/vertrag/{vertrag_id}/indexklausel/erstellen")
async def indexklausel_erstellen(request: Request, vertrag_id: str, session=Depends(_current_session)):
    form = await request.form()
    _verify_csrf(session, str(form.get("csrf_token", "")))
    vertrag = deps._stammdaten_repo.get_vertrag(vertrag_id)
    if vertrag is None or _objekt_fuer_vertrag_gesperrt(vertrag_id):
        return _fehlerseite(session, "Indexklauseln", "Vertrag nicht verfügbar.")
    try:
        werte = klausel_form_werte(form, vertrag, deps._stammdaten_repo.list_aktive_komponenten(vertrag_id, heute_wien()))
        klausel = deps._indexautomatik.index_service.klausel_anlegen(ctx=_ctx(session), **werte)
        deps._audit_service.log(entity_typ="index_klausel", entity_id=str(klausel.id), aktion="ENTWURF_ERFASST",
                           akteur=session.user_id, payload={"vertrag_id": vertrag_id, "version": klausel.version})
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Indexklauseln", str(exc), f"/backoffice/vertrag/{vertrag_id}/indexklauseln")
    return RedirectResponse(f"/backoffice/vertrag/{vertrag_id}/indexklauseln", status_code=303)


@router.post("/indexklausel/{klausel_id}/freigeben")
def indexklausel_freigeben(request: Request, klausel_id: int, csrf_token: str = Form(...), session=Depends(_current_session)):
    _verify_csrf(session, csrf_token)
    klausel = deps._indexautomatik.index_repository.get_klausel(klausel_id)
    if klausel is None or klausel.status != "ENTWURF" or _objekt_fuer_vertrag_gesperrt(klausel.vertrag_id):
        return _fehlerseite(session, "Indexklauseln", "Kein bestätigbarer Vertragsentwurf vorhanden.")
    try:
        deps._indexautomatik.index_service.klausel_freigeben(klausel_id, ctx=_ctx(session), freigegeben_von=session.user_id)
        deps._audit_service.log(entity_typ="index_klausel", entity_id=str(klausel_id), aktion="KLAUSEL_BESTAETIGT",
                           akteur=session.user_id, payload={"vertrag_id": klausel.vertrag_id, "version": klausel.version})
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Indexklauseln", str(exc))
    return RedirectResponse(f"/backoffice/vertrag/{klausel.vertrag_id}/rechtsprofil", status_code=303)


@router.get("/vertrag/{vertrag_id}/rechtsprofil", response_class=HTMLResponse)
def rechtsprofil_uebersicht(request: Request, vertrag_id: str, session=Depends(_current_session)) -> HTMLResponse:
    vertrag = deps._stammdaten_repo.get_vertrag(vertrag_id)
    if vertrag is None:
        return _fehlerseite(session, "Rechtsprofil", f"Unbekannter Vertrag {vertrag_id}.")
    if _objekt_fuer_vertrag_gesperrt(vertrag_id):
        return _fehlerseite(session, "Rechtsprofil", "Objekt ist gesperrt; kein Rechtsprofil möglich.")

    rechtsordnung_optionen = "".join(
        option(r, r, selected=(r == vertrag.rechtsordnung)) for r in _RECHTSORDNUNGEN_FUER_AUSWAHL
    )
    komponenten = deps._stammdaten_repo.list_aktive_komponenten(vertrag_id, date.today())
    komponenten_html = "".join(
        f'<label class="muted"><input type="checkbox" name="basis_komponenten_ids" value="{h(k.id)}"> '
        f"{h(k.bezeichnung)} ({eur(k.betrag_cent)}, {h(k.art)})</label><br>"
        for k in komponenten if k.indexierbar
    ) or '<p class="muted">Keine als indexierbar markierte Komponente vorhanden.</p>'
    belege_html = "".join(
        f'<fieldset><legend>{h(k.bezeichnung)} – bisheriger Basisbetrag</legend>'
        f'<input type="hidden" name="beleg_komponente_id" value="{h(k.id)}">'
        '<label>Belegter Betrag (EUR, brutto)</label><input name="beleg_betrag" type="text">'
        '<label>Datum des Vertrags oder Betragsbelegs</label><input name="beleg_datum" type="date">'
        '<label>Datei und Fundstelle</label><input name="beleg_quelle" type="text"></fieldset>'
        for k in komponenten if k.indexierbar
    )
    klauseln = deps._indexautomatik.index_repository.freigegebene_klausel(vertrag_id)
    klausel_hinweis = (
        f'<p class="muted">Freigegebene IndexKlausel: #{klauseln.id} (Basis {klauseln.basis_reihe}={klauseln.basis_wert}, '
        f"Bezugsmonat {klauseln.basis_monat})</p>"
        if klauseln else '<p class="muted">Keine freigegebene IndexKlausel für diesen Vertrag vorhanden (siehe Index-Modul).</p>'
    )
    historie = deps._indexautomatik.rechtsprofil_service.liste_fuer_vertrag(vertrag_id)
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
      <p><a href="/backoffice/vertrag/{h(vertrag_id)}/indexklauseln">Vertragliche Indexklausel erfassen oder prüfen</a></p>
      {klausel_hinweis}
      <form method="post" action="/backoffice/vertrag/{h(vertrag_id)}/rechtsprofil/erstellen">
        {csrf_feld(session.csrf_token)}
        <fieldset>
          <legend>Rechtsklassifikation</legend>
          <label>Rechtsordnung</label>
          <select name="rechtsordnung" required>{rechtsordnung_optionen}</select>
          <label>Nutzung</label>
          <select name="ist_wohnungsnutzung"><option value="">ungeklärt</option>
            <option value="1">Wohnung (geprüft)</option><option value="0">Geschäftsraum (geprüft)</option></select><br>
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
          <details><summary>Basisbelege für bereits vor dem Import vereinbarte Mieten</summary>
            <p class="muted">Nur ausfüllen, wenn der Vertrag den unveränderten Basisbetrag belegt.
               Das Belegdatum bleibt das echte Unterschrifts- oder Ausstellungsdatum;
               der vereinbarte VPI-Bezugsmonat steht getrennt oben. Der Betrag wird vor Freigabe abgeglichen.</p>
            {belege_html}
          </details>
        </fieldset>
        <fieldset>
          <legend>Vertragliche Spur - GENAU EINE der beiden Varianten</legend>
          <label>Statischer vertraglich zulässiger Betrag (EUR, BRUTTO)</label>
          <input type="text" name="vertraglicher_betrag">
          <label>Quellenbeleg</label>
          <input type="text" name="vertraglicher_quellenbeleg">
          <label>Vertraglich frühestmöglicher Termin</label>
          <input type="date" name="vertraglicher_termin">
          <p class="muted">ODER: bestätigte Vertragsklausel für die wiederkehrende Berechnung</p>
          <label>Vertragsklausel</label>
          <select name="vertragsklausel_id"><option value="">keine ausgewählt</option>
            {option(str(klauseln.id), f'Version {klauseln.version}: {klauseln.basis_reihe}, Basis {klauseln.basis_monat}') if klauseln else ''}
          </select>
        </fieldset>
        <fieldset>
          <legend>Belege</legend>
          <label>Vertragsbeleg-Referenz (Pflicht)</label>
          <input type="text" name="vertrag_beleg_referenz" required>
          <label>Klauselreferenz</label>
          <input type="text" name="klausel_referenz">
          <label>Vertragliche Frist vom Zugang bis zur Wirksamkeit (Tage)</label>
          <input type="number" name="frist_tage_zugang_bis_wirksamkeit" min="0">
          <label>Beleg der Zugangsfrist</label>
          <input type="text" name="frist_quellenbeleg" placeholder="Vertrag, Seite und Absatz">
          <p class="muted">Leer bedeutet ungeklärt. Gesetzliche Mindestfristen werden zusätzlich geprüft.</p>
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
    beleg_komponente_id: list[str] = Form([]),
    beleg_betrag: list[str] = Form([]),
    beleg_datum: list[str] = Form([]),
    beleg_quelle: list[str] = Form([]),
    frist_tage_zugang_bis_wirksamkeit: str = Form(""),
    frist_quellenbeleg: str = Form(""),
    vertrag_beleg_referenz: str = Form(...),
    klausel_referenz: str = Form(""),
    csrf_token: str = Form(...),
    session=Depends(_current_session),
):
    _verify_csrf(session, csrf_token)
    try:
        deps._indexautomatik.rechtsprofil_service.entwurf_anlegen(
            ctx=_ctx(session), vertrag_id=vertrag_id, rechtsordnung=rechtsordnung,
            ist_wohnungsnutzung=(None if ist_wohnungsnutzung == "" else bool(int(ist_wohnungsnutzung))),
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
            historische_basis_belege=_historische_belege_aus_form(
                beleg_komponente_id, beleg_betrag, beleg_datum, beleg_quelle, set(basis_komponenten_ids)
            ),
            frist_tage_zugang_bis_wirksamkeit=(
                int(frist_tage_zugang_bis_wirksamkeit) if frist_tage_zugang_bis_wirksamkeit.strip() else None
            ),
            frist_quellenbeleg=frist_quellenbeleg.strip() or None,
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
    profil = deps._indexautomatik.rechtsprofil_repository.get(rechtsprofil_id)
    if profil is None:
        return _fehlerseite(session, "Rechtsprofil", f"Unbekanntes Rechtsprofil {rechtsprofil_id}.")
    try:
        deps._indexautomatik.rechtsprofil_service.freigeben(rechtsprofil_id, ctx=_ctx(session), freigegeben_von=session.user_id)
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Rechtsprofil", str(exc), f"/backoffice/vertrag/{profil.vertrag_id}/rechtsprofil")
    return RedirectResponse(url=f"/backoffice/vertrag/{profil.vertrag_id}/rechtsprofil", status_code=303)
