"""Renderer der Zinsliste als klassische HV-Tabelle (HV-20261030-ZINSLISTE).

Reine Darstellung von `portallisten.zinsdetails`; alle Fremdwerte werden escaped,
CSS ist auf `zins-*` begrenzt, es werden keine externen Ressourcen geladen."""

from __future__ import annotations

import base64
import re
from datetime import datetime
from html import escape as h
from pathlib import Path
from urllib.parse import quote, urlencode

from mietinkasso.backoffice.views import eur
from mietinkasso.indexautomatik.zeit import WIEN
from mietinkasso.portallisten.zinsdetails import KATEGORIEN, Betraege, ZinsDetailListe, ZinsDetailZeile
from mietinkasso.portallisten.zinsvergleich import ART_BETRAG, ZinsAenderung, vergleiche_zinsdetails

_LOGO_MAX_BYTES = 1024 * 1024
_KEIN_WERT = '<span class="zins-fehlt">—</span>'
_SPALTEN = 20
_BEFRISTUNG = {"UNBEFRISTET": "unbefristet", "UNGEKLAERT": "ungeklärt"}
_KOPF_TRENNUNG = {"HAUPTMIETZINS": "Haupt&shy;mietzins", "BK": "Betriebs&shy;kosten"}
_VPI_REIHEN = {"VPI20C18": "VPI 2020", "VPI15C18": "VPI 2015", "VPI96": "VPI 1996", "VPI00": "VPI 2000"}
_VPI_MONAT = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")
_AUFTEILUNG_SPALTEN = 7
_KATEGORIE_NAMEN = dict(KATEGORIEN)
_ART_OHNE_ZUSATZ = frozenset({"HMZ", "BK", "WASSER", "HEIZUNG", "STROM"})
_OHNE_SOLLBETRAG = frozenset({"LEERSTAND", "EIGENNUTZUNG"})

