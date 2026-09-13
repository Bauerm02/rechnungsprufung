from __future__ import annotations

from datetime import date

from mietinkasso.indexautomatik.zeit import kalendermonate_subtrahieren, naechster_zinstermin_ab


def test_31_mai_minus_3_monate_faellt_auf_letzten_februartag():
    assert kalendermonate_subtrahieren(date(2026, 5, 31), 3) == date(2026, 2, 28)


def test_31_mai_minus_3_monate_im_schaltjahr():
    assert kalendermonate_subtrahieren(date(2028, 5, 31), 3) == date(2028, 2, 29)


def test_jahreswechsel_wird_korrekt_behandelt():
    assert kalendermonate_subtrahieren(date(2026, 1, 31), 3) == date(2025, 10, 31)


def test_normaler_fall_ohne_tageskappung():
    assert kalendermonate_subtrahieren(date(2026, 12, 15), 3) == date(2026, 9, 15)


def test_naechster_zinstermin_liegt_im_selben_monat_wenn_noch_nicht_erreicht():
    assert naechster_zinstermin_ab(date(2026, 3, 1), faelligkeit_tag=5) == date(2026, 3, 5)


def test_naechster_zinstermin_springt_in_folgemonat_wenn_bereits_verstrichen():
    assert naechster_zinstermin_ab(date(2026, 3, 10), faelligkeit_tag=5) == date(2026, 4, 5)


def test_naechster_zinstermin_kappt_auf_letzten_tag_im_februar():
    assert naechster_zinstermin_ab(date(2026, 2, 1), faelligkeit_tag=31) == date(2026, 2, 28)


def test_naechster_zinstermin_jahreswechsel():
    assert naechster_zinstermin_ab(date(2026, 12, 20), faelligkeit_tag=5) == date(2027, 1, 5)
