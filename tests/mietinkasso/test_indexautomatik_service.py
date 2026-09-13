from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest

from mietinkasso.index.repository import IndexRepository
from mietinkasso.index.service import IndexService
from mietinkasso.indexautomatik.outbox_service import ErhoehungsschreibenOutboxService
from mietinkasso.indexautomatik.rechtsprofil import RechtsprofilService
from mietinkasso.indexautomatik.repository import (
    ErhoehungsschreibenRepository,
    IndexautomatikLaufRepository,
    RechtsprofilRepository,
    VpiRepository,
)
from mietinkasso.indexautomatik.service import IndexautomatikService, _monate_addieren, _naechster_gueltiger_kalendermonat
from mietinkasso.infrastructure.db.tables import IndexKlauselTable
from mietinkasso.mieweg_vorschau.repository import MieWegVorschauRepository
from mietinkasso.mieweg_vorschau.service import MieWegVorschauService


@dataclass
class Bundle:
    rechtsprofil_service: RechtsprofilService
    index_service: IndexautomatikService
    outbox_repo: ErhoehungsschreibenRepository
    lauf_repo: IndexautomatikLaufRepository
    vpi_repo: VpiRepository
    index_repo: IndexRepository
    rechtsprofil_repo: RechtsprofilRepository
    outbox_service: ErhoehungsschreibenOutboxService


@pytest.fixture
def bundle(session_factory, stammdaten_repo) -> Bundle:
    rechtsprofil_repo = RechtsprofilRepository(session_factory)
    index_repo = IndexRepository(session_factory)
    lauf_repo = IndexautomatikLaufRepository(session_factory)
    outbox_repo = ErhoehungsschreibenRepository(session_factory)
    vpi_repo = VpiRepository(session_factory)
    mieweg_repo = MieWegVorschauRepository(session_factory)

    rechtsprofil_service = RechtsprofilService(rechtsprofil_repo, stammdaten_repo, index_repo)
    mieweg_service = MieWegVorschauService(mieweg_repo, stammdaten_repo)
    index_service = IndexService(index_repo, stammdaten_repo)
    outbox_service = ErhoehungsschreibenOutboxService(
        outbox_repo, stammdaten_repo, rechtsprofil_repo, rechtsprofil_service, jlb_signatur="JLB Projects GmbH"
    )

    automatik = IndexautomatikService(
        stammdaten_repository=stammdaten_repo,
        rechtsprofil_service=rechtsprofil_service,
        lauf_repository=lauf_repo,
        outbox_repository=outbox_repo,
        vpi_repository=vpi_repo,
        mieweg_service=mieweg_service,
        index_repository=index_repo,
        index_service=index_service,
        outbox_service=outbox_service,
    )
    return Bundle(rechtsprofil_service, automatik, outbox_repo, lauf_repo, vpi_repo, index_repo, rechtsprofil_repo, outbox_service)


def _seed_vpi(vpi_repo, *, reihe="VPI20C18", jahre_werte: dict[int, str]):
    for jahr, wert in jahre_werte.items():
        vpi_repo.jahreswert_erfassen(
            reihe=reihe, jahr=jahr, wert=Decimal(wert), quelle="Statistik Austria (synthetisch)",
            quelle_datum=date(jahr + 1, 2, 17), erfasst_von="markus",
        )


def _mit_komponente(stammdaten_repo, vertrag, *, id="K-1", betrag_cent=100_000):
    stammdaten_repo.add_komponente(
        id=id, vertrag_id=vertrag.id, art="HMZ", bezeichnung="Hauptmietzins", betrag_cent=betrag_cent,
        indexierbar=True, gueltig_von=date(2024, 1, 1),
    )


def _freigegebenes_wohnungsprofil(admin_ctx, rechtsprofil_service, vertrag, **overrides) -> object:
    basis = dict(
        ctx=admin_ctx, vertrag_id=vertrag.id, rechtsordnung="OESTERREICH_MRG_VOLL", ist_wohnungsnutzung=True,
        mrg_zinsbeschraenkung=False, mrg_zinsbeschraenkung_geprueft=True, ist_altvertrag=False, ist_hauptmiete=True,
        foerderbindung=False, foerderbindung_geprueft=True,
        mietzinsobergrenze_cent=None, mietzinsobergrenze_quellenbeleg=None, mietzinsobergrenze_gueltig_bis=None,
        bezugsjahr=2024, bezugsmonat=1, letzte_basis_war_jahresdurchschnitt=False, basis_komponenten_ids=["K-1"],
        vertraglich_zulaessiger_betrag_cent=200_000, vertraglicher_quellenbeleg="Mietvertrag Punkt 5",
        vertraglicher_fruehestmoeglicher_termin=date(2026, 4, 1), vertrag_beleg_referenz="Mietvertrag V-601-3",
        klausel_referenz="Punkt 5 Wertsicherung", erstellt_von="markus",
    )
    basis.update(overrides)
    profil = rechtsprofil_service.entwurf_anlegen(**basis)
    return rechtsprofil_service.freigeben(profil.id, ctx=admin_ctx, freigegeben_von="markus")


def test_januarvertrag_ausserhalb_januar_wird_korrekt_geprueft(admin_ctx, basis_vertrag, bundle, stammdaten_repo):
    """"Januarvertrag außerhalb Januar": der Monatslauf darf zu JEDEM
    Kalendermonat laufen (nicht nur im April/Januar) - nur das
    ERGEBNIS hängt vom April-Termin ab, nicht der Ausführungsmonat."""

    vertrag, _konto = basis_vertrag
    _mit_komponente(stammdaten_repo, vertrag)
    _freigegebenes_wohnungsprofil(admin_ctx, bundle.rechtsprofil_service, vertrag)
    _seed_vpi(bundle.vpi_repo, jahre_werte={2023: "100", 2024: "102", 2025: "104"})

    lauf = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 9, 13), akteur="test")
    assert lauf.status == "ERHOEHUNG_ERZEUGT"
    schreiben = bundle.outbox_repo.get(lauf.erhoehungsschreiben_id)
    assert schreiben.ziel_bewertungsjahr == 2026
    assert schreiben.erhoehung_cent > 0


def test_termin_nicht_erreicht_vor_erstem_moeglichen_april(admin_ctx, basis_vertrag, bundle, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    _mit_komponente(stammdaten_repo, vertrag)
    _freigegebenes_wohnungsprofil(admin_ctx, bundle.rechtsprofil_service, vertrag, bezugsjahr=2026, bezugsmonat=1)

    lauf = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 6, 1), akteur="test")
    assert lauf.status == "TERMIN_NICHT_ERREICHT"


def test_fehlende_vpi_publikation_blockiert_ohne_erfundenen_wert(admin_ctx, basis_vertrag, bundle, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    _mit_komponente(stammdaten_repo, vertrag)
    _freigegebenes_wohnungsprofil(admin_ctx, bundle.rechtsprofil_service, vertrag)
    # bewusst KEINE VPI-Werte erfasst

    lauf = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 9, 13), akteur="test")
    assert lauf.status == "BLOCKIERT"
    assert any("VPI" in grund for grund in lauf.blockiert_gruende)


