"""Server-gerenderte HTML-Bausteine für das Backoffice.

Bewusst kein Template-Engine/Frontend-Framework: alle Seiten sind
einfache, serverseitig zusammengesetzte f-Strings. JEDER interpolierte
Wert, der aus der Datenbank oder von einem Formular stammt, MUSS über
`h()` (html.escape) laufen - das ist die einzige XSS-Verteidigungslinie
dieser Seite und wird bewusst nicht "optimiert" (kein ungeprüftes
`|safe`-Äquivalent).
"""

from __future__ import annotations

import re
from decimal import InvalidOperation
from html import escape as h

from mietinkasso.domain.money import cents_to_decimal, to_cents


def eur(cent: int) -> str:
    """Formatiert Cent als Euro-Text (Decimal, niemals Float) fürs Anzeigen."""

    return f"{cents_to_decimal(cent):,.2f}".replace(",", "§").replace(".", ",").replace("§", ".") + " €"


def parse_eur_betrag(text: str | None) -> int:
    """Parst einen im Formular eingegebenen EUR-Betrag robust zu Cent.

    Akzeptiert sowohl das bisher unterstützte einfache Punkt-Dezimalformat
    (z. B. "500.00", "500" - unverändert, keine Formularstelle verlässt
    sich auf ein anderes Verhalten hierfür) als auch die deutsche
    Notation mit Punkt als Tausender- und Komma als Dezimaltrennzeichen
    (z. B. "1.500,00", "1500,00") - genau das Format, das `eur()` beim
    Vorbefüllen von Formularfeldern erzeugt (z. B. der Restbetrag-
    Vorschlag bei der manuellen Bankzuordnung). Ohne diese Funktion führte
    ein unverändert abgeschicktes vorbefülltes Feld wie "1.500,00" zu
    einer stillschweigend falschen Umrechnung ODER zu einem unbehandelten
    `decimal.InvalidOperation` (HTTP 500).

    Jede mehrdeutige (z. B. "1,500.00" im US-Format, das hier nicht
    unterstützt wird) oder anderweitig kaputte Eingabe wird mit einer
    verständlichen `ValueError` abgelehnt - nie mit einem 500 und nie mit
    einem stillschweigend falschen Faktor."""

    if text is None:
        raise ValueError("Betrag darf nicht leer sein.")
    original = text.strip()
    if not original:
        raise ValueError("Betrag darf nicht leer sein.")
    # Grouping must be validated BEFORE separators are removed. Otherwise
    # "1.50,00" silently becomes 150 EUR, and "1.500" becomes 1.50 EUR
    # even though a German-speaking operator may mean 1,500 EUR.
    deutsches_format = r"[+-]?(?:[0-9]+|[0-9]{1,3}(?:\.[0-9]{3})+),[0-9]{1,2}"
    einfaches_format = r"[+-]?[0-9]+(?:\.[0-9]{1,2})?"
    if re.fullmatch(deutsches_format, original):
        normalisiert = original.replace(".", "").replace(",", ".")
    elif re.fullmatch(einfaches_format, original):
        normalisiert = original
    else:
        raise ValueError(
            f"Betrag '{original}' ist ungültig oder mehrdeutig. "
            "Bitte z. B. 1.500,00 oder 1500.00 verwenden, mit höchstens zwei Nachkommastellen."
        )
    try:
        cent = to_cents(normalisiert)
    except (InvalidOperation, ArithmeticError, ValueError) as exc:
        raise ValueError(f"Betrag '{original}' ist kein gültiger Geldbetrag.") from exc
    if abs(cent) > 2**63 - 1:
        raise ValueError("Betrag überschreitet den zulässigen Speicherbereich.")
    return cent


