"""Tests für die rein lesende Zinslisten-Detailansicht (ausschließlich synthetische Daten)."""

from __future__ import annotations

import html
import inspect
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.responses import HTMLResponse

from mietinkasso.backoffice.routes import listen
from mietinkasso.backoffice.views import eur
from mietinkasso.backoffice.zinsliste_ansicht import _details_html, _vpi_html, logo_html, rendere_zinsliste_ansicht, rendere_zinsliste_druck
from mietinkasso.domain.money import zerlege_brutto_cent
from mietinkasso.portallisten.service import berechne_zinsliste
from mietinkasso.portallisten.zinsdetails import berechne_zinsdetails

MONAT = "2026-09"
ANFANG = date(2026, 9, 1)
ENDE = date(2026, 9, 30)
CTX = SimpleNamespace(has_zugriff=lambda gesellschaft_id: gesellschaft_id == "G-1")


class FakeRepo:
    def __init__(self, einheiten=(), vertraege=(), komponenten=(), profile=None, debitoren=None):
        self.einheiten = list(einheiten)
        self.vertraege = list(vertraege)
        self.komponenten = list(komponenten)
        self.profile = profile or {}
        self.debitoren = debitoren or {}
        self.objekt = SimpleNamespace(id="O-1", bezeichnung="Testobjekt", adresse=None, ausgeschlossen=False)

    def list_gesellschaften(self):
        return [SimpleNamespace(id="G-1", name="Testgesellschaft")]

    def list_objekte(self, gesellschaft_id):
        return [self.objekt]

    def get_objekt(self, objekt_id):
        return self.objekt if objekt_id == "O-1" else None

    def list_vertraege_fuer_objekt(self, objekt_id):
        return list(self.vertraege)

    def list_einheiten_fuer_objekt(self, objekt_id):
        return list(self.einheiten)

    def get_einheit(self, einheit_id):
        return next((e for e in self.einheiten if e.id == einheit_id), None)

    def get_debitor(self, debitor_id):
        return self.debitoren.get(debitor_id)

    def list_komponenten_im_zeitraum(self, vertrag_id, von, bis):
        return [
            k for k in self.komponenten
            if k.vertrag_id == vertrag_id and k.gueltig_von <= bis and (k.gueltig_bis is None or k.gueltig_bis >= von)
        ]

    def list_aktive_komponenten(self, vertrag_id, stichtag):
        return [
            k for k in self.komponenten
            if k.vertrag_id == vertrag_id and k.gueltig_von <= stichtag
            and (k.gueltig_bis is None or k.gueltig_bis >= stichtag)
        ]

    def get_komponente(self, komponente_id):
        return next((k for k in self.komponenten if k.id == komponente_id), None)

    def neuestes_mietvertragsprofil(self, vertrag_id):
        return self.profile.get(vertrag_id)


def _einheit(einheit_id="E-1", status="DAUERVERMIETUNG", **extra):
    werte = dict(
        id=einheit_id, bezeichnung=f"Top {einheit_id}", nutzungsstatus=status, flaeche_qm=Decimal("54.30"),
        geschoss="2. OG", verwaltet_bis=None,
    )
    werte.update(extra)
    return SimpleNamespace(**werte)


def _vertrag(vertrag_id="V-1", einheit_id="E-1", debitor_id="D-1", von=date(2026, 1, 1), bis=None):
    return SimpleNamespace(
        id=vertrag_id, einheit_id=einheit_id, debitor_id=debitor_id, gesellschaft_id="G-1", gueltig_von=von, gueltig_bis=bis,
    )


def _komponente(komponente_id, art, cent, satz=10_000, vertrag_id="V-1", bezeichnung=None):
    return SimpleNamespace(
        id=komponente_id, vertrag_id=vertrag_id, art=art, bezeichnung=bezeichnung or art, betrag_cent=cent,
        ust_satz_promille=satz, gueltig_von=date(2026, 1, 1), gueltig_bis=None,
    )


