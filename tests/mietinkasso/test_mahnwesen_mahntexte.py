"""Tests für die vier Mahnstufen-Texte (`mahnwesen/mahntexte.py`,
Auftrag HV-20261020-VIER-MAHNSTUFEN) - reine Textbausteine ohne DB."""

from __future__ import annotations

from datetime import date

import pytest

from mietinkasso.domain.enums import Rechtsordnung
from mietinkasso.mahnwesen.mahntexte import rechtlicher_kern_stufe4, text_fuer_stufe


def test_stufe1_ist_freundlich_aber_konkret_mit_frist():
    """Nutzerkorrektur 20.10.2026: 'bloß Vielleicht-übersehen reicht
    allein nicht' - Stufe 1 muss die konkrete Frist/den Ernst UND den
    freundlichen Einstieg zeigen."""

    text = text_fuer_stufe(stufe=1, rechtsordnung=Rechtsordnung.OESTERREICH_MRG_VOLL.value, zahlungsfrist_bis=date(2026, 11, 1))
    assert "Vielleicht haben Sie die Zahlung übersehen" in text.einleitung
    assert "binnen 14 Tagen ab Zugang" in text.kerntext
    assert "01.11.2026" in text.kerntext
    assert text.rechtlicher_kern is None


def test_stufe2_bietet_klaerung_ohne_automatischen_aufschub():
    text = text_fuer_stufe(stufe=2, rechtsordnung=Rechtsordnung.OESTERREICH_MRG_VOLL.value, zahlungsfrist_bis=date(2026, 11, 1))
    assert "Zahlungsschwierigkeiten" in text.kerntext
    assert "zur Klärung" in text.kerntext
    assert "KEIN Zahlungsaufschub zugesagt" in text.kerntext
    assert "binnen 14 Tagen ab Zugang" in text.kerntext  # auch Stufe 2 bleibt konkret/ernst


def test_stufe3_ist_nachdruecklich():
    text = text_fuer_stufe(stufe=3, rechtsordnung=Rechtsordnung.OESTERREICH_MRG_VOLL.value, zahlungsfrist_bis=date(2026, 11, 1))
    assert "nachdrücklich" in text.einleitung.lower() or "nachdrücklich" in text.kerntext.lower() or "Nachdrückliche" in text.betreff


def test_stufe4_letzte_zahlungsaufforderung_mit_ra_uebergabe_und_keine_kuendigung():
    text = text_fuer_stufe(stufe=4, rechtsordnung=Rechtsordnung.OESTERREICH_MRG_VOLL.value, zahlungsfrist_bis=date(2026, 11, 1))
    assert "binnen 14 Tagen ab Zugang" in text.kerntext
    assert "vollständig" in text.kerntext
    assert "Rechtsanwalt" in text.rechtlicher_kern
    assert "§ 30 Abs 2 Z 1 MRG" in text.rechtlicher_kern
    assert "§ 1118 zweiter Fall ABGB" in text.rechtlicher_kern
    assert "KEINE Kündigung" in text.rechtlicher_kern
    assert "KEIN Ersatz" in text.rechtlicher_kern  # § 1118-Fristhinweis
    assert "NICHT die gesondert ausgewiesenen Verzugszinsen" in text.rechtlicher_kern
    assert "Mitmieter" in text.rechtlicher_kern
    assert text.hinweis_intern is None  # AT MRG-Vollanwendung ist eindeutig hinterlegt


def test_stufe4_mrg_teilanwendung_nennt_kein_paragraf_30_mrg():
    """§ 30 MRG-Kündigungsschutz gilt im Teilanwendungsbereich nicht -
    der Text darf das NICHT behaupten."""

    text = text_fuer_stufe(stufe=4, rechtsordnung=Rechtsordnung.OESTERREICH_MRG_TEIL.value, zahlungsfrist_bis=date(2026, 11, 1))
    assert "§ 30" not in text.rechtlicher_kern
    assert "§ 1118 zweiter Fall ABGB" in text.rechtlicher_kern
    assert text.hinweis_intern is None


@pytest.mark.parametrize("rechtsordnung", [
    Rechtsordnung.OESTERREICH_MRG_FREI.value, Rechtsordnung.OESTERREICH_GEWERBE.value, Rechtsordnung.OESTERREICH_WGG.value,
])
def test_stufe4_weitere_oesterreichische_rechtsordnungen_nur_abgb(rechtsordnung):
    text = text_fuer_stufe(stufe=4, rechtsordnung=rechtsordnung, zahlungsfrist_bis=date(2026, 11, 1))
    assert "§ 30" not in text.rechtlicher_kern
    assert "§ 1118 zweiter Fall ABGB" in text.rechtlicher_kern


def test_stufe4_auslaendische_rechtsordnung_erfindet_keine_oesterreichische_norm():
    """'Unbekanntes nicht erfinden': für DEUTSCHLAND (fremdes
    Rechtssystem) darf NIE § 1118 ABGB/§ 30 MRG behauptet werden - ein
    interner Hinweis macht das Fehlen sichtbar, ohne Teil des
    Kundentexts zu sein."""

    kern, hinweis_intern = rechtlicher_kern_stufe4(rechtsordnung=Rechtsordnung.DEUTSCHLAND.value)
    assert "§ 1118" not in kern
    assert "§ 30" not in kern
    assert "MRG" not in kern
    assert "ABGB" not in kern
    assert hinweis_intern is not None
    assert "Deutschland" in hinweis_intern or "DEUTSCHLAND" in hinweis_intern

    text = text_fuer_stufe(stufe=4, rechtsordnung=Rechtsordnung.DEUTSCHLAND.value, zahlungsfrist_bis=date(2026, 11, 1))
    assert text.hinweis_intern is not None
    assert text.hinweis_intern not in (text.kerntext + (text.rechtlicher_kern or ""))  # NIE im Kundentext


def test_unbekannte_stufe_wirft_statt_zu_erfinden():
    with pytest.raises(ValueError):
        text_fuer_stufe(stufe=5, rechtsordnung=Rechtsordnung.OESTERREICH_MRG_VOLL.value, zahlungsfrist_bis=date(2026, 11, 1))
