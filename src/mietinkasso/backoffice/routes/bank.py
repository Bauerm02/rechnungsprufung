"""Bankdatei-Import, Zuordnung und Bankvollständigkeit.

Umfasst auch die Verknüpfung einer Rohtransaktion mit einer BEREITS
gebuchten Zahlung (stand bisher weiter unten in der Sammeldatei, gehört
fachlich hierher). Die automatische Zuordnung bleibt außerhalb bekannter
Demo-Umgebungen serverseitig gesperrt (`deps._DEMO_UMGEBUNG`)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from html import escape as h

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from mietinkasso.backoffice.views import csrf_feld, eur, flash_ok, option, parse_eur_betrag
from mietinkasso.bank.importer import (
    CamtKontoMismatchError,
    CamtMehrteiligeBuchungError,
    CamtUnvollstaendigError,
    CsvSpaltenMapping,
    parse_camt053,
    parse_csv,
)
from mietinkasso.bank.service import (
    KATEGORIE_EINGANG_PRUEFEN,
    KATEGORIE_RUECKLASTSCHRIFT_KLAERFALL,
    KATEGORIE_UMBUCHUNG,
    kategorisiere_bewegung,
)
from mietinkasso.domain.exceptions import MietinkassoError

from mietinkasso.backoffice import dependencies as deps
from mietinkasso.backoffice.auth import _ctx, _current_session, _fehlerseite, _layout, _verify_csrf


#: Ohne eigenes Prefix - das gemeinsame `/backoffice`-Prefix wird GENAU
#: EINMAL in `backoffice/app.py` gesetzt.
router = APIRouter()


# -- Bankimport + Zuordnung ------------------------------------------------------


@router.get("/bank", response_class=HTMLResponse)
def bank_formular(request: Request, session=Depends(_current_session)) -> HTMLResponse:
    bank_konten = deps._bank_repo.list_bank_konten()
    options = "".join(option(bk.id, f"{bk.bezeichnung} ({bk.gesellschaft_id}, {bk.iban})") for bk in bank_konten)
    inhalt = f"""
    <div class="card" style="max-width:640px;">
      <h1>Bankdatei-Import</h1>
      <form method="post" action="/backoffice/bank/vorschau" enctype="multipart/form-data">
        {csrf_feld(session.csrf_token)}
        <label>Bankkonto</label>
        <select name="bank_konto_id" required>{options}</select>
        <label>Format</label>
        <select name="format" required>
          <option value="CSV">CSV</option>
          <option value="CAMT">CAMT.053 (XML)</option>
        </select>
        <label>Datei</label>
        <input type="file" name="datei" required>
        <fieldset>
          <legend>CSV-Spaltennamen (nur relevant bei Format CSV)</legend>
          <label>Betrag-Spalte</label><input type="text" name="spalte_betrag" value="betrag">
          <label>Datum-Spalte</label><input type="text" name="spalte_datum" value="datum">
          <label>Referenz-Spalte</label><input type="text" name="spalte_referenz" value="referenz">
          <label>Eindeutige-Referenz-Spalte (optional)</label><input type="text" name="spalte_eindeutig" value="">
          <label>Dezimaltrennzeichen</label>
          <select name="dezimaltrennzeichen"><option value=".">Punkt (1234.56)</option><option value=",">Komma (1234,56)</option></select>
        </fieldset>
        <button type="submit">Vorschau anzeigen</button>
      </form>
    </div>
    <p><a href="/backoffice/bank/unzugeordnet">Offene Zuordnungen ansehen</a> &nbsp;|&nbsp;
       <a href="/backoffice/bank/vollstaendigkeit">Bankvollständigkeit bestätigen</a></p>"""
    return _layout(request, session, "Bankdatei-Import", inhalt)


def _bank_rohdaten_parsen(*, format_: str, inhalt_bytes: bytes, mapping_felder: dict, bank_konto_iban: str) -> list:
    if format_ == "CAMT":
        return parse_camt053(inhalt_bytes, erwartete_iban=bank_konto_iban)
    mapping = CsvSpaltenMapping(
        betrag=mapping_felder["spalte_betrag"], buchungsdatum=mapping_felder["spalte_datum"],
        referenz=mapping_felder["spalte_referenz"] or None,
        eindeutige_referenz=mapping_felder["spalte_eindeutig"] or None,
        dezimaltrennzeichen=mapping_felder["dezimaltrennzeichen"],
    )
    return parse_csv(inhalt_bytes.decode("utf-8-sig"), mapping)


@router.post("/bank/vorschau", response_class=HTMLResponse)
async def bank_vorschau(
    request: Request,
    bank_konto_id: str = Form(...),
    format: str = Form(...),
    datei: UploadFile = File(...),
    spalte_betrag: str = Form("betrag"),
    spalte_datum: str = Form("datum"),
    spalte_referenz: str = Form("referenz"),
    spalte_eindeutig: str = Form(""),
    dezimaltrennzeichen: str = Form("."),
    csrf_token: str = Form(...),
    session=Depends(_current_session),
) -> HTMLResponse:
    _verify_csrf(session, csrf_token)
    bank_konto = deps._bank_repo.get_bank_konto(bank_konto_id)
    if bank_konto is None:
        return _fehlerseite(session, "Bankvorschau", f"Unbekanntes Bankkonto {bank_konto_id}.", "/backoffice/bank")
    inhalt_bytes = await datei.read()
    mapping_felder = {
        "spalte_betrag": spalte_betrag, "spalte_datum": spalte_datum, "spalte_referenz": spalte_referenz,
        "spalte_eindeutig": spalte_eindeutig, "dezimaltrennzeichen": dezimaltrennzeichen,
    }
    try:
        rohdaten = _bank_rohdaten_parsen(
            format_=format, inhalt_bytes=inhalt_bytes, mapping_felder=mapping_felder, bank_konto_iban=bank_konto.iban,
        )
    except (MietinkassoError, ValueError, CamtUnvollstaendigError, CamtMehrteiligeBuchungError, CamtKontoMismatchError, KeyError) as exc:
        return _fehlerseite(session, "Bankvorschau", f"Datei nicht importierbar: {exc}", "/backoffice/bank")
    if not rohdaten:
        return _fehlerseite(session, "Bankvorschau", "Datei enthält keine Zeilen.", "/backoffice/bank")

    zeitraum_von = min(r.buchungsdatum for r in rohdaten)
    zeitraum_bis = max(r.buchungsdatum for r in rohdaten)
    vorschlaege = deps._bank_service.vorschau_zuordnungsvorschlaege(rohdaten)

    zeilen_html = []
    unklare = 0
    for eintrag in vorschlaege:
        roh = eintrag.roh
        unklar = eintrag.vorgeschlagenes_konto_id is None and roh.betrag_cent > 0
        if unklar:
            unklare += 1
        klasse = "gesperrt-row" if unklar else ""
        zeilen_html.append(
            f"<tr class='{klasse}'>"
            f"<td>{eur(roh.betrag_cent)}</td><td>{roh.buchungsdatum.isoformat()}</td>"
            f"<td>{h(roh.referenz or '')}</td><td>{'ja' if roh.native_id else 'nein'}</td>"
            f"<td>{h(eintrag.vorgeschlagenes_konto_id or '-')}</td><td>{h(eintrag.grund)}</td></tr>"
        )

    import base64

    versteckte_datei = base64.b64encode(inhalt_bytes).decode("ascii")
    inhalt = f"""
    <div class="card">
      <h1>Vorschau — Bankdatei</h1>
      <table>
        <tr><th>Bankkonto</th><td>{h(bank_konto.bezeichnung)} ({h(bank_konto.gesellschaft_id)}, {h(bank_konto.iban)})</td></tr>
        <tr><th>Zeitraum</th><td>{zeitraum_von.isoformat()} bis {zeitraum_bis.isoformat()}</td></tr>
        <tr><th>Zeilen</th><td>{len(rohdaten)} (davon {unklare} ohne eindeutigen Zuordnungsvorschlag)</td></tr>
      </table>
      <table>
        <tr><th>Betrag</th><th>Datum</th><th>Referenz</th><th>eindeutige ID?</th><th>Vorschlag Konto</th><th>Begründung</th></tr>
        {''.join(zeilen_html)}
      </table>
      <p class="muted">Dieser Import legt nur die Bankbewegungen ab; die eigentliche Zuordnung gegen ein
         Mietkonto erfolgt danach als separater, ausdrücklicher Schritt je Transaktion.</p>
      <form method="post" action="/backoffice/bank/importieren">
        {csrf_feld(session.csrf_token)}
        <input type="hidden" name="bank_konto_id" value="{h(bank_konto_id)}">
        <input type="hidden" name="format" value="{h(format)}">
        <input type="hidden" name="inhalt_b64" value="{versteckte_datei}">
        <input type="hidden" name="spalte_betrag" value="{h(spalte_betrag)}">
        <input type="hidden" name="spalte_datum" value="{h(spalte_datum)}">
        <input type="hidden" name="spalte_referenz" value="{h(spalte_referenz)}">
        <input type="hidden" name="spalte_eindeutig" value="{h(spalte_eindeutig)}">
        <input type="hidden" name="dezimaltrennzeichen" value="{h(dezimaltrennzeichen)}">
        <button type="submit">Datei importieren (Bankbewegungen ablegen)</button>
      </form>
      <p><a href="/backoffice/bank">&larr; andere Datei wählen</a></p>
    </div>"""
    return _layout(request, session, "Vorschau Bankdatei", inhalt)


@router.post("/bank/importieren", response_class=HTMLResponse)
def bank_importieren(
    request: Request,
    bank_konto_id: str = Form(...),
    format: str = Form(...),
    inhalt_b64: str = Form(...),
    spalte_betrag: str = Form("betrag"),
    spalte_datum: str = Form("datum"),
    spalte_referenz: str = Form("referenz"),
    spalte_eindeutig: str = Form(""),
    dezimaltrennzeichen: str = Form("."),
    csrf_token: str = Form(...),
    session=Depends(_current_session),
) -> HTMLResponse:
    _verify_csrf(session, csrf_token)
    import base64

    bank_konto = deps._bank_repo.get_bank_konto(bank_konto_id)
    if bank_konto is None:
        return _fehlerseite(session, "Bankimport", f"Unbekanntes Bankkonto {bank_konto_id}.", "/backoffice/bank")
    inhalt_bytes = base64.b64decode(inhalt_b64)
    try:
        if format == "CAMT":
            transaktionen = deps._bank_service.importiere_camt053(ctx=_ctx(session), bank_konto=bank_konto, xml_bytes=inhalt_bytes)
        else:
            mapping = CsvSpaltenMapping(
                betrag=spalte_betrag, buchungsdatum=spalte_datum, referenz=spalte_referenz or None,
                eindeutige_referenz=spalte_eindeutig or None, dezimaltrennzeichen=dezimaltrennzeichen,
            )
            transaktionen = deps._bank_service.importiere_csv(
                ctx=_ctx(session), bank_konto=bank_konto, text=inhalt_bytes.decode("utf-8-sig"), mapping=mapping,
            )
        deps._audit_service.log(
            entity_typ="bank_import", entity_id=bank_konto_id, aktion="importiert", akteur=session.user_id,
            payload={"anzahl": len(transaktionen), "format": format},
        )
    except (MietinkassoError, ValueError, CamtUnvollstaendigError, CamtMehrteiligeBuchungError, CamtKontoMismatchError) as exc:
        return _fehlerseite(session, "Bankimport", f"Import abgebrochen, NICHTS wurde übernommen: {exc}", "/backoffice/bank")
    inhalt = (
        flash_ok(f"{len(transaktionen)} Bankbewegung(en) importiert (noch nicht zugeordnet).")
        + f'<p><a href="/backoffice/bank/unzugeordnet?bank_konto_id={h(bank_konto_id)}">Jetzt zuordnen &rarr;</a></p>'
    )
    return _layout(request, session, "Bankimport erfolgreich", inhalt)


def _bank_tx_details_html(tx: object) -> str:
    """Technische IDs/Vorgangsdetails eingeklappt - für die Fachprüfung
    zählen Referenz/Datum/Betrag/Richtung, nicht die interne ID."""

    return (
        '<details class="tx-details"><summary>Details</summary>'
        f'<p class="muted">Transaktions-ID #{tx.id} &middot; '
        f'Gegenkonto {h(tx.gegenkonto_name or "-")} ({h(tx.gegenkonto_iban or "-")})</p></details>'
    )


def _bank_konten_select_optionen(gesellschaft_id: str) -> str:
    """Lesbare Konto-Auswahl bevorzugt Objekt/Einheit statt der reinen
    Konto-ID; dieselbe Ausschlussprüfung wie bisher (Pilotausschluss
    bleibt unverändert erhalten)."""

    optionen = []
    for objekt in deps._stammdaten_repo.list_objekte(gesellschaft_id=gesellschaft_id):
        if objekt.ausgeschlossen:
            continue
        for vertrag in deps._stammdaten_repo.list_vertraege_fuer_objekt(objekt.id):
            konto = deps._stammdaten_repo.get_konto_by_vertrag(vertrag.id)
            if konto is None:
                continue
            einheit = deps._stammdaten_repo.get_einheit(vertrag.einheit_id)
            einheit_label = f" / {einheit.bezeichnung}" if einheit else ""
            optionen.append(option(konto.id, f"{objekt.bezeichnung}{einheit_label} ({konto.id})"))
    return "".join(optionen)


@router.get("/bank/unzugeordnet", response_class=HTMLResponse)
def bank_unzugeordnet(request: Request, bank_konto_id: str | None = None, session=Depends(_current_session)) -> HTMLResponse:
    bank_konten = deps._bank_repo.list_bank_konten()
    options = "".join(option(bk.id, bk.bezeichnung, selected=(bk.id == bank_konto_id)) for bk in bank_konten)
    auswahl = f"""
    <form method="get" action="/backoffice/bank/unzugeordnet">
      <label>Bankkonto</label>
      <select name="bank_konto_id" onchange="this.form.submit()">
        <option value="">-- wählen --</option>{options}
      </select>
      <noscript><button type="submit">Anzeigen</button></noscript>
    </form>"""

    bereiche = ""
    if bank_konto_id:
        bank_konto = deps._bank_repo.get_bank_konto(bank_konto_id)
        if bank_konto is None:
            # Unbekanntes/leeres Bankkonto darf nie einen 500 erzeugen -
            # nur ein ruhiger Hinweis, die Auswahl bleibt bedienbar.
            bereiche = '<div class="card"><p class="warn">Unbekanntes Bankkonto.</p></div>'
        else:
            transaktionen = deps._bank_repo.list_unzugeordnet(bank_konto_id)
            konten_select = _bank_konten_select_optionen(bank_konto.gesellschaft_id)

            eingaenge: list[tuple[object, str]] = []
            klaerfaelle: list[tuple[object, str]] = []
            umbuchungen: list[tuple[object, str]] = []
            ausgaenge: list[tuple[object, str]] = []
            for tx in transaktionen:
                kategorie, begruendung = kategorisiere_bewegung(tx)
                if kategorie == KATEGORIE_EINGANG_PRUEFEN:
                    eingaenge.append((tx, begruendung))
                elif kategorie == KATEGORIE_RUECKLASTSCHRIFT_KLAERFALL:
                    klaerfaelle.append((tx, begruendung))
                elif kategorie == KATEGORIE_UMBUCHUNG:
                    umbuchungen.append((tx, begruendung))
                else:
                    ausgaenge.append((tx, begruendung))

            def _eingang_zeile(tx: object, begruendung: str) -> str:
                zugeordnet = deps._bank_repo.zugeordneter_betrag(tx.id)
                rest = tx.betrag_cent - zugeordnet
                vorschlag_td = ""
                # Automatische Zuordnung ist nutzerseitig zurückgestellt (bis
                # EBS/EBICS) - außerhalb bekannter Demo-Umgebungen weder
                # Vorschlagstext noch Schaltfläche anzeigen (Hinweis dazu
                # steht EINMAL oberhalb der Tabelle, nicht je Zeile). Die
                # Anzeige allein wäre KEIN Schutz - die POST-Route selbst
                # verweigert die Ausführung ebenfalls (siehe
                # bank_automatisch_zuordnen).
                if deps._DEMO_UMGEBUNG:
                    vorschlag_konto, vorschlag_grund = deps._bank_service.schlage_konto_vor(tx)
                    vorschlag_html = h(vorschlag_grund)
                    if vorschlag_konto is not None:
                        vorschlag_html += f"""
                        <form method="post" action="/backoffice/bank/{tx.id}/automatisch-zuordnen" class="inline">
                          {csrf_feld(session.csrf_token)}
                          <button type="submit">Vorschlag übernehmen ({h(vorschlag_konto.id)})</button>
                        </form>"""
                    vorschlag_td = f"<td>{vorschlag_html}</td>"
                return f"""
                <tr>
                  <td class="nowrap">{eur(tx.betrag_cent)}</td><td class="nowrap">{tx.buchungsdatum.isoformat()}</td>
                  <td class="tx-referenz">{h(tx.referenz or '')}</td><td class="nowrap">{eur(rest)} offen</td>
                  <td>{h(begruendung)}</td>
                  {vorschlag_td}
                  <td class="bank-aktion">
                    <a class="btn-verknuepfen" href="/backoffice/bank/{tx.id}/verknuepfen">Bestehende Zahlung verknüpfen</a>
                    <details class="tx-manuell"><summary>Manuell zuordnen</summary>
                      <form method="post" action="/backoffice/bank/{tx.id}/manuell-zuordnen">
                        {csrf_feld(session.csrf_token)}
                        <select name="konto_id" required><option value="">Konto wählen</option>{konten_select}</select>
                        <input type="text" name="betrag" placeholder="Betrag EUR" value="{eur(rest).split()[0]}" required>
                        <input type="text" name="vorgangs_id" placeholder="Vorgangs-ID" value="MANUELL-{tx.id}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}" required>
                        <button type="submit">Manuell zuordnen</button>
                      </form>
                    </details>
                    {_bank_tx_details_html(tx)}
                  </td>
                </tr>"""

            def _klaerfall_zeile(tx: object, begruendung: str) -> str:
                # Bewusst KEIN normales Zuordnungsformular/Vorschlag bei
                # negativen (oder Null-)Beträgen - nur ein Prüffall-Hinweis,
                # keine neue Rücklastschrift-Buchung wird hier implementiert.
                # Codex-Rückprüfung db3755a: der ursprüngliche Bankbetrag
                # allein reichte nicht - bereits verarbeitete Teil-
                # Rücklastschriften und der verbleibende Prüfrest müssen
                # DIREKT sichtbar sein, nicht nur implizit im Repository.
                urspruenglich = abs(tx.betrag_cent)
                verarbeitet = deps._bank_repo.verwendeter_betrag_rueckbuchung(tx.id)
                rest = urspruenglich - verarbeitet
                status_html = '<span class="badge badge-warn">Prüffall</span>'
                if verarbeitet > 0:
                    status_html += (
                        f'<p class="muted">verarbeitet {eur(verarbeitet)} &middot; Prüfrest {eur(rest)} '
                        f'(von {eur(urspruenglich)})</p>'
                    )
                return f"""
                <tr>
                  <td class="nowrap">{eur(tx.betrag_cent)}</td><td class="nowrap">{tx.buchungsdatum.isoformat()}</td>
                  <td class="tx-referenz">{h(tx.referenz or '')}</td>
                  <td>{h(begruendung)}</td>
                  <td>{status_html}</td>
                  <td>{_bank_tx_details_html(tx)}</td>
                </tr>"""

            def _sonstige_zeile(tx: object, begruendung: str, *, manuelle_optionen: bool) -> str:
                rest_html = ""
                manuell_html = ""
                if manuelle_optionen:
                    zugeordnet = deps._bank_repo.zugeordneter_betrag(tx.id)
                    rest = tx.betrag_cent - zugeordnet
                    # Codex-Rückprüfung db3755a: der Teilzuordnungsrest muss
                    # DIREKT in der Zeile sichtbar sein, nicht nur als
                    # vorbefülltes Formularfeld im eingeklappten Bereich.
                    rest_html = f'<p class="muted">offen {eur(rest)} (von {eur(tx.betrag_cent)})</p>'
                    manuell_html = f"""
                    <details class="tx-manuell"><summary>Manuelle Optionen</summary>
                      <a class="btn-verknuepfen" href="/backoffice/bank/{tx.id}/verknuepfen">Bestehende Zahlung verknüpfen</a>
                      <form method="post" action="/backoffice/bank/{tx.id}/manuell-zuordnen">
                        {csrf_feld(session.csrf_token)}
                        <select name="konto_id" required><option value="">Konto wählen</option>{konten_select}</select>
                        <input type="text" name="betrag" placeholder="Betrag EUR" value="{eur(rest).split()[0]}" required>
                        <input type="text" name="vorgangs_id" placeholder="Vorgangs-ID" value="MANUELL-{tx.id}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}" required>
                        <button type="submit">Manuell zuordnen</button>
                      </form>
                    </details>"""
                return f"""
                <tr>
                  <td class="nowrap">{eur(tx.betrag_cent)}</td><td class="nowrap">{tx.buchungsdatum.isoformat()}</td>
                  <td class="tx-referenz">{h(tx.referenz or '')}</td>
                  <td>{h(begruendung)}</td>
                  <td class="bank-aktion">{rest_html}{manuell_html}{_bank_tx_details_html(tx)}</td>
                </tr>"""

            def _summe(zeilen: list[tuple[object, str]]) -> int:
                return sum(tx.betrag_cent for tx, _ in zeilen)

            eingaenge_colspan = 7 if deps._DEMO_UMGEBUNG else 6
            eingaenge_html = "".join(_eingang_zeile(tx, b) for tx, b in eingaenge) or (
                f'<tr><td colspan="{eingaenge_colspan}" class="muted">Keine offenen Eingänge.</td></tr>'
            )
            klaerfaelle_html = "".join(_klaerfall_zeile(tx, b) for tx, b in klaerfaelle) or (
                '<tr><td colspan="6" class="muted">Keine Rücklastschriften/Klärfälle.</td></tr>'
            )
            umbuchungen_html = "".join(
                _sonstige_zeile(tx, b, manuelle_optionen=(tx.betrag_cent > 0)) for tx, b in umbuchungen
            ) or '<tr><td colspan="5" class="muted">Keine Umbuchungen.</td></tr>'
            ausgaenge_html = "".join(
                _sonstige_zeile(tx, b, manuelle_optionen=False) for tx, b in ausgaenge
            ) or '<tr><td colspan="5" class="muted">Keine Ausgänge.</td></tr>'

            eingang_vorschlag_th = "<th>Vorschlag</th>" if deps._DEMO_UMGEBUNG else ""
            eingang_ebs_hinweis = (
                "" if deps._DEMO_UMGEBUNG
                else '<p class="muted">Automatik zurückgestellt (EBS/EBICS ausstehend).</p>'
            )

            bereiche = f"""
            <div class="card">
              <h2>Eingänge / Mietzahlungen prüfen ({len(eingaenge)})</h2>
              <p class="muted">Summe (Bankbeträge): {eur(_summe(eingaenge))} &middot; kein Mieter-Offener-Posten &middot;
                 "Bestehende Zahlung verknüpfen" vor manueller Neuzuordnung prüfen (vermeidet Doppelbuchung).</p>
              {eingang_ebs_hinweis}
              <div class="tabelle-scroll"><table class="bank-tabelle">
                <tr><th class="nowrap">Betrag</th><th class="nowrap">Datum</th><th>Referenz</th>
                    <th class="nowrap">Offen</th><th>Hinweis</th>{eingang_vorschlag_th}<th>Aktion</th></tr>
                {eingaenge_html}
              </table></div>
            </div>
            <div class="card klaerfall-card">
              <h2>Rücklastschriften / Klärfälle ({len(klaerfaelle)})</h2>
              <p class="muted">Summe (Bankbeträge): {eur(_summe(klaerfaelle))} &middot; Prüffälle, kein
                 Zuordnungsformular &middot; kein Mieter-Offener-Posten &middot; hebt keine gültige Mahnsperre auf.</p>
              <div class="tabelle-scroll"><table class="bank-tabelle">
                <tr><th class="nowrap">Betrag</th><th class="nowrap">Datum</th><th>Referenz</th><th>Hinweis</th><th>Status</th><th></th></tr>
                {klaerfaelle_html}
              </table></div>
            </div>
            <details class="card">
              <summary>Umbuchungen ({len(umbuchungen)}) &mdash; Hinweis aus Banktext</summary>
              <p class="muted">Summe (Bankbeträge): {eur(_summe(umbuchungen))} &middot; reiner Anzeigehinweis
                 &middot; kein Mieter-Offener-Posten.</p>
              <div class="tabelle-scroll"><table class="bank-tabelle">
                <tr><th class="nowrap">Betrag</th><th class="nowrap">Datum</th><th>Referenz</th><th>Hinweis</th><th></th></tr>
                {umbuchungen_html}
              </table></div>
            </details>
            <details class="card">
              <summary>Ausgänge / Betriebsausgaben ({len(ausgaenge)}) &mdash; Hinweis aus Banktext</summary>
              <p class="muted">Summe (Bankbeträge): {eur(_summe(ausgaenge))} &middot; reiner Anzeigehinweis
                 &middot; kein Mieter-Offener-Posten.</p>
              <div class="tabelle-scroll"><table class="bank-tabelle">
                <tr><th class="nowrap">Betrag</th><th class="nowrap">Datum</th><th>Referenz</th><th>Hinweis</th><th></th></tr>
                {ausgaenge_html}
              </table></div>
            </details>"""

    inhalt = f'<div class="card"><h1>Bankbewegungen prüfen</h1>{auswahl}</div>{bereiche}'
    return _layout(request, session, "Bankbewegungen prüfen", inhalt)


@router.post("/bank/{transaktion_id}/automatisch-zuordnen")
def bank_automatisch_zuordnen(request: Request, transaktion_id: int, csrf_token: str = Form(...), session=Depends(_current_session)):
    _verify_csrf(session, csrf_token)
    if not deps._DEMO_UMGEBUNG:
        # Nutzerseitig zurückgestellt (bis EBS/EBICS) - die Route führt in
        # jeder NICHT bekannten Demo-Umgebung NICHTS aus, unabhängig davon,
        # ob im UI eine Schaltfläche dafür sichtbar war (die Anzeige allein
        # wäre kein Schutz gegen einen direkten POST).
        raise HTTPException(
            status_code=403,
            detail="Automatische Bankzuordnung ist zurückgestellt (EBS/EBICS ausstehend) und in dieser "
            "Umgebung deaktiviert.",
        )
    transaktion = deps._bank_repo.get_transaktion(transaktion_id)
    if transaktion is None:
        return _fehlerseite(session, "Zuordnung", f"Unbekannte Transaktion {transaktion_id}.", "/backoffice/bank/unzugeordnet")
    try:
        ergebnis = deps._bank_service.automatisch_zuordnen(ctx=_ctx(session), transaktion=transaktion)
        if not ergebnis.zugeordnet:
            return _fehlerseite(session, "Zuordnung", ergebnis.grund, f"/backoffice/bank/unzugeordnet?bank_konto_id={transaktion.bank_konto_id}")
        deps._audit_service.log(
            entity_typ="zuordnung", entity_id=str(ergebnis.zuordnung_id), aktion="automatisch_zugeordnet",
            akteur=session.user_id, payload={"transaktion_id": transaktion_id, "op_position_id": ergebnis.op_position_id},
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Zuordnung", str(exc), f"/backoffice/bank/unzugeordnet?bank_konto_id={transaktion.bank_konto_id}")
    return RedirectResponse(url=f"/backoffice/bank/unzugeordnet?bank_konto_id={transaktion.bank_konto_id}&zugeordnet=1", status_code=303)


@router.post("/bank/{transaktion_id}/manuell-zuordnen")
def bank_manuell_zuordnen(
    request: Request,
    transaktion_id: int,
    konto_id: str = Form(...),
    betrag: str = Form(...),
    vorgangs_id: str = Form(...),
    csrf_token: str = Form(...),
    session=Depends(_current_session),
):
    _verify_csrf(session, csrf_token)
    transaktion = deps._bank_repo.get_transaktion(transaktion_id)
    if transaktion is None:
        return _fehlerseite(session, "Zuordnung", f"Unbekannte Transaktion {transaktion_id}.", "/backoffice/bank/unzugeordnet")
    konto = deps._stammdaten_repo.get_konto(konto_id)
    if konto is None:
        return _fehlerseite(session, "Zuordnung", f"Unbekanntes Konto {konto_id}.", f"/backoffice/bank/unzugeordnet?bank_konto_id={transaktion.bank_konto_id}")
    try:
        betrag_cent = parse_eur_betrag(betrag)
        zuordnung = deps._bank_service.zuordnen_manuell(
            ctx=_ctx(session), transaktion=transaktion, konto=konto, betrag_cent=betrag_cent,
            beleg_referenz=f"Manuelle Zuordnung durch {session.user_id}", vorgang_id=vorgangs_id,
        )
        deps._audit_service.log(
            entity_typ="zuordnung", entity_id=str(zuordnung.id), aktion="manuell_zugeordnet", akteur=session.user_id,
            payload={"transaktion_id": transaktion_id, "konto_id": konto_id, "betrag_cent": betrag_cent, "vorgangs_id": vorgangs_id},
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Zuordnung", str(exc), f"/backoffice/bank/unzugeordnet?bank_konto_id={transaktion.bank_konto_id}")
    return RedirectResponse(url=f"/backoffice/bank/unzugeordnet?bank_konto_id={transaktion.bank_konto_id}&zugeordnet=1", status_code=303)


@router.get("/bank/vollstaendigkeit", response_class=HTMLResponse)
def bank_vollstaendigkeit_formular(request: Request, session=Depends(_current_session)) -> HTMLResponse:
    bank_konten = deps._bank_repo.list_bank_konten()
    zeilen = []
    for bk in bank_konten:
        bestaetigt_bis = deps._bank_service.bankvollstaendigkeit_bestaetigt_bis(bk.id)
        letzte_zeile_alter = deps._bank_service.bankstand_alter_tage(bk.id)
        zeilen.append(f"""
        <tr>
          <td>{h(bk.id)} ({h(bk.bezeichnung)})</td>
          <td>{bestaetigt_bis.isoformat() if bestaetigt_bis else '<span class="warn">nie bestätigt</span>'}</td>
          <td>{f'{letzte_zeile_alter} Tage' if letzte_zeile_alter is not None else '-'} <span class="muted">(nur Datumshinweis, kein Vollständigkeitsnachweis)</span></td>
          <td>
            <form method="post" action="/backoffice/bank/{h(bk.id)}/vollstaendigkeit-bestaetigen">
              {csrf_feld(session.csrf_token)}
              <input type="date" name="bestaetigt_bis" value="{date.today().isoformat()}" required>
              <button type="submit">Bestätigen: Import lückenlos bis Datum</button>
            </form>
          </td>
        </tr>""")
    inhalt = f"""
    <div class="card">
      <h1>Bankvollständigkeit</h1>
      <p class="muted">Nur eine ausdrückliche menschliche Bestätigung zählt fürs Mahnwesen - das bloße
         Vorhandensein einer aktuellen Zeile beweist keine Exportvollständigkeit.</p>
      <table><tr><th>Bankkonto</th><th>Zuletzt bestätigt bis</th><th>Letzte importierte Zeile</th><th>Neu bestätigen</th></tr>
        {''.join(zeilen)}
      </table>
    </div>"""
    return _layout(request, session, "Bankvollständigkeit", inhalt)


@router.post("/bank/{bank_konto_id}/vollstaendigkeit-bestaetigen")
def bank_vollstaendigkeit_bestaetigen(request: Request, bank_konto_id: str, bestaetigt_bis: date = Form(...), csrf_token: str = Form(...), session=Depends(_current_session)):
    _verify_csrf(session, csrf_token)
    deps._bank_service.bestaetige_bankvollstaendigkeit(bank_konto_id=bank_konto_id, bestaetigt_bis=bestaetigt_bis, bestaetigt_von=session.user_id)
    deps._audit_service.log(
        entity_typ="bank_vollstaendigkeit", entity_id=bank_konto_id, aktion="bestaetigt", akteur=session.user_id,
        payload={"bestaetigt_bis": bestaetigt_bis.isoformat()},
    )
    return RedirectResponse(url="/backoffice/bank/vollstaendigkeit?bestaetigt=1", status_code=303)


# -- Verknüpfung mit bestehender Zahlung (kein neuer Zahlungseintrag) --------------


@router.get("/bank/{transaktion_id}/verknuepfen", response_class=HTMLResponse)
def bank_verknuepfen_formular(request: Request, transaktion_id: int, session=Depends(_current_session)) -> HTMLResponse:
    transaktion = deps._bank_repo.get_transaktion(transaktion_id)
    if transaktion is None:
        return _fehlerseite(session, "Verknüpfung", f"Unbekannte Transaktion {transaktion_id}.", "/backoffice/bank/unzugeordnet")
    rest = transaktion.betrag_cent - deps._bank_repo.zugeordneter_betrag(transaktion_id)
    vorgang_vorschlag = f"VERKNUEPFT-{transaktion_id}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    inhalt = f"""
    <div class="card" style="max-width:560px;">
      <h1>Mit bestehender Zahlung verknüpfen — Transaktion #{transaktion_id}</h1>
      <p>Betrag: {eur(transaktion.betrag_cent)} &nbsp;|&nbsp; noch nicht zugeordnet: {eur(rest)}</p>
      <p class="muted">Verlinkt diese Rohtransaktion mit einer BEREITS gebuchten ZAHLUNG-Position (z. B.
         eine vor dem Bankfeed manuell erfasste Zahlung), OHNE einen zweiten Zahlungseintrag zu erzeugen -
         damit bestehende Mieterkonto-Buchungen bei diesem Rohbankimport nicht doppelt gutgeschrieben
         werden. Die OP-ID der bestehenden Zahlung steht im Kontoauszug des betroffenen Kontos
         (Spalte &quot;#&quot;).</p>
      <form method="post" action="/backoffice/bank/{transaktion_id}/verknuepfen">
        {csrf_feld(session.csrf_token)}
        <label>Bestehende ZAHLUNG-OP-ID</label>
        <input type="number" name="op_position_id" required>
        <label>Konto-ID (zur Bestätigung/Mandantenprüfung)</label>
        <input type="text" name="konto_id" required>
        <label>Verknüpfungsbetrag (EUR)</label>
        <input type="text" name="betrag" value="{eur(rest).split()[0]}" required>
        <label>Vorgangs-ID (eindeutig; ein Retry mit derselben ID ist ein sicherer No-Op)</label>
        <input type="text" name="vorgangs_id" value="{vorgang_vorschlag}" required>
        <button type="submit">Verknüpfen</button>
      </form>
    </div>"""
    return _layout(request, session, "Verknüpfung", inhalt)


@router.post("/bank/{transaktion_id}/verknuepfen")
def bank_verknuepfen(
    request: Request,
    transaktion_id: int,
    op_position_id: int = Form(...),
    konto_id: str = Form(...),
    betrag: str = Form(...),
    vorgangs_id: str = Form(...),
    csrf_token: str = Form(...),
    session=Depends(_current_session),
):
    _verify_csrf(session, csrf_token)
    transaktion = deps._bank_repo.get_transaktion(transaktion_id)
    if transaktion is None:
        return _fehlerseite(session, "Verknüpfung", f"Unbekannte Transaktion {transaktion_id}.", "/backoffice/bank/unzugeordnet")
    konto = deps._stammdaten_repo.get_konto(konto_id)
    if konto is None:
        return _fehlerseite(session, "Verknüpfung", f"Unbekanntes Konto {konto_id}.", f"/backoffice/bank/{transaktion_id}/verknuepfen")
    op_position = deps._op_service.get_position(op_position_id)
    if op_position is None:
        return _fehlerseite(session, "Verknüpfung", f"Unbekannte OP-Position {op_position_id}.", f"/backoffice/bank/{transaktion_id}/verknuepfen")
    try:
        betrag_cent = parse_eur_betrag(betrag)
        zuordnung = deps._bank_service.verknuepfe_mit_bestehender_zahlung(
            ctx=_ctx(session), transaktion=transaktion, op_position=op_position, konto=konto,
            betrag_cent=betrag_cent, vorgang_id=vorgangs_id,
        )
        deps._audit_service.log(
            entity_typ="zuordnung", entity_id=str(zuordnung.id), aktion="mit_bestehender_zahlung_verknuepft",
            akteur=session.user_id,
            payload={"transaktion_id": transaktion_id, "op_position_id": op_position_id, "betrag_cent": betrag_cent, "vorgangs_id": vorgangs_id},
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Verknüpfung", str(exc), f"/backoffice/bank/unzugeordnet?bank_konto_id={transaktion.bank_konto_id}")
    return RedirectResponse(url=f"/backoffice/bank/unzugeordnet?bank_konto_id={transaktion.bank_konto_id}&zugeordnet=1", status_code=303)