def _repo(*komponenten, status="DAUERVERMIETUNG", profile=None, name="Testmieter Eins"):
    return FakeRepo(
        einheiten=[_einheit(status=status)], vertraege=[_vertrag()], komponenten=komponenten, profile=profile,
        debitoren={"D-1": SimpleNamespace(id="D-1", name=name)},
    )


def _details(repo, *, index_repository=None, suche=None, freigabe=None):
    liste = berechne_zinsliste(
        ctx=CTX, monat=MONAT, objekt_id=None, stammdaten_repository=repo, komponenten_freigabe_service=freigabe, suche=suche,
    )
    return liste, berechne_zinsdetails(liste=liste, stammdaten_repository=repo, index_repository=index_repository)


def _kategorie(zeile, name):
    return next(k.betraege for k in zeile.kategorien if k.kategorie == name)


def _ansicht(details):
    return rendere_zinsliste_ansicht(
        details, logo="", listen_links="", filter_html="", monatslinks=(), zeitstempel_html="", druck_href="/druck",
    )


def test_gemischte_steuern_und_rundung():
    _, details = _details(_repo(
        _komponente("K-1", "HMZ", 10_001), _komponente("K-2", "GARAGE", 5_005, 20_000), _komponente("K-3", "BK", 333, 0),
    ))
    gesamt = details.zeilen[0].gesamt
    assert (gesamt.netto_cent, gesamt.ust_cent, gesamt.brutto_cent) == (13_596, 1_743, 15_339)
    assert gesamt.netto_cent + gesamt.ust_cent == gesamt.brutto_cent
    assert [s.betraege.brutto_cent for s in details.summen.steuersaetze] == [333, 10_001, 5_005]
    assert zerlege_brutto_cent(10_001, 10_000) == (9_092, 909)


def test_summen_entstehen_aus_positionen_statt_neuer_zerlegung():
    _, details = _details(_repo(*(_komponente(f"K-{i}", "HMZ", 5) for i in range(3))))
    gesamt = details.summen.gesamt
    assert (gesamt.netto_cent, gesamt.ust_cent, gesamt.brutto_cent) == (15, 0, 15)


@pytest.mark.parametrize(
    ("art", "kategorie"),
    [
        ("HMZ", "HAUPTMIETZINS"), ("KUECHE", "KUECHE_INVENTAR"), ("KMI", "KUECHE_INVENTAR"), ("BK_VZ", "BK"),
        ("BK_PARKPLATZ", "BK"), ("BK", "BK"), ("WASSER", "WASSER"), ("HK_VZ", "HEIZUNG"), ("HEIZUNG", "HEIZUNG"),
        ("STROM", "STROM"), ("SAT", "SAT_INTERNET"), ("SATELLIT", "SAT_INTERNET"), ("INTERNET", "SAT_INTERNET"),
        ("PARKPLATZ", "PARK_GARAGE"), ("STELLPLATZ", "PARK_GARAGE"), ("GARAGE", "PARK_GARAGE"),
        ("NEUE_ART", "SONSTIGE"),
    ],
)
def test_kategorien_sind_exhaustiv(art, kategorie):
    _, details = _details(_repo(_komponente("K-1", art, 1_100, bezeichnung="Originaltext")))
    zeile = details.zeilen[0]
    assert _kategorie(zeile, kategorie).brutto_cent == 1_100
    assert sum(1 for k in zeile.kategorien if k.betraege is not None) == 1
    assert zeile.positionen[0].unbekannte_art is (art == "NEUE_ART")
    if art == "NEUE_ART":
        assert "Originaltext" in _ansicht(details)


def test_bruttokonstanz_zum_basismodell_auch_bei_ungueltiger_ust():
    liste, details = _details(_repo(_komponente("K-1", "HMZ", 1_000), _komponente("K-2", "BK", 777, 7_000)))
    assert details.summen.gesamt.brutto_cent == liste.summen.brutto_gesamt_cent == 1_777
    assert details.zeilen[0].gesamt.brutto_cent == liste.zeilen[0].brutto_gesamt_cent


