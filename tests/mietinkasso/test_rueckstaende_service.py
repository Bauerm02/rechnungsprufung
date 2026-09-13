from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from mietinkasso.domain.enums import OPTyp
from mietinkasso.mahnwesen.repository import MahnFallRepository
from mietinkasso.rueckstaende.service import (
    OffenePositionZeile,
    UnbekanntesObjektFilterError,
    berechne_rueckstandsuebersicht,
)

_HEUTE = date(2026, 9, 13)


@pytest.fixture
def mahn_fall_repo(session_factory) -> MahnFallRepository:
    return MahnFallRepository(session_factory)


def _mahnfall_anlegen(mahn_fall_repo, *, vertrag_id, gesellschaft_id, forderung_op_position_id, stufe, outbox_key):
    return mahn_fall_repo.get_or_create(
        outbox_key=outbox_key, vertrag_id=vertrag_id, gesellschaft_id=gesellschaft_id,
        forderung_op_position_id=forderung_op_position_id, forderungsumfang_hash="hash", stufe=stufe,
        policy_version=1, status="GEPLANT", betrag_cent=10_000, bank_stand_datum=_HEUTE,
    )


@pytest.fixture
def bestand(stammdaten_repo, op_service, admin_ctx):
    """Zwei erlaubte Objekte (601, 602) derselben Gesellschaft (7DI),
    eine fremde Gesellschaft (ANDERE) mit eigenem Objekt, und ein
    ausgeschlossenes Objekt (603) - deckt Scope/Ausschluss gleichzeitig
    ab. Enthält u. a. positiven Saldo, Guthaben, Teilzahlung, Storno,
    unbekannte/künftige Fälligkeit und einen historischen Vertrag mit
    Rest."""

    stammdaten_repo.upsert_gesellschaft(id="7DI", name="7D Immobilien GmbH")
    stammdaten_repo.upsert_gesellschaft(id="ANDERE", name="Andere GmbH")

    stammdaten_repo.upsert_objekt(id="601", gesellschaft_id="7DI", bezeichnung="Am Corso")
    stammdaten_repo.upsert_objekt(id="602", gesellschaft_id="7DI", bezeichnung="Fockygasse")
    # 603 wird erst NACH der Buchung ausgeschlossen (weiter unten) -
    # ein bereits ausgeschlossenes Objekt lässt gar keine OP-Buchung
    # mehr zu (siehe `pruefe_konto_nicht_ausgeschlossen`).
    stammdaten_repo.upsert_objekt(id="603", gesellschaft_id="7DI", bezeichnung="Sieben Dörfer")
    stammdaten_repo.upsert_objekt(id="900", gesellschaft_id="ANDERE", bezeichnung="Fremdobjekt")

    # 601-TOP1: SOLL 500, Zahlung 200 -> Rest 300, bekannt & überfällig.
    stammdaten_repo.upsert_einheit(id="601-TOP1", objekt_id="601", bezeichnung="Top 1", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten_repo.upsert_debitor(id="DEB-1", name="Mieter Eins")
    stammdaten_repo.upsert_vertrag(
        id="V-601-1", einheit_id="601-TOP1", debitor_id="DEB-1", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    v1 = stammdaten_repo.get_vertrag("V-601-1")
    k1 = stammdaten_repo.get_or_create_konto(vertrag=v1)
    op_service.buchen(
        ctx=admin_ctx, konto=k1, typ=OPTyp.SOLL, betrag_cent=50_000, belegdatum=date(2026, 8, 1),
        buchungsdatum=date(2026, 8, 1), faelligkeit=date(2026, 8, 5), beleg_referenz="Miete August",
        leistungsperiode="2026-08",
    )
    op_service.buchen(
        ctx=admin_ctx, konto=k1, typ=OPTyp.ZAHLUNG, betrag_cent=20_000, belegdatum=date(2026, 8, 10),
        buchungsdatum=date(2026, 8, 10), faelligkeit=None, beleg_referenz="Teilzahlung",
    )

    # 601-TOP2: Guthaben (Zahlung > Soll) - der Soll-Betrag ist dabei
    # VOLLSTÄNDIG durch die Zahlung gedeckt (rest=0), erscheint also NICHT
    # als offene Forderung, obwohl der Kontosaldo negativ (Guthaben) ist.
    stammdaten_repo.upsert_einheit(id="601-TOP2", objekt_id="601", bezeichnung="Top 2", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten_repo.upsert_debitor(id="DEB-2", name="Mieter Zwei")
    stammdaten_repo.upsert_vertrag(
        id="V-601-2", einheit_id="601-TOP2", debitor_id="DEB-2", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    v2 = stammdaten_repo.get_vertrag("V-601-2")
    k2 = stammdaten_repo.get_or_create_konto(vertrag=v2)
    op_service.buchen(
        ctx=admin_ctx, konto=k2, typ=OPTyp.SOLL, betrag_cent=30_000, belegdatum=date(2026, 9, 1),
        buchungsdatum=date(2026, 9, 1), faelligkeit=date(2026, 9, 5), beleg_referenz="Miete September",
        leistungsperiode="2026-09",
    )
    op_service.buchen(
        ctx=admin_ctx, konto=k2, typ=OPTyp.ZAHLUNG, betrag_cent=50_000, belegdatum=date(2026, 9, 2),
        buchungsdatum=date(2026, 9, 2), faelligkeit=None, beleg_referenz="Überzahlung",
    )

    # 601-TOP3: SOLL mit unbekannter Fälligkeit, dazu eine per Storno
    # entwertete zweite Buchung (darf nicht mitzählen). Demonstriert
    # zugleich die Kontosaldo-vs-Einzelposition-Abweichung: der
    # Kontosaldo "fälliger unstrittiger Rest" zählt eine unbekannte
    # Fälligkeit NICHT (bleibt 0), während dieselbe Forderung in der
    # Einzelpositionsübersicht als "Fälligkeit unbekannt" (10.000) sichtbar
    # bleibt - beide Zahlen dürfen NICHT glattgerechnet werden.
    stammdaten_repo.upsert_einheit(id="601-TOP3", objekt_id="601", bezeichnung="Top 3", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten_repo.upsert_debitor(id="DEB-3", name="Mieter Drei")
    stammdaten_repo.upsert_vertrag(
        id="V-601-3", einheit_id="601-TOP3", debitor_id="DEB-3", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    v3 = stammdaten_repo.get_vertrag("V-601-3")
    k3 = stammdaten_repo.get_or_create_konto(vertrag=v3)
    op_service.buchen(
        ctx=admin_ctx, konto=k3, typ=OPTyp.SOLL, betrag_cent=10_000, belegdatum=date(2026, 8, 1),
        buchungsdatum=date(2026, 8, 1), faelligkeit=None, beleg_referenz="Nachzahlung, Fälligkeit ungeklärt",
    )
    stornierbar = op_service.buchen(
        ctx=admin_ctx, konto=k3, typ=OPTyp.SOLL, betrag_cent=99_999, belegdatum=date(2026, 8, 2),
        buchungsdatum=date(2026, 8, 2), faelligkeit=date(2026, 8, 3), beleg_referenz="Fehlbuchung",
    )
    op_service.storniere_und_korrigiere(
        ctx=admin_ctx, konto=k3, original_id=stornierbar.id, aenderungsgrund="Fehlbuchung storniert",
    )

    # 601-TOP4: SOLL mit KÜNFTIGER (noch nicht verstrichener) Fälligkeit,
    # ohne Zahlung - "noch nicht fällig".
    stammdaten_repo.upsert_einheit(id="601-TOP4", objekt_id="601", bezeichnung="Top 4", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten_repo.upsert_debitor(id="DEB-5", name="Mieter Fünf")
    stammdaten_repo.upsert_vertrag(
        id="V-601-4", einheit_id="601-TOP4", debitor_id="DEB-5", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    v5 = stammdaten_repo.get_vertrag("V-601-4")
    k5 = stammdaten_repo.get_or_create_konto(vertrag=v5)
    op_service.buchen(
        ctx=admin_ctx, konto=k5, typ=OPTyp.SOLL, betrag_cent=40_000, belegdatum=date(2026, 9, 1),
        buchungsdatum=date(2026, 9, 1), faelligkeit=date(2026, 10, 5), beleg_referenz="Miete Oktober (voraus)",
    )

    # 602-TOP1: historischer Vertrag (bereits beendet) mit weiterhin
    # offenem Rest - darf NICHT verschwinden, nur als "historisch"
    # markiert werden.
    stammdaten_repo.upsert_einheit(id="602-TOP1", objekt_id="602", bezeichnung="Top 1", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten_repo.upsert_debitor(id="DEB-4", name="Mieter Vier (ausgezogen)")
    stammdaten_repo.upsert_vertrag(
        id="V-602-1", einheit_id="602-TOP1", debitor_id="DEB-4", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2020, 1, 1), gueltig_bis=date(2025, 12, 31),
    )
    v4 = stammdaten_repo.get_vertrag("V-602-1")
    k4 = stammdaten_repo.get_or_create_konto(vertrag=v4)
    op_service.buchen(
        ctx=admin_ctx, konto=k4, typ=OPTyp.SOLL, betrag_cent=30_000, belegdatum=date(2025, 12, 1),
        buchungsdatum=date(2025, 12, 1), faelligkeit=date(2025, 12, 5), beleg_referenz="Letzte Monatsmiete",
    )

    # 602-TOP2: Einheit ohne Vertrag/Mietkonto (Leerstand) - Bestand,
    # kein erfundener Rückstand.
    stammdaten_repo.upsert_einheit(id="602-TOP2", objekt_id="602", bezeichnung="Top 2", nutzungsstatus="LEERSTAND")

    # Objekt 603 (ausgeschlossen): Vertrag mit hohem Saldo, darf NIRGENDS
    # in Summen/Auswahl auftauchen.
    stammdaten_repo.upsert_einheit(id="603-TOP1", objekt_id="603", bezeichnung="Top 1", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten_repo.upsert_debitor(id="DEB-9", name="Mieter Ausgeschlossen")
    stammdaten_repo.upsert_vertrag(
        id="V-603-1", einheit_id="603-TOP1", debitor_id="DEB-9", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    v9 = stammdaten_repo.get_vertrag("V-603-1")
    k9 = stammdaten_repo.get_or_create_konto(vertrag=v9)
    op_service.buchen(
        ctx=admin_ctx, konto=k9, typ=OPTyp.SOLL, betrag_cent=999_999, belegdatum=date(2026, 8, 1),
        buchungsdatum=date(2026, 8, 1), faelligkeit=date(2026, 8, 1), beleg_referenz="Sollte nie zählen",
    )
    # Erst JETZT ausschließen - simuliert ein Objekt, das NACH bereits
    # gebuchten Positionen aus der Pilotphase genommen wird.
    stammdaten_repo.upsert_objekt(id="603", gesellschaft_id="7DI", bezeichnung="Sieben Dörfer", ausgeschlossen=True)

    # Fremde Gesellschaft (ANDERE): eigener Vertrag/Saldo, darf für 7DI-
    # ctx nirgends sichtbar sein.
    stammdaten_repo.upsert_einheit(id="900-TOP1", objekt_id="900", bezeichnung="Top 1", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten_repo.upsert_debitor(id="DEB-8", name="Fremdmieter")
    stammdaten_repo.upsert_vertrag(
        id="V-900-1", einheit_id="900-TOP1", debitor_id="DEB-8", gesellschaft_id="ANDERE",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    v8 = stammdaten_repo.get_vertrag("V-900-1")
    k8 = stammdaten_repo.get_or_create_konto(vertrag=v8)
    op_service.buchen(
        ctx=admin_ctx, konto=k8, typ=OPTyp.SOLL, betrag_cent=777_777, belegdatum=date(2026, 8, 1),
        buchungsdatum=date(2026, 8, 1), faelligkeit=date(2026, 8, 1), beleg_referenz="Sollte für 7DI nie zählen",
    )

    return {"v1": v1, "k1": k1, "v2": v2, "k2": k2, "v3": v3, "k3": k3, "v4": v4, "k4": k4}


def test_objekt_optionen_ohne_ausgeschlossene_und_ohne_fremde_gesellschaft(ctx_factory, bestand, stammdaten_repo, op_service, mahn_fall_repo):
    # Ein AN "7DI" GEBUNDENER ctx (nicht ADMIN) - ADMIN (gesellschaft_ids
    # =None) sieht laut Fachregel bewusst ALLE Gesellschaften und würde
    # daher auch "900" sehen; die Scope-Ausgrenzung greift erst bei einem
    # tatsächlich gebundenen ctx.
    scoped_ctx = ctx_factory("7DI")
    uebersicht = berechne_rueckstandsuebersicht(
        ctx=scoped_ctx, objekt_id=None, stammdaten_repository=stammdaten_repo, op_service=op_service,
        mahn_fall_repository=mahn_fall_repo, heute=_HEUTE,
    )
    ids = {o.id for o in uebersicht.objekt_optionen}
    assert ids == {"601", "602"}  # NICHT 603 (ausgeschlossen), NICHT 900 (fremde Gesellschaft)


def test_alle_objekte_summen_schliessen_ausgeschlossenes_und_fremdes_objekt_aus(
    ctx_factory, bestand, stammdaten_repo, op_service, mahn_fall_repo
):
    scoped_ctx = ctx_factory("7DI")
    uebersicht = berechne_rueckstandsuebersicht(
        ctx=scoped_ctx, objekt_id=None, stammdaten_repository=stammdaten_repo, op_service=op_service,
        mahn_fall_repository=mahn_fall_repo, heute=_HEUTE,
    )
    objekt_ids_in_mietkonten = {z.objekt_id for z in uebersicht.mietkonten}
    assert "603" not in objekt_ids_in_mietkonten
    assert "900" not in objekt_ids_in_mietkonten
    assert not any(p.objekt_id in ("603", "900") for p in uebersicht.offene_positionen)
    # NUR 601 (80.000) + 602 (30.000, historisch) dürfen in die Kennzahlen
    # einfließen - NICHT die 9.999,99 EUR von Objekt 603 bzw. die
    # 7.777,77 EUR von Objekt 900 (fremde Gesellschaft).
    assert uebersicht.kennzahlen.summe_positiver_kontostaende_cent == 110_000
    assert uebersicht.kennzahlen.ueberfaellig_cent == 60_000


def test_positiver_saldo_und_guthaben_werden_nicht_gegeneinander_verrechnet(
    admin_ctx, bestand, stammdaten_repo, op_service, mahn_fall_repo
):
    """601-TOP2 hat ein Guthaben von 200 EUR (Überzahlung, SOLL dabei
    vollständig gedeckt) - die Summe positiver Kontostände (aus den
    ANDEREN drei Verträgen des Objekts) und die separate Guthabensumme
    müssen UNABHÄNGIG voneinander korrekt bleiben, NIE gegeneinander
    aufgerechnet."""

    uebersicht = berechne_rueckstandsuebersicht(
        ctx=admin_ctx, objekt_id="601", stammdaten_repository=stammdaten_repo, op_service=op_service,
        mahn_fall_repository=mahn_fall_repo, heute=_HEUTE,
    )
    assert uebersicht.kennzahlen.summe_positiver_kontostaende_cent == 80_000
    assert uebersicht.kennzahlen.summe_guthaben_cent == 20_000


def test_faelligkeitsklassen_ueberfaellig_kuenftig_unbekannt(admin_ctx, bestand, stammdaten_repo, op_service, mahn_fall_repo):
    uebersicht = berechne_rueckstandsuebersicht(
        ctx=admin_ctx, objekt_id="601", stammdaten_repository=stammdaten_repo, op_service=op_service,
        mahn_fall_repository=mahn_fall_repo, heute=_HEUTE,
    )
    assert uebersicht.kennzahlen.ueberfaellig_cent == 30_000  # 601-TOP1 Rest, Fälligkeit 05.08. bereits verstrichen
    assert uebersicht.kennzahlen.nicht_faellig_cent == 40_000  # 601-TOP4 SOLL, Fälligkeit 05.10. noch nicht da
    assert uebersicht.kennzahlen.faelligkeit_unbekannt_cent == 10_000  # 601-TOP3 SOLL ohne Fälligkeit
    # 601-TOP2s SOLL ist vollständig durch die Zahlung gedeckt (rest=0)
    # und erscheint daher gar NICHT als offene Position.
    assert not any(p.vertrag_id == "V-601-2" for p in uebersicht.offene_positionen)
    klassen = {z.op_position_id: z.faelligkeitsklasse for z in uebersicht.offene_positionen}
    assert set(klassen.values()) == {"UEBERFAELLIG", "NICHT_FAELLIG", "UNBEKANNT"}


def test_offene_position_zeigt_op_id_und_belegreferenz(admin_ctx, bestand, stammdaten_repo, op_service, mahn_fall_repo):
    """Unabhängiger Review: 'Derzeit nur Belegdatum, damit kann man
    ähnliche Forderungen nicht zuordnen.' - jede Einzelposition muss die
    OP-ID UND die tatsächliche Belegreferenz der zugrunde liegenden
    OP-Zeile tragen."""

    uebersicht = berechne_rueckstandsuebersicht(
        ctx=admin_ctx, objekt_id="601", stammdaten_repository=stammdaten_repo, op_service=op_service,
        mahn_fall_repository=mahn_fall_repo, heute=_HEUTE,
    )
    v1_position = next(p for p in uebersicht.offene_positionen if p.vertrag_id == "V-601-1")
    assert v1_position.op_position_id > 0
    assert v1_position.beleg_referenz == "Miete August"
    original = op_service.get_position(v1_position.op_position_id)
    assert original is not None and original.beleg_referenz == v1_position.beleg_referenz


def test_kontostand_ohne_entsprechende_einzelposition_zeigt_abweichung(
    admin_ctx, stammdaten_repo, op_service, mahn_fall_repo
):
    """Unabhängiger Review, exakt reproduziert: eine positive KORREKTUR-
    Buchung von 777 Cent erhöht den Kontostand auf 7,77 EUR, erzeugt aber
    KEINE eigene offene Einzelposition (KORREKTUR ist keine Forderungsart
    in `OPService.offene_forderungen`) - die Abweichung (7,77 EUR) muss
    explizit an der Mietkonto-Zeile ausgewiesen werden, statt beide Werte
    fälschlich gleichzusetzen."""

    stammdaten_repo.upsert_gesellschaft(id="7DI", name="7D Immobilien GmbH")
    stammdaten_repo.upsert_objekt(id="601", gesellschaft_id="7DI", bezeichnung="Am Corso")
    stammdaten_repo.upsert_einheit(id="601-TOP1", objekt_id="601", bezeichnung="Top 1", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten_repo.upsert_debitor(id="DEB-1", name="Mieter Eins")
    stammdaten_repo.upsert_vertrag(
        id="V-601-1", einheit_id="601-TOP1", debitor_id="DEB-1", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    vertrag = stammdaten_repo.get_vertrag("V-601-1")
    konto = stammdaten_repo.get_or_create_konto(vertrag=vertrag)
    op_service.buchen(
        ctx=admin_ctx, konto=konto, typ=OPTyp.KORREKTUR, betrag_cent=777, belegdatum=date(2026, 8, 1),
        buchungsdatum=date(2026, 8, 1), faelligkeit=None, beleg_referenz="Manuelle Korrektur ohne Forderung",
    )
    uebersicht = berechne_rueckstandsuebersicht(
        ctx=admin_ctx, objekt_id="601", stammdaten_repository=stammdaten_repo, op_service=op_service,
        mahn_fall_repository=mahn_fall_repo, heute=_HEUTE,
    )
    zeile = uebersicht.mietkonten[0]
    assert zeile.saldo_cent == 777
    assert zeile.positionen_rest_gesamt_cent == 0
    assert zeile.abweichung_saldo_zu_positionen_cent == 777
    assert not any(p.vertrag_id == "V-601-1" for p in uebersicht.offene_positionen)


def test_kontosaldo_und_einzelposition_koennen_bewusst_abweichen(
    admin_ctx, bestand, stammdaten_repo, op_service, mahn_fall_repo
):
    """`OPSaldo.faelliger_unstrittiger_rest_cent` zählt eine Forderung
    mit UNBEKANNTER Fälligkeit nicht (bleibt 0 für V-601-3), während
    dieselbe Forderung in der Einzelpositionsübersicht ALS "Fälligkeit
    unbekannt" (10.000) sichtbar bleibt - beide Werte dürfen NICHT
    glattgerechnet werden, die Abweichung muss sichtbar bleiben."""

    uebersicht = berechne_rueckstandsuebersicht(
        ctx=admin_ctx, objekt_id="601", stammdaten_repository=stammdaten_repo, op_service=op_service,
        mahn_fall_repository=mahn_fall_repo, heute=_HEUTE,
    )
    v3_zeile = next(z for z in uebersicht.mietkonten if z.vertrag_id == "V-601-3")
    assert v3_zeile.saldo_cent == 10_000
    assert v3_zeile.faelliger_unstrittiger_rest_cent == 0  # Konto-Sicht: nicht mitgezählt (Fälligkeit unbekannt)
    v3_position = next(p for p in uebersicht.offene_positionen if p.vertrag_id == "V-601-3")
    assert v3_position.rest_cent == 10_000  # Positions-Sicht: sehr wohl als offene Position sichtbar
    assert v3_position.faelligkeitsklasse == "UNBEKANNT"


def test_stornierte_position_zaehlt_nirgends(admin_ctx, bestand, stammdaten_repo, op_service, mahn_fall_repo):
    uebersicht = berechne_rueckstandsuebersicht(
        ctx=admin_ctx, objekt_id="601", stammdaten_repository=stammdaten_repo, op_service=op_service,
        mahn_fall_repository=mahn_fall_repo, heute=_HEUTE,
    )
    betraege = {z.betrag_cent for z in uebersicht.offene_positionen}
    assert 99_999 * 100 not in betraege  # die stornierte Fehlbuchung


def test_historischer_vertrag_mit_rest_bleibt_sichtbar_und_wird_gezaehlt(
    admin_ctx, bestand, stammdaten_repo, op_service, mahn_fall_repo
):
    uebersicht = berechne_rueckstandsuebersicht(
        ctx=admin_ctx, objekt_id="602", stammdaten_repository=stammdaten_repo, op_service=op_service,
        mahn_fall_repository=mahn_fall_repo, heute=_HEUTE,
    )
    zeile = next(z for z in uebersicht.mietkonten if z.vertrag_id == "V-602-1")
    assert zeile.historisch is True
    assert zeile.saldo_cent == 30_000
    assert any(p.vertrag_id == "V-602-1" for p in uebersicht.offene_positionen)


def test_leerstand_ohne_vertrag_ist_bestand_kein_erfundener_rueckstand(
    admin_ctx, bestand, stammdaten_repo, op_service, mahn_fall_repo
):
    uebersicht = berechne_rueckstandsuebersicht(
        ctx=admin_ctx, objekt_id="602", stammdaten_repository=stammdaten_repo, op_service=op_service,
        mahn_fall_repository=mahn_fall_repo, heute=_HEUTE,
    )
    assert any(e.einheit_id == "602-TOP2" for e in uebersicht.einheiten_ohne_konto)
    assert not any(z.einheit_id == "602-TOP2" for z in uebersicht.mietkonten)


def test_leeres_objekt_ohne_absturz(admin_ctx, stammdaten_repo, op_service, mahn_fall_repo):
    stammdaten_repo.upsert_gesellschaft(id="7DI", name="7D Immobilien GmbH")
    stammdaten_repo.upsert_objekt(id="601", gesellschaft_id="7DI", bezeichnung="Am Corso")
    uebersicht = berechne_rueckstandsuebersicht(
        ctx=admin_ctx, objekt_id="601", stammdaten_repository=stammdaten_repo, op_service=op_service,
        mahn_fall_repository=mahn_fall_repo, heute=_HEUTE,
    )
    assert uebersicht.mietkonten == ()
    assert uebersicht.offene_positionen == ()
    assert uebersicht.einheiten_ohne_konto == ()
    assert uebersicht.kennzahlen.summe_positiver_kontostaende_cent == 0


def test_vertrag_ohne_konto_erscheint_ohne_saldo_nicht_als_absturz(admin_ctx, stammdaten_repo, op_service, mahn_fall_repo):
    stammdaten_repo.upsert_gesellschaft(id="7DI", name="7D Immobilien GmbH")
    stammdaten_repo.upsert_objekt(id="601", gesellschaft_id="7DI", bezeichnung="Am Corso")
    stammdaten_repo.upsert_einheit(id="601-TOP1", objekt_id="601", bezeichnung="Top 1", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten_repo.upsert_debitor(id="DEB-1", name="Mieter")
    stammdaten_repo.upsert_vertrag(
        id="V-601-1", einheit_id="601-TOP1", debitor_id="DEB-1", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    # Bewusst KEIN get_or_create_konto() - simuliert eine Lücke, in der
    # noch kein Konto angelegt wurde.
    uebersicht = berechne_rueckstandsuebersicht(
        ctx=admin_ctx, objekt_id="601", stammdaten_repository=stammdaten_repo, op_service=op_service,
        mahn_fall_repository=mahn_fall_repo, heute=_HEUTE,
    )
    zeile = uebersicht.mietkonten[0]
    assert zeile.konto_id is None
    assert zeile.saldo_cent is None


def test_mahnsperre_sichtbar(admin_ctx, bestand, stammdaten_repo, op_service, mahn_fall_repo):
    stammdaten_repo.sperre_setzen(vertrag_id="V-601-3", grund="RATENPLAN", kommentar="Ratenplan vereinbart")
    uebersicht = berechne_rueckstandsuebersicht(
        ctx=admin_ctx, objekt_id="601", stammdaten_repository=stammdaten_repo, op_service=op_service,
        mahn_fall_repository=mahn_fall_repo, heute=_HEUTE,
    )
    v3_zeile = next(z for z in uebersicht.mietkonten if z.vertrag_id == "V-601-3")
    assert "RATENPLAN" in v3_zeile.sperrgruende


def test_alle_mahnfaelle_je_vertrag_sichtbar_nicht_nur_der_neueste(
    admin_ctx, bestand, stammdaten_repo, op_service, mahn_fall_repo
):
    """Unabhängiger Review: 'nur mahnfaelle[0] pro Vertrag blendet andere
    Forderungen/Stufen aus.' - Stufe 1 UND Stufe 2 (unterschiedliche
    Forderungen) müssen BEIDE in der dedizierten Mahnfall-Übersicht
    auftauchen, nicht nur die zuletzt angelegte Zeile."""

    _mahnfall_anlegen(
        mahn_fall_repo, vertrag_id="V-601-1", gesellschaft_id="7DI", forderung_op_position_id=1,
        stufe=1, outbox_key="MF-1",
    )
    _mahnfall_anlegen(
        mahn_fall_repo, vertrag_id="V-601-1", gesellschaft_id="7DI", forderung_op_position_id=2,
        stufe=2, outbox_key="MF-2",
    )
    uebersicht = berechne_rueckstandsuebersicht(
        ctx=admin_ctx, objekt_id="601", stammdaten_repository=stammdaten_repo, op_service=op_service,
        mahn_fall_repository=mahn_fall_repo, heute=_HEUTE,
    )
    faelle_v1 = [m for m in uebersicht.mahnfaelle if m.vertrag_id == "V-601-1"]
    assert {(m.stufe, m.forderung_op_position_id) for m in faelle_v1} == {(1, 1), (2, 2)}
    v1_zeile = next(z for z in uebersicht.mietkonten if z.vertrag_id == "V-601-1")
    assert v1_zeile.mahnfaelle_anzahl == 2
    # Der ursprüngliche Fallbetrag ist rein informativ und darf NIRGENDS
    # in die OP-Kennzahlen einfließen - V-601-1s Rest bleibt exakt 300 EUR
    # (überfällig), obwohl ZWEI Mahnfälle à 100 EUR dafür existieren.
    assert all(m.betrag_cent == 10_000 for m in faelle_v1)
    assert uebersicht.kennzahlen.ueberfaellig_cent == 30_000


def test_bekannte_faelligkeit_ist_keine_mahnfreigabe_und_unbekannte_nicht_automatisch_strittig():
    """Reine Datenmodell-Kontrolle: `OffenePositionZeile` liefert nur
    `faelligkeit`/`faelligkeit_bekannt`/`faelligkeitsklasse` - es gibt
    KEIN eigenes 'mahnfrei'/'strittig'-Feld, das eine bekannte
    Fälligkeit fälschlich als Mahnfreigabe oder eine unbekannte als
    automatisch strittig behaupten könnte (keine Namens-/
    Saldoheuristik für Strittigkeit)."""

    feldnamen = {f.name for f in dataclasses.fields(OffenePositionZeile)}
    assert "mahnfreigabe" not in feldnamen
    assert "strittig" not in feldnamen
    assert "mahnfrei" not in feldnamen


def test_filterwirkung_identisch_zu_teilmenge_von_alle_objekte(admin_ctx, bestand, stammdaten_repo, op_service, mahn_fall_repo):
    alle = berechne_rueckstandsuebersicht(
        ctx=admin_ctx, objekt_id=None, stammdaten_repository=stammdaten_repo, op_service=op_service,
        mahn_fall_repository=mahn_fall_repo, heute=_HEUTE,
    )
    gefiltert = berechne_rueckstandsuebersicht(
        ctx=admin_ctx, objekt_id="601", stammdaten_repository=stammdaten_repo, op_service=op_service,
        mahn_fall_repository=mahn_fall_repo, heute=_HEUTE,
    )
    erwartete_mietkonten = [z for z in alle.mietkonten if z.objekt_id == "601"]
    assert [z.vertrag_id for z in gefiltert.mietkonten] == [z.vertrag_id for z in erwartete_mietkonten]
    erwartete_positionen = [p for p in alle.offene_positionen if p.objekt_id == "601"]
    assert {p.op_position_id for p in gefiltert.offene_positionen} == {p.op_position_id for p in erwartete_positionen}
    erwartete_summe = sum(max(z.saldo_cent, 0) for z in erwartete_mietkonten if z.saldo_cent is not None)
    assert gefiltert.kennzahlen.summe_positiver_kontostaende_cent == erwartete_summe


def test_fremdes_objekt_wird_abgelehnt_ohne_offenlegung(ctx_factory, bestand, stammdaten_repo, op_service, mahn_fall_repo):
    scoped_ctx = ctx_factory("7DI")
    with pytest.raises(UnbekanntesObjektFilterError):
        berechne_rueckstandsuebersicht(
            ctx=scoped_ctx, objekt_id="900", stammdaten_repository=stammdaten_repo, op_service=op_service,
            mahn_fall_repository=mahn_fall_repo, heute=_HEUTE,
        )


def test_ausgeschlossenes_objekt_wird_abgelehnt(admin_ctx, bestand, stammdaten_repo, op_service, mahn_fall_repo):
    with pytest.raises(UnbekanntesObjektFilterError):
        berechne_rueckstandsuebersicht(
            ctx=admin_ctx, objekt_id="603", stammdaten_repository=stammdaten_repo, op_service=op_service,
            mahn_fall_repository=mahn_fall_repo, heute=_HEUTE,
        )


def test_unbekanntes_objekt_wird_abgelehnt(admin_ctx, bestand, stammdaten_repo, op_service, mahn_fall_repo):
    with pytest.raises(UnbekanntesObjektFilterError):
        berechne_rueckstandsuebersicht(
            ctx=admin_ctx, objekt_id="NICHT-VORHANDEN", stammdaten_repository=stammdaten_repo, op_service=op_service,
            mahn_fall_repository=mahn_fall_repo, heute=_HEUTE,
        )


def test_gleiche_fehlermeldung_fuer_fremd_ausgeschlossen_unbekannt(ctx_factory, bestand, stammdaten_repo, op_service, mahn_fall_repo):
    """Kein Erkenntnisgewinn für den Aufrufer, welcher der drei Fälle
    vorliegt - alle drei erzeugen denselben Fehlertyp mit derselben
    Meldungsstruktur."""

    scoped_ctx = ctx_factory("7DI")
    nachrichten = []
    for objekt_id in ("900", "603", "NICHT-VORHANDEN"):
        with pytest.raises(UnbekanntesObjektFilterError) as exc_info:
            berechne_rueckstandsuebersicht(
                ctx=scoped_ctx, objekt_id=objekt_id, stammdaten_repository=stammdaten_repo, op_service=op_service,
                mahn_fall_repository=mahn_fall_repo, heute=_HEUTE,
            )
        nachrichten.append(str(exc_info.value).split(objekt_id)[0])
    assert len(set(nachrichten)) == 1


def test_fremder_ctx_sieht_gar_nichts(ctx_factory, bestand, stammdaten_repo, op_service, mahn_fall_repo):
    fremder_ctx = ctx_factory("ANDERE")
    uebersicht = berechne_rueckstandsuebersicht(
        ctx=fremder_ctx, objekt_id=None, stammdaten_repository=stammdaten_repo, op_service=op_service,
        mahn_fall_repository=mahn_fall_repo, heute=_HEUTE,
    )
    assert {o.id for o in uebersicht.objekt_optionen} == {"900"}
    assert uebersicht.mietkonten and all(z.objekt_id == "900" for z in uebersicht.mietkonten)
    assert not any(z.objekt_id in ("601", "602") for z in uebersicht.mietkonten)


def test_inkonsistenter_vertrag_unter_erlaubtem_objekt_wird_ausgeblendet(
    ctx_factory, stammdaten_repo, op_service, mahn_fall_repo
):
    """Unabhängiger Review: 'fremde Gesellschaft an Vertrag oder Konto
    unter sonst erlaubtem Objekt darf keine Daten durchlassen.' -
    `list_vertraege_fuer_objekt` filtert NUR über Einheit->Objekt, nie
    über das eigene `gesellschaft_id`-Feld des Vertrags. Ein Vertrag
    (und das davon abgeleitete Konto), der unter einer erlaubten
    Gesellschaft/Objekt hängt, aber selbst `gesellschaft_id='ANDERE'`
    trägt (Dateninkonsistenz), darf für einen auf '7DI' beschränkten ctx
    trotzdem NICHT sichtbar werden."""

    stammdaten_repo.upsert_gesellschaft(id="7DI", name="7D Immobilien GmbH")
    stammdaten_repo.upsert_gesellschaft(id="ANDERE", name="Andere GmbH")
    stammdaten_repo.upsert_objekt(id="601", gesellschaft_id="7DI", bezeichnung="Am Corso")
    stammdaten_repo.upsert_einheit(id="601-TOP1", objekt_id="601", bezeichnung="Top 1", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten_repo.upsert_debitor(id="DEB-1", name="Inkonsistenter Mieter")
    # Einheit gehört zu Objekt 601 (Gesellschaft 7DI), der Vertrag TRÄGT
    # ABER selbst gesellschaft_id="ANDERE" - eine Dateninkonsistenz, die
    # `upsert_vertrag` technisch zulässt (keine Querprüfung).
    stammdaten_repo.upsert_vertrag(
        id="V-601-INKONSISTENT", einheit_id="601-TOP1", debitor_id="DEB-1", gesellschaft_id="ANDERE",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    vertrag = stammdaten_repo.get_vertrag("V-601-INKONSISTENT")
    konto = stammdaten_repo.get_or_create_konto(vertrag=vertrag)
    ctx_admin_fuer_buchung = ctx_factory("ANDERE")  # Buchung selbst braucht Zugriff auf konto.gesellschaft_id
    op_service.buchen(
        ctx=ctx_admin_fuer_buchung, konto=konto, typ=OPTyp.SOLL, betrag_cent=123_456, belegdatum=date(2026, 8, 1),
        buchungsdatum=date(2026, 8, 1), faelligkeit=date(2026, 8, 1), beleg_referenz="Darf für 7DI nie sichtbar sein",
    )

    scoped_ctx = ctx_factory("7DI")
    uebersicht = berechne_rueckstandsuebersicht(
        ctx=scoped_ctx, objekt_id="601", stammdaten_repository=stammdaten_repo, op_service=op_service,
        mahn_fall_repository=mahn_fall_repo, heute=_HEUTE,
    )
    assert not any(z.vertrag_id == "V-601-INKONSISTENT" for z in uebersicht.mietkonten)
    assert not any(p.vertrag_id == "V-601-INKONSISTENT" for p in uebersicht.offene_positionen)
    # Die Einheit gilt trotzdem als "belegt" - sie darf NICHT fälschlich
    # als "ohne Mietkonto" auftauchen, nur weil ihr (inkonsistenter)
    # Vertrag ausgeblendet wurde.
    assert not any(e.einheit_id == "601-TOP1" for e in uebersicht.einheiten_ohne_konto)
    # Auch über "Alle Objekte" (kein expliziter Filter) bleibt sie unsichtbar.
    alle = berechne_rueckstandsuebersicht(
        ctx=scoped_ctx, objekt_id=None, stammdaten_repository=stammdaten_repo, op_service=op_service,
        mahn_fall_repository=mahn_fall_repo, heute=_HEUTE,
    )
    assert not any(z.vertrag_id == "V-601-INKONSISTENT" for z in alle.mietkonten)
    assert alle.kennzahlen.summe_positiver_kontostaende_cent == 0  # NICHT 123.456 EUR


def test_uebersicht_ist_vollstaendig_schreibfrei(admin_ctx, bestand, stammdaten_repo, op_service, mahn_fall_repo):
    vorher_op = len(op_service.list_alle_positionen(bestand["k1"].id))
    vorher_mahn = len(mahn_fall_repo.list_fuer_vertrag("V-601-1"))
    for _ in range(3):
        berechne_rueckstandsuebersicht(
            ctx=admin_ctx, objekt_id=None, stammdaten_repository=stammdaten_repo, op_service=op_service,
            mahn_fall_repository=mahn_fall_repo, heute=_HEUTE,
        )
    assert len(op_service.list_alle_positionen(bestand["k1"].id)) == vorher_op
    assert len(mahn_fall_repo.list_fuer_vertrag("V-601-1")) == vorher_mahn