def test_monatslauf_ist_idempotent_je_periode(admin_ctx, basis_vertrag, bundle, stammdaten_repo):
    """Doppel-/Parallelstart: zwei Aufrufe für dieselbe Periode liefern
    denselben Lauf, kein zweites Erhöhungsschreiben."""

    vertrag, _konto = basis_vertrag
    _mit_komponente(stammdaten_repo, vertrag)
    _freigegebenes_wohnungsprofil(admin_ctx, bundle.rechtsprofil_service, vertrag)
    _seed_vpi(bundle.vpi_repo, jahre_werte={2023: "100", 2024: "102", 2025: "104"})

    erster = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 9, 13), akteur="test")
    zweiter = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 9, 20), akteur="test")

    assert erster.id == zweiter.id
    assert len(bundle.outbox_repo.liste_fuer_vertrag(vertrag.id)) == 1


def test_bereits_erfasstes_ziel_bewertungsjahr_erzeugt_kein_zweites_schreiben(admin_ctx, basis_vertrag, bundle, stammdaten_repo):
    """Anderer Kalendermonat, aber dasselbe Ziel-Bewertungsjahr - z. B.
    ein manuell gelöschter Lauf-Eintrag darf trotzdem kein zweites
    Erhöhungsschreiben für dasselbe Jahr erzeugen (Outbox-Unique bleibt
    die tatsächliche Garantie)."""

    vertrag, _konto = basis_vertrag
    debitor = stammdaten_repo.get_debitor(vertrag.debitor_id)
    stammdaten_repo.upsert_debitor(id=debitor.id, name=debitor.name, email=debitor.email, adresse="Corsogasse 1/3, 1010 Wien")
    _mit_komponente(stammdaten_repo, vertrag)
    _freigegebenes_wohnungsprofil(admin_ctx, bundle.rechtsprofil_service, vertrag)
    _seed_vpi(bundle.vpi_repo, jahre_werte={2023: "100", 2024: "102", 2025: "104"})

    bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 9, 13), akteur="test")
    lauf_oktober = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 10, 13), akteur="test")

    assert lauf_oktober.status == "BEREITS_ERFASST"
    assert len(bundle.outbox_repo.liste_fuer_vertrag(vertrag.id)) == 1


def test_blockiertes_erhoehungsschreiben_wird_nach_behobener_adresse_erneuert_ohne_duplikat(
    admin_ctx, basis_vertrag, bundle, stammdaten_repo
):
    """Unabhängiger Review (fd8c2b2-Folgereview): "Ein unsent BLOCKIERTer
    MieWeG-Fall muss nach behobener Quelle dagegen erneuerbar sein;
    dauerhaftes BEREITS_ERFASST darf den ganzen Aprilzyklus nicht
    verschlucken." - fehlende Adresse blockiert im ersten Monat; nach
    Korrektur wird im Folgemonat DERSELBE Outbox-Datensatz (kein
    Duplikat) auf BEREIT aktualisiert."""

    vertrag, _konto = basis_vertrag
    _mit_komponente(stammdaten_repo, vertrag)
    _freigegebenes_wohnungsprofil(admin_ctx, bundle.rechtsprofil_service, vertrag)
    _seed_vpi(bundle.vpi_repo, jahre_werte={2023: "100", 2024: "102", 2025: "104"})

    erster = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 9, 13), akteur="test")
    erstes_schreiben = bundle.outbox_repo.get(erster.erhoehungsschreiben_id)
    assert erstes_schreiben.status == "BLOCKIERT"  # Debitor in basis_vertrag hat keine Adresse

    debitor = stammdaten_repo.get_debitor(vertrag.debitor_id)
    stammdaten_repo.upsert_debitor(id=debitor.id, name=debitor.name, email=debitor.email, adresse="Corsogasse 1/3, 1010 Wien")

    zweiter = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 10, 13), akteur="test")
    zweites_schreiben = bundle.outbox_repo.get(zweiter.erhoehungsschreiben_id)
    assert zweites_schreiben.status == "BEREIT"
    assert zweites_schreiben.id == erstes_schreiben.id  # dieselbe Zeile aktualisiert, kein Duplikat
    assert len(bundle.outbox_repo.liste_fuer_vertrag(vertrag.id)) == 1


def test_kein_rechtsprofil_blockiert_ohne_rechtsannahme(admin_ctx, basis_vertrag, bundle, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    lauf = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 9, 13), akteur="test")
    assert lauf.status == "BLOCKIERT"


def test_haupt_untermiete_ungeklaert_blockiert(admin_ctx, basis_vertrag, bundle, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    _mit_komponente(stammdaten_repo, vertrag)
    _freigegebenes_wohnungsprofil(admin_ctx, bundle.rechtsprofil_service, vertrag, ist_hauptmiete=None)
    lauf = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 9, 13), akteur="test")
    assert lauf.status == "BLOCKIERT"
    assert any("Untermiete" in g or "Hauptmiete" in g for g in lauf.blockiert_gruende)


def test_geprueft_bestaetigte_untermiete_wird_nicht_gesperrt(admin_ctx, basis_vertrag, bundle, stammdaten_repo):
    """Modellreview 13.09.: "ist_hauptmiete is not True sperrt pauschal
    UNTERMIETEN, obwohl MieWeG ausdrücklich auch Wohnungsuntermiete
    umfasst" - `False` (GEPRÜFTE Untermiete) läuft normal über den
    Wohnungsrechner-Pfad, nur `None` (ungeklärt) sperrt."""

    vertrag, _konto = basis_vertrag
    _mit_komponente(stammdaten_repo, vertrag)
    _freigegebenes_wohnungsprofil(admin_ctx, bundle.rechtsprofil_service, vertrag, ist_hauptmiete=False)
    _seed_vpi(bundle.vpi_repo, jahre_werte={2023: "100", 2024: "102", 2025: "104"})
    lauf = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 9, 13), akteur="test")
    assert lauf.status == "ERHOEHUNG_ERZEUGT"


def test_geschaeftsraum_ohne_klausel_blockiert_kein_pauschales_mieweg(admin_ctx, basis_vertrag, bundle, stammdaten_repo):
    """"Geschäft unter MRG, unbekanntes Profil": MRG_TEIL OHNE bestätigte
    Wohnungsnutzung UND ohne Vertragsklausel darf keine automatische
    Berechnung auslösen."""

    vertrag, _konto = basis_vertrag
    _mit_komponente(stammdaten_repo, vertrag)
    _freigegebenes_wohnungsprofil(
        admin_ctx, bundle.rechtsprofil_service, vertrag, rechtsordnung="OESTERREICH_MRG_TEIL", ist_wohnungsnutzung=False,
    )
    lauf = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 9, 13), akteur="test")
    assert lauf.status == "BLOCKIERT"
    assert any("Geschäftsraum" in g or "Vertragsklausel" in g for g in lauf.blockiert_gruende)


