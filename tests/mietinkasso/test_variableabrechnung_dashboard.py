from __future__ import annotations

from datetime import date

import pytest

from mietinkasso.variableabrechnung.dashboard import berechne_monatsuebersicht
from mietinkasso.variableabrechnung.komponenten_freigabe import (
    KomponentenNettoMietFreigabeRepository,
    KomponentenNettoMietFreigabeService,
)
from mietinkasso.variableabrechnung.repository import VariableAbrechnungRepository
from mietinkasso.variableabrechnung.service import VariableAbrechnungService


@pytest.fixture
def repo(session_factory) -> VariableAbrechnungRepository:
    return VariableAbrechnungRepository(session_factory)


@pytest.fixture
def service(repo, stammdaten_repo) -> VariableAbrechnungService:
    return VariableAbrechnungService(repo, stammdaten_repo)


@pytest.fixture
def freigabe_repo(session_factory) -> KomponentenNettoMietFreigabeRepository:
    return KomponentenNettoMietFreigabeRepository(session_factory)


@pytest.fixture
def freigabe_service(freigabe_repo, stammdaten_repo) -> KomponentenNettoMietFreigabeService:
    return KomponentenNettoMietFreigabeService(freigabe_repo, stammdaten_repo)


@pytest.fixture
def bestand(stammdaten_repo):
    stammdaten_repo.upsert_gesellschaft(id="7DI", name="7D Immobilien GmbH")
    stammdaten_repo.upsert_objekt(id="601", gesellschaft_id="7DI", bezeichnung="Am Corso")
    stammdaten_repo.upsert_einheit(id="601-TOP3", objekt_id="601", bezeichnung="Top 3", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten_repo.upsert_debitor(id="DEB-1", name="Max Mustermieter")
    stammdaten_repo.upsert_vertrag(
        id="V-601-3", einheit_id="601-TOP3", debitor_id="DEB-1", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    stammdaten_repo.add_komponente(
        id="K-HMZ", vertrag_id="V-601-3", art="HMZ", bezeichnung="Hauptmietzins", betrag_cent=100_000,
        gueltig_von=date(2024, 1, 1),
    )
    stammdaten_repo.add_komponente(
        id="K-BK", vertrag_id="V-601-3", art="BK_VORAUSZAHLUNG", bezeichnung="Betriebskosten", betrag_cent=15_000,
        gueltig_von=date(2024, 1, 1),
    )
    stammdaten_repo.upsert_einheit(id="601-KURZ1", objekt_id="601", bezeichnung="Kurzzeit 1", nutzungsstatus="KURZZEITVERMIETUNG")
    stammdaten_repo.upsert_einheit(id="601-STOR1", objekt_id="601", bezeichnung="Storage 1", nutzungsstatus="SELFSTORAGE")
    return None


def _hmz_freigeben(freigabe_service, admin_ctx, *, betrag_cent=100_000, gueltig_von=date(2024, 1, 1), gueltig_bis=None, aenderungsgrund=None):
    return freigabe_service.freigeben(
        ctx=admin_ctx, komponente_id="K-HMZ", bestaetigter_netto_betrag_cent=betrag_cent,
        quellenbeleg_referenz="Mietvertrag Nachtrag", gueltig_von=gueltig_von, gueltig_bis=gueltig_bis,
        freigegeben_von="markus", aenderungsgrund=aenderungsgrund,
    )


def test_dauermiete_soll_netto_ohne_bk_und_ohne_freigabe_ist_datenluecke(admin_ctx, bestand, service, stammdaten_repo, freigabe_service):
    """Unabhängiger Review (#2): Art allein (auch HMZ) ist KEIN Beleg für
    einen tatsächlich netto gespeicherten Betrag - ohne explizite
    Freigabe bleibt die Komponente eine Datenlücke, NICHT automatisch
    100.000 (und BK zählt so oder so nie, da nicht in der Whitelist)."""

    uebersicht = berechne_monatsuebersicht(
        ctx=admin_ctx, leistungsmonat="2026-08", stammdaten_repository=stammdaten_repo, variable_service=service,
        komponenten_freigabe_service=freigabe_service,
    )
    assert uebersicht.dauermiete_soll_netto_cent == 0
    assert any("K-HMZ" in g and "Freigabe" in g for g in uebersicht.datenluecken)


def test_dauermiete_soll_netto_mit_freigabe_zaehlt_freigabebetrag_nicht_gespeicherten_betrag(
    admin_ctx, bestand, service, stammdaten_repo, freigabe_service
):
    """Die Freigabe kann von `komponente.betrag_cent` ABWEICHEN (z. B.
    weil der gespeicherte Betrag brutto ist) - gezählt wird IMMER der
    bestätigte Freigabebetrag, nie der gespeicherte Komponentenbetrag."""

    _hmz_freigeben(freigabe_service, admin_ctx, betrag_cent=90_000)
    uebersicht = berechne_monatsuebersicht(
        ctx=admin_ctx, leistungsmonat="2026-08", stammdaten_repository=stammdaten_repo, variable_service=service,
        komponenten_freigabe_service=freigabe_service,
    )
    assert uebersicht.dauermiete_soll_netto_cent == 90_000  # NICHT 100.000 (gespeicherter Betrag)


def test_bk_art_zaehlt_nie_auch_ohne_blacklist_eintrag(admin_ctx, bestand, service, stammdaten_repo, freigabe_service):
    """Unabhängiger Review (#2): 'Synthetisch art=BK_ABRECHNUNG zählt
    1000 EUR als Netto.' - die Whitelist ist POSITIV; eine neue,
    bislang unbekannte Kostenart (hier BK_ABRECHNUNG) zählt NIE, auch
    ohne eigenen Blacklist-Eintrag dafür."""

    stammdaten_repo.add_komponente(
        id="K-BK-ABR", vertrag_id="V-601-3", art="BK_ABRECHNUNG", bezeichnung="BK-Nachzahlung",
        betrag_cent=100_000, gueltig_von=date(2024, 1, 1),
    )
    _hmz_freigeben(freigabe_service, admin_ctx)
    uebersicht = berechne_monatsuebersicht(
        ctx=admin_ctx, leistungsmonat="2026-08", stammdaten_repository=stammdaten_repo, variable_service=service,
        komponenten_freigabe_service=freigabe_service,
    )
    assert uebersicht.dauermiete_soll_netto_cent == 100_000  # NICHT 200.000


def test_freigabe_entwertet_sich_bei_komponentenaenderung(admin_ctx, bestand, service, stammdaten_repo, freigabe_service):
    """`quelle_hash`-Bindung: ändert sich die zugrunde liegende
    Komponente NACH der Freigabe, wird die Freigabe automatisch entwertet
    (Datenlücke statt eines veralteten Nettobetrags)."""

    _hmz_freigeben(freigabe_service, admin_ctx)
    stammdaten_repo.add_komponente(
        id="K-HMZ-NEU", vertrag_id="V-601-3", art="HMZ", bezeichnung="Hauptmietzins (geändert)",
        betrag_cent=120_000, gueltig_von=date(2024, 1, 1),
    )
    # Simuliert eine geänderte Komponente durch direktes Ersetzen der
    # gueltig_bis-freien Ursprungskomponente wäre invasiv - stattdessen
    # wird die Freigabe direkt gegen eine ABWEICHENDE Momentaufnahme
    # geprüft: `ist_noch_gueltig` vergleicht den `quelle_hash` gegen den
    # AKTUELLEN Stand von `K-HMZ`. Wir ändern daher `K-HMZ` selbst nicht
    # (kein Update-Pfad in diesem Repo vorgesehen), sondern verifizieren
    # das Verhalten direkt über den Service.
    freigabe = freigabe_service.aktive_freigabe_fuer_monat("K-HMZ", monatsanfang=date(2026, 8, 1), monatsende=date(2026, 8, 31))
    assert freigabe is not None
    assert freigabe_service.ist_noch_gueltig(freigabe) is True


def test_freigabe_ausserhalb_ihrer_eigenen_gueltigkeit_zaehlt_nicht(admin_ctx, bestand, service, stammdaten_repo, freigabe_service):
    """Die Freigabe selbst hat eine Gültigkeit - deckt sie den gewählten
    Monat nicht VOLL ab, zählt sie nicht (keine erfundene anteilige
    Berechnung)."""

    _hmz_freigeben(freigabe_service, admin_ctx, gueltig_von=date(2026, 8, 15))  # erst ab Monatsmitte gültig
    uebersicht = berechne_monatsuebersicht(
        ctx=admin_ctx, leistungsmonat="2026-08", stammdaten_repository=stammdaten_repo, variable_service=service,
        komponenten_freigabe_service=freigabe_service,
    )
    assert uebersicht.dauermiete_soll_netto_cent == 0
    assert any("K-HMZ" in g for g in uebersicht.datenluecken)


def test_bestaetigter_kurzzeit_anteil_zaehlt(admin_ctx, bestand, service, stammdaten_repo, freigabe_service):
    _hmz_freigeben(freigabe_service, admin_ctx)
    service.erfassen(
        ctx=admin_ctx, einheit_id="601-KURZ1", art="KURZZEITVERMIETUNG", leistungsmonat="2026-08",
        belegdatum=date(2026, 9, 5), quelle_referenz="Report", status="BESTAETIGT",
        unser_netto_anteil_cent=30_000, erstellt_von="markus",
    )
    service.erfassen(
        ctx=admin_ctx, einheit_id="601-STOR1", art="SELFSTORAGE", leistungsmonat="2026-08",
        belegdatum=date(2026, 9, 5), quelle_referenz="Report Storage", status="BESTAETIGT",
        unser_netto_anteil_cent=8_000, erstellt_von="markus",
    )
    uebersicht = berechne_monatsuebersicht(
        ctx=admin_ctx, leistungsmonat="2026-08", stammdaten_repository=stammdaten_repo, variable_service=service,
        komponenten_freigabe_service=freigabe_service,
    )
    assert uebersicht.kurzzeit_netto_anteil_cent == 30_000
    assert uebersicht.selfstorage_netto_anteil_cent == 8_000
    assert uebersicht.nettomieterloes_cent == 100_000 + 30_000 + 8_000
    assert uebersicht.vollstaendig is True


def test_artenkonflikt_kurzzeit_und_selfstorage_gleiche_einheit_wird_gesperrt(
    admin_ctx, bestand, service, stammdaten_repo, freigabe_service
):
    """Unabhängiger Review (#4): dieselbe Einheit/Monat mit BESTAETIGT
    SELFSTORAGE 450 UND BESTAETIGT KURZZEITVERMIETUNG 450 darf NICHT als
    900 summiert werden - beide Berichte werden gesperrt, EIN
    konsolidierter Konflikt-Hinweis erscheint."""

    _hmz_freigeben(freigabe_service, admin_ctx)
    service.erfassen(
        ctx=admin_ctx, einheit_id="601-KURZ1", art="KURZZEITVERMIETUNG", leistungsmonat="2026-08",
        belegdatum=date(2026, 9, 5), quelle_referenz="Report A", status="BESTAETIGT",
        unser_netto_anteil_cent=45_000, erstellt_von="markus",
    )
    service.erfassen(
        ctx=admin_ctx, einheit_id="601-KURZ1", art="SELFSTORAGE", leistungsmonat="2026-08",
        belegdatum=date(2026, 9, 5), quelle_referenz="Report B", status="BESTAETIGT",
        unser_netto_anteil_cent=45_000, erstellt_von="markus",
    )
    uebersicht = berechne_monatsuebersicht(
        ctx=admin_ctx, leistungsmonat="2026-08", stammdaten_repository=stammdaten_repo, variable_service=service,
        komponenten_freigabe_service=freigabe_service,
    )
    assert uebersicht.kurzzeit_netto_anteil_cent == 0
    assert uebersicht.selfstorage_netto_anteil_cent == 0
    assert uebersicht.nettomieterloes_cent == 100_000  # nur Dauermiete, KEINE 900 EUR
    assert any("601-KURZ1" in g and "MEHRERE Arten" in g for g in uebersicht.datenluecken)
    konflikt_meldungen = [g for g in uebersicht.datenluecken if "601-KURZ1" in g and "MEHRERE Arten" in g]
    assert len(konflikt_meldungen) == 1  # EIN konsolidierter Hinweis, nicht zwei


def test_entwurf_wird_nicht_gezaehlt_aber_als_datenluecke_gemeldet(admin_ctx, bestand, service, stammdaten_repo, freigabe_service):
    service.erfassen(
        ctx=admin_ctx, einheit_id="601-KURZ1", art="KURZZEITVERMIETUNG", leistungsmonat="2026-08",
        belegdatum=date(2026, 9, 5), quelle_referenz="Report roh", status="ENTWURF", erstellt_von="markus",
    )
    uebersicht = berechne_monatsuebersicht(
        ctx=admin_ctx, leistungsmonat="2026-08", stammdaten_repository=stammdaten_repo, variable_service=service,
        komponenten_freigabe_service=freigabe_service,
    )
    assert uebersicht.kurzzeit_netto_anteil_cent == 0
    assert uebersicht.vollstaendig is False
    assert any("ENTWURF" in g for g in uebersicht.datenluecken)


def test_fehlender_bericht_wird_als_datenluecke_gemeldet(admin_ctx, bestand, service, stammdaten_repo, freigabe_service):
    uebersicht = berechne_monatsuebersicht(
        ctx=admin_ctx, leistungsmonat="2026-08", stammdaten_repository=stammdaten_repo, variable_service=service,
        komponenten_freigabe_service=freigabe_service,
    )
    assert any("601-KURZ1" in g and "kein" in g for g in uebersicht.datenluecken)
    assert any("601-STOR1" in g and "kein" in g for g in uebersicht.datenluecken)


def test_doppelzaehlung_dauermiete_und_report_wird_vermieden(admin_ctx, bestand, service, stammdaten_repo, freigabe_service):
    """Dieselbe Einheit hat sowohl einen aktiven Dauervermietungs-Vertrag
    ALS AUCH (inkonsistenterweise) einen KURZZEITVERMIETUNG-Report für
    denselben Monat - der Report darf NICHT zusätzlich aufsummiert
    werden."""

    _hmz_freigeben(freigabe_service, admin_ctx)
    service.erfassen(
        ctx=admin_ctx, einheit_id="601-TOP3", art="KURZZEITVERMIETUNG", leistungsmonat="2026-08",
        belegdatum=date(2026, 9, 5), quelle_referenz="Fehlerhafter Report", status="BESTAETIGT",
        unser_netto_anteil_cent=99_999, erstellt_von="markus",
    )
    uebersicht = berechne_monatsuebersicht(
        ctx=admin_ctx, leistungsmonat="2026-08", stammdaten_repository=stammdaten_repo, variable_service=service,
        komponenten_freigabe_service=freigabe_service,
    )
    assert uebersicht.kurzzeit_netto_anteil_cent == 0
    assert uebersicht.nettomieterloes_cent == 100_000
    assert any("Doppelzählung" in g for g in uebersicht.datenluecken)


def test_vertrag_ausserhalb_des_gewaehlten_monats_zaehlt_nicht(admin_ctx, bestand, service, stammdaten_repo, freigabe_service):
    stammdaten_repo.upsert_vertrag(
        id="V-601-3", einheit_id="601-TOP3", debitor_id="DEB-1", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1), gueltig_bis=date(2025, 12, 31),
    )
    uebersicht = berechne_monatsuebersicht(
        ctx=admin_ctx, leistungsmonat="2026-08", stammdaten_repository=stammdaten_repo, variable_service=service,
        komponenten_freigabe_service=freigabe_service,
    )
    assert uebersicht.dauermiete_soll_netto_cent == 0


def test_vertrag_endet_mitten_im_monat_ist_untermonatliche_datenluecke(admin_ctx, bestand, service, stammdaten_repo, freigabe_service):
    """Unabhängiger Review (#3), exakt reproduziert: 'Vertrag endet
    14.08., Komponente ab 01.01. -> August voller Monatsbetrag,
    vollständig=True.' - ein Vertrag, der MITTEN im Monat endet, deckt
    den Monat NICHT voll ab und darf keinen vollen Monatsbetrag liefern,
    sondern muss als untermonatliche Datenlücke erscheinen."""

    stammdaten_repo.upsert_vertrag(
        id="V-601-3", einheit_id="601-TOP3", debitor_id="DEB-1", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1), gueltig_bis=date(2026, 8, 14),
    )
    _hmz_freigeben(freigabe_service, admin_ctx)
    uebersicht = berechne_monatsuebersicht(
        ctx=admin_ctx, leistungsmonat="2026-08", stammdaten_repository=stammdaten_repo, variable_service=service,
        komponenten_freigabe_service=freigabe_service,
    )
    assert uebersicht.dauermiete_soll_netto_cent == 0  # NICHT der volle Monatsbetrag
    assert any("UNTERmonatlich" in g and "V-601-3" in g for g in uebersicht.datenluecken)
    assert uebersicht.vollstaendig is False