#: Sichtbare Hauptnavigation für angemeldete Benutzer - Auftrag
#: HV-20260914-UI-EINFACH: GENAU vier fachliche Hauptbereiche statt einer
#: flachen Liste technischer Einzellinks, plus ein fünfter Sammelbereich
#: "Einstellungen" für Konfiguration/Regeln, die im Alltag nicht gebraucht
#: werden. JEDE bisher über `_NAV_LINKS` erreichbare Route bleibt
#: erreichbar - nur nicht mehr als Einzellink hier, sondern über die
#: jeweilige Bereichs-Startseite (`/backoffice/zahlungen`,
#: `/backoffice/abrechnungen`, `/backoffice/einstellungen`), die diese
#: Routen ihrerseits verlinkt. Reihenfolge = Anzeigereihenfolge.
_BEREICHE = (
    ("uebersicht", "/backoffice/", "Übersicht"),
    ("mieter", "/backoffice/vertraege", "Mieter & Objekte"),
    ("zahlungen", "/backoffice/zahlungen", "Zahlungen & Mahnungen"),
    ("abrechnungen", "/backoffice/abrechnungen", "Abrechnungen"),
    ("einstellungen", "/backoffice/einstellungen", "Einstellungen"),
)

#: Pfadpräfixe je Bereich, NUR für die aktive Hervorhebung in der
#: Navigation (rein optisch - ändert keine Berechtigung/Route). Eine
#: konkrete Vertrags-/Kontoseite (z. B. `/vertrag/{id}/mahnvorschau`)
#: bleibt dabei bewusst dem Bereich "Mieter & Objekte" zugeordnet, weil
#: sie von dort (der Mieterakte) aus erreicht wird, nicht dem
#: fachlichen Thema der Zielseite.
_BEREICH_PFADPRAEFIXE = {
    "mieter": ("/backoffice/vertraege", "/backoffice/vertrag/", "/backoffice/konto/", "/backoffice/op/"),
    "zahlungen": (
        "/backoffice/zahlungen", "/backoffice/bank", "/backoffice/mahnwesen", "/backoffice/mailversand",
        "/backoffice/mahnfall",
    ),
    "abrechnungen": (
        "/backoffice/abrechnungen", "/backoffice/variable-abrechnung", "/backoffice/dashboard/monatsuebersicht",
    ),
    "einstellungen": (
        "/backoffice/einstellungen", "/backoffice/eroeffnung", "/backoffice/indexautomatik", "/backoffice/basiszinssatz",
    ),
}


def _aktueller_bereich(pfad: str) -> str | None:
    if pfad in ("/backoffice/", "/backoffice"):
        return "uebersicht"
    for bereich, praefixe in _BEREICH_PFADPRAEFIXE.items():
        if any(pfad.startswith(p) for p in praefixe):
            return bereich
    return None


#: Anzeigefreundliche Bestandsart statt des rohen Enum-Werts
#: (`domain/enums.py::Nutzungsstatus`) - reine Beschriftung, LEITET
#: NICHTS aus einem Saldo/Nullkonto ab, sondern zeigt exakt den
#: eingespielten/gepflegten `Einheit.nutzungsstatus` an.
_NUTZUNGSSTATUS_LABEL = {
    "DAUERVERMIETUNG": "vermietet",
    "KURZZEITVERMIETUNG": "Kurzzeitvermietung",
    "SELFSTORAGE": "Selfstorage",
    "EIGENNUTZUNG": "Eigennutzung",
    "LEERSTAND": "Leerstand",
}


def nutzungsstatus_label(nutzungsstatus: str | None) -> str:
    if not nutzungsstatus:
        return "-"
    return _NUTZUNGSSTATUS_LABEL.get(nutzungsstatus, nutzungsstatus)


#: NUR diese Umgebungswerte gelten als "wir wissen sicher, dass hier
#: ausschließlich synthetische Demodaten liegen" - jeder ANDERE Wert
#: (production, staging, ein Zwischenschritt wie
#: "local_realdata_staged", oder irgendein unbekannter Wert) behauptet
#: NIE synthetische Daten, sondern zeigt den Echtbetrieb-Banner.
#: Codex-Rückprüfung (Paket A): die vorherige Logik prüfte umgekehrt
#: ("Echtbetrieb nur bei genau 'production'") und hätte einen
#: Zwischenschritt mit bereits echten, gestagten Daten fälschlich als
#: "nur synthetische Demodaten" ausgewiesen - sicherer ist, im Zweifel
#: NICHT zu behaupten, es seien Demodaten.
_BEKANNTE_DEMO_UMGEBUNGEN = frozenset({"development", "test", "ci"})