def test_geschaeftsraum_klausel_ohne_kalenderregel_bleibt_gesperrt(admin_ctx, basis_vertrag, bundle, stammdaten_repo):
    """Geschäftsraum mit einer geprüften, freigegebenen IndexKlausel wird
    über index/service.py berechnet, NICHT über MieWeG - OHNE ein belegtes
    Kalender-/Intervallregelprofil (Auftrag Markus) bleibt der Pfad
    weiterhin gesperrt, KEIN Termin wird aus dem heutigen Datum erfunden."""

    vertrag, _konto = basis_vertrag
    _mit_komponente(stammdaten_repo, vertrag, betrag_cent=200_000)
    klausel = bundle.index_repo.anlegen(
        IndexKlauselTable(
            vertrag_id=vertrag.id, version=1, rechtsordnung="OESTERREICH_MRG_TEIL",
            berechnungsprofil="EINFACHER_SCHWELLENVERGLEICH", abschlussdatum=date(2024, 1, 1),
            basis_reihe="VPI20C18", basis_wert=Decimal("100"), basis_monat="2024-01",
            schwelle_prozent=Decimal("0"), indexierbare_komponenten=["HMZ"],
        )
    )
    bundle.index_repo.freigeben(klausel.id, freigegeben_von="markus")

    _freigegebenes_wohnungsprofil(
        admin_ctx, bundle.rechtsprofil_service, vertrag, rechtsordnung="OESTERREICH_MRG_TEIL",
        ist_wohnungsnutzung=False, vertraglich_zulaessiger_betrag_cent=None, vertraglicher_quellenbeleg=None,
        vertragsklausel_id=klausel.id,
    )
    bundle.vpi_repo.monatswert_erfassen(
        reihe="VPI20C18", jahr=2026, monat=8, wert=Decimal("105"), finalitaet="ENDGUELTIG",
        quelle_datei="synthetisch", quelle_zeile=1, quelle_hash="deadbeef",
        abgerufen_am=__import__("datetime").datetime(2026, 9, 1, tzinfo=__import__("datetime").timezone.utc),
        importiert_von="markus",
    )

    lauf = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 9, 13), akteur="test")
    assert lauf.status == "BLOCKIERT"
    assert any("Terminmodell" in g for g in lauf.blockiert_gruende)
    assert lauf.erhoehungsschreiben_id is None
    assert bundle.index_repo.letzte_anpassung(vertrag.id) is None  # keine IndexAnpassung ohne belegte Regel


def _mit_kalenderklausel(
    bundle, vertrag, *, schwelle_prozent=Decimal("0"), schwelle_inklusive=True,
    terminmodus="FIXER_MONAT", anpassungsmonat=1, mindestintervall_monate=12,
) -> IndexKlauselTable:
    klausel = bundle.index_repo.anlegen(
        IndexKlauselTable(
            vertrag_id=vertrag.id, version=1, rechtsordnung="OESTERREICH_MRG_TEIL",
            berechnungsprofil="EINFACHER_SCHWELLENVERGLEICH", abschlussdatum=date(2024, 1, 1),
            basis_reihe="VPI20C18", basis_wert=Decimal("100"), basis_monat="2024-01",
            schwelle_prozent=schwelle_prozent, schwelle_inklusive=schwelle_inklusive,
            indexierbare_komponenten=["HMZ"], terminmodus=terminmodus, anpassungsmonat=anpassungsmonat,
            mindestintervall_monate=mindestintervall_monate,
        )
    )
    return bundle.index_repo.freigeben(klausel.id, freigegeben_von="markus")


def _seed_vpi_monat(bundle, *, jahr, monat, wert, reihe="VPI20C18", veroeffentlicht_am=None):
    bundle.vpi_repo.monatswert_erfassen(
        reihe=reihe, jahr=jahr, monat=monat, wert=Decimal(wert), finalitaet="ENDGUELTIG",
        quelle_datei="synthetisch", quelle_zeile=1, quelle_hash=f"hash-{jahr}-{monat:02d}",
        abgerufen_am=__import__("datetime").datetime(jahr, monat, 20, tzinfo=__import__("datetime").timezone.utc),
        importiert_von="markus", veroeffentlicht_am=veroeffentlicht_am,
    )


def test_geschaeftsraum_ausserhalb_anpassungsmonat_kein_brief(admin_ctx, basis_vertrag, bundle, stammdaten_repo):
    """Auftrag Markus: außerhalb des belegten Anpassungsmonats (hier
    Jänner) darf niemals ein Erhöhungsschreiben entstehen."""

    vertrag, _konto = basis_vertrag
    _mit_komponente(stammdaten_repo, vertrag)
    klausel = _mit_kalenderklausel(bundle, vertrag)
    _freigegebenes_wohnungsprofil(
        admin_ctx, bundle.rechtsprofil_service, vertrag, rechtsordnung="OESTERREICH_MRG_TEIL",
        ist_wohnungsnutzung=False, vertraglich_zulaessiger_betrag_cent=None, vertraglicher_quellenbeleg=None,
        vertragsklausel_id=klausel.id,
    )
    _seed_vpi_monat(bundle, jahr=2026, monat=5, wert="110")

    lauf = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 6, 13), akteur="test")
    assert lauf.status == "TERMIN_NICHT_ERREICHT"
    assert lauf.erhoehungsschreiben_id is None
    assert bundle.index_repo.letzte_anpassung(vertrag.id) is None


def test_geschaeftsraum_januar_unter_schwelle_kein_brief(admin_ctx, basis_vertrag, bundle, stammdaten_repo):
    """Auftrag Markus: im richtigen Kalendermonat, aber unterhalb der
    vertraglichen Schwelle, entsteht ebenfalls kein Brief."""

    vertrag, _konto = basis_vertrag
    _mit_komponente(stammdaten_repo, vertrag)
    klausel = _mit_kalenderklausel(bundle, vertrag, schwelle_prozent=Decimal("5"))
    _freigegebenes_wohnungsprofil(
        admin_ctx, bundle.rechtsprofil_service, vertrag, rechtsordnung="OESTERREICH_MRG_TEIL",
        ist_wohnungsnutzung=False, vertraglich_zulaessiger_betrag_cent=None, vertraglicher_quellenbeleg=None,
        vertragsklausel_id=klausel.id,
    )
    _seed_vpi_monat(bundle, jahr=2025, monat=12, wert="102")  # +2%, unter der 5%-Schwelle

    lauf = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 1, 13), akteur="test")
    assert lauf.status == "KEIN_ERHOEHUNGSBEDARF"
    assert lauf.erhoehungsschreiben_id is None


def test_geschaeftsraum_berechtigter_januarfall_bis_outbox_kein_doppelbrief(
    admin_ctx, basis_vertrag, bundle, stammdaten_repo
):
    """Auftrag Markus: ein berechtigter Jännerfall muss über den echten
    Monatslauf bis zu einer BEREITen Outbox-Zeile führen; der
    unveränderte Folgemonat (Februar, außerhalb des Anpassungsmonats)
    darf keinen zweiten Brief erzeugen."""

    vertrag, _konto = basis_vertrag
    stammdaten_repo.upsert_debitor(id="DEB-1001", name="Max Mustermieter", email="mieter@example.at", adresse="Corsogasse 1/3, 1010 Wien")
    _mit_komponente(stammdaten_repo, vertrag)
    klausel = _mit_kalenderklausel(bundle, vertrag)
    _freigegebenes_wohnungsprofil(
        admin_ctx, bundle.rechtsprofil_service, vertrag, rechtsordnung="OESTERREICH_MRG_TEIL",
        ist_wohnungsnutzung=False, vertraglich_zulaessiger_betrag_cent=None, vertraglicher_quellenbeleg=None,
        vertragsklausel_id=klausel.id,
    )
    _seed_vpi_monat(bundle, jahr=2025, monat=12, wert="110")  # +10%

    lauf_januar = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 1, 13), akteur="test")
    assert lauf_januar.status == "ERHOEHUNG_ERZEUGT", lauf_januar.blockiert_gruende
    assert lauf_januar.erhoehungsschreiben_id is not None
    schreiben = bundle.outbox_repo.get(lauf_januar.erhoehungsschreiben_id)
    assert schreiben.status == "BEREIT"
    assert schreiben.erhoehung_cent == 10_000
    assert schreiben.massgeblicher_termin == date(2026, 1, 1)

    anpassung = bundle.index_repo.letzte_anpassung(vertrag.id)
    assert anpassung.vpi_jahr == 2025 and anpassung.vpi_monat == 12

    neue_klausel = bundle.index_repo.get_klausel(klausel.id)  # ursprüngliche Version bleibt unverändert (append-only)
    assert neue_klausel.basis_wert == Decimal("100")

    lauf_februar = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 2, 13), akteur="test")
    assert lauf_februar.status == "TERMIN_NICHT_ERREICHT"
    assert lauf_februar.erhoehungsschreiben_id is None
    assert bundle.index_repo.letzte_anpassung(vertrag.id).id == anpassung.id  # keine zweite IndexAnpassung


