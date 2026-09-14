"""Formulare für die vereinfachte Vertragsanlage/-anzeige (Auftrag
HV-20260913-VERTRAGSANLAGE). Reine Renderer/Validatoren wie
`indexklausel_form.py` - kein FastAPI-Import, keine Datenbankschreibung
hier. Nutzt den BESTEHENDEN generischen Intake (`intake/`) als einzige
Schreibstrecke; dieses Modul baut nur die editierbare Formularoberfläche
und übersetzt Formularwerte in `IntakePaket`-Rohdaten."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from html import escape as h

from mietinkasso.backoffice.views import csrf_feld, eur, option, parse_eur_betrag
from mietinkasso.domain.money import cents_to_decimal, zerlege_brutto_cent

NUTZUNGSARTEN = ["UNGEKLAERT", "WOHNUNG", "BUERO", "GESCHAEFTSLOKAL", "SONSTIGE"]
RECHTSORDNUNGEN = [
    "UNGEKLAERT", "OESTERREICH_MRG_VOLL", "OESTERREICH_MRG_TEIL", "OESTERREICH_MRG_FREI",
    "OESTERREICH_WGG", "OESTERREICH_GEWERBE", "DEUTSCHLAND",
]


def feld_status(wert, *, unklar_werte: tuple = ()) -> str:
    """Kurzstatus für die Übersicht - NIE mehr als diese drei Zustände,
    kein kilometerlanges Roh-JSON im Hauptbild (Auftrag Markus)."""

    if wert in unklar_werte:
        return "Klärung erforderlich"
    if wert is None or wert == "":
        return "Angabe fehlt"
    return "bereit"


def _status_badge(status: str) -> str:
    klasse = {"bereit": "badge-muted", "Angabe fehlt": "badge-warn", "Klärung erforderlich": "badge-error"}[status]
    farbe = {"bereit": "ok", "Angabe fehlt": "warn", "Klärung erforderlich": "error"}[status]
    return f'<span class="badge {klasse}"><span class="{farbe}">{h(status)}</span></span>'


def _beleg_hinweis(vorschlaege: dict, feld: str) -> str:
    v = vorschlaege.get(feld)
    if v is None:
        return ""
    return (
        f'<p class="muted">Vorschlag aus Dokument, Seite {v.seite}: '
        f'&bdquo;&hellip;{h(v.auszug)}&hellip;&ldquo; — bitte prüfen.</p>'
    )


def einheit_label(objekt, einheit) -> str:
    return f"{objekt.bezeichnung} ({objekt.id}) / {einheit.bezeichnung} ({einheit.id})"


# ---------------------------------------------------------------------------
# Liste + Auswahl
# ---------------------------------------------------------------------------


def vertraege_liste_formular(zeilen: list[dict], csrf: str, *, leerstand_zeilen: list[dict] | None = None) -> str:
    """`zeilen`: Liste von {vertrag, objekt, einheit, debitor}.
    `leerstand_zeilen`: Liste von {objekt, einheit} OHNE Vertrag (Auftrag
    HV-20260914-UI-EINFACH: Leerstände bleiben sichtbar, statt nur
    vermietete Einheiten zu zeigen)."""

    from mietinkasso.backoffice.views import nutzungsstatus_label

    optionen = "".join(
        option(z["vertrag"].id, f"{einheit_label(z['objekt'], z['einheit'])} — {z['debitor'].name}")
        for z in zeilen
    )
    tabellenzeilen = "".join(
        f"<tr><td>{h(z['debitor'].name)}<br><span class='muted'>{h(z['objekt'].bezeichnung)} / "
        f"{h(z['einheit'].bezeichnung)}</span></td>"
        f"<td>{h(nutzungsstatus_label(z['einheit'].nutzungsstatus))}</td>"
        f"<td><a href='/backoffice/vertrag/{h(z['vertrag'].id)}'>Akte öffnen</a>"
        f"<details><summary>Details</summary>Vertrag: <code>{h(z['vertrag'].id)}</code></details></td></tr>"
        for z in zeilen
    ) or "<tr><td colspan=3>Noch keine Verträge vorhanden.</td></tr>"

    leerstand_zeilen = leerstand_zeilen or []
    leerstand_html = "".join(
        f"<tr><td>{h(z['objekt'].bezeichnung)} / {h(z['einheit'].bezeichnung)}</td>"
        f"<td>{h(nutzungsstatus_label(z['einheit'].nutzungsstatus))}</td></tr>"
        for z in leerstand_zeilen
    )
    leerstand_karte = f"""
    <details class="card">
      <summary>Leerstände &amp; sonstige Einheiten ohne Mietvertrag ({len(leerstand_zeilen)})</summary>
      <table><tr><th>Objekt / Einheit</th><th>Bestandsart</th></tr>
      {leerstand_html or '<tr><td colspan=2 class="muted">Keine Einheiten ohne Mietvertrag.</td></tr>'}</table>
    </details>"""

    return f"""
    <div class="card">
      <h1>Mieter &amp; Objekte</h1>
      <p><a href="/backoffice/vertraege/neu"><button type="button" class="gross">+ Mietvertrag hinzufügen</button></a></p>
      <form method="get" action="/backoffice/vertrag/weiterleiten" style="max-width:520px;">
        <label>Mietvertrag auswählen (Objekt / Einheit — Mieter)</label>
        <select name="vertrag_id" required>{optionen}</select>
        <button type="submit" class="secondary">Öffnen</button>
      </form>
    </div>
    <div class="card"><h2>Mieter</h2>
      <div class="tabelle-scroll">
      <table><tr><th>Mieter / Objekt / Einheit</th><th>Bestandsart</th><th></th></tr>
      {tabellenzeilen}</table>
      </div>
    </div>
    {leerstand_karte}"""


# ---------------------------------------------------------------------------
# Neuanlage - Kontext (Objekt/Einheit/Debitor/Gesellschaft + Vertragsdaten)
# ---------------------------------------------------------------------------


def neu_kontext_formular(*, einheiten_mit_objekt: list[tuple], debitoren: list, gesellschaften: list, csrf: str, fehler: str | None = None) -> str:
    einheit_optionen = "".join(
        f'<option value="{h(einheit.id)}">{h(einheit_label(objekt, einheit))}</option>'
        for objekt, einheit in einheiten_mit_objekt
    )
    debitor_optionen = option("", "— aus Liste wählen —") + "".join(option(d.id, f"{d.name} ({d.id})") for d in debitoren)
    gesellschaft_optionen = "".join(option(g.id, f"{g.name} ({g.id})") for g in gesellschaften)
    rechtsordnung_optionen = "".join(option(r, r) for r in RECHTSORDNUNGEN)
    fehlerblock = f'<div class="flash-error">{h(fehler)}</div>' if fehler else ""
    return f"""
    <div class="card">
      <h1>Neuen Mietvertrag anlegen</h1>
      <p class="muted">Objekt/Einheit und Gesellschaft müssen bereits als Stammdaten vorhanden sein
         (Einspielung über den Echtbetrieb-Intake). Der Mieter kann entweder aus der Liste gewählt oder
         hier gleich neu angelegt werden - beides läuft im selben atomaren Vorgang. Dieser Ablauf legt den
         Mietvertrag selbst sowie das Mietvertragsprofil an - optional vorausgefüllt aus einem hochgeladenen PDF.</p>
      {fehlerblock}
      <form method="post" action="/backoffice/vertragsanlage/pdf-hochladen" enctype="multipart/form-data">
        {csrf_feld(csrf)}
        <input type="hidden" name="modus" value="NEU">
        <fieldset><legend>Vertrag</legend>
          <label>Vertrag-ID (frei wählbar, eindeutig)</label>
          <input name="vertrag_id" required placeholder="z. B. V-601-TOP4">
          <label>Einheit</label><select name="einheit_id" required>{einheit_optionen}</select>
          <label>Vermieter-Gesellschaft</label><select name="gesellschaft_id" required>{gesellschaft_optionen}</select>
          <label>Rechtsordnung</label><select name="rechtsordnung" required>{rechtsordnung_optionen}</select>
          <label>Vertragsbeginn (technisch, Sollstellung/OP)</label><input type="date" name="gueltig_von" required>
          <label>Vertragsende (leer lassen = unbefristet)</label><input type="date" name="gueltig_bis">
        </fieldset>
        <fieldset><legend>Mieter</legend>
          <label>Vorhandenen Mieter wählen</label><select name="debitor_id">{debitor_optionen}</select>
          <p class="muted">ODER neu anlegen (wenn ausgefüllt, hat das Vorrang vor der Auswahl oben):</p>
          <label>Neue Mieter-ID</label><input name="neuer_debitor_id" placeholder="z. B. DEB-NEU-1">
          <label>Name</label><input name="neuer_debitor_name">
          <label>E-Mail</label><input name="neuer_debitor_email" type="email">
          <label>Adresse</label><input name="neuer_debitor_adresse">
        </fieldset>
        <fieldset><legend>Mietbestandteile (optional, geprüfte Komponenten - keine historische Sollbuchung)</legend>
          <p class="muted">Werden als aktive Vertragskomponenten ab Vertragsbeginn angelegt, lösen aber KEINE
             rückwirkende OP-Buchung aus - die künftige Vorschreibung nutzt sie normal.</p>
          <label>Hauptmietzins (brutto, EUR)</label><input name="komponente_hmz">
          <label>Betriebskosten (brutto, EUR)</label><input name="komponente_bk">
          <label>Heizkosten (brutto, EUR)</label><input name="komponente_hk">
          <label>Küche (brutto, EUR)</label><input name="komponente_kueche">
          <label>Parkplatz (brutto, EUR)</label><input name="komponente_parkplatz">
        </fieldset>
        <fieldset><legend>Optionale einmalige PDF-Aufnahme</legend>
          <p class="muted">Lokale Texterkennung ohne KI-/Cloud-Aufruf - liefert nur Vorschläge mit Seitenbeleg,
             nichts wird ungeprüft übernommen. Ohne Datei geht es direkt zur manuellen Eingabe weiter.</p>
          <label>Vertrags-PDF (optional)</label><input type="file" name="pdf_datei" accept="application/pdf">
        </fieldset>
        <button type="submit">Weiter zur Prüfung</button>
      </form>
    </div>"""


def neu_kontext_werte(form) -> dict:
    vertrag_id = str(form.get("vertrag_id", "")).strip()
    if not vertrag_id:
        raise ValueError("Vertrag-ID darf nicht leer sein.")
    einheit_id = str(form.get("einheit_id", "")).strip()
    gesellschaft_id = str(form.get("gesellschaft_id", "")).strip()
    rechtsordnung = str(form.get("rechtsordnung", "")).strip()
    if rechtsordnung not in RECHTSORDNUNGEN:
        raise ValueError("Ungültige Rechtsordnung.")

    neuer_debitor_name = str(form.get("neuer_debitor_name", "")).strip()
    neuer_debitor: dict | None = None
    if neuer_debitor_name:
        neuer_debitor_id = str(form.get("neuer_debitor_id", "")).strip()
        if not neuer_debitor_id:
            raise ValueError("Neue Mieter-ID darf nicht leer sein.")
        neuer_debitor = dict(
            id=neuer_debitor_id, name=neuer_debitor_name,
            email=str(form.get("neuer_debitor_email", "")).strip() or None,
            adresse=str(form.get("neuer_debitor_adresse", "")).strip() or None,
        )
        debitor_id = neuer_debitor_id
    else:
        debitor_id = str(form.get("debitor_id", "")).strip()

    if not (einheit_id and debitor_id and gesellschaft_id):
        raise ValueError("Einheit, Mieter (Auswahl oder Neuanlage) und Gesellschaft sind Pflichtfelder.")
    gueltig_von_raw = str(form.get("gueltig_von", "")).strip()
    if not gueltig_von_raw:
        raise ValueError("Vertragsbeginn ist ein Pflichtfeld.")
    date.fromisoformat(gueltig_von_raw)  # Formatprüfung
    gueltig_bis_raw = str(form.get("gueltig_bis", "")).strip()
    if gueltig_bis_raw:
        date.fromisoformat(gueltig_bis_raw)  # Formatprüfung
    return dict(
        vertrag_id=vertrag_id, einheit_id=einheit_id, debitor_id=debitor_id, gesellschaft_id=gesellschaft_id,
        rechtsordnung=rechtsordnung, gueltig_von=gueltig_von_raw, gueltig_bis=gueltig_bis_raw or None,
        neuer_debitor=neuer_debitor,
    )


#: (Formularfeld-Präfix, Komponentenart, Bezeichnung) - feste, einfache
#: Auswahl gängiger Mietbestandteile (Auftrag: "Küche/Parkplatz/weitere
#: Mietkomponenten"). Kein freies Hinzufügen beliebiger Arten in dieser
#: Runde - das deckt die explizit genannten Fälle ab.
_KOMPONENTEN_FELDER = (
    ("komponente_hmz", "HMZ", "Hauptmietzins"),
    ("komponente_bk", "BK_VORAUSZAHLUNG", "Betriebskosten"),
    ("komponente_hk", "HEIZ_WW_VORAUSZAHLUNG", "Heizkosten"),
    ("komponente_kueche", "KUECHE", "Küche"),
    ("komponente_parkplatz", "PARKPLATZ", "Parkplatz"),
)


def komponenten_werte_aus_form(form, *, vertrag_id: str, gueltig_von: str) -> list[dict]:
    """Baut aus den (optionalen) Mietbestandteile-Feldern im
    Neuanlage-Formular `komponenten[]`-Rohdaten für den generischen
    Intake - nur ausgefüllte Felder werden übernommen, kein erfundener
    Nullbetrag für ein leer gelassenes Feld."""

    ergebnis: list[dict] = []
    for feldname, art, bezeichnung in _KOMPONENTEN_FELDER:
        roh = str(form.get(feldname, "")).strip()
        if not roh:
            continue
        betrag_cent = parse_eur_betrag(roh)
        ergebnis.append(dict(
            id=f"{vertrag_id}-{art}", art=art, bezeichnung=bezeichnung,
            betrag_cent=betrag_cent, gueltig_von=gueltig_von,
        ))
    return ergebnis


# ---------------------------------------------------------------------------
# Editierbares Review (Mietvertragsprofil + optionale Kaution)
# ---------------------------------------------------------------------------


def _dezimal_feld(name: str, label: str, werte: dict, vorschlaege: dict, *, hinweis: str = "") -> str:
    wert = werte.get(name, "") or ""
    beleg = _beleg_hinweis(vorschlaege, name)
    hinweistext = f'<p class="muted">{h(hinweis)}</p>' if hinweis else ""
    return f'<label>{h(label)}</label><input name="{name}" value="{h(str(wert))}">{beleg}{hinweistext}'


def _kaution_eingang_feld(werte: dict, kaution_bereits_vorhanden: bool) -> str:
    if kaution_bereits_vorhanden:
        return (
            f'<p><strong>Bereits erfasst: {h(str(werte.get("kaution_eingegangen_cent") or ""))} '
            f'am {h(str(werte.get("kaution_eingegangen_stichtag") or ""))}.</strong> '
            "Eine bestätigte Kaution wird über diesen Ablauf NICHT geändert (strikte Idempotenz) - "
            "Korrekturen bitte gesondert klären.</p>"
        )
    return (
        '<label>Tatsächlich eingegangener Betrag (nur bei belegtem Zahlungseingang ausfüllen)</label>'
        f'<input name="kaution_eingegangen_cent" value="{h(str(werte.get("kaution_eingegangen_cent") or ""))}">'
        '<label>Stichtag des Zahlungseingangs</label>'
        f'<input type="date" name="kaution_eingegangen_stichtag" value="{h(str(werte.get("kaution_eingegangen_stichtag") or ""))}">'
        '<label>Referenz (Überweisungsbeleg o. Ä.)</label>'
        f'<input name="kaution_eingegangen_referenz" value="{h(str(werte.get("kaution_eingegangen_referenz") or ""))}">'
    )


def pdf_upload_mini_formular(vertrag_id: str, csrf: str) -> str:
    return f"""
    <div class="card">
      <h2>Optional: aus PDF vorausfüllen</h2>
      <p class="muted">Lokale Texterkennung ohne KI-/Cloud-Aufruf - überschreibt NIE bereits gespeicherte
         Angaben, füllt nur leere Felder unten vor.</p>
      <form method="post" action="/backoffice/vertragsanlage/pdf-hochladen" enctype="multipart/form-data">
        {csrf_feld(csrf)}
        <input type="hidden" name="modus" value="BESTEHEND">
        <input type="hidden" name="vertrag_id" value="{h(vertrag_id)}">
        <input type="file" name="pdf_datei" accept="application/pdf" required>
        <button type="submit">Hochladen und Vorschläge einblenden</button>
      </form>
    </div>"""


def _mehrdeutigkeit_block(mehrdeutigkeiten: dict) -> str:
    if not mehrdeutigkeiten:
        return ""
    zeilen = []
    for label, eintrag in mehrdeutigkeiten.items():
        fundstellen = "; ".join(f"„{h(f.wert)}“ (Seite {f.seite})" for f in eintrag.funde)
        zeilen.append(f"<li><strong>{h(label)}</strong>: mehrere unterschiedliche Fundstellen im Dokument — {fundstellen}. Bitte manuell entscheiden.</li>")
    return f'<div class="flash-error"><strong>Mehrdeutige Angaben im Dokument gefunden (kein automatischer Vorschlag):</strong><ul>{"".join(zeilen)}</ul></div>'


def _eckdaten_block(eckdaten: dict | None, pdf_hinweise: dict) -> str:
    if not eckdaten:
        return ""
    mieter_hinweis = pdf_hinweise.get("mieter_name_hinweis")
    vermieter_hinweis = pdf_hinweise.get("vermieter_name_hinweis")
    mieter_vergleich = (
        f' <span class="muted">(laut Dokument, Seite {mieter_hinweis.seite}: „{h(mieter_hinweis.formularwert)}“ — bitte mit Auswahl vergleichen)</span>'
        if mieter_hinweis else ""
    )
    vermieter_vergleich = (
        f' <span class="muted">(laut Dokument, Seite {vermieter_hinweis.seite}: „{h(vermieter_hinweis.formularwert)}“ — bitte mit Auswahl vergleichen)</span>'
        if vermieter_hinweis else ""
    )
    return f"""
    <div class="card"><h2>Eckdaten dieses Vorgangs</h2>
      <table>
        <tr><th>Vertrag-ID</th><td>{h(eckdaten.get('vertrag_id', ''))}</td></tr>
        <tr><th>Objekt / Einheit</th><td>{h(eckdaten.get('objekt_einheit_label', ''))}</td></tr>
        <tr><th>Mieter</th><td>{h(eckdaten.get('mieter_anzeige', ''))}{mieter_vergleich}</td></tr>
        <tr><th>Vermieter-Gesellschaft</th><td>{h(eckdaten.get('gesellschaft_name', ''))}{vermieter_vergleich}</td></tr>
        <tr><th>Rechtsordnung</th><td>{h(eckdaten.get('rechtsordnung', ''))}</td></tr>
        <tr><th>Vertragsbeginn / -ende</th><td>{h(eckdaten.get('gueltig_von', ''))} – {h(eckdaten.get('gueltig_bis') or 'unbefristet')}</td></tr>
      </table>
    </div>"""


def review_formular(
    *, ist_neu: bool, kontext_hidden: dict, werte: dict, vorschlaege: dict, warnungen: tuple[str, ...],
    csrf: str, aktion_url: str, zurueck_href: str, kaution_bereits_vorhanden: bool = False,
    eckdaten: dict | None = None, mehrdeutigkeiten: dict | None = None,
) -> str:
    hidden_felder = "".join(f'<input type="hidden" name="{h(k)}" value="{h(str(v))}">' for k, v in kontext_hidden.items())
    warnblock = "".join(f'<p class="warn">{h(w)}</p>' for w in warnungen)
    eckdaten_block = _eckdaten_block(eckdaten, vorschlaege)
    mehrdeutigkeit_block = _mehrdeutigkeit_block(mehrdeutigkeiten or {})
    nutzungsart_optionen = "".join(
        option(n, n, selected=(werte.get("nutzungsart") or "UNGEKLAERT") == n) for n in NUTZUNGSARTEN
    )
    inklusive_wert = werte.get("index_schwelle_inklusive")
    inklusive_optionen = "".join([
        option("", "nicht festgestellt", selected=inklusive_wert in (None, "")),
        option("1", "ab Erreichen der Schwelle (inklusive)", selected=inklusive_wert == "1"),
        option("0", "erst über der Schwelle", selected=inklusive_wert == "0"),
    ])
    return f"""
    <div class="card">
      <h1>{'Neuen Mietvertrag prüfen' if ist_neu else 'Mietvertragsprofil aktualisieren'}</h1>
      <p class="muted">Lokal ausgelesene bzw. bereits gespeicherte Werte - jedes Feld bleibt editierbar.
         Fehlende/unklare Angaben bewusst offen lassen, nichts wird erfunden.</p>
      {warnblock}
    </div>
    {eckdaten_block}
    {mehrdeutigkeit_block}
    <div class="card">
      <form method="post" action="{h(aktion_url)}">
        {csrf_feld(csrf)}
        {hidden_felder}
        <fieldset><legend>Nutzung und Rechtsgrundlage</legend>
          <label>Nutzungsart (unabhängig von der Rechtsordnung)</label>
          <select name="nutzungsart">{nutzungsart_optionen}</select>
          {_beleg_hinweis(vorschlaege, "nutzungsart")}
        </fieldset>
        <fieldset><legend>Mietbeginn und Verwaltung</legend>
          <label>Ursprünglicher tatsächlicher Mietbeginn (falls abweichend vom Vertragsbeginn)</label>
          <input type="date" name="urspruenglicher_mietbeginn" value="{h(str(werte.get('urspruenglicher_mietbeginn') or ''))}">
          {_beleg_hinweis(vorschlaege, "urspruenglicher_mietbeginn")}
          <label>Verwaltungsübernahme durch JLB/7DI am</label>
          <input type="date" name="verwaltungsuebernahme_am" value="{h(str(werte.get('verwaltungsuebernahme_am') or ''))}">
          <label>Verwaltung (Bezeichnung, falls abweichend)</label>
          <input name="verwaltung_bezeichnung" value="{h(str(werte.get('verwaltung_bezeichnung') or ''))}">
        </fieldset>
        <fieldset><legend>Kaution</legend>
          <label>Vereinbarter Betrag laut Vertrag (KEIN Zahlungsbeleg)</label>
          <input name="vertragliche_kaution_cent" value="{h(str(werte.get('vertragliche_kaution_cent') or ''))}" placeholder="z. B. 1.500,00">
          {_beleg_hinweis(vorschlaege, "vertragliche_kaution_cent")}
          <label>Fundstelle im Vertrag</label>
          <input name="vertragliche_kaution_quellenbeleg" value="{h(str(werte.get('vertragliche_kaution_quellenbeleg') or ''))}">
          <p class="muted">Tatsächlich eingegangene Kaution getrennt unten erfassen - niemals automatisch gleichgesetzt.</p>
          {_kaution_eingang_feld(werte, kaution_bereits_vorhanden)}
        </fieldset>
        <fieldset><legend>Mahngebühren</legend>
          <p class="muted">Leer = unbekannt/kein Fund. Nur bei ausdrücklich belegter Klausel "0" für
             "keine Gebühr vereinbart" eintragen - niemals automatisch verrechnet oder verzinst.</p>
          <label>Mahngebühr laut Vertrag</label>
          <input name="mahngebuehr_cent" value="{h(str(werte.get('mahngebuehr_cent') or ''))}">
          {_beleg_hinweis(vorschlaege, "mahngebuehr_cent")}
          <label>Fundstelle/Klausel</label>
          <input name="mahngebuehr_quellenbeleg" value="{h(str(werte.get('mahngebuehr_quellenbeleg') or ''))}">
        </fieldset>
        <fieldset><legend>Index-Quellfelder (ausdrücklich unverbindlich)</legend>
          <p class="muted">Reine Gedächtnisstütze für die spätere Anlage der tatsächlichen Indexklausel unter
             "Indexregel" - erzeugt hier KEINE aktive Klausel, keine Freigabe, keine Sollstellung.</p>
          <label>Indexreihe laut Vertrag</label>
          <input name="index_reihe" value="{h(str(werte.get('index_reihe') or ''))}">
          {_beleg_hinweis(vorschlaege, "index_reihe")}
          <label>Ursprünglicher vertraglicher Basismonat (JJJJ-MM)</label>
          <input type="month" name="index_urspruenglicher_basismonat" value="{h(str(werte.get('index_urspruenglicher_basismonat') or ''))}">
          {_dezimal_feld("index_urspruenglicher_basiswert", "Ursprünglicher vertraglicher Basiswert", werte, vorschlaege)}
          {_dezimal_feld("index_schwelle_prozent", "Schwelle in Prozent", werte, vorschlaege)}
          <label>Schwelle bezogen auf</label><select name="index_schwelle_inklusive">{inklusive_optionen}</select>
          <label>Anpassungsmonat (1-12, falls fix vereinbart)</label>
          <input name="index_anpassungsmonat" value="{h(str(werte.get('index_anpassungsmonat') or ''))}">
          <label>Mindestabstand zwischen Anpassungen (Monate)</label>
          <input name="index_mindestintervall_monate" value="{h(str(werte.get('index_mindestintervall_monate') or ''))}">
          <label>Klauseltext (Auszug)</label>
          <textarea name="index_klauseltext_auszug" rows="3">{h(str(werte.get('index_klauseltext_auszug') or ''))}</textarea>
          <label>Seite im Dokument</label>
          <input name="index_klauseltext_seite" value="{h(str(werte.get('index_klauseltext_seite') or ''))}">
        </fieldset>
        <button type="submit">Vorschau anzeigen</button>
      </form>
      <p><a href="{h(zurueck_href)}">&larr; zurück</a></p>
    </div>"""


_STATUS_ANZEIGE = {
    "NEU": ("bereit (neu)", "badge-muted"),
    "UNVERAENDERT": ("bereit (unverändert)", "badge-muted"),
    "AKTUALISIERUNG": ("bereit (neue Version)", "badge-warn"),
    "KONFLIKT": ("Klärung erforderlich", "badge-error"),
    "GESPERRT": ("gesperrt", "badge-error"),
}


def vorschau_ansicht(*, ist_neu: bool, befunde: list, anwendbar: bool, hinweise: tuple[str, ...], paket_json: str, csrf: str, aktion_url: str, zurueck_href: str) -> str:
    zeilen = []
    for b in befunde:
        text, klasse = _STATUS_ANZEIGE.get(b.status, (b.status, "badge-muted"))
        grund = f'<br><span class="muted">{h(b.grund)}</span>' if b.grund else ""
        zeilen.append(f"<tr><td>{h(b.entitaet)}</td><td>{h(b.id)}</td><td><span class='badge {klasse}'>{h(text)}</span>{grund}</td></tr>")
    hinweisblock = "".join(f'<p class="muted">{h(x)}</p>' for x in hinweise)
    if anwendbar:
        bestaetigen = f"""
        <form method="post" action="{h(aktion_url)}">
          {csrf_feld(csrf)}
          <textarea name="paket_json" hidden>{h(paket_json)}</textarea>
          <button type="submit">Jetzt übernehmen (Stammdaten, keine Sollstellung/Mail/Lastschrift)</button>
        </form>"""
    else:
        bestaetigen = '<div class="flash-error">Es gibt ungeklärte Punkte (siehe oben) - es wird NICHTS übernommen, solange diese bestehen.</div>'
    return f"""
    <div class="card">
      <h1>Vorschau — {'Neuer Mietvertrag' if ist_neu else 'Mietvertragsprofil-Aktualisierung'}</h1>
      {hinweisblock}
      <table><tr><th>Bereich</th><th>ID</th><th>Status</th></tr>{''.join(zeilen)}</table>
      {bestaetigen}
      <p><a href="{h(zurueck_href)}">&larr; zurück zur Bearbeitung</a></p>
    </div>"""


def _eur_ohne_symbol(cent: int) -> str:
    """Wie `views.eur()`, aber OHNE " €"-Suffix - für die Formular-
    Vorbefüllung, damit ein unverändert abgeschicktes Feld wieder
    `parse_eur_betrag()`-kompatibel ist (das lehnt ein Suffix als
    mehrdeutig ab)."""

    return f"{cents_to_decimal(cent):,.2f}".replace(",", "§").replace(".", ",").replace("§", ".")


def bestehende_werte(profil, kaution) -> dict:
    """Übersetzt eine bereits gespeicherte `MietvertragsprofilTable`-Zeile
    (+ optionale `KautionTable`-Zeile) zurück in die vom Formular
    erwarteten String-Werte - Basis für die Bearbeiten-Ansicht eines
    BESTEHENDEN Vertrags (kein PDF-Upload nötig)."""

    werte: dict = {}
    if profil is not None:
        werte.update(dict(
            nutzungsart=profil.nutzungsart,
            urspruenglicher_mietbeginn=profil.urspruenglicher_mietbeginn.isoformat() if profil.urspruenglicher_mietbeginn else None,
            verwaltungsuebernahme_am=profil.verwaltungsuebernahme_am.isoformat() if profil.verwaltungsuebernahme_am else None,
            verwaltung_bezeichnung=profil.verwaltung_bezeichnung,
            vertragliche_kaution_cent=_eur_ohne_symbol(profil.vertragliche_kaution_cent) if profil.vertragliche_kaution_cent is not None else None,
            vertragliche_kaution_quellenbeleg=profil.vertragliche_kaution_quellenbeleg,
            mahngebuehr_cent=_eur_ohne_symbol(profil.mahngebuehr_cent) if profil.mahngebuehr_cent is not None else None,
            mahngebuehr_quellenbeleg=profil.mahngebuehr_quellenbeleg,
            index_reihe=profil.index_reihe,
            index_urspruenglicher_basismonat=profil.index_urspruenglicher_basismonat,
            index_urspruenglicher_basiswert=profil.index_urspruenglicher_basiswert,
            index_schwelle_prozent=profil.index_schwelle_prozent,
            index_schwelle_inklusive=("1" if profil.index_schwelle_inklusive is True else "0" if profil.index_schwelle_inklusive is False else ""),
            index_anpassungsmonat=profil.index_anpassungsmonat,
            index_mindestintervall_monate=profil.index_mindestintervall_monate,
            index_klauseltext_auszug=profil.index_klauseltext_auszug,
            index_klauseltext_seite=profil.index_klauseltext_seite,
        ))
    if kaution is not None:
        werte.update(dict(
            kaution_eingegangen_cent=eur(kaution.betrag_cent),
            kaution_eingegangen_stichtag=kaution.stichtag.isoformat(),
            kaution_eingegangen_referenz=kaution.referenz,
        ))
    return werte


def profil_werte_aus_form(form) -> dict:
    def text(name: str) -> str | None:
        wert = str(form.get(name, "")).strip()
        return wert or None

    def eur_cent(name: str) -> int | None:
        wert = text(name)
        if wert is None:
            return None
        return parse_eur_betrag(wert)

    def ganzzahl(name: str) -> int | None:
        wert = text(name)
        if wert is None:
            return None
        try:
            return int(wert)
        except ValueError:
            raise ValueError(f"'{name}' muss eine Ganzzahl sein.") from None

    def dezimal(name: str) -> Decimal | None:
        wert = text(name)
        if wert is None:
            return None
        try:
            return Decimal(wert.replace(",", "."))
        except InvalidOperation:
            raise ValueError(f"'{name}' ist keine gültige Dezimalzahl.") from None

    nutzungsart = text("nutzungsart") or "UNGEKLAERT"
    if nutzungsart not in NUTZUNGSARTEN:
        raise ValueError("Ungültige Nutzungsart.")

    inklusive_raw = text("index_schwelle_inklusive")
    index_schwelle_inklusive = {"1": True, "0": False}.get(inklusive_raw)

    basismonat = text("index_urspruenglicher_basismonat")
    if basismonat is not None:
        date.fromisoformat(basismonat + "-01")  # Formatprüfung, wirft bei ungültigem Wert

    return dict(
        nutzungsart=nutzungsart,
        urspruenglicher_mietbeginn=text("urspruenglicher_mietbeginn"),
        verwaltungsuebernahme_am=text("verwaltungsuebernahme_am"),
        verwaltung_bezeichnung=text("verwaltung_bezeichnung"),
        vertragliche_kaution_cent=eur_cent("vertragliche_kaution_cent"),
        vertragliche_kaution_quellenbeleg=text("vertragliche_kaution_quellenbeleg"),
        mahngebuehr_cent=eur_cent("mahngebuehr_cent"),
        mahngebuehr_quellenbeleg=text("mahngebuehr_quellenbeleg"),
        index_reihe=text("index_reihe"),
        index_urspruenglicher_basismonat=basismonat,
        index_urspruenglicher_basiswert=dezimal("index_urspruenglicher_basiswert"),
        index_schwelle_prozent=dezimal("index_schwelle_prozent"),
        index_schwelle_inklusive=index_schwelle_inklusive,
        index_anpassungsmonat=ganzzahl("index_anpassungsmonat"),
        index_mindestintervall_monate=ganzzahl("index_mindestintervall_monate"),
        index_klauseltext_auszug=text("index_klauseltext_auszug"),
        index_klauseltext_seite=ganzzahl("index_klauseltext_seite"),
        kaution_eingegangen_cent=eur_cent("kaution_eingegangen_cent"),
        kaution_eingegangen_stichtag=text("kaution_eingegangen_stichtag"),
        kaution_eingegangen_referenz=text("kaution_eingegangen_referenz"),
    )


# ---------------------------------------------------------------------------
# Detailansicht
# ---------------------------------------------------------------------------


def detail_ansicht(
    *, vertrag, objekt, einheit, debitor, gesellschaft, profil, kaution, konto_id, komponenten,
    rechtsprofil_hinweis: str | None, index_klausel_hinweis: str | None, versionen: list, csrf: str,
    gesperrt: bool = False,
    vorschreibungen: list | None = None,
    vorschreibung_summen: dict[int, int] | None = None,
    kontostatus=None,
    unbekannte_faelligkeit_positionen: list | None = None,
    zahlungen_positionen: list | None = None,
    sperren: list | None = None,
    mahnfaelle: list | None = None,
    letzte_pruefung_hinweis: str | None = None,
) -> str:
    """Mieterakte (Auftrag HV-20260914-AUFGABEN-MIETERAKTE): EIN
    gemeinsamer, gegliederter Einstieg. Alle neuen Abschnitte lesen
    AUSSCHLIESSLICH bereits bestehende, andernorts geprüfte Quellen -
    keine neue Berechnung, keine neue juristische/Index-Bewertung.
    Fehlende Daten werden ehrlich als "nicht hinterlegt"/"kein Nachweis
    im System" ausgewiesen, nie als erfundener Wert (bezahlt/zugestellt/
    0,00). `gesperrt` (Objekt von der Pilotphase ausgeschlossen) blendet
    NUR die live berechneten Abschnitte (Kontostatus/offene Positionen/
    Mahnfälle) aus - Stammdaten bleiben wie beim bestehenden Kontoauszug
    sichtbar, aber es wird für ein ausgeschlossenes Objekt NIE eine
    Finanzzahl über diese neue, gemeinsame Akte "eingeschleust"."""

    nutzungsart = profil.nutzungsart if profil else "UNGEKLAERT"
    nutzungsart_status = feld_status(nutzungsart, unklar_werte=("UNGEKLAERT",))
    rechtsordnung_status = feld_status(vertrag.rechtsordnung, unklar_werte=("UNGEKLAERT",))
    mahngebuehr_status = "bereit" if (profil and profil.mahngebuehr_cent is not None) else "Angabe fehlt"
    kaution_vereinbart_status = "bereit" if (profil and profil.vertragliche_kaution_cent is not None) else "Angabe fehlt"
    kaution_eingegangen_status = "bereit" if kaution is not None else "Angabe fehlt"

    # `VertragsKomponenteTable.betrag_cent` ist per Definition BRUTTO
    # (siehe `domain/money.py::zerlege_brutto_cent`-Docstring) - der
    # gebuchte/vorgeschriebene Betrag bleibt unverändert; Netto/USt werden
    # NUR für den Ausweis über denselben kanonischen Helper zerlegt, den
    # auch die Vorschreibung selbst verwendet (kein eigener, zweiter
    # Rechenweg). `ust_satz_promille` ist ein Promille-Wert (10000 =
    # 100 Promille der Basis = 10 %) - die Prozentanzeige teilt daher
    # durch 1000, NICHT durch 100.
    komponenten_zeilen = []
    brutto_summe_cent = 0
    netto_summe_cent = 0
    ust_summe_cent = 0
    for k in komponenten:
        netto_cent, ust_cent = zerlege_brutto_cent(k.betrag_cent, k.ust_satz_promille)
        brutto_summe_cent += k.betrag_cent
        netto_summe_cent += netto_cent
        ust_summe_cent += ust_cent
        prozent_text = f"{k.ust_satz_promille / 1000:.1f}".replace(".", ",")
        komponenten_zeilen.append(
            f"<tr><td>{h(k.art)}</td><td>{h(k.bezeichnung)}</td>"
            f"<td>{eur(netto_cent)}</td><td>{eur(ust_cent)}</td><td>{eur(k.betrag_cent)}</td>"
            f"<td>{prozent_text} %</td></tr>"
        )
    komponenten_leer = not komponenten
    komponenten_zeilen = "".join(komponenten_zeilen) or "<tr><td colspan=6 class='muted'>Keine aktiven Mietbestandteile hinterlegt.</td></tr>"

    versionen_zeilen = "".join(
        f"<tr><td>{v.version}</td><td>{h(v.quelle_typ)}</td><td>{h(v.quelle_referenz or '')}</td>"
        f"<td>{h(v.erstellt_von)}</td><td>{v.erstellt_am.strftime('%Y-%m-%d %H:%M') if v.erstellt_am else ''}</td></tr>"
        for v in versionen
    ) or "<tr><td colspan=5>Noch keine Version gespeichert.</td></tr>"

    konto_link = f'<a href="/backoffice/konto/{h(konto_id)}">Mietkonto</a> · ' if konto_id else '<span class="muted">Mietkonto (noch keine Eröffnung)</span> · '
    gesperrt_banner = (
        '<div class="flash-error">Objekt ist von der Pilotphase ausgeschlossen - Kontostatus, offene Positionen '
        'und Mahnfälle werden hier nicht angezeigt.</div>' if gesperrt else ""
    )

    # -- Aktuelle Vorschreibung (Auftrag HV-20260914-AUFGABEN-MIETERAKTE, --
    # -- Ergänzung Codex-Bestandsprüfung): tatsächlich PERSISTIERTE --------
    # -- Vorschreibungsdatensätze, NICHT die vereinbarten Komponenten. -----
    vorschreibungen = vorschreibungen or []
    vorschreibung_summen = vorschreibung_summen or {}
    _VORSCHREIBUNG_STATUS_LABEL = {
        "ENTWURF": "Entwurf (noch nicht sollgestellt)",
        "SOLLGESTELLT": "sollgestellt",
        "ZUGESTELLT": "zugestellt",
        "EXPORTIERT": "zugestellt, ins Hauptbuch exportiert",
    }
    if vorschreibungen:
        aktuelle_v = vorschreibungen[0]
        weitere_v = vorschreibungen[1:]
        weitere_v_zeilen = "".join(
            f"<tr><td>{h(v.monat)}</td><td>{eur(vorschreibung_summen.get(v.id, 0))}</td>"
            f"<td>{h(_VORSCHREIBUNG_STATUS_LABEL.get(v.status, v.status))}</td></tr>"
            for v in weitere_v
        )
        vorschreibung_karte = f"""
    <div class="card"><h2>Aktuelle Vorschreibung</h2>
      <table>
        <tr><th>Monat</th><td>{h(aktuelle_v.monat)}</td></tr>
        <tr><th>Betrag (belegte Positionen)</th><td>{eur(vorschreibung_summen.get(aktuelle_v.id, 0))}</td></tr>
        <tr><th>Status</th><td>{h(_VORSCHREIBUNG_STATUS_LABEL.get(aktuelle_v.status, aktuelle_v.status))}</td></tr>
        <tr><th>Fälligkeit</th><td>{aktuelle_v.faelligkeit.isoformat() if aktuelle_v.faelligkeit else '—'}</td></tr>
        <tr><th>Dokument zugestellt am</th>
            <td>{aktuelle_v.dokument_zugestellt_am.strftime('%Y-%m-%d') if aktuelle_v.dokument_zugestellt_am else 'kein Nachweis im System'}</td></tr>
      </table>
      {f'<details><summary>Weitere Monate ({len(weitere_v)})</summary><table><tr><th>Monat</th><th>Betrag</th><th>Status</th></tr>{weitere_v_zeilen}</table></details>' if weitere_v else ''}
      <p><a href="/backoffice/vertrag/{h(vertrag.id)}/vorschreibung">Vorschreibungsentwurf öffnen</a></p>
    </div>"""
    else:
        vorschreibung_karte = f"""
    <div class="card"><h2>Aktuelle Vorschreibung</h2>
      <p class="muted">Keine Vorschreibung im System hinterlegt - das sagt nichts darüber aus, ob vor Verwaltungs-
         übernahme extern vorgeschrieben wurde, nur dass hier keine erfasst ist.</p>
      <p><a href="/backoffice/vertrag/{h(vertrag.id)}/vorschreibung">Vorschreibungsentwurf öffnen</a></p>
    </div>"""

    # -- Kontostatus (id="kontodetails") - dieselbe, bereits geprüfte ------
    # -- Berechnung wie Übersicht/Dashboard (rueckstaende/service.py), ------
    # -- KEIN zweiter Rechenweg. -------------------------------------------
    if gesperrt:
        kontostatus_karte = ""
    elif kontostatus is None:
        kontostatus_karte = """
    <div class="card" id="kontodetails"><h2>Offener Betrag &amp; Kontostatus</h2>
      <p class="muted">Kein Mietkonto vorhanden.</p>
    </div>"""
    else:
        abweichung_zeile = (
            f"<tr><th>Abweichung Kontostand/Einzelpositionen</th><td>{eur(kontostatus.abweichung_saldo_zu_positionen_cent)}"
            " <span class='muted'>(z. B. eine Korrekturbuchung ohne eigene offene Position)</span></td></tr>"
            if kontostatus.abweichung_saldo_zu_positionen_cent else ""
        )
        kontostatus_karte = f"""
    <div class="card" id="kontodetails"><h2>Offener Betrag &amp; Kontostatus</h2>
      <table>
        <tr><th>Kontostand (offen/Guthaben)</th><td>{eur(kontostatus.saldo_cent) if kontostatus.saldo_cent is not None else '—'}</td></tr>
        <tr><th>Davon fällig (Kontoberechnung)</th><td>{eur(kontostatus.faelliger_unstrittiger_rest_cent) if kontostatus.faelliger_unstrittiger_rest_cent is not None else '—'}</td></tr>
        {abweichung_zeile}
      </table>
      <p class="muted">Eine bekannte Fälligkeit ist KEINE Mahnfreigabe - eine aktive Sperre (unten) blockiert
         unabhängig davon.</p>
    </div>"""

    # -- Offene Positionen & Zahlungen (id="zahlungen") ---------------------
    unbekannte_faelligkeit_positionen = unbekannte_faelligkeit_positionen or []
    zahlungen_positionen = zahlungen_positionen or []
    if gesperrt:
        zahlungen_karte = ""
    else:
        unbekannt_html = ""
        if unbekannte_faelligkeit_positionen:
            unbekannt_zeilen = "".join(
                f"<tr><td>{h(p.beleg_referenz)}</td><td>{h(p.art)}</td><td>{eur(p.rest_cent)}</td>"
                f"<td>{p.belegdatum.isoformat()}</td></tr>"
                for p in unbekannte_faelligkeit_positionen
            )
            unbekannt_html = f"""
      <p class="warn">Offene Position(en) ohne erfasste Fälligkeit:</p>
      <table><tr><th>Beleg</th><th>Art</th><th>Rest</th><th>Belegdatum</th></tr>{unbekannt_zeilen}</table>"""
        zahlungen_zeilen = "".join(
            f"<tr><td>{p.belegdatum.isoformat()}</td><td>{h(p.typ)}</td><td>{eur(p.betrag_cent)}</td>"
            f"<td>{h(p.beleg_referenz or '')}</td></tr>"
            for p in zahlungen_positionen
        )
        zahlungen_karte = f"""
    <div class="card" id="zahlungen"><h2>Offene Positionen &amp; Zahlungen/Buchungen</h2>
      {unbekannt_html}
      <details {"open" if not unbekannte_faelligkeit_positionen else ""}>
        <summary>Alle Buchungen ({len(zahlungen_positionen)})</summary>
        <table><tr><th>Datum</th><th>Typ</th><th>Betrag</th><th>Beleg/Quelle</th></tr>
        {zahlungen_zeilen or '<tr><td colspan=4 class="muted">Keine Buchungen.</td></tr>'}</table>
      </details>
      {f'<p><a href="/backoffice/konto/{h(konto_id)}">Vollständigen Kontoauszug öffnen</a></p>' if konto_id else ''}
    </div>"""

    # -- Sperren (id="sperren") - reine Statusanzeige, KEINE ----------------
    # -- Aufforderung zum Entsperren (Rückprüfung 14.09.2026). --------------
    sperren = sperren or []
    sperren_zeilen = "".join(
        f"<tr><td>{h(s.grund)}</td><td>{s.gesetzt_am.strftime('%Y-%m-%d') if s.gesetzt_am else ''}</td>"
        f"<td>{h(s.kommentar or '')}</td></tr>"
        for s in sperren
    )
    sperren_karte = f"""
    <div class="card" id="sperren"><h2>Sperren</h2>
      {f'<table><tr><th>Grund</th><th>Gesetzt am</th><th>Kommentar</th></tr>{sperren_zeilen}</table><p class="muted">Aktive Sperre(n) - vor einem Mahnlauf beachten, keine Aufforderung zum Entsperren.</p>' if sperren else '<p class="muted">Keine aktiven Sperren.</p>'}
    </div>"""

    # -- Mahnfälle (id="mahnfaelle") -----------------------------------------
    mahnfaelle = mahnfaelle or []
    mahnfaelle_zeilen = "".join(
        f"<tr><td>#{m.forderung_op_position_id}</td><td>{m.stufe}</td><td>{h(m.status)}</td>"
        f"<td>{eur(m.betrag_cent)}</td><td>{m.geplant_am.date().isoformat()}</td></tr>"
        for m in mahnfaelle
    )
    mahnfaelle_karte = "" if gesperrt else f"""
    <div class="card" id="mahnfaelle"><h2>Mahnfälle &amp; Schriftverkehr</h2>
      {f'<table><tr><th>OP-Nr.</th><th>Stufe</th><th>Status</th><th>Fallbetrag</th><th>Geplant am</th></tr>{mahnfaelle_zeilen}</table>' if mahnfaelle else '<p class="muted">Keine Mahnfälle im System hinterlegt.</p>'}
      <p><a href="/backoffice/vertrag/{h(vertrag.id)}/mahnvorschau">Mahnvorschau öffnen</a></p>
    </div>"""

    return f"""
    {gesperrt_banner}
    <div class="card">
      <h1>{h(debitor.name)}</h1>
      <p class="muted">{h(objekt.bezeichnung)} / {h(einheit.bezeichnung)} · Vertrag <code>{h(vertrag.id)}</code></p>
      <p>{konto_link}
         <a href="/backoffice/vertrag/{h(vertrag.id)}/mahnvorschau">Mahnvorschau</a> ·
         <a href="/backoffice/vertrag/{h(vertrag.id)}/indexklauseln">Indexregel</a> ·
         <a href="/backoffice/vertrag/{h(vertrag.id)}/mieweg-vorschau">Rekonstruktionsmodell (Mietzinsobergrenze)</a></p>
      <a href="/backoffice/vertrag/{h(vertrag.id)}/mietvertragsprofil/bearbeiten"><button type="button">Profil aktualisieren</button></a>
    </div>
    <div class="card"><h2>Mieter und Objekt</h2>
      <table>
        <tr><th>Mieter</th><td>{h(debitor.name)}{' — ' + h(debitor.email) if debitor.email else ''}</td></tr>
        <tr><th>Kontakt</th><td>{h(debitor.adresse) if debitor.adresse else 'nicht hinterlegt'}</td></tr>
        <tr><th>Vermieter-Gesellschaft</th><td>{h(gesellschaft.name)}</td></tr>
        <tr><th>Verwaltung</th><td>{h(profil.verwaltung_bezeichnung) if profil and profil.verwaltung_bezeichnung else '—'}
            {_status_badge('bereit' if (profil and profil.verwaltung_bezeichnung) else 'Angabe fehlt')}</td></tr>
        <tr><th>Objekt / Einheit</th><td>{h(objekt.bezeichnung)} / {h(einheit.bezeichnung)}
            (<span title="Nutzungsstatus der Einheit">{h(einheit.nutzungsstatus)}</span>)</td></tr>
        <tr><th>Nutzung (laut Profil)</th><td>{h(nutzungsart)} {_status_badge(nutzungsart_status)}</td></tr>
        <tr><th>Rechtsordnung (MRG)</th><td>{h(vertrag.rechtsordnung)} {_status_badge(rechtsordnung_status)}</td></tr>
      </table>
    </div>
    <div class="card"><h2>Laufzeit</h2>
      <table>
        <tr><th>Vertragsbeginn</th><td>{vertrag.gueltig_von.isoformat()}</td></tr>
        <tr><th>Vertragsende</th><td>{vertrag.gueltig_bis.isoformat() if vertrag.gueltig_bis else 'unbefristet'}</td></tr>
        <tr><th>Ursprünglicher tatsächlicher Mietbeginn</th>
            <td>{profil.urspruenglicher_mietbeginn.isoformat() if profil and profil.urspruenglicher_mietbeginn else '—'}
            {_status_badge('bereit' if (profil and profil.urspruenglicher_mietbeginn) else 'Angabe fehlt')}</td></tr>
        <tr><th>Verwaltungsübernahme</th>
            <td>{profil.verwaltungsuebernahme_am.isoformat() if profil and profil.verwaltungsuebernahme_am else '—'}
            {_status_badge('bereit' if (profil and profil.verwaltungsuebernahme_am) else 'Angabe fehlt')}</td></tr>
      </table>
    </div>
    <div class="card"><h2>Vereinbarte Mietbestandteile (aktiv)</h2>
      <p class="muted">Vereinbarte Beträge laut aktiven Vertragskomponenten - KEIN Nachweis einer tatsächlichen
         Sollstellung/Buchung. Tatsächlich vorgeschriebene/gebuchte Beträge siehe "Aktuelle Vorschreibung" und
         "Zahlungen/Buchungen" unten.</p>
      <table><tr><th>Art</th><th>Bezeichnung</th><th>Netto</th><th>USt</th><th>Brutto</th><th>USt-Satz</th></tr>{komponenten_zeilen}</table>
      {f'<p><strong>Summe netto: {eur(netto_summe_cent)}</strong> · Summe USt: {eur(ust_summe_cent)} · <strong>Summe brutto (vereinbart): {eur(brutto_summe_cent)}</strong></p>' if not komponenten_leer else ''}
    </div>
    {vorschreibung_karte}
    {kontostatus_karte}
    {zahlungen_karte}
    {sperren_karte}
    {mahnfaelle_karte}
    <div class="card"><h2>Kaution und Mahngebühren</h2>
      <table>
        <tr><th>Kaution vereinbart (laut Vertrag)</th>
            <td>{eur(profil.vertragliche_kaution_cent) if profil and profil.vertragliche_kaution_cent is not None else '—'}
            {_status_badge(kaution_vereinbart_status)}</td></tr>
        <tr><th>Kaution eingegangen (bestätigt)</th>
            <td>{eur(kaution.betrag_cent) if kaution else '—'} {_status_badge(kaution_eingegangen_status)}</td></tr>
        <tr><th>Mahngebühr laut Vertrag</th>
            <td>{eur(profil.mahngebuehr_cent) if profil and profil.mahngebuehr_cent is not None else '—'}
            {_status_badge(mahngebuehr_status)}
            {' <span class="muted">(0 = ausdrücklich keine Gebühr vereinbart)</span>' if profil and profil.mahngebuehr_cent == 0 else ''}</td></tr>
      </table>
    </div>
    <div class="card"><h2>Index-Quellfelder (unverbindlich)</h2>
      <p class="muted">Reine Gedächtnisstütze - erzeugt keine aktive Klausel. Die tatsächlich wirksame Indexklausel
         steht getrennt unter "Indexregel" oben.</p>
      <table>
        <tr><th>Reihe</th><td>{h(profil.index_reihe) if profil and profil.index_reihe else '—'}</td></tr>
        <tr><th>Basismonat</th><td>{h(profil.index_urspruenglicher_basismonat) if profil and profil.index_urspruenglicher_basismonat else '—'}</td></tr>
        <tr><th>Basiswert</th><td>{profil.index_urspruenglicher_basiswert if profil and profil.index_urspruenglicher_basiswert is not None else '—'}</td></tr>
        <tr><th>Schwelle</th><td>{f"{profil.index_schwelle_prozent} % ({'ab' if profil.index_schwelle_inklusive else 'über'})" if profil and profil.index_schwelle_prozent is not None else '—'}</td></tr>
        <tr><th>Anpassungsmonat</th><td>{profil.index_anpassungsmonat if profil and profil.index_anpassungsmonat else '—'}</td></tr>
        <tr><th>Mindestabstand (Monate)</th><td>{profil.index_mindestintervall_monate if profil and profil.index_mindestintervall_monate else '—'}</td></tr>
      </table>
    </div>
    <details class="card"><summary>Quellen und Historie</summary>
      <p class="muted">Technischer Vertragsbeginn = für Sollstellung/Buchung maßgebliches Feld
         (<code>{h(vertrag.id)}</code>, Gesellschaft <code>{h(vertrag.gesellschaft_id)}</code>) - kann bei
         Altobjekten von der Verwaltungsübernahme abweichen (siehe oben, "Ursprünglicher tatsächlicher Mietbeginn").
         "Quelle" (PDF-Extraktion/manuell/Importformat) ist ein Herkunftsvermerk, KEINE fachliche Freigabe.</p>
      <h3>Freigegebenes Rechtsprofil</h3><p>{h(rechtsprofil_hinweis) if rechtsprofil_hinweis else 'Kein Rechtsprofil freigegeben.'}</p>
      <h3>Indexklausel</h3><p>{h(index_klausel_hinweis) if index_klausel_hinweis else 'Keine Indexklausel erfasst.'}</p>
      <h3>Letzte Vertragsprüfung</h3><p>{h(letzte_pruefung_hinweis) if letzte_pruefung_hinweis else 'Keine Vertragsprüfung im System hinterlegt.'}</p>
      <h3>Mietvertragsprofil-Versionen</h3>
      <table><tr><th>Version</th><th>Quelle</th><th>Referenz</th><th>Erfasst von</th><th>Am</th></tr>{versionen_zeilen}</table>
    </details>"""