ZINS_CSS = """
.zins-ansicht { color: #1F2125; background: #F5F2EC; font-family: "Segoe UI", Arial, sans-serif; padding: 16px; }
.zins-kopf { display: flex; justify-content: space-between; gap: 24px; align-items: flex-start; border-bottom: 2px solid #C9A86A; margin-bottom: 12px; }
.zins-titel { font-family: Forum, Cambria, Georgia, serif; font-weight: 400; color: #1F2125; margin: 0 0 8px; }
.zins-logo { max-width: 180px; max-height: 64px; }
.zins-logo-text { font-family: Forum, Cambria, Georgia, serif; font-size: 28px; color: #1F2125; }
.zins-aktionen { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin: 8px 0; }
.zins-knopf { background: #1F2125; color: #F5F2EC; border: 1px solid #C9A86A; padding: 6px 12px; text-decoration: none; cursor: pointer; font: inherit; }
.zins-scroll { overflow: auto; max-height: 75vh; border: 1px solid #1F2125; background: #fff; }
.zins-tabelle { border-collapse: separate; border-spacing: 0; min-width: 2300px; width: 100%; font-size: 13px; font-variant-numeric: tabular-nums; background: #fff; }
.zins-tabelle th, .zins-tabelle td { border-right: 1px solid #d8d3c9; border-bottom: 1px solid #d8d3c9; padding: 4px 6px; vertical-align: top; text-align: left; }
.zins-tabelle thead { position: sticky; top: 0; z-index: 3; }
.zins-tabelle thead th { background: #1F2125; color: #F5F2EC; font-weight: 600; }
.zins-tabelle thead tr:first-child th { color: #C9A86A; text-align: center; border-bottom: 1px solid #C9A86A; }
.zins-tabelle .zins-top, .zins-tabelle .zins-name { position: sticky; background: #fff; z-index: 1; box-sizing: border-box; }
.zins-tabelle .zins-top { left: 0; width: 100px; min-width: 100px; max-width: 100px; }
.zins-tabelle .zins-name { left: 100px; width: 220px; min-width: 220px; max-width: 220px; }
.zins-tabelle thead .zins-top, .zins-tabelle thead .zins-name { background: #1F2125; z-index: 2; }
.zins-tabelle tfoot td, .zins-tabelle tfoot th { background: #F5F2EC; font-weight: 700; border-top: 2px solid #C9A86A; }
.zins-objektzeile th { background: #F5F2EC; font-family: Forum, Cambria, Georgia, serif; }
.zins-zahl { text-align: right !important; white-space: nowrap; }
.zins-nobr { white-space: nowrap; }
.zins-fehlt, .zins-klein { color: #5c5c5c; }
.zins-klein { display: block; font-size: 11px; }
.zins-warn { color: #8a4b00; font-weight: 600; }
.zins-ok { color: #1f6b3a; font-weight: 600; }
.zins-vpi { white-space: normal; min-width: 240px; }
.zins-summen { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(420px, 100%), 1fr)); gap: 16px; margin-top: 16px; }
.zins-summen h2 { font-family: Forum, Cambria, Georgia, serif; font-weight: 400; margin: 0 0 6px; font-size: 18px; }
.zins-summen .zins-tabelle { min-width: 0; }
.zins-summen section { min-width: 0; overflow-x: auto; }
.zins-summen .zins-tabelle thead { position: static; }
.zins-mobil { display: none; }
.zins-mobil-objekt { font-family: Forum, Cambria, Georgia, serif; font-weight: 400; font-size: 16px; margin: 12px 0 4px; }
.zins-mobil-mieter { background: #fff; border: 1px solid #d8d3c9; margin-bottom: 4px; overflow-wrap: break-word; }
.zins-mobil-mieter > summary { cursor: pointer; padding: 8px; }
.zins-mobil-kopf { display: inline-flex; justify-content: space-between; gap: 8px; width: calc(100% - 24px); vertical-align: top; }
.zins-mobil-name { min-width: 0; }
.zins-mobil-brutto { flex: 0 0 auto; max-width: 48%; text-align: right; font-variant-numeric: tabular-nums; }
.zins-mobil-brutto strong { white-space: nowrap; }
.zins-mobil-inhalt { padding: 0 8px 8px; }
.zins-mobil-positionen { list-style: none; margin: 8px 0 0; padding: 0; }
.zins-mobil-positionen li { border-top: 1px solid #d8d3c9; padding: 6px 0; }
.zins-mobil-werte { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 2px 12px; margin: 4px 0 0; }
.zins-mobil-werte div { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 0 6px; }
.zins-mobil-werte dt { color: #5c5c5c; }
.zins-mobil-werte dd { margin: 0; font-variant-numeric: tabular-nums; }
.zins-vergleich-liste { margin: 0; padding: 6px 12px 10px 28px; font-size: 13px; overflow-wrap: break-word; }
@media screen and (max-width: 639.98px) {
  body:not(.zins-druckseite) .zins-summen { grid-template-columns: 1fr; }
  body:not(.zins-druckseite) .zins-summen .zins-teilmenge { display: none; }
  body:not(.zins-druckseite) .zins-mobil { display: block; }
  body:not(.zins-druckseite) .zins-aufteilung-rahmen { display: none; }
}
.zins-legende, .zins-fuss { font-size: 12px; margin-top: 12px; }
.zins-fuss { border-top: 1px solid #C9A86A; padding-top: 6px; }
.zins-druckseite { margin: 0; background: #fff; }
.zins-druckseite .zins-ansicht { background: #fff; padding: 0; min-width: 404mm; box-sizing: border-box; }
.zins-druckseite .zins-scroll { overflow: visible; max-height: none; border: 0; }
.zins-druckseite .zins-tabelle { min-width: 0; width: 100%; table-layout: fixed; font-size: 7.5pt; }
.zins-druckseite .zins-tabelle th, .zins-druckseite .zins-tabelle td { padding: 1mm; overflow-wrap: break-word; position: static; width: auto; min-width: 0; max-width: none; }
.zins-druckseite .zins-tabelle thead { position: static; display: table-header-group; }
.zins-druckseite .zins-tabelle tfoot { display: table-row-group; }
.zins-druckseite .zins-klein, .zins-druckseite .zins-hinweise { font-size: 7.5pt; }
.zins-druckseite thead .zins-zahl, .zins-druckseite .zins-zahl .zins-warn, .zins-druckseite .zins-zahl .zins-klein { white-space: normal; }
.zins-druckseite .zins-c-top { width: 4.4%; }
.zins-druckseite .zins-c-name { width: 11.4%; }
.zins-druckseite .zins-c-beginn { width: 4%; }
.zins-druckseite .zins-c-befristung { width: 5%; }
.zins-druckseite .zins-c-art { width: 3.7%; }
.zins-druckseite .zins-c-klein { width: 2.5%; }
.zins-druckseite .zins-c-betrag { width: 4.2%; }
.zins-druckseite .zins-c-netto { width: 4.5%; }
.zins-druckseite .zins-c-ust { width: 4%; }
.zins-druckseite .zins-c-brutto { width: 4.7%; }
.zins-druckseite .zins-c-vpi { width: 15.5%; }
.zins-abschnitt { font-family: Forum, Cambria, Georgia, serif; font-weight: 400; font-size: 18px; margin: 16px 0 6px; }
.zins-aufteilung-rahmen { margin-bottom: 16px; }
.zins-aufteilung { border-collapse: separate; border-spacing: 0; width: 100%; min-width: 760px; font-size: 14px; font-variant-numeric: tabular-nums; background: #fff; }
.zins-aufteilung caption { text-align: left; font-family: Forum, Cambria, Georgia, serif; font-size: 18px; padding: 8px; }
.zins-aufteilung th, .zins-aufteilung td { border-right: 1px solid #d8d3c9; border-bottom: 1px solid #d8d3c9; padding: 5px 8px; vertical-align: top; text-align: left; font-weight: 400; }
.zins-aufteilung thead { position: sticky; top: 0; z-index: 2; }
.zins-aufteilung thead th { background: #1F2125; color: #F5F2EC; font-weight: 600; }
.zins-aufteilung .zins-objektzeile th { background: #F5F2EC; color: #1F2125; font-family: Forum, Cambria, Georgia, serif; font-size: 16px; border-top: 2px solid #C9A86A; }
.zins-aufteilung .zins-top-zelle { font-weight: 600; }
.zins-aufteilung .zins-mietersumme th, .zins-aufteilung .zins-mietersumme td { background: #F5F2EC; font-weight: 700; border-bottom: 2px solid #1F2125; }
.zins-aufteilung tfoot th, .zins-aufteilung tfoot td { background: #F5F2EC; color: #1F2125; font-weight: 700; font-size: 15px; border-top: 3px solid #C9A86A; }
.zins-a-top { width: 9%; }
.zins-a-mieter { width: 23%; }
.zins-a-bestandteil { width: 28%; }
.zins-a-betrag { width: 10%; }
.zins-a-satz { width: 8%; }
.zins-a-brutto { width: 12%; }
.zins-uebersicht { margin: 16px 0; border: 1px solid #1F2125; background: #fff; }
.zins-uebersicht summary { cursor: pointer; padding: 8px 12px; background: #F5F2EC; font-family: Forum, Cambria, Georgia, serif; font-size: 18px; }
.zins-uebersicht > .zins-klein { padding: 6px 12px; margin: 0; }
.zins-ansicht a:focus-visible, .zins-ansicht summary:focus-visible, .zins-scroll:focus-visible { outline: 3px solid #1F2125; outline-offset: 2px; }
.zins-druckseite .zins-aufteilung { min-width: 0; table-layout: fixed; font-size: 9pt; }
.zins-druckseite .zins-aufteilung th, .zins-druckseite .zins-aufteilung td { padding: 1mm 1.5mm; overflow-wrap: break-word; }
.zins-druckseite .zins-aufteilung thead { position: static; display: table-header-group; }
.zins-druckseite .zins-aufteilung tfoot { display: table-row-group; }
.zins-druckseite .zins-aufteilung-rahmen { margin-bottom: 6mm; }
@media print {
  @page { size: A3 landscape; margin: 8mm; }
  .zins-keindruck { display: none !important; }
  .zins-zeile, .zins-objektzeile { break-inside: avoid; page-break-inside: avoid; }
  .zins-tabelle thead { display: table-header-group; }
  .zins-tabelle tfoot { display: table-row-group; }
  .zins-summen { display: block; }
  .zins-summen section { break-inside: avoid; margin-bottom: 4mm; }
  .zins-summen section { overflow: visible; }
  .zins-druckseite .zins-ansicht { min-width: 0; width: 100%; }
  .zins-aufteilung-rahmen { overflow: visible; max-height: none; border: 0; }
  .zins-aufteilung { min-width: 0; table-layout: fixed; }
  .zins-aufteilung thead { position: static; display: table-header-group; }
  .zins-aufteilung tfoot { display: table-row-group; }
  .zins-aufteilung tr { break-inside: avoid; page-break-inside: avoid; }
  .zins-aufteilung .zins-position, .zins-aufteilung .zins-objektzeile { break-after: avoid; page-break-after: avoid; }
  .zins-uebersicht-druck { break-before: page; page-break-before: always; }
  .zins-mobil { display: none !important; }
  * { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
}
"""


