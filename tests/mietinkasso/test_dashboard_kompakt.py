"""Synthetische Renderer-Tests der Startseite (HV-20261030-UEBERSICHT-EINFACH).

Lädt nur die benötigten Funktionen per AST aus dashboard.py: kein App-Import,
keine Datenbank, keine echten Daten. Die Integration bleibt separate Abnahme."""

from __future__ import annotations

import ast
from html import escape
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

QUELLE = Path(__file__).resolve().parents[2] / "src/mietinkasso/backoffice/routes/dashboard.py"
KENNZAHLEN = SimpleNamespace(
    summe_positiver_kontostaende_cent=101,
    ueberfaellig_cent=202,
    faelligkeit_unbekannt_cent=303,
    summe_guthaben_cent=404,
    nicht_faellig_cent=505,
)


def _baum() -> ast.Module:
    return ast.parse(QUELLE.read_text(encoding="utf-8"))


def _lade(*namen: str) -> dict:
    knoten = [
        n
        for n in _baum().body
        if (isinstance(n, ast.FunctionDef) and n.name in namen)
        or (isinstance(n, ast.Assign) and any(getattr(t, "id", None) in namen for t in n.targets))
    ]
    ns = {
        "h": escape,
        "eur": lambda cent: f"EUR[{cent}]",
        "urlencode": urlencode,
        "_abgleich_nachweis_satz": lambda status: "SENTINEL-NACHWEIS",
    }
    exec(compile(ast.Module(body=knoten, type_ignores=[]), str(QUELLE), "exec"), ns)
    return ns


def test_oben_nur_offen_und_guthaben_rest_aufklappbar() -> None:
    html = _lade("_rueckstaende_kpi_html")["_rueckstaende_kpi_html"](KENNZAHLEN)
    oben, details = html.split("<details", 1)
    for wert in ("EUR[101]", "EUR[404]"):
        assert wert in oben and wert not in details
    for wert in ("EUR[202]", "EUR[303]", "EUR[505]"):
        assert wert in details and wert not in oben
    assert "Weitere Summen und Erklärung" in details
    assert "kpi-grid" not in html


def test_erklaerungen_bleiben_erhalten() -> None:
    html = _lade("_rueckstaende_kpi_html")["_rueckstaende_kpi_html"](KENNZAHLEN)
    for text in ("KEINE Mahnfreigabe", 'NICHT automatisch "strittig"', "wird nicht automatisch verrechnet"):
        assert text in html


def test_abgleichkarte_ohne_doppelte_summen() -> None:
    ns = _lade("_abgleich_link", "_abgleich_karte_html")
    status = SimpleNamespace(
        summe_offen_cent=111,
        summe_guthaben_cent=222,
        einzuege_offen_cent=333,
        einzuege_offen_anzahl=4,
        objekt_filter="objekt-a",
    )
    html = ns["_abgleich_karte_html"](status)
    for text in (
        'id="abgleichstand"',
        "SENTINEL-NACHWEIS",
        "Eingereicht, Bankeingang noch offen",
        "EUR[333]",
        "(4)",
        "/backoffice/abgleichstatus?objekt_id=objekt-a",
        "Abgleichstand im Detail",
    ):
        assert text in html
    for text in ("EUR[111]", "EUR[222]", "Offene Beträge", "Guthaben"):
        assert text not in html


def test_startseitenrahmen_und_css_bleiben_lokal() -> None:
    ns = _lade("_HV_START_CSS", "_startseite_html")
    html = ns["_startseite_html"]("ERSTER", "ZWEITER")
    assert html.count("<h1>Übersicht</h1>") == 1
    assert 'class="hv-start"' in html
    assert html.index("ERSTER") < html.index("ZWEITER")
    selektoren = [z.split("{")[0].strip() for z in ns["_HV_START_CSS"].splitlines() if "{" in z]
    assert selektoren and all(s.startswith(".hv-start") for s in selektoren)
    for verboten in ("@import", "url(", "animation", "http"):
        assert verboten not in ns["_HV_START_CSS"]


def test_dashboard_reihenfolge() -> None:
    dashboard = next(n for n in _baum().body if isinstance(n, ast.FunctionDef) and n.name == "dashboard")
    aufruf = next(
        n
        for n in ast.walk(dashboard)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_startseite_html"
    )
    assert [a.id for a in aufruf.args] == [
        "auswahl_form",
        "kpi_html",
        "abgleich_html",
        "kompakt_tabelle",
        "erledigen_html",
        "mietkonten_tabelle",
        "positionen_tabelle",
        "mahnfaelle_tabelle",
        "bestand_tabelle",
    ]
