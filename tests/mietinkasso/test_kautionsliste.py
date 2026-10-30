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