def test_naechster_gueltiger_kalendermonat_ist_kein_erfundenes_zwoelf_monats_intervall():
    """Auftrag Markus: "Jährlich am 01.01. bedeutet bei Beginn 01.04.2026
    den ersten möglichen Termin 01.01.2027; nicht automatisch zwölf
    Monate Wartezeit ab Mietbeginn erfinden" - der erste zulässige Termin
    ist die nächste TATSÄCHLICHE Kalendermonats-Wiederkehr, nicht
    Vertragsbeginn + 12 Monate (der wäre 01.04.2027)."""

    assert _naechster_gueltiger_kalendermonat(date(2026, 4, 1), 1) == date(2027, 1, 1)
    # Ein bereits gültiger Zieltag bleibt unverändert (keine künstliche
    # Verschiebung um ein volles Jahr).
    assert _naechster_gueltiger_kalendermonat(date(2026, 1, 1), 1) == date(2026, 1, 1)
    assert _naechster_gueltiger_kalendermonat(date(2026, 1, 2), 1) == date(2027, 1, 1)


def test_monate_addieren_rechnet_kalendermonate_nicht_tage():
    """Auftrag Markus: "keine pauschale Umrechnung zwei Monate=60 Tage" -
    Monatsaddition landet immer auf dem 1. des Zielmonats, unabhängig von
    der tatsächlichen Tageszahl der durchlaufenen Monate."""

    assert _monate_addieren(date(2026, 1, 15), 2) == date(2026, 3, 1)
    assert _monate_addieren(date(2026, 12, 1), 2) == date(2027, 2, 1)
    assert _monate_addieren(date(2026, 1, 1), 12) == date(2027, 1, 1)


def test_geschaeftsraum_erster_termin_ist_naechste_kalenderwiederkehr_nicht_beginn_plus_zwoelf(
    admin_ctx, bundle, stammdaten_repo
):
    """End-to-End-Beleg zum obigen Einheitstest: Vertragsbeginn 1.4.2026,
    Anpassungsmonat Jänner - im Jänner 2027 (9 Monate nach Vertragsbeginn)
    muss die Anpassung bereits möglich sein, nicht erst im April 2027."""

    stammdaten_repo.upsert_gesellschaft(id="7DI", name="7D Immobilien GmbH")
    stammdaten_repo.upsert_objekt(id="601", gesellschaft_id="7DI", bezeichnung="Am Corso")
    stammdaten_repo.upsert_einheit(id="601-TOP9", objekt_id="601", bezeichnung="Top 9", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten_repo.upsert_debitor(id="DEB-9", name="Geschäftsraum-Mieterin", email="gr@example.at", adresse="Corsogasse 1/9, 1010 Wien")
    stammdaten_repo.upsert_vertrag(
        id="V-601-9", einheit_id="601-TOP9", debitor_id="DEB-9", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_TEIL", gueltig_von=date(2026, 4, 1),
    )
    vertrag = stammdaten_repo.get_vertrag("V-601-9")
    stammdaten_repo.get_or_create_konto(vertrag=vertrag)
    _mit_komponente(stammdaten_repo, vertrag)
    klausel = _mit_kalenderklausel(bundle, vertrag)
    _freigegebenes_wohnungsprofil(
        admin_ctx, bundle.rechtsprofil_service, vertrag, rechtsordnung="OESTERREICH_MRG_TEIL",
        ist_wohnungsnutzung=False, vertraglich_zulaessiger_betrag_cent=None, vertraglicher_quellenbeleg=None,
        vertragsklausel_id=klausel.id,
    )
    _seed_vpi_monat(bundle, jahr=2026, monat=12, wert="110")

    lauf = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2027, 1, 13), akteur="test")
    assert lauf.status == "ERHOEHUNG_ERZEUGT", lauf.blockiert_gruende


def test_geschaeftsraum_indexwert_rundung_auf_eine_dezimalstelle(admin_ctx, basis_vertrag, bundle, stammdaten_repo):
    """Auftrag Markus: bekannte vertragliche Rundung des Indexwerts auf
    eine Dezimalstelle explizit unterstützen - ein Wert, der erst nach
    Rundung die Schwelle erreicht, muss dann auch tatsächlich auslösen."""

    vertrag, _konto = basis_vertrag
    stammdaten_repo.upsert_debitor(id="DEB-1001", name="Max Mustermieter", email="mieter@example.at", adresse="Corsogasse 1/3, 1010 Wien")
    _mit_komponente(stammdaten_repo, vertrag)
    klausel_id = bundle.index_repo.anlegen(
        IndexKlauselTable(
            vertrag_id=vertrag.id, version=1, rechtsordnung="OESTERREICH_MRG_TEIL",
            berechnungsprofil="EINFACHER_SCHWELLENVERGLEICH", abschlussdatum=date(2024, 1, 1),
            basis_reihe="VPI20C18", basis_wert=Decimal("100"), basis_monat="2024-01",
            schwelle_prozent=Decimal("5"), schwelle_inklusive=True, indexierbare_komponenten=["HMZ"],
            terminmodus="FIXER_MONAT", anpassungsmonat=1, mindestintervall_monate=12,
            indexwert_rundung_dezimalstellen=1,
        )
    ).id
    bundle.index_repo.freigeben(klausel_id, freigegeben_von="markus")
    _freigegebenes_wohnungsprofil(
        admin_ctx, bundle.rechtsprofil_service, vertrag, rechtsordnung="OESTERREICH_MRG_TEIL",
        ist_wohnungsnutzung=False, vertraglich_zulaessiger_betrag_cent=None, vertraglicher_quellenbeleg=None,
        vertragsklausel_id=klausel_id,
    )
    # Roher amtlicher Wert 104.96 -> rohe Veränderung 4,96% - UNGERUNDET
    # unter der 5%-Schwelle, löst NICHT aus. Auf eine Dezimalstelle
    # gerundet 105.0 -> exakt 5,0%, bei schwelle_inklusive=True auslösend.
    # Beweist, dass die Rundung tatsächlich VOR dem Schwellenvergleich
    # angewendet wird (nicht nur eine wirkungslose Zier-Option).
    _seed_vpi_monat(bundle, jahr=2025, monat=12, wert="104.96")

    lauf = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 1, 13), akteur="test")
    assert lauf.status == "ERHOEHUNG_ERZEUGT", lauf.blockiert_gruende


