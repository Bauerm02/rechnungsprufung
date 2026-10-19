"""MieWeG-2026-Berechnungsvorschau je Vertrag.

AUSDRÜCKLICH nur Vorschau: keine Route hier löst eine Vorschreibung,
Freigabe oder einen Versand aus."""

from __future__ import annotations

import json

from datetime import date
from decimal import Decimal, InvalidOperation
from html import escape as h

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from mietinkasso.backoffice.views import csrf_feld, eur, option, parse_eur_betrag
from mietinkasso.domain.exceptions import MietinkassoError
from mietinkasso.mieweg_vorschau.service import VpiWert

from mietinkasso.backoffice import dependencies as deps
from mietinkasso.backoffice.auth import _ctx, _current_session, _fehlerseite, _layout, _objekt_fuer_vertrag_gesperrt, _verify_csrf
from mietinkasso.backoffice.routes.shared import _RECHTSORDNUNGEN_FUER_AUSWAHL


#: Ohne eigenes Prefix - das gemeinsame `/backoffice`-Prefix wird GENAU
#: EINMAL in `backoffice/app.py` gesetzt.
router = APIRouter()


# -- MieWeG-2026-Berechnungsvorschau (Auftrag 12.09., Paket C) ---------------------
#
# AUSDRÜCKLICH nur eine Berechnungsvorschau: keine Route hier löst eine
# Vorschreibung, Freigabe oder einen Versand aus - siehe
# `mieweg_vorschau/service.py`.


def _parse_vpi_zeilen(rohtext: str) -> dict[int, VpiWert]:
    """Format je Zeile: `JAHR;WERT;QUELLE;DATUM` - bewusst eine einfache
    Textform statt dynamischer JS-Zeilen, um die Bedienseite klein zu
    halten. Leere Zeilen werden übersprungen; eine fehlerhafte Zeile
    wird NICHT still ignoriert, sondern als ValueError gemeldet (kein
    erfundener VPI-Wert)."""

    ergebnis: dict[int, VpiWert] = {}
    for zeilennummer, zeile in enumerate(rohtext.splitlines(), start=1):
        zeile = zeile.strip()
        if not zeile:
            continue
        teile = [teil.strip() for teil in zeile.split(";")]
        if len(teile) != 4:
            raise ValueError(
                f"VPI-Zeile {zeilennummer} ('{zeile}') hat nicht das Format JAHR;WERT;QUELLE;DATUM."
            )
        jahr_text, wert_text, quelle, datum = teile
        try:
            jahr = int(jahr_text)
            Decimal(wert_text.replace(",", "."))  # nur Gültigkeit prüfen, Service parst erneut
        except (ValueError, InvalidOperation) as exc:
            raise ValueError(f"VPI-Zeile {zeilennummer} ('{zeile}') hat kein gültiges Jahr/keinen gültigen Wert.") from exc
        ergebnis[jahr] = VpiWert(wert=wert_text.replace(",", "."), quelle=quelle, datum=datum)
    return ergebnis


