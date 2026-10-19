"""Index-Monatsbericht für den Eigentümer (Liste und Detail je Periode).

Zeigt ausschließlich, was der monatliche Indexautomatik-Lauf erzeugt hat
- nie eine zweite/eigene Berechnung. Der VPI-Ausfall bleibt vom
Versandstatus des Owner-Hinweises getrennt sichtbar."""

from __future__ import annotations

from html import escape as h

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from mietinkasso.backoffice.views import eur
from mietinkasso.indexautomatik.monatsbericht_service import status_label as _monatsbericht_status_label

from mietinkasso.backoffice import dependencies as deps
from mietinkasso.backoffice.auth import _current_session, _fehlerseite, _layout


#: Ohne eigenes Prefix - das gemeinsame `/backoffice`-Prefix wird GENAU
#: EINMAL in `backoffice/app.py` gesetzt.
router = APIRouter()


# -- Index-Monatsbericht (Auftrag HV-20260919-INDEX-MONATSBERICHT) -----------
# UI-Abnahme: "bisherige breite Excel-/Portalübersicht unten nicht lesbar -
# bitte schmale, responsive Mieter-Karten statt einer breiten Tabelle mit
# horizontalem Scrollen." Jede Zeile wird als eigene `.card` gerendert
# (kein <table>), Quellen-/Basisdetails stecken in einem `<details>`.

_MONATSBERICHT_STATUS_BADGE = {
    "MOEGLICH": "badge-ok", "NOCH_NICHT_MOEGLICH": "badge-muted", "PRUEFUNG_NOETIG": "badge-warn",
}


_MONATSBERICHT_KOPF_BADGE = {
    "BEREIT": "badge-muted", "IN_VERSAND": "badge-warn", "GESENDET": "badge-ok", "UNKLAR": "badge-error",
}


_MONATSBERICHT_KOPF_TEXT = {
    "BEREIT": "Bereit (noch nicht versendet)", "IN_VERSAND": "Versand läuft", "GESENDET": "Versendet",
    "UNKLAR": "Versand unklar - prüfen",
}


def _monatsbericht_kopf_badge(status: str) -> str:
    klasse = _MONATSBERICHT_KOPF_BADGE.get(status, "badge-muted")
    text = _MONATSBERICHT_KOPF_TEXT.get(status, status)
    return f'<span class="badge {klasse}">{h(text)}</span>'


def _vpi_ausfall_badge(vpi_fehlergrund: str | None) -> str:
    if not vpi_fehlergrund:
        return "-"
    return '<span class="badge badge-error">VPI-Ausfall</span>'


def _monatsbericht_kv_text(daten) -> str:
    if not daten:
        return "-"
    if isinstance(daten, dict):
        teile = []
        for schluessel, wert in daten.items():
            if wert is None or wert == "" or wert == []:
                continue
            teile.append(f"{h(str(schluessel))}: {_monatsbericht_kv_text(wert) if isinstance(wert, dict) else h(str(wert))}")
        return "; ".join(teile) or "-"
    return h(str(daten))


def _monatsbericht_termin_text(zeile) -> str:
    if zeile.fruehester_termin is None:
        return "kein Termin ermittelbar"
    zusatz = {"BEDINGT": " (bedingt)", "GEPRUEFT": " (geprüft)"}.get(zeile.fruehester_termin_status, "")
    return f"{zeile.fruehester_termin.isoformat()}{zusatz}"


