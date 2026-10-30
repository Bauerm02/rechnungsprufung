"""HTML-Ansicht der rein lesenden Kautionsübersicht."""

from __future__ import annotations

from datetime import date
from html import escape
from typing import Any
from urllib.parse import quote

_CSS = """
<style>
.kaut-seite{background:#F5F2EC;color:#1F2125;font:14px/1.4 Arial,Helvetica,sans-serif;padding:18px}
.kaut-seite h1{font:600 24px/1.2 Cambria,Georgia,serif;margin:8px 0}
.kaut-kopf{display:flex;justify-content:space-between;gap:20px;align-items:center;border-bottom:1px solid #C9A86A;padding-bottom:10px}
.kaut-logo img{max-width:100px;max-height:64px}
.kaut-hinweis{padding:6px 0;margin:10px 0;max-width:85ch}
.kaut-seite :focus-visible{outline:2px solid #8A6F33;outline-offset:3px}
.kaut-tabelle details{max-width:360px;overflow-wrap:anywhere}
.kaut-filter{display:flex;flex-wrap:wrap;gap:10px;align-items:end;margin:12px 0}
.kaut-filter label{display:flex;flex-direction:column;gap:2px;font-size:13px}
.kaut-filter input,.kaut-filter select,.kaut-filter button,.kaut-druck{font-size:13px;padding:5px 8px}
.kaut-filter button,.kaut-druck{background:#1F2125;color:#F5F2EC;border:1px solid #1F2125;cursor:pointer}
.kaut-tabellenrahmen{overflow-x:auto;background:#fff}
.kaut-tabelle{border-collapse:collapse;min-width:1100px;width:100%;font-size:13px}
.kaut-tabelle th,.kaut-tabelle td{border-bottom:1px solid #d9d4ca;padding:6px 8px;text-align:left;vertical-align:top}
.kaut-tabelle thead th{background:#1F2125;color:#F5F2EC}
.kaut-tabelle tfoot th,.kaut-tabelle tfoot td{border-top:2px solid #C9A86A;font-weight:600}
.kaut-tabelle .kaut-betrag{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.kaut-klein{display:block;font-size:13px;color:#555;font-weight:400}
.kaut-zukunft{display:block;font-size:13px;color:#8a5a00;font-weight:600;white-space:normal}
.kaut-fuss{margin-top:14px;font-size:13px}
@media print{
@page{size:A4 landscape;margin:12mm}
nav,header,form,button,.kaut-navigation{display:none!important}
.kaut-seite{background:#fff;padding:0}
.kaut-tabellenrahmen{overflow:visible}
.kaut-tabelle{min-width:0;table-layout:auto}
.kaut-tabelle th,.kaut-tabelle td{white-space:normal;overflow-wrap:normal}
.kaut-tabelle .kaut-betrag{white-space:nowrap}
.kaut-tabelle thead{display:table-header-group}
.kaut-tabelle tr{break-inside:avoid}
}
</style>
"""


def _text(wert: Any) -> str:
    return escape(str(wert), quote=True)


def _datum(wert: date) -> str:
    return wert.strftime("%d.%m.%Y")


def _euro(cent: int) -> str:
    euro, rest = divmod(abs(cent), 100)
    vorzeichen = "-" if cent < 0 else ""
    return f"{vorzeichen}{euro:,}".replace(",", ".") + f",{rest:02d} €"


def _betrag(cent: int | None) -> str:
    return "nicht erfasst" if cent is None else _euro(cent)


def _status_wert(status: Any) -> str:
    return str(getattr(status, "value", status))


def _akte_url(mieter: Any) -> str:
    vertrag = quote(str(mieter.vertrag_id), safe="")
    objekt = quote(str(mieter.objekt_id), safe="")
    return f"/backoffice/vertrag/{vertrag}?von_objekt={objekt}"


def _filter_html(basis: Any) -> str:
    objekte = ['<option value="">Alle Objekte</option>']
    for option in basis.objekt_optionen:
        gewaehlt = " selected" if str(option.id) == str(basis.objekt_filter) else ""
        objekte.append(
            f'<option value="{_text(option.id)}"{gewaehlt}>'
            f"{_text(option.bezeichnung)} ({_text(option.gesellschaft_name)})</option>"
        )
    stati = []
    for wert, label in (("alle", "Alle"), ("aktiv", "Aktiv"), ("beendet", "Beendet")):
        gewaehlt = " selected" if wert == basis.status_filter else ""
        stati.append(f'<option value="{wert}"{gewaehlt}>{label}</option>')
    return (
        '<form class="kaut-filter" method="get" action="/backoffice/kautionen">'
        f'<label>Objekt<select name="objekt_id">{"".join(objekte)}</select></label>'
        f'<label>Suche<input type="search" name="q" value="{_text(basis.suche or "")}"></label>'
        f'<label>Mietverhältnisstatus<select name="status">{"".join(stati)}</select></label>'
        '<button type="submit">Filtern</button>'
        "</form>"
    )


def _quellen_html(zeile: Any) -> str:
    teile = []
    if zeile.vereinbart_quelle:
        teile.append(f"<div>Vertrag: {_text(zeile.vereinbart_quelle)}</div>")
    if zeile.eingang_referenz:
        teile.append(f"<div>Eingang: {_text(zeile.eingang_referenz)}</div>")
    inhalt = "".join(teile) or "<div>Keine Quelle erfasst</div>"
    return f"<details><summary>Quellen</summary>{inhalt}</details>"