def _mieweg_vorschau_zeile_html(v) -> str:
    ergebnis = json.loads(v.ergebnis_json)
    hoechstbetrag = (
        eur(v.massgeblicher_hoechstbetrag_cent) if v.massgeblicher_hoechstbetrag_cent is not None else "-"
    )
    gesetzliche_grenze = (
        eur(ergebnis["gesetzliche_hoechstgrenze_cent"]) if ergebnis.get("gesetzliche_hoechstgrenze_cent") is not None else "-"
    )
    vertragsspur = (
        eur(ergebnis["vertraglich_zulaessiger_betrag_cent"])
        if ergebnis.get("vertraglich_zulaessiger_betrag_cent") is not None
        else "-"
    )
    aktuell_verrechnet = (
        eur(ergebnis["aktuell_verrechneter_betrag_cent"])
        if ergebnis.get("aktuell_verrechneter_betrag_cent") is not None
        else "-"
    )
    ausfuehrbare_erhoehung = (
        eur(ergebnis["ausfuehrbare_erhoehung_cent"]) if ergebnis.get("ausfuehrbare_erhoehung_cent") is not None else "-"
    )
    termin = v.fruehester_termin.isoformat() if v.fruehester_termin else "-"
    offene_nachweise = ergebnis.get("offene_nachweise") or []
    blockiert_grund = ergebnis.get("blockiert_grund")
    status_html = (
        f'<span class="error">{h(blockiert_grund)}</span>'
        if blockiert_grund
        else ("<span class=\"ok\">vollständig</span>" if v.vollstaendig else '<span class="warn">Prüfbedarf</span>')
    )
    nachweise_html = "<br>".join(h(n) for n in offene_nachweise) or "-"
    return (
        "<tr>"
        f"<td>{v.version}</td><td>{h(v.rechtsordnung)}</td><td>{v.ziel_bewertungsjahr or '-'}</td>"
        f"<td>{gesetzliche_grenze}</td><td>{vertragsspur}</td><td>{hoechstbetrag}</td>"
        f"<td>{aktuell_verrechnet}</td><td>{ausfuehrbare_erhoehung}</td><td>{h(termin)}</td>"
        f"<td>{status_html}</td><td>{nachweise_html}</td>"
        f"<td>{h(v.erstellt_von)}</td><td>{v.erstellt_am.isoformat() if v.erstellt_am else ''}</td>"
        "</tr>"
    )