def test_geschaeftsraum_wartefrist_ohne_bezug_blockiert_anlage(admin_ctx, basis_vertrag, bundle, stammdaten_repo):
    """Auftrag Markus: eine Wartefrist ohne eindeutigen Bezug (VPI_PERIODE
    ODER VEROEFFENTLICHUNG) ist ein Rateversuch und wird bereits bei der
    Klauselanlage (vor jedem DB-Zugriff) abgelehnt."""

    vertrag, _konto = basis_vertrag
    index_service = IndexService(bundle.index_repo, stammdaten_repo)
    with pytest.raises(ValueError, match="wartefrist"):
        index_service.klausel_anlegen(
            ctx=admin_ctx, vertrag_id=vertrag.id, rechtsordnung="OESTERREICH_MRG_TEIL",
            berechnungsprofil="EINFACHER_SCHWELLENVERGLEICH", abschlussdatum=date(2024, 1, 1),
            basis_reihe="VPI20C18", basis_wert=Decimal("100"), basis_monat="2024-01",
            wartefrist_monate_nach_indexereignis=2, wartefrist_bezug=None,
        )


def test_geschaeftsraum_wartefrist_nach_veroeffentlichung(admin_ctx, basis_vertrag, bundle, stammdaten_repo):
    """Auftrag Markus: eine vertragliche Wartefrist NACH der amtlichen
    Veröffentlichung des Indexwerts (nicht nach der VPI-Periode selbst) -
    explizit belegt über `wartefrist_bezug="VEROEFFENTLICHUNG"` UND das
    eigene `veroeffentlicht_am`-Feld (NICHT `abgerufen_am`, Codex-
    Rückprüfung zu 5535ae2: "VPI.abgerufen_am ist NUR Abruf, niemals
    amtlicher Veröffentlichungstag")."""

    vertrag, _konto = basis_vertrag
    stammdaten_repo.upsert_debitor(id="DEB-1001", name="Max Mustermieter", email="mieter@example.at", adresse="Corsogasse 1/3, 1010 Wien")
    _mit_komponente(stammdaten_repo, vertrag)
    klausel_id = bundle.index_repo.anlegen(
        IndexKlauselTable(
            vertrag_id=vertrag.id, version=1, rechtsordnung="OESTERREICH_MRG_TEIL",
            berechnungsprofil="EINFACHER_SCHWELLENVERGLEICH", abschlussdatum=date(2024, 1, 1),
            basis_reihe="VPI20C18", basis_wert=Decimal("100"), basis_monat="2024-01",
            schwelle_prozent=Decimal("0"), indexierbare_komponenten=["HMZ"],
            terminmodus="FIXER_MONAT", anpassungsmonat=1, mindestintervall_monate=12,
            wartefrist_monate_nach_indexereignis=2, wartefrist_bezug="VEROEFFENTLICHUNG",
        )
    ).id
    bundle.index_repo.freigeben(klausel_id, freigegeben_von="markus")
    _freigegebenes_wohnungsprofil(
        admin_ctx, bundle.rechtsprofil_service, vertrag, rechtsordnung="OESTERREICH_MRG_TEIL",
        ist_wohnungsnutzung=False, vertraglich_zulaessiger_betrag_cent=None, vertraglicher_quellenbeleg=None,
        vertragsklausel_id=klausel_id,
    )
    # Amtlich veröffentlicht am 2025-12-20 (Abruf erfolgte erst am 20.,
    # ist aber für die Frist irrelevant) - zwei taggenaue Kalendermonate
    # Wartefrist enden am 2026-02-20, also NACH dem Jänner-Anpassungsmonat.
    _seed_vpi_monat(bundle, jahr=2025, monat=12, wert="110", veroeffentlicht_am=date(2025, 12, 20))

    lauf_januar = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 1, 13), akteur="test")
    assert lauf_januar.status == "TERMIN_NICHT_ERREICHT"
    assert lauf_januar.erhoehungsschreiben_id is None


def test_geschaeftsraum_wartefrist_ohne_belegte_veroeffentlichung_blockiert(
    admin_ctx, basis_vertrag, bundle, stammdaten_repo
):
    """Codex-Rückprüfung zu 5535ae2: `abgerufen_am` (der eigene
    Abrufzeitpunkt) darf NIE als amtlicher Veröffentlichungstag
    herhalten - ohne belegtes `veroeffentlicht_am` bleibt der Fall
    gesperrt, obwohl `abgerufen_am` gesetzt ist."""

    vertrag, _konto = basis_vertrag
    stammdaten_repo.upsert_debitor(id="DEB-1001", name="Max Mustermieter", email="mieter@example.at", adresse="Corsogasse 1/3, 1010 Wien")
    _mit_komponente(stammdaten_repo, vertrag)
    klausel_id = bundle.index_repo.anlegen(
        IndexKlauselTable(
            vertrag_id=vertrag.id, version=1, rechtsordnung="OESTERREICH_MRG_TEIL",
            berechnungsprofil="EINFACHER_SCHWELLENVERGLEICH", abschlussdatum=date(2024, 1, 1),
            basis_reihe="VPI20C18", basis_wert=Decimal("100"), basis_monat="2024-01",
            schwelle_prozent=Decimal("0"), indexierbare_komponenten=["HMZ"],
            terminmodus="FIXER_MONAT", anpassungsmonat=1, mindestintervall_monate=12,
            wartefrist_monate_nach_indexereignis=2, wartefrist_bezug="VEROEFFENTLICHUNG",
        )
    ).id
    bundle.index_repo.freigeben(klausel_id, freigegeben_von="markus")
    _freigegebenes_wohnungsprofil(
        admin_ctx, bundle.rechtsprofil_service, vertrag, rechtsordnung="OESTERREICH_MRG_TEIL",
        ist_wohnungsnutzung=False, vertraglich_zulaessiger_betrag_cent=None, vertraglicher_quellenbeleg=None,
        vertragsklausel_id=klausel_id,
    )
    # `abgerufen_am` wird von `_seed_vpi_monat` immer gesetzt, aber KEIN
    # `veroeffentlicht_am` - das darf NICHT als Ersatzbeleg genügen.
    _seed_vpi_monat(bundle, jahr=2025, monat=12, wert="110")

    lauf = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 1, 13), akteur="test")
    assert lauf.status == "BLOCKIERT"
    assert any("Veröffentlichung" in g for g in lauf.blockiert_gruende)
    assert lauf.erhoehungsschreiben_id is None


