"""Gezielter Regressionstest zu Fix6 (unabhängige Rückprüfung Codex
22.10.2026): ein Manualimport OHNE Kosten-Freigabe (`pauschale_
abgegolten=False`) muss auf dem GEMEINSAMEN Kontoblatt sichtbar sein -
`erstelle_kontoblatt` kannte `ManuellerMahnversandTable` bisher gar
nicht. Synthetische Daten, keine DB, kein PDF."""

from __future__ import annotations

from datetime import date

from mietinkasso.infrastructure.db.tables import ManuellerMahnversandTable
from mietinkasso.op.kontoblatt import erstelle_kontoblatt


def _manualimport(*, id: int, pauschale_abgegolten: bool, beleg_referenz: str = "BELEG-1") -> ManuellerMahnversandTable:
    return ManuellerMahnversandTable(
        id=id, vertrag_id="V-1", stufe=1, forderung_op_position_ids_json="[1]",
        versanddatum=date(2026, 3, 1), kanal="BRIEF", empfaenger_name="Max Mustermieter",
        empfaenger_adresse="Musterstraße 1\n1010 Wien", beleg_referenz=beleg_referenz,
        beleg_pdf_bytes=b"", beleg_sha256="abc", idempotenz_schluessel=f"KEY-{id}",
        erfasst_von="codex-test", mahnkosten_buchung_id=None, pauschale_abgegolten=pauschale_abgegolten,
    )


def _kontoblatt(**overrides):
    werte = dict(
        konto_id="K-1", saldo_cent=0, faelliger_unstrittiger_rest_cent=0, positionen_aktiv=[], positionen_alle=[],
        offene_forderungen_rest_je_id={}, mahnkosten_buchungen=[], ruecklastschriftkosten_zeilen=[], mahnlaeufe=[],
    )
    werte.update(overrides)
    return erstelle_kontoblatt(**werte)


def test_manualimport_ohne_kostenfreigabe_erscheint_als_ausstehend():
    nicht_gebucht = _manualimport(id=1, pauschale_abgegolten=False, beleg_referenz="SYNRENT-MAHNUNG-1")

    kontoblatt = _kontoblatt(manuelle_mahnversand_zeilen=[nicht_gebucht])

    assert len(kontoblatt.ausstehende_manuelle_mahnversand) == 1
    assert kontoblatt.ausstehende_manuelle_mahnversand[0].beleg_referenz == "SYNRENT-MAHNUNG-1"


def test_manualimport_bereits_abgegolten_erscheint_nicht_doppelt_als_ausstehend():
    bereits_gebucht = _manualimport(id=2, pauschale_abgegolten=True)

    kontoblatt = _kontoblatt(manuelle_mahnversand_zeilen=[bereits_gebucht])

    assert kontoblatt.ausstehende_manuelle_mahnversand == []


def test_ohne_manuelle_mahnversand_zeilen_bleibt_liste_leer_rueckwaertskompatibel():
    """Bestehende Aufrufer, die `manuelle_mahnversand_zeilen` (noch) nicht
    übergeben, laufen unverändert weiter - leere Liste, kein Fehler."""

    kontoblatt = _kontoblatt()

    assert kontoblatt.ausstehende_manuelle_mahnversand == []
