from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from mietinkasso.domain.enums import IndexAnpassungStatus
from mietinkasso.domain.exceptions import (
    CrossTenantError,
    IndexKlauselFehltError,
    ObjektAusgeschlossenError,
    RechtsordnungUngeklaertError,
    RechtsprofilNichtImplementiertError,
)
from mietinkasso.domain.money import round_index_half_cent_down
from mietinkasso.index.repository import IndexRepository
from mietinkasso.index.service import EINFACHER_SCHWELLENVERGLEICH, IndexService


@pytest.fixture
def index_service(session_factory, stammdaten_repo) -> IndexService:
    return IndexService(IndexRepository(session_factory), stammdaten_repo)


def test_halber_cent_wird_laut_par1_abs2_z3_abgerundet():
    # 10.005 ist exakt der halbe Cent -> abwärts auf 10.00, NICHT 10.01
    assert round_index_half_cent_down(Decimal("10.005")) == Decimal("10.00")
    # Alles andere rundet normal kaufmännisch
    assert round_index_half_cent_down(Decimal("10.006")) == Decimal("10.01")
    assert round_index_half_cent_down(Decimal("10.004")) == Decimal("10.00")


def test_fehlende_klausel_blockiert_erhoehung(index_service, basis_vertrag, ctx_factory):
    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    with pytest.raises(IndexKlauselFehltError):
        index_service.berechne_vorschlag(
            ctx=ctx, vertrag_id=vertrag.id, stichtag=date(2026, 4, 1), neuer_wert=Decimal("110.0"), quelle_referenz="VPI"
        )


def test_klausel_anlegen_sperrt_bei_ungeklaerter_rechtsordnung(index_service, stammdaten_repo, basis_vertrag, ctx_factory):
    vertrag, _ = basis_vertrag
    stammdaten_repo.upsert_vertrag(
        id=vertrag.id, einheit_id=vertrag.einheit_id, debitor_id=vertrag.debitor_id,
        gesellschaft_id=vertrag.gesellschaft_id, rechtsordnung="UNGEKLAERT", gueltig_von=vertrag.gueltig_von,
    )
    ctx = ctx_factory("7DI")
    with pytest.raises(RechtsordnungUngeklaertError):
        index_service.klausel_anlegen(
            ctx=ctx, vertrag_id=vertrag.id, rechtsordnung="OESTERREICH_MRG_VOLL",
            berechnungsprofil=EINFACHER_SCHWELLENVERGLEICH, abschlussdatum=date(2024, 1, 1),
            basis_reihe="VPI2020", basis_wert=Decimal("100.0"), basis_monat="2024-01",
        )


def test_berechne_vorschlag_sperrt_wenn_rechtsordnung_nachtraeglich_ungeklaert_wird(index_service, stammdaten_repo, basis_vertrag, ctx_factory):
    """Eine bereits freigegebene Klausel darf keine neue Berechnung mehr
    auslösen, wenn der Vertrag NACHTRÄGLICH (z. B. wegen eines
    aufkommenden Rechtsstreits) auf UNGEKLAERT reklassifiziert wird."""

    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    klausel = index_service.klausel_anlegen(
        ctx=ctx, vertrag_id=vertrag.id, rechtsordnung="OESTERREICH_MRG_VOLL",
        berechnungsprofil=EINFACHER_SCHWELLENVERGLEICH, abschlussdatum=date(2024, 1, 1),
        basis_reihe="VPI2020", basis_wert=Decimal("100.0"), basis_monat="2024-01",
    )
    index_service.klausel_freigeben(klausel.id, ctx=ctx, freigegeben_von="markus")

    stammdaten_repo.upsert_vertrag(
        id=vertrag.id, einheit_id=vertrag.einheit_id, debitor_id=vertrag.debitor_id,
        gesellschaft_id=vertrag.gesellschaft_id, rechtsordnung="UNGEKLAERT", gueltig_von=vertrag.gueltig_von,
    )
    with pytest.raises(RechtsordnungUngeklaertError):
        index_service.berechne_vorschlag(
            ctx=ctx, vertrag_id=vertrag.id, stichtag=date(2026, 4, 1), neuer_wert=Decimal("110.0"), quelle_referenz="VPI",
        )