def test_geschaeftsraum_bei_schwelle_ohne_kalenderbindung_loest_ausserhalb_jaenner_aus(
    admin_ctx, basis_vertrag, bundle, stammdaten_repo
):
    """Terminmodell BEI_SCHWELLE (Codex-Rückprüfung zu 5535ae2, Punkt 1):
    eine reine Schwellenklausel OHNE festen Anpassungsmonat muss auch
    AUSSERHALB Jänner auslösen können, sobald die Schwelle überschritten
    ist - "reine Schwellenklauseln ohne festen Monat" waren mit
    zwingendem anpassungsmonat bisher nicht darstellbar."""

    vertrag, _konto = basis_vertrag
    stammdaten_repo.upsert_debitor(id="DEB-1001", name="Max Mustermieter", email="mieter@example.at", adresse="Corsogasse 1/3, 1010 Wien")
    _mit_komponente(stammdaten_repo, vertrag)
    klausel = bundle.index_repo.anlegen(
        IndexKlauselTable(
            vertrag_id=vertrag.id, version=1, rechtsordnung="OESTERREICH_MRG_TEIL",
            berechnungsprofil="EINFACHER_SCHWELLENVERGLEICH", abschlussdatum=date(2024, 1, 1),
            basis_reihe="VPI20C18", basis_wert=Decimal("100"), basis_monat="2024-01",
            schwelle_prozent=Decimal("5"), indexierbare_komponenten=["HMZ"], terminmodus="BEI_SCHWELLE",
        )
    )
    bundle.index_repo.freigeben(klausel.id, freigegeben_von="markus")
    _freigegebenes_wohnungsprofil(
        admin_ctx, bundle.rechtsprofil_service, vertrag, rechtsordnung="OESTERREICH_MRG_TEIL",
        ist_wohnungsnutzung=False, vertraglich_zulaessiger_betrag_cent=None, vertraglicher_quellenbeleg=None,
        vertragsklausel_id=klausel.id,
    )
    _seed_vpi_monat(bundle, jahr=2026, monat=5, wert="110")  # +10%, Mai - AUSSERHALB Jänner

    lauf = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 6, 13), akteur="test")
    assert lauf.status == "ERHOEHUNG_ERZEUGT", lauf.blockiert_gruende


def test_geschaeftsraum_intervall_ohne_fixen_monat_respektiert_mindestabstand(
    admin_ctx, basis_vertrag, bundle, stammdaten_repo
):
    """Terminmodell INTERVALL (Codex-Rückprüfung zu 5535ae2, Punkt 1):
    "maximal einmal jährlich" OHNE Kalenderbindung - beliebiger Monat ist
    zulässig, aber der Mindestabstand seit der letzten Anpassung gilt
    weiterhin."""

    vertrag, _konto = basis_vertrag
    stammdaten_repo.upsert_debitor(id="DEB-1001", name="Max Mustermieter", email="mieter@example.at", adresse="Corsogasse 1/3, 1010 Wien")
    _mit_komponente(stammdaten_repo, vertrag)
    klausel = bundle.index_repo.anlegen(
        IndexKlauselTable(
            vertrag_id=vertrag.id, version=1, rechtsordnung="OESTERREICH_MRG_TEIL",
            berechnungsprofil="EINFACHER_SCHWELLENVERGLEICH", abschlussdatum=date(2024, 1, 1),
            basis_reihe="VPI20C18", basis_wert=Decimal("100"), basis_monat="2024-01",
            schwelle_prozent=Decimal("0"), indexierbare_komponenten=["HMZ"], terminmodus="INTERVALL",
            mindestintervall_monate=12,
        )
    )
    bundle.index_repo.freigeben(klausel.id, freigegeben_von="markus")
    _freigegebenes_wohnungsprofil(
        admin_ctx, bundle.rechtsprofil_service, vertrag, rechtsordnung="OESTERREICH_MRG_TEIL",
        ist_wohnungsnutzung=False, vertraglich_zulaessiger_betrag_cent=None, vertraglicher_quellenbeleg=None,
        vertragsklausel_id=klausel.id,
    )
    _seed_vpi_monat(bundle, jahr=2026, monat=4, wert="105")

    lauf_april = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 4, 13), akteur="test")
    assert lauf_april.status == "ERHOEHUNG_ERZEUGT", lauf_april.blockiert_gruende

    _seed_vpi_monat(bundle, jahr=2026, monat=10, wert="110")
    lauf_oktober = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 10, 13), akteur="test")
    assert lauf_oktober.status == "TERMIN_NICHT_ERREICHT"  # erst 6 statt 12 Monate seit April vergangen


def test_geschaeftsraum_wartefrist_anker_verschiebt_sich_nicht_mit_neuen_vpi_daten(
    admin_ctx, basis_vertrag, bundle, stammdaten_repo
):
    """Codex-Rückprüfung zu 5535ae2 (Punkt 3): die Wartefrist läuft ab dem
    ERSTEN (chronologisch frühesten) Überschreitungsereignis, NICHT ab
    dem jeweils NEUESTEN verfügbaren VPI-Wert - sonst würde jeder neu
    hinzukommende Monat die Frist immer weiter nach hinten verschieben,
    sodass sie NIE erfüllt wäre ("sonst verschiebt sie sich endlos")."""

    vertrag, _konto = basis_vertrag
    stammdaten_repo.upsert_debitor(id="DEB-1001", name="Max Mustermieter", email="mieter@example.at", adresse="Corsogasse 1/3, 1010 Wien")
    _mit_komponente(stammdaten_repo, vertrag)
    klausel = bundle.index_repo.anlegen(
        IndexKlauselTable(
            vertrag_id=vertrag.id, version=1, rechtsordnung="OESTERREICH_MRG_TEIL",
            berechnungsprofil="EINFACHER_SCHWELLENVERGLEICH", abschlussdatum=date(2024, 1, 1),
            basis_reihe="VPI20C18", basis_wert=Decimal("100"), basis_monat="2025-01",
            schwelle_prozent=Decimal("0"), indexierbare_komponenten=["HMZ"], terminmodus="BEI_SCHWELLE",
            wartefrist_monate_nach_indexereignis=2, wartefrist_bezug="VPI_PERIODE",
        )
    )
    bundle.index_repo.freigeben(klausel.id, freigegeben_von="markus")
    _freigegebenes_wohnungsprofil(
        admin_ctx, bundle.rechtsprofil_service, vertrag, rechtsordnung="OESTERREICH_MRG_TEIL",
        ist_wohnungsnutzung=False, vertraglich_zulaessiger_betrag_cent=None, vertraglicher_quellenbeleg=None,
        vertragsklausel_id=klausel.id,
    )
    # Erstes Überschreitungsereignis: Februar 2025 (+5%, Schwelle 0%).
    _seed_vpi_monat(bundle, jahr=2025, monat=2, wert="105")

    lauf_maerz = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2025, 3, 1), akteur="test")
    assert lauf_maerz.status == "TERMIN_NICHT_ERREICHT"

    # Ein weiterer Monat mit einem NEUEREN VPI-Wert trifft ein, während
    # die Wartefrist noch läuft - der Anker bleibt trotzdem Februar 2025.
    _seed_vpi_monat(bundle, jahr=2025, monat=3, wert="106")

    lauf_april = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2025, 4, 15), akteur="test")
    assert lauf_april.status == "ERHOEHUNG_ERZEUGT", lauf_april.blockiert_gruende
    anpassung = bundle.index_repo.letzte_anpassung(vertrag.id)
    assert anpassung.vpi_jahr == 2025 and anpassung.vpi_monat == 2  # Anker bleibt Februar, nicht März


