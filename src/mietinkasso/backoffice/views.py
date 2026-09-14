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
  body {{ font-family: "Segoe UI", system-ui, -apple-system, sans-serif; margin: 0; background: var(--creme); color: var(--anthrazit); }}
  header {{ background: var(--anthrazit); color: #fff; padding: 0.6rem 1.25rem; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.5rem; }}
  header a {{ color: #fff; text-decoration: none; font-weight: 700; letter-spacing: 0.01em; }}
  header .muted {{ color: #d8d3c8; }}
  header form button {{ background: transparent; border: 1px solid var(--gold); color: var(--gold); border-radius: 4px; padding: 0.25rem 0.7rem; }}
  header form button:hover {{ background: var(--gold); color: var(--anthrazit); }}
  .pilot-banner {{ background: var(--gold-dunkel); color: #fff; text-align: center; padding: 0.35rem 0.75rem; font-size: 0.82rem; font-weight: 600; }}
  nav.hauptnav {{ background: var(--anthrazit-hell); padding: 0 1.25rem; display: flex; flex-wrap: wrap; }}
  nav.hauptnav a {{
    color: #eee9df; text-decoration: none; font-size: 0.92rem; font-weight: 600; padding: 0.7rem 0.9rem;
    border-bottom: 3px solid transparent; white-space: nowrap;
  }}
  nav.hauptnav a:hover {{ color: #fff; }}
  nav.hauptnav a.aktiv {{ color: #fff; border-bottom-color: var(--gold); }}
  main {{ padding: 1.25rem; max-width: 1150px; margin: 0 auto; }}
  h1, h2, h3 {{ color: var(--anthrazit); }}
  a {{ color: var(--anthrazit); }}
  table {{ border-collapse: collapse; width: 100%; margin: 0.75rem 0; background: #fff; }}
  th, td {{ border: 1px solid #ddd; padding: 0.35rem 0.55rem; text-align: left; font-size: 0.88rem; vertical-align: top; }}
  th {{ background: #efeae0; }}
  .card {{ background: #fff; border: 1px solid #ddd; border-radius: 8px; padding: 1rem; margin-bottom: 1rem; }}
  .warn {{ color: var(--gold-dunkel); font-weight: 600; }}
  .error {{ color: #b00020; font-weight: 600; }}
  .ok {{ color: #1a7f37; font-weight: 600; }}
  .muted {{ color: #666; font-size: 0.85rem; }}
  .flash-ok {{ background: #e6f4ea; border: 1px solid #1a7f37; padding: 0.55rem 0.9rem; border-radius: 4px; margin-bottom: 1rem; }}
  .flash-error {{ background: #fbeaea; border: 1px solid #b00020; padding: 0.55rem 0.9rem; border-radius: 4px; margin-bottom: 1rem; }}
  .gesperrt-row {{ background: #fbeaea; }}
  .kpi-grid {{ display: flex; flex-wrap: wrap; gap: 0.75rem; margin-bottom: 0.75rem; }}
  .kpi {{ background: #fff; border: 1px solid #ddd; border-left: 4px solid var(--gold); border-radius: 6px; padding: 0.65rem 0.9rem; flex: 1 1 170px; min-width: 150px; }}
  .kpi .zahl {{ font-size: 1.3rem; font-weight: 700; display: block; color: var(--anthrazit); }}
  .kpi .kpi-label {{ font-size: 0.76rem; color: #666; }}
  .badge {{ display: inline-block; padding: 0.05rem 0.4rem; border-radius: 3px; font-size: 0.78rem; font-weight: 600; }}
  .badge-error {{ background: #fbeaea; color: #b00020; }}
  .badge-warn {{ background: #fdf0dc; color: #a15c00; }}
  .badge-ok {{ background: #e6f4ea; color: #1a7f37; }}
  .badge-muted {{ background: #eee; color: #555; }}
  .tabelle-scroll {{ overflow-x: auto; max-width: 100%; }}
  label {{ display: block; margin: 0.5rem 0 0.15rem; font-weight: 600; font-size: 0.85rem; }}
  input, select, textarea {{ width: 100%; box-sizing: border-box; padding: 0.35rem 0.5rem; font: inherit; border: 1px solid #ccc; border-radius: 4px; }}
  button, input[type=submit] {{ font: inherit; padding: 0.45rem 0.95rem; cursor: pointer; border-radius: 4px; border: 1px solid var(--anthrazit); background: var(--anthrazit); color: #fff; width: auto; margin-top: 0.6rem; }}
  button:hover, input[type=submit]:hover {{ background: var(--anthrazit-hell); }}
  button.secondary {{ background: #fff; color: var(--anthrazit); }}
  button.secondary:hover {{ background: #f0ede5; }}
  button.gross {{ background: var(--gold); color: var(--anthrazit); border-color: var(--gold-dunkel); font-size: 1rem; padding: 0.6rem 1.3rem; font-weight: 700; }}
  button.gross:hover {{ background: var(--gold-dunkel); color: #fff; }}
  fieldset {{ border: 1px solid #ddd; border-radius: 6px; margin-bottom: 1rem; padding: 0.75rem 1rem; }}
  nav.tabs a {{ margin-right: 1rem; font-size: 0.9rem; }}
  code {{ background: #f0f0f0; padding: 0.05rem 0.3rem; border-radius: 3px; }}
  .bereich-karten {{ display: flex; flex-wrap: wrap; gap: 0.9rem; }}
  .bereich-karten .card {{ flex: 1 1 260px; margin-bottom: 0; }}
  .todo-liste {{ list-style: none; margin: 0; padding: 0; }}
  .todo-liste li {{ padding: 0.4rem 0; border-bottom: 1px solid #eee; }}
  .todo-liste li:last-child {{ border-bottom: none; }}
  .status-zeile {{ display: flex; flex-wrap: wrap; gap: 0.5rem 1.5rem; font-size: 0.85rem; color: #555; margin: 0.5rem 0; }}
  details > summary {{ cursor: pointer; color: var(--anthrazit); font-weight: 600; }}
  @media (max-width: 640px) {{
    main {{ padding: 0.75rem; }}
    header {{ padding: 0.5rem 0.75rem; }}
    nav.hauptnav {{ padding: 0 0.5rem; }}
    nav.hauptnav a {{ padding: 0.6rem 0.55rem; font-size: 0.82rem; }}
    .kpi {{ flex: 1 1 100%; }}
    .bereich-karten .card {{ flex: 1 1 100%; }}
  }}
</style>
</head>
<body>
<div class="pilot-banner">{h(banner_text)}</div>
<header>
  <a href="/backoffice/">JLB Hausverwaltung</a>
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