def ist_bekannte_demo_umgebung(environment: str) -> bool:
    """Reine Klassifizierung (kein Zustand, kein Seiteneffekt) - von
    `betriebsmodus_banner` UND von `backoffice/app.py` für die
    Autozuordnungs-Routensperre benutzt, damit beide Stellen exakt
    dieselbe Definition von "Demo-Umgebung" verwenden."""

    return environment in _BEKANNTE_DEMO_UMGEBUNGEN


def betriebsmodus_banner(*, environment: str, send_enabled: bool) -> str:
    """Reine Anzeigeentscheidung (kein neues Datenmodell, keine
    automatische Freigabe von irgendetwas) - liest ausschließlich die
    bereits vorhandene `environment`/`send_enabled`-Konfiguration und
    entscheidet NUR, welcher Banner-Text angezeigt wird. Diese Funktion
    selbst liest/ändert keinen Zustand."""

    if ist_bekannte_demo_umgebung(environment):
        return "PILOT-BETRIEB — nur synthetische Demodaten, kein realer Bank-/Mailversand, kein Mehrbenutzerbetrieb"
    # Escaping passiert erst beim Rendern in `seite()` (`h(banner_text)`) -
    # diese Funktion liefert reinen Text, kein HTML.
    mailversand = "AKTIV" if send_enabled else "AUS (SEND_ENABLED=false)"
    return (
        f"ECHTBETRIEB ({environment}) — reale Hausverwaltungsdaten. Mailversand: {mailversand}. "
        "Automatischer Bankabgleich: AUS (EBS/EBICS ausstehend)."
    )


def kurzstatus_text(*, environment: str, send_enabled: bool) -> str:
    """Kurzer, alltagstauglicher Betriebszustand OHNE technischen Jargon
    (Auftrag HV-20260914-UI-EINFACH, Rückprüfung 14.09.2026: der bisherige
    Banner war "groß und technisch", `SEND_ENABLED=false`/"EBS/EBICS"
    tauchte zusätzlich in "Das ist zu erledigen" auf). Liest dieselbe
    `environment`/`send_enabled`-Konfiguration wie `betriebsmodus_banner`,
    NUR knapper formuliert - der vollständige Banner bleibt unverändert
    in aufklappbaren Systemdetails erhalten."""

    betrieb = "Pilotbetrieb" if ist_bekannte_demo_umgebung(environment) else f"Echtbetrieb ({environment})"
    versand = "aktiv" if send_enabled else "pausiert"
    return f"{betrieb} · E-Mail-Versand {versand} · Bankdaten manuell aktualisieren"


_SPERRGRUND_LABEL = {
    "STREIT": "Streitfall",
    "MIETMINDERUNG": "Mietminderung geltend gemacht",
    "RATENPLAN": "Ratenplan vereinbart",
    "INSOLVENZ": "Insolvenzverfahren",
    "RECHTSANWALT": "Beim Rechtsanwalt",
    "UNGEKLAERTER_EINGANG": "Ungeklärter Zahlungseingang",
    "UNKLARER_EROEFFNUNGSSALDO": "Unklarer Eröffnungssaldo",
    "BOUNCE": "E-Mail unzustellbar",
    "MANUELL": "Manuell gesetzt",
}


def sperrgrund_label(code: str) -> str:
    """Verständliche Übersetzung eines technischen Sperrgrund-Codes fürs
    UI (Auftrag HV-20260914-UI-LESBAR: "MANUELL/RECHTSANWALT/RATENPLAN
    verständlich übersetzen"). Der Rohcode bleibt an jeder Anzeigestelle
    zusätzlich im HTML erhalten (Tooltip oder Klammerzusatz je nach
    Platz) - nur die primäre Anzeige wird lesbar, Umfang/Block der
    Sperre ändert sich nicht. Ein unbekannter/künftiger Code fällt
    unverändert auf den Rohcode zurück (kein stiller
    Informationsverlust)."""

    return _SPERRGRUND_LABEL.get(code, code)