def test_geschaeftsraum_senkung_erzeugt_pruefbedarf_statt_verstecktem_kein_erhoehungsbedarf(
    admin_ctx, basis_vertrag, bundle, stammdaten_repo
):
    """Codex-Rückprüfung zu 5535ae2 (Punkt 5): eine tatsächliche SENKUNG
    darf nicht unter KEIN_ERHOEHUNGSBEDARF verschwinden - sie bleibt als
    eigener, sichtbarer interner Prüfbedarf erkennbar (automatische
    Senkungen sind nicht implementiert)."""

    vertrag, _konto = basis_vertrag
    stammdaten_repo.upsert_debitor(id="DEB-1001", name="Max Mustermieter", email="mieter@example.at", adresse="Corsogasse 1/3, 1010 Wien")
    _mit_komponente(stammdaten_repo, vertrag)
    klausel = _mit_kalenderklausel(bundle, vertrag)
    _freigegebenes_wohnungsprofil(
        admin_ctx, bundle.rechtsprofil_service, vertrag, rechtsordnung="OESTERREICH_MRG_TEIL",
        ist_wohnungsnutzung=False, vertraglich_zulaessiger_betrag_cent=None, vertraglicher_quellenbeleg=None,
        vertragsklausel_id=klausel.id,
    )
    _seed_vpi_monat(bundle, jahr=2025, monat=12, wert="90")  # -10%

    lauf = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 1, 13), akteur="test")
    assert lauf.status == "SENKUNG_PRUEFBEDARF", lauf.blockiert_gruende
    assert lauf.erhoehungsschreiben_id is None
    anpassung = bundle.index_repo.letzte_anpassung(vertrag.id)
    assert anpassung is not None and anpassung.erhoehung_cent < 0


def test_migrierte_klausel_mit_belegter_letzter_anpassung_respektiert_mindestintervall(
    admin_ctx, basis_vertrag, bundle, stammdaten_repo
):
    """Auftrag Markus (Portal-Anforderung): eine bei Neuanlage/Migration
    bereits belegte `letzte_anpassung_monat` muss den Mindestabstand
    korrekt AB DIESEM Datum prüfen, NICHT wie eine echte Erstanpassung ab
    Vertragsbeginn (die dort implementierte `_naechster_gueltiger_
    kalendermonat`-Logik gilt nur, wenn wirklich noch NIE angepasst
    wurde)."""

    vertrag, _konto = basis_vertrag
    stammdaten_repo.upsert_debitor(id="DEB-1001", name="Max Mustermieter", email="mieter@example.at", adresse="Corsogasse 1/3, 1010 Wien")
    _mit_komponente(stammdaten_repo, vertrag)
    klausel = bundle.index_repo.anlegen(
        IndexKlauselTable(
            vertrag_id=vertrag.id, version=1, rechtsordnung="OESTERREICH_MRG_TEIL",
            berechnungsprofil="EINFACHER_SCHWELLENVERGLEICH", abschlussdatum=date(2020, 1, 1),
            basis_reihe="VPI20C18", basis_wert=Decimal("100"), basis_monat="2025-01",
            letzte_anpassung_monat="2026-01",  # bereits vor Systemeinführung erfolgt (belegt)
            schwelle_prozent=Decimal("0"), indexierbare_komponenten=["HMZ"],
            terminmodus="FIXER_MONAT", anpassungsmonat=1, mindestintervall_monate=12,
        )
    )
    bundle.index_repo.freigeben(klausel.id, freigegeben_von="markus")
    _freigegebenes_wohnungsprofil(
        admin_ctx, bundle.rechtsprofil_service, vertrag, rechtsordnung="OESTERREICH_MRG_TEIL",
        ist_wohnungsnutzung=False, vertraglich_zulaessiger_betrag_cent=None, vertraglicher_quellenbeleg=None,
        vertragsklausel_id=klausel.id,
    )
    _seed_vpi_monat(bundle, jahr=2026, monat=12, wert="110")

    # Jänner 2027 - exakt 12 Monate nach der BELEGTEN letzten Anpassung
    # (Jänner 2026), nicht 3 Jahre nach Vertragsbeginn (2024) - zulässig.
    lauf = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2027, 1, 13), akteur="test")
    assert lauf.status == "ERHOEHUNG_ERZEUGT", lauf.blockiert_gruende


def test_mietzinsobergrenze_kappt_erhoehung_unabhaengig_von_foerderbindung(admin_ctx, basis_vertrag, bundle, stammdaten_repo):
    """Unabhängiger Review (fd8c2b2-Folgereview): "Die harte
    Mietzinsobergrenze gilt bei MRG-Voll unabhängig davon, ob
    foerderbindung gesetzt ist" - die Kappung wirkt bereits, wenn
    mietzinsobergrenze_cent erfasst ist, ohne foerderbindung=True."""

    vertrag, _konto = basis_vertrag
    _mit_komponente(stammdaten_repo, vertrag)
    _freigegebenes_wohnungsprofil(
        admin_ctx, bundle.rechtsprofil_service, vertrag, foerderbindung=False,
        mietzinsobergrenze_cent=100_500, mietzinsobergrenze_quellenbeleg="Bescheid XY",
    )
    _seed_vpi(bundle.vpi_repo, jahre_werte={2023: "100", 2024: "102", 2025: "104"})

    lauf = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 9, 13), akteur="test")
    assert lauf.status == "ERHOEHUNG_ERZEUGT"
    schreiben = bundle.outbox_repo.get(lauf.erhoehungsschreiben_id)
    assert schreiben.erhoehung_cent == 500  # gekappt auf 100.500 statt des höheren gesetzlichen/vertraglichen Werts


def test_abgelaufene_mietzinsobergrenze_blockiert(admin_ctx, basis_vertrag, bundle, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    _mit_komponente(stammdaten_repo, vertrag)
    _freigegebenes_wohnungsprofil(
        admin_ctx, bundle.rechtsprofil_service, vertrag, mietzinsobergrenze_cent=100_500,
        mietzinsobergrenze_quellenbeleg="Bescheid XY", mietzinsobergrenze_gueltig_bis=date(2025, 12, 31),
    )
    _seed_vpi(bundle.vpi_repo, jahre_werte={2023: "100", 2024: "102", 2025: "104"})

    lauf = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 9, 13), akteur="test")
    # Bereits RechtsprofilService.ist_noch_gueltig erkennt die
    # verstrichene Mietzinsobergrenze-Gültigkeit und entwertet die
    # Freigabe zeitbasiert (siehe test_indexautomatik_rechtsprofil.py) -
    # der Fall bleibt dadurch sicher blockiert, auch ohne dass die
    # zusätzliche Kappungsprüfung in service.py je erreicht wird.
    assert lauf.status == "BLOCKIERT"
    assert any("Rechtsprofil" in g for g in lauf.blockiert_gruende)


def test_auth_wird_vor_bestehendem_lauf_geprueft(admin_ctx, ctx_factory, basis_vertrag, bundle, stammdaten_repo):
    """Unabhängiger Review (fd8c2b2-Folgereview): "monatslauf_fuer_vertrag
    gibt vorhandenen Lauf VOR Auth zurück" - ein Aufrufer ohne Zugriff
    auf die Gesellschaft darf eine bereits existierende Zeile nicht
    einmal lesen können."""

    vertrag, _konto = basis_vertrag
    _mit_komponente(stammdaten_repo, vertrag)
    _freigegebenes_wohnungsprofil(admin_ctx, bundle.rechtsprofil_service, vertrag)
    _seed_vpi(bundle.vpi_repo, jahre_werte={2023: "100", 2024: "102", 2025: "104"})
    bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 9, 13), akteur="test")

    fremder_ctx = ctx_factory("ANDERE-GESELLSCHAFT")
    with pytest.raises(Exception):
        bundle.index_service.monatslauf_fuer_vertrag(ctx=fremder_ctx, vertrag=vertrag, heute=date(2026, 9, 13), akteur="test")


