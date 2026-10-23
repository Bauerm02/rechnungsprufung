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
    # Korrektur (Codex-Abnahme 20.10.2026): KEIN konkretes Kalenderdatum
    # mehr im Kundentext - der tatsächliche Zugang ist beim Versand noch
    # nicht bekannt, ein daraus abgeleitetes Datum wäre vorgetäuscht.
    assert "01.11.2026" not in text.kerntext
    assert text.rechtlicher_kern is None


def test_stufe2_bietet_klaerung_ohne_automatischen_aufschub():
    """Nachtrag Markus 23.10.2026 (Gegenlesen von d52948f): die
    bisherige Fassung enthielt eine unnötige Rechts-/Verfahrensbelehrung
    ("KEIN Zahlungsaufschub zugesagt", Stundung/Ratenvereinbarung) -
    jetzt kurz: EIN Einleitungssatz mit Kontaktangebot bei
    Zahlungsschwierigkeiten, dieselbe gemeinsame 14-Tage-
    Pflichtaufforderung wie jede andere Stufe."""

    text = text_fuer_stufe(stufe=2, rechtsordnung=Rechtsordnung.OESTERREICH_MRG_VOLL.value, zahlungsfrist_bis=date(2026, 11, 1))
    assert "Zahlungsschwierigkeiten" in text.einleitung
    assert "sprechen Sie bitte mit uns" in text.einleitung
    assert "binnen 14 Tagen ab Zugang" in text.kerntext  # auch Stufe 2 bleibt konkret/ernst
    # Die frühere Verfahrensbelehrung ist bewusst entfallen.
    assert "Zahlungsaufschub" not in text.kerntext and "Zahlungsaufschub" not in text.einleitung
    assert "Stundung" not in text.kerntext and "Ratenvereinbarung" not in text.kerntext


def test_stufe3_ist_nachdruecklich():
    text = text_fuer_stufe(stufe=3, rechtsordnung=Rechtsordnung.OESTERREICH_MRG_VOLL.value, zahlungsfrist_bis=date(2026, 11, 1))
    assert "nachdrücklich" in text.einleitung.lower() or "nachdrücklich" in text.kerntext.lower() or "Nachdrückliche" in text.betreff


def test_stufe4_letzte_zahlungsaufforderung_mit_ra_uebergabe_und_keine_kuendigung():
    """Auftrag HV-20261023-MAHNUNG-EINFACH (23.10.2026): der Kundentext
    bleibt kurz und KONDITIONAL, OHNE Paragraphen-Nennung ("Rechts-
    grundlagen... gehören in interne Prüfung, nicht lange Quellenblöcke
    an Mieter") - die konkrete §-Herleitung lebt jetzt ausschließlich in
    `hinweis_intern` (siehe eigener Test unten)."""

    text = text_fuer_stufe(stufe=4, rechtsordnung=Rechtsordnung.OESTERREICH_MRG_VOLL.value, zahlungsfrist_bis=date(2026, 11, 1))
    assert "binnen 14 Tagen ab Zugang" in text.kerntext
    assert "letzte Zahlungsaufforderung" in text.einleitung
    assert "Rechtsanwalt" in text.rechtlicher_kern
    assert "Räumung" in text.rechtlicher_kern
    assert "gesetzlichen Voraussetzungen" in text.rechtlicher_kern  # konditional, keine Tatsachenbehauptung
    assert "KEINE Kündigung" in text.rechtlicher_kern
    assert "NICHT die gesondert ausgewiesenen Verzugszinsen" in text.rechtlicher_kern
    # Kein Paragraph mehr im Kundentext - gehört jetzt ausschließlich
    # intern dokumentiert.
    assert "§" not in text.rechtlicher_kern
    assert "MRG" not in text.rechtlicher_kern
    assert "ABGB" not in text.rechtlicher_kern
    # Korrektur (Codex-Abnahme 20.10.2026): der Mitmieter-Zustellprüf-
    # hinweis ist ein interner Prozesshinweis und darf NIE im Kundentext
    # stehen - er wird jetzt ausschließlich über `hinweis_intern`
    # transportiert, der deshalb (anders als vorher) NIE `None` ist.
    assert "Mitmieter" not in text.rechtlicher_kern
    assert text.hinweis_intern is not None
    assert "Mitmieter" in text.hinweis_intern
    # Die konkrete §-Herleitung ist weiterhin vollständig, nur intern.
    assert "§ 30 Abs 2 Z 1 MRG" in text.hinweis_intern
    assert "§ 1118 zweiter Fall ABGB" in text.hinweis_intern
    assert "KEIN Ersatz" in text.hinweis_intern  # § 1118-Fristhinweis