def test_bk_vorauszahlung_wird_nicht_indexiert(index_service, stammdaten_repo, basis_vertrag, ctx_factory):
    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    stammdaten_repo.add_komponente(
        id="K-HMZ", vertrag_id=vertrag.id, art="HMZ", bezeichnung="Hauptmietzins", betrag_cent=50_000,
        indexierbar=True, gueltig_von=date(2024, 1, 1),
    )
    # BK-Vorauszahlung wird versehentlich auch als indexierbar markiert -> muss trotzdem ausgeschlossen bleiben
    stammdaten_repo.add_komponente(
        id="K-BKVZ", vertrag_id=vertrag.id, art="BK_VORAUSZAHLUNG", bezeichnung="BK-Vorauszahlung",
        betrag_cent=12_000, indexierbar=True, gueltig_von=date(2024, 1, 1),
    )
    klausel = index_service.klausel_anlegen(
        ctx=ctx, vertrag_id=vertrag.id, rechtsordnung="OESTERREICH_MRG_VOLL",
        berechnungsprofil=EINFACHER_SCHWELLENVERGLEICH, abschlussdatum=date(2024, 1, 1),
        basis_reihe="VPI2020", basis_wert=Decimal("100.0"), basis_monat="2024-01",
    )
    assert index_service._repository.freigegebene_klausel(vertrag.id) is None

    index_service.klausel_freigeben(klausel.id, ctx=ctx, freigegeben_von="markus")

    vorschlag = index_service.berechne_vorschlag(
        ctx=ctx, vertrag_id=vertrag.id, stichtag=date(2026, 4, 1), neuer_wert=Decimal("110.0"), quelle_referenz="VPI"
    )
    # 10% auf 500,00 EUR HMZ = 50,00 EUR; BK 120,00 EUR bleibt unangetastet
    assert vorschlag.erhoehung_cent == 5_000


def test_aenderung_nach_freigabe_invalidiert_offenen_vorschlag(index_service, stammdaten_repo, basis_vertrag, ctx_factory):
    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    stammdaten_repo.add_komponente(
        id="K-HMZ", vertrag_id=vertrag.id, art="HMZ", bezeichnung="Hauptmietzins", betrag_cent=50_000,
        indexierbar=True, gueltig_von=date(2024, 1, 1),
    )
    klausel_v1 = index_service.klausel_anlegen(
        ctx=ctx, vertrag_id=vertrag.id, rechtsordnung="OESTERREICH_MRG_VOLL",
        berechnungsprofil=EINFACHER_SCHWELLENVERGLEICH, abschlussdatum=date(2024, 1, 1),
        basis_reihe="VPI2020", basis_wert=Decimal("100.0"), basis_monat="2024-01",
    )
    index_service.klausel_freigeben(klausel_v1.id, ctx=ctx, freigegeben_von="markus")
    vorschlag = index_service.berechne_vorschlag(
        ctx=ctx, vertrag_id=vertrag.id, stichtag=date(2026, 4, 1), neuer_wert=Decimal("110.0"), quelle_referenz="VPI"
    )
    assert vorschlag.status == IndexAnpassungStatus.VORSCHLAG.value

    # Neue Version der Klausel ersetzt die freigegebene -> offener Vorschlag wird ungültig
    index_service.klausel_anlegen(
        ctx=ctx, vertrag_id=vertrag.id, rechtsordnung="OESTERREICH_MRG_VOLL",
        berechnungsprofil=EINFACHER_SCHWELLENVERGLEICH, abschlussdatum=date(2024, 1, 1),
        basis_reihe="VPI2020", basis_wert=Decimal("102.0"), basis_monat="2025-01",
    )
    invalidiert = index_service._repository.get_anpassung(vorschlag.id)
    assert invalidiert.status == IndexAnpassungStatus.INVALIDIERT.value

    # Ein bereits INVALIDIERTER Vorschlag darf nicht mehr freigegeben werden
    with pytest.raises(ValueError):
        index_service.anpassung_freigeben(vorschlag.id, ctx=ctx)


