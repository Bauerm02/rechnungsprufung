"""Regressionstests für die drei automatischen Online-Listen (Auftrag
HV-20260930-PORTAL-LISTEN) - ausschließlich synthetische Daten."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from mietinkasso.bank.repository import BankRepository
from mietinkasso.bank.service import BankImportService
from mietinkasso.domain.enums import OPTyp
from mietinkasso.infrastructure.db.base import Base
from mietinkasso.mahnwesen.repository import MahnFallRepository
from mietinkasso.portallisten.service import (
    berechne_mieterliste,
    berechne_saldenliste,
    berechne_zinsliste,
)
from mietinkasso.rueckstaende.service import UnbekanntesObjektFilterError
from mietinkasso.variableabrechnung.bootstrap import bauen as variableabrechnung_bauen

_HEUTE = date(2026, 9, 30)


@pytest.fixture
def mahn_fall_repo(session_factory) -> MahnFallRepository:
    return MahnFallRepository(session_factory)


@pytest.fixture
def bank_repo(session_factory) -> BankRepository:
    return BankRepository(session_factory)


@pytest.fixture
def bank_service(bank_repo, stammdaten_repo, op_service) -> BankImportService:
    return BankImportService(bank_repo, stammdaten_repo, op_service)


@pytest.fixture
def variable(session_factory, stammdaten_repo):
    return variableabrechnung_bauen(session_factory, stammdaten_repo)


def _vertrag(stammdaten_repo, *, vertrag_id, einheit_id, debitor_id, gesellschaft_id="7DI", von=date(2024, 1, 1), bis=None):
    stammdaten_repo.upsert_vertrag(
        id=vertrag_id, einheit_id=einheit_id, debitor_id=debitor_id, gesellschaft_id=gesellschaft_id,
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=von, gueltig_bis=bis,
    )
    return stammdaten_repo.get_vertrag(vertrag_id)


def _komponente(stammdaten_repo, *, id, vertrag_id, art, betrag_cent, von=date(2024, 1, 1), bis=None, vorgaenger=None):
    stammdaten_repo.add_komponente(
        id=id, vertrag_id=vertrag_id, art=art, bezeichnung=f"{art} synthetisch", betrag_cent=betrag_cent,
        gueltig_von=von, gueltig_bis=bis, historisiert_von_id=vorgaenger,
    )


def _soll(op_service, ctx, konto, betrag_cent, *, faelligkeit, beleg, belegdatum=date(2026, 9, 1)):
    op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=betrag_cent, belegdatum=belegdatum,
        buchungsdatum=belegdatum, faelligkeit=faelligkeit, beleg_referenz=beleg,
    )


def _zahlung(op_service, ctx, konto, betrag_cent, *, beleg, belegdatum=date(2026, 9, 2)):
    op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.ZAHLUNG, betrag_cent=betrag_cent, belegdatum=belegdatum,
        buchungsdatum=belegdatum, faelligkeit=None, beleg_referenz=beleg,
    )


@pytest.fixture
def bestand(stammdaten_repo, op_service, admin_ctx, variable):
    """Erlaubtes Objekt 601 (mit Adresse) und 602 (ohne Adresse) der
    Gesellschaft 7DI, ein nachträglich ausgeschlossenes Objekt 603 und
    ein Objekt 900 einer fremden Gesellschaft.

    601: fälliger Rückstand (TOP1), echtes Guthaben (TOP2, ohne
    Komponenten/Kontaktdaten), künftige + unbekannte Fälligkeit (TOP3, mit
    historisierter Komponente), am 15.09.2026 beendeter Vertrag ohne
    Mietkonto bei weiterhin "vermieteter" Einheit (TOP4), ausgeglichenes
    Konto (TOP5), Vertrag mit FREMDER gesellschaft_id auf erlaubtem Objekt
    (TOP9), echter Leerstand, Kurzzeitvermietung mit bestätigtem
    Monatsbericht, Selfstorage, Eigennutzung."""

    repo = stammdaten_repo
    repo.upsert_gesellschaft(id="7DI", name="7D Immobilien GmbH (synthetisch)")
    repo.upsert_gesellschaft(id="ANDERE", name="Andere GmbH (synthetisch)")
    repo.upsert_objekt(id="601", gesellschaft_id="7DI", bezeichnung="Am Corso", adresse="Am Corso 1, 1010 Wien")
    repo.upsert_objekt(id="602", gesellschaft_id="7DI", bezeichnung="Fockygasse")
    repo.upsert_objekt(id="603", gesellschaft_id="7DI", bezeichnung="Sieben Dörfer")
    repo.upsert_objekt(id="900", gesellschaft_id="ANDERE", bezeichnung="Fremdobjekt")

    for einheit_id, bezeichnung in (
        ("601-TOP1", "Top 1"), ("601-TOP2", "Top 2"), ("601-TOP3", "Top 3"), ("601-TOP4", "Top 4"),
        ("601-TOP5", "Top 5"), ("601-TOP9", "Top 9"),
    ):
        repo.upsert_einheit(id=einheit_id, objekt_id="601", bezeichnung=bezeichnung, nutzungsstatus="DAUERVERMIETUNG")
    repo.upsert_einheit(id="601-LEER", objekt_id="601", bezeichnung="Top L", nutzungsstatus="LEERSTAND")
    repo.upsert_einheit(id="601-KURZ", objekt_id="601", bezeichnung="Top K", nutzungsstatus="KURZZEITVERMIETUNG")
    repo.upsert_einheit(id="601-SELF", objekt_id="601", bezeichnung="Top S", nutzungsstatus="SELFSTORAGE")
    repo.upsert_einheit(id="601-EIGEN", objekt_id="601", bezeichnung="Top E", nutzungsstatus="EIGENNUTZUNG")
    repo.upsert_einheit(id="602-TOP1", objekt_id="602", bezeichnung="Top 1", nutzungsstatus="DAUERVERMIETUNG")
    repo.upsert_einheit(id="603-TOP1", objekt_id="603", bezeichnung="Top 1", nutzungsstatus="DAUERVERMIETUNG")
    repo.upsert_einheit(id="900-TOP1", objekt_id="900", bezeichnung="Top 1", nutzungsstatus="DAUERVERMIETUNG")
    repo.upsert_einheit(id="900-TOP2", objekt_id="900", bezeichnung="Top 2", nutzungsstatus="DAUERVERMIETUNG")

    repo.upsert_debitor(
        id="DEB-1", name="Mieter Eins", email="eins@example.at", adresse="Postfach 1, 1010 Wien", telefon="+43 660 000 00 01",
    )
    repo.upsert_debitor(id="DEB-2", name="Mieter Zwei")
    repo.upsert_debitor(id="DEB-3", name="Mieter Drei", email="drei@example.at")
    repo.upsert_debitor(id="DEB-4", name="Mieter Vier")
    repo.upsert_debitor(id="DEB-5", name="Mieter Fünf", email="fuenf@example.at")
    repo.upsert_debitor(id="DEB-6", name="Mieter Sechs")
    repo.upsert_debitor(id="DEB-7", name="Fremdvertrag Mieter")
    repo.upsert_debitor(id="DEB-8", name="Fremdmieter", email="fremd@example.at")
    repo.upsert_debitor(id="DEB-9", name="Mieter Ausgeschlossen", email="ausgeschlossen@example.at")

    # TOP1: Miete + Küche + Stellplatz + BK; SOLL 700 fällig, Zahlung 200 -> 500 fällig offen.
    v1 = _vertrag(repo, vertrag_id="V-601-1", einheit_id="601-TOP1", debitor_id="DEB-1")
    _komponente(repo, id="K-1-HMZ", vertrag_id="V-601-1", art="HMZ", betrag_cent=50_000)
    _komponente(repo, id="K-1-KUE", vertrag_id="V-601-1", art="KUECHE", betrag_cent=5_000)
    _komponente(repo, id="K-1-PARK", vertrag_id="V-601-1", art="PARKPLATZ", betrag_cent=3_000)
    _komponente(repo, id="K-1-BK", vertrag_id="V-601-1", art="BK_VORAUSZAHLUNG", betrag_cent=12_000)
    k1 = repo.get_or_create_konto(vertrag=v1)
    _soll(op_service, admin_ctx, k1, 70_000, faelligkeit=date(2026, 8, 5), beleg="Miete August", belegdatum=date(2026, 8, 1))
    _zahlung(op_service, admin_ctx, k1, 20_000, beleg="Teilzahlung", belegdatum=date(2026, 8, 10))

    # TOP2: KEINE Komponenten, KEINE Kontaktdaten; Zahlung > Soll -> echtes Guthaben 200.
    v2 = _vertrag(repo, vertrag_id="V-601-2", einheit_id="601-TOP2", debitor_id="DEB-2")
    k2 = repo.get_or_create_konto(vertrag=v2)
    _soll(op_service, admin_ctx, k2, 30_000, faelligkeit=date(2026, 9, 5), beleg="Miete September")
    _zahlung(op_service, admin_ctx, k2, 50_000, beleg="Überzahlung")

    # TOP3: historisierte Miete (350 bis 31.08., 400 ab 01.09.); 100 ohne
    # Fälligkeit + 400 erst am 05.10. fällig.
    v3 = _vertrag(repo, vertrag_id="V-601-3", einheit_id="601-TOP3", debitor_id="DEB-3")
    _komponente(repo, id="K-3-ALT", vertrag_id="V-601-3", art="HMZ", betrag_cent=35_000, bis=date(2026, 8, 31))
    _komponente(
        repo, id="K-3-NEU", vertrag_id="V-601-3", art="HMZ", betrag_cent=40_000, von=date(2026, 9, 1), vorgaenger="K-3-ALT",
    )
    k3 = repo.get_or_create_konto(vertrag=v3)
    _soll(op_service, admin_ctx, k3, 10_000, faelligkeit=None, beleg="Nachzahlung ohne Fälligkeit")
    _soll(op_service, admin_ctx, k3, 40_000, faelligkeit=date(2026, 10, 5), beleg="Miete Oktober (voraus)")

    # TOP4: Vertrag endet am 15.09.2026, Einheit bleibt "vermietet", KEIN Mietkonto.
    _vertrag(repo, vertrag_id="V-601-4", einheit_id="601-TOP4", debitor_id="DEB-4", bis=date(2026, 9, 15))
    _komponente(repo, id="K-4-HMZ", vertrag_id="V-601-4", art="HMZ", betrag_cent=60_000)

    # TOP5: ausgeglichen (Soll 100, Zahlung 100).
    v5 = _vertrag(repo, vertrag_id="V-601-5", einheit_id="601-TOP5", debitor_id="DEB-5")
    _komponente(repo, id="K-5-HMZ", vertrag_id="V-601-5", art="HMZ", betrag_cent=10_000)
    k5 = repo.get_or_create_konto(vertrag=v5)
    _soll(op_service, admin_ctx, k5, 10_000, faelligkeit=date(2026, 9, 5), beleg="Miete September")
    _zahlung(op_service, admin_ctx, k5, 10_000, beleg="Zahlung September")

    # TOP9: inkonsistente Stammdaten - Vertrag mit FREMDER gesellschaft_id auf erlaubtem Objekt.
    _vertrag(repo, vertrag_id="V-601-9", einheit_id="601-TOP9", debitor_id="DEB-7", gesellschaft_id="ANDERE")
    _komponente(repo, id="K-9-HMZ", vertrag_id="V-601-9", art="HMZ", betrag_cent=123_456)

    # 602: nur ein längst beendeter Vertrag.
    _vertrag(
        repo, vertrag_id="V-602-1", einheit_id="602-TOP1", debitor_id="DEB-6", von=date(2020, 1, 1), bis=date(2025, 12, 31),
    )
    _komponente(repo, id="K-602-HMZ", vertrag_id="V-602-1", art="HMZ", betrag_cent=45_000, von=date(2020, 1, 1))

    # Kurzzeitvermietung: bestätigter variabler Monatsbericht - darf NIE als Mietzins zählen.
    variable.service.erfassen(
        ctx=admin_ctx, einheit_id="601-KURZ", art="KURZZEITVERMIETUNG", leistungsmonat="2026-09",
        belegdatum=date(2026, 9, 30), quelle_referenz="Synthetischer Betreiberreport", status="BESTAETIGT",
        unser_netto_anteil_cent=99_900, erstellt_von="test",
    )

    # 603: wird NACH der Buchung ausgeschlossen.
    v9 = _vertrag(repo, vertrag_id="V-603-1", einheit_id="603-TOP1", debitor_id="DEB-9")
    _komponente(repo, id="K-603-HMZ", vertrag_id="V-603-1", art="HMZ", betrag_cent=999_999)
    k9 = repo.get_or_create_konto(vertrag=v9)
    _soll(op_service, admin_ctx, k9, 999_999, faelligkeit=date(2026, 8, 1), beleg="Darf nie zählen", belegdatum=date(2026, 8, 1))
    repo.upsert_objekt(id="603", gesellschaft_id="7DI", bezeichnung="Sieben Dörfer", ausgeschlossen=True)

    # 900: fremde Gesellschaft - eigener Mieter UND ein zweiter Vertrag des 7DI-Mieters DEB-1.
    v8 = _vertrag(repo, vertrag_id="V-900-1", einheit_id="900-TOP1", debitor_id="DEB-8", gesellschaft_id="ANDERE")
    _komponente(repo, id="K-900-HMZ", vertrag_id="V-900-1", art="HMZ", betrag_cent=777_777)
    k8 = repo.get_or_create_konto(vertrag=v8)
    _soll(op_service, admin_ctx, k8, 777_777, faelligkeit=date(2026, 8, 1), beleg="Fremd, darf nie zählen", belegdatum=date(2026, 8, 1))
    _vertrag(repo, vertrag_id="V-900-2", einheit_id="900-TOP2", debitor_id="DEB-1", gesellschaft_id="ANDERE")


def _db_abbild(session_factory) -> dict:
    with session_factory() as session:
        return {
            tabelle.name: [tuple(zeile) for zeile in session.execute(select(tabelle)).all()]
            for tabelle in Base.metadata.sorted_tables
        }


def _mieterliste(ctx, repo, **kwargs):
    kwargs.setdefault("objekt_id", None)
    return berechne_mieterliste(ctx=ctx, stammdaten_repository=repo, heute=_HEUTE, **kwargs)


def _zinsliste(ctx, repo, variable, monat="2026-09", **kwargs):
    kwargs.setdefault("objekt_id", None)
    return berechne_zinsliste(
        ctx=ctx, monat=monat, stammdaten_repository=repo, variable_service=variable.service,
        komponenten_freigabe_service=variable.komponenten_freigabe_service, **kwargs,
    )


def _saldenliste(ctx, repo, op_service, mahn_fall_repo, bank_repo, bank_service, **kwargs):
    kwargs.setdefault("objekt_id", None)
    return berechne_saldenliste(
        ctx=ctx, stammdaten_repository=repo, op_service=op_service, mahn_fall_repository=mahn_fall_repo,
        bank_repository=bank_repo, bank_service=bank_service, heute=_HEUTE, **kwargs,
    )


def _zeile(liste, **kriterien):
    treffer = [z for z in liste.zeilen if all(getattr(z, feld) == wert for feld, wert in kriterien.items())]
    assert len(treffer) == 1, f"erwartet genau eine Zeile für {kriterien}, gefunden {len(treffer)}"
    return treffer[0]


# -- Scope: fremde Gesellschaft, ausgeschlossenes/unbekanntes Objekt ----------


@pytest.mark.parametrize("objekt_id", ["900", "603", "GIBT-ES-NICHT"])
def test_fremdes_ausgeschlossenes_und_unbekanntes_objekt_wird_in_allen_listen_einheitlich_abgelehnt(
    objekt_id, bestand, ctx_factory, stammdaten_repo, op_service, mahn_fall_repo, bank_repo, bank_service, variable
):
    ctx = ctx_factory("7DI")
    erwartet = f"Objekt {objekt_id} ist unbekannt, gehört zu keiner zugänglichen Gesellschaft, oder ist ausgeschlossen."
    with pytest.raises(UnbekanntesObjektFilterError) as mieter_fehler:
        _mieterliste(ctx, stammdaten_repo, objekt_id=objekt_id)
    with pytest.raises(UnbekanntesObjektFilterError) as zins_fehler:
        _zinsliste(ctx, stammdaten_repo, variable, objekt_id=objekt_id)
    with pytest.raises(UnbekanntesObjektFilterError) as salden_fehler:
        _saldenliste(ctx, stammdaten_repo, op_service, mahn_fall_repo, bank_repo, bank_service, objekt_id=objekt_id)
    # Wortgleich zur bestehenden Rückstandsübersicht - kein Hinweis, welcher der drei Fälle vorliegt.
    assert str(mieter_fehler.value) == str(zins_fehler.value) == str(salden_fehler.value) == erwartet


def test_ausgeschlossenes_objekt_bleibt_auch_fuer_admin_gesperrt(
    bestand, admin_ctx, stammdaten_repo, op_service, mahn_fall_repo, bank_repo, bank_service, variable
):
    for aufruf in (
        lambda: _mieterliste(admin_ctx, stammdaten_repo, objekt_id="603"),
        lambda: _zinsliste(admin_ctx, stammdaten_repo, variable, objekt_id="603"),
        lambda: _saldenliste(admin_ctx, stammdaten_repo, op_service, mahn_fall_repo, bank_repo, bank_service, objekt_id="603"),
    ):
        with pytest.raises(UnbekanntesObjektFilterError):
            aufruf()
    assert "603" not in {o.id for o in _mieterliste(admin_ctx, stammdaten_repo).objekt_optionen}
    assert "Mieter Ausgeschlossen" not in {z.debitor_name for z in _mieterliste(admin_ctx, stammdaten_repo, status="alle").zeilen}


def test_alle_objekte_enthaelt_weder_fremde_noch_ausgeschlossene_daten(
    bestand, ctx_factory, stammdaten_repo, op_service, mahn_fall_repo, bank_repo, bank_service, variable
):
    ctx = ctx_factory("7DI")
    mieter = _mieterliste(ctx, stammdaten_repo, status="alle")
    zins = _zinsliste(ctx, stammdaten_repo, variable)
    salden = _saldenliste(ctx, stammdaten_repo, op_service, mahn_fall_repo, bank_repo, bank_service)

    for liste in (mieter, zins, salden):
        assert {o.id for o in liste.objekt_optionen} == {"601", "602"}
        assert {z.objekt_id for z in liste.zeilen} <= {"601", "602"}
    # Vertrag mit FREMDER gesellschaft_id auf erlaubtem Objekt (V-601-9) fehlt überall.
    assert "V-601-9" not in {z.vertrag_id for z in mieter.zeilen}
    assert "V-601-9" not in {z.vertrag_id for z in salden.zeilen}
    assert "601-TOP9" not in {z.einheit_id for z in zins.zeilen}
    assert zins.summen.brutto_gesamt_cent == 180_000  # ohne 999.999 (603), 777.777 (900), 123.456 (V-601-9)
    assert salden.summen.offen_cent == 100_000


# -- Mieterliste --------------------------------------------------------------


def test_mieterliste_standard_zeigt_nur_aktive_und_markiert_ehemalige_nur_auf_wunsch(bestand, ctx_factory, stammdaten_repo):
    ctx = ctx_factory("7DI")
    aktiv = _mieterliste(ctx, stammdaten_repo)
    assert aktiv.status_filter == "aktiv"
    assert [z.debitor_name for z in aktiv.zeilen] == ["Mieter Drei", "Mieter Eins", "Mieter Fünf", "Mieter Zwei"]
    assert {z.status for z in aktiv.zeilen} == {"AKTIV"}
    assert (aktiv.anzahl_aktiv, aktiv.anzahl_beendet) == (4, 2)

    beendet = _mieterliste(ctx, stammdaten_repo, status="beendet")
    assert {(z.debitor_name, z.status) for z in beendet.zeilen} == {("Mieter Vier", "BEENDET"), ("Mieter Sechs", "BEENDET")}

    alle = _mieterliste(ctx, stammdaten_repo, status="alle")
    assert len(alle.zeilen) == 6
    assert _zeile(alle, vertrag_id="V-601-4").status == "BEENDET"  # endete am 15.09., Stichtag 30.09.

    with pytest.raises(ValueError):
        _mieterliste(ctx, stammdaten_repo, status="irgendwas")


def test_mieterliste_vertragsende_am_stichtag_ist_noch_aktiv_und_kuenftiger_vertrag_markiert(bestand, ctx_factory, stammdaten_repo):
    ctx = ctx_factory("7DI")
    am_endtag = berechne_mieterliste(ctx=ctx, objekt_id=None, stammdaten_repository=stammdaten_repo, heute=date(2026, 9, 15))
    assert _zeile(am_endtag, vertrag_id="V-601-4").status == "AKTIV"

    stammdaten_repo.upsert_debitor(id="DEB-NEU", name="Mieter Künftig")
    _vertrag(stammdaten_repo, vertrag_id="V-602-NEU", einheit_id="602-TOP1", debitor_id="DEB-NEU", von=date(2026, 11, 1))
    aktiv = _mieterliste(ctx, stammdaten_repo)
    assert _zeile(aktiv, vertrag_id="V-602-NEU").status == "KUENFTIG"
    # Ein künftiger Mieter zählt NICHT als aktiv - eigene Zahl, eigener Status.
    assert (aktiv.anzahl_aktiv, aktiv.anzahl_kuenftig) == (4, 1)
    assert "V-602-NEU" not in {z.vertrag_id for z in aktiv.zeilen if z.status == "AKTIV"}
    # Am ersten Vertragstag ist er aktiv, am Tag davor noch nicht.
    am_vortag = berechne_mieterliste(ctx=ctx, objekt_id=None, stammdaten_repository=stammdaten_repo, heute=date(2026, 10, 31))
    am_beginn = berechne_mieterliste(ctx=ctx, objekt_id=None, stammdaten_repository=stammdaten_repo, heute=date(2026, 11, 1))
    assert _zeile(am_vortag, vertrag_id="V-602-NEU").status == "KUENFTIG"
    assert _zeile(am_beginn, vertrag_id="V-602-NEU").status == "AKTIV"


def test_mieterliste_kontaktdaten_fehlend_bleibt_none_und_adressen_bleiben_getrennt(bestand, ctx_factory, stammdaten_repo):
    liste = _mieterliste(ctx_factory("7DI"), stammdaten_repo, status="alle")

    eins = _zeile(liste, vertrag_id="V-601-1")
    assert eins.email == "eins@example.at"
    assert eins.objekt_adresse == "Am Corso 1, 1010 Wien"  # Mietobjekt
    assert eins.korrespondenzadresse == "Postfach 1, 1010 Wien"  # Postadresse des Mieters
    assert (eins.objekt_bezeichnung, eins.einheit_bezeichnung) == ("Am Corso", "Top 1")
    assert eins.telefon == "+43 660 000 00 01"

    zwei = _zeile(liste, vertrag_id="V-601-2")
    assert (zwei.email, zwei.korrespondenzadresse, zwei.telefon) == (None, None, None)  # fehlend - nie erfunden
    # Die Objektadresse wird NICHT als Korrespondenzadresse unterstellt.
    assert zwei.objekt_adresse == "Am Corso 1, 1010 Wien"

    assert _zeile(liste, vertrag_id="V-602-1").objekt_adresse is None

    stammdaten_repo.upsert_debitor(id="DEB-2", name="Mieter Zwei", email="   ", adresse="")
    leer = _zeile(_mieterliste(ctx_factory("7DI"), stammdaten_repo), vertrag_id="V-601-2")
    assert (leer.email, leer.korrespondenzadresse) == (None, None)  # Leerstring ist keine Angabe

    # Ein Upsert OHNE Telefon (wie jeder Altimport) lässt die gespeicherte Nummer in der Liste stehen.
    stammdaten_repo.upsert_debitor(id="DEB-1", name="Mieter Eins", email="eins-neu@example.at", adresse="Postfach 1, 1010 Wien")
    nachher = _zeile(_mieterliste(ctx_factory("7DI"), stammdaten_repo), vertrag_id="V-601-1")
    assert (nachher.email, nachher.telefon) == ("eins-neu@example.at", "+43 660 000 00 01")
    assert [z.vertrag_id for z in _mieterliste(ctx_factory("7DI"), stammdaten_repo, suche="660 000 00 01").zeilen] == ["V-601-1"]


def test_mieterliste_suche_objektfilter_und_debitor_scoping(bestand, ctx_factory, admin_ctx, stammdaten_repo):
    ctx = ctx_factory("7DI")
    assert [z.vertrag_id for z in _mieterliste(ctx, stammdaten_repo, suche="zWEI").zeilen] == ["V-601-2"]
    assert [z.vertrag_id for z in _mieterliste(ctx, stammdaten_repo, suche="drei@example").zeilen] == ["V-601-3"]
    assert _mieterliste(ctx, stammdaten_repo, objekt_id="602").zeilen == ()
    assert [z.vertrag_id for z in _mieterliste(ctx, stammdaten_repo, objekt_id="602", status="alle").zeilen] == ["V-602-1"]

    # Fremde/ausgeschlossene Debitoren sind auch über Suche und Status "alle" nicht auffindbar.
    for suchbegriff in ("Fremdmieter", "fremd@example", "Ausgeschlossen", "Fremdvertrag", "V-900", "V-603"):
        assert _mieterliste(ctx, stammdaten_repo, status="alle", suche=suchbegriff).zeilen == ()

    # DEB-1 hat zusätzlich einen Vertrag in der fremden Gesellschaft - sichtbar bleibt NUR der eigene.
    eigene = [z for z in _mieterliste(ctx, stammdaten_repo, status="alle").zeilen if z.debitor_id == "DEB-1"]
    assert [(z.vertrag_id, z.objekt_id) for z in eigene] == [("V-601-1", "601")]
    # ADMIN sieht laut Fachregel alle Gesellschaften - aber nie das ausgeschlossene Objekt.
    admin = [z.vertrag_id for z in _mieterliste(admin_ctx, stammdaten_repo, status="alle").zeilen if z.debitor_id == "DEB-1"]
    assert admin == ["V-601-1", "V-900-2"]


# -- Zinsliste ----------------------------------------------------------------


def test_zinsliste_gliedert_gespeicherte_komponenten_und_summiert_nur_echte_betraege(bestand, ctx_factory, stammdaten_repo, variable):
    liste = _zinsliste(ctx_factory("7DI"), stammdaten_repo, variable)

    eins = _zeile(liste, einheit_id="601-TOP1")
    assert (eins.miete_cent, eins.kueche_cent, eins.stellplatz_cent, eins.nebenkosten_cent) == (50_000, 5_000, 3_000, 12_000)
    assert eins.brutto_gesamt_cent == 70_000
    assert (eins.vertrag_id, eins.debitor_name) == ("V-601-1", "Mieter Eins")
    assert eins.netto_miete_geprueft_cent is None  # keine geprüfte Netto-Freigabe -> Aufteilung unbekannt
    assert not eins.pruefbedarf

    drei = _zeile(liste, einheit_id="601-TOP3")
    assert (drei.miete_cent, drei.kueche_cent, drei.stellplatz_cent, drei.nebenkosten_cent) == (40_000, None, None, None)

    s = liste.summen
    assert (s.miete_cent, s.kueche_cent, s.stellplatz_cent, s.nebenkosten_cent) == (160_000, 5_000, 3_000, 12_000)
    assert s.brutto_gesamt_cent == 180_000
    assert (s.anzahl_zeilen, s.anzahl_mit_betrag, s.anzahl_ohne_betrag) == (10, 4, 6)
    assert s.brutto_gesamt_cent == sum(z.brutto_gesamt_cent for z in liste.zeilen if z.brutto_gesamt_cent is not None)


def test_zinsliste_fehlende_komponenten_sind_unbekannt_und_nie_null(bestand, ctx_factory, stammdaten_repo, variable):
    liste = _zinsliste(ctx_factory("7DI"), stammdaten_repo, variable)
    zwei = _zeile(liste, einheit_id="601-TOP2")
    assert (zwei.vertrag_id, zwei.debitor_name) == ("V-601-2", "Mieter Zwei")
    assert zwei.brutto_gesamt_cent is None and zwei.miete_cent is None and zwei.komponenten == ()
    assert zwei.pruefbedarf
    assert any("Betrag unbekannt (nicht 0)" in text for text in zwei.hinweise)

    # Eine ausdrücklich mit 0 gespeicherte Komponente ist dagegen ein echter, summierbarer Betrag.
    _komponente(stammdaten_repo, id="K-2-HMZ-NULL", vertrag_id="V-601-2", art="HMZ", betrag_cent=0)
    danach = _zinsliste(ctx_factory("7DI"), stammdaten_repo, variable)
    assert _zeile(danach, einheit_id="601-TOP2").brutto_gesamt_cent == 0
    assert (danach.summen.anzahl_mit_betrag, danach.summen.brutto_gesamt_cent) == (5, 180_000)


def test_zinsliste_leerstand_kurzzeit_selfstorage_eigennutzung_kommen_aus_dem_nutzungsstatus(
    bestand, ctx_factory, stammdaten_repo, variable
):
    liste = _zinsliste(ctx_factory("7DI"), stammdaten_repo, variable)

    leer = _zeile(liste, einheit_id="601-LEER")
    assert (leer.nutzungsstatus, leer.vertrag_id, leer.debitor_name, leer.brutto_gesamt_cent) == ("LEERSTAND", None, None, None)
    assert not leer.pruefbedarf and not leer.variabel

    kurz = _zeile(liste, einheit_id="601-KURZ")
    assert (kurz.nutzungsstatus, kurz.variabel, kurz.variabler_bericht_status) == ("KURZZEITVERMIETUNG", True, "BESTAETIGT")
    assert kurz.vertrag_id is None and kurz.brutto_gesamt_cent is None  # 999,00 aus dem Bericht NICHT als Mietzins

    selfstorage = _zeile(liste, einheit_id="601-SELF")
    assert (selfstorage.nutzungsstatus, selfstorage.variabel, selfstorage.variabler_bericht_status) == ("SELFSTORAGE", True, None)
    assert selfstorage.brutto_gesamt_cent is None

    eigen = _zeile(liste, einheit_id="601-EIGEN")
    assert (eigen.nutzungsstatus, eigen.brutto_gesamt_cent, eigen.pruefbedarf) == ("EIGENNUTZUNG", None, False)

    # Kein variabler Erlös in irgendeiner Summe, und kein Leerstand aus einem fehlenden Betrag:
    assert liste.summen.brutto_gesamt_cent == 180_000
    assert _zeile(liste, einheit_id="601-TOP2").nutzungsstatus == "DAUERVERMIETUNG"
    assert [z.einheit_id for z in liste.zeilen if z.nutzungsstatus == "LEERSTAND"] == ["601-LEER"]


def test_zinsliste_fester_vertrag_auf_selfstorage_zaehlt_einmal_variabler_bericht_wird_nicht_addiert(
    bestand, ctx_factory, admin_ctx, stammdaten_repo, variable
):
    stammdaten_repo.upsert_debitor(id="DEB-BETREIBER", name="Lager Betreiber")
    _vertrag(stammdaten_repo, vertrag_id="V-601-SELF", einheit_id="601-SELF", debitor_id="DEB-BETREIBER")
    _komponente(stammdaten_repo, id="K-SELF-HMZ", vertrag_id="V-601-SELF", art="HMZ", betrag_cent=20_000)
    ohne_bericht = _zeile(_zinsliste(ctx_factory("7DI"), stammdaten_repo, variable), einheit_id="601-SELF")
    assert (ohne_bericht.brutto_gesamt_cent, ohne_bericht.variabel, ohne_bericht.pruefbedarf) == (20_000, True, False)

    variable.service.erfassen(
        ctx=admin_ctx, einheit_id="601-SELF", art="SELFSTORAGE", leistungsmonat="2026-09", belegdatum=date(2026, 9, 30),
        quelle_referenz="Synthetischer Lagerreport", status="BESTAETIGT", unser_netto_anteil_cent=55_500, erstellt_von="test",
    )
    liste = _zinsliste(ctx_factory("7DI"), stammdaten_repo, variable)
    zeile = _zeile(liste, einheit_id="601-SELF")
    assert zeile.brutto_gesamt_cent == 20_000  # nur der feste Bestandteil
    assert zeile.pruefbedarf and any("NICHT addiert" in text for text in zeile.hinweise)
    assert liste.summen.brutto_gesamt_cent == 200_000  # 180.000 + 20.000, ohne 55.500/99.900


def test_zinsliste_vertragsende_im_monat_und_im_folgemonat(bestand, ctx_factory, stammdaten_repo, variable):
    ctx = ctx_factory("7DI")

    # Monat, der das Vertragsende (15.09.) enthält: Vertrag gilt, voller Betrag, Status prüfen.
    september = _zeile(_zinsliste(ctx, stammdaten_repo, variable, "2026-09"), einheit_id="601-TOP4")
    assert (september.vertrag_id, september.debitor_name, september.brutto_gesamt_cent) == ("V-601-4", "Mieter Vier", 60_000)
    assert september.vertrag_bis == date(2026, 9, 15)
    assert september.pruefbedarf
    assert any("endet am 2026-09-15" in text for text in september.hinweise)
    assert any("kein automatischer Leerstand" in text for text in september.hinweise)

    # Folgemonat: kein Vertrag, kein Mieter, kein Betrag - aber NICHT automatisch Leerstand.
    oktober_liste = _zinsliste(ctx, stammdaten_repo, variable, "2026-10")
    oktober = _zeile(oktober_liste, einheit_id="601-TOP4")
    assert (oktober.vertrag_id, oktober.debitor_name, oktober.brutto_gesamt_cent) == (None, None, None)
    assert oktober.nutzungsstatus == "DAUERVERMIETUNG" and oktober.pruefbedarf
    assert (oktober.letzter_vertrag_id, oktober.letzter_vertrag_bis) == ("V-601-4", date(2026, 9, 15))
    assert any("beweist keinen Leerstand" in text for text in oktober.hinweise)
    assert oktober_liste.summen.brutto_gesamt_cent == 120_000  # 180.000 - 60.000

    # Wird der Leerstand ausdrücklich gepflegt, verschwindet der Prüfhinweis.
    stammdaten_repo.set_nutzungsstatus(einheit_id="601-TOP4", nutzungsstatus="LEERSTAND")
    gepflegt = _zeile(_zinsliste(ctx, stammdaten_repo, variable, "2026-10"), einheit_id="601-TOP4")
    assert (gepflegt.nutzungsstatus, gepflegt.pruefbedarf) == ("LEERSTAND", False)

    # Längst beendeter Vertrag (602): ehemaliger Mieter ist nie aktueller Mieter.
    alt = _zeile(_zinsliste(ctx, stammdaten_repo, variable, "2026-09"), einheit_id="602-TOP1")
    assert (alt.vertrag_id, alt.debitor_name, alt.brutto_gesamt_cent) == (None, None, None)
    assert (alt.letzter_vertrag_id, alt.letzter_vertrag_debitor_name) == ("V-602-1", "Mieter Sechs")
    dezember_2025 = _zeile(_zinsliste(ctx, stammdaten_repo, variable, "2025-12"), einheit_id="602-TOP1")
    assert (dezember_2025.vertrag_id, dezember_2025.brutto_gesamt_cent) == ("V-602-1", 45_000)


def test_zinsliste_komponentengueltigkeit_gilt_fuer_den_gewaehlten_monat(bestand, ctx_factory, stammdaten_repo, variable):
    ctx = ctx_factory("7DI")
    assert _zeile(_zinsliste(ctx, stammdaten_repo, variable, "2026-08"), einheit_id="601-TOP3").miete_cent == 35_000
    assert _zeile(_zinsliste(ctx, stammdaten_repo, variable, "2026-09"), einheit_id="601-TOP3").miete_cent == 40_000

    # Komponente beginnt mitten im Monat: im Startmonat nicht gezählt (Hinweis), ab Folgemonat gezählt.
    _komponente(stammdaten_repo, id="K-5-KUE", vertrag_id="V-601-5", art="KUECHE", betrag_cent=2_000, von=date(2026, 9, 15))
    september = _zeile(_zinsliste(ctx, stammdaten_repo, variable, "2026-09"), einheit_id="601-TOP5")
    assert (september.kueche_cent, september.brutto_gesamt_cent) == (None, 10_000)
    assert september.pruefbedarf and any("gilt erst ab 2026-09-15" in text for text in september.hinweise)
    assert september.aenderung_im_monat  # 100,00 ist nur der Stand am Monatsersten, kein geklärter Monatsbetrag
    oktober = _zeile(_zinsliste(ctx, stammdaten_repo, variable, "2026-10"), einheit_id="601-TOP5")
    assert (oktober.kueche_cent, oktober.brutto_gesamt_cent) == (2_000, 12_000)
    assert not oktober.aenderung_im_monat


def test_zinsliste_aenderung_im_monat_wird_markiert_und_getrennt_summiert_ohne_aliquotierung(
    bestand, ctx_factory, stammdaten_repo, variable
):
    ctx = ctx_factory("7DI")

    # Ausgangslage September: nur TOP4 (Vertragsende 15.09.) hat eine Änderung im Monat.
    liste = _zinsliste(ctx, stammdaten_repo, variable, "2026-09")
    assert [z.einheit_id for z in liste.zeilen if z.aenderung_im_monat] == ["601-TOP4"]
    assert _zeile(liste, einheit_id="601-TOP4").brutto_gesamt_cent == 60_000  # voller Stand, nichts anteilig gerechnet
    assert (liste.summen.anzahl_aenderung_im_monat, liste.summen.brutto_aenderung_im_monat_cent) == (1, 60_000)
    assert liste.summen.brutto_gesamt_cent == 180_000  # Summe unverändert, Teilmenge nur zusätzlich ausgewiesen
    assert not _zeile(liste, einheit_id="601-TOP1").aenderung_im_monat
    assert not _zeile(liste, einheit_id="601-TOP3").aenderung_im_monat  # Wechsel exakt zum Monatsersten ist keine Änderung IM Monat

    # Eine Komponente, die mitten im Monat ENDET, bleibt mit vollem Betrag stehen - aber markiert.
    _komponente(
        stammdaten_repo, id="K-1-GARAGE", vertrag_id="V-601-1", art="STELLPLATZ", betrag_cent=4_000, bis=date(2026, 9, 20),
    )
    liste = _zinsliste(ctx, stammdaten_repo, variable, "2026-09")
    eins = _zeile(liste, einheit_id="601-TOP1")
    assert (eins.stellplatz_cent, eins.brutto_gesamt_cent) == (7_000, 74_000)
    assert eins.aenderung_im_monat and eins.pruefbedarf
    assert any("endet am 2026-09-20" in text and "keine Aliquotierung" in text for text in eins.hinweise)
    assert (liste.summen.anzahl_aenderung_im_monat, liste.summen.brutto_aenderung_im_monat_cent) == (2, 134_000)

    # Im Folgemonat ist die beendete Komponente weg und nichts mehr markiert.
    oktober = _zeile(_zinsliste(ctx, stammdaten_repo, variable, "2026-10"), einheit_id="601-TOP1")
    assert (oktober.brutto_gesamt_cent, oktober.aenderung_im_monat) == (70_000, False)

    # Vertragsbeginn im Monat: gar kein Betrag - also auch kein "Stand Monatserster".
    stammdaten_repo.upsert_einheit(id="602-TOP2", objekt_id="602", bezeichnung="Top 2", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten_repo.upsert_debitor(id="DEB-NEU", name="Mieter Neu")
    _vertrag(stammdaten_repo, vertrag_id="V-602-2", einheit_id="602-TOP2", debitor_id="DEB-NEU", von=date(2026, 9, 10))
    _komponente(stammdaten_repo, id="K-602-2-HMZ", vertrag_id="V-602-2", art="HMZ", betrag_cent=80_000, von=date(2026, 9, 10))
    beginn = _zeile(_zinsliste(ctx, stammdaten_repo, variable, "2026-09"), einheit_id="602-TOP2")
    assert (beginn.brutto_gesamt_cent, beginn.aenderung_im_monat, beginn.pruefbedarf) == (None, False, True)


def test_zinsliste_vertragsbeginn_im_monat_erfindet_keinen_anteiligen_betrag(bestand, ctx_factory, stammdaten_repo, variable):
    ctx = ctx_factory("7DI")
    stammdaten_repo.upsert_einheit(id="602-TOP2", objekt_id="602", bezeichnung="Top 2", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten_repo.upsert_debitor(id="DEB-NEU", name="Mieter Neu")
    _vertrag(stammdaten_repo, vertrag_id="V-602-2", einheit_id="602-TOP2", debitor_id="DEB-NEU", von=date(2026, 10, 10))
    _komponente(stammdaten_repo, id="K-602-2-HMZ", vertrag_id="V-602-2", art="HMZ", betrag_cent=80_000, von=date(2026, 10, 10))

    september = _zeile(_zinsliste(ctx, stammdaten_repo, variable, "2026-09"), einheit_id="602-TOP2")
    assert (september.vertrag_id, september.brutto_gesamt_cent) == (None, None)
    oktober = _zeile(_zinsliste(ctx, stammdaten_repo, variable, "2026-10"), einheit_id="602-TOP2")
    assert (oktober.vertrag_id, oktober.brutto_gesamt_cent) == ("V-602-2", None)
    assert any("beginnt am 2026-10-10" in text for text in oktober.hinweise)
    november = _zeile(_zinsliste(ctx, stammdaten_repo, variable, "2026-11"), einheit_id="602-TOP2")
    assert november.brutto_gesamt_cent == 80_000


def test_zinsliste_netto_nur_bei_gepruefter_freigabe_aller_mietkomponenten(bestand, ctx_factory, admin_ctx, stammdaten_repo, variable):
    ctx = ctx_factory("7DI")
    variable.komponenten_freigabe_service.freigeben(
        ctx=admin_ctx, komponente_id="K-5-HMZ", bestaetigter_netto_betrag_cent=9_091,
        quellenbeleg_referenz="Synthetischer Beleg", gueltig_von=date(2024, 1, 1), gueltig_bis=None, freigegeben_von="test",
    )
    variable.komponenten_freigabe_service.freigeben(
        ctx=admin_ctx, komponente_id="K-1-HMZ", bestaetigter_netto_betrag_cent=45_455,
        quellenbeleg_referenz="Synthetischer Beleg", gueltig_von=date(2024, 1, 1), gueltig_bis=None, freigegeben_von="test",
    )
    liste = _zinsliste(ctx, stammdaten_repo, variable)
    assert _zeile(liste, einheit_id="601-TOP5").netto_miete_geprueft_cent == 9_091
    # TOP1: nur EINE von drei Mietkomponenten freigegeben -> keine Teil-Netto-Summe.
    assert _zeile(liste, einheit_id="601-TOP1").netto_miete_geprueft_cent is None
    assert _zeile(liste, einheit_id="601-TOP1").brutto_gesamt_cent == 70_000  # Bruttobetrag unverändert


def test_zinsliste_suche_objektfilter_summen_aus_gefilterten_zeilen_und_monatsformat(bestand, ctx_factory, stammdaten_repo, variable):
    ctx = ctx_factory("7DI")
    gesucht = _zinsliste(ctx, stammdaten_repo, variable, suche="mieter eins")
    assert [z.einheit_id for z in gesucht.zeilen] == ["601-TOP1"]
    assert (gesucht.summen.brutto_gesamt_cent, gesucht.summen.anzahl_zeilen) == (70_000, 1)

    nur_602 = _zinsliste(ctx, stammdaten_repo, variable, objekt_id="602")
    assert [z.einheit_id for z in nur_602.zeilen] == ["602-TOP1"]
    assert (nur_602.summen.brutto_gesamt_cent, nur_602.summen.anzahl_ohne_betrag) == (0, 1)

    assert _zinsliste(ctx, stammdaten_repo, variable, suche="Fremdvertrag").zeilen == ()
    for ungueltig in ("2026-13", "2026-9", "09/2026", "", "2026-09-01"):
        with pytest.raises(ValueError):
            _zinsliste(ctx, stammdaten_repo, variable, ungueltig)


# -- Saldenliste --------------------------------------------------------------


def test_salden_trennt_offen_faellig_kuenftig_unbekannt_und_guthaben(
    bestand, ctx_factory, stammdaten_repo, op_service, mahn_fall_repo, bank_repo, bank_service
):
    liste = _saldenliste(ctx_factory("7DI"), stammdaten_repo, op_service, mahn_fall_repo, bank_repo, bank_service)

    eins = _zeile(liste, vertrag_id="V-601-1")
    assert (eins.status, eins.offen_cent, eins.faellig_cent, eins.guthaben_cent) == ("OFFEN", 50_000, 50_000, 0)
    assert (eins.nicht_faellig_cent, eins.faelligkeit_unbekannt_cent) == (0, 0)

    zwei = _zeile(liste, vertrag_id="V-601-2")  # echtes Guthaben, positiv dargestellt
    assert (zwei.status, zwei.saldo_cent, zwei.offen_cent, zwei.guthaben_cent, zwei.faellig_cent) == ("GUTHABEN", -20_000, 0, 20_000, 0)

    drei = _zeile(liste, vertrag_id="V-601-3")  # nichts fällig: 400 künftig, 100 ohne Fälligkeit
    assert (drei.status, drei.offen_cent, drei.faellig_cent) == ("OFFEN", 50_000, 0)
    assert (drei.nicht_faellig_cent, drei.faelligkeit_unbekannt_cent, drei.guthaben_cent) == (40_000, 10_000, 0)

    fuenf = _zeile(liste, vertrag_id="V-601-5")
    assert (fuenf.status, fuenf.offen_cent, fuenf.guthaben_cent, fuenf.faellig_cent) == ("AUSGEGLICHEN", 0, 0, 0)

    # Identisch zur bestehenden OP-Rechnung - kein zweites Ledger.
    for zeile in liste.zeilen:
        if zeile.konto_id is not None:
            assert zeile.saldo_cent == op_service.berechne_saldo(zeile.konto_id, stichtag=_HEUTE).saldo_cent


def test_salden_guthaben_wird_nie_gegen_fremden_rueckstand_verrechnet(
    bestand, ctx_factory, stammdaten_repo, op_service, mahn_fall_repo, bank_repo, bank_service
):
    liste = _saldenliste(ctx_factory("7DI"), stammdaten_repo, op_service, mahn_fall_repo, bank_repo, bank_service)
    s = liste.summen
    assert (s.offen_cent, s.guthaben_cent) == (100_000, 20_000)  # NICHT 80.000 netto
    assert (s.faellig_cent, s.nicht_faellig_cent, s.faelligkeit_unbekannt_cent) == (50_000, 40_000, 10_000)
    assert s.offen_cent == sum(z.offen_cent or 0 for z in liste.zeilen)
    assert s.guthaben_cent == sum(z.guthaben_cent or 0 for z in liste.zeilen)


def test_salden_ohne_mietkonto_ist_unbekannt_und_nicht_ausgeglichen(
    bestand, ctx_factory, stammdaten_repo, op_service, mahn_fall_repo, bank_repo, bank_service
):
    argumente = (ctx_factory("7DI"), stammdaten_repo, op_service, mahn_fall_repo, bank_repo, bank_service)
    liste = _saldenliste(*argumente)
    vier = _zeile(liste, vertrag_id="V-601-4")
    assert (vier.status, vier.konto_id) == ("KEIN_KONTO", None)
    assert (vier.saldo_cent, vier.offen_cent, vier.faellig_cent, vier.guthaben_cent) == (None, None, None, None)
    assert vier.historisch
    assert liste.summen.anzahl_ohne_konto == 2  # V-601-4 und V-602-1

    assert [z.vertrag_id for z in _saldenliste(*argumente, status="ausgeglichen").zeilen] == ["V-601-5"]
    assert [z.vertrag_id for z in _saldenliste(*argumente, status="guthaben").zeilen] == ["V-601-2"]
    offen = _saldenliste(*argumente, status="offen")
    assert [z.vertrag_id for z in offen.zeilen] == ["V-601-1", "V-601-3"]
    assert (offen.summen.offen_cent, offen.summen.guthaben_cent) == (100_000, 0)
    assert _saldenliste(*argumente, objekt_id="602").summen.anzahl_ohne_konto == 1
    with pytest.raises(ValueError):
        _saldenliste(*argumente, status="bezahlt")


def test_salden_mahnsperre_bleibt_sichtbar_und_es_entsteht_kein_mahnfall(
    bestand, ctx_factory, stammdaten_repo, op_service, mahn_fall_repo, bank_repo, bank_service
):
    stammdaten_repo.sperre_setzen(vertrag_id="V-601-1", grund="RATENPLAN")
    stammdaten_repo.sperre_setzen(vertrag_id="V-601-2", grund="STREIT")  # Sperre auf Guthabenkonto bleibt ebenfalls sichtbar
    liste = _saldenliste(ctx_factory("7DI"), stammdaten_repo, op_service, mahn_fall_repo, bank_repo, bank_service)
    assert _zeile(liste, vertrag_id="V-601-1").sperrgruende == ("RATENPLAN",)
    assert _zeile(liste, vertrag_id="V-601-2").sperrgruende == ("STREIT",)
    assert _zeile(liste, vertrag_id="V-601-1").offen_cent == 50_000  # Sperre ändert keinen Betrag
    for zeile in liste.zeilen:
        assert mahn_fall_repo.list_fuer_vertrag(zeile.vertrag_id) == []
        assert zeile.mahnfaelle_anzahl == 0


def test_salden_bankstand_kommt_aus_bankdaten_nicht_aus_dem_anzeigezeitpunkt(
    bestand, ctx_factory, admin_ctx, stammdaten_repo, op_service, mahn_fall_repo, bank_repo, bank_service
):
    argumente = (ctx_factory("7DI"), stammdaten_repo, op_service, mahn_fall_repo, bank_repo, bank_service)

    ohne_bank = _saldenliste(*argumente).bankstaende
    assert [(b.gesellschaft_id, b.bank_konto_id, b.bestaetigt_bis, b.letzte_buchung) for b in ohne_bank] == [("7DI", None, None, None)]

    bank_repo.upsert_bank_konto(id="BK-7DI", gesellschaft_id="7DI", iban="AT00SYNTHETISCH", bezeichnung="Synthetisches Mietenkonto")
    bank_repo.upsert_bank_konto(id="BK-FREMD", gesellschaft_id="ANDERE", iban="AT00FREMD", bezeichnung="Fremdkonto")
    nie_bestaetigt = _saldenliste(*argumente).bankstaende
    assert [(b.bank_konto_id, b.bestaetigt_bis, b.letzte_buchung) for b in nie_bestaetigt] == [("BK-7DI", None, None)]

    bank_service.bestaetige_bankvollstaendigkeit(bank_konto_id="BK-7DI", bestaetigt_bis=date(2026, 8, 31), bestaetigt_von="test")
    bank_service.bestaetige_bankvollstaendigkeit(bank_konto_id="BK-FREMD", bestaetigt_bis=date(2026, 9, 29), bestaetigt_von="test")
    liste = _saldenliste(*argumente)
    assert [(b.bank_konto_id, b.bestaetigt_bis) for b in liste.bankstaende] == [("BK-7DI", date(2026, 8, 31))]
    assert liste.stichtag == _HEUTE and liste.bankstaende[0].bestaetigt_bis != liste.stichtag
    assert {z.gesellschaft_id for z in liste.zeilen} == {"7DI"}

    # ADMIN sieht beide Gesellschaften; mit Objektfilter nur die Bank der betroffenen Gesellschaft.
    admin_argumente = (admin_ctx, stammdaten_repo, op_service, mahn_fall_repo, bank_repo, bank_service)
    assert {b.bank_konto_id for b in _saldenliste(*admin_argumente).bankstaende} == {"BK-7DI", "BK-FREMD"}
    assert {b.bank_konto_id for b in _saldenliste(*admin_argumente, objekt_id="900").bankstaende} == {"BK-FREMD"}


# -- Lesepfad: deterministisch, ohne jede DB-Änderung -------------------------


def test_alle_listen_sind_deterministisch_und_veraendern_die_datenbank_nicht(
    bestand, ctx_factory, admin_ctx, session_factory, stammdaten_repo, op_service, mahn_fall_repo, bank_repo, bank_service, variable
):
    bank_repo.upsert_bank_konto(id="BK-7DI", gesellschaft_id="7DI", iban="AT00SYNTHETISCH", bezeichnung="Synthetisches Mietenkonto")
    stammdaten_repo.sperre_setzen(vertrag_id="V-601-1", grund="MANUELL")
    vorher = _db_abbild(session_factory)
    assert vorher["op_positionen"] and vorher["vertraege"]  # das Abbild erfasst tatsächlich Daten

    def _alle(ctx):
        return (
            _mieterliste(ctx, stammdaten_repo),
            _mieterliste(ctx, stammdaten_repo, status="alle", suche="mieter", objekt_id="601"),
            _zinsliste(ctx, stammdaten_repo, variable, "2026-09"),
            _zinsliste(ctx, stammdaten_repo, variable, "2026-10", objekt_id="601", suche="top"),
            _saldenliste(ctx, stammdaten_repo, op_service, mahn_fall_repo, bank_repo, bank_service),
            _saldenliste(ctx, stammdaten_repo, op_service, mahn_fall_repo, bank_repo, bank_service, objekt_id="601", status="offen"),
        )

    for ctx in (ctx_factory("7DI"), admin_ctx):
        assert _alle(ctx) == _alle(ctx)
    with pytest.raises(UnbekanntesObjektFilterError):
        _saldenliste(ctx_factory("7DI"), stammdaten_repo, op_service, mahn_fall_repo, bank_repo, bank_service, objekt_id="900")

    assert _db_abbild(session_factory) == vorher
    assert vorher["vorschreibungen"] == [] and vorher["mahn_faelle"] == []
