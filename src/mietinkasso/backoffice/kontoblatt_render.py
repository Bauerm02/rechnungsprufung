"""Gemeinsame HTML-Darstellung des Kontoblatt-Journals (Auftrag
HV-20261022-EIN-KONTOBLATT) - EIN chronologisches Journal (Datum,
Buchungsart, Soll/Haben, laufender Saldo, Beleg/Forderungszuordnung) wird
hier GENAU EINMAL gerendert und von der Kontoansicht
(`backoffice/routes/konten.py::kontoauszug`) UND der Mieterakte
(`backoffice/vertragsanlage_form.py`) gleichermaßen eingebettet - keine
zweite, abweichende Tabelle an einer dieser Stellen. Reines Rendering
über dem bereits aggregierten `op/kontoblatt.py::Kontoblatt`/
`erstelle_journal(...)` - keine eigene Berechnung, kein eigener I/O.

Kurze Summen oben, aufklappbare Details je Zeile (bestehende
`MahnkostenBuchungTable`-Ledgerdetails, siehe `kontoblatt.py`-Moduldoc) -
ein langes Journal darf mehrere Bildschirmseiten lang sein, es gibt aber
bewusst KEINE separat gepflegte zweite Kosten-/Zinstabelle daneben.
Dokumentierte, nicht belastete Bankaufwände und noch nicht
kostenverarbeitete Mahnläufe bleiben als eigene "in Prüfung"-Hinweise
UNTER dem Journal sichtbar (sie sind keine OP-Zeilen und daher nicht Teil
des laufenden Saldos) - das ist KEINE zweite widersprüchliche
Gesamttabelle, sondern ein expliziter Hinweis auf (noch) nicht gebuchte
Ansätze (Auftrag Markus: "ungeprüfte Ansätze sichtbar in Prüfung/nicht im
Saldo statt 0 oder weglassen")."""

from __future__ import annotations

from datetime import datetime
from html import escape as h

from mietinkasso.backoffice.views import eur
from mietinkasso.domain.enums import OPTyp
from mietinkasso.op.kontoblatt import (
    KATEGORIE_MAHNPAUSCHALE,
    KATEGORIE_MAHNSPESEN,
    KATEGORIE_VERZUGSZINSEN,
    STATUS_LABEL,
    Kontoblatt,
    erstelle_journal,
)


def _kontoblatt_status_html(zeile) -> str:
    """Wie `konten.py::_kontoblatt_status_html` (Codex-Abnahme
    20.10.2026, P3) - `None` (keine eigene Forderung, z. B. ZAHLUNG/
    GUTSCHRIFT/KORREKTUR) wird als "—" gezeigt, nie als bezahlt/offen
    behauptet."""

    if zeile.status is None:
        return '<span class="muted">—</span>'
    warn = zeile.status in ("OFFEN", "STORNIERT")
    css = "warn" if warn else "ok" if zeile.status == "BEZAHLT" else ""
    return f'<span class="{css}">{h(STATUS_LABEL.get(zeile.status, zeile.status))}</span>'


def _segment_text_roh(s: dict) -> str:
    von, bis = s.get("von", "?"), s.get("bis", "?")
    satz = s.get("satz_prozent")
    satz_text = f"{satz} %" if satz else "Basis ungeklärt"
    quelle = s.get("quelle") or "?"
    rest_cent = s.get("rest_cent")
    basis_text = eur(rest_cent) if isinstance(rest_cent, int) else "unbekannt"
    zinsen_cent = s.get("zinsen_cent")
    zinsen_text = eur(zinsen_cent) if isinstance(zinsen_cent, int) else "unbekannt"
    return f"{von}–{bis}: {satz_text} ({quelle}) auf Basis {basis_text} → {zinsen_text} kumulativ"


def _kontoblatt_ledger_html(zeile) -> str:
    """Wie `konten.py::_kontoblatt_ledger_html` - rein lesende
    `MahnkostenBuchungTable`-Ledgerdetails je Verzugszinsen-/Mahnspesen-/
    Mahnpauschale-Zeile; fehlendes `ledger` wird ausdrücklich als
    "fehlend" gezeigt."""

    if zeile.kategorie not in (KATEGORIE_VERZUGSZINSEN, KATEGORIE_MAHNSPESEN, KATEGORIE_MAHNPAUSCHALE):
        return ""
    ledger = zeile.ledger
    if ledger is None:
        return '<div class="muted">Mahnbeleg/Ledger: fehlend</div>'

    teile = [
        f"Mahnbeleg: {h(ledger.versandnachweis_referenz)}",
        "zugrunde liegende Forderungen: " + (", ".join(f"#{i}" for i in ledger.forderung_op_position_ids) or "—"),
    ]
    html = f'<div class="muted">{" · ".join(teile)}</div>'

    if zeile.kategorie == KATEGORIE_VERZUGSZINSEN:
        if ledger.zins_nachweis_unvollstaendig:
            html += '<div class="warn">Zinsnachweis unvollständig/beschädigt - Originaldaten prüfen.</div>'
        zeilen_html = []
        for zuordnung in ledger.zins_zuordnung:
            segmente_text_roh = "; ".join(_segment_text_roh(s) for s in zuordnung.segmente) or "keine Segmente erfasst"
            delta_text_roh = (
                eur(zuordnung.delta_cent) if zuordnung.delta_cent is not None else "Zuordnung/Betrag nicht belegt"
            )
            zeile_text = (
                f"Forderung #{zuordnung.op_position_id} — kumulatives Zinsbild: {segmente_text_roh} "
                f"· tatsächlich an diesem Tag neu gebucht: {delta_text_roh}"
            )
            zeilen_html.append(f"<li>{h(zeile_text)}</li>")
        if zeilen_html:
            html += f'<ul class="muted">{"".join(zeilen_html)}</ul>'
    return html


