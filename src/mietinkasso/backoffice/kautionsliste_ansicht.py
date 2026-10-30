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
.kaut-tabelle{border-collapse:collapse;width:100%;font-size:14px}
.kaut-tabelle th,.kaut-tabelle td{border-bottom:1px solid #d9d4ca;padding:6px 8px;text-align:left;vertical-align:top}
.kaut-tabelle thead th{background:#1F2125;color:#F5F2EC}
.kaut-tabelle .kaut-betrag{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.kaut-klein{display:block;font-size:13px;color:#555;font-weight:400}
.kaut-zukunft{display:block;font-size:13px;color:#8a5a00;font-weight:600;white-space:normal}
.kaut-fuss{margin-top:14px;font-size:13px}
.kaut-zaehler{display:flex;flex-wrap:wrap;gap:4px 18px;margin:10px 0;font-weight:600}
.kaut-erlaeuterung{margin:10px 0;max-width:85ch}
.kaut-erlaeuterung summary{cursor:pointer}
.kaut-summen{margin:8px 0}
.kaut-gruppen{margin:20px 0}
.kaut-gruppen h2{font:600 19px Cambria,Georgia,serif;margin:0 0 6px}
.kaut-gruppen p{margin:0 0 8px}
.kaut-summentabelle{border-collapse:collapse;width:100%;font-size:14px}
.kaut-summentabelle th,.kaut-summentabelle td{padding:8px;text-align:left;border-bottom:1px solid #d9d4ca}
.kaut-summentabelle thead,.kaut-summentabelle tfoot{background:#ECE9E2}
.kaut-summentabelle .kaut-betrag{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.kaut-summentabelle tfoot{font-weight:700}
.kaut-summen dt{font-weight:600;margin-top:8px}
.kaut-summen dd{margin:2px 0 0}
.kaut-nur-druck{display:none}
.kaut-name{display:inline-block;font-weight:600;margin-right:8px}
.kaut-marke{display:inline-block;margin:2px 0;padding:1px 6px;border:1px solid #8b8578;font-size:13px;font-weight:600}
.kaut-marke-ok{background:#E8F1EA;border-color:#2F6B45;color:#1F4D31}
.kaut-marke-offen{background:#FBF0DA;border-color:#8A5A00;color:#6B4600}
.kaut-marke-neutral{background:#ECE9E2;color:#1F2125}
.kaut-akte{display:block;font-weight:600;margin-bottom:4px}
@media screen and (max-width:700px){
.kaut-filter label,.kaut-filter input,.kaut-filter select{width:100%;box-sizing:border-box}
.kaut-tabellenrahmen{overflow:visible}
.kaut-gruppen .kaut-tabellenrahmen{overflow-x:auto}
.kaut-tabelle,.kaut-tabelle tbody,.kaut-tabelle tr,.kaut-tabelle td{display:block}
.kaut-tabelle thead{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%)}
.kaut-tabelle tr{border-bottom:2px solid #C9A86A;padding:6px 0}
.kaut-tabelle td{border-bottom:0;overflow-wrap:anywhere}
.kaut-tabelle td[data-label]::before{content:attr(data-label);display:block;font-size:13px;font-weight:600;color:#555}
.kaut-tabelle .kaut-betrag{text-align:left;white-space:normal}
.kaut-tabelle details{max-width:none}
}
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
.kaut-erlaeuterung,.kaut-akte,.kaut-details:not([open]){display:none}
.kaut-nur-druck{display:block}
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


def _filter_html(liste: Any) -> str:
    basis = liste.basis
    objekte = ['<option value="">Alle Objekte</option>']
    for option in basis.objekt_optionen:
        gewaehlt = " selected" if str(option.id) == str(basis.objekt_filter) else ""
        objekte.append(
            f'<option value="{_text(option.id)}"{gewaehlt}>'
            f"{_text(option.bezeichnung)} ({_text(option.gesellschaft_name)})</option>"
        )
    firmen = {str(o.gesellschaft_id): o.gesellschaft_name for o in basis.objekt_optionen}
    firma_optionen = ['<option value="">Alle Firmen</option>']
    for key, name in sorted(firmen.items(), key=lambda pair: pair[1].casefold()):
        selected = " selected" if key == liste.gesellschaft_filter else ""
        firma_optionen.append(f'<option value="{_text(key)}"{selected}>{_text(name)}</option>')
    fall_optionen = []
    for key, name in (("offen", "Laufende Kautionsfälle"), ("erledigt", "Erledigte Kautionsfälle"), ("alle", "Alle mit Historie")):
        selected = " selected" if key == liste.abschluss_filter else ""
        fall_optionen.append(f'<option value="{key}"{selected}>{name}</option>')
    gruppen_optionen = []
    for key, name in (("gesamt", "Gesamt"), ("gesellschaft", "Je Firma"), ("objekt", "Je Liegenschaft")):
        selected = " selected" if key == liste.gruppierung else ""
        gruppen_optionen.append(f'<option value="{key}"{selected}>{name}</option>')
    stati = []
    for wert, label in (("alle", "Alle"), ("aktiv", "Aktiv"), ("beendet", "Beendet")):
        gewaehlt = " selected" if wert == basis.status_filter else ""
        stati.append(f'<option value="{wert}"{gewaehlt}>{label}</option>')
    return (
        '<form class="kaut-filter" method="get" action="/backoffice/kautionen">'
        f'<label>Liegenschaft<select name="objekt_id">{"".join(objekte)}</select></label>'
        f'<label>Firma<select name="gesellschaft_id">{"".join(firma_optionen)}</select></label>'
        f'<label>Kautionsfälle<select name="abschluss">{"".join(fall_optionen)}</select></label>'
        f'<label>Summen<select name="gruppierung">{"".join(gruppen_optionen)}</select></label>'
        f'<label>Suche<input type="search" name="q" value="{_text(basis.suche or "")}"></label>'
        f'<label>Verträge<select name="status">{"".join(stati)}</select></label>'
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
    if zeile.abgeschlossen_am:
        if not teile:
            teile.append("<div>Keine Eingangsquelle erfasst</div>")
        teile.append(f"<div>Kaution {_betrag(zeile.abschluss_rueckzahlung_cent)} erledigt am {_datum(zeile.abgeschlossen_am)} durch {_text(zeile.abgeschlossen_durch or '')}: {_text(zeile.abschluss_referenz or '')}</div>")
        inhalt = "".join(teile)
    status = _text(_status_wert(zeile.mieter.status))
    return (
        '<details class="kaut-details"><summary>Details</summary>'
        f"<div>Vertragsstatus: {status}</div>"
        f"<div>Objekt: {_text(zeile.mieter.objekt_bezeichnung)}</div>{inhalt}</details>"
    )


def _eingang_html(zeile: Any) -> str:
    if zeile.eingang_cent is None:
        return "entfällt" if zeile.vereinbart_cent == 0 else "nicht belegt"
    if zeile.eingang_stichtag is None:
        return f'{_euro(zeile.eingang_cent)}<span class="kaut-klein">Stichtag nicht erfasst</span>'
    datum = _datum(zeile.eingang_stichtag)
    if zeile.eingang_zukuenftig:
        return (
            f"{_euro(zeile.eingang_cent)}"
            f'<span class="kaut-zukunft">Zukünftig: Datum {datum} liegt nach dem Listenstichtag</span>'
        )
    return f'{_euro(zeile.eingang_cent)}<span class="kaut-klein">Stichtag {datum}</span>'


def _mietverhaeltnis_html(mieter: Any) -> str:
    wert = _status_wert(mieter.status)
    label = {"AKTIV": "Aktiv", "BEENDET": "Beendet", "KUENFTIG": "Künftig"}.get(wert, "Status unbekannt")
    html = f'<span class="kaut-marke kaut-marke-neutral">{label}</span>'
    if mieter.verwaltung_beendet:
        bis = f" zum {_datum(mieter.verwaltet_bis)}" if mieter.verwaltet_bis else ""
        html += f'<span class="kaut-klein">Verwaltung beendet{bis}</span>'
    return html


def _mieter_html(mieter: Any) -> str:
    angaben = (mieter.objekt_adresse or mieter.objekt_bezeichnung, mieter.einheit_bezeichnung)
    zeilen = "".join(f'<span class="kaut-klein">{_text(wert)}</span>' for wert in angaben if wert)
    return (
        f'<span class="kaut-name">{_text(mieter.debitor_name)}</span>'
        f"{_mietverhaeltnis_html(mieter)}{zeilen}"
    )


_EINORDNUNG_KLASSEN = {
    "Eingang bestätigt": "kaut-marke-ok",
    "Keine Kaution vereinbart": "kaut-marke-neutral",
    "Eingang nicht belegt": "kaut-marke-offen",
    "Kautionsvereinbarung fehlt": "kaut-marke-offen",
    "Zurückbezahlt / erledigt": "kaut-marke-neutral",
}


def _einordnung_html(zeile: Any) -> str:
    klasse = _EINORDNUNG_KLASSEN.get(zeile.status_label, "kaut-marke-neutral")
    html = f'<span class="kaut-marke {klasse}">{_text(zeile.status_label)}</span>'
    if zeile.eingang_zukuenftig:
        html += '<span class="kaut-klein">Eingang nach Listenstichtag, nicht summiert</span>'
    return html


def _zeile_html(zeile: Any) -> str:
    mieter = zeile.mieter
    return (
        "<tr>"
        f'<td data-label="Mieter / Objekt / Einheit">{_mieter_html(mieter)}</td>'
        f'<td class="kaut-betrag" data-label="Kaution laut Vertrag">{_betrag(zeile.vereinbart_cent)}</td>'
        f'<td class="kaut-betrag" data-label="Bestätigter Eingang">{_eingang_html(zeile)}</td>'
        f'<td data-label="Einordnung">{_einordnung_html(zeile)}</td>'
        f'<td data-label="Akte / Details"><a class="kaut-akte" href="{_text(_akte_url(mieter))}">Akte öffnen</a>'
        f"{_quellen_html(zeile)}</td>"
        "</tr>"
    )


def _summe(cent: int | None) -> str:
    return "keine bekannten Werte" if cent is None else _euro(cent)


def _nullfaelle(liste: Any) -> int:
    return sum(
        z.eingang_cent == 0 and z.vereinbart_cent == 0 and not z.eingang_zukuenftig
        for z in liste.zeilen
    )


def _summen_html(liste: Any) -> str:
    anzahl = len(liste.zeilen)
    entfaellt = sum(z.eingang_cent is None and z.vereinbart_cent == 0 for z in liste.zeilen)
    nullfaelle = _nullfaelle(liste)
    null_hinweis = f"; unter bekannt: {nullfaelle} Eingang 0,00 € ohne vereinbarte Kaution" if nullfaelle else ""
    return (
        '<dl class="kaut-summen">'
        "<dt>Summe vertraglich vereinbarter Kautionen (kein Geldbestand)</dt>"
        f"<dd>{_summe(liste.vereinbart_summe_cent)}"
        f'<span class="kaut-klein">Umfang: {anzahl} sichtbare Zeilen; '
        f"{liste.vereinbart_bekannt} bekannt, {liste.vereinbart_fehlend} fehlend</span></dd>"
        "<dt>Summe bestätigter Eingänge bis Listenstichtag (kein Geldbestand)</dt>"
        f"<dd>{_summe(liste.eingang_summe_cent)}"
        f'<span class="kaut-klein">Umfang: {anzahl} sichtbare Zeilen; '
        f"{liste.eingang_bekannt} bekannt, {liste.eingang_fehlend} fehlend, "
        f"{liste.eingang_zukuenftig} zukünftig (nicht summiert), {entfaellt} ohne Kaution{null_hinweis}</span></dd>"
        "</dl>"
    )


_ERLAEUTERUNG = (
    "<p>Kautionen sind strikt vom Mietsaldo getrennt. Ein bestätigter Eingang ist kein aktueller "
    "Bank- oder Kautionsbestand. Rückzahlungen und Zinsen werden hier nicht mit dem Eingang "
    "verrechnet. Fehlende Angaben bedeuten weder 0 noch einen Zahlungsrückstand.</p>"
)


def _zaehler_html(liste: Any) -> str:
    nullfaelle = _nullfaelle(liste)
    entfaellt = sum(z.eingang_cent is None and z.vereinbart_cent == 0 for z in liste.zeilen)
    teile = [
        ("Mietverhältnisse", len(liste.zeilen)),
        ("Eingänge bestätigt", liste.eingang_bekannt - nullfaelle),
        ("Nachweis fehlt", liste.eingang_fehlend),
        ("Keine Kaution", entfaellt + nullfaelle),
    ]
    if liste.eingang_zukuenftig:
        teile.append(("Zukünftig datiert", liste.eingang_zukuenftig))
    inhalt = "".join(f"<span>{label}: {wert}</span>" for label, wert in teile)
    return f'<p class="kaut-zaehler">{inhalt}</p>'


def _offene_nachweise(vereinbarung: int, eingang: int, zukuenftig: int) -> str:
    teile = []
    if vereinbarung: teile.append(f"{vereinbarung} Vereinbarung(en) fehlen")
    if eingang: teile.append(f"{eingang} Eingang/Eingänge fehlen")
    if zukuenftig: teile.append(f"{zukuenftig} Eingang/Eingänge künftig")
    return "; ".join(teile) or "vollständig"


def _gruppensummen_html(liste: Any) -> str:
    zeilen = "".join(
        f'<tr><th scope="row">{_text(g.bezeichnung)}</th><td>{g.anzahl}</td>'
        f'<td class="kaut-betrag">{_summe(g.vereinbart_cent)}</td>'
        f'<td class="kaut-betrag">{_summe(g.eingang_cent)}</td><td>{_offene_nachweise(g.vereinbarung_fehlend, g.nachweis_fehlend, g.eingang_zukuenftig)}</td></tr>'
        for g in liste.gruppensummen
    ) or '<tr><td colspan="5">Keine Kautionsfälle für diese Auswahl.</td></tr>'
    footer = ""
    if liste.gruppierung != "gesamt" and liste.zeilen:
        footer = (f'<tfoot><tr><th scope="row">Gesamt</th><td>{len(liste.zeilen)}</td>'
                  f'<td class="kaut-betrag">{_summe(liste.vereinbart_summe_cent)}</td>'
                  f'<td class="kaut-betrag">{_summe(liste.eingang_summe_cent)}</td><td>{_offene_nachweise(liste.vereinbart_fehlend, liste.eingang_fehlend, liste.eingang_zukuenftig)}</td></tr></tfoot>')
    return ('<section class="kaut-gruppen"><h2>Summen der Auswahl</h2>'
            '<p>Die Summen berücksichtigen die gewählten Filter. Historische Eingänge sind kein aktueller Bankbestand. Fehlende Beträge sind nicht enthalten.</p>'
            '<div class="kaut-tabellenrahmen"><table class="kaut-summentabelle"><thead><tr>'
            '<th>Firma / Liegenschaft</th><th>Fälle</th><th>Vereinbart</th><th>Historischer Eingang</th><th>Offene Nachweise</th>'
            f'</tr></thead><tbody>{zeilen}</tbody>{footer}</table></div></section>')


def rendere_kautionsliste(liste: Any, *, logo: str, navigation: str) -> str:
    """Rendert ein HTML-Fragment; logo und navigation sind bereits sicheres HTML."""
    basis = liste.basis
    zeilen = "".join(_zeile_html(zeile) for zeile in liste.zeilen) or (
        '<tr><td colspan="5">Keine Mietverhältnisse für diese Auswahl.</td></tr>'
    )
    summen = _summen_html(liste)
    return (
        f"{_CSS}"
        '<section class="kaut-seite">'
        '<div class="kaut-kopf"><h1>Kautionsübersicht</h1>'
        f'<div class="kaut-logo">{logo}</div></div>'
        f'<div class="kaut-navigation">{navigation}</div>'
        f"<p>Listenstichtag: {_datum(basis.stichtag)}</p>"
        '<p class="kaut-hinweis">Die Beträge zeigen die vorhandenen Unterlagen – nicht den heutigen Kontostand.</p>'
        f"{_zaehler_html(liste)}"
        + (f'<p>{liste.erledigte_ausgeblendet} {"erledigter Kautionsfall" if liste.erledigte_ausgeblendet == 1 else "erledigte Kautionsfälle"} ausgeblendet. Über „Kautionsfälle“ bleibt die Historie abrufbar.</p>' if liste.erledigte_ausgeblendet else "")
        + f"{_filter_html(liste)}"
        f"{_gruppensummen_html(liste)}"
        '<button type="button" class="kaut-druck" onclick="window.print()">Drucken</button>'
        '<div class="kaut-tabellenrahmen"><table class="kaut-tabelle"><thead><tr>'
        "<th>Mieter / Objekt / Einheit</th>"
        '<th class="kaut-betrag">Kaution laut Vertrag</th>'
        '<th class="kaut-betrag">Bestätigter Eingang</th>'
        "<th>Einordnung</th><th>Akte / Details</th>"
        f"</tr></thead><tbody>{zeilen}</tbody></table></div>"
        '<details class="kaut-erlaeuterung"><summary>Summen und Erläuterung</summary>'
        f"{summen}{_ERLAEUTERUNG}</details>"
        f'<div class="kaut-nur-druck">{summen}{_ERLAEUTERUNG}</div>'
        '<footer class="kaut-fuss">JLB Projects GmbH, hausverwaltung@jlb-immo.at</footer>'
        "</section>"
    )