def test_zukuenftiger_vertrag_wird_im_batch_uebersprungen(admin_ctx, basis_vertrag, bundle, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    stammdaten_repo.upsert_vertrag(
        id=vertrag.id, einheit_id=vertrag.einheit_id, debitor_id=vertrag.debitor_id,
        gesellschaft_id=vertrag.gesellschaft_id, rechtsordnung=vertrag.rechtsordnung, gueltig_von=date(2027, 1, 1),
    )
    laeufe = bundle.index_service.monatslauf_alle(ctx=admin_ctx, heute=date(2026, 9, 13), akteur="test")
    assert laeufe == []


def test_blockierter_lauf_wird_im_folgemonat_erneut_versucht_ohne_terminalen_status_zu_verlieren(
    admin_ctx, basis_vertrag, bundle, stammdaten_repo
):
    """"Begründet blockierte Fälle müssen nach Quellen-/Profiländerung
    sicher erneut prüfbar sein" - ein BLOCKIERTer Fall (fehlende VPI)
    wird, nachdem der Wert nachgetragen wurde, in einem NEUEN
    Kalendermonat erfolgreich abgeschlossen (die Perioden-Idempotenz
    bleibt dabei intakt: kein doppeltes Erhöhungsschreiben)."""

    vertrag, _konto = basis_vertrag
    _mit_komponente(stammdaten_repo, vertrag)
    _freigegebenes_wohnungsprofil(admin_ctx, bundle.rechtsprofil_service, vertrag)

    erster = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 9, 13), akteur="test")
    assert erster.status == "BLOCKIERT"

    _seed_vpi(bundle.vpi_repo, jahre_werte={2023: "100", 2024: "102", 2025: "104"})
    zweiter = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 10, 13), akteur="test")
    assert zweiter.status == "ERHOEHUNG_ERZEUGT"
    assert len(bundle.outbox_repo.liste_fuer_vertrag(vertrag.id)) == 1


def test_versand_wird_blockiert_wenn_unveraenderte_komponente_sich_seit_entwurf_geaendert_hat(
    admin_ctx, basis_vertrag, bundle, stammdaten_repo
):
    """Ergänzender Repro (6317f96): "echter Monatslauf erzeugt BEREIT mit
    HMZ 1000 + BK 100; danach BK_VORAUSZAHLUNG von 100 auf 200 ändern,
    Profil unverändert (nur HMZ referenziert). versenden(...) ergibt
    GESENDET/1 Aufruf mit veraltetem Gesamtbetrag. Erwartung BLOCKIERT/0."
    Entwurf und Versand müssen an ALLE im Schreiben enthaltenen aktiven
    Komponenten gebunden sein - auch die unveränderte BK, nicht nur an
    Rechtsprofil-Hash/Empfänger."""

    from mietinkasso.indexautomatik.transport import FakeTransportadapter
    from mietinkasso.infrastructure.db.tables import VertragsKomponenteTable

    vertrag, _konto = basis_vertrag
    debitor = stammdaten_repo.get_debitor(vertrag.debitor_id)
    stammdaten_repo.upsert_debitor(id=debitor.id, name=debitor.name, email=debitor.email, adresse="Corsogasse 1/3, 1010 Wien")
    _mit_komponente(stammdaten_repo, vertrag)
    stammdaten_repo.add_komponente(
        id="K-BK", vertrag_id=vertrag.id, art="BK_VORAUSZAHLUNG", bezeichnung="Betriebskosten", betrag_cent=10_000,
        indexierbar=False, gueltig_von=date(2024, 1, 1),
    )
    _freigegebenes_wohnungsprofil(admin_ctx, bundle.rechtsprofil_service, vertrag)
    _seed_vpi(bundle.vpi_repo, jahre_werte={2023: "100", 2024: "102", 2025: "104"})

    lauf = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 9, 13), akteur="test")
    assert lauf.status == "ERHOEHUNG_ERZEUGT"
    schreiben = bundle.outbox_repo.get(lauf.erhoehungsschreiben_id)
    assert schreiben.status == "BEREIT"

    # BK ändert sich NACH Entwurfserstellung, VOR dem Versand - das
    # Rechtsprofil selbst (nur HMZ referenziert) bleibt unverändert.
    with stammdaten_repo._session_factory() as session:
        komponente = session.get(VertragsKomponenteTable, "K-BK")
        komponente.betrag_cent = 20_000
        session.commit()

    transport = FakeTransportadapter()
    ergebnis = bundle.outbox_service.versenden(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 9, 20), send_enabled=True,
        mailops_allowlist_bestaetigt=True, transport=transport,
    )
    assert ergebnis.status == "BLOCKIERT"
    assert transport.aufrufe == []
    assert bundle.outbox_repo.get(schreiben.id).status == "BLOCKIERT"


def test_monatslauf_alle_ueberspringt_fremde_gesellschaft_ohne_lauf_zu_schreiben(
    admin_ctx, ctx_factory, basis_vertrag, bundle, stammdaten_repo
):
    """Unabhängiger Review (b31-Folgereview, synthetisch mit "1 fremde
    Rückgabe + fremde Laufzeile geschrieben" reproduziert):
    monatslauf_alle fing bislang jeden Fehler (inklusive CrossTenantError)
    unter einem breiten except ab und legte dabei eine BLOCKIERT-Zeile
    für einen Vertrag außerhalb des Gesellschaftsscope des Aufrufers an.
    Jetzt wird ein solcher Vertrag VOR jedem Zugriffsversuch komplett
    übersprungen - weder gelesen noch geschrieben."""

    vertrag, _konto = basis_vertrag
    _mit_komponente(stammdaten_repo, vertrag)
    _freigegebenes_wohnungsprofil(admin_ctx, bundle.rechtsprofil_service, vertrag)
    _seed_vpi(bundle.vpi_repo, jahre_werte={2023: "100", 2024: "102", 2025: "104"})

    fremder_ctx = ctx_factory("ANDERE-GESELLSCHAFT")
    laeufe = bundle.index_service.monatslauf_alle(ctx=fremder_ctx, heute=date(2026, 9, 13), akteur="test")

    assert laeufe == []
    assert bundle.lauf_repo.get_by_periode(vertrag.id, "2026-09") is None


def test_abgelaufener_vertrag_wird_im_batch_uebersprungen(admin_ctx, basis_vertrag, bundle, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    stammdaten_repo.upsert_vertrag(
        id=vertrag.id, einheit_id=vertrag.einheit_id, debitor_id=vertrag.debitor_id,
        gesellschaft_id=vertrag.gesellschaft_id, rechtsordnung=vertrag.rechtsordnung, gueltig_von=vertrag.gueltig_von,
        gueltig_bis=date(2026, 1, 1),
    )
    laeufe = bundle.index_service.monatslauf_alle(ctx=admin_ctx, heute=date(2026, 9, 13), akteur="test")
    assert laeufe == []
