"""Bestandfilter, Vertragsende-Grenze und Verwaltungsende (nur synthetische Daten)."""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace as NS

import pytest

from mietinkasso.backoffice import dependencies as deps
from mietinkasso.backoffice.routes import vertragsanlage as route
from mietinkasso.backoffice.routes.listen import _mieter_zeile_html
from mietinkasso.backoffice.vertragsanlage_form import vertraege_liste_formular
from mietinkasso.infrastructure.db.tables import EinheitTable
from mietinkasso.portallisten.service import berechne_mieterliste, berechne_zinsliste
from mietinkasso.stammdaten.bestand import (
    bestand_status,
    einheit_in_verwaltung_am,
    normalisiere_bestand_filter,
    status_passt_zu_bestandfilter,
    vertrag_status_am,
)

HEUTE = date(2026, 10, 15)
GESTERN = HEUTE - timedelta(days=1)
BEGINN = date(2020, 1, 1)
CTX = NS(has_zugriff=lambda gesellschaft_id: gesellschaft_id == "G1")


def _objekt(objekt_id, *, ausgeschlossen=False, gesellschaft_id="G1"):
    return NS(id=objekt_id, gesellschaft_id=gesellschaft_id, bezeichnung=objekt_id, adresse=None, ausgeschlossen=ausgeschlossen)


def _einheit(einheit_id, *, objekt_id="O1", verwaltet_bis=None, nutzungsstatus="DAUERVERMIETUNG"):
    return NS(
        id=einheit_id, objekt_id=objekt_id, bezeichnung=einheit_id, nutzungsstatus=nutzungsstatus,
        verwaltet_bis=verwaltet_bis, verwaltungsende_grund=None,
    )


def _vertrag(vertrag_id, einheit_id, *, von=BEGINN, bis=None, gesellschaft_id="G1"):
    return NS(
        id=vertrag_id, einheit_id=einheit_id, debitor_id=f"D-{vertrag_id}", gesellschaft_id=gesellschaft_id,
        gueltig_von=von, gueltig_bis=bis,
    )


class _Repo:
    def __init__(self, objekte, einheiten, vertraege):
        self.objekte, self.einheiten, self.vertraege = objekte, einheiten, vertraege

    def list_gesellschaften(self):
        return [NS(id="G1", name="Gesellschaft")]

    def list_objekte(self, gesellschaft_id=None):
        return self.objekte

    def get_objekt(self, objekt_id):
        return next((o for o in self.objekte if o.id == objekt_id), None)

    def get_einheit(self, einheit_id):
        return next((e for e in self.einheiten if e.id == einheit_id), None)

    def get_debitor(self, debitor_id):
        return NS(id=debitor_id, name=f"Mieter {debitor_id}", adresse=None, telefon=None, email=None)

    def list_einheiten_fuer_objekt(self, objekt_id):
        return [e for e in self.einheiten if e.objekt_id == objekt_id]

    def list_vertraege_fuer_objekt(self, objekt_id):
        ids = {e.id for e in self.list_einheiten_fuer_objekt(objekt_id)}
        return [v for v in self.vertraege if v.einheit_id in ids]

    def list_alle_vertraege(self):
        return self.vertraege

    def list_aktive_komponenten(self, vertrag_id, stichtag):
        return [NS(id=f"K-{vertrag_id}", art="HMZ", bezeichnung="HMZ", betrag_cent=1000, gueltig_von=BEGINN, gueltig_bis=None)]

    def list_komponenten_im_zeitraum(self, vertrag_id, von, bis):
        return self.list_aktive_komponenten(vertrag_id, von)

    def neuestes_mietvertragsprofil(self, vertrag_id):
        return None


@pytest.mark.parametrize(("roh", "erwartet"), [(None, "aktiv"), ("", "aktiv"), ("xyz", "aktiv"), (" Beendet ", "beendet"), ("alle", "alle")])
def test_unbekannter_filter_wird_normalisiert(roh, erwartet):
    assert normalisiere_bestand_filter(roh) == erwartet


