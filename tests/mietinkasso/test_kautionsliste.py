"""Synthetische Tests der Kautionsübersicht; keine App-Imports beim Collection."""

import importlib
import inspect
from datetime import date
from types import SimpleNamespace as NS

import pytest

STICHTAG = date(2026, 10, 30)


def _kautionen():
    return importlib.import_module("mietinkasso.portallisten.kautionen")


def _ansicht():
    return importlib.import_module("mietinkasso.backoffice.kautionsliste_ansicht")


def _route():
    return importlib.import_module("mietinkasso.backoffice.routes.kautionen")


def _mieter(vertrag_id="V1", **werte):
    daten = dict(
        vertrag_id=vertrag_id,
        debitor_id="D1",
        debitor_name="Test Mieter",
        objekt_id="O1",
        objekt_bezeichnung="Testhaus",
        objekt_adresse="Beispielweg 1",
        einheit_id="E1",
        einheit_bezeichnung="Top 1",
        gueltig_von=date(2024, 1, 1),
        gueltig_bis=None,
        status="AKTIV",
        verwaltung_beendet=False,
        verwaltet_bis=None,
    )
    daten.update(werte)
    return NS(**daten)


def _basis(*zeilen):
    return NS(
        objekt_filter=None,
        objekt_optionen=(NS(id="O1", bezeichnung="Testhaus", gesellschaft_id="G1", gesellschaft_name="Test GmbH"),),
        status_filter="alle",
        suche=None,
        stichtag=STICHTAG,
        zeilen=zeilen,
    )


class FakeRepo:
    def __init__(self, profile=None, kautionen=None):
        self.profile = profile or {}
        self.kautionen = kautionen or {}
        self.aufrufe = []

    def neuestes_mietvertragsprofil(self, vertrag_id):
        self.aufrufe.append(("profil", vertrag_id))
        return self.profile.get(vertrag_id)

    def get_kaution(self, vertrag_id):
        self.aufrufe.append(("kaution", vertrag_id))
        return self.kautionen.get(vertrag_id)


def _profil(cent, quelle=None):
    return NS(vertragliche_kaution_cent=cent, vertragliche_kaution_quellenbeleg=quelle)


def _kaution(cent, stichtag=STICHTAG, referenz=None):
    return NS(betrag_cent=cent, stichtag=stichtag, referenz=referenz)


def _liste(monkeypatch, basis, repo):
    modul = _kautionen()
    monkeypatch.setattr(modul, "berechne_mieterliste", lambda **_: basis)
    return modul.berechne_kautionsliste("ctx", None, repo)


def test_vereinbart_ist_kein_eingang(monkeypatch):
    repo = FakeRepo(profile={"V1": _profil(100000)})
    zeile = _liste(monkeypatch, _basis(_mieter()), repo).zeilen[0]
    assert zeile.vereinbart_cent == 100000
    assert zeile.eingang_cent is None
    assert zeile.status_label == "Eingang nicht belegt"


def test_none_ist_nicht_null_und_zeile_bleibt(monkeypatch):
    liste = _liste(monkeypatch, _basis(_mieter()), FakeRepo())
    html = _ansicht().rendere_kautionsliste(liste, logo="", navigation="")
    assert len(liste.zeilen) == 1
    assert liste.vereinbart_summe_cent is None
    assert liste.zeilen[0].status_label == "Kautionsvereinbarung fehlt"
    assert "nicht erfasst" in html
    assert "0,00 €" not in html


def test_vereinbart_null_und_erfasster_nulleingang(monkeypatch):
    repo = FakeRepo(
        profile={"V1": _profil(0), "V2": _profil(50000)},
        kautionen={"V2": _kaution(0)},
    )
    liste = _liste(monkeypatch, _basis(_mieter(), _mieter("V2")), repo)
    assert liste.zeilen[0].status_label == "Keine Kaution vereinbart"
    assert liste.zeilen[1].eingang_cent == 0
    assert liste.eingang_fehlend == 0  # Keine Kaution ist kein fehlender Zahlungsnachweis.
    assert liste.eingang_summe_cent == 0
    html = _ansicht().rendere_kautionsliste(liste, logo="", navigation="")
    assert "0,00 €" in html


