"""Formular für Zinsprofile (Mahnkosten - Auftrag Markus 13.09.2026) und
für den globalen OeNB-Basiszinssatz. Erfasst nur belegte Vereinbarungen/
Werte; die eigentliche Berechnung/Auswahl steht in `mahnwesen/kosten.py`.
Keine Berechnung oder Buchung hier."""

from datetime import date
from html import escape as h

from mietinkasso.backoffice.views import csrf_feld, eur


def zinsprofil_formular(vertrag, profile, csrf):
    historien = []
    for p in profile:
        aktion = ""
        if p.status == "ENTWURF":
            aktion = f'''<form method="post" action="/backoffice/zinsprofil/{p.id}/freigeben">
                {csrf_feld(csrf)}<button type="submit">Zinsprofil bestätigen</button></form>'''
        vereinbart = (
            f"{p.vereinbarter_zinssatz_prozent} % ({'geprüft' if p.vereinbarung_geprueft else 'NICHT geprüft - wird ignoriert'})"
            if p.vereinbarter_zinssatz_prozent is not None else "keine (gesetzliche/UGB-Basis gilt)"
        )
        historien.append(
            f'<tr><td>{p.version}</td><td>{h(p.status)}</td><td>{"B2B" if p.ist_b2b else "Privat/Verbraucher"}</td>'
            f'<td>{p.vertragsdatum.isoformat() if p.vertragsdatum else "unbekannt"}</td>'
            f'<td>{h(vereinbart)}</td>'
            f'<td>{eur(p.mahngebuehr_kostenbasis_cent) if p.mahngebuehr_kostenbasis_cent is not None else "ungeklärt"}</td>'
            f'<td><details><summary>Belege</summary>'
            f'<pre>Zins: {h(p.vereinbarung_beleg or "")}\nGebühr: {h(p.mahngebuehr_kostenbasis_beleg or "")}</pre>'
            f'</details>{aktion}</td></tr>'
        )
    return f'''
    <div class="card"><h1>Zinsprofil (Mahnkosten) – {h(vertrag.id)}</h1>
      <p class="muted">§1000/§1333 ABGB: ohne geprüftes Profil gilt die gesetzliche Basis von 4 % p.a.
         Eine vereinbarte Verbraucherklausel wird NUR nach menschlicher Prüfung verwendet (KSchG §6/OGH 7Ob111/25m).
         §456 UGB (9,2 Prozentpunkte über Basiszinssatz) gilt nur bei beiderseits unternehmensbezogenem Geschäft
         und einem Vertragsdatum ab 16.03.2013.</p>
      <p><a href="/backoffice/vertrag/{h(vertrag.id)}/mahnvorschau">Zur Mahnvorschau</a></p>
      <table>
        <tr><th>Version</th><th>Status</th><th>B2B/Privat</th><th>Vertragsdatum</th>
            <th>Vereinbarter Zinssatz</th><th>Mahngebühr-Kostenbasis</th><th></th></tr>
        {''.join(historien) if historien else '<tr><td colspan=7 class="muted">Noch kein Zinsprofil erfasst - es gilt die gesetzliche Basis.</td></tr>'}
      </table>
    </div>
    <div class="card"><h2>Neues Zinsprofil</h2>
      <p class="muted">Eine neue Version pausiert die bisherige Freigabe bis zur erneuten Prüfung.</p>
      <form method="post" action="/backoffice/vertrag/{h(vertrag.id)}/zinsprofil/erstellen">
      {csrf_feld(csrf)}
      <fieldset><legend>Vertragseinordnung</legend>
        <label><input type="checkbox" name="ist_b2b" value="1"> Beiderseits unternehmensbezogenes Geschäft (B2B)</label>
        <label>Vertragsdatum (für §456 UGB-Stichtag 16.03.2013 nötig)</label>
        <input type="date" name="vertragsdatum">
      </fieldset>
      <fieldset><legend>Vereinbarter Zinssatz (nur nach Prüfung wirksam)</legend>
        <label>Vereinbarter Zinssatz in Prozent p.a.</label><input name="vereinbarter_zinssatz_prozent" placeholder="z. B. 5,0">
        <label><input type="checkbox" name="vereinbarung_geprueft" value="1">
          Wirksamkeit der Klausel wurde menschlich geprüft (KSchG §6 Abs 1 Z 13/OGH 7Ob111/25m)</label>
        <label>Beleg/Fundstelle der Vereinbarung</label><textarea name="vereinbarung_beleg" rows="3"></textarea>
      </fieldset>
      <fieldset><legend>Mahngebühr - Kostenbasis (§1333 Abs 2 ABGB, §458 UGB)</legend>
        <label>Notwendige, zweckmäßige tatsächliche Betreibungskosten in Cent</label>
        <input name="mahngebuehr_kostenbasis_cent" placeholder="z. B. 2000 für 20,00 EUR">
        <label>Beleg/Nachweis der Kostenbasis</label><textarea name="mahngebuehr_kostenbasis_beleg" rows="3"></textarea>
        <p class="muted">Leer lassen, solange keine belegte Kostenbasis vorliegt - dann wird KEINE Mahngebühr angesetzt.</p>
      </fieldset>
      <button type="submit">Als Entwurf speichern</button>
      </form>
    </div>'''


