"""Drei automatische Online-Listen: Mieterliste, Zinsliste, Salden
(Auftrag HV-20260930-PORTAL-LISTEN).

Rein lesend: jede Seite wird bei jedem GET neu aus
`portallisten.service` berechnet - keine Buchung, keine Vorschreibung,
kein Mahnfall, kein Audit-Eintrag, kein Job. Scope/Objektfilter und die
Ablehnung fremder/unbekannter/ausgeschlossener Objekte entsprechen der
Rückstandsübersicht (`routes/dashboard.py`)."""

from __future__ import annotations

from datetime import datetime
from html import escape as h
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from mietinkasso.backoffice.vertragsanlage_form import telefon_html
from mietinkasso.backoffice.views import eur, nutzungsstatus_label, option, sperrgrund_label
from mietinkasso.domain.exceptions import MietinkassoError
from mietinkasso.indexautomatik.zeit import WIEN, heute_wien
from mietinkasso.portallisten.service import (
    berechne_mieterliste,
    berechne_saldenliste,
    berechne_zinsliste,
)

from mietinkasso.backoffice import dependencies as deps
from mietinkasso.backoffice.auth import _ctx, _current_session, _fehlerseite, _layout
from mietinkasso.backoffice.routes.shared import listen_links_html


#: Ohne eigenes Prefix - das gemeinsame `/backoffice`-Prefix wird GENAU
#: EINMAL in `backoffice/app.py` gesetzt.
router = APIRouter()


_NICHT_HINTERLEGT = '<span class="muted">Nicht hinterlegt</span>'
_KEIN_WERT = '<span class="muted">–</span>'


# Lädt die Seite alle 60 Sekunden neu - NUR solange der Tab sichtbar ist
# und kein Filterfeld gerade bearbeitet wird. `location.reload()` behält
# die Query (= den Filter); ein im Hintergrund liegender Tab erzeugt
# keinen einzigen Request.
_AUTO_AKTUALISIERUNG_HTML = """
<p class="muted">Automatische Aktualisierung alle 60 Sekunden, solange diese Seite sichtbar ist - der
   gewählte Filter bleibt erhalten.</p>
<script>
(function () {
  var timer = null;
  function planen() {
    if (timer) { clearTimeout(timer); timer = null; }
    if (document.visibilityState !== "visible") { return; }
    timer = setTimeout(function () {
      var aktiv = document.activeElement;
      var inEingabe = aktiv && (aktiv.tagName === "INPUT" || aktiv.tagName === "SELECT" || aktiv.tagName === "TEXTAREA");
      if (document.visibilityState === "visible" && !inEingabe) { window.location.reload(); } else { planen(); }
    }, 60000);
  }
  document.addEventListener("visibilitychange", planen);
  planen();
})();
</script>"""


def _objekt_optionen_html(objekt_optionen, ausgewaehlt: str | None) -> str:
    optionen = [option("", "Alle Objekte", selected=(ausgewaehlt is None))]
    je_gesellschaft: dict[str, list] = {}
    for o in objekt_optionen:
        je_gesellschaft.setdefault(o.gesellschaft_name, []).append(o)
    for gesellschaft_name, objekte in je_gesellschaft.items():
        optionen.append(f'<optgroup label="{h(gesellschaft_name)}">')
        for o in objekte:
            optionen.append(option(o.id, o.bezeichnung, selected=(o.id == ausgewaehlt)))
        optionen.append("</optgroup>")
    return "".join(optionen)


def _zeitstempel_html(datenstand_html: str) -> str:
    """Anzeige-/Berechnungszeitpunkt IMMER getrennt vom Datenstand
    beschriftet - die Anzeigezeit sagt nichts über die Aktualität der
    Bankdaten."""

    jetzt = datetime.now(WIEN)
    return (
        '<p class="status-zeile">'
        f"<span>{datenstand_html}</span>"
        f"<span>Berechnet/angezeigt: {jetzt:%d.%m.%Y %H:%M} (Wien)</span></p>"
    )


def _filter_feld(feld_id: str, beschriftung: str, feld_html: str) -> str:
    """Ein Feld der kompakten Filterzeile - Beschriftung per `for`/`id`
    fest mit dem Eingabefeld verbunden."""

    return f'<div><label for="{feld_id}">{h(beschriftung)}</label>{feld_html}</div>'


def _objekt_filter_feld(objekt_optionen, ausgewaehlt: str | None) -> str:
    return _filter_feld(
        "filter-objekt", "Objekt",
        f'<select id="filter-objekt" name="objekt_id">{_objekt_optionen_html(objekt_optionen, ausgewaehlt)}</select>',
    )