def seite(
    *,
    titel: str,
    inhalt: str,
    user_id: str | None = None,
    csrf_token: str | None = None,
    environment: str = "development",
    send_enabled: bool = False,
    aktueller_pfad: str = "",
) -> str:
    banner_text = betriebsmodus_banner(environment=environment, send_enabled=send_enabled)
    kurzstatus = kurzstatus_text(environment=environment, send_enabled=send_enabled)
    titel_suffix = "PILOT" if ist_bekannte_demo_umgebung(environment) else "ECHTBETRIEB"
    logout_form = ""
    nav = ""
    if user_id is not None:
        aktiver_bereich = _aktueller_bereich(aktueller_pfad)
        nav_links = "".join(
            f'<a href="{h(pfad)}" class="{"aktiv" if bereich == aktiver_bereich else ""}">{h(label)}</a>'
            for bereich, pfad, label in _BEREICHE
        )
        logout_form = f"""
        <span class="muted">angemeldet als {h(user_id)}</span>
        <form method="post" action="/backoffice/logout" style="display:inline">
          <input type="hidden" name="csrf_token" value="{h(csrf_token or '')}">
          <button type="submit">Abmelden</button>
        </form>"""
        nav = f'<nav class="hauptnav">{nav_links}</nav>'
    return f"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{h(titel)} — Hausverwaltung & Mietinkasso ({titel_suffix})</title>