@pytest.mark.parametrize(
    ("von", "bis", "erwartet"),
    [(BEGINN, HEUTE, "AKTIV"), (BEGINN, GESTERN, "BEENDET"), (BEGINN, None, "AKTIV"), (HEUTE + timedelta(days=1), None, "KUENFTIG")],
)
def test_vertragsende_grenze(von, bis, erwartet):
    assert vertrag_status_am(NS(gueltig_von=von, gueltig_bis=bis), HEUTE) == erwartet


def test_verwaltet_bis_inklusive_und_alte_fixture():
    assert einheit_in_verwaltung_am(_einheit("E"), HEUTE)
    assert einheit_in_verwaltung_am(NS(id="alt"), HEUTE)
    assert einheit_in_verwaltung_am(_einheit("E", verwaltet_bis=HEUTE), HEUTE)
    assert not einheit_in_verwaltung_am(_einheit("E", verwaltet_bis=GESTERN), HEUTE)


def test_verwaltungsende_aendert_vertragslaufzeit_nicht():
    vertrag = _vertrag("V", "E")
    status = bestand_status(vertrag, _einheit("E", verwaltet_bis=GESTERN), HEUTE)
    assert status == "VERWALTUNG_BEENDET" and vertrag.gueltig_bis is None
    assert status_passt_zu_bestandfilter(status, "beendet")
    assert not status_passt_zu_bestandfilter(status, "aktiv")


def test_einheit_spalten_sind_additiv_nullable():
    spalten = EinheitTable.__table__.c
    assert all(spalten[n].nullable for n in ("verwaltet_bis", "verwaltungsende_grund", "verwaltungsende_quelle"))


def test_mieterliste_trennt_ausgeschiedene_einheit():
    repo = _Repo([_objekt("O1")], [_einheit("E1"), _einheit("E2", verwaltet_bis=GESTERN)], [_vertrag("V1", "E1"), _vertrag("V2", "E2")])
    aktiv = berechne_mieterliste(ctx=CTX, objekt_id=None, stammdaten_repository=repo, heute=HEUTE)
    beendet = berechne_mieterliste(ctx=CTX, objekt_id=None, stammdaten_repository=repo, status="beendet", heute=HEUTE)
    assert [z.vertrag_id for z in aktiv.zeilen] == ["V1"]
    assert [z.vertrag_id for z in beendet.zeilen] == ["V2"]
    assert beendet.zeilen[0].verwaltung_beendet and beendet.zeilen[0].gueltig_bis is None


def test_zinsliste_ignoriert_einheit_erst_nach_monatsanfang():
    repo = _Repo([_objekt("O1")], [_einheit("E1", verwaltet_bis=date(2026, 9, 15))], [_vertrag("V1", "E1")])
    september = berechne_zinsliste(ctx=CTX, monat="2026-09", objekt_id=None, stammdaten_repository=repo)
    oktober = berechne_zinsliste(ctx=CTX, monat="2026-10", objekt_id=None, stammdaten_repository=repo)
    assert [z.vertrag_id for z in september.zeilen] == ["V1"]
    assert september.zeilen[0].brutto_gesamt_cent == 1000
    assert oktober.zeilen == ()


def _liste(monkeypatch, repo, **filter_werte):
    erfasst = {}

    def formular(zeilen, csrf, **optionen):
        erfasst.update(optionen, zeilen=zeilen)
        return ""

    monkeypatch.setattr(deps, "_stammdaten_repo", repo, raising=False)
    monkeypatch.setattr(route, "_ctx", lambda session: None)
    monkeypatch.setattr(route, "_hat_gesellschaft_zugriff", lambda ctx, gesellschaft_id: gesellschaft_id == "G1")
    monkeypatch.setattr(route, "heute_wien", lambda: HEUTE)
    monkeypatch.setattr(route, "listen_links_html", lambda: "")
    monkeypatch.setattr(route, "_layout", lambda *args, **kwargs: None)
    monkeypatch.setattr(route, "_vertragsanlage_liste_formular", formular)
    route.vertragsanlage_liste(NS(), session=NS(csrf_token="x"), **filter_werte)
    return {z["vertrag"].id for z in erfasst["zeilen"]}, {z["einheit"].id for z in erfasst["leerstand_zeilen"]}