def _eingang_html(zeile: Any) -> str:
    if zeile.eingang_cent is None:
        return "entfällt" if zeile.vereinbart_cent == 0 else "nicht belegt"
    datum = _datum(zeile.eingang_stichtag)
    if zeile.eingang_zukuenftig:
        return (
            f"{_euro(zeile.eingang_cent)}"
            f'<span class="kaut-zukunft">Zukünftig: Datum {datum} liegt nach dem Listenstichtag</span>'
        )
    return f'{_euro(zeile.eingang_cent)}<span class="kaut-klein">Stichtag {datum}</span>'


def _mietverhaeltnis_html(mieter: Any) -> str:
    wert = _status_wert(mieter.status)
    html = _text({"AKTIV":"Aktiv", "BEENDET":"Beendet", "KUENFTIG":"Künftig"}.get(wert, wert))
    if mieter.verwaltung_beendet:
        bis = f" zum {_datum(mieter.verwaltet_bis)}" if mieter.verwaltet_bis else ""
        html += f'<span class="kaut-klein">Verwaltung beendet{bis}</span>'
    return html


def _zeile_html(zeile: Any) -> str:
    mieter = zeile.mieter
    return (
        "<tr>"
        f"<td>{_text(mieter.debitor_name)}</td>"
        f"<td>{_text(mieter.objekt_bezeichnung)}"
        f'<span class="kaut-klein">{_text(mieter.objekt_adresse or "")}</span>'
        f'<span class="kaut-klein">{_text(mieter.einheit_bezeichnung or "")}</span></td>'
        f"<td>{_mietverhaeltnis_html(mieter)}</td>"
        f'<td class="kaut-betrag">{_betrag(zeile.vereinbart_cent)}</td>'
        f'<td class="kaut-betrag">{_eingang_html(zeile)}</td>'
        f"<td>{_text(zeile.status_label)}</td>"
        f"<td>{_quellen_html(zeile)}</td>"
        f'<td><a href="{_text(_akte_url(mieter))}">Akte öffnen</a></td>'
        "</tr>"
    )


def _summe(cent: int | None) -> str:
    return "keine bekannten Werte" if cent is None else _euro(cent)


def _summen_html(liste: Any) -> str:
    anzahl = len(liste.zeilen)
    entfaellt = sum(z.eingang_cent is None and z.vereinbart_cent == 0 for z in liste.zeilen)
    return (
        "<tfoot><tr>"
        '<th colspan="3">Summe vertraglich vereinbarter Kautionen (kein Geldbestand)</th>'
        f'<td class="kaut-betrag">{_summe(liste.vereinbart_summe_cent)}</td>'
        f'<td colspan="4">Umfang: {anzahl} sichtbare Zeilen; '
        f"{liste.vereinbart_bekannt} bekannt, {liste.vereinbart_fehlend} fehlend</td>"
        "</tr><tr>"
        '<th colspan="4">Summe bestätigter Eingänge bis Listenstichtag (kein Geldbestand)</th>'
        f'<td class="kaut-betrag">{_summe(liste.eingang_summe_cent)}</td>'
        f'<td colspan="3">Umfang: {anzahl} sichtbare Zeilen; '
        f"{liste.eingang_bekannt} bekannt, {liste.eingang_fehlend} fehlend, "
        f"{liste.eingang_zukuenftig} zukünftig (nicht summiert), {entfaellt} ohne Kaution</td>"
        "</tr></tfoot>"
    )


def rendere_kautionsliste(liste: Any, *, logo: str, navigation: str) -> str:
    """Rendert ein HTML-Fragment; logo und navigation sind bereits sicheres HTML."""
    basis = liste.basis
    zeilen = "".join(_zeile_html(zeile) for zeile in liste.zeilen) or (
        '<tr><td colspan="8">Keine Mietverhältnisse für diese Auswahl.</td></tr>'
    )
    return (
        f"{_CSS}"
        '<section class="kaut-seite">'
        '<div class="kaut-kopf"><h1>Kautionsübersicht</h1>'
        f'<div class="kaut-logo">{logo}</div></div>'
        f'<div class="kaut-navigation">{navigation}</div>'
        f"<p>Listenstichtag: {_datum(basis.stichtag)}</p>"
        '<p class="kaut-hinweis">Kautionen sind strikt vom Mietsaldo getrennt. Ein bestätigter '
        "Eingang ist kein aktueller Bank- oder Kautionsbestand. Rückzahlungen und Zinsen werden "
        "hier nicht mit dem Eingang verrechnet. Fehlende Angaben bedeuten weder 0 noch einen Zahlungsrückstand.</p>"
        f"{_filter_html(basis)}"
        '<button type="button" class="kaut-druck" onclick="window.print()">Drucken</button>'
        '<div class="kaut-tabellenrahmen"><table class="kaut-tabelle"><thead><tr>'
        "<th>Mieter</th><th>Objekt/Einheit</th><th>Mietverhältnisstatus</th>"
        '<th class="kaut-betrag">Vertraglich vereinbart</th>'
        '<th class="kaut-betrag">Bestätigter Eingang</th>'
        "<th>Kautionsstatus</th><th>Quellen</th><th>Akte</th>"
        f"</tr></thead><tbody>{zeilen}</tbody>{_summen_html(liste)}</table></div>"
        '<footer class="kaut-fuss">JLB Projects GmbH, hausverwaltung@jlb-immo.at</footer>'
        "</section>"
    )