<style>
  :root {{
    --anthrazit: #1F2125; --anthrazit-hell: #33363b; --gold: #C9A86A; --gold-dunkel: #a9824a; --creme: #F5F2EC;
  }}
  * {{ box-sizing: border-box; }}
  /* Auftrag HV-20260914-UI-LESBAR (Userkorrektur, hat für die
     funktionale Weboberfläche Vorrang vor der JLB-Garamond-CI - PDFs/
     Mails/Brandassets sind davon NICHT betroffen): System-UI-Sans-Serif
     für alle Bedienelemente/Tabellen/Zahlen/Texte statt der bisherigen
     Serifenschrift, die auf manchen Systemen winzig/fett wirkte. Reine
     Font-Family-Angabe ohne @font-face/Web-Font-Request - kein neues
     Framework, keine neue Abhängigkeit. Anthrazit/Gold bleiben die
     Akzentfarben, aber sparsam (kleine Marker/Ränder statt große
     einfarbige Flächen) auf hellem, neutralem Grund. */
  body {{
    font-family: "Segoe UI", system-ui, -apple-system, "Helvetica Neue", Arial, sans-serif;
    font-size: 15px; line-height: 1.5; font-weight: 400; margin: 0;
    background: var(--creme); color: var(--anthrazit);
  }}
  h1, h2, h3, .schriftzug {{ font-family: inherit; font-weight: 600; line-height: 1.3; }}
  h1 {{ font-size: 1.35rem; }}
  h2 {{ font-size: 1.1rem; }}
  h3 {{ font-size: 0.98rem; }}
  header {{ background: var(--anthrazit); color: #fff; padding: 0.55rem 1.25rem; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.5rem 1rem; }}
  header a {{ color: #fff; text-decoration: none; font-weight: 600; letter-spacing: 0.01em; }}
  header .muted {{ color: #d8d3c8; }}
  header form {{ display: inline-flex; align-items: center; gap: 0.5rem; }}
  header form button {{ background: transparent; border: 1px solid var(--gold); color: var(--gold); border-radius: 4px; padding: 0.25rem 0.7rem; margin-top: 0; }}
  header form button:hover {{ background: var(--gold); color: var(--anthrazit); }}
  /* Kein "dominanter goldener Warnstreifen" mehr - der Betriebsstatus
     bleibt lesbar/erkennbar (kleiner Gold-Punkt als sparsamer Akzent),
     aber als schmale, helle Zeile statt einer vollflächigen Farbleiste. */
  .pilot-banner {{
    background: #fff; color: #4a4d52; border-bottom: 1px solid #e3ded3;
    text-align: left; padding: 0.3rem 1.25rem; font-size: 0.78rem; font-weight: 400;
  }}
  .pilot-banner::before {{ content: "●"; color: var(--gold-dunkel); margin-right: 0.4rem; font-size: 0.7em; }}
  .pilot-banner .banner-details {{ margin-top: 0.1rem; font-size: 0.74rem; font-weight: 400; }}
  .pilot-banner .banner-details summary {{ cursor: pointer; color: var(--anthrazit); text-decoration: underline; font-weight: 400; }}
  nav.hauptnav {{ background: var(--anthrazit-hell); padding: 0 1.25rem; display: flex; flex-wrap: wrap; }}
  nav.hauptnav a {{
    color: #eee9df; text-decoration: none; font-size: 0.85rem; font-weight: 600; padding: 0.65rem 0.85rem;
    border-bottom: 3px solid transparent; white-space: nowrap;
  }}
  nav.hauptnav a:hover {{ color: #fff; }}
  nav.hauptnav a.aktiv {{ color: #fff; border-bottom-color: var(--gold); }}
  main {{ padding: 1.25rem; max-width: 1150px; margin: 0 auto; }}
  h1, h2, h3 {{ color: var(--anthrazit); }}
  a {{ color: var(--anthrazit); }}
  a:focus-visible, button:focus-visible, input:focus-visible, select:focus-visible,
  textarea:focus-visible, summary:focus-visible {{
    outline: 2px solid var(--gold-dunkel); outline-offset: 2px;
  }}
  table {{ border-collapse: collapse; width: 100%; margin: 0.75rem 0; background: #fff; }}
  th, td {{ border: 1px solid #ddd; padding: 0.35rem 0.55rem; text-align: left; font-size: 0.85rem; vertical-align: top; overflow-wrap: anywhere; word-break: break-word; }}
  th {{ background: #efeae0; font-weight: 600; }}
  .card {{ background: #fff; border: 1px solid #ddd; border-radius: 8px; padding: 1rem; margin-bottom: 1rem; }}
  .warn {{ color: #8a6416; font-weight: 600; }}
  .error {{ color: #b00020; font-weight: 600; }}
  .ok {{ color: #1a7f37; font-weight: 600; }}
  .muted {{ color: #666; font-size: 0.85rem; }}
  .flash-ok {{ background: #eef7f0; border: 1px solid #bcdfc4; padding: 0.55rem 0.9rem; border-radius: 4px; margin-bottom: 1rem; }}
  .flash-error {{ background: #fbeaea; border: 1px solid #e0a8a8; padding: 0.55rem 0.9rem; border-radius: 4px; margin-bottom: 1rem; }}
  .gesperrt-row {{ background: #fdf3f3; }}
  .kpi-grid {{ display: flex; flex-wrap: wrap; gap: 0.75rem; margin-bottom: 0.75rem; }}
  .kpi {{ background: #fff; border: 1px solid #ddd; border-left: 4px solid var(--gold); border-radius: 6px; padding: 0.65rem 0.9rem; flex: 1 1 170px; min-width: 150px; }}
  .kpi .zahl {{ font-size: 1.2rem; font-weight: 600; display: block; color: var(--anthrazit); }}
  .kpi .kpi-label {{ font-size: 0.76rem; color: #666; }}
  .badge {{ display: inline-block; padding: 0.1rem 0.5rem; border-radius: 3px; font-size: 0.78rem; font-weight: 600; }}
  .badge-error {{ background: #fbeaea; color: #b00020; }}
  .badge-warn {{ background: #fdf0dc; color: #8a6416; }}
  .badge-ok {{ background: #e6f4ea; color: #1a7f37; }}
  .badge-muted {{ background: #eee; color: #555; }}
  .tabelle-scroll {{ overflow-x: auto; max-width: 100%; }}
  /* Bankübersicht (Auftrag HV-20260914-BANKUEBERSICHT): "Mit bestehender
     Zahlung verknüpfen" bewusst optisch hervorgehoben (gefüllt statt nur
     Outline), um Doppelbuchungen zu vermeiden - sparsamer Golddunkel-
     Rahmen statt einer dominanten goldenen Fläche. */
  a.btn-verknuepfen {{
    display: inline-block; margin: 0.3rem 0.4rem 0.3rem 0; padding: 0.35rem 0.7rem; border-radius: 4px;
    border: 1px solid var(--gold-dunkel); background: var(--anthrazit); color: #fff; text-decoration: none;
    font-size: 0.82rem; font-weight: 600;
  }}
  a.btn-verknuepfen:hover {{ background: var(--anthrazit-hell); }}
  .klaerfall-card {{ border-left: 3px solid #b00020; }}
  .tx-manuell, .tx-details {{ margin-top: 0.4rem; }}
  .tx-manuell summary, .tx-details summary {{ font-size: 0.82rem; font-weight: 600; }}
  label {{ display: block; margin: 0.5rem 0 0.15rem; font-weight: 600; font-size: 0.85rem; }}
  input, select, textarea {{ width: 100%; max-width: 100%; box-sizing: border-box; padding: 0.35rem 0.5rem; font: inherit; border: 1px solid #ccc; border-radius: 4px; }}
  button, input[type=submit] {{ font: inherit; font-weight: 600; padding: 0.45rem 0.95rem; cursor: pointer; border-radius: 4px; border: 1px solid var(--anthrazit); background: var(--anthrazit); color: #fff; width: auto; max-width: 100%; margin-top: 0.6rem; }}
  button:hover, input[type=submit]:hover {{ background: var(--anthrazit-hell); }}
  button.secondary {{ background: #fff; color: var(--anthrazit); }}
  button.secondary:hover {{ background: #f0ede5; }}
  button.gross {{ background: var(--gold); color: var(--anthrazit); border-color: var(--gold-dunkel); font-size: 1rem; padding: 0.6rem 1.3rem; font-weight: 600; }}
  button.gross:hover {{ background: var(--gold-dunkel); color: #fff; }}
  fieldset {{ border: 1px solid #ddd; border-radius: 6px; margin-bottom: 1rem; padding: 0.75rem 1rem; }}
  nav.tabs a {{ margin-right: 1rem; font-size: 0.88rem; }}
  code {{ background: #f0f0f0; padding: 0.05rem 0.3rem; border-radius: 3px; font-size: 0.9em; }}
  .bereich-karten {{ display: flex; flex-wrap: wrap; gap: 0.9rem; }}
  .bereich-karten .card {{ flex: 1 1 260px; margin-bottom: 0; }}
  .todo-liste {{ list-style: none; margin: 0; padding: 0; }}
  .todo-liste li {{ padding: 0.4rem 0; border-bottom: 1px solid #eee; }}
  .todo-liste li:last-child {{ border-bottom: none; }}
  /* Aufgabenliste (Dashboard "Das ist zu erledigen") - Rückprüfung
     Codex 14.09.2026: KEINE farbig gefüllten "Pillen"/Balken mehr für
     ganze Satzstrecken (das erzeugte die "langen roten/gelben
     Fettstreifen") und KEINE vollbreiten schwarzen Buttons bei 820px.
     Name in eigener Zeile, Objekt/Top sekundär darunter. Je Grund: ein
     Grid aus Text (links, schrumpft nie unter 0, bricht per
     `overflow-wrap`) und einem ruhigen, schmalen Outline-Button
     (rechts, fixe Breite) - erst bei sehr schmalem Viewport (≤520px)
     wird untereinander gestapelt, NIE dazwischen. */
  .aufgaben-liste {{ list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.6rem; }}
  .aufgaben-karte {{ border: 1px solid #ddd; border-left: 3px solid var(--gold); border-radius: 6px; padding: 0.6rem 0.8rem; background: #fff; }}
  .aufgaben-karte-kopf {{ margin-bottom: 0.4rem; display: flex; flex-direction: column; gap: 0.1rem; }}
  .aufgaben-name {{ font-weight: 600; }}
  .aufgaben-gruende {{ list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.5rem; }}
  .aufgaben-gruende li {{
    display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 12px;
  }}
  .aufgaben-grund-text {{
    min-width: 0; font-size: 14px; font-weight: 400; line-height: 1.5; color: var(--anthrazit);
    background: transparent; overflow-wrap: anywhere;
  }}
  .grund-punkt {{
    display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 8px;
    vertical-align: middle; flex: 0 0 auto;
  }}
  .grund-punkt-warn {{ background: #c9820f; }}
  .grund-punkt-error {{ background: #b00020; }}
  a.aufgabe-aktion {{
    display: inline-flex; align-items: center; justify-content: center; box-sizing: border-box;
    min-height: 36px; width: 150px; padding: 0 0.9rem; border-radius: 4px;
    border: 1px solid var(--anthrazit); background: #fff; color: var(--anthrazit); text-decoration: none;
    font-size: 0.82rem; font-weight: 600; white-space: nowrap; justify-self: end;
  }}
  a.aufgabe-aktion:hover {{ background: #f2f0ea; }}
  .status-zeile {{ display: flex; flex-wrap: wrap; gap: 0.5rem 1.5rem; font-size: 0.85rem; color: #555; margin: 0.5rem 0; }}
  details > summary {{ cursor: pointer; color: var(--anthrazit); font-weight: 600; }}
  img {{ max-width: 100%; }}
  code, pre {{ overflow-wrap: anywhere; }}
  @media (max-width: 520px) {{
    /* Erst hier untereinander stapeln - bei 820px bleibt die Zeile
       zweispaltig (Text/Button nebeneinander), niemals vollbreit. */
    .aufgaben-gruende li {{ grid-template-columns: 1fr; }}
    a.aufgabe-aktion {{ width: auto; justify-self: start; }}
  }}
  @media (max-width: 640px) {{
    main {{ padding: 0.75rem; }}
    header {{ padding: 0.5rem 0.75rem; }}
    .pilot-banner {{ padding: 0.3rem 0.75rem; }}
    nav.hauptnav {{ padding: 0 0.5rem; }}
    nav.hauptnav a {{ padding: 0.6rem 0.55rem; font-size: 0.8rem; }}
    .kpi {{ flex: 1 1 100%; }}
    .bereich-karten .card {{ flex: 1 1 100%; }}
    /* Kompakte Kontentabelle wird zu lesbaren Karten/Zeilen statt seitlich
       zu scrollen (Rückprüfung 14.09.2026) - technische Detailtabellen in
       `.tabelle-scroll` behalten ihr normales Scrollverhalten, das ist
       hier bewusst NICHT betroffen. */
    .tabelle-kompakt table, .tabelle-kompakt thead, .tabelle-kompakt tbody,
    .tabelle-kompakt tr, .tabelle-kompakt td {{ display: block; width: 100%; }}
    .tabelle-kompakt thead {{ display: none; }}
    .tabelle-kompakt tr {{ border: 1px solid #ddd; border-radius: 6px; margin-bottom: 0.6rem; padding: 0.4rem 0.6rem; }}
    .tabelle-kompakt td {{ border: none; padding: 0.25rem 0; }}
    .tabelle-kompakt td[data-label]::before {{
      content: attr(data-label); display: block; font-size: 0.72rem; color: #666; font-weight: 600;
    }}
  }}
</style>
</head>
<body>
<div class="pilot-banner">
  {h(kurzstatus)}
  <details class="banner-details"><summary>Systemdetails</summary>{h(banner_text)}</details>
</div>
<header>
  <a href="/backoffice/" class="schriftzug">JLB Hausverwaltung</a>
  <div>{logout_form}</div>
</header>
{nav}
<main>
{inhalt}
</main>
</body>
</html>"""


def flash_ok(text: str) -> str:
    return f'<div class="flash-ok">{h(text)}</div>'


def flash_error(text: str) -> str:
    return f'<div class="flash-error">{h(text)}</div>'


def csrf_feld(csrf_token: str) -> str:
    return f'<input type="hidden" name="csrf_token" value="{h(csrf_token)}">'


def option(value: str, label: str, *, selected: bool = False) -> str:
    sel = " selected" if selected else ""
    return f'<option value="{h(value)}"{sel}>{h(label)}</option>'