def _pfad_mit_query(pfad: str, **parameter: str | None) -> str:
    gesetzt = {name: wert for name, wert in parameter.items() if wert}
    return f"{pfad}?{urlencode(gesetzt)}" if gesetzt else pfad


def _akte_link(vertrag_id: str, objekt_filter: str | None, text: str = "Akte öffnen") -> str:
    href = _pfad_mit_query(f"/backoffice/vertrag/{vertrag_id}", von_objekt=objekt_filter)
    return f'<a href="{h(href)}">{h(text)}</a>'


def _betrag_html(cent: int | None) -> str:
    return eur(cent) if cent is not None else _KEIN_WERT


# -- 1. Mieterliste -----------------------------------------------------------


_MIETER_STATUS_LABEL = (
    ("aktiv", "Laufende und künftige"), ("beendet", "Nur beendete"), ("alle", "Alle (beendete markiert)"),
)


def _mieter_zeile_html(z, objekt_filter: str | None) -> str:
    if z.status == "BEENDET":
        status_html = f'<span class="badge badge-warn">beendet am {z.gueltig_bis.isoformat()}</span>'
    elif z.status == "KUENFTIG":
        # Ein künftiger Mieter ist noch NICHT aktiv - eigenes Etikett.
        status_html = f'<span class="badge badge-muted">künftig - beginnt am {z.gueltig_von.isoformat()}</span>'
    else:
        status_html = '<span class="badge badge-ok">aktiv</span>'
        if z.gueltig_bis is not None:
            status_html += f' <span class="muted">bis {z.gueltig_bis.isoformat()}</span>'
    objekt_adresse = h(z.objekt_adresse) if z.objekt_adresse else _NICHT_HINTERLEGT
    email_html = f'<a href="mailto:{h(z.email)}">{h(z.email)}</a>' if z.email else _NICHT_HINTERLEGT
    ehemalig = {
        "BEENDET": " <span class='muted'>(ehemaliger Mieter)</span>",
        "KUENFTIG": " <span class='muted'>(künftiger Mieter)</span>",
    }.get(z.status, "")
    return (
        "<tr>"
        f"<td><strong>{h(z.debitor_name)}</strong>{ehemalig}</td>"
        f"<td>{h(z.objekt_bezeichnung)} / {h(z.einheit_bezeichnung)}"
        f"<br><span class='muted'>Objektadresse:</span> {objekt_adresse}</td>"
        f"<td>{h(z.korrespondenzadresse) if z.korrespondenzadresse else _NICHT_HINTERLEGT}</td>"
        f'<td class="nowrap">{telefon_html(z.telefon)}</td>'
        f"<td>{email_html}</td>"
        f"<td class='listen-status'>{status_html}</td>"
        f'<td class="nowrap">{_akte_link(z.vertrag_id, objekt_filter)}</td>'
        "</tr>"
    )