def _monatsbericht_karte_html(zeile) -> str:
    snap = zeile.snapshot_json or {}
    mieter = snap.get("mieter_name") or zeile.vertrag_id
    objekt = snap.get("objekt_bezeichnung") or "-"
    einheit = snap.get("einheit_bezeichnung") or "-"
    betrag_bisher = eur(zeile.gesamtvorschreibung_cent) if zeile.gesamtvorschreibung_cent is not None else "unbekannt"
    vorschlag = eur(zeile.vorschlag_cent) if zeile.vorschlag_cent is not None else "noch nicht berechenbar"
    termin = _monatsbericht_termin_text(zeile)
    status_klasse = _MONATSBERICHT_STATUS_BADGE.get(zeile.status, "badge-muted")

    detail_zeilen = []
    if zeile.gesamtvorschreibung_quelle:
        detail_zeilen.append(f"<div>Quelle Betrag bisher: {h(zeile.gesamtvorschreibung_quelle)}</div>")
    if zeile.indexierbarer_mietanteil_cent is not None:
        detail_zeilen.append(f"<div>Davon indexierbarer Mietanteil: {eur(zeile.indexierbarer_mietanteil_cent)}</div>")
    elif zeile.indexierbarer_mietanteil_hinweis:
        detail_zeilen.append(f"<div>Indexierbarer Mietanteil: {h(zeile.indexierbarer_mietanteil_hinweis)}</div>")
    if zeile.differenz_cent is not None:
        detail_zeilen.append(f"<div>Differenz zum bisherigen indexierten Anteil: {eur(zeile.differenz_cent)}</div>")
    if zeile.rechtsprofil_id is not None:
        detail_zeilen.append(f"<div>Rechtsprofil: #{zeile.rechtsprofil_id} (Version {zeile.rechtsprofil_version})</div>")
    if zeile.quellen_fakten_id is not None:
        detail_zeilen.append(f"<div>Quellenfakten: #{zeile.quellen_fakten_id} - noch keine Ausführungsfreigabe</div>")
    if zeile.erhoehungsschreiben_id is not None:
        detail_zeilen.append(
            f'<div>Erhöhungsschreiben: <a href="/backoffice/indexautomatik/outbox">#{zeile.erhoehungsschreiben_id}</a></div>'
        )
    urspruengliche_basis = snap.get("urspruengliche_vertragsbasis")
    if urspruengliche_basis:
        detail_zeilen.append(f"<div>Ursprüngliche Vertragsbasis: {_monatsbericht_kv_text(urspruengliche_basis)}</div>")
    dokumentierte_basis = snap.get("zuletzt_dokumentierte_basis")
    if dokumentierte_basis:
        detail_zeilen.append(
            f"<div>Zuletzt dokumentierte Basis (freigegebenes Rechtsprofil): {_monatsbericht_kv_text(dokumentierte_basis)}</div>"
        )
    letzte_indexierung = snap.get("letzte_tatsaechliche_indexierung")
    if letzte_indexierung:
        detail_zeilen.append(f"<div>Letzte tatsächlich umgesetzte Indexierung: {_monatsbericht_kv_text(letzte_indexierung)}</div>")
    hinweise = snap.get("quellenfakten_hinweise")
    if hinweise:
        detail_zeilen.append(f"<div>Quellenfakten-Hinweise: {_monatsbericht_kv_text(hinweise)}</div>")
    details_html = "".join(detail_zeilen) or '<div class="muted">Keine weiteren Quellendetails hinterlegt.</div>'

    return f"""
    <div class="card monatsbericht-karte">
      <div class="monatsbericht-kopf">
        <strong>{h(objekt)} / {h(mieter)}</strong>
        <span class="muted">({h(einheit)}, Vertrag {h(zeile.vertrag_id)})</span>
        <span class="badge {status_klasse}">{h(_monatsbericht_status_label(zeile.status))}</span>
      </div>
      <div class="monatsbericht-zeilen">
        <div><span class="muted">Betrag bisher:</span> {betrag_bisher}</div>
        <div><span class="muted">Vorschlag:</span> {vorschlag}</div>
        <div><span class="muted">Termin:</span> {h(termin)}</div>
        <div><span class="muted">Grund:</span> {h(zeile.status_grund)}</div>
      </div>
      <p><a href="/backoffice/vertrag/{h(zeile.vertrag_id)}">Mieterakte öffnen &rarr;</a></p>
      <details class="tx-details"><summary>Quellen/Basisdetails</summary>{details_html}</details>
    </div>
    """


@router.get("/indexautomatik/monatsbericht", response_class=HTMLResponse)
def indexautomatik_monatsbericht_liste(request: Request, session=Depends(_current_session)) -> HTMLResponse:
    berichte = deps._indexautomatik.monatsbericht_repository.liste_alle()
    zeilen = "".join(
        f'<tr><td><a href="/backoffice/indexautomatik/monatsbericht/{h(b.periode)}">{h(b.periode)}</a></td>'
        f"<td>{_monatsbericht_kopf_badge(b.status)}</td>"
        # Codex-Korrektur (Schlussreview 2d45e27): `vpi_fehlergrund` ist
        # vom Versandstatus/`fehlergrund` (Transport-Diagnose) getrennt -
        # eine eigene Spalte, damit ein VPI-Ausfall auch NACH bereits
        # versendetem/UNKLAREM Owner-Hinweis in der Übersicht sichtbar
        # bleibt (nicht nur auf der Detailseite).
        f"<td>{_vpi_ausfall_badge(b.vpi_fehlergrund)}</td>"
        f"<td>{b.anzahl_vertraege}</td><td>{b.anzahl_moeglich}</td><td>{b.anzahl_noch_nicht_moeglich}</td>"
        f"<td>{b.anzahl_pruefung_noetig}</td><td>{h(b.fehlergrund or '-')}</td></tr>"
        for b in berichte
    ) or '<tr><td colspan=8 class="muted">Noch kein Monatsbericht erzeugt.</td></tr>'
    aktivierung_text = (
        f"aktiv ab Periode {h(deps._settings.index_monatsbericht_send_ab)}" if deps._settings.index_monatsbericht_send_ab
        else "noch nicht gesetzt - kein automatischer Versand irgendeiner Periode"
    )
    inhalt = f"""
    <div class="card">
      <h1>Index-Monatsbericht (Owner)</h1>
      <p class="muted">Ein Bericht je Kalendermonat, EINE Zeile je aktivem Vertrag - erzeugt automatisch vom
         monatlichen Indexautomatik-Lauf, NIE eine zweite/eigene Berechnung.</p>
      <p class="status-zeile">
        <span>Owner-Mailversand (Monatsbericht):
          <strong>{"aktiviert" if deps._settings.index_monatsbericht_send_enabled else "deaktiviert"}</strong>,
          Aktivierungsperiode {h(aktivierung_text)}.</span>
      </p>
      <p class="muted">Dieser Schalter betrifft AUSSCHLIESSLICH die eine automatische Owner-Sammelmail
         dieses Monatsberichts an die fest hinterlegte Owner-Adresse. Der allgemeine Mieter-/Mahnmailversand
         (globales <code>SEND_ENABLED</code> sowie die getrennten Indexanpassungs-/Vertragsende-Schalter)
         bleibt davon vollständig unberührt und unverändert deaktiviert, solange er nicht separat
         freigegeben wurde - eine Aktivierung hier öffnet NIEMALS den Mieterversand.</p>
      <table>
        <tr><th>Periode</th><th>Versandstatus</th><th>VPI</th><th>Verträge</th><th>Möglich</th><th>Noch nicht möglich</th>
          <th>Prüfung nötig</th><th>Fehlergrund</th></tr>
        {zeilen}
      </table>
    </div>
    """
    return _layout(request, session, "Index-Monatsbericht", inhalt)