def test_scope_fehler_vor_repo_aufrufen(monkeypatch):
    modul = _kautionen()
    repo = FakeRepo()

    def verweigert(**_):
        raise ValueError("kein Zugriff")

    monkeypatch.setattr(modul, "berechne_mieterliste", verweigert)
    with pytest.raises(ValueError):
        modul.berechne_kautionsliste("ctx", "fremd", repo)
    assert repo.aufrufe == []


def test_filter_unveraendert_weitergegeben(monkeypatch):
    modul = _kautionen()
    gesehen = {}

    def basisfunktion(**kwargs):
        gesehen.update(kwargs)
        return _basis()

    monkeypatch.setattr(modul, "berechne_mieterliste", basisfunktion)
    repo = FakeRepo()
    modul.berechne_kautionsliste("ctx", "O1", repo, status="beendet", suche=" Test ", heute=STICHTAG)
    assert gesehen == {
        "ctx": "ctx",
        "objekt_id": "O1",
        "stammdaten_repository": repo,
        "status": "beendet",
        "suche": " Test ",
        "heute": STICHTAG,
    }


def test_summen_und_zukunftsstichtag(monkeypatch):
    repo = FakeRepo(
        profile={"V1": _profil(100000), "V2": _profil(None)},
        kautionen={"V1": _kaution(90000), "V2": _kaution(70000, date(2026, 11, 1))},
    )
    liste = _liste(monkeypatch, _basis(_mieter(), _mieter("V2"), _mieter("V3")), repo)
    assert (liste.vereinbart_summe_cent, liste.vereinbart_bekannt, liste.vereinbart_fehlend) == (100000, 1, 2)
    assert (liste.eingang_summe_cent, liste.eingang_bekannt, liste.eingang_fehlend) == (90000, 1, 1)
    assert liste.eingang_zukuenftig == 1
    assert liste.zeilen[1].status_label == "Kautionsvereinbarung fehlt"
    assert "Zukünftig" in _ansicht().rendere_kautionsliste(liste, logo="", navigation="")


def test_fehlende_repomethoden_ergeben_none(monkeypatch):
    zeile = _liste(monkeypatch, _basis(_mieter()), object()).zeilen[0]
    assert zeile.vereinbart_cent is None
    assert zeile.eingang_cent is None


def test_beendet_und_verwaltungsende_unveraendert(monkeypatch):
    mieter = _mieter(status="BEENDET", verwaltung_beendet=True, verwaltet_bis=date(2025, 12, 31))
    liste = _liste(monkeypatch, _basis(mieter), FakeRepo())
    html = _ansicht().rendere_kautionsliste(liste, logo="", navigation="")
    assert liste.zeilen[0].mieter is mieter
    assert "Beendet" in html
    assert "Verwaltung beendet zum 31.12.2025" in html


def test_xss_aktenlink_filter_und_druck(monkeypatch):
    boese = "<script>alert(1)</script>"
    basis = _basis(_mieter("V/1", debitor_name=boese))
    basis.suche = '"><img src=x>'
    repo = FakeRepo(profile={"V/1": _profil(100, boese)})
    liste = _liste(monkeypatch, basis, repo)
    html = _ansicht().rendere_kautionsliste(liste, logo="<i>Logo</i>", navigation="<nav>N</nav>")
    assert boese not in html
    assert "<img src=x>" not in html
    assert "&lt;script&gt;" in html
    assert "/backoffice/vertrag/V%2F1?von_objekt=O1" in html
    assert 'name="objekt_id"' in html and 'name="q"' in html and 'name="status"' in html
    assert "window.print()" in html and ">Drucken<" in html
    assert "<i>Logo</i>" in html and "<nav>N</nav>" in html


def test_route_nutzt_auth_und_reicht_filter_weiter(monkeypatch):
    route = _route()
    parameter = inspect.signature(route.kautionen_seite).parameters["session"]
    assert parameter.default.dependency is route._current_session
    assert route.router.routes[0].path == "/kautionen"
    gesehen = {}

    def liste(**kwargs):
        gesehen.update(kwargs)
        return NS(basis=NS(objekt_filter="O1"))

    monkeypatch.setattr(route, "deps", NS(_stammdaten_repo="repo", _settings=NS()))
    monkeypatch.setattr(route, "_ctx", lambda session: "ctx")
    monkeypatch.setattr(route, "berechne_kautionsliste", liste)
    monkeypatch.setattr(route, "logo_html", lambda pfad: "logo")
    monkeypatch.setattr(route, "shared", NS(listen_links_html=lambda **_: "nav"))
    monkeypatch.setattr(route, "rendere_kautionsliste", lambda *_, **__: "html")
    monkeypatch.setattr(route, "_layout", lambda *args: args[2:])
    antwort = route.kautionen_seite(request="req", objekt_id="O1", q="x", status="aktiv", session="s")
    assert antwort == ("Kautionsübersicht", "html")
    assert gesehen["objekt_id"] == "O1" and gesehen["suche"] == "x" and gesehen["status"] == "aktiv"