@router.get("/mieterliste", response_class=HTMLResponse)
def mieterliste(
    request: Request, objekt_id: str | None = None, status: str | None = None, q: str | None = None,
    session=Depends(_current_session),
) -> HTMLResponse:
    try:
        liste = berechne_mieterliste(
            ctx=_ctx(session), objekt_id=objekt_id or None, stammdaten_repository=deps._stammdaten_repo,
            status=status or "aktiv", suche=q,
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Mieterliste", str(exc), "/backoffice/mieterliste")

    status_optionen = "".join(
        option(wert, label, selected=(wert == liste.status_filter)) for wert, label in _MIETER_STATUS_LABEL
    )
    zeilen_html = "".join(_mieter_zeile_html(z, liste.objekt_filter) for z in liste.zeilen) or (
        '<tr><td colspan=7 class="muted">Keine Mietverhältnisse für diesen Filter.</td></tr>'
    )
    inhalt = f"""
    <div class="card">
      <h1>Mieterliste</h1>
      {listen_links_html(aktiver_pfad="/backoffice/mieterliste", objekt_id=liste.objekt_filter)}
      <form method="get" action="/backoffice/mieterliste" class="listen-filter">
        {_objekt_filter_feld(liste.objekt_optionen, liste.objekt_filter)}
        {_filter_feld("filter-status", "Mietverhältnis", f'<select id="filter-status" name="status">{status_optionen}</select>')}
        {_filter_feld(
            "filter-q", "Suche",
            f'<input id="filter-q" type="search" name="q" value="{h(liste.suche or "")}" maxlength="100"'
            ' placeholder="Name, Objekt, Telefon, E-Mail">',
        )}
        <button type="submit">Anzeigen</button>
      </form>
      {_zeitstempel_html(
          f"<strong>{liste.anzahl_aktiv}</strong> aktiv · {liste.anzahl_kuenftig} künftig · "
          f"{liste.anzahl_beendet} beendet · Stand Mietverhältnis {liste.stichtag.isoformat()}"
      )}
      <details>
        <summary>Was zeigt diese Liste?</summary>
        <ul>
          <li>Ein Mieter ist ausschließlich, wer über einen Vertrag als Debitor geführt wird. Die Zahlen oben
              gelten für das gewählte Objekt und die Suche.</li>
          <li>„aktiv“ heißt: der Vertrag läuft am heutigen Tag. Ein bereits angelegter, aber erst später
              beginnender Vertrag steht als „künftig“, ein abgelaufener nur unter „Nur beendete“/„Alle“.</li>
          <li>„Nicht hinterlegt“ heißt: die Angabe fehlt in den Stammdaten - es wird nichts ergänzt oder
              geschätzt. Die Telefonnummer wird in der Akte des Mieters gepflegt.</li>
          <li>„Objektadresse“ ist der Ort des Mietobjekts, „Korrespondenzadresse“ die beim Mieter hinterlegte
              Postadresse.</li>
          <li>Die Seite wird bei jedem Aufruf neu aus dem aktuellen Datenbestand berechnet.</li>
        </ul>
      </details>
    </div>
    <div class="card">
      <div class="tabelle-scroll">
      <table style="min-width: 900px">
        <tr><th>Mieter</th><th>Mietobjekt / Einheit</th><th>Korrespondenzadresse</th><th class="nowrap">Telefon</th>
            <th>E-Mail</th><th>Mietverhältnis</th><th></th></tr>
        {zeilen_html}
      </table>
      </div>
    </div>
    {_AUTO_AKTUALISIERUNG_HTML}
    <p><a href="/backoffice/">&larr; zur Übersicht</a></p>"""
    return _layout(request, session, "Mieterliste", inhalt)


# -- 2. Zinsliste -------------------------------------------------------------


def _nachbarmonat(monat: str, schritt: int) -> str | None:
    jahr, monatszahl = (int(teil) for teil in monat.split("-"))
    index = jahr * 12 + (monatszahl - 1) + schritt
    jahr, monatszahl = index // 12, index % 12 + 1
    if not 1 <= jahr <= 9999:
        return None
    return f"{jahr:04d}-{monatszahl:02d}"


def _zins_mieter_html(z, liste) -> str:
    if z.vertrag_id is not None:
        laufzeit = f"{z.vertrag_von.isoformat()} – {z.vertrag_bis.isoformat() if z.vertrag_bis else 'unbefristet'}"
        # Beginnt der Vertrag erst im Monat, ist der Mieter am Monatsersten
        # noch nicht Mieter - sichtbar am Namen, nicht nur im Hinweistext.
        beginn = (
            f' <span class="badge badge-muted">erst ab {z.vertrag_von.isoformat()}</span>'
            if z.vertrag_von > liste.monatsanfang else ""
        )
        return (
            f"<strong>{h(z.debitor_name or '-')}</strong>{beginn}<br>"
            f"{_akte_link(z.vertrag_id, liste.objekt_filter, z.vertrag_id)} <span class='muted'>({laufzeit})</span>"
        )
    if z.letzter_vertrag_id is not None:
        return (
            '<span class="muted">kein im Monat gültiger Vertrag</span><br>'
            f"<span class='muted'>zuletzt (beendet {z.letzter_vertrag_bis.isoformat()}): "
            f"{h(z.letzter_vertrag_debitor_name or '-')},</span> "
            f"{_akte_link(z.letzter_vertrag_id, liste.objekt_filter, z.letzter_vertrag_id)}"
        )
    return '<span class="muted">kein Vertrag</span>'


def _zins_gesamt_html(z, liste) -> str:
    if z.brutto_gesamt_cent is not None:
        details = "".join(
            f"<br><span class='muted'>{h(k.art)} {h(k.bezeichnung)}: {eur(k.betrag_cent)}</span>" for k in z.komponenten
        )
        # Änderung im Monat: der Betrag ist nur der Stand am Monatsersten -
        # weder anteilig gerechnet noch als geklärter Monatsbetrag ausgegeben.
        stand = (
            '<br><span class="badge badge-warn">Stand Monatserster - Änderung im Monat, nicht anteilig</span>'
            if z.aenderung_im_monat else ""
        )
        return (
            f"<strong>{eur(z.brutto_gesamt_cent)}</strong>{stand}"
            f"<details><summary>Bestandteile</summary>{details}</details>"
        )
    if z.vertrag_id is not None:
        return '<span class="badge badge-warn">Betrag unbekannt</span>'
    if z.variabel:
        href = _pfad_mit_query("/backoffice/variable-abrechnung", monat=liste.monat)
        bericht = {
            None: "kein Monatsbericht vorhanden", "BESTAETIGT": "Monatsbericht bestätigt",
            "ENTWURF": "Monatsbericht nur Entwurf",
        }.get(z.variabler_bericht_status, f"Monatsbericht {z.variabler_bericht_status}")
        return f'<a href="{h(href)}">variabel - Monatsbericht</a><br><span class="muted">{h(bericht)}</span>'
    return '<span class="muted">kein Sollbetrag</span>'


def _zins_zeile_html(z, liste) -> str:
    nutzung_html = h(nutzungsstatus_label(z.nutzungsstatus))
    if z.pruefbedarf:
        nutzung_html += ' <span class="badge badge-warn">prüfen</span>'
    if z.vertrag_id is None:
        netto_html = _KEIN_WERT
    elif z.netto_miete_geprueft_cent is not None:
        netto_html = eur(z.netto_miete_geprueft_cent)
    else:
        netto_html = '<span class="muted">Aufteilung unbekannt</span>'
    hinweise_html = "<br>".join(h(text) for text in z.hinweise) or _KEIN_WERT
    return (
        "<tr>"
        f"<td>{h(z.objekt_bezeichnung)} / {h(z.einheit_bezeichnung)}</td>"
        f"<td>{nutzung_html}</td>"
        f"<td>{_zins_mieter_html(z, liste)}</td>"
        f'<td class="nowrap">{_betrag_html(z.miete_cent)}</td>'
        f'<td class="nowrap">{_betrag_html(z.kueche_cent)}</td>'
        f'<td class="nowrap">{_betrag_html(z.stellplatz_cent)}</td>'
        f'<td class="nowrap">{_betrag_html(z.nebenkosten_cent)}</td>'
        f"<td>{_zins_gesamt_html(z, liste)}</td>"
        f'<td class="nowrap">{netto_html}</td>'
        f"<td>{hinweise_html}</td>"
        "</tr>"
    )


@router.get("/zinsliste", response_class=HTMLResponse)
def zinsliste(
    request: Request, monat: str | None = None, objekt_id: str | None = None, q: str | None = None,
    session=Depends(_current_session),
) -> HTMLResponse:
    heute = heute_wien()
    gewaehlter_monat = (monat or "").strip() or f"{heute.year:04d}-{heute.month:02d}"
    try:
        liste = berechne_zinsliste(
            ctx=_ctx(session), monat=gewaehlter_monat, objekt_id=objekt_id or None,
            stammdaten_repository=deps._stammdaten_repo, variable_service=deps._variableabrechnung.service,
            komponenten_freigabe_service=deps._variableabrechnung.komponenten_freigabe_service, suche=q,
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Zinsliste", str(exc), "/backoffice/zinsliste")

    monatslinks = []
    for schritt, text in ((-1, "&larr; Vormonat"), (1, "Folgemonat &rarr;")):
        nachbar = _nachbarmonat(liste.monat, schritt)
        if nachbar is not None:
            href = _pfad_mit_query("/backoffice/zinsliste", monat=nachbar, objekt_id=liste.objekt_filter, q=liste.suche)
            monatslinks.append(f'<a href="{h(href)}">{text}</a>')
    zeilen_html = "".join(_zins_zeile_html(z, liste) for z in liste.zeilen) or (
        '<tr><td colspan=10 class="muted">Keine Einheiten für diesen Filter.</td></tr>'
    )
    s = liste.summen
    aenderung_html = (
        f'<span class="warn">{s.anzahl_aenderung_im_monat} davon mit Änderung im Monat '
        f"({eur(s.brutto_aenderung_im_monat_cent)}, Stand Monatserster)</span>"
        if s.anzahl_aenderung_im_monat else ""
    )
    monat_feld = (
        f'<input id="filter-monat" type="month" name="monat" value="{h(liste.monat)}" placeholder="2026-09"'
        ' pattern="[0-9]{4}-(0[1-9]|1[0-2])" required>'
    )
    inhalt = f"""
    <div class="card">
      <h1>Zinsliste {h(liste.monat)}</h1>
      {listen_links_html(aktiver_pfad="/backoffice/zinsliste", objekt_id=liste.objekt_filter)}
      <form method="get" action="/backoffice/zinsliste" class="listen-filter">
        {_filter_feld("filter-monat", "Monat", monat_feld)}
        {_objekt_filter_feld(liste.objekt_optionen, liste.objekt_filter)}
        {_filter_feld(
            "filter-q", "Suche",
            f'<input id="filter-q" type="search" name="q" value="{h(liste.suche or "")}" maxlength="100"'
            ' placeholder="Mieter, Objekt, Einheit">',
        )}
        <button type="submit">Anzeigen</button>
      </form>
      <p class="status-zeile">
        <span>Brutto gesamt: <strong>{eur(s.brutto_gesamt_cent)}</strong> aus {s.anzahl_mit_betrag} Zeile(n)</span>
        <span>{s.anzahl_ohne_betrag} ohne Betrag (nicht als 0 gezählt)</span>
        {aenderung_html}
        {''.join(f'<span>{link}</span>' for link in monatslinks)}
      </p>
      {_zeitstempel_html(f"Vertragsstand am {liste.monatsanfang.isoformat()} · keine Zahlungsdaten")}
      <details>
        <summary>Was zeigt diese Liste?</summary>
        <ul>
          <li>Beginnt oder endet ein Vertrag oder ein Bestandteil mitten im Monat, steht beim Betrag
              „Stand Monatserster“: gezeigt wird, was am Monatsersten galt - es wird nichts anteilig
              gerechnet. Diese Zeilen sind in der Summe enthalten und oben getrennt ausgewiesen.</li>
          <li>Monatliche <strong>vertragliche</strong> Beträge aus den gespeicherten Vertragskomponenten, die am
              Monatsersten gelten. Es entsteht <strong>keine Vorschreibung</strong> und keine Sollstellung; ob
              gezahlt wurde, steht unter „Salden“.</li>
          <li>Beträge sind die gespeicherten Komponentenbeträge (Brutto laut Vorschreibung). Eine Netto-/USt-Aufteilung
              wird nur gezeigt, wo für alle Mietkomponenten eine geprüfte Netto-Mietanteil-Freigabe vorliegt -
              sonst „Aufteilung unbekannt“.</li>
          <li>„–“ heißt: dieser Bestandteil ist nicht hinterlegt. „Betrag unbekannt“ heißt: Vertrag gültig,
              aber keine am Monatsersten gültige Komponente - das ist nie 0 € und fließt in keine Summe ein.</li>
          <li>Die Nutzung (vermietet, Leerstand, Kurzzeitvermietung, Selfstorage, Eigennutzung) ist der
              <strong>aktuell gepflegte</strong> Nutzungsstatus der Einheit, nicht aus Beträgen oder Namen
              abgeleitet und ohne Historie. Ein beendeter Vertrag beweist keinen Leerstand - dann steht
              „prüfen“.</li>
          <li>Kurzzeitvermietung/Selfstorage ohne festen Vertrag haben variable Erlöse; sie stehen im
              Monatsbericht und werden hier nicht als Mietzins summiert.</li>
          <li>Eine noch nicht umgesetzte Indexanpassung ist nicht enthalten: maßgeblich ist allein die
              gespeicherte Komponente.</li>
          <li>Die Seite wird bei jedem Aufruf neu aus dem aktuellen Datenbestand berechnet; sie enthält
              keine Bank- oder Zahlungsdaten.</li>
        </ul>
      </details>
    </div>
    <div class="card">
      <div class="tabelle-scroll">
      <table style="min-width: 1080px">
        <tr><th>Objekt / Einheit</th><th>Nutzung</th><th>Mieter / Vertrag</th><th class="nowrap">Miete</th>
            <th class="nowrap">Küche</th><th class="nowrap">Stellplatz</th><th class="nowrap">BK / Sonstige</th>
            <th class="nowrap">Brutto gesamt</th><th class="nowrap">Netto-Miete (geprüft)</th><th>Hinweise</th></tr>
        {zeilen_html}
        <tr><th colspan=3>Summe der {s.anzahl_mit_betrag} Zeile(n) mit Betrag
              ({s.anzahl_ohne_betrag} ohne Betrag nicht enthalten{
                  f"; {s.anzahl_aenderung_im_monat} mit Stand Monatserster" if s.anzahl_aenderung_im_monat else ""
              })</th>
            <th class="nowrap">{eur(s.miete_cent)}</th><th class="nowrap">{eur(s.kueche_cent)}</th>
            <th class="nowrap">{eur(s.stellplatz_cent)}</th><th class="nowrap">{eur(s.nebenkosten_cent)}</th>
            <th class="nowrap">{eur(s.brutto_gesamt_cent)}</th><th></th><th></th></tr>
      </table>
      </div>
    </div>
    {_AUTO_AKTUALISIERUNG_HTML}
    <p><a href="/backoffice/">&larr; zur Übersicht</a></p>"""
    return _layout(request, session, f"Zinsliste {liste.monat}", inhalt)


# -- 3. Salden ----------------------------------------------------------------


_SALDEN_STATUS_LABEL = (("", "Alle Konten"), ("offen", "Offen"), ("guthaben", "Guthaben"), ("ausgeglichen", "Ausgeglichen"))


def _bank_kurz_html(bankstaende) -> str:
    """EINE kurze, immer sichtbare Zeile zum Bankstand. Hält drei Dinge
    auseinander: bis wann die Kontoauszüge ausdrücklich als lückenlos
    BESTÄTIGT sind, von wann die letzte IMPORTIERTE Buchung stammt (kein
    Vollständigkeitsnachweis), und - getrennt daneben - wann die Seite
    berechnet wurde. Die Tabelle je Bankkonto steht eingeklappt darunter."""

    konten = [b for b in bankstaende if b.bank_konto_id is not None]
    warnungen = []
    ohne_konto = len(bankstaende) - len(konten)
    if ohne_konto or not bankstaende:
        warnungen.append("kein Bankkonto hinterlegt" if not konten else f"{ohne_konto} Gesellschaft(en) ohne Bankkonto")
    nie_bestaetigt = sum(1 for b in konten if b.bestaetigt_bis is None)
    if nie_bestaetigt:
        warnungen.append(
            "Kontoauszüge nie als lückenlos bestätigt" if len(konten) == 1
            else f"{nie_bestaetigt} von {len(konten)} Bankkonten nie als lückenlos bestätigt"
        )
    ohne_import = sum(1 for b in konten if b.letzte_buchung is None)
    if ohne_import:
        warnungen.append(
            "keine Bankzeile importiert" if len(konten) == 1
            else f"{ohne_import} von {len(konten)} Bankkonten ohne importierte Bankzeile"
        )

    teile = []
    bestaetigt = [b.bestaetigt_bis for b in konten if b.bestaetigt_bis is not None]
    if bestaetigt:
        # Bei mehreren Konten zählt das am wenigsten weit bestätigte.
        einschraenkung = "" if len(konten) == 1 else " (frühestes Konto)"
        teile.append(f"Kontoauszüge lückenlos bestätigt bis <strong>{min(bestaetigt).isoformat()}</strong>{einschraenkung}")
    importiert = [b.letzte_buchung for b in konten if b.letzte_buchung is not None]
    if importiert:
        teile.append(f"letzte importierte Buchung vom {max(importiert).isoformat()}")
    teile.extend(f'<span class="warn">{h(text)}</span>' for text in warnungen)
    return (
        '<p class="status-zeile"><span>Bank: ' + " · ".join(teile) + "</span>"
        "<span>Zahlungen nach diesem Stand fehlen in den Salden noch.</span></p>"
    )


def _saldo_zeile_html(z, liste) -> str:
    if z.status == "KEIN_KONTO":
        status_html = '<span class="badge badge-warn">Kein Mietkonto - Saldo unbekannt</span>'
    elif z.status == "OFFEN" and z.faellig_cent:
        status_html = '<span class="badge badge-error">Rückstand fällig</span>'
    elif z.status == "OFFEN":
        status_html = '<span class="badge badge-muted">Offener Betrag</span>'
    elif z.status == "GUTHABEN":
        status_html = '<span class="badge badge-ok">Guthaben</span>'
    else:
        status_html = '<span class="badge badge-ok">ausgeglichen</span>'

    hinweise = []
    if z.sperrgruende:
        labels = ", ".join(sperrgrund_label(g) for g in z.sperrgruende)
        hinweise.append(
            f'<span class="badge badge-error" title="{h(", ".join(z.sperrgruende))}">Mahnung gesperrt ({h(labels)})</span>'
        )
    if z.abweichung_cent:
        hinweise.append(f'<span class="badge badge-warn">Kontoabweichung {eur(z.abweichung_cent)}</span>')
    if z.historisch:
        hinweise.append('<span class="badge badge-muted">Vertrag beendet</span>')
    if z.mahnfaelle_anzahl:
        hinweise.append(f'<span class="badge badge-muted">{z.mahnfaelle_anzahl} Mahnfall(e)</span>')
    if hinweise:
        status_html += " " + " ".join(hinweise)

    konto_html = f' · <a href="/backoffice/konto/{h(z.konto_id)}">Konto</a>' if z.konto_id else ""
    return (
        f"<tr class='{'gesperrt-row' if z.sperrgruende else ''}'>"
        f"<td><strong>{h(z.debitor_name)}</strong><br><span class='muted'>{h(z.objekt_bezeichnung)} / "
        f"{h(z.einheit_bezeichnung)} ({h(nutzungsstatus_label(z.nutzungsstatus))})</span></td>"
        f'<td class="nowrap">{_betrag_html(z.offen_cent)}</td>'
        f'<td class="nowrap">{_betrag_html(z.faellig_cent)}</td>'
        f'<td class="nowrap">{_betrag_html(z.nicht_faellig_cent)}</td>'
        f'<td class="nowrap">{_betrag_html(z.faelligkeit_unbekannt_cent)}</td>'
        f'<td class="nowrap">{_betrag_html(z.guthaben_cent)}</td>'
        f"<td class='listen-status'>{status_html}</td>"
        f'<td class="nowrap">{_akte_link(z.vertrag_id, liste.objekt_filter)}{konto_html}</td>'
        "</tr>"
    )


def _bankstand_zeile_html(b) -> str:
    if b.bank_konto_id is None:
        return (
            f"<tr><td>{h(b.gesellschaft_name)}</td>"
            '<td colspan=3 class="warn">Kein Bankkonto hinterlegt - es liegen keine Bankdaten vor.</td></tr>'
        )
    bestaetigt_html = b.bestaetigt_bis.isoformat() if b.bestaetigt_bis else '<span class="warn">nie bestätigt</span>'
    letzte_html = (
        b.letzte_buchung.isoformat() if b.letzte_buchung else '<span class="warn">keine Bankzeile importiert</span>'
    )
    return (
        f"<tr><td>{h(b.gesellschaft_name)}</td><td>{h(b.bank_konto_id)} ({h(b.bank_konto_bezeichnung or '')})</td>"
        f"<td>{bestaetigt_html}</td><td>{letzte_html}</td></tr>"
    )


@router.get("/salden", response_class=HTMLResponse)
def salden(
    request: Request, objekt_id: str | None = None, status: str | None = None, session=Depends(_current_session),
) -> HTMLResponse:
    try:
        liste = berechne_saldenliste(
            ctx=_ctx(session), objekt_id=objekt_id or None, stammdaten_repository=deps._stammdaten_repo,
            op_service=deps._op_service, mahn_fall_repository=deps._mahn_fall_repo,
            bank_repository=deps._bank_repo, bank_service=deps._bank_service, status=status or "",
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Salden", str(exc), "/backoffice/salden")

    status_optionen = "".join(
        option(wert, label, selected=(wert == liste.status_filter)) for wert, label in _SALDEN_STATUS_LABEL
    )
    zeilen_html = "".join(_saldo_zeile_html(z, liste) for z in liste.zeilen) or (
        '<tr><td colspan=8 class="muted">Keine Mietkonten für diesen Filter.</td></tr>'
    )
    bank_html = "".join(_bankstand_zeile_html(b) for b in liste.bankstaende) or (
        '<tr><td colspan=4 class="muted">Keine Gesellschaft im Filter.</td></tr>'
    )
    s = liste.summen
    inhalt = f"""
    <div class="card">
      <h1>Salden</h1>
      {listen_links_html(aktiver_pfad="/backoffice/salden", objekt_id=liste.objekt_filter)}
      <form method="get" action="/backoffice/salden" class="listen-filter">
        {_objekt_filter_feld(liste.objekt_optionen, liste.objekt_filter)}
        {_filter_feld("filter-status", "Status", f'<select id="filter-status" name="status">{status_optionen}</select>')}
        <button type="submit">Anzeigen</button>
      </form>
      <p class="status-zeile">
        <span>Offene Kontosalden: <strong>{eur(s.offen_cent)}</strong></span>
        <span>Fällige Einzelposten: <strong>{eur(s.faellig_cent)}</strong></span>
        <span>Guthaben der Mieter: <strong>{eur(s.guthaben_cent)}</strong></span>
        {f'<span class="warn">{s.anzahl_ohne_konto} ohne Mietkonto (Saldo unbekannt)</span>' if s.anzahl_ohne_konto else ''}
      </p>
      {_bank_kurz_html(liste.bankstaende)}
      {_zeitstempel_html(f"Buchungen bis {liste.stichtag.isoformat()}")}
      <details>
        <summary>Was bedeuten die Spalten?</summary>
        <ul>
          <li><strong>Kontosaldo offen</strong>: positiver Stand des Mietkontos (Eröffnung + Vorschreibungen −
              Zahlungen/Gutschriften). <strong>Guthaben</strong>: negativer Stand, als positiver Betrag gezeigt.
              Das Guthaben eines Mieters wird nie vom Rückstand eines anderen abgezogen - auch nicht in den Summen.</li>
          <li><strong>Fällig</strong>: offene Einzelposten mit bekannter, verstrichener Fälligkeit.
              <strong>Noch nicht fällig</strong>: bekannte künftige Fälligkeit. <strong>Fälligkeit unbekannt</strong>:
              kein Fälligkeitsdatum erfasst - das heißt nicht „strittig“ und nicht „fällig“.</li>
          <li>Diese drei Spalten sind Summen der offenen <strong>Einzelposten</strong> und werden eigenständig
              ermittelt - sie sind keine Aufteilung des Kontosaldos und müssen sich nicht zu ihm addieren.
              Weichen Kontostand und Einzelposten voneinander ab, steht in der Zeile „Kontoabweichung“ mit dem
              Betrag; Details in der Akte bzw. im Konto.</li>
          <li><strong>Kein Mietkonto</strong>: der Saldo ist unbekannt, nicht ausgeglichen.</li>
          <li>Eine Mahnsperre bleibt sichtbar. Diese Seite plant oder versendet keine Mahnung.</li>
          <li>„Buchungen bis“ ist der Stichtag der Saldorechnung, „Berechnet/angezeigt“ der Zeitpunkt des
              Seitenaufbaus. Beides sagt nichts über die Aktualität der Bankdaten - dafür steht die Zeile „Bank“.</li>
        </ul>
      </details>
    </div>
    <div class="card">
      <div class="tabelle-scroll">
      <table style="min-width: 900px">
        <tr><th>Mieter / Einheit</th><th class="nowrap">Kontosaldo offen</th><th class="nowrap">Fällig</th>
            <th class="nowrap">Noch nicht fällig</th><th class="nowrap">Fälligkeit unbekannt</th>
            <th class="nowrap">Guthaben</th><th>Status</th><th></th></tr>
        {zeilen_html}
        <tr><th>Summen ({s.anzahl_zeilen} Zeile(n), getrennt - keine Verrechnung)</th>
            <th class="nowrap">{eur(s.offen_cent)}</th><th class="nowrap">{eur(s.faellig_cent)}</th>
            <th class="nowrap">{eur(s.nicht_faellig_cent)}</th><th class="nowrap">{eur(s.faelligkeit_unbekannt_cent)}</th>
            <th class="nowrap">{eur(s.guthaben_cent)}</th><th></th><th></th></tr>
      </table>
      </div>
    </div>
    <details class="card">
      <summary>Bankdatenstand je Bankkonto</summary>
      <p class="muted">Zahlungen erscheinen erst, nachdem eine Bankdatei eingelesen und zugeordnet wurde - es gibt
         keinen automatischen Bankabruf. „Lückenlos bestätigt bis“ ist die ausdrückliche Bestätigung unter
         <a href="/backoffice/bank/vollstaendigkeit">Bankvollständigkeit</a>; das Datum der letzten importierten
         Buchung allein beweist keine Vollständigkeit. Ein offener Saldo kann daher eine bereits geleistete, noch
         nicht eingespielte Zahlung enthalten. Ein Bankkonto gehört einer Gesellschaft - der Stand gilt für alle
         ihre Mietkonten gemeinsam.</p>
      <div class="tabelle-scroll">
      <table style="min-width: 640px">
        <tr><th>Gesellschaft</th><th>Bankkonto</th><th>Lückenlos bestätigt bis</th><th>Letzte importierte Buchung vom</th></tr>
        {bank_html}
      </table>
      </div>
    </details>
    {_AUTO_AKTUALISIERUNG_HTML}
    <p><a href="/backoffice/">&larr; zur Übersicht</a></p>"""
    return _layout(request, session, "Salden", inhalt)