def _logo_data_uri(pfad) -> str | None:
    if not pfad:
        return None
    try:
        datei = Path(pfad)
        if not datei.is_file() or datei.stat().st_size > _LOGO_MAX_BYTES:
            return None
        daten = datei.read_bytes()
    except (OSError, TypeError, ValueError):
        return None
    if len(daten) > _LOGO_MAX_BYTES:
        return None
    if daten.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif daten.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    else:
        return None
    return f"data:{mime};base64,{base64.b64encode(daten).decode('ascii')}"


def logo_html(pfad) -> str:
    uri = _logo_data_uri(pfad)
    if uri is None:
        return '<span class="zins-logo-text">JLB</span>'
    return f'<img class="zins-logo" src="{uri}" alt="JLB Projects GmbH">'


def _geld(cent: int | None) -> str:
    return eur(cent) if cent is not None else _KEIN_WERT


def _datum(wert) -> str:
    return f'<span class="zins-nobr">{wert:%d.%m.%Y}</span>' if wert is not None else _KEIN_WERT


def _text(wert) -> str:
    return h(str(wert)) if wert not in (None, "") else _KEIN_WERT


def _dezimal(wert) -> str:
    return h(format(wert, "f").replace(".", ",")) if wert is not None else _KEIN_WERT


def _teilwert(cent: int, betraege: Betraege) -> str:
    """Summenwert: bei fehlender Aufteilung sichtbar nur eine Teilsumme."""

    if betraege.anzahl_positionen == 0:
        return _KEIN_WERT
    if betraege.anzahl_ohne_aufteilung == betraege.anzahl_positionen:
        return '<span class="zins-warn">unbekannt</span>'
    if betraege.vollstaendig:
        return eur(cent)
    return f'{eur(cent)}<span class="zins-klein zins-warn">Teilsumme</span>'


def _aufteilung_zeile(cent: int | None, betraege: Betraege | None) -> str:
    if betraege is None:
        return _KEIN_WERT
    if cent is None:
        return '<span class="zins-warn">unbekannt</span>'
    return eur(cent)


def _name_html(zeile: ZinsDetailZeile, objekt_filter: str | None, druck: bool) -> str:
    teile = [f"<strong>{h(zeile.anzeige_name)}</strong>"]
    if zeile.vertragspartner:
        teile.append(f'<span class="zins-klein">Vertrag: {h(zeile.vertragspartner)}</span>')
    basis = zeile.basis
    if basis.vertrag_id is not None and not druck:
        query = f"?{urlencode({'von_objekt': objekt_filter})}" if objekt_filter else ""
        href = f"/backoffice/vertrag/{quote(basis.vertrag_id, safe='')}{query}"
        teile.append(f'<a class="zins-klein" href="{h(href)}">Akte öffnen</a>')
    teile.append(_details_html(zeile, druck))
    return "".join(teile)


def _ist_pruef_fall(zeile: ZinsDetailZeile) -> bool:
    """Druck zeigt Hinweise nur bei echten Prüffällen; online bleiben alle aufklappbar."""

    basis, gesamt = zeile.basis, zeile.gesamt
    if basis.pruefbedarf:
        return True
    if gesamt is None:
        return False
    if gesamt.netto_cent is None or gesamt.ust_cent is None or not gesamt.vollstaendig:
        return True
    geprueft = basis.netto_miete_geprueft_cent
    return geprueft is not None and geprueft != gesamt.netto_cent


def _details_html(zeile: ZinsDetailZeile, druck: bool) -> str:
    hinweise = "".join(f"<li>{h(text)}</li>" for text in zeile.hinweise)
    liste = f'<ul class="zins-hinweise">{hinweise}</ul>' if hinweise else ""
    if druck:
        return liste if _ist_pruef_fall(zeile) else ""
    positionen = "".join(
        f"<li>{h(p.art)} „{h(p.bezeichnung)}“: Brutto {eur(p.brutto_cent)}, "
        f"Netto {_geld(p.netto_cent)}, USt {_geld(p.ust_cent)}</li>"
        for p in zeile.positionen
    )
    if not hinweise and not positionen:
        return ""
    titel = f"Prüfhinweise ({len(zeile.hinweise)})" if hinweise else "Bestandteile"
    bestandteile = f'<ul class="zins-hinweise">{positionen}</ul>' if positionen else ""
    return f'<details class="zins-klein"><summary>{titel}</summary>{liste}{bestandteile}</details>'


def _befristung_html(zeile: ZinsDetailZeile) -> str:
    if zeile.befristung is None:
        return _KEIN_WERT
    if zeile.befristung == "BEFRISTET":
        ende = f"bis {_datum(zeile.befristet_bis)}" if zeile.befristet_bis else "Ende nicht hinterlegt"
        return f"befristet {ende}"
    text = _BEFRISTUNG.get(zeile.befristung, zeile.befristung)
    ende = f'<span class="zins-klein">Vertragsende {_datum(zeile.befristet_bis)}</span>' if zeile.befristet_bis else ""
    return f"{h(text)}{ende}"


def _vpi_monat(wert) -> str:
    text = "" if wert is None else str(wert)
    return f"{text[5:7]}.{text[:4]}" if _VPI_MONAT.fullmatch(text) else _text(wert)