def test_route_fehlerseite(monkeypatch):
    route = _route()

    def fehler(**_):
        raise ValueError("ungültig")

    monkeypatch.setattr(route, "deps", NS(_stammdaten_repo="repo", _settings=NS()))
    monkeypatch.setattr(route, "_ctx", lambda session: "ctx")
    monkeypatch.setattr(route, "berechne_kautionsliste", fehler)
    monkeypatch.setattr(route, "_fehlerseite", lambda *args: args)
    antwort = route.kautionen_seite(request="req", objekt_id=None, q=None, status="alle", session="s")
    assert antwort == ("s", "Kautionsübersicht", "ungültig", "/backoffice/kautionen")


def _html(liste):
    return _ansicht().rendere_kautionsliste(liste, logo="", navigation="")


def test_fuenf_spalten_ohne_tabellensummen(monkeypatch):
    html = _html(_liste(monkeypatch, _basis(_mieter()), FakeRepo()))
    kopf = html.split("<thead>")[1].split("</thead>")[0]
    for titel in ("Mieter / Objekt / Einheit", "Kaution laut Vertrag", "Bestätigter Eingang", "Einordnung", "Akte / Details"):
        assert titel in kopf
    assert kopf.count("</th>") == 5
    assert html.count("</td>") == 5
    assert "<tfoot" not in html
    assert 'colspan="5"' in _html(_liste(monkeypatch, _basis(), FakeRepo()))


def test_responsive_markup_und_druck(monkeypatch):
    html = _html(_liste(monkeypatch, _basis(_mieter()), FakeRepo()))
    assert "@media screen and (max-width:700px)" in html
    assert "min-width:1100px" not in html
    assert html.count(' data-label="') == 5
    assert 'data-label="Einordnung"' in html
    druck = html.split("@media print")[1]
    assert ".kaut-erlaeuterung,.kaut-akte,.kaut-details:not([open]){display:none}" in druck
    assert ".kaut-nur-druck{display:block}" in druck


def test_zeilenzustaende_und_anzahlen(monkeypatch):
    repo = FakeRepo(
        profile={
            "V1": _profil(100000),
            "V2": _profil(None),
            "V3": _profil(0),
            "V4": _profil(0),
            "V5": _profil(50000),
            "V6": _profil(100000),
            "V7": _profil(100000),
        },
        kautionen={
            "V4": _kaution(0),
            "V5": _kaution(0),
            "V6": _kaution(90000),
            "V7": _kaution(70000, date(2026, 11, 1)),
        },
    )
    mieter = [_mieter(f"V{nummer}") for nummer in range(1, 8)]
    html = _html(_liste(monkeypatch, _basis(*mieter), repo))
    for teil in (
        "<span>Mietverhältnisse: 7</span>",
        "<span>Eingänge bestätigt: 2</span>",
        "<span>Nachweis fehlt: 2</span>",
        "<span>Keine Kaution: 2</span>",
        "<span>Zukünftig datiert: 1</span>",
        '<span class="kaut-marke kaut-marke-offen">Eingang nicht belegt</span>',
        '<span class="kaut-marke kaut-marke-offen">Kautionsvereinbarung fehlt</span>',
        '<span class="kaut-marke kaut-marke-neutral">Keine Kaution vereinbart</span>',
        '<span class="kaut-marke kaut-marke-ok">Eingang bestätigt</span>',
        "entfällt",
        "nicht belegt",
        "unter bekannt: 1 Eingang 0,00 € ohne vereinbarte Kaution",
    ):
        assert teil in html
    assert "unbezahlt" not in html.casefold()


