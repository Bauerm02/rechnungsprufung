"""Grenzfalltests für den deterministischen MieWeG-Berechnungskern
(Auftrag 12.09., Paket C) - unabhängig je Regel: Kappung/Dämpfung,
Erstjahresanteiligkeit, Altverträge (mehrjährige Historie), halbe
Cents, fehlende Eingaben. Reine Arithmetik, kein DB-/HTTP-Zugriff."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from mietinkasso.mieweg_vorschau.berechnung import (
    berechne_gesetzliche_hoechstgrenze,
    daempfe,
    runde_halbcent,
)

# ---------------------------------------------------------------------------
# Dämpfung (NUR bei Erhöhung über 3 Prozentpunkte die Hälfte des
# übersteigenden Teils - KEINE symmetrische Dämpfung bei Senkung/Deflation,
# siehe Korrektur nach unabhängiger RIS-Primärquellenprüfung, §1 Abs2 Z1)
# ---------------------------------------------------------------------------


def test_daempfung_unterhalb_der_schwelle_bleibt_unveraendert():
    assert daempfe(Decimal("0.02")) == Decimal("0.02")


def test_daempfung_exakt_an_der_schwelle_bleibt_unveraendert():
    assert daempfe(Decimal("0.03")) == Decimal("0.03")


def test_daempfung_oberhalb_der_schwelle_halbiert_den_uebersteigenden_teil():
    # 4% -> 3% + (4%-3%)/2 = 3.5%
    assert daempfe(Decimal("0.04")) == Decimal("0.035")


def test_daempfung_bei_deflation_ist_nicht_symmetrisch_volle_senkung():
    """Korrektur: §1 Abs2 Z1 dämpft nur eine Erhöhung über 3% - für eine
    Senkung unter -3% gibt es dafür keine Rechtsgrundlage. Eine frühere
    Fassung halbierte hier fälschlich auch den übersteigenden Teil einer
    Senkung ("symmetrisch auch bei Deflation"); das ist jetzt korrigiert:
    die volle, ungedämpfte Senkung wird durchgereicht."""

    assert daempfe(Decimal("-0.05")) == Decimal("-0.05")
    assert daempfe(Decimal("-0.10")) == Decimal("-0.10")


def test_daempfung_senkung_wird_nicht_unbegruendet_auf_null_gekappt():
    """Eine Senkung unterhalb der Dämpfungsschwelle bleibt eine echte
    Senkung - keine Kappung auf 0."""

    assert daempfe(Decimal("-0.02")) == Decimal("-0.02")
    assert daempfe(Decimal("-0.02")) != Decimal("0")


# ---------------------------------------------------------------------------
# Rundung: halber Cent oder weniger ab, mehr als halber Cent auf
# (exakte Decimal-Randtests)
# ---------------------------------------------------------------------------


def test_rundung_exakt_halber_cent_wird_abgerundet():
    assert runde_halbcent(Decimal("12345.50")) == Decimal("12345")


def test_rundung_knapp_ueber_halbem_cent_wird_aufgerundet():
    assert runde_halbcent(Decimal("12345.500001")) == Decimal("12346")


def test_rundung_knapp_unter_halbem_cent_wird_abgerundet():
    assert runde_halbcent(Decimal("12345.499999")) == Decimal("12345")


def test_rundung_glatter_cent_bleibt_unveraendert():
    assert runde_halbcent(Decimal("12345.00")) == Decimal("12345")


# ---------------------------------------------------------------------------
# Erste Jahresanteiligkeit (volle Monate nach Bezugsmonat / 12)
# ---------------------------------------------------------------------------


def test_erstjahresanteiligkeit_dezember_ergibt_null_zwoelftel():
    """Bezugsmonat Dezember -> 0 volle Monate im Bezugsjahr -> das erste
    Jahr trägt NICHTS bei, der Multiplikator bleibt nach diesem Schritt 1."""

    ergebnis = berechne_gesetzliche_hoechstgrenze(
        mrg_zinsbeschraenkung=False,
        erster_bezug_jahr=2024,
        erster_bezug_monat=12,
        ziel_bewertungsjahr=2025,
        vpi_jahresdurchschnitte={2023: Decimal("100"), 2024: Decimal("104")},
        basis_betrag_cent=100_000,
    )
    assert ergebnis.vollstaendig
    assert len(ergebnis.jahresschritte) == 1
    schritt = ergebnis.jahresschritte[0]
    assert schritt.anteil == Decimal("0")
    assert schritt.angewandte_veraenderung == Decimal("0")
    assert ergebnis.hoechstbetrag_cent == 100_000  # unverändert


def test_erstjahresanteiligkeit_juni_ergibt_sechs_zwoelftel():
    """Bezugsmonat Juni -> 6 volle Monate -> halbe Jahresrate."""

    ergebnis = berechne_gesetzliche_hoechstgrenze(
        mrg_zinsbeschraenkung=False,
        erster_bezug_jahr=2024,
        erster_bezug_monat=6,
        ziel_bewertungsjahr=2025,
        vpi_jahresdurchschnitte={2023: Decimal("100"), 2024: Decimal("102")},  # 2% roh, unter Dämpfungsschwelle
        basis_betrag_cent=100_000,
    )
    schritt = ergebnis.jahresschritte[0]
    assert schritt.anteil == Decimal("0.5")
    assert schritt.rohe_veraenderung == Decimal("0.02")
    assert schritt.gedaempfte_veraenderung == Decimal("0.02")  # unter Schwelle, keine Dämpfung
    assert schritt.angewandte_veraenderung == Decimal("0.01")  # 2% * 0.5
    assert ergebnis.hoechstbetrag_cent == 101_000


def test_daempfung_vor_erster_jahresanteiligkeit():
    """Dämpfung muss VOR der Anteiligkeit angewendet werden: 8% roh wird
    zuerst auf 5.5% gedämpft (3%+(8%-3%)/2), erst DANACH mit 6/12
    anteilig -> 2.75%, NICHT 8%*0.5=4% zuerst gedämpft."""

    ergebnis = berechne_gesetzliche_hoechstgrenze(
        mrg_zinsbeschraenkung=False,
        erster_bezug_jahr=2024,
        erster_bezug_monat=6,
        ziel_bewertungsjahr=2025,
        vpi_jahresdurchschnitte={2023: Decimal("100"), 2024: Decimal("108")},
        basis_betrag_cent=100_000,
    )
    schritt = ergebnis.jahresschritte[0]
    assert schritt.gedaempfte_veraenderung == Decimal("0.055")
    assert schritt.angewandte_veraenderung == Decimal("0.0275")


def test_zweites_jahr_zaehlt_voll_nicht_mehr_anteilig():
    ergebnis = berechne_gesetzliche_hoechstgrenze(
        mrg_zinsbeschraenkung=False,
        erster_bezug_jahr=2023,
        erster_bezug_monat=6,
        ziel_bewertungsjahr=2025,
        vpi_jahresdurchschnitte={2022: Decimal("100"), 2023: Decimal("102"), 2024: Decimal("104")},
        basis_betrag_cent=100_000,
    )
    assert len(ergebnis.jahresschritte) == 2
    erstes, zweites = ergebnis.jahresschritte
    assert erstes.anteil == Decimal("0.5")
    assert zweites.anteil == Decimal("1")


# ---------------------------------------------------------------------------
# Kappung: MRG-Vollanwendungs-Übergangsdeckel 2025 (1%) / 2026 (2%)
# ---------------------------------------------------------------------------


def test_uebergangsdeckel_2025_kappt_auf_ein_prozent():
    ergebnis = berechne_gesetzliche_hoechstgrenze(
        mrg_zinsbeschraenkung=True,
        erster_bezug_jahr=2024,
        erster_bezug_monat=12,  # 0/12 im ersten Jahr, damit NUR 2025 die Kappung zeigt
        ziel_bewertungsjahr=2026,
        vpi_jahresdurchschnitte={2023: Decimal("100"), 2024: Decimal("100"), 2025: Decimal("106")},
        basis_betrag_cent=100_000,
    )
    schritt_2025 = next(s for s in ergebnis.jahresschritte if s.jahr == 2025)
    assert schritt_2025.rohe_veraenderung == Decimal("0.06")
    assert schritt_2025.gedaempfte_veraenderung == Decimal("0.01")  # gekappt, NICHT die allgemeine Dämpfung (3.5%)
    assert schritt_2025.uebergangsdeckel_angewandt is True


def test_uebergangsdeckel_2026_kappt_auf_zwei_prozent():
    ergebnis = berechne_gesetzliche_hoechstgrenze(
        mrg_zinsbeschraenkung=True,
        erster_bezug_jahr=2024,
        erster_bezug_monat=12,
        ziel_bewertungsjahr=2027,
        vpi_jahresdurchschnitte={
            2023: Decimal("100"), 2024: Decimal("100"), 2025: Decimal("100"), 2026: Decimal("105"),
        },
        basis_betrag_cent=100_000,
    )
    schritt_2026 = next(s for s in ergebnis.jahresschritte if s.jahr == 2026)
    assert schritt_2026.rohe_veraenderung == Decimal("0.05")
    assert schritt_2026.gedaempfte_veraenderung == Decimal("0.02")
    assert schritt_2026.uebergangsdeckel_angewandt is True


def test_uebergangsdeckel_gilt_nicht_ohne_mrg_zinsbeschraenkung():
    """Dieselbe 6%-Veränderung im Jahr 2025 OHNE Zinsbeschränkungsflag
    unterliegt der ALLGEMEINEN Dämpfung (3%+(6%-3%)/2=4.5%), nicht dem
    1%-Deckel."""

    ergebnis = berechne_gesetzliche_hoechstgrenze(
        mrg_zinsbeschraenkung=False,
        erster_bezug_jahr=2024,
        erster_bezug_monat=12,
        ziel_bewertungsjahr=2026,
        vpi_jahresdurchschnitte={2023: Decimal("100"), 2024: Decimal("100"), 2025: Decimal("106")},
        basis_betrag_cent=100_000,
    )
    schritt_2025 = next(s for s in ergebnis.jahresschritte if s.jahr == 2025)
    assert schritt_2025.gedaempfte_veraenderung == Decimal("0.045")
    assert schritt_2025.uebergangsdeckel_angewandt is False


def test_uebergangsdeckel_gilt_nicht_fuer_negative_veraenderung():
    """Der Deckel ist eine reine Erhöhungsgrenze - eine Senkung im Jahr
    2025 unterliegt weiterhin der allgemeinen Dämpfungsregel, nicht dem
    1%-Deckel (der keine Untergrenze für Deflation ist). Die allgemeine
    Regel dämpft aber (Korrektur nach unabhängiger RIS-Prüfung, §1 Abs2
    Z1) NUR Erhöhungen über 3% - eine Senkung bleibt daher in voller
    Höhe."""

    ergebnis = berechne_gesetzliche_hoechstgrenze(
        mrg_zinsbeschraenkung=True,
        erster_bezug_jahr=2024,
        erster_bezug_monat=12,
        ziel_bewertungsjahr=2026,
        vpi_jahresdurchschnitte={2023: Decimal("100"), 2024: Decimal("100"), 2025: Decimal("94")},
        basis_betrag_cent=100_000,
    )
    schritt_2025 = next(s for s in ergebnis.jahresschritte if s.jahr == 2025)
    assert schritt_2025.rohe_veraenderung == Decimal("-0.06")
    assert schritt_2025.gedaempfte_veraenderung == Decimal("-0.06")  # volle Senkung, keine Dämpfung
    assert schritt_2025.uebergangsdeckel_angewandt is False


def test_uebergangsdeckel_gilt_ab_referenzjahr_2027_nicht_mehr():
    ergebnis = berechne_gesetzliche_hoechstgrenze(
        mrg_zinsbeschraenkung=True,
        erster_bezug_jahr=2027,
        erster_bezug_monat=12,
        ziel_bewertungsjahr=2028,
        vpi_jahresdurchschnitte={2026: Decimal("100"), 2027: Decimal("104")},
        basis_betrag_cent=100_000,
    )
    schritt_2027 = next(s for s in ergebnis.jahresschritte if s.jahr == 2027)
    assert schritt_2027.gedaempfte_veraenderung == Decimal("0.035")  # allgemeine Dämpfung, kein Deckel mehr
    assert schritt_2027.uebergangsdeckel_angewandt is False


# ---------------------------------------------------------------------------
# Altverträge / mehrjährige mehrjährige Historie ohne Doppelanrechnung
# ---------------------------------------------------------------------------


def test_altvertrag_mehrjaehrige_historie_kumuliert_je_jahr_getrennt():
    """Bezugsmonat September (eines Altvertrags, 3 volle Monate im ersten
    Jahr), erste Modellbewertung fix 2026 - mehrere Jahre werden EINZELN
    (mit je eigener Dämpfung) verrechnet, nicht als ein einziger
    Mehrjahres-Sprung."""

    ergebnis = berechne_gesetzliche_hoechstgrenze(
        mrg_zinsbeschraenkung=False,
        erster_bezug_jahr=2023,  # Bezugsmonat September 2023
        erster_bezug_monat=9,
        ziel_bewertungsjahr=2026,
        vpi_jahresdurchschnitte={
            2022: Decimal("100"), 2023: Decimal("104"), 2024: Decimal("108"), 2025: Decimal("110"),
        },
        basis_betrag_cent=100_000,
    )
    assert ergebnis.vollstaendig
    assert [s.jahr for s in ergebnis.jahresschritte] == [2023, 2024, 2025]
    assert ergebnis.jahresschritte[0].anteil == Decimal("3") / Decimal("12")  # Sept -> 3 volle Monate
    assert ergebnis.jahresschritte[1].anteil == Decimal("1")
    assert ergebnis.jahresschritte[2].anteil == Decimal("1")
    # Jedes Jahr wurde EINZELN gedämpft (keine der drei Raten übersteigt
    # 3%, also unverändert): 2023 4%*3/12=1%, 2024 108/104-1≈3.846%->
    # gedämpft auf 3%+0.846%/2≈3.423%, 2025 110/108-1≈1.852%<3% unverändert.
    assert ergebnis.jahresschritte[1].gedaempfte_veraenderung < ergebnis.jahresschritte[1].rohe_veraenderung
    assert ergebnis.jahresschritte[2].gedaempfte_veraenderung == ergebnis.jahresschritte[2].rohe_veraenderung
    assert ergebnis.fruehester_termin == date(2026, 4, 1)


def test_altvertrag_annualaverage_als_bisherige_basis_verwendet_dezember():
    """War die zuletzt verwendete Basis selbst ein Jahresdurchschnitt
    (kein spezifischer Monat), wird sie wie ein Bezugsmonat Dezember
    behandelt (0/12 im ersten Jahr) - das entscheidet die aufrufende
    Service-Schicht, die pure Funktion nimmt hier einfach Monat=12 als
    Eingabe entgegen."""

    ergebnis = berechne_gesetzliche_hoechstgrenze(
        mrg_zinsbeschraenkung=False,
        erster_bezug_jahr=2024,
        erster_bezug_monat=12,
        ziel_bewertungsjahr=2026,
        vpi_jahresdurchschnitte={2023: Decimal("100"), 2024: Decimal("100"), 2025: Decimal("102")},
        basis_betrag_cent=100_000,
    )
    assert ergebnis.jahresschritte[0].anteil == Decimal("0")


# ---------------------------------------------------------------------------
# Basis läuft unabhängig weiter (nicht auf tatsächlich niedrigere Miete
# zurückgesetzt) - ergibt sich hier daraus, dass ausschließlich
# `basis_betrag_cent` fortgeschrieben wird, niemals ein "tatsächlich
# gezahlter" Betrag als Zwischenbasis übernommen wird.
# ---------------------------------------------------------------------------


def test_kumulierung_bleibt_unabhaengig_von_zwischenzeitlich_niedrigerer_miete():
    """Zwei Jahre mit je 2% (unter der Dämpfungsschwelle) kumulieren
    MULTIPLIKATIV auf der URSPRÜNGLICHEN Basis - 100.000 * 1.02 * 1.02,
    NICHT 100.000 + 2% + 2% (linear) und nicht auf einen fiktiv
    niedrigeren Zwischenbetrag bezogen."""

    ergebnis = berechne_gesetzliche_hoechstgrenze(
        mrg_zinsbeschraenkung=False,
        erster_bezug_jahr=2023,
        erster_bezug_monat=12,
        ziel_bewertungsjahr=2025,
        vpi_jahresdurchschnitte={2022: Decimal("100"), 2023: Decimal("100"), 2024: Decimal("102")},
        basis_betrag_cent=100_000,
    )
    erwarteter_multiplikator = Decimal("1") * Decimal("1.02")  # erstes Jahr: Dez->0/12, zweites Jahr: voll 2%
    assert ergebnis.kumulierter_multiplikator == erwarteter_multiplikator
    assert ergebnis.hoechstbetrag_cent == 102_000


# ---------------------------------------------------------------------------
# Fehlende Eingaben: kein erfundener Wert
# ---------------------------------------------------------------------------


def test_fehlender_vpi_jahresdurchschnitt_ergibt_offenes_ergebnis_ohne_erfundenen_wert():
    ergebnis = berechne_gesetzliche_hoechstgrenze(
        mrg_zinsbeschraenkung=False,
        erster_bezug_jahr=2023,
        erster_bezug_monat=1,
        ziel_bewertungsjahr=2026,
        vpi_jahresdurchschnitte={2022: Decimal("100"), 2023: Decimal("102")},  # 2024/2025 fehlen
        basis_betrag_cent=100_000,
    )
    assert not ergebnis.vollstaendig
    assert ergebnis.fehlende_jahre == [2024]
    assert ergebnis.hoechstbetrag_cent is None
    assert ergebnis.kumulierter_multiplikator is None
    assert len(ergebnis.jahresschritte) == 1  # 2023 wurde noch berechnet, dann Abbruch


def test_fehlendes_vorjahr_fuer_das_allererste_jahr_ergibt_sofort_offenes_ergebnis():
    ergebnis = berechne_gesetzliche_hoechstgrenze(
        mrg_zinsbeschraenkung=False,
        erster_bezug_jahr=2023,
        erster_bezug_monat=1,
        ziel_bewertungsjahr=2024,
        vpi_jahresdurchschnitte={2023: Decimal("102")},  # 2022 (Vorjahr) fehlt
        basis_betrag_cent=100_000,
    )
    assert ergebnis.fehlende_jahre == [2023]
    assert ergebnis.jahresschritte == []


# ---------------------------------------------------------------------------
# Ungültige Eingaben
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("monat", [0, 13, -1])
def test_ungueltiger_bezugsmonat_wird_abgelehnt(monat):
    with pytest.raises(ValueError, match="erster_bezug_monat"):
        berechne_gesetzliche_hoechstgrenze(
            mrg_zinsbeschraenkung=False, erster_bezug_jahr=2023, erster_bezug_monat=monat,
            ziel_bewertungsjahr=2024, vpi_jahresdurchschnitte={}, basis_betrag_cent=100_000,
        )


def test_ziel_bewertungsjahr_muss_nach_erster_bezug_jahr_liegen():
    with pytest.raises(ValueError, match="ziel_bewertungsjahr"):
        berechne_gesetzliche_hoechstgrenze(
            mrg_zinsbeschraenkung=False, erster_bezug_jahr=2025, erster_bezug_monat=1,
            ziel_bewertungsjahr=2025, vpi_jahresdurchschnitte={}, basis_betrag_cent=100_000,
        )


def test_negativer_basisbetrag_wird_abgelehnt():
    with pytest.raises(ValueError, match="basis_betrag_cent"):
        berechne_gesetzliche_hoechstgrenze(
            mrg_zinsbeschraenkung=False, erster_bezug_jahr=2023, erster_bezug_monat=1,
            ziel_bewertungsjahr=2024, vpi_jahresdurchschnitte={2022: Decimal("100"), 2023: Decimal("102")},
            basis_betrag_cent=0,
        )