def _vpi_html(zeile: ZinsDetailZeile) -> str:
    vpi = zeile.vpi
    if vpi is None:
        return _KEIN_WERT
    status = (
        '<span class="zins-ok">Klausel freigegeben</span>'
        if vpi.status == "FREIGEGEBEN" else '<span class="zins-warn">erfasst / ungeprüft</span>'
    )
    return (
        f"{_text(_VPI_REIHEN.get(vpi.reihe, vpi.reihe))}<br>Basis {_vpi_monat(vpi.basismonat)}: {_dezimal(vpi.basiswert)}<br>"
        f"Anpassung {_text(vpi.letzte_anpassung)}<br>{status}"
    )


def _brutto_html(zeile: ZinsDetailZeile, monat: str = "", druck: bool = False) -> str:
    basis = zeile.basis
    if zeile.gesamt is not None:
        stand = '<span class="zins-klein zins-warn">Stand Monatserster – nicht anteilig</span>' if basis.aenderung_im_monat else ""
        return f"<strong>{eur(zeile.gesamt.brutto_cent)}</strong>{stand}"
    if zeile.variabel_ohne_fix:
        status = basis.variabler_bericht_status or "kein Monatsbericht"
        link = ""
        if monat and not druck:
            href = "/backoffice/variable-abrechnung?" + urlencode({"monat": monat})
            link = f'<a class="zins-klein" href="{h(href)}">Monatsabrechnung öffnen</a>'
        return f'<span class="zins-warn">variabel</span><span class="zins-klein">{h(status)}</span>{link}'
    if basis.vertrag_id is not None:
        return '<span class="zins-warn">Betrag unbekannt</span>'
    return _KEIN_WERT


def _zeile_html(zeile: ZinsDetailZeile, objekt_filter: str | None, druck: bool, monat: str = "") -> str:
    beginn = _datum(zeile.mietbeginn)
    if zeile.mietbeginn_quelle == "VERTRAG":
        beginn += '<span class="zins-klein">erfasster Beginn</span>'
    betraege = "".join(
        f'<td class="zins-zahl">{_aufteilung_zeile(k.betraege.netto_cent if k.betraege else None, k.betraege)}</td>' for k in zeile.kategorien
    )
    gesamt = zeile.gesamt
    return (
        '<tr class="zins-zeile">'
        f'<td class="zins-top">{h(zeile.top)}</td>'
        f'<td class="zins-name">{_name_html(zeile, objekt_filter, druck)}</td>'
        f"<td>{beginn}</td>"
        f"<td>{_befristung_html(zeile)}</td>"
        f"<td>{_text(zeile.nutzungsart)}</td>"
        f'<td class="zins-zahl">{_dezimal(zeile.flaeche_qm)}</td>'
        f"<td>{_text(zeile.geschoss)}</td>"
        f"{betraege}"
        f'<td class="zins-zahl">{_aufteilung_zeile(gesamt.netto_cent if gesamt else None, gesamt)}</td>'
        f'<td class="zins-zahl">{_aufteilung_zeile(gesamt.ust_cent if gesamt else None, gesamt)}</td>'
        f'<td class="zins-zahl">{_brutto_html(zeile, monat, druck)}</td>'
        f'<td class="zins-vpi">{_vpi_html(zeile)}</td>'
        "</tr>"
    )


def _kopf_html() -> str:
    kosten = "".join(
        f'<th scope="col" class="zins-zahl">{_KOPF_TRENNUNG.get(schluessel, h(name))}</th>'
        for schluessel, name in KATEGORIEN
    )
    return (
        '<colgroup><col class="zins-c-top"><col class="zins-c-name"><col class="zins-c-beginn">'
        '<col class="zins-c-befristung"><col class="zins-c-art"><col class="zins-c-klein"><col class="zins-c-klein">'
        + '<col class="zins-c-betrag">' * len(KATEGORIEN)
        + '<col class="zins-c-netto"><col class="zins-c-ust"><col class="zins-c-brutto"><col class="zins-c-vpi"></colgroup>'
        '<thead><tr><th colspan="7" scope="colgroup">Stammdaten</th>'
        '<th colspan="9" scope="colgroup">Miete &amp; Kosten (netto)</th>'
        '<th colspan="3" scope="colgroup">Netto / USt / Brutto</th>'
        '<th scope="colgroup">VPI - aktuell hinterlegt, unabhängig vom Berichtsmonat</th></tr><tr>'
        '<th scope="col" class="zins-top">Top</th><th scope="col" class="zins-name">Mieter / Status</th>'
        '<th scope="col">Mietbeginn</th><th scope="col">Befristung</th><th scope="col">Art</th>'
        '<th scope="col" class="zins-zahl">m²</th><th scope="col">Geschoss</th>'
        f"{kosten}"
        '<th scope="col" class="zins-zahl">Netto</th><th scope="col" class="zins-zahl">USt</th>'
        '<th scope="col" class="zins-zahl">Brutto</th>'
        '<th scope="col">Reihe, Basismonat, Basiswert, letzte Anpassung, Status</th></tr></thead>'
    )


def _fuss_tabelle_html(details: ZinsDetailListe) -> str:
    s = details.summen
    basis = details.basis.summen
    kosten = "".join(
        f'<td class="zins-zahl">{_teilwert(k.betraege.netto_bekannt_cent, k.betraege)}</td>'
        for k in s.kategorien
    )
    return (
        f'<tfoot><tr><th colspan="7">Summe {basis.anzahl_mit_betrag} von {basis.anzahl_zeilen} Zeile(n)</th>{kosten}'
        f'<td class="zins-zahl">{_teilwert(s.gesamt.netto_bekannt_cent, s.gesamt)}</td>'
        f'<td class="zins-zahl">{_teilwert(s.gesamt.ust_bekannt_cent, s.gesamt)}</td>'
        f'<td class="zins-zahl">{_geld(s.gesamt.brutto_cent if s.gesamt.anzahl_positionen else None)}</td>'
        "<td></td></tr></tfoot>"
    )