def _journal_aktion_html(jz) -> str:
    """Stornieren/Korrigieren- und Rücklastschriftkosten-erfassen-Links
    EINER aktiven Journalzeile - migriert unverändert aus der bisherigen,
    separaten rohen OP-Tabelle (`konten.py::_op_zeile_html`), die mit dem
    gemeinsamen Journal entfällt. KEINE Aktion auf einer bereits
    stornierten/historischen Zeile (`beeinflusst_saldo=False`)."""

    if not jz.beeinflusst_saldo:
        return ""
    position = jz.zeile.position
    if position.status != "AKTIV":
        return ""
    aktion = f'<a href="/backoffice/op/{position.id}/korrigieren">Stornieren/Korrigieren</a>'
    if position.typ == OPTyp.RUECKLASTSCHRIFT.value:
        aktion += f' &nbsp;<a href="/backoffice/op/{position.id}/ruecklastschriftkosten">Rücklastschriftkosten erfassen</a>'
    return aktion


def _journal_zeile_html(jz) -> str:
    soll_html = eur(jz.soll_cent) if jz.soll_cent else '<span class="muted">—</span>'
    haben_html = eur(jz.haben_cent) if jz.haben_cent else '<span class="muted">—</span>'
    row_class = ' class="muted"' if not jz.beeinflusst_saldo else ""
    detail = _kontoblatt_ledger_html(jz.zeile)
    detail_html = f"<details><summary>Details</summary>{detail}</details>" if detail else ""
    return (
        f"<tr{row_class}>"
        f"<td>{jz.datum.isoformat()}</td><td>{h(jz.buchungsart)}</td>"
        f"<td>{soll_html}</td><td>{haben_html}</td><td>{eur(jz.laufender_saldo_cent)}</td>"
        f"<td>{_kontoblatt_status_html(jz.zeile)}</td>"
        f"<td>{h(jz.beleg_referenz)}{detail_html}</td>"
        f"<td>{h(jz.forderungsbezug)}</td>"
        f"<td>{_journal_aktion_html(jz)}</td>"
        "</tr>"
    )