def test_schwelle_wirkt_auf_betrag_der_veraenderung_auch_bei_senkung(index_service, stammdaten_repo, basis_vertrag, ctx_factory):
    """Regression (Codex-Fund #7): Basis100, neu95 (-5%), Schwelle3,
    HMZ 500 EUR -> erwartet -25 EUR, nicht 0. Die Schwelle darf keine
    Senkungen unterdrücken, nur Veränderungen UNTER dem Schwellenbetrag."""

    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    stammdaten_repo.add_komponente(
        id="K-HMZ", vertrag_id=vertrag.id, art="HMZ", bezeichnung="Hauptmietzins", betrag_cent=50_000,
        indexierbar=True, gueltig_von=date(2024, 1, 1),
    )
    klausel = index_service.klausel_anlegen(
        ctx=ctx, vertrag_id=vertrag.id, rechtsordnung="OESTERREICH_MRG_VOLL",
        berechnungsprofil=EINFACHER_SCHWELLENVERGLEICH, abschlussdatum=date(2024, 1, 1),
        basis_reihe="VPI2020", basis_wert=Decimal("100.0"), basis_monat="2024-01",
        schwelle_prozent=Decimal("3"),
    )
    index_service.klausel_freigeben(klausel.id, ctx=ctx, freigegeben_von="markus")

    vorschlag = index_service.berechne_vorschlag(
        ctx=ctx, vertrag_id=vertrag.id, stichtag=date(2026, 4, 1), neuer_wert=Decimal("95.0"), quelle_referenz="VPI"
    )
    assert vorschlag.veraenderung_prozent == Decimal("-5")
    assert vorschlag.erhoehung_cent == -25_00


def test_schwelle_grenzfall_ist_inklusive(index_service, stammdaten_repo, basis_vertrag, ctx_factory):
    """Exakt die Schwelle (hier 3% bei 3% Veränderung) löst bereits aus."""

    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    stammdaten_repo.add_komponente(
        id="K-HMZ", vertrag_id=vertrag.id, art="HMZ", bezeichnung="Hauptmietzins", betrag_cent=100_000,
        indexierbar=True, gueltig_von=date(2024, 1, 1),
    )
    klausel = index_service.klausel_anlegen(
        ctx=ctx, vertrag_id=vertrag.id, rechtsordnung="OESTERREICH_MRG_VOLL",
        berechnungsprofil=EINFACHER_SCHWELLENVERGLEICH, abschlussdatum=date(2024, 1, 1),
        basis_reihe="VPI2020", basis_wert=Decimal("100.0"), basis_monat="2024-01",
        schwelle_prozent=Decimal("3"),
    )
    index_service.klausel_freigeben(klausel.id, ctx=ctx, freigegeben_von="markus")

    genau_an_der_schwelle = index_service.berechne_vorschlag(
        ctx=ctx, vertrag_id=vertrag.id, stichtag=date(2026, 4, 1), neuer_wert=Decimal("103.0"), quelle_referenz="VPI"
    )
    assert genau_an_der_schwelle.erhoehung_cent == 3_000  # 3% von 1000,00 EUR

    knapp_darunter = index_service.berechne_vorschlag(
        ctx=ctx, vertrag_id=vertrag.id, stichtag=date(2026, 4, 1), neuer_wert=Decimal("102.99"), quelle_referenz="VPI"
    )
    assert knapp_darunter.erhoehung_cent == 0