def _tabelle_html(details: ZinsDetailListe, druck: bool) -> str:
    objekt_filter = details.basis.objekt_filter
    zeilen: list[str] = []
    letztes_objekt = None
    for zeile in details.zeilen:
        if zeile.basis.objekt_id != letztes_objekt:
            letztes_objekt = zeile.basis.objekt_id
            zeilen.append(
                f'<tr class="zins-objektzeile"><th colspan="{_SPALTEN}" scope="colgroup">'
                f"{h(zeile.basis.objekt_bezeichnung)}</th></tr>"
            )
        zeilen.append(_zeile_html(zeile, objekt_filter, druck, details.basis.monat))
    inhalt = "".join(zeilen) or f'<tr><td colspan="{_SPALTEN}">Keine Einheiten für diesen Filter.</td></tr>'
    return (
        '<div class="zins-scroll" tabindex="0" role="region" aria-label="Zinsliste, horizontal scrollbar">'
        f'<table class="zins-tabelle">{_kopf_html()}<tbody>{inhalt}</tbody>{_fuss_tabelle_html(details)}</table></div>'
    )


def _summen_zeile(name: str, betraege: Betraege, zusatz: str) -> str:
    brutto = _geld(betraege.brutto_cent if betraege.anzahl_positionen else None)
    return (
        f"<tr><td>{h(name)}</td><td class='zins-zahl'>{_teilwert(betraege.netto_bekannt_cent, betraege)}</td>"
        f"<td class='zins-zahl'>{_teilwert(betraege.ust_bekannt_cent, betraege)}</td>"
        f"<td class='zins-zahl'>{brutto}</td><td class='zins-teilmenge'>{zusatz}</td></tr>"
    )


def _teilmenge(betraege: Betraege) -> str:
    if betraege.vollstaendig:
        return ""
    return f'<span class="zins-warn">{eur(betraege.brutto_ohne_aufteilung_cent)} brutto ohne Aufteilung</span>'


def _summen_html(details: ZinsDetailListe) -> str:
    s = details.summen
    basis = details.basis.summen
    kopf = (
        '<thead><tr><th scope="col">Position</th><th scope="col" class="zins-zahl">Netto</th>'
        '<th scope="col" class="zins-zahl">USt</th><th scope="col" class="zins-zahl">Brutto</th>'
        '<th scope="col" class="zins-teilmenge">Teilmenge</th></tr></thead>'
    )
    kategorien = "".join(
        _summen_zeile(k.bezeichnung, k.betraege, f"{k.anzahl_zeilen} Zeile(n) {_teilmenge(k.betraege)}")
        for k in s.kategorien
    )
    saetze = "".join(_summen_zeile(k.bezeichnung, k.betraege, _teilmenge(k.betraege)) for k in s.steuersaetze)
    if s.ohne_gueltigen_satz.anzahl_positionen:
        saetze += _summen_zeile("Ungültiger oder unbekannter Satz", s.ohne_gueltigen_satz, _teilmenge(s.ohne_gueltigen_satz))
    gesamt = _summen_zeile("Gesamt", s.gesamt, _teilmenge(s.gesamt))
    aenderung = (
        f"; davon {basis.anzahl_aenderung_im_monat} mit Stand Monatserster ({eur(basis.brutto_aenderung_im_monat_cent)})"
        if basis.anzahl_aenderung_im_monat else ""
    )
    return (
        '<div class="zins-summen">'
        f'<section><h2>Summen je Erlöskategorie</h2><table class="zins-tabelle">{kopf}<tbody>{kategorien}{gesamt}</tbody></table></section>'
        f'<section><h2>Summen je Steuersatz</h2><table class="zins-tabelle">{kopf}<tbody>{saetze}{gesamt}</tbody></table></section>'
        f'</div><p class="zins-legende">Summen aus den {basis.anzahl_zeilen} sichtbaren Zeilen; '
        f"{basis.anzahl_ohne_betrag} ohne festen Betrag sind nicht enthalten{aenderung}.</p>"
    )


_LEGENDE = (
    '<p class="zins-legende">— = nicht hinterlegt oder unbekannt, nie 0 · 0,00 € = bekannte Null · „unbekannt“ = Aufteilung oder Satz fehlt · '
    "Teilsumme = mindestens ein Betrag fehlt · variabel = kein fester Betrag, nicht in Summen · "
    "Netto, USt und Brutto aus den hinterlegten Komponenten; Brutto unverändert laut Vorschreibung, USt herausgerechnet, nie aufgeschlagen · "
    "„erfasster Beginn“ = kein gesonderter ursprünglicher Mietbeginn belegt; kann Verwaltungsübernahme sein · Nutzung = aktueller Status · "
    "VPI = heutiger Stand, kein historischer Monatswert, „erfasst / ungeprüft“ = unverbindlicher Profilwert · "
    "Hauptmietzins ohne getrennte Betriebskosten ist kein Beleg für eine Pauschale; gezeigt wird nur die vorhandene Aufteilung.</p>"
)
_TEILSUMME = '<span class="zins-klein zins-warn">Teilsumme</span>'


def _satz_html(satz: int | None) -> str:
    """`ust_satz_promille` trägt Tausendstel-Prozent: 20000 = 20 %."""

    if satz is None:
        return '<span class="zins-warn">unbekannt</span>'
    ganz, rest = divmod(satz, 1000)
    text = str(ganz) if not rest else f"{ganz},{rest:03d}".rstrip("0")
    return f"{h(text)} %"


def _summen_satz_html(positionen) -> str:
    if not positionen:
        return _KEIN_WERT
    saetze = {p.ust_satz_promille for p in positionen}
    if None in saetze:
        text = "unbekannt" if len(saetze) == 1 else "teilweise unbekannt"
        return f'<span class="zins-warn">{text}</span>'
    return _satz_html(next(iter(saetze))) if len(saetze) == 1 else "gemischt"


def _summenwert(cent: int, betraege: Betraege, offen: bool) -> str:
    wert = _teilwert(cent, betraege)
    if offen and betraege.anzahl_positionen and betraege.vollstaendig:
        return f"{wert}{_TEILSUMME}"
    return wert


def _betrag_offen(zeile: ZinsDetailZeile) -> bool:
    """Fehlender oder variabler Betrag macht Summen zur Teilsumme; Leerstand hat keinen Sollbetrag."""

    if zeile.gesamt is not None:
        return bool(zeile.basis.variabel)
    return zeile.basis.vertrag_id is not None or zeile.anzeige_art not in _OHNE_SOLLBETRAG