def render_kontoblatt_html(kontoblatt: Kontoblatt, *, konto_id: str, zinsabrechnung_link_html: str = "") -> str:
    """Das EINE gemeinsame Kontoblatt-Journal - eingebettet identisch in
    Kontoansicht und Mieterakte (Auftrag HV-20261022-EIN-KONTOBLATT).
    `zinsabrechnung_link_html` ist optional (Mieterakte verlinkt z. B.
    nicht zwingend direkt auf die Zinsabrechnung-Aktion)."""

    journal = erstelle_journal(kontoblatt)
    journal_zeilen_html = "".join(_journal_zeile_html(jz) for jz in journal)

    _unbekanntes_datum_html = '<span class="muted">Datum unbekannt</span>'
    bankaufwand_zeilen = "".join(
        f"<tr><td>{b.rueckgabe_datum.isoformat() if b.rueckgabe_datum else _unbekanntes_datum_html}</td>"
        f"<td>{b.row.gebucht_am.strftime('%Y-%m-%d') if b.row.gebucht_am else ''}</td>"
        f"<td>{eur(b.row.bankentgelt_cent)}</td><td>{h(b.row.beleg_referenz)}</td>"
        f"<td>RLS-OP #{b.row.ruecklastschrift_op_position_id}"
        f"{' / Bank-Tx #' + str(b.row.bank_transaktion_id) if b.row.bank_transaktion_id is not None else ''}</td>"
        f"<td><span class='warn'>{h(b.status_text)}</span></td></tr>"
        for b in kontoblatt.dokumentierte_bankaufwaende
    )
    bankaufwand_karte = ""
    if kontoblatt.dokumentierte_bankaufwaende:
        bankaufwand_summe = sum(b.row.bankentgelt_cent for b in kontoblatt.dokumentierte_bankaufwaende)
        bankaufwand_karte = f"""
        <div class="card"><h3>Dokumentierte, (noch) nicht belastete Bankaufwände</h3>
          <p class="muted">Erfasst, aber NICHT als Forderung gebucht - NICHT im Gesamtsaldo enthalten. Summe:
             <strong>{eur(bankaufwand_summe)}</strong></p>
          <table><tr><th>Rückgabe (Bankereignistag)</th><th>Erfasst am</th><th>Bankentgelt</th><th>Beleg</th><th>Bezug</th><th>Status</th></tr>
          {bankaufwand_zeilen}</table>
        </div>"""

    mahnlauf_zeilen = "".join(
        f"<tr><td>{m.geplant_am.strftime('%Y-%m-%d') if isinstance(m.geplant_am, datetime) else ''}</td><td>{m.stufe}</td>"
        f"<td>{h(m.kanal)}</td><td>{h(m.status)}</td></tr>"
        for m in kontoblatt.ausstehende_mahnlaeufe
    )
    ausstehend_karte = ""
    if kontoblatt.ausstehende_mahnlaeufe:
        ausstehend_karte = f"""
        <div class="card"><h3>Ausstehende Mahnläufe (Kosten noch nicht gebucht)</h3>
          <p class="warn">Die Kostenverarbeitung ist für diese Mahnläufe noch NICHT abgeschlossen - das
             bedeutet "nicht gebucht", NICHT "nichts offen".</p>
          <table><tr><th>Geplant am</th><th>Stufe</th><th>Kanal</th><th>Status</th></tr>
          {mahnlauf_zeilen}</table>
        </div>"""

    # Fix6 (unabhängige Rückprüfung Codex 22.10.2026): ein Manualimport
    # OHNE Kosten-Freigabe (`pauschale_abgegolten=False`) erschien bisher
    # NIRGENDS am Kontoblatt - weder Summe noch eigene Zeile. Zeigt hier
    # dieselben, bereits dokumentierten Belege als "in Prüfung" - KEINE
    # erfundene 0,00-EUR-Summe, nur die reale Existenz des Vorgangs.
    manueller_versand_zeilen = "".join(
        f"<tr><td>{z.versanddatum.isoformat()}</td><td>{z.stufe}</td><td>{h(z.kanal)}</td>"
        f"<td>{h(z.beleg_referenz)}</td><td>{h(z.empfaenger_name)}</td></tr>"
        for z in kontoblatt.ausstehende_manuelle_mahnversand
    )
    manueller_versand_karte = ""
    if kontoblatt.ausstehende_manuelle_mahnversand:
        manueller_versand_karte = f"""
        <div class="card"><h3>Manuell dokumentierte Mahnschreiben (Kosten noch nicht gebucht)</h3>
          <p class="warn">Für diese bereits erfassten, tatsächlich versandten Schreiben ist noch KEINE
             Kostenbuchung erfolgt (Freigabe/Bestätigung steht aus) - das bedeutet "in Prüfung, noch nicht
             gebucht", NICHT "keine Kosten".</p>
          <table><tr><th>Versanddatum</th><th>Stufe</th><th>Kanal</th><th>Belegreferenz</th><th>Empfänger</th></tr>
          {manueller_versand_zeilen}</table>
        </div>"""

    return f"""
    <div class="card"><h2>Kontoblatt</h2>
      <p>Hauptforderung gebucht: <strong>{eur(kontoblatt.hauptforderung_cent)}</strong>
         (offen: {eur(kontoblatt.hauptforderung_offener_rest_cent)}) &nbsp;·&nbsp;
         Kosten gebucht: <strong>{eur(kontoblatt.kosten_cent)}</strong>
         (offen: {eur(kontoblatt.kosten_offener_rest_cent)}) &nbsp;·&nbsp;
         Zahlungs-/Stornostand: <strong>{eur(kontoblatt.zahlungen_saldo_cent)}</strong></p>
      <p>Gesamtsaldo: <strong>{eur(kontoblatt.saldo_cent)}</strong> &nbsp;|&nbsp;
         davon mit bekannter, bereits verstrichener Fälligkeit: <strong>{eur(kontoblatt.faelliger_unstrittiger_rest_cent)}</strong></p>
      <p><a href="/backoffice/konto/{h(konto_id)}/kontoauszug.pdf">PDF-Kontoauszug herunterladen</a>{zinsabrechnung_link_html}</p>
      <details open><summary>Kontoblatt — chronologisches Journal ({len(journal)})</summary>
        <table>
          <tr><th>Datum</th><th>Buchungsart</th><th>Soll</th><th>Haben</th><th>Laufender Saldo</th>
              <th>Status</th><th>Beleg</th><th>Forderungsbezug</th><th></th></tr>
          {journal_zeilen_html if journal else '<tr><td colspan=9 class="muted">Keine Buchungen.</td></tr>'}
        </table>
        <p class="muted">Grau markierte Zeilen sind storniert/ersetzt - sie bleiben zur Nachvollziehbarkeit
           sichtbar, verändern aber NICHT den laufenden Saldo.</p>
      </details>
    </div>
    {bankaufwand_karte}
    {ausstehend_karte}
    {manueller_versand_karte}"""