@router.get("/vertrag/{vertrag_id}/mieweg-vorschau", response_class=HTMLResponse)
def mieweg_vorschau_uebersicht(request: Request, vertrag_id: str, session=Depends(_current_session)) -> HTMLResponse:
    vertrag = deps._stammdaten_repo.get_vertrag(vertrag_id)
    if vertrag is None:
        return _fehlerseite(session, "MieWeG-Vorschau", f"Unbekannter Vertrag {vertrag_id}.")
    if _objekt_fuer_vertrag_gesperrt(vertrag_id):
        return _fehlerseite(session, "MieWeG-Vorschau", "Objekt ist gesperrt; keine Vorschau möglich.")

    rechtsordnung_optionen = "".join(
        option(r, r, selected=(r == vertrag.rechtsordnung)) for r in _RECHTSORDNUNGEN_FUER_AUSWAHL
    )
    komponenten = deps._stammdaten_repo.list_aktive_komponenten(vertrag_id, date.today())
    indexierbare = [k for k in komponenten if k.indexierbar]
    if indexierbare:
        komponenten_html = "".join(
            f'<label class="muted"><input type="checkbox" name="basis_komponenten_ids" value="{h(k.id)}"> '
            f"{h(k.bezeichnung)} ({eur(k.betrag_cent)})</label><br>"
            for k in indexierbare
        )
    else:
        komponenten_html = (
            '<p class="muted">Keine als indexierbar markierte Komponente vorhanden - der Basisbetrag '
            "unten wird dann ohne Komponentenreferenz erfasst.</p>"
        )
    nicht_indexierbare = [k for k in komponenten if not k.indexierbar]
    if nicht_indexierbare:
        komponenten_html += (
            '<p class="muted">Nicht indexierbar (nie automatisch einbezogen): '
            + ", ".join(h(k.bezeichnung) for k in nicht_indexierbare) + "</p>"
        )

    historie = deps._mieweg_vorschau_service.liste_fuer_vertrag(vertrag_id)
    historie_html = "".join(_mieweg_vorschau_zeile_html(v) for v in historie) or (
        '<tr><td colspan=11 class="muted">Noch keine Vorschau erstellt.</td></tr>'
    )

    inhalt = f"""
    <div class="card">
      <h1>MieWeG-2026-Berechnungsvorschau — {h(vertrag_id)}</h1>
      <p class="muted">AUSDRÜCKLICH eine reine Berechnungsvorschau - keine Vorschreibung, keine Freigabe,
         kein Versand. Jede Vorschau ist eine neue, unveränderliche Version. Aktuelle Rechtsordnung
         (wirksam laut Vertragsprüfung): <strong>{h(vertrag.rechtsordnung)}</strong>.</p>
    </div>

    <div class="card">
      <h2>Neue Vorschau erstellen</h2>
      <form method="post" action="/backoffice/vertrag/{h(vertrag_id)}/mieweg-vorschau/erstellen">
        {csrf_feld(session.csrf_token)}
        <fieldset>
          <legend>Rechtsprofil (explizit, keine automatische Einstufung)</legend>
          <label>Rechtsordnung</label>
          <select name="rechtsordnung" required>{rechtsordnung_optionen}</select>
          <label><input type="checkbox" name="ist_wohnungsnutzung" value="1"> Wohnungsnutzung bestätigt
            (MieWeG §1 Abs1 gilt nur für Wohnungen - ohne Bestätigung kein Wohnungsrechner-Fall, auch
            nicht bei MRG-Vollanwendung/-Teilanwendung eines Geschäftsraums)</label><br>
          <label><input type="checkbox" name="mrg_zinsbeschraenkung" value="1"> MRG-Zinsbeschränkung
            (Richtwert-/Kategoriemiete) - nur bei MRG-Vollanwendung, aktiviert den Übergangsdeckel
            2025 (1%)/2026 (2%)</label><br>
          <label><input type="checkbox" name="ist_altvertrag" value="1"> Altvertrag (Bezugsmonat statt
            Abschlussdatum, erste Modellbewertung frühestens April 2026)</label>
        </fieldset>
        <fieldset>
          <legend>Gesetzliche Spur - Eingaben</legend>
          <p class="muted">Bei Altvertrag: Bezugsjahr/-monat des tatsächlich ZULETZT verwendeten
             Indexwertes, NICHT das Mietbeginn-/Erhöhungsschreiben-Datum.</p>
          <label>Bezugsjahr (Abschlussjahr bzw. Jahr des zuletzt verwendeten Indexwertes)</label>
          <input type="number" name="bezugsjahr" min="1990" max="2100">
          <label>Bezugsmonat (1-12)</label>
          <input type="number" name="bezugsmonat" min="1" max="12">
          <label><input type="checkbox" name="letzte_basis_war_jahresdurchschnitt" value="1"> Die
            bisherige Basis war selbst ein Jahresdurchschnitt (statt eines Einzelmonats) - wird dann wie
            Bezugsmonat Dezember behandelt</label>
          <label>Ziel-Bewertungsjahr (1. April dieses Jahres)</label>
          <input type="number" name="ziel_bewertungsjahr" min="1990" max="2100">
          <label>Basisbetrag (EUR, BRUTTO, letzter unveränderter Betrag)</label>
          <input type="text" name="basis_betrag" placeholder="Betrag EUR">
          <label>Einbezogene, explizit indexierte Komponenten</label>
          {komponenten_html}
          <label>VPI-2020-Jahresdurchschnittswerte (eine Zeile je Jahr: JAHR;WERT;QUELLE;DATUM)</label>
          <textarea name="vpi_zeilen" rows="4" placeholder="2023;100.0;Statistik Austria VPI 2020;2026-01-15"></textarea>
        </fieldset>
        <fieldset>
          <legend>Vertragsspur - manuell geprüfter, vertraglich zulässiger Betrag</legend>
          <p class="muted">Wird NICHT aus den VPI-Daten hergeleitet - die individuelle Vertragsklausel
             bleibt Fachprüfung. Ohne Betrag bleibt diese Spur offen (Prüfbedarf).</p>
          <label>Vertraglich zulässiger Betrag (EUR, BRUTTO)</label>
          <input type="text" name="vertraglicher_betrag" placeholder="Betrag EUR">
          <label>Quellenbeleg (Pflicht, sobald ein Betrag erfasst wird)</label>
          <input type="text" name="vertraglicher_quellenbeleg" placeholder="z. B. Mietvertrag-2024.pdf, Wertsicherungsklausel">
          <label>Vertraglich frühestmöglicher Termin</label>
          <input type="date" name="vertraglicher_termin">
        </fieldset>
        <fieldset>
          <legend>Aktuell verrechneter Betrag - GETRENNT vom historischen Basisbetrag</legend>
          <p class="muted">Der historische Basisbetrag oben ist NICHT automatisch der heute tatsächlich
             verrechnete Betrag - zwischen dem historischen Bezugszeitpunkt und heute können bereits
             (teilweise) Erhöhungen umgesetzt worden sein. Ohne diesen Vergleichswert (mit Datum/Beleg)
             wird KEINE ausführbare Erhöhung ausgewiesen, nur die gesetzliche/vertragliche Obergrenze -
             sonst würde ein bereits verrechneter Teil ein zweites Mal aufgeschlagen.</p>
          <label>Aktuell verrechneter Betrag (EUR, BRUTTO - dieselbe Grundlage wie alle Beträge oben)</label>
          <input type="text" name="aktuell_verrechneter_betrag" placeholder="Betrag EUR">
          <label>Quellenbeleg (Pflicht, sobald ein Betrag erfasst wird)</label>
          <input type="text" name="aktuell_verrechnet_quellenbeleg" placeholder="z. B. Vorschreibung 2026-03.pdf">
          <label>Stichtag des aktuell verrechneten Betrags</label>
          <input type="date" name="aktuell_verrechnet_stichtag">
        </fieldset>
        <fieldset>
          <legend>Nachweise</legend>
          <label>Zustellnachweis-Referenz (ohne diese bleibt jedes Ergebnis reine Vorschau)</label>
          <input type="text" name="zustellnachweis_referenz">
          <label>Kommentar</label>
          <input type="text" name="kommentar">
        </fieldset>
        <button type="submit">Vorschau berechnen und speichern</button>
      </form>
    </div>

    <div class="card">
      <h2>Historie</h2>
      <table>
        <tr>
          <th>Version</th><th>Rechtsordnung</th><th>Ziel-Jahr</th><th>Gesetzliche Grenze</th>
          <th>Vertragsspur</th><th>Maßgeblich</th><th>Aktuell verrechnet</th><th>Ausführbare Erhöhung</th>
          <th>Frühester Termin</th><th>Status</th>
          <th>Offene Nachweise</th><th>Von</th><th>Am</th>
        </tr>
        {historie_html}
      </table>
    </div>
    <p><a href="/backoffice/vertrag/{h(vertrag_id)}/pruefung">&larr; zur Vertragsprüfung</a></p>
    """
    return _layout(request, session, "MieWeG-Vorschau", inhalt)


