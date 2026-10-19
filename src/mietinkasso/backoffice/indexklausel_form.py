"""Formular für belegte Vertragsregeln. Keine Berechnung oder Sollstellung."""
from datetime import date
from decimal import Decimal, InvalidOperation
from html import escape as h

from mietinkasso.backoffice.views import csrf_feld, eur, option


def klausel_formular(vertrag, komponenten, klauseln, csrf, mietprofil=None):
    beginn = mietprofil.urspruenglicher_mietbeginn if mietprofil else None
    beginn_text = beginn.strftime("%d.%m.%Y") if beginn else "noch nicht gesondert belegt"
    komponenten_html = "".join(
        f'<label><input type="checkbox" name="komponenten" value="{h(k.id)}">'
        f'{h(k.bezeichnung)} ({eur(k.betrag_cent)})</label><br>' for k in komponenten if k.indexierbar
    ) or '<p>Keine freigegebene indexierbare Mietkomponente vorhanden.</p>'
    historien = []
    for k in klauseln:
        aktion = ""
        if k.status == "ENTWURF":
            aktion = f'''<form method="post" action="/backoffice/indexklausel/{k.id}/freigeben">
                {csrf_feld(csrf)}<button type="submit">Vertragsklausel bestätigen</button></form>'''
        historien.append(
            f'<tr><td>{k.version}</td><td>{h(k.status)}</td><td>{h(k.basis_reihe)} '
            f'{k.basis_wert} ({h(k.basis_monat)})</td><td>{"ab" if k.schwelle_inklusive else "über"} '
            f'{k.schwelle_prozent} %</td><td>{k.anpassungsmonat or "kein fixer Monat"}</td>'
            f'<td><details><summary>Vertragsbeleg</summary><pre>{h(k.klausel_text or "")}</pre></details>{aktion}</td></tr>'
        )
    reihen = "".join(option(code, label) for code, label in [
        ("VPI20C18", "VPI 2020"), ("VPI15C18", "VPI 2015"), ("VPI00", "VPI 2000"), ("VPI96", "VPI 1996")])
    return f'''
    <div class="card"><h1>Vertragliche Indexklausel – {h(vertrag.id)}</h1>
      <p>Hier wird die belegte Vertragsregel gespeichert. Die monatliche Prüfung berücksichtigt zusätzlich
         das freigegebene Rechtsprofil. Diese Seite erzeugt keine Vorschreibung und versendet keine Nachricht.</p>
      <p><a href="/backoffice/vertrag/{h(vertrag.id)}/rechtsprofil">Zum Rechtsprofil</a></p></div>
    <div class="card"><h2>Neue Vertragsregel</h2>
      <p class="muted">Eine neue Version pausiert die bisherige Regel bis zur erneuten Prüfung.</p>
      <form method="post" action="/backoffice/vertrag/{h(vertrag.id)}/indexklausel/erstellen">
      {csrf_feld(csrf)}
      <fieldset><legend>Vertrag und letzte tatsächlich verwendete Basis</legend>
        <p><strong>Ursprünglicher Mietbeginn: {h(beginn_text)}</strong><br>
           Buchhaltung im System ab: {vertrag.gueltig_von.strftime("%d.%m.%Y")}.</p>
        <p class="muted">Unterschrift im Juni, Mietbeginn im Dezember: keine Anpassung vor Dezember.
           Eine ausdrücklich vereinbarte VPI-Basis bleibt erhalten. Das Abschlussdatum wird für
           gesetzliche Fristen separat geführt. Frühere Erhöhungen werden nicht nochmals gerechnet.</p>
        <label>Datum des Vertragsabschlusses (Unterschrift)</label><input type="date" name="abschlussdatum" required>
        <label>VPI-Reihe laut Vertrag</label><select name="basis_reihe">{reihen}</select>
        <label>VPI-Basismonat</label><input type="month" name="basis_monat" required>
        <label>Tatsächlich verwendeter VPI-Basiswert</label><input name="basis_wert" required placeholder="z. B. 130,0">
        <label>Monat der letzten tatsächlich durchgeführten Anpassung</label>
        <input type="month" name="letzte_anpassung_monat">
        <p class="muted">Nur leer lassen, wenn noch nie angepasst wurde. Bei unbekannter bisheriger Basis bleibt die Fachfreigabe offen.</p>
        <label>Vertragswortlaut und Fundstelle</label><textarea name="klausel_text" rows="5" required></textarea>
        <label>Indexierbare Mietbestandteile</label>{komponenten_html}
      </fieldset>
      <fieldset><legend>Schwelle und Kalenderregel</legend>
        <label>Vertraglicher Anpassungstermin</label><select name="terminmodus" required>
          <option value="">Bitte anhand der Klausel auswählen</option>
          <option value="FIXER_MONAT">Bestimmter Kalendermonat</option>
          <option value="BEI_SCHWELLE">Bei Erreichen der Schwelle, ohne festen Monat</option>
          <option value="INTERVALL">Nach Mindestabstand, ohne festen Monat</option></select>
        <label>Schwelle in Prozent (0 nur bei belegter Anpassung ohne Schwelle)</label>
        <input name="schwelle_prozent" required>
        <label>Grenze</label><select name="schwelle_inklusive">
          <option value="0">Erst über der Schwelle</option><option value="1">Ab Erreichen der Schwelle</option></select>
        <label>Fester Anpassungsmonat, falls vertraglich vereinbart</label>
        <input type="number" name="anpassungsmonat" min="1" max="12">
        <label>Mindestabstand zwischen Anpassungen (Monate)</label>
        <input type="number" name="mindestintervall_monate" min="1">
        <label>Rundung des Indexwerts (Nachkommastellen, nur bei ausdrücklicher Klausel)</label>
        <input type="number" name="indexwert_rundung_dezimalstellen" min="0" max="6">
        <p class="muted">Die Rundung des Indexwerts ist keine Rundung der Schwellen-Grenzwerte.</p>
        <label>Rundung der oberen und unteren Index-Grenzwerte (Nachkommastellen)</label>
        <input type="number" name="schwellenkorridor_rundung_dezimalstellen" min="0" max="6">
      </fieldset>
      <fieldset><legend>Zusätzliche Wartefrist nach Indexereignis</legend>
        <label>Kalendermonate</label><input type="number" name="wartefrist_monate_nach_indexereignis" min="1">
        <label>Bezug laut Vertrag</label><select name="wartefrist_bezug"><option value="">keine erfasst</option>
          <option value="VPI_PERIODE">VPI-Bezugsmonat</option><option value="VEROEFFENTLICHUNG">Belegte amtliche Veröffentlichung</option></select>
        <p class="muted">Ohne belegtes maßgebliches Ereignis bleibt eine solche Anpassung gesperrt.</p>
      </fieldset>
      <button type="submit">Vertragsklausel als Entwurf speichern</button></form></div>
    <div class="card"><h2>Gespeicherte Versionen</h2><table><tr><th>Version</th><th>Status</th>
      <th>Basis</th><th>Schwelle</th><th>Anpassungsmonat</th><th>Beleg und Bestätigung</th></tr>
      {''.join(historien) or '<tr><td colspan="6">Noch keine Klausel erfasst.</td></tr>'}</table></div>'''