def zinsprofil_form_werte(form) -> dict:
    def _decimal_oder_none(name):
        roh = str(form.get(name, "") or "").strip().replace(",", ".")
        if not roh:
            return None
        from decimal import Decimal, InvalidOperation
        try:
            return Decimal(roh)
        except InvalidOperation as exc:
            raise ValueError(f"Ungültiger Zahlenwert für {name}: {roh}") from exc

    def _cent_oder_none(name):
        roh = str(form.get(name, "") or "").strip()
        if not roh:
            return None
        try:
            return int(roh)
        except ValueError as exc:
            raise ValueError(f"Ungültiger Cent-Betrag für {name}: {roh}") from exc

    vertragsdatum_roh = str(form.get("vertragsdatum", "") or "").strip()
    return {
        "ist_b2b": str(form.get("ist_b2b", "")) == "1",
        "vertragsdatum": date.fromisoformat(vertragsdatum_roh) if vertragsdatum_roh else None,
        "vereinbarter_zinssatz_prozent": _decimal_oder_none("vereinbarter_zinssatz_prozent"),
        "vereinbarung_geprueft": str(form.get("vereinbarung_geprueft", "")) == "1",
        "vereinbarung_beleg": str(form.get("vereinbarung_beleg", "") or "").strip() or None,
        "mahngebuehr_kostenbasis_cent": _cent_oder_none("mahngebuehr_kostenbasis_cent"),
        "mahngebuehr_kostenbasis_beleg": str(form.get("mahngebuehr_kostenbasis_beleg", "") or "").strip() or None,
    }


def basiszinssatz_formular(basiszinssaetze, csrf):
    zeilen = "".join(
        f'<tr><td>{h(b.id)}</td><td>{b.gueltig_von.isoformat()}</td><td>{b.gueltig_bis.isoformat()}</td>'
        f'<td>{b.basiszinssatz_prozent} %</td><td>{h(b.quelle_referenz)}</td></tr>'
        for b in basiszinssaetze
    )
    return f'''
    <div class="card"><h1>OeNB-Basiszinssatz (§456 UGB)</h1>
      <p class="muted">Ohne erfassten Wert für ein Halbjahr bleibt die B2B-Berechnung für diesen Zeitraum
         explizit "Basis ungeklärt" - der letzte bekannte Wert wird NIE stillschweigend fortgeschrieben.</p>
      <table>
        <tr><th>Halbjahr-ID</th><th>Gültig von</th><th>Gültig bis</th><th>Basiszinssatz</th><th>Quelle</th></tr>
        {zeilen if zeilen else '<tr><td colspan=5 class="muted">Noch kein Basiszinssatz erfasst.</td></tr>'}
      </table>
    </div>
    <div class="card"><h2>Neuer Basiszinssatz</h2>
      <form method="post" action="/backoffice/basiszinssatz/erfassen">
      {csrf_feld(csrf)}
      <label>Halbjahr-ID (z. B. 2026-1)</label><input name="id" required>
      <label>Gültig von</label><input type="date" name="gueltig_von" required>
      <label>Gültig bis</label><input type="date" name="gueltig_bis" required>
      <label>Basiszinssatz in Prozent</label><input name="basiszinssatz_prozent" required placeholder="z. B. 1,53">
      <label>Quelle/Referenz (z. B. OeNB-Kundmachung)</label><input name="quelle_referenz" required>
      <button type="submit">Erfassen</button>
      </form>
    </div>'''