def test_stufe4_mrg_teilanwendung_nennt_gehedgten_paragraf_30_mrg_intern():
    """Korrektur (Codex-Abnahme 20.10.2026, Primärquelle RIS § 1 MRG):
    § 1 Abs 4 MRG erklärt die §§ 29-36 MRG (also auch § 30) im
    Teilanwendungsbereich grundsätzlich für anwendbar - die vorherige
    Fassung behauptete fälschlich das Gegenteil. Der Hinweis bleibt aber
    AUSDRÜCKLICH gehedgt ("soweit... anwendbar ist"), keine unbedingte
    Behauptung wie bei MRG_VOLL. Seit HV-20261023-MAHNUNG-EINFACH lebt
    diese Herleitung ausschließlich intern, nicht mehr im Kundentext."""

    text = text_fuer_stufe(stufe=4, rechtsordnung=Rechtsordnung.OESTERREICH_MRG_TEIL.value, zahlungsfrist_bis=date(2026, 11, 1))
    assert "§" not in text.rechtlicher_kern
    assert text.hinweis_intern is not None
    assert "§ 30 Abs 2 Z 1 MRG" in text.hinweis_intern
    assert "soweit" in text.hinweis_intern
    assert "§ 1 Abs 4 MRG" in text.hinweis_intern
    assert "§ 1118 zweiter Fall ABGB" in text.hinweis_intern
    assert "Mitmieter" not in text.rechtlicher_kern


@pytest.mark.parametrize("rechtsordnung", [
    Rechtsordnung.OESTERREICH_MRG_FREI.value, Rechtsordnung.OESTERREICH_GEWERBE.value, Rechtsordnung.OESTERREICH_WGG.value,
])
def test_stufe4_weitere_oesterreichische_rechtsordnungen_nur_abgb(rechtsordnung):
    text = text_fuer_stufe(stufe=4, rechtsordnung=rechtsordnung, zahlungsfrist_bis=date(2026, 11, 1))
    assert "§" not in text.rechtlicher_kern  # kein Paragraph mehr im Kundentext (HV-20261023-MAHNUNG-EINFACH)
    assert "Mitmieter" not in text.rechtlicher_kern
    assert text.hinweis_intern is not None
    assert "§ 30" not in text.hinweis_intern
    assert "§ 1118 zweiter Fall ABGB" in text.hinweis_intern


def test_gewerbe_legacy_hinweis_nur_intern_und_keine_automatische_mrg_ausnahme():
    """Auftrag HV-20261020-VERTRAGSDATENBASIS: Gewerbenutzung ist KEINE
    automatische MRG-Ausnahme - das Legacy-Enum GEWERBE darf das nicht
    implizit behaupten, der entsprechende Vorbehalt bleibt aber intern."""

    text = text_fuer_stufe(stufe=4, rechtsordnung=Rechtsordnung.OESTERREICH_GEWERBE.value, zahlungsfrist_bis=date(2026, 11, 1))
    assert "automatische MRG-Ausnahme" not in text.rechtlicher_kern
    assert "GEWERBE" in text.hinweis_intern or "Gewerbe" in text.hinweis_intern


@pytest.mark.parametrize("rechtsordnung", [
    Rechtsordnung.OESTERREICH_MRG_VOLL.value, Rechtsordnung.OESTERREICH_MRG_TEIL.value,
    Rechtsordnung.OESTERREICH_MRG_FREI.value, Rechtsordnung.OESTERREICH_GEWERBE.value, Rechtsordnung.OESTERREICH_WGG.value,
])
def test_mitmieter_zustellhinweis_nie_im_kundentext_jeder_rechtsordnung(rechtsordnung):
    """Auftrag Markus (Korrektur 20.10.2026): allgemeine Zustellprüf-
    hinweise/Mitmieter-rechtssicherer-Zugang gehören intern, NIE als
    technischer Prozesshinweis in den an den Mieter gehenden Text."""

    text = text_fuer_stufe(stufe=4, rechtsordnung=rechtsordnung, zahlungsfrist_bis=date(2026, 11, 1))
    assert "Mitmieter" not in text.kerntext
    assert "Mitmieter" not in (text.rechtlicher_kern or "")
    assert text.hinweis_intern is not None
    assert "Mitmieter" in text.hinweis_intern


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