def klausel_form_werte(form, vertrag, komponenten):
    def zahl(name, optional=False):
        raw = str(form.get(name, "")).strip().replace(",", ".")
        if not raw and optional:
            return None
        try:
            value = Decimal(raw)
        except InvalidOperation:
            raise ValueError(f"Ungültige Zahl: {name}") from None
        if not value.is_finite() or value < 0:
            raise ValueError(f"Ungültige Zahl: {name}")
        return value

    basis = zahl("basis_wert")
    if basis <= 0:
        raise ValueError("Der VPI-Basiswert muss größer als null sein.")
    monat = str(form.get("basis_monat", ""))
    parsed = date.fromisoformat(monat + "-01")
    if monat != parsed.strftime("%Y-%m"):
        raise ValueError("Der Basismonat muss Jahr und Monat enthalten.")
    reihe = str(form.get("basis_reihe", ""))
    if reihe not in {"VPI20C18", "VPI15C18", "VPI00", "VPI96"}:
        raise ValueError("Unbekannte VPI-Reihe.")
    quelle = str(form.get("klausel_text", "")).strip()
    if not quelle:
        raise ValueError("Vertragswortlaut und Fundstelle fehlen.")
    ids = form.getlist("komponenten")
    erlaubte = {k.id for k in komponenten if k.indexierbar and k.art not in {"BK_VORAUSZAHLUNG", "HEIZ_WW_VORAUSZAHLUNG"}}
    if not ids or len(ids) != len(set(ids)) or not set(ids) <= erlaubte:
        raise ValueError("Bitte ausschließlich die belegten indexierbaren Mietbestandteile auswählen.")
    data = dict(vertrag_id=vertrag.id, rechtsordnung=vertrag.rechtsordnung,
                berechnungsprofil="EINFACHER_SCHWELLENVERGLEICH",
                abschlussdatum=date.fromisoformat(str(form.get("abschlussdatum", ""))),
                basis_reihe=reihe, basis_wert=basis, basis_monat=monat,
                schwelle_prozent=zahl("schwelle_prozent"), schwelle_inklusive=form.get("schwelle_inklusive") == "1",
                klausel_text=quelle, indexierbare_komponenten=ids,
                wartefrist_bezug=str(form.get("wartefrist_bezug", "")).strip() or None)
    for key in ("anpassungsmonat", "mindestintervall_monate", "indexwert_rundung_dezimalstellen", "schwellenkorridor_rundung_dezimalstellen", "wartefrist_monate_nach_indexereignis"):
        raw = str(form.get(key, "")).strip()
        data[key] = int(raw) if raw else None
    data["terminmodus"] = str(form.get("terminmodus", "")).strip() or None
    if data["terminmodus"] not in {"FIXER_MONAT", "BEI_SCHWELLE", "INTERVALL"}:
        raise ValueError("Bitte den belegten Anpassungstermin auswählen.")
    letzte = str(form.get("letzte_anpassung_monat", "")).strip()
    if letzte:
        date.fromisoformat(letzte + "-01")
    data["letzte_anpassung_monat"] = letzte or None
    return data