@router.post("/vertrag/{vertrag_id}/mieweg-vorschau/erstellen")
def mieweg_vorschau_erstellen(
    request: Request,
    vertrag_id: str,
    rechtsordnung: str = Form(...),
    ist_wohnungsnutzung: str = Form(""),
    mrg_zinsbeschraenkung: str = Form(""),
    ist_altvertrag: str = Form(""),
    bezugsjahr: str = Form(""),
    bezugsmonat: str = Form(""),
    letzte_basis_war_jahresdurchschnitt: str = Form(""),
    ziel_bewertungsjahr: str = Form(""),
    basis_betrag: str = Form(""),
    basis_komponenten_ids: list[str] = Form([]),
    vpi_zeilen: str = Form(""),
    vertraglicher_betrag: str = Form(""),
    vertraglicher_quellenbeleg: str = Form(""),
    vertraglicher_termin: str = Form(""),
    aktuell_verrechneter_betrag: str = Form(""),
    aktuell_verrechnet_quellenbeleg: str = Form(""),
    aktuell_verrechnet_stichtag: str = Form(""),
    zustellnachweis_referenz: str = Form(""),
    kommentar: str = Form(""),
    csrf_token: str = Form(...),
    session=Depends(_current_session),
):
    _verify_csrf(session, csrf_token)
    vertrag = deps._stammdaten_repo.get_vertrag(vertrag_id)
    if vertrag is None:
        return _fehlerseite(session, "MieWeG-Vorschau", f"Unbekannter Vertrag {vertrag_id}.")
    try:
        basis_betrag_cent = parse_eur_betrag(basis_betrag) if (basis_betrag or "").strip() else None
        vertraglich_zulaessiger_betrag_cent = (
            parse_eur_betrag(vertraglicher_betrag) if (vertraglicher_betrag or "").strip() else None
        )
        vpi_jahresdurchschnitte = _parse_vpi_zeilen(vpi_zeilen)
        vertraglicher_fruehestmoeglicher_termin = (
            date.fromisoformat(vertraglicher_termin) if (vertraglicher_termin or "").strip() else None
        )
        aktuell_verrechneter_betrag_cent = (
            parse_eur_betrag(aktuell_verrechneter_betrag) if (aktuell_verrechneter_betrag or "").strip() else None
        )
        aktuell_verrechnet_stichtag_datum = (
            date.fromisoformat(aktuell_verrechnet_stichtag) if (aktuell_verrechnet_stichtag or "").strip() else None
        )
        deps._mieweg_vorschau_service.vorschau_erstellen(
            ctx=_ctx(session), vertrag=vertrag, rechtsordnung=rechtsordnung,
            ist_wohnungsnutzung=bool(ist_wohnungsnutzung),
            mrg_zinsbeschraenkung=bool(mrg_zinsbeschraenkung), ist_altvertrag=bool(ist_altvertrag),
            bezugsjahr=int(bezugsjahr) if (bezugsjahr or "").strip() else None,
            bezugsmonat=int(bezugsmonat) if (bezugsmonat or "").strip() else None,
            letzte_basis_war_jahresdurchschnitt=bool(letzte_basis_war_jahresdurchschnitt),
            ziel_bewertungsjahr=int(ziel_bewertungsjahr) if (ziel_bewertungsjahr or "").strip() else None,
            basis_betrag_cent=basis_betrag_cent, basis_komponenten_ids=basis_komponenten_ids,
            vpi_jahresdurchschnitte=vpi_jahresdurchschnitte,
            vertraglich_zulaessiger_betrag_cent=vertraglich_zulaessiger_betrag_cent,
            vertraglicher_quellenbeleg=vertraglicher_quellenbeleg or None,
            vertraglicher_fruehestmoeglicher_termin=vertraglicher_fruehestmoeglicher_termin,
            aktuell_verrechneter_betrag_cent=aktuell_verrechneter_betrag_cent,
            aktuell_verrechnet_quellenbeleg=aktuell_verrechnet_quellenbeleg or None,
            aktuell_verrechnet_stichtag=aktuell_verrechnet_stichtag_datum,
            zustellnachweis_referenz=zustellnachweis_referenz or None, kommentar=kommentar or None,
            akteur=session.user_id,
        )
        deps._audit_service.log(
            entity_typ="mieweg_vorschau", entity_id=vertrag_id, aktion="erstellt", akteur=session.user_id,
            payload={"rechtsordnung": rechtsordnung, "ziel_bewertungsjahr": ziel_bewertungsjahr},
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "MieWeG-Vorschau", str(exc), f"/backoffice/vertrag/{vertrag_id}/mieweg-vorschau")
    return RedirectResponse(url=f"/backoffice/vertrag/{vertrag_id}/mieweg-vorschau", status_code=303)