def test_fehlende_betraege_sind_nicht_null_und_bekannte_null_bleibt():
    _, ohne = _details(_repo())
    assert ohne.zeilen[0].gesamt is None
    assert all(k.betraege is None for k in ohne.zeilen[0].kategorien)
    assert ohne.summen.gesamt.anzahl_positionen == 0
    assert "Betrag unbekannt" in _ansicht(ohne)
    assert eur(0) not in _ansicht(ohne).split("<tbody>", 1)[1].split("</tbody>", 1)[0]

    _, null = _details(_repo(_komponente("K-1", "HMZ", 0)))
    assert null.zeilen[0].gesamt.netto_cent == 0
    assert eur(0) in _ansicht(null)


def test_ungueltige_ust_erhaelt_brutto_und_zeigt_unbekannt():
    _, details = _details(_repo(_komponente("K-1", "HMZ", 1_100), _komponente("K-2", "BK", 500, 7_000)))
    gesamt = details.zeilen[0].gesamt
    assert (gesamt.netto_cent, gesamt.ust_cent, gesamt.brutto_cent) == (None, None, 1_600)
    assert (gesamt.netto_bekannt_cent, gesamt.ust_bekannt_cent, gesamt.brutto_ohne_aufteilung_cent) == (1_000, 100, 500)
    assert details.summen.ohne_gueltigen_satz.brutto_cent == 500
    ansicht = _ansicht(details)
    assert "unbekannt" in ansicht and "Teilsumme" in ansicht and "7000" in ansicht


def test_summen_nur_aus_gefilterten_zeilen():
    repo = FakeRepo(
        einheiten=[_einheit("E-1"), _einheit("E-2")],
        vertraege=[_vertrag(), _vertrag("V-2", "E-2", "D-2")],
        komponenten=[_komponente("K-1", "HMZ", 1_000), _komponente("K-2", "HMZ", 2_000, vertrag_id="V-2")],
        debitoren={"D-1": SimpleNamespace(name="Anna Test"), "D-2": SimpleNamespace(name="Bert Test")},
    )
    _, details = _details(repo, suche="Bert")
    assert len(details.zeilen) == 1
    assert details.summen.gesamt.brutto_cent == 2_000


def test_status_bleibt_und_variabel_wird_nicht_null():
    leerstand = FakeRepo(einheiten=[_einheit(status="LEERSTAND")])
    assert _details(leerstand)[1].zeilen[0].anzeige_name == "Leerstand"

    kzv = FakeRepo(einheiten=[_einheit(status="KURZZEITVERMIETUNG")])
    _, details = _details(kzv)
    zeile = details.zeilen[0]
    assert zeile.anzeige_name == "Kurzzeitvermietung"
    assert zeile.variabel_ohne_fix and zeile.gesamt is None
    assert details.summen.gesamt.anzahl_positionen == 0
    assert "variabel" in _ansicht(details)

    _, mit_vertrag = _details(_repo(_komponente("K-1", "HMZ", 900), status="KURZZEITVERMIETUNG", name="Technischer Partner"))
    zeile = mit_vertrag.zeilen[0]
    assert zeile.anzeige_name == "Kurzzeitvermietung" and zeile.vertragspartner == "Technischer Partner"
    assert not zeile.variabel_ohne_fix
    assert mit_vertrag.summen.gesamt.brutto_cent == 900