def test_zukuenftige_bestaetigung_bleibt_ausserhalb_der_summe(monkeypatch):
    repo = FakeRepo(profile={"V1": _profil(100000)}, kautionen={"V1": _kaution(70000, date(2026, 11, 1))})
    liste = _liste(monkeypatch, _basis(_mieter()), repo)
    html = _html(liste)
    assert liste.eingang_summe_cent is None
    assert html.count("700,00 €") == 1
    assert "Zukünftig: Datum 01.11.2026 liegt nach dem Listenstichtag" in html
    assert "Eingang nach Listenstichtag, nicht summiert" in html
    assert "<span>Eingänge bestätigt: 0</span>" in html
    assert "<span>Nachweis fehlt: 0</span>" in html


def test_eingangsdatum_sichtbar_und_fehlender_stichtag_bricht_nicht(monkeypatch):
    repo = FakeRepo(kautionen={"V1": _kaution(100), "V2": _kaution(200, None)})
    html = _html(_liste(monkeypatch, _basis(_mieter(), _mieter("V2")), repo))
    assert "Stichtag 30.10.2026" in html
    assert "Stichtag nicht erfasst" in html


def test_beendet_leitet_nichts_ab_und_keine_rohcodes(monkeypatch):
    beendet = _mieter(status="BEENDET")
    unbekannt = _mieter("V2", status="XYZ_ROH")
    repo = FakeRepo(profile={"V1": _profil(100000)}, kautionen={"V1": _kaution(100000)})
    html = _html(_liste(monkeypatch, _basis(beendet, unbekannt), repo))
    assert '<span class="kaut-marke kaut-marke-neutral">Beendet</span>' in html
    assert '<span class="kaut-marke kaut-marke-ok">Eingang bestätigt</span>' in html
    assert "auszahl" not in html.casefold() and "erledig" not in html.casefold()
    assert "Status unbekannt" in html
    assert html.count("XYZ_ROH") == 1
    assert "<div>Vertragsstatus: XYZ_ROH</div>" in html


def test_quellen_und_akte_bleiben_erhalten(monkeypatch):
    repo = FakeRepo(
        profile={"V/1": _profil(100, "Vertrag S. 3")},
        kautionen={"V/1": _kaution(100, referenz="Beleg 7")},
    )
    html = _html(_liste(monkeypatch, _basis(_mieter("V/1"), _mieter("V2")), repo))
    assert "<summary>Details</summary>" in html
    assert "<div>Vertrag: Vertrag S. 3</div>" in html
    assert "<div>Eingang: Beleg 7</div>" in html
    assert "<div>Keine Quelle erfasst</div>" in html
    assert "<div>Vertragsstatus: AKTIV</div>" in html
    assert html.count("Akte öffnen") == 2
    assert "/backoffice/vertrag/V%2F1?von_objekt=O1" in html


def test_summen_und_erlaeuterung_unveraendert_aufklappbar(monkeypatch):
    repo = FakeRepo(profile={"V1": _profil(100000)}, kautionen={"V1": _kaution(90000)})
    liste = _liste(monkeypatch, _basis(_mieter(), _mieter("V2"), _mieter("V3")), repo)
    html = _html(liste)
    assert (liste.vereinbart_summe_cent, liste.eingang_summe_cent) == (100000, 90000)
    assert "Die Beträge zeigen die vorhandenen Unterlagen – nicht den heutigen Kontostand." in html
    aufklappbar = html.split("<summary>Summen und Erläuterung</summary>")[1].split("</details>")[0]
    druck = html.split('<div class="kaut-nur-druck">')[1].split("</div>")[0]
    for teil in (
        "Summe vertraglich vereinbarter Kautionen (kein Geldbestand)",
        "Summe bestätigter Eingänge bis Listenstichtag (kein Geldbestand)",
        "1.000,00 €",
        "900,00 €",
        "Umfang: 3 sichtbare Zeilen; 1 bekannt, 2 fehlend",
        "1 bekannt, 2 fehlend, 0 zukünftig (nicht summiert), 0 ohne Kaution",
        "Fehlende Angaben bedeuten weder 0 noch einen Zahlungsrückstand.",
    ):
        assert teil in aufklappbar and teil in druck


def test_xss_in_neuen_darstellungsfeldern(monkeypatch):
    boese = "<script>alert(1)</script>"
    mieter = _mieter(status=boese, objekt_bezeichnung=boese, objekt_adresse=boese, einheit_bezeichnung=boese)
    repo = FakeRepo(profile={"V1": _profil(100, boese)}, kautionen={"V1": _kaution(100, referenz=boese)})
    html = _html(_liste(monkeypatch, _basis(mieter), repo))
    assert boese not in html
    assert "&lt;script&gt;" in html