def _bestandteil_html(position, bk_hk_inklusive: bool = False) -> str:
    if bk_hk_inklusive and position.art == "HMZ":
        return 'Pauschalmiete<span class="zins-klein">inkl. Betriebs- und Heizkosten</span>'
    name = _KATEGORIE_NAMEN.get(position.kategorie, position.kategorie)
    bezeichnung = (position.bezeichnung or "").strip()
    if bezeichnung and bezeichnung.casefold() not in (position.art.casefold(), name.casefold()):
        zusatz = bezeichnung
    elif position.art in _ART_OHNE_ZUSATZ or position.unbekannte_art:
        zusatz = ""
    else:
        zusatz = position.art.replace("_", " ")
    teile = [h(name)]
    if zusatz:
        teile.append(f'<span class="zins-klein">{h(zusatz)}</span>')
    if position.unbekannte_art:
        teile.append(f'<span class="zins-klein zins-warn">Art {h(position.art)} unter Sonstige</span>')
    return "".join(teile)


def _mieter_zelle(zeile, objekt_filter, druck) -> str:
    teile = [f"<strong>{h(zeile.anzeige_name)}</strong>"]
    if zeile.bk_hk_inklusive:
        teile.append('<span class="zins-klein">BK und Heizung in der Pauschalmiete enthalten</span>')
    if zeile.vertragspartner:
        teile.append(f'<span class="zins-klein">Vertrag: {h(zeile.vertragspartner)}</span>')
    vertrag_id = zeile.basis.vertrag_id
    if vertrag_id is not None and not druck:
        query = f"?{urlencode({'von_objekt': objekt_filter})}" if objekt_filter else ""
        href = f"/backoffice/vertrag/{quote(vertrag_id, safe='')}{query}"
        teile.append(f'<a class="zins-klein" href="{h(href)}">Akte öffnen</a>')
    if _ist_pruef_fall(zeile):
        teile.append('<span class="zins-klein zins-warn">Prüffall: Hinweise in der Vertrags- und Indexübersicht</span>')
    return "".join(teile)


def _ohne_positionen_text(zeile: ZinsDetailZeile) -> str:
    if zeile.variabel_ohne_fix:
        return "Variabel – kein fester Betrag"
    if zeile.basis.vertrag_id is None and zeile.anzeige_art in _OHNE_SOLLBETRAG:
        return f"Keine Mietbestandteile ({h(zeile.anzeige_name)})"
    return "Keine Komponenten hinterlegt"


def _aufteilung_zeilen(zeile: ZinsDetailZeile, objekt_filter: str | None, druck: bool, monat: str) -> str:
    mieter = _mieter_zelle(zeile, objekt_filter, druck)
    gesamt = zeile.gesamt
    if gesamt is None:
        return (
            f'<tr class="zins-ohne-position"><th scope="row" class="zins-top-zelle">{h(zeile.top)}</th><td>{mieter}</td>'
            f"<td>{_ohne_positionen_text(zeile)}</td>"
            f'<td class="zins-zahl">{_KEIN_WERT}</td><td class="zins-zahl">{_KEIN_WERT}</td><td class="zins-zahl">{_KEIN_WERT}</td>'
            f'<td class="zins-zahl">{_brutto_html(zeile, monat, druck)}</td></tr>'
        )
    anzahl = len(zeile.positionen) + 1
    kopf = (
        f'<th scope="row" rowspan="{anzahl}" class="zins-top-zelle">{h(zeile.top)}</th>'
        f'<td rowspan="{anzahl}">{mieter}</td>'
    )
    reihen = []
    for nummer, position in enumerate(zeile.positionen):
        reihen.append(
            f'<tr class="zins-position">{kopf if nummer == 0 else ""}<td>{_bestandteil_html(position, zeile.bk_hk_inklusive)}</td>'
            f'<td class="zins-zahl">{_aufteilung_zeile(position.netto_cent, gesamt)}</td>'
            f'<td class="zins-zahl">{_satz_html(position.ust_satz_promille)}</td>'
            f'<td class="zins-zahl">{_aufteilung_zeile(position.ust_cent, gesamt)}</td>'
            f'<td class="zins-zahl">{eur(position.brutto_cent)}</td></tr>'
        )
    variabel = bool(zeile.basis.variabel)
    zusatz = f'<span class="zins-klein">{_teilmenge(gesamt)}</span>' if not gesamt.vollstaendig else ""
    if variabel:
        zusatz += '<span class="zins-klein zins-warn">variabler Anteil nicht enthalten</span>'
    bezeichnung = "Mietersumme" if zeile.anzeige_art == "MIETER" else "Summe"
    brutto = _brutto_html(zeile, monat, druck) + (_TEILSUMME if variabel else "")
    reihen.append(
        f'<tr class="zins-mietersumme"><th scope="row">{bezeichnung}{zusatz}</th>'
        f'<td class="zins-zahl">{_summenwert(gesamt.netto_bekannt_cent, gesamt, variabel)}</td>'
        f'<td class="zins-zahl">{_summen_satz_html(zeile.positionen)}</td>'
        f'<td class="zins-zahl">{_summenwert(gesamt.ust_bekannt_cent, gesamt, variabel)}</td>'
        f'<td class="zins-zahl">{brutto}</td></tr>'
    )
    return "".join(reihen)


def _offen_grund(zeile: ZinsDetailZeile) -> str:
    if zeile.gesamt is not None:
        return "variabler Anteil"
    return "variabel" if zeile.variabel_ohne_fix else "Betrag fehlt"


def _feste_summe(details: ZinsDetailListe) -> tuple[str, str, int]:
    """Hinweise, Bruttowert und Anzahl offener Zeilen für Tabelle und Mobilansicht."""

    gesamt = details.summen.gesamt
    basis = details.basis.summen
    offene = [zeile for zeile in details.zeilen if _betrag_offen(zeile)]
    hinweise = f'<span class="zins-klein">{basis.anzahl_mit_betrag} von {basis.anzahl_zeilen} Zeile(n) mit festem Betrag</span>'
    if not gesamt.vollstaendig:
        hinweise += f'<span class="zins-klein">{_teilmenge(gesamt)}</span>'
    if offene:
        namen = " · ".join(f"{h(zeile.top)}: {_offen_grund(zeile)}" for zeile in offene)
        hinweise += f'<span class="zins-klein zins-warn">{len(offene)} Zeile(n) mit fehlendem oder variablem Betrag nicht oder nur teilweise enthalten: {namen}</span>'
    brutto = _geld(gesamt.brutto_cent if gesamt.anzahl_positionen else None)
    if offene and gesamt.anzahl_positionen:
        brutto += _TEILSUMME
    return hinweise, brutto, len(offene)