def test_befristung_und_original_mietbeginn():
    original = date(2019, 3, 1)
    profil = SimpleNamespace(urspruenglicher_mietbeginn=original, befristungsstatus="UNBEFRISTET", nutzungsart="BUERO")
    _, details = _details(_repo(_komponente("K-1", "HMZ", 100), profile={"V-1": profil}))
    zeile = details.zeilen[0]
    assert (zeile.mietbeginn, zeile.mietbeginn_quelle) == (original, "PROFIL")
    assert (zeile.befristung, zeile.nutzungsart) == ("UNBEFRISTET", "Büro")

    ende = date(2028, 12, 31)
    repo = _repo(_komponente("K-1", "HMZ", 100), profile={"V-1": SimpleNamespace(befristungsstatus="BEFRISTET")})
    repo.vertraege = [_vertrag(bis=ende)]
    _, details = _details(repo)
    zeile = details.zeilen[0]
    assert (zeile.mietbeginn, zeile.mietbeginn_quelle) == (date(2026, 1, 1), "VERTRAG")
    assert "31.12.2028" in _ansicht(details)

    _, ohne_profil = _details(_repo(_komponente("K-1", "HMZ", 100)))
    assert ohne_profil.zeilen[0].befristung == "UNGEKLAERT"


def test_vpi_freigegeben_vor_profil_und_profil_als_ungeprueft():
    profil = SimpleNamespace(
        index_reihe="VPI 2020", index_urspruenglicher_basismonat="2024-01", index_urspruenglicher_basiswert=Decimal("120.5000"),
    )
    repo = _repo(_komponente("K-1", "HMZ", 100), profile={"V-1": profil})
    _, ungeprueft = _details(repo)
    assert ungeprueft.zeilen[0].vpi.status == "ERFASST_UNGEPRUEFT"
    assert "erfasst / ungeprüft" in _ansicht(ungeprueft)

    klausel = SimpleNamespace(
        basis_reihe="VPI 2015", basis_monat="2023-05", basis_wert=Decimal("118.1000"), letzte_anpassung_monat="2026-04",
    )
    index = SimpleNamespace(freigegebene_klausel=lambda vertrag_id: klausel)
    _, frei = _details(repo, index_repository=index)
    assert frei.zeilen[0].vpi.status == "FREIGEGEBEN"
    ansicht = _ansicht(frei)
    assert "VPI 2015" in ansicht and "2026-04" in ansicht and "Klausel freigegeben" in ansicht
    assert "unabhängig vom Berichtsmonat" in ansicht


def test_alte_fixtures_ohne_profil_index_und_geschoss_bleiben_lauffaehig():
    repo = _repo(_komponente("K-1", "HMZ", 100))
    del repo.einheiten[0].geschoss
    repo.neuestes_mietvertragsprofil = None
    _, details = _details(repo, index_repository=object())
    assert details.zeilen[0].geschoss is None and details.zeilen[0].vpi is None


def test_geprueftes_netto_bleibt_und_abweichung_wird_sichtbar():
    freigabe = SimpleNamespace(
        aktive_freigabe_fuer_monat=lambda k_id, monatsanfang, monatsende: SimpleNamespace(bestaetigter_netto_betrag_cent=1)
    )
    liste, details = _details(_repo(_komponente("K-1", "HMZ", 1_100)), freigabe=freigabe)
    assert liste.zeilen[0].netto_miete_geprueft_cent == 1
    assert details.zeilen[0].gesamt.netto_cent == 1_000
    assert any("weicht von der rechnerischen Summe" in text for text in details.zeilen[0].hinweise)


def test_xss_wird_escaped():
    boese = "<script>alert(1)</script>"
    repo = _repo(_komponente("K-1", "NEUE_ART", 100, bezeichnung=boese), name=boese)
    repo.einheiten[0].geschoss = boese
    _, details = _details(repo, suche="script")
    for dokument in (_ansicht(details), rendere_zinsliste_druck(details, logo="", zurueck_href="/z")):
        assert boese not in dokument
        assert html.escape(boese) in dokument