@router.get("/indexautomatik/monatsbericht/{periode}", response_class=HTMLResponse)
def indexautomatik_monatsbericht_detail(request: Request, periode: str, session=Depends(_current_session)) -> HTMLResponse:
    bericht = deps._indexautomatik.monatsbericht_repository.get_by_periode(periode)
    if bericht is None:
        return _fehlerseite(
            session, "Index-Monatsbericht", f"Kein Bericht für Periode {periode} vorhanden.",
            "/backoffice/indexautomatik/monatsbericht",
        )
    # Codex-Korrektur (Schlussreview 2d45e27): `vpi_fehlergrund` ist ein
    # vom Versandstatus (`bericht.status`) UNABHÄNGIGES, dauerhaftes Feld
    # (siehe `IndexMonatsberichtTable`-Docstring) - dieser Hinweis muss
    # UNVERÄNDERT sichtbar bleiben, auch nachdem der Owner-Hinweis dazu
    # bereits beansprucht/versendet/als UNKLAR markiert wurde ("nach
    # GESENDET zeigt Portal keinen VPI-Fehler mehr"). Er behauptet KEINE
    # automatische Wiederholung - der bestehende JobRunner lässt eine
    # bereits FEHLGESCHLAGENE Periode nicht von selbst erneut laufen.
    vpi_fehler_block = ""
    if bericht.vpi_fehlergrund:
        vpi_fehler_block = f"""
        <div class="card">
          <p class="error">VPI-Abruf fehlgeschlagen - der amtliche VPI-Abruf/-Import für diese Periode ist
             fehlgeschlagen, es wurde bewusst NICHT mit veralteten Werten weitergerechnet, daher liegt für
             diese Periode (noch) kein vollständiger Bericht vor.</p>
          <p><strong>Grund:</strong> {h(bericht.vpi_fehlergrund)}</p>
          <p class="muted">Das erfordert eine technische Klärung (amtliche VPI-Quelle/-Import prüfen, ggf.
             den fehlgeschlagenen Job-Lock dieser Periode gezielt zurücksetzen) - es gibt KEINE
             automatische Wiederholung; erst ein manuell veranlasster, erfolgreicher Monatslauf für diese
             Periode erzeugt den vollständigen Bericht. Versandstatus des Owner-Hinweises dazu:
             {_monatsbericht_kopf_badge(bericht.status)}.</p>
        </div>
        """

    zeilen = deps._indexautomatik.monatsbericht_repository.zeilen_fuer_periode(periode)
    karten = "".join(_monatsbericht_karte_html(z) for z in zeilen) or '<p class="muted">Keine Verträge in dieser Periode.</p>'
    inhalt = f"""
    <div class="card">
      <h1>Index-Monatsbericht {h(periode)}</h1>
      <p>{_monatsbericht_kopf_badge(bericht.status)}
         <span class="muted">{bericht.anzahl_vertraege} Verträge geprüft: {bericht.anzahl_moeglich} möglich,
         {bericht.anzahl_noch_nicht_moeglich} noch nicht möglich, {bericht.anzahl_pruefung_noetig} Prüfung
         nötig.</span></p>
      <p><a href="/backoffice/indexautomatik/monatsbericht">&larr; alle Perioden</a></p>
    </div>
    {vpi_fehler_block}
    <div class="monatsbericht-karten">
      {karten}
    </div>
    """
    return _layout(request, session, f"Index-Monatsbericht {periode}", inhalt)