def _aufteilung_fuss_html(details: ZinsDetailListe) -> str:
    gesamt = details.summen.gesamt
    hinweise, brutto, offen = _feste_summe(details)
    alle = [position for zeile in details.zeilen for position in zeile.positionen]
    return (
        f'<tfoot class="zins-gesamt"><tr><th colspan="3" scope="row">Summe feste Monatsvorschreibungen{hinweise}</th>'
        f'<td class="zins-zahl">{_summenwert(gesamt.netto_bekannt_cent, gesamt, bool(offen))}</td>'
        f'<td class="zins-zahl">{_summen_satz_html(alle)}</td>'
        f'<td class="zins-zahl">{_summenwert(gesamt.ust_bekannt_cent, gesamt, bool(offen))}</td>'
        f'<td class="zins-zahl">{brutto}</td></tr></tfoot>'
    )


def _aufteilung_html(details: ZinsDetailListe, druck: bool) -> str:
    objekt_filter = details.basis.objekt_filter
    monat = details.basis.monat
    teile: list[str] = []
    letztes_objekt: object = object()
    for zeile in details.zeilen:
        if zeile.basis.objekt_id != letztes_objekt:
            if teile:
                teile.append("</tbody>")
            letztes_objekt = zeile.basis.objekt_id
            teile.append(
                f'<tbody class="zins-objektgruppe"><tr class="zins-objektzeile">'
                f'<th colspan="{_AUFTEILUNG_SPALTEN}" scope="rowgroup">{h(zeile.basis.objekt_bezeichnung)}</th></tr>'
            )
        teile.append(_aufteilung_zeilen(zeile, objekt_filter, druck, monat))
    inhalt = "".join(teile) + "</tbody>" if teile else (
        f'<tbody class="zins-objektgruppe"><tr><td colspan="{_AUFTEILUNG_SPALTEN}">Keine Einheiten für diesen Filter.</td></tr></tbody>'
    )
    return (
        '<div class="zins-scroll zins-aufteilung-rahmen" tabindex="0" role="region" aria-label="Mietbestandteile, horizontal scrollbar">'
        f'<table class="zins-aufteilung"><caption>Mietbestandteile {h(monat)}</caption>'
        '<colgroup><col class="zins-a-top"><col class="zins-a-mieter"><col class="zins-a-bestandteil">'
        '<col class="zins-a-betrag"><col class="zins-a-satz"><col class="zins-a-betrag"><col class="zins-a-brutto"></colgroup>'
        '<thead><tr><th scope="col">Top</th><th scope="col">Mieter</th><th scope="col">Bestandteil</th>'
        '<th scope="col" class="zins-zahl">Netto</th><th scope="col" class="zins-zahl">USt-Satz</th>'
        '<th scope="col" class="zins-zahl">USt</th><th scope="col" class="zins-zahl">Brutto</th></tr></thead>'
        f"{inhalt}{_aufteilung_fuss_html(details)}</table></div>"
    )


def _mobil_wert(name: str, wert: str) -> str:
    return f"<div><dt>{name}</dt><dd>{wert}</dd></div>"


def _mobil_position(position, gesamt: Betraege, bk_hk_inklusive: bool = False) -> str:
    return (
        f'<li>{_bestandteil_html(position, bk_hk_inklusive)}<dl class="zins-mobil-werte">'
        f'{_mobil_wert("Netto", _aufteilung_zeile(position.netto_cent, gesamt))}'
        f'{_mobil_wert("USt-Satz", _satz_html(position.ust_satz_promille))}'
        f'{_mobil_wert("USt", _aufteilung_zeile(position.ust_cent, gesamt))}'
        f'{_mobil_wert("Brutto", eur(position.brutto_cent))}</dl></li>'
    )


def _mobil_zeile(zeile: ZinsDetailZeile, objekt_filter: str | None, monat: str) -> str:
    gesamt = zeile.gesamt
    if gesamt is None:
        positionen = f"<p>{_ohne_positionen_text(zeile)}</p>{_brutto_html(zeile, monat, False)}"
        teilsumme = ""
    else:
        eintraege = "".join(_mobil_position(position, gesamt, zeile.bk_hk_inklusive) for position in zeile.positionen)
        positionen = f'<ul class="zins-mobil-positionen">{eintraege}</ul>'
        teilsumme = _TEILSUMME if zeile.basis.variabel else ""
    return (
        '<details class="zins-mobil-mieter"><summary><span class="zins-mobil-kopf">'
        f'<span class="zins-mobil-name">{h(zeile.top)} · <strong>{h(zeile.anzeige_name)}</strong></span>'
        f'<span class="zins-mobil-brutto">{_brutto_html(zeile, monat, True)}{teilsumme}</span></span></summary>'
        f'<div class="zins-mobil-inhalt">{_mieter_zelle(zeile, objekt_filter, False)}{positionen}</div></details>'
    )


def _mobil_html(details: ZinsDetailListe) -> str:
    objekt_filter, monat = details.basis.objekt_filter, details.basis.monat
    teile: list[str] = []
    letztes_objekt: object = object()
    for zeile in details.zeilen:
        if zeile.basis.objekt_id != letztes_objekt:
            letztes_objekt = zeile.basis.objekt_id
            teile.append(f'<h3 class="zins-mobil-objekt">{h(zeile.basis.objekt_bezeichnung)}</h3>')
        teile.append(_mobil_zeile(zeile, objekt_filter, monat))
    hinweise, brutto, _ = _feste_summe(details)
    inhalt = "".join(teile) or "<p>Keine Einheiten für diesen Filter.</p>"
    return (
        f'<section class="zins-mobil" aria-label="Mietbeträge je Mieter"><h2 class="zins-abschnitt">Mietbeträge {h(monat)}</h2>{inhalt}'
        f"<p><strong>Summe feste Monatsvorschreibungen</strong> {brutto}{hinweise}</p></section>"
    )