def test_schwelle_exklusiv_loest_erst_ueber_der_grenze_aus(index_service, stammdaten_repo, basis_vertrag, ctx_factory):
    """Codex-Rückprüfung: viele reale Verträge verlangen strikt "über X%"
    (exklusiv), nicht "ab X%" (inklusiv). `schwelle_inklusive=False` muss
    exakt an der Grenze NICHT auslösen, knapp darüber schon."""

    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    stammdaten_repo.add_komponente(
        id="K-HMZ", vertrag_id=vertrag.id, art="HMZ", bezeichnung="Hauptmietzins", betrag_cent=100_000,
        indexierbar=True, gueltig_von=date(2024, 1, 1),
    )
    klausel = index_service.klausel_anlegen(
        ctx=ctx, vertrag_id=vertrag.id, rechtsordnung="OESTERREICH_MRG_VOLL",
        berechnungsprofil=EINFACHER_SCHWELLENVERGLEICH, abschlussdatum=date(2024, 1, 1),
        basis_reihe="VPI2020", basis_wert=Decimal("100.0"), basis_monat="2024-01",
        schwelle_prozent=Decimal("3"), schwelle_inklusive=False,
    )
    assert klausel.schwelle_inklusive is False
    index_service.klausel_freigeben(klausel.id, ctx=ctx, freigegeben_von="markus")

    genau_an_der_schwelle = index_service.berechne_vorschlag(
        ctx=ctx, vertrag_id=vertrag.id, stichtag=date(2026, 4, 1), neuer_wert=Decimal("103.0"), quelle_referenz="VPI"
    )
    assert genau_an_der_schwelle.erhoehung_cent == 0  # exakt 3% löst bei EXKLUSIVER Schwelle noch nicht aus

    knapp_darueber = index_service.berechne_vorschlag(
        ctx=ctx, vertrag_id=vertrag.id, stichtag=date(2026, 4, 1), neuer_wert=Decimal("103.01"), quelle_referenz="VPI"
    )
    assert knapp_darueber.erhoehung_cent > 0