@pytest.fixture
def bestand_repo():
    return _Repo(
        [_objekt("O1"), _objekt("O2"), _objekt("O3", ausgeschlossen=True)],
        [
            _einheit("E-LAUF"), _einheit("E-KUENFTIG"), _einheit("E-ENDE"), _einheit("E-VERKAUFT", verwaltet_bis=GESTERN),
            _einheit("E-FREMD"), _einheit("E-OHNE"), _einheit("E-OHNE-WEG", verwaltet_bis=GESTERN),
            _einheit("E-O2", objekt_id="O2"), _einheit("E-O3", objekt_id="O3"),
        ],
        [
            _vertrag("V-LAUF", "E-LAUF"), _vertrag("V-KUENFTIG", "E-KUENFTIG", von=HEUTE + timedelta(days=1)),
            _vertrag("V-ENDE", "E-ENDE", bis=GESTERN), _vertrag("V-VERKAUFT", "E-VERKAUFT"),
            _vertrag("V-FREMD", "E-FREMD", gesellschaft_id="G2"), _vertrag("V-O3", "E-O3"),
        ],
    )


def test_liste_standard_und_unbekannter_filter_zeigen_aktiven_bestand(monkeypatch, bestand_repo):
    erwartet = ({"V-LAUF", "V-KUENFTIG"}, {"E-OHNE", "E-O2"})
    assert _liste(monkeypatch, bestand_repo) == erwartet
    assert _liste(monkeypatch, bestand_repo, bestand="xyz") == erwartet


def test_liste_beendet_alle_und_objektfilter(monkeypatch, bestand_repo):
    assert _liste(monkeypatch, bestand_repo, bestand="beendet") == ({"V-ENDE", "V-VERKAUFT"}, {"E-OHNE-WEG"})
    assert _liste(monkeypatch, bestand_repo, bestand="alle") == (
        {"V-LAUF", "V-KUENFTIG", "V-ENDE", "V-VERKAUFT"}, {"E-OHNE", "E-OHNE-WEG", "E-O2"},
    )
    assert _liste(monkeypatch, bestand_repo, objekt_id="O1") == ({"V-LAUF", "V-KUENFTIG"}, {"E-OHNE"})


def test_formular_zeigt_status_und_bestandfilter():
    einheit = _einheit("E", verwaltet_bis=GESTERN)
    zeile = {
        "vertrag": _vertrag("V", "E"), "objekt": _objekt("O1"), "einheit": einheit,
        "debitor": NS(name="Mieter"), "profil": None, "bestand_status": "VERWALTUNG_BEENDET",
    }
    html = vertraege_liste_formular([zeile], "csrf", filter_bestand="alle")
    assert 'name="bestand"' in html and "Verwaltung beendet" in html and GESTERN.isoformat() in html


def _mieter_zeile(**werte):
    basis = dict(
        status="BEENDET", gueltig_von=BEGINN, gueltig_bis=None, debitor_name="Mieter", objekt_bezeichnung="O1",
        einheit_bezeichnung="E1", objekt_adresse=None, korrespondenzadresse=None, telefon=None, email=None, vertrag_id="V1",
    )
    return NS(**{**basis, **werte})


def test_mieter_zeile_verwaltungsende_ist_kein_vertragsende():
    html = _mieter_zeile_html(_mieter_zeile(verwaltung_beendet=True, verwaltet_bis=GESTERN), None)
    assert f"Verwaltung beendet am {GESTERN.isoformat()}" in html
    assert ">beendet am" not in html and "ehemaliger Mieter" not in html


