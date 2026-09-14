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

from mietinkasso.backoffice.views import csrf_feld, eur, option, parse_eur_betrag, sperrgrund_label
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


_VORSCHREIBUNG_STATUS_LABEL = {
    "ENTWURF": "Entwurf (noch nicht sollgestellt)",
    "SOLLGESTELLT": "sollgestellt",
    "ZUGESTELLT": "zugestellt",
    "EXPORTIERT": "zugestellt, ins Hauptbuch exportiert",
}


def detail_ansicht(
    *, vertrag, objekt, einheit, debitor, gesellschaft, profil, kaution, konto_id, komponenten,
    rechtsprofil_freigegeben_hinweis: str | None, rechtsprofil_entwurf_hinweis: str | None,
    index_klausel_hinweis: str | None, index_pruefbedarf_hinweis: str | None,
    letzter_indexautomatik_lauf_hinweis: str | None,
    letzte_pruefung_hinweis: str | None, versionen: list, csrf: str,
    aktueller_monat: str,
    vorschreibungen: list,
    vorschreibung_positionen_je_id: dict,
    kontostatus,
    unbekannte_faelligkeit_positionen: list,
    zahlungen_positionen: list,
    sperren: list,
    mahnfaelle: list,
    mahnlaeufe: list,
    erhoehungsschreiben: list,
    vertragsende_erinnerungen: list,
    von_objekt: str | None = None,
) -> str:
    """Mieterakte (Auftrag HV-20260914-AUFGABEN-MIETERAKTE, Rückprüfung
    Codex 14.09.2026): EIN gemeinsamer, gegliederter Einstieg für NICHT
    ausgeschlossene Objekte - der Aufruf für ein Pilotausschluss-Objekt
    wird bereits VOR diesem Renderer in der Route mit einer eigenen
    Fehlerseite abgefangen (`app.py::vertragsanlage_detail`), daher gibt
    es hier keinen `gesperrt`-Zweig mehr und NIE eine nur selektiv
    unterdrückte Finanzzahl. Alle Abschnitte lesen AUSSCHLIESSLICH
    bereits bestehende, andernorts geprüfte Quellen - keine neue
    Berechnung, keine neue juristische/Index-Bewertung. Fehlende Daten
    werden ehrlich als "nicht hinterlegt"/"kein Nachweis im System"/
    "kein Beleg" ausgewiesen, nie als erfundener Wert (0,00/bezahlt/
    zugestellt)."""

    zurueck_html = (
        f'<a href="/backoffice/?objekt_id={h(von_objekt)}">&larr; zurück zur gefilterten Übersicht</a>'
        if von_objekt else '<a href="/backoffice/vertraege">&larr; zurück zur Mieterliste</a>'
    )

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

    # -- Vorschreibung: exakter aktueller Monat vorrangig; ohne Treffer ----
    # -- wird der letzte VERGANGENE Monat ausdrücklich als "historisch" ----
    # -- ausgewiesen, ein künftig hinterlegter Entwurf NIE als "aktuell" ---
    # -- (Codex-Rückprüfung: bloße DESC-Sortierung wählte vorher den am ----
    # -- weitesten in der Zukunft liegenden Datensatz als "aktuell", und ---
    # -- eine leere Positionsliste wurde als belegte 0,00-Summe gezeigt). --
    vorschreibungen = vorschreibungen or []
    vorschreibung_positionen_je_id = vorschreibung_positionen_je_id or {}

    def _positionen_tabelle(v):
        positionen = vorschreibung_positionen_je_id.get(v.id) or []
        if not positionen:
            return None, '<p class="muted">Keine Einzelpositionen zu diesem Datensatz hinterlegt - der Betrag ist NICHT belegt (kein "0,00" als Beleg).</p>'
        zeilen = []
        summe = 0
        for p in positionen:
            netto_cent, ust_cent = zerlege_brutto_cent(p.betrag_cent, p.ust_satz_promille)
            summe += p.betrag_cent
            zeilen.append(
                f"<tr><td>{h(p.art)}</td><td>{h(p.bezeichnung)}</td><td>{eur(netto_cent)}</td>"
                f"<td>{eur(ust_cent)}</td><td>{eur(p.betrag_cent)}</td></tr>"
            )
        tabelle = (
            "<table><tr><th>Art</th><th>Bezeichnung</th><th>Netto</th><th>USt</th><th>Brutto</th></tr>"
            f"{''.join(zeilen)}</table>"
        )
        return summe, tabelle

    _je_vorschreibung = {v.id: _positionen_tabelle(v) for v in vorschreibungen}
    vergangene_oder_aktuelle = [v for v in vorschreibungen if v.monat <= aktueller_monat]
    zukuenftige = [v for v in vorschreibungen if v.monat > aktueller_monat]
    primaer = vergangene_oder_aktuelle[0] if vergangene_oder_aktuelle else None
    ist_aktueller_monat = primaer is not None and primaer.monat == aktueller_monat
    weitere_vergangene = vergangene_oder_aktuelle[1:]
    primaer_summe, primaer_tabelle = _je_vorschreibung[primaer.id] if primaer is not None else (None, None)

    if primaer is not None:
        primaer_status_label = h(_VORSCHREIBUNG_STATUS_LABEL.get(primaer.status, primaer.status))
        titel = (
            f"Aktuelle Vorschreibung ({h(primaer.monat)}) — {primaer_status_label}" if ist_aktueller_monat
            else f"Letzte hinterlegte Vorschreibung — historisch ({h(primaer.monat)}) — {primaer_status_label}"
        )
        monat_hinweis = (
            "" if ist_aktueller_monat
            else f'<p class="warn">Kein Datensatz für den laufenden Monat ({h(aktueller_monat)}) hinterlegt - '
                 f"dies ist der letzte vorhandene, bereits VERGANGENE Monat.</p>"
        )
        betrag_zeile = (
            f"<tr><th>Betrag (belegte Positionen)</th><td>{eur(primaer_summe)}</td></tr>" if primaer_summe is not None
            else '<tr><th>Betrag</th><td class="muted">kein Beleg - keine Einzelpositionen hinterlegt</td></tr>'
        )
        weitere_v_zeilen = "".join(
            f"<tr><td>{h(v.monat)}</td>"
            f"<td>{eur(_je_vorschreibung[v.id][0]) if _je_vorschreibung[v.id][0] is not None else 'kein Beleg'}</td>"
            f"<td>{h(_VORSCHREIBUNG_STATUS_LABEL.get(v.status, v.status))}</td></tr>"
            for v in weitere_vergangene
        )
        zukunft_zeilen = "".join(
            f"<tr><td>{h(v.monat)}</td><td>{h(_VORSCHREIBUNG_STATUS_LABEL.get(v.status, v.status))}</td></tr>"
            for v in zukuenftige
        )
        vorschreibung_karte = f"""
    <div class="card"><h2>{titel}</h2>
      {monat_hinweis}
      <table>
        <tr><th>Monat</th><td>{h(primaer.monat)}</td></tr>
        {betrag_zeile}
        <tr><th>Status</th><td>{h(_VORSCHREIBUNG_STATUS_LABEL.get(primaer.status, primaer.status))}</td></tr>
        <tr><th>Fälligkeit</th><td>{primaer.faelligkeit.isoformat() if primaer.faelligkeit else '—'}</td></tr>
        <tr><th>Dokument zugestellt am</th>
            <td>{primaer.dokument_zugestellt_am.strftime('%Y-%m-%d') if primaer.dokument_zugestellt_am else 'kein Nachweis im System'}</td></tr>
      </table>
      <h3>Einzelbestandteile dieser Vorschreibung</h3>
      {primaer_tabelle}
      {f'<details><summary>Weitere vergangene Monate ({len(weitere_vergangene)})</summary><table><tr><th>Monat</th><th>Betrag</th><th>Status</th></tr>{weitere_v_zeilen}</table></details>' if weitere_vergangene else ''}
      {f'<details><summary>Zukünftig terminierte Vorschreibungen ({len(zukuenftige)}) — NICHT aktuell, Status siehe Spalte</summary><table><tr><th>Monat</th><th>Status</th></tr>{zukunft_zeilen}</table></details>' if zukuenftige else ''}
      <p><a href="/backoffice/vertrag/{h(vertrag.id)}/vorschreibung">Vorschreibungsentwurf öffnen</a></p>
    </div>"""
    else:
        zukunft_zeilen = "".join(
            f"<tr><td>{h(v.monat)}</td><td>{h(_VORSCHREIBUNG_STATUS_LABEL.get(v.status, v.status))}</td></tr>"
            for v in zukuenftige
        )
        zukunft_hinweis = (
            f'<p class="warn">Keine aktuelle/vergangene Vorschreibung, aber {len(zukuenftige)} zukünftig '
            "terminierte(r) Vorschreibungsdatensatz/-sätze (Status siehe Tabelle unten) - NICHT als bereits "
            "wirksam behandeln.</p>"
            f'<details open><summary>Zukünftig terminierte Vorschreibungen ({len(zukuenftige)})</summary>'
            f"<table><tr><th>Monat</th><th>Status</th></tr>{zukunft_zeilen}</table></details>"
            if zukuenftige else
            '<p class="muted">Keine Vorschreibung im System hinterlegt - das sagt nichts darüber aus, ob vor '
            "Verwaltungsübernahme extern vorgeschrieben wurde, nur dass hier keine erfasst ist.</p>"
        )
        vorschreibung_karte = f"""
    <div class="card"><h2>Aktuelle Vorschreibung</h2>
      {zukunft_hinweis}
      <p><a href="/backoffice/vertrag/{h(vertrag.id)}/vorschreibung">Vorschreibungsentwurf öffnen</a></p>
    </div>"""

    # -- Ledger-only SOLL-Buchungen ohne eigenen Vorschreibungsdatensatz ---
    # -- (z. B. Altimport/manuelle Nachbuchung) - eine solche Buchung ist --
    # -- eine ECHTE gebuchte Vorschreibung, auch ohne VorschreibungTable. --
    bekannte_monate = {v.monat for v in vorschreibungen}
    ledger_nur_gebucht: dict[str, int] = {}
    for p in zahlungen_positionen:
        if p.typ == "SOLL" and p.leistungsperiode and p.leistungsperiode not in bekannte_monate:
            ledger_nur_gebucht[p.leistungsperiode] = ledger_nur_gebucht.get(p.leistungsperiode, 0) + p.betrag_cent
    if ledger_nur_gebucht:
        ledger_zeilen = "".join(
            f"<tr><td>{h(periode)}</td><td>{eur(betrag)}</td></tr>"
            for periode, betrag in sorted(ledger_nur_gebucht.items(), reverse=True)
        )
        vorschreibung_karte += f"""
    <div class="card"><h2>Zusätzlich im Konto gebuchte SOLL-Perioden</h2>
      <p class="muted">Tatsächlich als SOLL gebucht (z. B. Altimport/manuelle Nachbuchung), aber OHNE eigenen
         Vorschreibungsdatensatz oben - trotzdem eine echte, gebuchte Vorschreibung, kein bloßer Entwurf.</p>
      <table><tr><th>Leistungsperiode</th><th>Betrag</th></tr>{ledger_zeilen}</table>
    </div>"""

    # -- Kontostatus & Buchungsvergleich (id="kontodetails") - dieselbe, ---
    # -- bereits geprüfte Berechnung wie Übersicht/Dashboard, KEIN zweiter -
    # -- Rechenweg; zeigt Kontoberechnung UND Summe Einzelpositionen -------
    # -- NEBENEINANDER, damit "Buchungen vergleichen" tatsächlich einen ----
    # -- Vergleich zeigt statt nur eine Summenkarte (Codex-Rückprüfung). ---
    if kontostatus is None:
        kontostatus_karte = """
    <div class="card" id="kontodetails"><h2>Offener Betrag &amp; Kontostatus</h2>
      <p class="muted">Kein Mietkonto vorhanden.</p>
    </div>"""
    else:
        abweichung_zeile = (
            f'<tr><th>Abweichung (Kontoberechnung minus Positionen)</th><td>{eur(kontostatus.abweichung_saldo_zu_positionen_cent)}'
            " <span class='muted'>(z. B. eine Korrekturbuchung ohne eigene offene Position)</span></td></tr>"
            if kontostatus.abweichung_saldo_zu_positionen_cent else ""
        )
        kontostatus_karte = f"""
    <div class="card" id="kontodetails"><h2>Offener Betrag &amp; Kontostatus</h2>
      <h3>Vergleich: Kontoberechnung ⟷ Summe Einzelpositionen</h3>
      <table>
        <tr><th></th><th>Kontoberechnung</th><th>Summe Einzelpositionen</th></tr>
        <tr><th>Offen gesamt</th>
            <td>{eur(max(kontostatus.saldo_cent, 0)) if kontostatus.saldo_cent is not None else '—'}</td>
            <td>{eur(kontostatus.positionen_rest_gesamt_cent) if kontostatus.positionen_rest_gesamt_cent is not None else '—'}</td></tr>
        <tr><th>Davon fällig</th>
            <td>{eur(kontostatus.faelliger_unstrittiger_rest_cent) if kontostatus.faelliger_unstrittiger_rest_cent is not None else '—'}</td>
            <td>{eur(kontostatus.positionen_faelliger_rest_cent) if kontostatus.positionen_faelliger_rest_cent is not None else '—'}</td></tr>
      </table>
      {f'<table>{abweichung_zeile}</table>' if abweichung_zeile else ''}
      <table>
        <tr><th>Kontostand (offen/Guthaben) — Kontoberechnung</th><td>{eur(kontostatus.saldo_cent) if kontostatus.saldo_cent is not None else '—'}</td></tr>
      </table>
      <p class="muted">Eine bekannte Fälligkeit ist KEINE Mahnfreigabe - eine aktive Sperre (unten) blockiert
         unabhängig davon.</p>
    </div>"""

    # -- Offene Positionen & Zahlungen (id="zahlungen") ---------------------
    unbekannte_faelligkeit_positionen = unbekannte_faelligkeit_positionen or []
    zahlungen_positionen = zahlungen_positionen or []
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
        f"<td>{h(p.leistungsperiode or '')}</td><td>{h(p.beleg_referenz or '')}</td></tr>"
        for p in zahlungen_positionen
    )
    zahlungen_karte = f"""
    <div class="card" id="zahlungen"><h2>Offene Positionen &amp; Zahlungen/Buchungen</h2>
      {unbekannt_html}
      <details {"open" if not unbekannte_faelligkeit_positionen else ""}>
        <summary>Alle Buchungen ({len(zahlungen_positionen)})</summary>
        <table><tr><th>Datum</th><th>Typ</th><th>Betrag</th><th>Periode</th><th>Beleg/Quelle</th></tr>
        {zahlungen_zeilen or '<tr><td colspan=5 class="muted">Keine Buchungen.</td></tr>'}</table>
      </details>
      {f'<p><a href="/backoffice/konto/{h(konto_id)}">Vollständigen Kontoauszug öffnen</a></p>' if konto_id else ''}
    </div>"""

    # -- Sperren (id="sperren") - reine Statusanzeige, KEINE ----------------
    # -- Aufforderung zum Entsperren (Rückprüfung 14.09.2026). --------------
    sperren = sperren or []
    sperren_zeilen = "".join(
        f"<tr><td>{h(sperrgrund_label(s.grund))} <span class='muted'>({h(s.grund)})</span></td>"
        f"<td>{s.gesetzt_am.strftime('%Y-%m-%d') if s.gesetzt_am else ''}</td>"
        f"<td>{h(s.kommentar or '')}</td></tr>"
        for s in sperren
    )
    sperren_karte = f"""
    <div class="card" id="sperren"><h2>Sperren</h2>
      {f'<table><tr><th>Grund</th><th>Gesetzt am</th><th>Kommentar</th></tr>{sperren_zeilen}</table><p class="muted">Aktive Sperre(n) - vor einem Mahnlauf beachten, keine Aufforderung zum Entsperren.</p>' if sperren else '<p class="muted">Keine aktiven Sperren.</p>'}
    </div>"""

    # -- Mahnfälle & Schriftverkehr (id="mahnfaelle") - fasst ALLE in ------
    # -- diesem System tatsächlich existierenden verknüpften Nachweise -----
    # -- zusammen (Mahnfälle/Mahnläufe/Erhöhungsschreiben/Vertragsende-Er- -
    # -- innerungen), Gesendet/Zugang jeweils getrennt ausgewiesen. Externe-
    # -- Korrespondenz außerhalb dieses Systems wird NICHT geführt --------
    # -- (Codex-Rückprüfung: bisher nur Mahnfälle, keine tatsächlichen -----
    # -- Versand-/Zustellnachweise). ----------------------------------------
    mahnfaelle = mahnfaelle or []
    mahnfaelle_zeilen = "".join(
        f"<tr><td>#{m.forderung_op_position_id}</td><td>{m.stufe}</td><td>{h(m.status)}</td>"
        f"<td>{eur(m.betrag_cent)}</td><td>{m.geplant_am.date().isoformat()}</td></tr>"
        for m in mahnfaelle
    )
    mahnlaeufe = mahnlaeufe or []
    mahnlauf_zeilen = "".join(
        f"<tr><td>Stufe {m.stufe}</td><td>{h(m.kanal)}</td><td>{h(m.status)}</td>"
        f"<td>{m.geplant_am.strftime('%Y-%m-%d') if m.geplant_am else '—'}</td>"
        f"<td>{m.gesendet_am.strftime('%Y-%m-%d') if m.gesendet_am else 'kein Nachweis im System'}</td></tr>"
        for m in mahnlaeufe
    )
    erhoehungsschreiben = erhoehungsschreiben or []
    erhoehung_zeilen = "".join(
        f"<tr><td>{h(e.status)}</td><td>{e.massgeblicher_termin.isoformat()}</td><td>{eur(e.erhoehung_cent)}</td>"
        f"<td>{e.versendet_am.strftime('%Y-%m-%d') if e.versendet_am else 'kein Nachweis im System'}</td>"
        f"<td>{e.zugang_bestaetigt_am.isoformat() if e.zugang_bestaetigt_am else 'kein Nachweis im System'}</td></tr>"
        for e in erhoehungsschreiben
    )
    vertragsende_erinnerungen = vertragsende_erinnerungen or []
    vertragsende_zeilen = "".join(
        f"<tr><td>{h(v.status)}</td><td>{v.end_datum.isoformat()}</td><td>{v.faellig_am.isoformat()}</td>"
        f"<td>{v.benachrichtigt_am.strftime('%Y-%m-%d') if v.benachrichtigt_am else 'kein Nachweis im System'}</td></tr>"
        for v in vertragsende_erinnerungen
    )
    schriftverkehr_teile = []
    if mahnfaelle:
        schriftverkehr_teile.append(
            "<h3>Mahnfälle</h3><table><tr><th>OP-Nr.</th><th>Stufe</th><th>Status</th><th>Fallbetrag</th>"
            f"<th>Geplant am</th></tr>{mahnfaelle_zeilen}</table>"
        )
    if mahnlaeufe:
        schriftverkehr_teile.append(
            "<h3>Mahnläufe (Versand)</h3><table><tr><th>Stufe</th><th>Kanal</th><th>Status</th>"
            f"<th>Geplant am</th><th>Gesendet/bestätigt am</th></tr>{mahnlauf_zeilen}</table>"
        )
    if erhoehungsschreiben:
        schriftverkehr_teile.append(
            "<h3>Erhöhungsschreiben (Index)</h3><table><tr><th>Status</th><th>Maßgeblicher Termin</th>"
            f"<th>Erhöhung</th><th>Versendet am</th><th>Zugang bestätigt am</th></tr>{erhoehung_zeilen}</table>"
        )
    if vertragsende_erinnerungen:
        schriftverkehr_teile.append(
            "<h3>Vertragsende-Erinnerungen</h3><table><tr><th>Status</th><th>Enddatum</th><th>Fällig am</th>"
            f"<th>Benachrichtigt am</th></tr>{vertragsende_zeilen}</table>"
        )
    mahnfaelle_karte = f"""
    <div class="card" id="mahnfaelle"><h2>Mahnfälle &amp; Schriftverkehr</h2>
      {"".join(schriftverkehr_teile) or '<p class="muted">Keine Mahnfälle, Mahnläufe, Erhöhungsschreiben oder Vertragsende-Erinnerungen im System hinterlegt.</p>'}
      <p class="muted">Diese Übersicht zeigt nur in DIESEM System selbst geplante/versendete Schreiben - eine
         etwaige externe Korrespondenz (z. B. Outlook/Papierpost außerhalb dieses Systems) wird hier NICHT
         geführt und ist separat zu prüfen.</p>
      <p><a href="/backoffice/vertrag/{h(vertrag.id)}/mahnvorschau">Mahnvorschau öffnen</a></p>
    </div>"""

    # -- Index & Rechtsprofil - konsolidiert und OHNE Klick sichtbar -------
    # -- (vorher großteils in "Quellen und Historie" versteckt); Entwurf ---
    # -- und Freigabe bleiben GETRENNT, ein fehlendes Profil wird NICHT ----
    # -- mit fehlendem Rechts-/Indexwissen insgesamt gleichgesetzt. --------
    index_karte = f"""
    <div class="card"><h2>Index &amp; Rechtsprofil</h2>
      <table>
        <tr><th>Freigegebenes Rechtsprofil</th>
            <td>{h(rechtsprofil_freigegeben_hinweis) if rechtsprofil_freigegeben_hinweis else 'Kein freigegebenes Rechtsprofil.'}</td></tr>
        {f'<tr><th>Neuerer Rechtsprofil-Entwurf (ungeprüft)</th><td>{h(rechtsprofil_entwurf_hinweis)}</td></tr>' if rechtsprofil_entwurf_hinweis else ''}
        <tr><th>Gespeicherte freigegebene Indexklausel</th>
            <td>{h(index_klausel_hinweis) if index_klausel_hinweis else 'Keine Indexklausel erfasst.'}</td></tr>
        <tr><th>Offener Index-Prüfbedarf</th><td>{h(index_pruefbedarf_hinweis) if index_pruefbedarf_hinweis else 'Kein offener Prüfbedarf hinterlegt.'}</td></tr>
        <tr><th>Letzter Indexautomatik-Lauf</th>
            <td>{h(letzter_indexautomatik_lauf_hinweis) if letzter_indexautomatik_lauf_hinweis else 'Noch kein Indexautomatik-Lauf für diesen Vertrag hinterlegt.'}</td></tr>
        <tr><th>Letzte Vertragsprüfung</th><td>{h(letzte_pruefung_hinweis) if letzte_pruefung_hinweis else 'Keine Vertragsprüfung im System hinterlegt.'}</td></tr>
      </table>
      <p class="muted">"Gespeicherte freigegebene Indexklausel" bedeutet: eine Klausel mit Status FREIGEGEBEN ist
         hinterlegt - dies ist KEIN laufender Gültigkeitscheck (z. B. Mindestabstand/Schwelle zum heutigen Tag),
         das prüft weiterhin gesondert die Indexregel-Verwaltung.</p>
      <p class="muted">Ein fehlendes/neues Mietvertragsprofil bedeutet NICHT, dass hier keinerlei Rechts-/
         Indexwissen vorliegt - ältere Rechtsprofile/Prüfungen/Klauseln (oben) können unabhängig davon bereits
         bestehen.</p>
      <details><summary>Index-Quellfelder aus dem Profil (unverbindlich, erzeugt keine Klausel)</summary>
        <table>
          <tr><th>Reihe</th><td>{h(profil.index_reihe) if profil and profil.index_reihe else '—'}</td></tr>
          <tr><th>Basismonat</th><td>{h(profil.index_urspruenglicher_basismonat) if profil and profil.index_urspruenglicher_basismonat else '—'}</td></tr>
          <tr><th>Basiswert</th><td>{profil.index_urspruenglicher_basiswert if profil and profil.index_urspruenglicher_basiswert is not None else '—'}</td></tr>
          <tr><th>Schwelle</th><td>{f"{profil.index_schwelle_prozent} % ({'ab' if profil.index_schwelle_inklusive else 'über'})" if profil and profil.index_schwelle_prozent is not None else '—'}</td></tr>
          <tr><th>Anpassungsmonat</th><td>{profil.index_anpassungsmonat if profil and profil.index_anpassungsmonat else '—'}</td></tr>
          <tr><th>Mindestabstand (Monate)</th><td>{profil.index_mindestintervall_monate if profil and profil.index_mindestintervall_monate else '—'}</td></tr>
        </table>
      </details>
      <p><a href="/backoffice/vertrag/{h(vertrag.id)}/rechtsprofil">Rechtsprofil verwalten</a> ·
         <a href="/backoffice/vertrag/{h(vertrag.id)}/indexklauseln">Indexregel verwalten</a> ·
         <a href="/backoffice/vertrag/{h(vertrag.id)}/pruefung">Vertragsprüfung</a></p>
    </div>"""

    # -- Auf einen Blick - zentrale Werte OHNE Klick sichtbar, lange -------
    # -- Historie/Detailtabellen bleiben in den Karten/Details darunter ----
    # -- eingeklappt (Codex-Rückprüfung: "zentralen Kontobetrag/aktuelle ---
    # -- Vorschreibung/Kaution/Index oben sichtbar halten"). ---------------
    kaution_ueberblick = (
        eur(kaution.betrag_cent) if kaution
        else ("nicht hinterlegt" if not (profil and profil.vertragliche_kaution_cent is not None) else "vereinbart, kein Eingang bestätigt")
    )
    # Codex-Rückprüfung: die oberste Kachel muss den tatsächlichen Status
    # (insbesondere ENTWURF) direkt nennen statt unkommentiert nur einen
    # Betrag zu zeigen; ein Ledger-only-SOLL für den laufenden Monat
    # (siehe `ledger_nur_gebucht` oben) darf hier NICHT als "keine
    # hinterlegt" erscheinen, obwohl tatsächlich gebucht wurde.
    if primaer is not None:
        vorschreibung_ueberblick_label = (
            f"{'Aktuelle' if ist_aktueller_monat else 'Letzte hinterlegte'} Vorschreibung ({h(primaer.monat)})"
        )
        vorschreibung_ueberblick_wert = (
            f"{eur(primaer_summe)} — {primaer_status_label}" if primaer_summe is not None
            else f"kein Beleg — {primaer_status_label}"
        )
    else:
        ledger_aktuell_cent = ledger_nur_gebucht.get(aktueller_monat)
        if ledger_aktuell_cent is not None:
            vorschreibung_ueberblick_label = f"Aktuelle Vorschreibung ({h(aktueller_monat)})"
            vorschreibung_ueberblick_wert = f"{eur(ledger_aktuell_cent)} — im Konto gebucht, kein eigener Datensatz"
        else:
            vorschreibung_ueberblick_label = "Aktuelle Vorschreibung"
            vorschreibung_ueberblick_wert = "siehe Buchungen"
    rechtsprofil_ueberblick_wert = (
        "freigegeben" if rechtsprofil_freigegeben_hinweis
        else ("Entwurf, ungeprüft" if rechtsprofil_entwurf_hinweis else "kein Rechtsprofil")
    )
    ueberblick_html = f"""
    <div class="card">
      <div class="kpi-grid">
        <div class="kpi"><span class="zahl">{eur(kontostatus.saldo_cent) if (kontostatus and kontostatus.saldo_cent is not None) else '—'}</span>
            <span class="kpi-label">Kontostand (offen/Guthaben)</span></div>
        <div class="kpi"><span class="zahl">{vorschreibung_ueberblick_wert}</span>
            <span class="kpi-label">{vorschreibung_ueberblick_label}</span></div>
        <div class="kpi"><span class="zahl">{kaution_ueberblick}</span><span class="kpi-label">Kaution eingegangen</span></div>
        <div class="kpi"><span class="zahl">{rechtsprofil_ueberblick_wert}</span><span class="kpi-label">Rechtsprofil-Status</span></div>
      </div>
    </div>"""

    return f"""
    <p>{zurueck_html}</p>
    <div class="card">
      <h1>{h(debitor.name)}</h1>
      <p class="muted">{h(objekt.bezeichnung)} / {h(einheit.bezeichnung)} · Vertrag <code>{h(vertrag.id)}</code></p>
      <p>{konto_link}
         <a href="/backoffice/vertrag/{h(vertrag.id)}/mahnvorschau">Mahnvorschau</a> ·
         <a href="/backoffice/vertrag/{h(vertrag.id)}/indexklauseln">Indexregel</a> ·
         <a href="/backoffice/vertrag/{h(vertrag.id)}/mieweg-vorschau">Rekonstruktionsmodell (Mietzinsobergrenze)</a></p>
      <a href="/backoffice/vertrag/{h(vertrag.id)}/mietvertragsprofil/bearbeiten"><button type="button">Profil aktualisieren</button></a>
    </div>
    {ueberblick_html}
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
        <tr><th>Vertragsbeginn (technisch, für Sollstellung/Buchung)</th><td>{vertrag.gueltig_von.isoformat()}</td></tr>
        <tr><th>Vertragsende</th><td>{vertrag.gueltig_bis.isoformat() if vertrag.gueltig_bis else 'unbefristet'}</td></tr>
        <tr><th>Ursprünglicher tatsächlicher Mietbeginn</th>
            <td>{profil.urspruenglicher_mietbeginn.isoformat() if profil and profil.urspruenglicher_mietbeginn else 'nicht hinterlegt'}
            {_status_badge('bereit' if (profil and profil.urspruenglicher_mietbeginn) else 'Angabe fehlt')}</td></tr>
        <tr><th>Verwaltungsübernahme</th>
            <td>{profil.verwaltungsuebernahme_am.isoformat() if profil and profil.verwaltungsuebernahme_am else 'nicht hinterlegt'}
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
    {index_karte}
    <details class="card"><summary>Quellen und Historie (Mietvertragsprofil-Versionen)</summary>
      <p class="muted">"Quelle" (PDF-Extraktion/manuell/Importformat) ist ein Herkunftsvermerk, KEINE fachliche
         Freigabe.</p>
      <table><tr><th>Version</th><th>Quelle</th><th>Referenz</th><th>Erfasst von</th><th>Am</th></tr>{versionen_zeilen}</table>
    </details>"""