_VERGLEICH_FEHLT = '<p class="zins-klein">Vergleich nicht verfügbar.</p>'
_VERGLEICH_GRENZE = (
    '<p class="zins-klein">Vergleich je Monatserster im gleichen Filter; keine Aussage, ob eine Änderung erlaubt oder beschlossen ist. '
    "Nutzungsstatus und VPI werden nicht historisiert.</p>"
)


def _delta_html(delta: int | None) -> str:
    if delta is None:
        return '<span class="zins-warn">nicht berechenbar</span>'
    return f"{'+' if delta > 0 else '-'}{eur(abs(delta))}"


def _aenderung_html(aenderung: ZinsAenderung) -> str:
    kopf = f"<strong>{h(aenderung.top)}</strong> · {h(aenderung.mieter)}: {h(aenderung.bereich)}"
    if aenderung.art == ART_BETRAG:
        werte = f": alt {_geld(aenderung.alt_cent)}, neu {_geld(aenderung.neu_cent)}, Differenz {_delta_html(aenderung.delta_cent)}"
    elif aenderung.alt_text is None and aenderung.neu_text is None:
        werte = ""
    else:
        werte = f": alt {_text(aenderung.alt_text)}, neu {_text(aenderung.neu_text)}"
    return f"<li>{kopf}{werte}</li>"


def _vergleich_inhalt(details: ZinsDetailListe, vormonat_details: ZinsDetailListe | None) -> str:
    if vormonat_details is None:
        return _VERGLEICH_FEHLT
    try:
        aenderungen = vergleiche_zinsdetails(vormonat_details, details)
    except Exception:  # Darstellung bleibt nutzbar; nie eine falsche Leermeldung.
        return _VERGLEICH_FEHLT
    if not aenderungen:
        return f'<p class="zins-klein">Keine Änderungen zum Vormonat im gleichen Filter.</p>{_VERGLEICH_GRENZE}'
    eintraege = "".join(_aenderung_html(aenderung) for aenderung in aenderungen)
    return f'<ul class="zins-vergleich-liste">{eintraege}</ul>{_VERGLEICH_GRENZE}'


def _vergleich_html(details: ZinsDetailListe, vormonat_details: ZinsDetailListe | None, druck: bool) -> str:
    inhalt = _vergleich_inhalt(details, vormonat_details)
    if druck:
        return f'<section class="zins-vergleich-druck"><h2 class="zins-abschnitt">Änderungen zum Vormonat</h2>{inhalt}</section>'
    return f'<details class="zins-uebersicht zins-vergleich"><summary>Änderungen zum Vormonat</summary>{inhalt}</details>'


def _fuss_html() -> str:
    jetzt = datetime.now(WIEN)
    return (
        f'<p class="zins-fuss">Stand {jetzt:%d.%m.%Y %H:%M} (Wien) · JLB Projects GmbH · Marc-Aurel-Straße 4/16, 1010 Wien · '
        "FN 631126b · UID ATU81269707 · hausverwaltung@jlb-immo.at · +43 1 4351011</p>"
    )


def rendere_zinsliste_ansicht(
    details: ZinsDetailListe, *, logo: str, listen_links: str, filter_html: str, monatslinks: tuple[str, ...],
    zeitstempel_html: str, druck_href: str, vormonat_details: ZinsDetailListe | None = None,
) -> str:
    liste = details.basis
    links = "".join(f"<span>{link}</span>" for link in monatslinks)
    return f"""
    <style>{ZINS_CSS}</style>
    <div class="zins-ansicht">
      <div class="zins-kopf">
        <div><h1 class="zins-titel">Zinsliste {h(liste.monat)}</h1>{listen_links}</div>
        <div>{logo}</div>
      </div>
      {filter_html}
      {zeitstempel_html}
      <div class="zins-aktionen">
        <a class="zins-knopf" href="{h(druck_href)}">Drucken / PDF</a>
        {links}
      </div>
      {_aufteilung_html(details, druck=False)}
      {_mobil_html(details)}
      {_vergleich_html(details, vormonat_details, druck=False)}
      <details class="zins-uebersicht">
        <summary>Vertrags- und Indexübersicht</summary>
        <p class="zins-klein">Alle {_SPALTEN} Spalten: waagrecht scrollen; Top und Mieter bleiben fixiert.</p>
        {_tabelle_html(details, druck=False)}
      </details>
      {_summen_html(details)}
      {_LEGENDE}
      {_fuss_html()}
    </div>
    <p><a href="/backoffice/">&larr; zur Übersicht</a></p>"""


def rendere_zinsliste_druck(
    details: ZinsDetailListe, *, logo: str, zurueck_href: str, vormonat_details: ZinsDetailListe | None = None,
) -> str:
    liste = details.basis
    objekt = next((o.bezeichnung for o in liste.objekt_optionen if o.id == liste.objekt_filter), "Alle Objekte")
    suche = f" · Suche: {h(liste.suche)}" if liste.suche else ""
    return f"""<!doctype html>
<html lang="de">
<head><meta charset="utf-8"><title>Zinsliste {h(liste.monat)}</title><style>{ZINS_CSS}</style></head>
<body class="zins-druckseite">
  <div class="zins-ansicht">
    <div class="zins-kopf">
      <div><h1 class="zins-titel">Zinsliste {h(liste.monat)}</h1><p>{h(objekt)}{suche}</p></div>
      <div>{logo}</div>
    </div>
    <p class="zins-aktionen zins-keindruck">
      <button type="button" class="zins-knopf" onclick="window.print()">Drucken / PDF</button>
      <a href="{h(zurueck_href)}">Zurück zur Zinsliste</a>
    </p>
    {_aufteilung_html(details, druck=True)}
    {_vergleich_html(details, vormonat_details, druck=True)}
    <section class="zins-uebersicht-druck"><h2 class="zins-abschnitt">Vertrags- und Indexübersicht</h2>{_tabelle_html(details, druck=True)}</section>
    {_summen_html(details)}
    {_LEGENDE}
    {_fuss_html()}
  </div>
</body>
</html>
"""