def test_index_sperrt_objekt_107(index_service, stammdaten_repo, ctx_factory):
    """Regression (Codex-Rückprüfung): der Objekt-107-Ausschluss muss auch
    im Index-Service greifen, nicht nur bei der OP-Buchung."""

    stammdaten_repo.upsert_gesellschaft(id="7DI", name="7D Immobilien GmbH")
    stammdaten_repo.upsert_objekt(id="107", gesellschaft_id="7DI", bezeichnung="Sieben Dörfer", ausgeschlossen=True)
    stammdaten_repo.upsert_einheit(id="107-TOP1", objekt_id="107", bezeichnung="Top 1", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten_repo.upsert_debitor(id="DEB-107", name="Mieterin 107")
    stammdaten_repo.upsert_vertrag(
        id="V-107-1", einheit_id="107-TOP1", debitor_id="DEB-107", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    ctx = ctx_factory("7DI")

    with pytest.raises(ObjektAusgeschlossenError):
        index_service.klausel_anlegen(
            ctx=ctx, vertrag_id="V-107-1", rechtsordnung="OESTERREICH_MRG_VOLL",
            berechnungsprofil=EINFACHER_SCHWELLENVERGLEICH, abschlussdatum=date(2024, 1, 1),
            basis_reihe="VPI2020", basis_wert=Decimal("100.0"), basis_monat="2024-01",
        )
    with pytest.raises(ObjektAusgeschlossenError):
        index_service.berechne_vorschlag(
            ctx=ctx, vertrag_id="V-107-1", stichtag=date(2026, 4, 1), neuer_wert=Decimal("110.0"),
            quelle_referenz="VPI",
        )


def test_gesetzliche_daempfung_ist_schwelle_plus_haelfte_des_ueberschusses(index_service, stammdaten_repo, basis_vertrag, ctx_factory):
    """Regression (Codex-Fund #7): Dämpfung ist kein harter Deckel bei
    daempfung_prozent, sondern "Schwelle plus Hälfte des darüberliegenden
    Anstiegs". Basis100, neu110 (+10%), Dämpfung3 -> effektiv 3 + (10-3)/2
    = 6,5%."""

    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    stammdaten_repo.add_komponente(
        id="K-HMZ", vertrag_id=vertrag.id, art="HMZ", bezeichnung="Hauptmietzins", betrag_cent=100_000,
        indexierbar=True, gueltig_von=date(2024, 1, 1),
    )
    klausel = index_service.klausel_anlegen(
        ctx=ctx, vertrag_id=vertrag.id, rechtsordnung="OESTERREICH_MRG_VOLL",
        berechnungsprofil=EINFACHER_SCHWELLENVERGLEICH, abschlussdatum=date(2024, 1, 1),
        basis_reihe="VPI2020", basis_wert=Decimal("100.0"), basis_monat="2024-01",
        daempfung_prozent=Decimal("3"),
    )
    index_service.klausel_freigeben(klausel.id, ctx=ctx, freigegeben_von="markus")

    vorschlag = index_service.berechne_vorschlag(
        ctx=ctx, vertrag_id=vertrag.id, stichtag=date(2026, 4, 1), neuer_wert=Decimal("110.0"), quelle_referenz="VPI"
    )
    assert vorschlag.veraenderung_prozent == Decimal("6.5")
    assert vorschlag.erhoehung_cent == 6_500  # 6,5% von 1000,00 EUR


def test_index_ist_fremder_gesellschaft_nicht_zugaenglich(index_service, basis_vertrag, ctx_factory):
    """Regression (Abnahmesperre): Index-Service hatte zuvor KEINE
    Auth-Prüfung - jede Gesellschaft konnte für jeden fremden Vertrag eine
    IndexKlausel anlegen oder eine Anpassung berechnen."""

    vertrag, _ = basis_vertrag
    ctx_fremd = ctx_factory("ANDERE-GESELLSCHAFT")
    with pytest.raises(CrossTenantError):
        index_service.klausel_anlegen(
            ctx=ctx_fremd, vertrag_id=vertrag.id, rechtsordnung="OESTERREICH_MRG_VOLL",
            berechnungsprofil=EINFACHER_SCHWELLENVERGLEICH, abschlussdatum=date(2024, 1, 1),
            basis_reihe="VPI2020", basis_wert=Decimal("100.0"), basis_monat="2024-01",
        )
    with pytest.raises(CrossTenantError):
        index_service.berechne_vorschlag(
            ctx=ctx_fremd, vertrag_id=vertrag.id, stichtag=date(2026, 4, 1), neuer_wert=Decimal("110.0"),
            quelle_referenz="VPI",
        )


def test_nicht_implementiertes_rechtsprofil_wird_gesperrt(index_service, stammdaten_repo, basis_vertrag, ctx_factory):
    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    stammdaten_repo.add_komponente(
        id="K-HMZ", vertrag_id=vertrag.id, art="HMZ", bezeichnung="Hauptmietzins", betrag_cent=50_000,
        indexierbar=True, gueltig_von=date(2024, 1, 1),
    )
    klausel = index_service.klausel_anlegen(
        ctx=ctx, vertrag_id=vertrag.id, rechtsordnung="OESTERREICH_MRG_VOLL",
        berechnungsprofil="MIEWEG_2026_VOLLPROFIL_MIT_JAHRESDURCHSCHNITT",  # (noch) nicht implementiert
        abschlussdatum=date(2024, 1, 1), basis_reihe="VPI2020", basis_wert=Decimal("100.0"), basis_monat="2024-01",
    )
    index_service.klausel_freigeben(klausel.id, ctx=ctx, freigegeben_von="markus")

    with pytest.raises(RechtsprofilNichtImplementiertError):
        index_service.berechne_vorschlag(
            ctx=ctx, vertrag_id=vertrag.id, stichtag=date(2026, 4, 1), neuer_wert=Decimal("110.0"), quelle_referenz="VPI"
        )