@pytest.fixture
def route_umgebung(monkeypatch):
    repo = _repo(_komponente("K-1", "HMZ", 1_100))
    layout_aufrufe = []

    def layout(request, session, titel, inhalt):
        layout_aufrufe.append(titel)
        return HTMLResponse(inhalt)

    monkeypatch.setattr(listen, "_ctx", lambda session: CTX)
    monkeypatch.setattr(listen, "_layout", layout)
    monkeypatch.setattr(listen, "_fehlerseite", lambda session, titel, text, pfad: HTMLResponse(f"FEHLER {text}", 400))
    monkeypatch.setattr(listen, "listen_links_html", lambda **kwargs: "")
    monkeypatch.setattr(listen.deps, "_stammdaten_repo", repo, raising=False)
    monkeypatch.setattr(
        listen.deps, "_variableabrechnung", SimpleNamespace(service=None, komponenten_freigabe_service=None), raising=False,
    )
    monkeypatch.setattr(listen.deps, "_index_repo", None, raising=False)
    monkeypatch.setattr(listen.deps, "_settings", SimpleNamespace(brief_logo_pfad=None), raising=False)
    return SimpleNamespace(repo=repo, layout_aufrufe=layout_aufrufe)


def _route(**parameter):
    werte = {"monat": MONAT, "objekt_id": None, "q": None, "druck": False}
    werte.update(parameter)
    return listen.zinsliste(request=None, session=object(), **werte)


def test_route_nutzt_bestehende_auth_dependency():
    parameter = inspect.signature(listen.zinsliste).parameters
    assert parameter["session"].default.dependency is listen._current_session


def test_objektscope_greift_vor_anreicherung(route_umgebung, monkeypatch):
    aufrufe = []
    monkeypatch.setattr(listen, "berechne_zinsdetails", lambda **kwargs: aufrufe.append(kwargs))
    antwort = _route(objekt_id="O-FREMD")
    assert b"FEHLER" in antwort.body
    assert aufrufe == []


def test_druckansicht_erhaelt_filter_und_kommt_ohne_navigation(route_umgebung):
    normal = html.unescape(_route(objekt_id="O-1", q="Testmieter").body.decode())
    assert "/backoffice/zinsliste?monat=2026-09&objekt_id=O-1&q=Testmieter&druck=1" in normal

    route_umgebung.layout_aufrufe.clear()
    druck = _route(objekt_id="O-1", q="Testmieter", druck=True).body.decode()
    assert druck.startswith("<!doctype html>")
    assert route_umgebung.layout_aufrufe == []
    assert "size: A3 landscape" in druck and "margin: 8mm" in druck and "table-header-group" in druck
    assert "Testmieter" in druck and "Testobjekt" in druck


def test_logo_fallback_und_sichere_einbettung(tmp_path):
    assert "JLB" in logo_html(None)
    assert "JLB" in logo_html(tmp_path / "fehlt.png")
    falsch = tmp_path / "logo.png"
    falsch.write_bytes(b"<svg></svg>")
    assert "<img" not in logo_html(falsch)
    zu_gross = tmp_path / "gross.png"
    zu_gross.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * (1024 * 1024))
    assert "<img" not in logo_html(zu_gross)
    echt = tmp_path / "echt.png"
    echt.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 8)
    assert 'src="data:image/png;base64,' in logo_html(echt)


def test_druckansicht_ohne_logo_zeigt_textfallback(route_umgebung):
    assert 'zins-logo-text">JLB' in _route(druck=True).body.decode()


def _tbody(dokument):
    return dokument.split("<tbody>", 1)[1].split("</tbody>", 1)[0]


def _tfoot(dokument):
    return dokument.split("<tfoot>", 1)[1].split("</tfoot>", 1)[0]


def _druck(details):
    return rendere_zinsliste_druck(details, logo="", zurueck_href="/z")