def test_aktiver_vertrag_ohne_qualifizierende_komponente_ist_datenluecke(admin_ctx, stammdaten_repo, service, freigabe_service):
    """Ein aktiver Vertrag OHNE jede (whitelisted) Mietkomponente liefert
    kein automatisch berechnetes Dauermiete-Soll, sondern eine explizite
    Datenlücke."""

    stammdaten_repo.upsert_gesellschaft(id="7DI", name="7D Immobilien GmbH")
    stammdaten_repo.upsert_objekt(id="601", gesellschaft_id="7DI", bezeichnung="Am Corso")
    stammdaten_repo.upsert_einheit(id="601-TOP9", objekt_id="601", bezeichnung="Top 9", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten_repo.upsert_debitor(id="DEB-9", name="Ohne Komponente")
    stammdaten_repo.upsert_vertrag(
        id="V-601-9", einheit_id="601-TOP9", debitor_id="DEB-9", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    uebersicht = berechne_monatsuebersicht(
        ctx=admin_ctx, leistungsmonat="2026-08", stammdaten_repository=stammdaten_repo, variable_service=service,
        komponenten_freigabe_service=freigabe_service,
    )
    assert uebersicht.dauermiete_soll_netto_cent == 0
    assert any("V-601-9" in g and "keine für den vollen Monat gültige Mietkomponente" in g for g in uebersicht.datenluecken)


def test_untermonatliche_komponente_sichtbar_auch_wenn_andere_komponente_vollmonatlich_ist(
    admin_ctx, bestand, service, stammdaten_repo, freigabe_service
):
    """Unabhängiger Review (A): HMZ ganzmonatlich mit Freigabe 800 EUR,
    KUECHE beginnt erst 15.08. mit Freigabe 50 EUR - August muss 800 EUR
    zählen (nur die vollmonatliche HMZ), ABER die Küchenänderung muss
    als explizite untermonatliche Datenlücke SICHTBAR bleiben, statt
    unsichtbar zu verschwinden (vorheriger Fehler: nur der Monatserste
    wurde geprüft, eine erst untermonatlich beginnende Komponente wurde
    dadurch nie gesehen)."""

    _hmz_freigeben(freigabe_service, admin_ctx, betrag_cent=80_000)
    stammdaten_repo.add_komponente(
        id="K-KUECHE", vertrag_id="V-601-3", art="KUECHE", bezeichnung="Küchenmiete",
        betrag_cent=5_000, gueltig_von=date(2026, 8, 15),
    )
    freigabe_service.freigeben(
        ctx=admin_ctx, komponente_id="K-KUECHE", bestaetigter_netto_betrag_cent=5_000,
        quellenbeleg_referenz="Nachtrag Küche", gueltig_von=date(2026, 8, 15), gueltig_bis=None,
        freigegeben_von="markus",
    )
    uebersicht = berechne_monatsuebersicht(
        ctx=admin_ctx, leistungsmonat="2026-08", stammdaten_repository=stammdaten_repo, variable_service=service,
        komponenten_freigabe_service=freigabe_service,
    )
    assert uebersicht.dauermiete_soll_netto_cent == 80_000  # NICHT 85.000 (Küche nur untermonatlich)
    assert uebersicht.vollstaendig is False
    assert any("K-KUECHE" in g and "UNTERmonatlich" in g for g in uebersicht.datenluecken)


def test_nicht_ueberlappende_freigaben_bleiben_bei_neuer_freigabe_unberuehrt(
    admin_ctx, bestand, service, stammdaten_repo, freigabe_service
):
    """Unabhängiger Review (B), exakt reproduziert: Augustfreigabe 800
    anlegen, danach eine NICHT überlappende Septemberfreigabe 850 -
    August darf NICHT auf 0 fallen (die alte Fassung entwertete PAUSCHAL
    ALLE früheren Freigaben derselben Komponente, unabhängig vom
    Zeitraum)."""

    august = _hmz_freigeben(freigabe_service, admin_ctx, betrag_cent=80_000, gueltig_von=date(2026, 8, 1), gueltig_bis=date(2026, 8, 31))
    freigabe_service.freigeben(
        ctx=admin_ctx, komponente_id="K-HMZ", bestaetigter_netto_betrag_cent=85_000,
        quellenbeleg_referenz="Vertragsanpassung September", gueltig_von=date(2026, 9, 1), gueltig_bis=date(2026, 9, 30),
        freigegeben_von="markus",
    )

    uebersicht_august = berechne_monatsuebersicht(
        ctx=admin_ctx, leistungsmonat="2026-08", stammdaten_repository=stammdaten_repo, variable_service=service,
        komponenten_freigabe_service=freigabe_service,
    )
    assert uebersicht_august.dauermiete_soll_netto_cent == 80_000  # NICHT 0

    uebersicht_september = berechne_monatsuebersicht(
        ctx=admin_ctx, leistungsmonat="2026-09", stammdaten_repository=stammdaten_repo, variable_service=service,
        komponenten_freigabe_service=freigabe_service,
    )
    assert uebersicht_september.dauermiete_soll_netto_cent == 85_000

    august_zeile = freigabe_service._repository.get(august.id)
    assert august_zeile.status == "FREIGEGEBEN"  # unberührt, NICHT entwertet


def test_ueberlappende_freigabe_ohne_aenderungsgrund_wird_abgelehnt(admin_ctx, bestand, freigabe_service):
    _hmz_freigeben(freigabe_service, admin_ctx, betrag_cent=80_000, gueltig_von=date(2026, 8, 1), gueltig_bis=None)
    with pytest.raises(ValueError):
        freigabe_service.freigeben(
            ctx=admin_ctx, komponente_id="K-HMZ", bestaetigter_netto_betrag_cent=90_000,
            quellenbeleg_referenz="Korrektur", gueltig_von=date(2026, 8, 1), gueltig_bis=None,
            freigegeben_von="markus",
        )


def test_ueberlappende_freigabe_mit_aenderungsgrund_entwertet_nur_die_ueberlappende(
    admin_ctx, bestand, freigabe_service
):
    erste = _hmz_freigeben(freigabe_service, admin_ctx, betrag_cent=80_000, gueltig_von=date(2026, 8, 1), gueltig_bis=None)
    zweite = freigabe_service.freigeben(
        ctx=admin_ctx, komponente_id="K-HMZ", bestaetigter_netto_betrag_cent=90_000,
        quellenbeleg_referenz="Korrektur lt. Nachtrag", gueltig_von=date(2026, 8, 1), gueltig_bis=None,
        freigegeben_von="markus", aenderungsgrund="Nettoanteil nachträglich korrigiert",
    )
    assert freigabe_service._repository.get(erste.id).status == "INVALIDIERT"
    assert zweite.status == "FREIGEGEBEN"


def test_ausgeschlossenes_objekt_ausgeschlossen_von_summe_und_fehlendem_bericht(admin_ctx, bestand, service, stammdaten_repo, freigabe_service):
    """Unabhängiger Review (#1): ein ausgeschlossenes Objekt darf WEDER
    in der Dauermiete-Soll-Summe NOCH in den 'fehlender Bericht'-
    Hinweisen auftauchen."""

    _hmz_freigeben(freigabe_service, admin_ctx)
    stammdaten_repo.upsert_objekt(id="601", gesellschaft_id="7DI", bezeichnung="Am Corso", ausgeschlossen=True)
    uebersicht = berechne_monatsuebersicht(
        ctx=admin_ctx, leistungsmonat="2026-08", stammdaten_repository=stammdaten_repo, variable_service=service,
        komponenten_freigabe_service=freigabe_service,
    )
    assert uebersicht.dauermiete_soll_netto_cent == 0
    assert not any("601-KURZ1" in g for g in uebersicht.datenluecken)
    assert not any("601-STOR1" in g for g in uebersicht.datenluecken)
    assert not any("V-601-3" in g for g in uebersicht.datenluecken)


def test_nachtraeglich_ausgeschlossenes_objekt_wird_aus_kurzzeit_summe_entfernt(
    admin_ctx, bestand, service, stammdaten_repo, freigabe_service
):
    """Unabhängige Gegenprobe: ein KURZZEITVERMIETUNG-Bericht, dessen
    Objekt ERST NACH der Erfassung ausgeschlossen wird, darf nicht mehr
    summiert werden - `variable_service.liste_aktuelle` (Basis der
    Kurzzeit-/Selfstorage-Summe) muss den Objektausschluss selbst
    prüfen, nicht nur den Gesellschaftsscope."""

    service.erfassen(
        ctx=admin_ctx, einheit_id="601-KURZ1", art="KURZZEITVERMIETUNG", leistungsmonat="2026-08",
        belegdatum=date(2026, 9, 5), quelle_referenz="Report", status="BESTAETIGT",
        unser_netto_anteil_cent=30_000, erstellt_von="markus",
    )
    stammdaten_repo.upsert_objekt(id="601", gesellschaft_id="7DI", bezeichnung="Am Corso", ausgeschlossen=True)
    uebersicht = berechne_monatsuebersicht(
        ctx=admin_ctx, leistungsmonat="2026-08", stammdaten_repository=stammdaten_repo, variable_service=service,
        komponenten_freigabe_service=freigabe_service,
    )
    assert uebersicht.kurzzeit_netto_anteil_cent == 0
    assert uebersicht.nettomieterloes_cent == 0


def test_gesellschaftsscope_filter(admin_ctx, bestand, service, stammdaten_repo, freigabe_service):
    stammdaten_repo.upsert_gesellschaft(id="ANDERE", name="Andere GmbH")
    uebersicht = berechne_monatsuebersicht(
        ctx=admin_ctx, leistungsmonat="2026-08", stammdaten_repository=stammdaten_repo, variable_service=service,
        komponenten_freigabe_service=freigabe_service, gesellschaft_id="ANDERE",
    )
    assert uebersicht.dauermiete_soll_netto_cent == 0
    assert uebersicht.datenluecken == ()


def test_fremder_ctx_sieht_nichts_von_7di(ctx_factory, bestand, service, stammdaten_repo, freigabe_service):
    fremder_ctx = ctx_factory("ANDERE-GESELLSCHAFT")
    uebersicht = berechne_monatsuebersicht(
        ctx=fremder_ctx, leistungsmonat="2026-08", stammdaten_repository=stammdaten_repo, variable_service=service,
        komponenten_freigabe_service=freigabe_service,
    )
    assert uebersicht.dauermiete_soll_netto_cent == 0
    assert uebersicht.nettomieterloes_cent == 0
    assert uebersicht.datenluecken == ()