def test_mieter_zeile_verwaltungsende_ohne_datum():
    html = _mieter_zeile_html(_mieter_zeile(verwaltung_beendet=True), None)
    assert "Verwaltung beendet" in html


def test_mieter_zeile_alte_fixture_bleibt_vertragsende():
    html = _mieter_zeile_html(_mieter_zeile(gueltig_bis=GESTERN), None)
    assert f"beendet am {GESTERN.isoformat()}" in html and "ehemaliger Mieter" in html


def test_mieter_zeile_aus_service_zeigt_verwaltungsende():
    repo = _Repo([_objekt("O1")], [_einheit("E2", verwaltet_bis=GESTERN)], [_vertrag("V2", "E2")])
    liste = berechne_mieterliste(ctx=CTX, objekt_id=None, stammdaten_repository=repo, status="beendet", heute=HEUTE)
    assert f"Verwaltung beendet am {GESTERN.isoformat()}" in _mieter_zeile_html(liste.zeilen[0], None)


def test_liste_zeigt_objekt_fremder_gesellschaft_nicht(monkeypatch):
    repo = _Repo([_objekt("O-FREMD", gesellschaft_id="G2")], [_einheit("E", objekt_id="O-FREMD")], [_vertrag("V", "E")])
    assert _liste(monkeypatch, repo, bestand="alle") == (set(), set())
    assert _liste(monkeypatch, repo, objekt_id="O-FREMD") == (set(), set())


@pytest.fixture
def leerstand_repo():
    leer = {"nutzungsstatus": "LEERSTAND"}
    return _Repo(
        [_objekt("O1")],
        [
            _einheit("E-LEER", **leer), _einheit("E-LEER-LAUF", **leer), _einheit("E-LEER-KUENFTIG", **leer),
            _einheit("E-LEER-FREMD", **leer), _einheit("E-LEER-FREMD-LAUF", **leer),
            _einheit("E-LEER-WEG", verwaltet_bis=GESTERN, **leer), _einheit("E-DAUER-ENDE"),
        ],
        [
            _vertrag("V-LEER", "E-LEER", bis=GESTERN),
            _vertrag("V-ALT", "E-LEER-LAUF", bis=GESTERN), _vertrag("V-LAUF", "E-LEER-LAUF", von=HEUTE),
            _vertrag("V-ALT2", "E-LEER-KUENFTIG", bis=GESTERN), _vertrag("V-NEU", "E-LEER-KUENFTIG", von=HEUTE + timedelta(days=1)),
            _vertrag("V-FREMD", "E-LEER-FREMD", bis=GESTERN, gesellschaft_id="G2"),
            _vertrag("V-FREMD-LAUF", "E-LEER-FREMD-LAUF", gesellschaft_id="G2"),
            _vertrag("V-WEG", "E-LEER-WEG", bis=GESTERN), _vertrag("V-DAUER", "E-DAUER-ENDE", bis=GESTERN),
        ],
    )


def test_beendete_einheit_mit_gepflegtem_leerstand_in_aktiver_box(monkeypatch, leerstand_repo):
    vertraege, leerstand = _liste(monkeypatch, leerstand_repo)
    assert leerstand == {"E-LEER", "E-LEER-FREMD"}
    assert vertraege == {"V-LAUF", "V-NEU"}


def test_leerstand_unabhaengig_vom_namensfilter(monkeypatch, leerstand_repo):
    assert _liste(monkeypatch, leerstand_repo, q="niemand") == (set(), {"E-LEER", "E-LEER-FREMD"})


def test_gepflegter_leerstand_nicht_in_beendet_ansicht(monkeypatch, leerstand_repo):
    _, leerstand = _liste(monkeypatch, leerstand_repo, bestand="beendet")
    assert leerstand == set()