def test_kategorien_und_tfoot_sind_netto():
    _, details = _details(_repo(_komponente("K-1", "HMZ", 1_100)))
    ansicht = _ansicht(details)
    zelle = f'<td class="zins-zahl">{eur(1_000)}</td>'
    assert "Miete &amp; Kosten (netto)" in ansicht
    assert _tbody(ansicht).count(zelle) == 2
    assert f'<td class="zins-zahl">{eur(1_100)}</td>' not in _tbody(ansicht)
    assert f"<strong>{eur(1_100)}</strong>" in _tbody(ansicht)
    assert _tfoot(ansicht).count(zelle) == 2
    assert _tfoot(ansicht).count(f'<td class="zins-zahl">{eur(1_100)}</td>') == 1


def test_ungueltige_steuer_zeigt_kategorie_unbekannt_statt_null():
    _, details = _details(_repo(_komponente("K-1", "BK", 500, 7_000)))
    ansicht = _ansicht(details)
    unbekannt = '<td class="zins-zahl"><span class="zins-warn">unbekannt</span></td>'
    null = f'<td class="zins-zahl">{eur(0)}</td>'
    for teil in (_tbody(ansicht), _tfoot(ansicht)):
        assert unbekannt in teil and null not in teil


def test_vpi_alias_monat_und_escape():
    def vpi(reihe, monat):
        return _vpi_html(SimpleNamespace(vpi=SimpleNamespace(
            reihe=reihe, basismonat=monat, basiswert=Decimal("120.5000"), letzte_anpassung=None,
            status="ERFASST_UNGEPRUEFT",
        )))

    bekannt = vpi("VPI20C18", "2024-01")
    assert "VPI 2020" in bekannt and "01.2024" in bekannt and "120,5000" in bekannt
    assert bekannt.count("<br>") <= 3
    assert "VPI 2015" in vpi("VPI15C18", None) and "VPI 1996" in vpi("VPI96", None) and "VPI 2000" in vpi("VPI00", None)
    boese = vpi("<i>X</i>", "2024-13<b>")
    assert "&lt;i&gt;X&lt;/i&gt;" in boese and "2024-13&lt;b&gt;" in boese and "13.2024" not in boese


def test_druck_unterdrueckt_routinehinweise_und_behaelt_prueffaelle():
    routine = SimpleNamespace(
        hinweise=("Routine",), positionen=(), gesamt=None,
        basis=SimpleNamespace(pruefbedarf=False, netto_miete_geprueft_cent=None),
    )
    assert _details_html(routine, True) == ""
    assert "Routine" in _details_html(routine, False)
    marke = '<ul class="zins-hinweise">'
    for status in ("LEERSTAND", "KURZZEITVERMIETUNG"):
        _, details = _details(FakeRepo(einheiten=[_einheit(status=status)]))
        assert marke not in _druck(details)
        for hinweis in details.zeilen[0].hinweise:
            assert html.escape(hinweis) in _ansicht(details)
    _, steuer = _details(_repo(_komponente("K-1", "HMZ", 1_100), _komponente("K-2", "BK", 500, 7_000)))
    assert marke in _druck(steuer) and "7000" in _druck(steuer)
    freigabe = SimpleNamespace(
        aktive_freigabe_fuer_monat=lambda k_id, monatsanfang, monatsende: SimpleNamespace(bestaetigter_netto_betrag_cent=1)
    )
    _, abweichung = _details(_repo(_komponente("K-1", "HMZ", 1_100)), freigabe=freigabe)
    assert "weicht von der rechnerischen Summe" in _druck(abweichung)


def test_druck_layout_und_ci_fuss():
    _, details = _details(_repo(_komponente("K-1", "HMZ", 1_100)))
    druck = _druck(details)
    assert "min-width: 404mm" in druck and "minmax(min(420px, 100%), 1fr)" in druck and "max-width: 639.98px" in druck
    assert "box-sizing: border-box" in druck and "overflow-wrap: anywhere" not in druck and "zins-teilmenge" in druck
    assert "Marc-Aurel-Straße 4/16, 1010 Wien" in druck and "FN 631126b" in druck and "UID ATU81269707" in druck and "+43 1 4351011" in druck
