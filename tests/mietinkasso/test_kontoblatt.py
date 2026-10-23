"""Tests für das reine Kontoblatt-Kostenmodell (`op/kontoblatt.py`,
Auftrag HV-20261020-KONTOBLATT-KOSTEN) - `erstelle_kontoblatt` ist eine
reine Funktion (keine DB), bekommt hier ausschließlich synthetische,
von Hand gebaute `OPPositionTable`/`MahnkostenBuchungTable`/
`RuecklastschriftkostenTable`/`MahnLaufTable`-Objekte übergeben (nie
persistiert) - keine echten Daten, keine Produktivbuchungen."""

from __future__ import annotations

from datetime import date

from mietinkasso.infrastructure.db.tables import (
    MahnkostenBuchungTable,
    MahnLaufTable,
    OPPositionTable,
    RuecklastschriftkostenTable,
)
import json

from decimal import Decimal

from mietinkasso.mahnwesen.kosten import ZinsSegment
from mietinkasso.op.kontoblatt import (
    EinfacheZeile,
    KATEGORIE_HAUPTFORDERUNG,
    KATEGORIE_MAHNPAUSCHALE,
    KATEGORIE_MAHNSPESEN,
    KATEGORIE_RUECKLASTSCHRIFTKOSTEN,
    KATEGORIE_VERZUGSZINSEN,
    STATUS_BEZAHLT,
    STATUS_ERSETZT,
    STATUS_OFFEN,
    STATUS_STORNIERT,
    STATUS_TEILBEZAHLT,
    _hauptforderung_komponente_text,
    _satz_text,
    erstelle_einfache_tabelle,
    erstelle_journal,
    erstelle_kontoblatt,
    erweitere_um_vorschau,
)


def _op(
    id: int, typ: str, betrag_cent: int, *, status: str = "AKTIV", quelle_system: str | None = None,
    storniert_durch_id: int | None = None, belegdatum: date = date(2026, 9, 1), beleg_referenz: str | None = None,
    bezieht_sich_auf_id: int | None = None,
) -> OPPositionTable:
    return OPPositionTable(
        id=id, konto_id="K-1", typ=typ, betrag_cent=betrag_cent, belegdatum=belegdatum, buchungsdatum=belegdatum,
        faelligkeit=None, faelligkeit_bekannt=False, status=status, quelle_system=quelle_system,
        storniert_durch_id=storniert_durch_id, beleg_referenz=beleg_referenz, bezieht_sich_auf_id=bezieht_sich_auf_id,
    )


def _buchung(
    *, vertrag_id: str = "V-1", stufe: int = 1, zinsen_op_position_id: int | None = None,
    gebuehr_op_position_id: int | None = None, mahnpauschale_op_position_id: int | None = None,
    versandnachweis_referenz: str = "mahnung:X", forderung_op_position_ids: str = "[]",
    zins_segmente_json: str = "[]", zinsen_delta_je_op_json: str = "{}",
    zins_von: date = date(2026, 9, 1), zins_bis: date = date(2026, 9, 30), zinssatz_prozent=0,
) -> MahnkostenBuchungTable:
    return MahnkostenBuchungTable(
        vertrag_id=vertrag_id, stufe=stufe, mahnlauf_schluessel="HASH", forderung_op_position_ids=forderung_op_position_ids,
        hauptforderung_cent=0, zinsbasis="VEREINBART_GEPRUEFT", zinssatz_prozent=zinssatz_prozent, zins_von=zins_von,
        zins_bis=zins_bis, zinsen_cent=0, versandnachweis_referenz=versandnachweis_referenz, erstellt_von="codex-test",
        zinsen_op_position_id=zinsen_op_position_id, gebuehr_op_position_id=gebuehr_op_position_id,
        mahnpauschale_op_position_id=mahnpauschale_op_position_id,
        zins_segmente_json=zins_segmente_json, zinsen_delta_je_op_json=zinsen_delta_je_op_json,
    )


def _ruecklastschrift_kosten(
    *, kosten_op_position_id: int | None, ruecklastschrift_op_position_id: int = 1, bankentgelt_cent: int = 700,
) -> RuecklastschriftkostenTable:
    return RuecklastschriftkostenTable(
        vertrag_id="V-1", ruecklastschrift_op_position_id=ruecklastschrift_op_position_id,
        bankentgelt_cent=bankentgelt_cent, rueckgabeursache="Mangels Deckung", dem_zahlungspflichtigen_zurechenbar=kosten_op_position_id is not None,
        zurechenbarkeit_begruendung="synthetisch", beleg_referenz="BELEG-SYNTH", kosten_op_position_id=kosten_op_position_id,
        erstellt_von="codex-test",
    )


def _mahnlauf(*, id: int, status: str, mahnkosten_verarbeitet_am=None) -> MahnLaufTable:
    return MahnLaufTable(
        id=id, outbox_key=f"OUT-{id}", vertrag_id="V-1", gesellschaft_id="7DI", stufe=1,
        mitglieder_mahnfall_ids="[]", kanal="EMAIL", status=status, mahnkosten_verarbeitet_am=mahnkosten_verarbeitet_am,
    )


def _leer(**overrides):
    werte = dict(
        konto_id="K-1", saldo_cent=0, faelliger_unstrittiger_rest_cent=0, positionen_aktiv=[], positionen_alle=[],
        offene_forderungen_rest_je_id={}, mahnkosten_buchungen=[], ruecklastschriftkosten_zeilen=[], mahnlaeufe=[],
    )
    werte.update(overrides)
    return erstelle_kontoblatt(**werte)


def test_beispiel_1000_hauptsaldo_plus_zwei_undokumentierte_7eur_bankentgelte():
    """Wörtliches Beispiel aus dem Auftrag: 1.000 EUR Hauptsaldo plus
    zwei dokumentierte 7-EUR-Bankentgelte OHNE Forderungs-OP. Gesamtsaldo
    bleibt 1.000,00, die 14,00 EUR erscheinen separat, OHNE den Saldo zu
    verändern."""

    hauptsaldo = _op(1, "EROEFFNUNG", 100_000)
    rls1 = _ruecklastschrift_kosten(kosten_op_position_id=None, ruecklastschrift_op_position_id=10, bankentgelt_cent=700)
    rls2 = _ruecklastschrift_kosten(kosten_op_position_id=None, ruecklastschrift_op_position_id=11, bankentgelt_cent=700)

    blatt = _leer(
        saldo_cent=100_000, positionen_aktiv=[hauptsaldo], positionen_alle=[hauptsaldo],
        ruecklastschriftkosten_zeilen=[rls1, rls2],
    )

    assert blatt.saldo_cent == 100_000  # Gesamtsaldo bleibt 1.000,00
    assert blatt.hauptforderung_cent == 100_000
    assert blatt.kosten_cent == 0  # keine OP-Zeile, daher keine Kostenbuchung
    assert len(blatt.dokumentierte_bankaufwaende) == 2
    assert sum(b.row.bankentgelt_cent for b in blatt.dokumentierte_bankaufwaende) == 1400  # 14,00 EUR separat sichtbar
    for b in blatt.dokumentierte_bankaufwaende:
        assert b.status_text == "dokumentiert – nicht belastet / rechtlich in Prüfung"


def test_dokumentierter_bankaufwand_zeigt_rueckgabedatum_der_op_nicht_erfassungsdatum():
    """Codex-Abnahme 20.10.2026, P2: `rueckgabe_datum` muss aus der
    gebundenen Rücklastschrift-OP-Zeile kommen (`belegdatum`), NICHT aus
    `row.gebucht_am` (das ist nur das spätere Erfassungsdatum der
    Kosten-Dokumentationszeile selbst). Rückgabe 10.04., Erfassung
    18.04. - `rueckgabe_datum` muss den 10.04. zeigen."""

    rueckgabe_op = _op(10, "RUECKLASTSCHRIFT", 5000, belegdatum=date(2026, 4, 10))
    rls = _ruecklastschrift_kosten(kosten_op_position_id=None, ruecklastschrift_op_position_id=10)
    rls.gebucht_am = date(2026, 4, 18)  # spätere Erfassung

    blatt = _leer(positionen_aktiv=[rueckgabe_op], positionen_alle=[rueckgabe_op], ruecklastschriftkosten_zeilen=[rls])

    assert len(blatt.dokumentierte_bankaufwaende) == 1
    aufwand = blatt.dokumentierte_bankaufwaende[0]
    assert aufwand.rueckgabe_datum == date(2026, 4, 10)
    assert aufwand.row.gebucht_am == date(2026, 4, 18)  # bleibt als eigenes Erfassungsdatum erhalten
    assert aufwand.rueckgabe_datum != aufwand.row.gebucht_am


def test_dokumentierter_bankaufwand_ohne_auffindbare_op_hat_kein_erfundenes_datum():
    """Fehlt der Bezug zur Rücklastschrift-OP-Zeile, wird NIE ein Datum
    erfunden - `rueckgabe_datum` bleibt `None` ("Datum unbekannt")."""

    rls = _ruecklastschrift_kosten(kosten_op_position_id=None, ruecklastschrift_op_position_id=999)
    blatt = _leer(positionen_aktiv=[], positionen_alle=[], ruecklastschriftkosten_zeilen=[rls])
    assert blatt.dokumentierte_bankaufwaende[0].rueckgabe_datum is None


def test_gebuchte_kosten_genau_einmal_beide_ruecklastschriften_separat():
    """Zwei TATSÄCHLICH gebuchte Rücklastschriftkosten (je eine eigene
    OP-SOLL-Zeile) - jede erscheint genau einmal in `kosten_zeilen`, die
    Summe entspricht exakt der OP-Summe, keine Verdopplung."""

    hauptsaldo = _op(1, "EROEFFNUNG", 100_000)
    kosten_op_1 = _op(2, "SOLL", 700, quelle_system="ruecklastschriftkosten")
    kosten_op_2 = _op(3, "SOLL", 700, quelle_system="ruecklastschriftkosten")
    rls1 = _ruecklastschrift_kosten(kosten_op_position_id=2, ruecklastschrift_op_position_id=10)
    rls2 = _ruecklastschrift_kosten(kosten_op_position_id=3, ruecklastschrift_op_position_id=11)

    alle_positionen = [hauptsaldo, kosten_op_1, kosten_op_2]
    blatt = _leer(
        saldo_cent=101_400, positionen_aktiv=alle_positionen, positionen_alle=alle_positionen,
        ruecklastschriftkosten_zeilen=[rls1, rls2],
    )

    assert blatt.saldo_cent == 101_400
    assert blatt.hauptforderung_cent == 100_000
    assert blatt.kosten_cent == 1400
    assert len(blatt.kosten_zeilen) == 2
    assert all(z.kategorie == KATEGORIE_RUECKLASTSCHRIFTKOSTEN for z in blatt.kosten_zeilen)
    assert blatt.dokumentierte_bankaufwaende == []  # beide bereits gebucht, keine Dopplung als "nicht belastet"
    # Keine Verdopplung: Hauptforderung + Kosten + Zahlungen == echter Saldo.
    assert blatt.hauptforderung_cent + blatt.kosten_cent + blatt.zahlungen_saldo_cent == blatt.saldo_cent


def test_mahngebuehr_und_zinsen_getrennt():
    hauptsaldo = _op(1, "EROEFFNUNG", 100_000)
    zinsen_op = _op(2, "SOLL", 489, quelle_system="mahnkosten")
    gebuehr_op = _op(3, "SOLL", 2000, quelle_system="mahnkosten")
    pauschale_op = _op(4, "SOLL", 1000, quelle_system="mahnkosten")
    buchung = _buchung(zinsen_op_position_id=2, gebuehr_op_position_id=3, mahnpauschale_op_position_id=4)

    alle = [hauptsaldo, zinsen_op, gebuehr_op, pauschale_op]
    blatt = _leer(saldo_cent=103_489, positionen_aktiv=alle, positionen_alle=alle, mahnkosten_buchungen=[buchung])

    kategorien = {z.position.id: z.kategorie for z in blatt.kosten_zeilen}
    assert kategorien[2] == KATEGORIE_VERZUGSZINSEN
    assert kategorien[3] == KATEGORIE_MAHNSPESEN
    assert kategorien[4] == KATEGORIE_MAHNPAUSCHALE
    assert blatt.kosten_cent == 489 + 2000 + 1000
    assert blatt.hauptforderung_cent == 100_000


def test_kostenzeile_status_offen_teilbezahlt_bezahlt():
    """Codex-Abnahme 20.10.2026, P3: `rest=None` bedeutet nicht mehr
    pauschal "bezahlt" - `status` macht offen/teilbezahlt/bezahlt
    ausdrücklich unterscheidbar."""

    gebuehr_offen = _op(2, "SOLL", 2000, quelle_system="mahnkosten")
    gebuehr_teilbezahlt = _op(3, "SOLL", 2000, quelle_system="mahnkosten")
    gebuehr_bezahlt = _op(4, "SOLL", 2000, quelle_system="mahnkosten")
    buchung_offen = _buchung(stufe=1, gebuehr_op_position_id=2)
    buchung_teil = _buchung(stufe=2, gebuehr_op_position_id=3)
    buchung_bezahlt = _buchung(stufe=3, gebuehr_op_position_id=4)

    alle = [gebuehr_offen, gebuehr_teilbezahlt, gebuehr_bezahlt]
    blatt = _leer(
        positionen_aktiv=alle, positionen_alle=alle,
        mahnkosten_buchungen=[buchung_offen, buchung_teil, buchung_bezahlt],
        offene_forderungen_rest_je_id={2: 2000, 3: 800},  # 4 fehlt -> vollständig beglichen
    )
    status_je_id = {z.position.id: z.status for z in blatt.kosten_zeilen}
    assert status_je_id[2] == STATUS_OFFEN
    assert status_je_id[3] == STATUS_TEILBEZAHLT
    assert status_je_id[4] == STATUS_BEZAHLT
    # Gebuchte Sollsumme (6000) vs. tatsächlich noch offener Rest (2000+800=2800).
    assert blatt.kosten_cent == 6000
    assert blatt.kosten_offener_rest_cent == 2800


def test_rein_stornierte_kostenzeile_ohne_ersatz_zeigt_status_storniert():
    """Eine Kostenzeile, die OHNE Ersatz storniert wurde (`storniert_durch_id
    is None`), muss als STORNIERT (nicht ERSETZT) in der Historie
    erscheinen - und NICHT in kosten_zeilen/kosten_cent."""

    stornierte_gebuehr = _op(10, "SOLL", 2000, status="STORNIERT", quelle_system="mahnkosten", storniert_durch_id=None)
    buchung = _buchung(gebuehr_op_position_id=10)

    blatt = _leer(saldo_cent=0, positionen_aktiv=[], positionen_alle=[stornierte_gebuehr], mahnkosten_buchungen=[buchung])

    assert blatt.kosten_zeilen == []
    assert blatt.kosten_cent == 0
    assert len(blatt.kosten_historie) == 1
    assert blatt.kosten_historie[0].status == STATUS_STORNIERT


def test_hauptforderung_offener_rest_unterscheidet_sich_von_gebuchter_sollsumme():
    """Codex-Abnahme 20.10.2026, P3: `hauptforderung_cent` (gebuchte
    Sollsumme) darf NICHT mit `hauptforderung_offener_rest_cent`
    (tatsächlicher Rückstand nach Zahlungen) verwechselt werden - sonst
    wird eine bereits teilweise bezahlte Forderung als voller Rückstand
    missverstanden."""

    hauptforderung = _op(1, "SOLL", 50_000)
    blatt = _leer(
        positionen_aktiv=[hauptforderung], positionen_alle=[hauptforderung],
        offene_forderungen_rest_je_id={1: 20_000},  # 30.000 bereits bezahlt
    )
    assert blatt.hauptforderung_cent == 50_000  # gebuchte Sollsumme vor Zahlungen
    assert blatt.hauptforderung_offener_rest_cent == 20_000  # tatsächlicher Rückstand


def test_storno_und_korrektur_einer_kostenzeile_bleibt_kostenzeile():
    """`storniere_und_korrigiere` legt die Ersatzzeile mit
    `quelle_system="korrektur"` an - die korrigierte Zeile muss trotzdem
    als Kostenzeile (nicht als Hauptforderung) erkannt werden, die
    STORNIERTE Originalzeile zählt nicht mehr mit."""

    original_gebuehr = _op(10, "SOLL", 2000, status="STORNIERT", quelle_system="mahnkosten", storniert_durch_id=11)
    korrigierte_gebuehr = _op(11, "SOLL", 1000, status="AKTIV", quelle_system="korrektur")
    buchung = _buchung(gebuehr_op_position_id=10)  # Buchung referenziert die URSPRÜNGLICHE Position

    positionen_aktiv = [korrigierte_gebuehr]
    positionen_alle = [original_gebuehr, korrigierte_gebuehr]
    blatt = _leer(
        saldo_cent=1000, positionen_aktiv=positionen_aktiv, positionen_alle=positionen_alle,
        mahnkosten_buchungen=[buchung],
    )

    assert len(blatt.kosten_zeilen) == 1
    assert blatt.kosten_zeilen[0].position.id == 11
    assert blatt.kosten_zeilen[0].kategorie == KATEGORIE_MAHNSPESEN
    assert blatt.kosten_cent == 1000  # der KORRIGIERTE Betrag, nicht der ursprüngliche
    assert blatt.hauptforderung_zeilen == []  # NICHT fälschlich als Hauptforderung einsortiert

    # Codex-Abnahme 20.10.2026, P3: die stornierte ORIGINAL-Zeile bleibt
    # in der Historie sichtbar (Status ERSETZT, da eine Korrekturzeile
    # existiert) - NICHT Teil von kosten_zeilen/kosten_cent.
    assert len(blatt.kosten_historie) == 1
    assert blatt.kosten_historie[0].position.id == 10
    assert blatt.kosten_historie[0].status == STATUS_ERSETZT
    assert blatt.kosten_historie[0].kategorie == KATEGORIE_MAHNSPESEN


def test_unkorrigierte_fremde_korrektur_ohne_herkunft_bleibt_hauptforderung():
    """Eine `quelle_system="korrektur"`-Zeile OHNE auffindbaren Vorgänger
    (z. B. eine generische Korrektur einer gewöhnlichen Miet-SOLL-Zeile)
    bleibt Hauptforderung - keine falsche Kostenkategorie."""

    korrektur_ohne_vorgaenger = _op(5, "SOLL", 500, quelle_system="korrektur")
    blatt = _leer(saldo_cent=500, positionen_aktiv=[korrektur_ohne_vorgaenger], positionen_alle=[korrektur_ohne_vorgaenger])
    assert blatt.hauptforderung_zeilen[0].kategorie == KATEGORIE_HAUPTFORDERUNG
    assert blatt.kosten_zeilen == []


def test_zahlung_und_ruecklastschrift_bilden_getrennten_zahlungsstand():
    hauptsaldo = _op(1, "EROEFFNUNG", 100_000)
    zahlung = _op(2, "ZAHLUNG", 50_000)
    ruecklastschrift = _op(3, "RUECKLASTSCHRIFT", 50_000)  # die Zahlung platzte wieder

    alle = [hauptsaldo, zahlung, ruecklastschrift]
    blatt = _leer(saldo_cent=100_000, positionen_aktiv=alle, positionen_alle=alle)

    assert len(blatt.zahlungen_zeilen) == 2
    assert blatt.zahlungen_saldo_cent == 0  # Zahlung minus ihre eigene Rücklastschrift gleicht sich aus
    assert blatt.hauptforderung_cent == 100_000
    assert blatt.hauptforderung_cent + blatt.kosten_cent + blatt.zahlungen_saldo_cent == blatt.saldo_cent


def test_ausstehender_mahnlauf_zeigt_nicht_gebucht_nicht_alles_bezahlt():
    laufend = _mahnlauf(id=1, status="UNSICHER", mahnkosten_verarbeitet_am=None)
    abgeschlossen = _mahnlauf(id=2, status="GESENDET", mahnkosten_verarbeitet_am="2026-10-01T00:00:00+00:00")
    geplant = _mahnlauf(id=3, status="GEPLANT", mahnkosten_verarbeitet_am=None)

    blatt = _leer(mahnlaeufe=[laufend, abgeschlossen, geplant])

    ausstehende_ids = {m.id for m in blatt.ausstehende_mahnlaeufe}
    assert ausstehende_ids == {1, 3}  # UNSICHER und GEPLANT: Kosten noch nicht abgeschlossen
    assert 2 not in ausstehende_ids  # bereits verarbeitet


def test_blockiert_und_uebersprungen_gelten_nicht_als_ausstehend():
    """Diese beiden Status sind ein ENDGÜLTIGES "wird nie gebucht"
    (Reservierung bereits freigegeben) - KEIN "noch offen"."""

    blockiert = _mahnlauf(id=1, status="BLOCKIERT", mahnkosten_verarbeitet_am=None)
    uebersprungen = _mahnlauf(id=2, status="UEBERSPRUNGEN", mahnkosten_verarbeitet_am=None)

    blatt = _leer(mahnlaeufe=[blockiert, uebersprungen])
    assert blatt.ausstehende_mahnlaeufe == []


def test_rest_cent_wird_je_zeile_aus_offenen_forderungen_uebernommen():
    gebuehr_op = _op(2, "SOLL", 2000, quelle_system="mahnkosten")
    buchung = _buchung(gebuehr_op_position_id=2)
    blatt = _leer(
        saldo_cent=2000, positionen_aktiv=[gebuehr_op], positionen_alle=[gebuehr_op],
        mahnkosten_buchungen=[buchung], offene_forderungen_rest_je_id={2: 500},
    )
    assert blatt.kosten_zeilen[0].rest_cent == 500  # teilbezahlt: 1500 von 2000 bereits beglichen


def test_vollstaendig_beglichene_kostenzeile_hat_rest_cent_none():
    gebuehr_op = _op(2, "SOLL", 2000, quelle_system="mahnkosten")
    buchung = _buchung(gebuehr_op_position_id=2)
    # op_position_id 2 taucht NICHT in offene_forderungen_rest_je_id auf,
    # weil `OPService.offene_forderungen` eine vollständig beglichene
    # Forderung gar nicht erst zurückgibt (rest_cent > 0 Filter).
    blatt = _leer(
        saldo_cent=0, positionen_aktiv=[gebuehr_op], positionen_alle=[gebuehr_op],
        mahnkosten_buchungen=[buchung], offene_forderungen_rest_je_id={},
    )
    assert blatt.kosten_zeilen[0].rest_cent is None


def test_offene_ruecklastschrift_ohne_weitere_forderung_zaehlt_als_hauptforderung_offener_rest():
    """Unabhängiger Befund nach Commit 239e33c: eine RUECKLASTSCHRIFT-Zeile
    reinstalliert eine ECHTE Forderung und landet zur ANZEIGE in
    `zahlungen_zeilen` (zusammen mit der Zahlung, die sie storniert) -
    ist sie selbst noch offen, darf dieser Rest NICHT unsichtbar bleiben.
    Wörtliches Beispiel aus der Rückmeldung: nur eine offene
    RUECKLASTSCHRIFT über 1.207,90 EUR - `hauptforderung_offener_rest_cent`
    darf NICHT 0 bleiben."""

    rls = _op(1, "RUECKLASTSCHRIFT", 120_790)
    blatt = _leer(
        saldo_cent=120_790, positionen_aktiv=[rls], positionen_alle=[rls],
        offene_forderungen_rest_je_id={1: 120_790},
    )

    assert len(blatt.zahlungen_zeilen) == 1  # Anzeige unverändert: weiterhin in zahlungen_zeilen
    assert blatt.zahlungen_zeilen[0].rest_cent == 120_790
    assert blatt.zahlungen_zeilen[0].status == STATUS_OFFEN
    assert blatt.zahlungen_saldo_cent == 120_790  # Zahlungs-/Stornostand-Anzeige bleibt unverändert
    assert blatt.hauptforderung_offener_rest_cent == 120_790  # der eigentliche Fehler: war zuvor 0
    assert blatt.hauptforderung_cent + blatt.kosten_cent + blatt.zahlungen_saldo_cent == blatt.saldo_cent


def test_offene_ruecklastschrift_als_kostenzeile_zaehlt_als_kosten_offener_rest():
    """Dieselbe Lücke wie oben, aber für den Fall, dass die offene
    RUECKLASTSCHRIFT-Zeile einer Kostenkategorie zugeordnet ist (direkt
    über `RuecklastschriftkostenTable.kosten_op_position_id`) - muss in
    `kosten_offener_rest_cent`, NICHT in `hauptforderung_offener_rest_cent`,
    landen."""

    rls_kosten_op = _op(3, "RUECKLASTSCHRIFT", 700)
    rls = _ruecklastschrift_kosten(kosten_op_position_id=3)
    blatt = _leer(
        positionen_aktiv=[rls_kosten_op], positionen_alle=[rls_kosten_op], ruecklastschriftkosten_zeilen=[rls],
        offene_forderungen_rest_je_id={3: 700},
    )
    assert blatt.hauptforderung_offener_rest_cent == 0
    assert blatt.kosten_offener_rest_cent == 700


def test_kostenzeile_zeigt_versandnachweis_und_forderung_ids():
    """Zusatz Zinszuordnung: `ledger.versandnachweis_referenz` und
    `ledger.forderung_op_position_ids` sind die bereits bestehenden
    `MahnkostenBuchungTable`-Ledgerdetails - rein durchgereicht, keine
    neue Buchung."""

    gebuehr_op = _op(2, "SOLL", 2000, quelle_system="mahnkosten")
    buchung = _buchung(
        gebuehr_op_position_id=2, versandnachweis_referenz="mahnung:V-1:stufe1:2026-09-15",
        forderung_op_position_ids=json.dumps([100, 101]),
    )
    blatt = _leer(positionen_aktiv=[gebuehr_op], positionen_alle=[gebuehr_op], mahnkosten_buchungen=[buchung])

    zeile = blatt.kosten_zeilen[0]
    assert zeile.ledger is not None
    assert zeile.ledger.versandnachweis_referenz == "mahnung:V-1:stufe1:2026-09-15"
    assert zeile.ledger.forderung_op_position_ids == [100, 101]
    assert zeile.ledger.zins_zuordnung == []  # nur für Verzugszinsen befüllt, nicht für Mahnspesen


def test_zinszeile_trennt_kumulatives_segment_von_tatsaechlich_gebuchtem_delta():
    """Kernregel der Zinszuordnung: das vollständige, KUMULATIVE
    Segment-Bild (`zins_segmente_json`) darf NIE mit dem an diesem Tag
    tatsächlich NEU gebuchten Delta (`zinsen_delta_je_op_json`)
    verwechselt werden - beide müssen UNABHÄNGIG voneinander ablesbar
    sein."""

    zinsen_op = _op(5, "SOLL", 489, quelle_system="mahnkosten")
    segmente = [
        {"op_position_id": 100, "von": "2026-07-01", "bis": "2026-08-31", "rest_cent": 50_000,
         "satz_prozent": "4.000", "quelle": "VEREINBART_GEPRUEFT", "zinsen_cent": 334},
        {"op_position_id": 100, "von": "2026-09-01", "bis": "2026-09-15", "rest_cent": 50_000,
         "satz_prozent": "4.000", "quelle": "VEREINBART_GEPRUEFT", "zinsen_cent": 83},
    ]
    # Kumulativ über beide Segmente: 417 Cent - TATSÄCHLICH an diesem Tag
    # neu gebucht wurden aber nur 83 Cent (die ersten 334 liefen bereits
    # über eine frühere Mahnstufe).
    buchung = _buchung(
        zinsen_op_position_id=5, zins_segmente_json=json.dumps(segmente),
        zinsen_delta_je_op_json=json.dumps({"100": 83}),
    )
    blatt = _leer(positionen_aktiv=[zinsen_op], positionen_alle=[zinsen_op], mahnkosten_buchungen=[buchung])

    zeile = blatt.kosten_zeilen[0]
    assert zeile.kategorie == KATEGORIE_VERZUGSZINSEN
    assert len(zeile.ledger.zins_zuordnung) == 1
    zuordnung = zeile.ledger.zins_zuordnung[0]
    assert zuordnung.op_position_id == 100
    assert len(zuordnung.segmente) == 2  # das vollständige kumulative Bild bleibt sichtbar
    assert sum(s["zinsen_cent"] for s in zuordnung.segmente) == 417  # kumulativ, NICHT das Buchungsdelta
    assert zuordnung.delta_cent == 83  # das TATSÄCHLICH an diesem Tag gebuchte Delta


def test_zinszeile_ohne_gebuchtes_delta_zeigt_ehrlich_nicht_gebucht_statt_erfundener_null():
    """Eine zugrunde liegende Forderung kann im kumulativen Segment-Bild
    auftauchen, ohne dass an DIESEM Tag ein Delta für sie gebucht wurde
    (z. B. weil ihr Anteil bereits vollständig über eine frühere Stufe
    abgerechnet war) - `delta_cent` muss dann `None` sein, NIE 0 EUR
    erfunden."""

    zinsen_op = _op(5, "SOLL", 83, quelle_system="mahnkosten")
    segmente = [
        {"op_position_id": 200, "von": "2026-07-01", "bis": "2026-09-15", "rest_cent": 30_000,
         "satz_prozent": "4.000", "quelle": "VEREINBART_GEPRUEFT", "zinsen_cent": 200},
    ]
    buchung = _buchung(
        zinsen_op_position_id=5, zins_segmente_json=json.dumps(segmente), zinsen_delta_je_op_json=json.dumps({}),
    )
    blatt = _leer(positionen_aktiv=[zinsen_op], positionen_alle=[zinsen_op], mahnkosten_buchungen=[buchung])

    zuordnung = blatt.kosten_zeilen[0].ledger.zins_zuordnung[0]
    assert zuordnung.op_position_id == 200
    assert zuordnung.delta_cent is None  # ehrlich "nicht gebucht", nie 0 erfunden
    assert blatt.kosten_zeilen[0].ledger.zins_nachweis_unvollstaendig is False  # gültige Struktur, nur leeres Delta


def test_zins_segmente_json_mit_falscher_struktur_crasht_nicht_und_markiert_unvollstaendig():
    """Unabhängige Prüfung Commit 8ae0406, (3), echter Bug: bisher wurde
    nur die JSON-SYNTAX geprüft - gültiges JSON mit der FALSCHEN Struktur
    (hier: ein Objekt statt eines Arrays) führte beim Iterieren zu einer
    rohen Exception (500). Jetzt: kein Crash, `zins_nachweis_
    unvollstaendig` zeigt den Defekt ehrlich an, statt ihn zu verschweigen
    oder als "keine Zinsen" zu interpretieren."""

    zinsen_op = _op(5, "SOLL", 83, quelle_system="mahnkosten")
    buchung = _buchung(zinsen_op_position_id=5, zins_segmente_json=json.dumps({"oops": 1}))
    blatt = _leer(positionen_aktiv=[zinsen_op], positionen_alle=[zinsen_op], mahnkosten_buchungen=[buchung])

    ledger = blatt.kosten_zeilen[0].ledger
    assert ledger.zins_zuordnung == []
    assert ledger.zins_nachweis_unvollstaendig is True


def test_zins_segmente_json_mit_null_eintrag_crasht_nicht_und_markiert_unvollstaendig():
    """`[null]` ist gültiges JSON, aber kein Segment-Objekt - darf NICHT
    mit `segment["op_position_id"]` crashen (ursprünglicher Bug: 500)."""

    zinsen_op = _op(5, "SOLL", 83, quelle_system="mahnkosten")
    buchung = _buchung(zinsen_op_position_id=5, zins_segmente_json=json.dumps([None]))
    blatt = _leer(positionen_aktiv=[zinsen_op], positionen_alle=[zinsen_op], mahnkosten_buchungen=[buchung])

    ledger = blatt.kosten_zeilen[0].ledger
    assert ledger.zins_zuordnung == []
    assert ledger.zins_nachweis_unvollstaendig is True


def test_zins_segment_ohne_op_position_id_wird_uebersprungen_und_markiert_unvollstaendig():
    zinsen_op = _op(5, "SOLL", 83, quelle_system="mahnkosten")
    segmente = [
        {"von": "2026-07-01", "bis": "2026-09-15", "rest_cent": 30_000, "satz_prozent": "4.000", "quelle": "X", "zinsen_cent": 100},
        {"op_position_id": 100, "von": "2026-09-16", "bis": "2026-09-30", "rest_cent": 30_000, "satz_prozent": "4.000", "quelle": "X", "zinsen_cent": 50},
    ]
    buchung = _buchung(
        zinsen_op_position_id=5, zins_segmente_json=json.dumps(segmente), zinsen_delta_je_op_json=json.dumps({"100": 50}),
    )
    blatt = _leer(positionen_aktiv=[zinsen_op], positionen_alle=[zinsen_op], mahnkosten_buchungen=[buchung])

    ledger = blatt.kosten_zeilen[0].ledger
    assert ledger.zins_nachweis_unvollstaendig is True  # das Segment ohne op_position_id wurde übersprungen
    assert len(ledger.zins_zuordnung) == 1  # das gültige Segment (op_position_id=100) bleibt erhalten
    assert ledger.zins_zuordnung[0].op_position_id == 100
    assert ledger.zins_zuordnung[0].delta_cent == 50


def test_zinsen_delta_je_op_json_mit_nichtnumerischem_schluessel_crasht_nicht():
    """Ein nicht-numerischer Delta-Schlüssel (`int(...)` würde werfen)
    darf NICHT crashen - wird übersprungen, als unvollständig markiert."""

    zinsen_op = _op(5, "SOLL", 83, quelle_system="mahnkosten")
    buchung = _buchung(zinsen_op_position_id=5, zinsen_delta_je_op_json=json.dumps({"abc": 50}))
    blatt = _leer(positionen_aktiv=[zinsen_op], positionen_alle=[zinsen_op], mahnkosten_buchungen=[buchung])

    ledger = blatt.kosten_zeilen[0].ledger
    assert ledger.zins_zuordnung == []
    assert ledger.zins_nachweis_unvollstaendig is True


def test_kostenzeile_ohne_auffindbare_buchung_hat_kein_ledger():
    """Fehlt ausnahmsweise die zugehörige `MahnkostenBuchungTable`-Zeile
    (z. B. eine fremd/manuell gebuchte Kostenzeile), bleibt `ledger`
    `None` - Anzeige dann ausdrücklich "fehlend", nie ein erfundener
    Beleg. Hier simuliert über eine Rücklastschriftkosten-Zeile (hat
    strukturell ohnehin keinen `MahnkostenBuchungTable`-Bezug)."""

    kosten_op = _op(2, "SOLL", 700, quelle_system="ruecklastschriftkosten")
    rls = _ruecklastschrift_kosten(kosten_op_position_id=2)
    blatt = _leer(positionen_aktiv=[kosten_op], positionen_alle=[kosten_op], ruecklastschriftkosten_zeilen=[rls])

    zeile = blatt.kosten_zeilen[0]
    assert zeile.kategorie == KATEGORIE_RUECKLASTSCHRIFTKOSTEN
    assert zeile.ledger is None


def test_leeres_konto_ergibt_leeres_kontoblatt_ohne_fehler():
    blatt = _leer()
    assert blatt.saldo_cent == 0
    assert blatt.hauptforderung_zeilen == []
    assert blatt.kosten_zeilen == []
    assert blatt.zahlungen_zeilen == []
    assert blatt.dokumentierte_bankaufwaende == []
    assert blatt.ausstehende_mahnlaeufe == []


# -- Integrationstest: `lade_kontoblatt` gegen ECHTE Repositories/eine -----
# -- echte DB (keine synthetischen Objekte mehr) - prüft die tatsächliche --
# -- Verdrahtung, nicht nur die reine Aggregationsfunktion oben. -----------


def test_lade_kontoblatt_gegen_echte_repositories(op_service, admin_ctx, ctx_factory, basis_vertrag, session_factory, stammdaten_repo):
    from mietinkasso.bank.importer import CsvSpaltenMapping
    from mietinkasso.bank.repository import BankRepository
    from mietinkasso.bank.ruecklastschriftkosten_service import RuecklastschriftkostenRepository, RuecklastschriftkostenService
    from mietinkasso.bank.service import BankImportService
    from mietinkasso.domain.enums import OPTyp
    from mietinkasso.mahnwesen.kosten_repository import MahnkostenRepository
    from mietinkasso.mahnwesen.kosten_service import MahnkostenService
    from mietinkasso.mahnwesen.repository import MahnLaufRepository
    from mietinkasso.op.kontoblatt import lade_kontoblatt

    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")

    # Hauptforderung: eine gewöhnliche Miet-SOLL-Zeile.
    op_service.buchen(
        ctx=admin_ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=50_000,
        belegdatum=date(2026, 1, 1), buchungsdatum=date(2026, 1, 1),
        faelligkeit=date(2026, 1, 5), beleg_referenz="HMZ Jänner",
    )

    # Echte Verzugszinsen-Buchung über den tatsächlichen Mahnkosten-Service.
    mahnkosten_repo = MahnkostenRepository(session_factory)
    mahnkosten_service = MahnkostenService(mahnkosten_repo, op_service, stammdaten_repo)
    gebucht = mahnkosten_service.buche_bei_versand(
        ctx=admin_ctx, vertrag_id=vertrag.id, stufe=1, heute=date(2026, 3, 1),
        versandnachweis_referenz="mahnung:kontoblatt-test-1", akteur="codex-test",
    )
    assert gebucht is not None and gebucht.zinsen_op_position_id is not None

    # Echte Rücklastschriftkosten: eine gebuchte, eine rein dokumentarische.
    bank_repo = BankRepository(session_factory)
    bank_service = BankImportService(bank_repo, stammdaten_repo, op_service)
    rls_kosten_repo = RuecklastschriftkostenRepository(session_factory)
    rls_kosten_service = RuecklastschriftkostenService(session_factory=session_factory, op_service=op_service, repository=rls_kosten_repo)

    bank_repo.upsert_bank_konto(id="BK-7DI-KONTOBLATT", gesellschaft_id="7DI", iban="AT000000000000000001", bezeichnung="7DI")
    bank_konto = bank_repo.get_bank_konto("BK-7DI-KONTOBLATT")
    mapping = CsvSpaltenMapping(betrag="betrag", buchungsdatum="datum", referenz="referenz")

    def _neue_ruecklastschrift(betrag_eur: str, zahlungsdatum: str, rueckdatum: str, referenz_suffix: str):
        zahl_csv = f"betrag,datum,referenz\n{betrag_eur},{zahlungsdatum},VERTRAG:{vertrag.id} {referenz_suffix}\n"
        zahl_tx = bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=zahl_csv, mapping=mapping)[0]
        ergebnis = bank_service.automatisch_zuordnen(ctx=ctx, transaktion=zahl_tx)
        assert ergebnis.zugeordnet is True
        zahlung_op = op_service.get_position(ergebnis.op_position_id)
        rueck_csv = f"betrag,datum,referenz\n-{betrag_eur},{rueckdatum},RUECKLASTSCHRIFT VERTRAG:{vertrag.id} {referenz_suffix}\n"
        rueck_tx = bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=rueck_csv, mapping=mapping)[0]
        return bank_service.verarbeite_ruecklastschrift(ctx=ctx, transaktion=rueck_tx, original_op_position=zahlung_op, konto=konto)

    rls_op_gebucht = _neue_ruecklastschrift("200.00", "2026-04-06", "2026-04-10", "A")
    rls_kosten_service.erfasse_und_buche(
        ctx=ctx, ruecklastschrift_op_position=rls_op_gebucht, vertrag_id=vertrag.id, konto=konto,
        bankentgelt_cent=700, rueckgabeursache="Mangels Deckung", dem_zahlungspflichtigen_zurechenbar=True,
        zurechenbarkeit_begruendung="Dauerhaft fehlende Kontodeckung, vom Mieter zu vertreten.",
        beleg_referenz="Bankbeleg KONTOBLATT-A", heute=date(2026, 4, 11), akteur="codex-test",
    )
    rls_op_dokumentiert = _neue_ruecklastschrift("150.00", "2026-05-06", "2026-05-10", "B")
    rls_kosten_service.erfasse_und_buche(
        ctx=ctx, ruecklastschrift_op_position=rls_op_dokumentiert, vertrag_id=vertrag.id, konto=konto,
        bankentgelt_cent=700, rueckgabeursache="Unklar - wird geprüft", dem_zahlungspflichtigen_zurechenbar=False,
        zurechenbarkeit_begruendung="Zurechenbarkeit (noch) nicht belegt, rechtlich in Prüfung.",
        beleg_referenz="Bankbeleg KONTOBLATT-B", heute=date(2026, 5, 11), akteur="codex-test",
    )

    # Ein ausstehender (noch nicht kostenverarbeiteter) Mahnlauf.
    mahnlauf_repo = MahnLaufRepository(session_factory)
    mahnlauf_repo.get_or_create(
        outbox_key="KONTOBLATT-AUSSTEHEND", vertrag_id=vertrag.id, gesellschaft_id="7DI", stufe=2,
        mitglieder_mahnfall_ids="[]", kanal="BRIEF", status="UNSICHER",
    )

    blatt = lade_kontoblatt(
        konto_id=konto.id, vertrag_id=vertrag.id, op_service=op_service, mahnkosten_repo=mahnkosten_repo,
        ruecklastschriftkosten_repo=rls_kosten_repo, mahnlauf_repo=mahnlauf_repo,
    )

    assert blatt.hauptforderung_cent == 50_000
    kategorien = {z.kategorie for z in blatt.kosten_zeilen}
    assert KATEGORIE_VERZUGSZINSEN in kategorien
    assert KATEGORIE_RUECKLASTSCHRIFTKOSTEN in kategorien
    assert len(blatt.dokumentierte_bankaufwaende) == 1
    assert blatt.dokumentierte_bankaufwaende[0].row.bankentgelt_cent == 700
    assert len(blatt.ausstehende_mahnlaeufe) == 1
    assert blatt.ausstehende_mahnlaeufe[0].outbox_key == "KONTOBLATT-AUSSTEHEND"
    # Zentrale Invariante: keine Verdopplung - Summe aller Kategorien ==
    # exakt der echte, bereits etablierte `OPService.berechne_saldo`-Saldo.
    echter_saldo = op_service.berechne_saldo(konto.id).saldo_cent
    assert blatt.saldo_cent == echter_saldo
    assert blatt.hauptforderung_cent + blatt.kosten_cent + blatt.zahlungen_saldo_cent == echter_saldo


# -- EIN gemeinsames chronologisches Journal (Auftrag HV-20261022-EIN- -----
# -KONTOBLATT) - unabhängige QA-Baseline, wörtliches Zahlenbeispiel: SYNRENT
# (+1000 am 1.4.), SYNPAY (-400 am 2.4.), SYNFEE (+20 am 3.4., ein
# tatsächlich versendetes, kostenfreigegebenes Mahnschreiben), SYNBANK (+7
# am 4.4., geprüft/zugerechnete Rücklastschriftkosten), SYNINT (+0.60 am
# 5.4., gebuchtes Zinsdelta) -> erwarteter Schlussaldo 627,60 EUR.


def test_journal_synthetisches_zahlenbeispiel_ergibt_schlussaldo_627_60():
    rent = _op(1, "SOLL", 100_000, belegdatum=date(2026, 4, 1), beleg_referenz="SYNRENT")
    pay = _op(2, "ZAHLUNG", 40_000, belegdatum=date(2026, 4, 2), beleg_referenz="SYNPAY")
    fee = _op(3, "SOLL", 2_000, belegdatum=date(2026, 4, 3), beleg_referenz="SYNFEE")
    bank = _op(4, "SOLL", 700, belegdatum=date(2026, 4, 4), beleg_referenz="SYNBANK")
    zins = _op(5, "SOLL", 60, belegdatum=date(2026, 4, 5), beleg_referenz="SYNINT")

    # SYNFEE+SYNINT stammen aus DEMSELBEN tatsächlichen Mahnversand
    # (ein Mahnlauf kann Zinsen UND Gebühr gleichzeitig buchen) - EIN
    # `MahnkostenBuchungTable`-Beleg mit beiden Referenzen, wie es
    # `kosten_service.py::buche_vorschau` auch tatsächlich anlegt.
    buchung = _buchung(gebuehr_op_position_id=3, zinsen_op_position_id=5, versandnachweis_referenz="mahnung:V-601-1:stufe1:2026-04-05")
    # SYNBANK: geprüfte/zugerechnete Rücklastschriftkosten - ECHTE
    # Forderung gebucht (kosten_op_position_id gesetzt), NICHT nur
    # dokumentarisch.
    bank_kosten = _ruecklastschrift_kosten(kosten_op_position_id=4, ruecklastschrift_op_position_id=99, bankentgelt_cent=700)

    blatt = _leer(
        saldo_cent=100_000 - 40_000 + 2_000 + 700 + 60,
        positionen_aktiv=[rent, pay, fee, bank, zins], positionen_alle=[rent, pay, fee, bank, zins],
        mahnkosten_buchungen=[buchung], ruecklastschriftkosten_zeilen=[bank_kosten],
    )
    assert blatt.saldo_cent == 62_760  # 627,60 EUR

    journal = erstelle_journal(blatt)
    # EIN gemeinsames Journal - alle fünf Buchungsarten in EINER Liste,
    # chronologisch, keine Kategorie-Gruppierung.
    assert [jz.beleg_referenz for jz in journal] == ["SYNRENT", "SYNPAY", "SYNFEE", "SYNBANK", "SYNINT"]
    assert [jz.datum for jz in journal] == [date(2026, 4, d) for d in (1, 2, 3, 4, 5)]
    # Soll/Haben korrekt verteilt (Miete/Gebühr/Bankkosten/Zinsen Soll,
    # Zahlung Haben).
    assert [(jz.soll_cent, jz.haben_cent) for jz in journal] == [
        (100_000, 0), (0, 40_000), (2_000, 0), (700, 0), (60, 0),
    ]
    # Laufender Saldo wächst/schrumpft Zeile für Zeile und endet exakt
    # beim Schlussaldo - KEINE zweite, abweichende Berechnung.
    assert [jz.laufender_saldo_cent for jz in journal] == [100_000, 60_000, 62_000, 62_700, 62_760]
    assert journal[-1].laufender_saldo_cent == blatt.saldo_cent == 62_760
    assert all(jz.beeinflusst_saldo for jz in journal)  # keine Historie-Zeile in diesem Beispiel


def test_journal_html_und_pdf_zeigen_identischen_schlussaldo_und_reihenfolge():
    """Dasselbe Zahlenbeispiel wie oben - HTML-Rendering (Kontoansicht/
    Mieterakte, identische Funktion) UND PDF-Rendering lesen beide
    AUSSCHLIESSLICH `erstelle_journal(blatt)` - niemals eine zweite,
    unabhängig berechnete Darstellung."""

    from mietinkasso.backoffice.kontoblatt_render import render_kontoblatt_html
    from mietinkasso.mahnwesen.brief_pdf import Absender
    from mietinkasso.op.kontoauszug_pdf import erzeuge_kontoauszug_pdf
    from pypdf import PdfReader
    import io

    rent = _op(1, "SOLL", 100_000, belegdatum=date(2026, 4, 1), beleg_referenz="SYNRENT")
    pay = _op(2, "ZAHLUNG", 40_000, belegdatum=date(2026, 4, 2), beleg_referenz="SYNPAY")
    fee = _op(3, "SOLL", 2_000, belegdatum=date(2026, 4, 3), beleg_referenz="SYNFEE")
    bank = _op(4, "SOLL", 700, belegdatum=date(2026, 4, 4), beleg_referenz="SYNBANK")
    zins = _op(5, "SOLL", 60, belegdatum=date(2026, 4, 5), beleg_referenz="SYNINT")
    buchung = _buchung(gebuehr_op_position_id=3, zinsen_op_position_id=5, versandnachweis_referenz="mahnung:V-601-1:stufe1:2026-04-05")
    bank_kosten = _ruecklastschrift_kosten(kosten_op_position_id=4, ruecklastschrift_op_position_id=99, bankentgelt_cent=700)
    blatt = _leer(
        saldo_cent=62_760, positionen_aktiv=[rent, pay, fee, bank, zins], positionen_alle=[rent, pay, fee, bank, zins],
        mahnkosten_buchungen=[buchung], ruecklastschriftkosten_zeilen=[bank_kosten],
    )

    html = render_kontoblatt_html(blatt, konto_id="K-1")
    for marker in ("SYNRENT", "SYNPAY", "SYNFEE", "SYNBANK", "SYNINT"):
        assert marker in html
    assert "627,60" in html  # Schlussaldo/Gesamtsaldo

    absender = Absender(
        name="JLB Projects GmbH", adresse="Marc-Aurel-Straße 4/16, 1010 Wien",
        fn="FN 631126b, Handelsgericht Wien", uid="ATU81269707", telefon="+43 1 435 10 11",
        website="jlb-immo.at", email="hausverwaltung@jlb-immo.at",
        farbe_anthrazit="#1F2125", farbe_gold="#C9A86A",
    )
    pdf_bytes = erzeuge_kontoauszug_pdf(
        absender=absender, objekt_bezeichnung="Am Corso, Top 3", einheit_bezeichnung="", debitor_name="Max Mustermieter",
        vertrag_id="V-601-1", konto_id="K-1", heute=date(2026, 4, 5), kontoblatt=blatt,
    )
    pdf_text = " ".join("".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf_bytes)).pages).split())
    for marker in ("SYNRENT", "SYNPAY", "SYNFEE", "SYNBANK", "SYNINT"):
        assert marker in pdf_text
    assert "627,60" in pdf_text
    # Chronologische Reihenfolge auch im PDF (nicht mehr Kategorie-
    # Reihenfolge Miete/Fee/Bank/Zins/Zahlung) - jeder Marker erscheint
    # strikt NACH dem vorigen.
    positionen = [pdf_text.index(m) for m in ("SYNRENT", "SYNPAY", "SYNFEE", "SYNBANK", "SYNINT")]
    assert positionen == sorted(positionen)


# -- Auftrag HV-20261023-MAHNUNG-EINFACH: `erstelle_einfache_tabelle`/ ----
# `erweitere_um_vorschau` - wörtliches Zahlenbeispiel aus dem Auftrag.


def test_erstelle_einfache_tabelle_woertliches_zahlenbeispiel_gesamt_offen_1029():
    """Wörtliches Beispiel: Ausgangssaldo 0; Miete 1000; Zahlung -1000;
    Rückbuchung +1000; geprüfte, bereits GEBUCHTE Bankspesen +7
    (`ruecklastschriftkosten_zeilen`); zulässige, bereits GEBUCHTE
    Mahngebühr +20 und Verzugszinsen +2 (`mahnkosten_buchungen`) ->
    Gesamt offen 1.029,00 EUR - reine Umformung von `erstelle_journal`,
    keine zweite Berechnung/Engine."""

    miete = _op(1, "SOLL", 100_000, belegdatum=date(2026, 1, 1))
    zahlung = _op(2, "ZAHLUNG", 100_000, belegdatum=date(2026, 1, 5))
    # `bezieht_sich_auf_id` bindet die Rücklastschrift an die ursprüngliche
    # Zahlung (05.01.) - der Buchungstext muss DIESES Datum nennen, nicht
    # ihr eigenes Rückbuchungsdatum (10.01., siehe Gegenprobe unten).
    rueckbuchung = _op(3, "RUECKLASTSCHRIFT", 100_000, belegdatum=date(2026, 1, 10), bezieht_sich_auf_id=2)
    bankspesen = _op(4, "SOLL", 700, belegdatum=date(2026, 1, 11))
    gebuehr = _op(5, "SOLL", 2_000, belegdatum=date(2026, 1, 12))
    zinsen = _op(6, "SOLL", 200, belegdatum=date(2026, 1, 12))
    buchung = _buchung(gebuehr_op_position_id=5, zinsen_op_position_id=6)
    bank_kosten = _ruecklastschrift_kosten(kosten_op_position_id=4, ruecklastschrift_op_position_id=3, bankentgelt_cent=700)

    blatt = _leer(
        saldo_cent=102_900, positionen_aktiv=[miete, zahlung, rueckbuchung, bankspesen, gebuehr, zinsen],
        positionen_alle=[miete, zahlung, rueckbuchung, bankspesen, gebuehr, zinsen],
        mahnkosten_buchungen=[buchung], ruecklastschriftkosten_zeilen=[bank_kosten],
    )
    zeilen = erstelle_einfache_tabelle(blatt)

    # Ausgangssaldo als erste Zeile (keine EROEFFNUNG-Buchung vorhanden ->
    # synthetische 0,00-Zeile, "keine verlorene Ausgangslage").
    assert zeilen[0].buchung == "Ausgangssaldo" and zeilen[0].betrag_cent == 0 and zeilen[0].offen_cent == 0
    assert [z.offen_cent for z in zeilen] == [0, 100_000, 0, 100_000, 100_700, 102_700, 102_900]
    assert zeilen[-1].offen_cent == 102_900  # 1.029,00 EUR
    assert zeilen[-1].offen_cent == blatt.saldo_cent  # keine zweite, abweichende Summe
    # Miete/Rückbuchung/Bankspesen/Gebühr/Zinsen bleiben jeweils als
    # eigene, menschlich benannte Zeile erkennbar - KEINE Verschmelzung
    # zu einer einzigen Sammelzeile.
    buchungstexte = [z.buchung for z in zeilen]
    assert any("Mietvorschreibung" in b for b in buchungstexte)
    assert any(b == "Zahlung" for b in buchungstexte)
    # Korrektur (Codex 23.10.2026, Gegenlesen von d52948f): der Text
    # nennt jetzt das Datum der ZAHLUNG (05.01.), auf die sich die
    # Rücklastschrift über `bezieht_sich_auf_id` bezieht - NICHT ihr
    # eigenes Rückbuchungsdatum (10.01.). Die Zeilen-eigene Datumsspalte
    # bleibt unverändert das Rückbuchungsdatum.
    assert any(b == "Rückbuchung der Zahlung vom 05.01.2026" for b in buchungstexte)
    rueckbuchungs_zeile = next(z for z in zeilen if z.buchung.startswith("Rückbuchung der Zahlung"))
    assert rueckbuchungs_zeile.datum == date(2026, 1, 10)
    # Die gebuchte Rücklastschriftkosten-Zeile behauptet KEIN eigenes
    # Buchungsdatum mehr als Rückbuchungsdatum (siehe Moduldoc-Korrektur).
    assert any(b == "Bankspesen Rücklastschrift" for b in buchungstexte)
    assert any("Mahngebühr" in b for b in buchungstexte)
    assert any("Verzugszinsen" in b for b in buchungstexte)


def test_erweitere_um_vorschau_haengt_neue_zinsen_und_gebuehr_einmal_an_ohne_db_schreibung():
    """Nachtrag Markus 23.10.2026 (Abnahme-Hinweis): hängt GENAU EINMAL
    einen noch NICHT gebuchten Zinsen-/Gebührenposten an - rein
    darstellungsseitig (reine Python-Funktion, es gibt hier keine DB,
    die Prüfung "ohne DB-Buchung" ist also durch die reine Signatur
    selbst belegt: weder `erstelle_einfache_tabelle` noch
    `erweitere_um_vorschau` nehmen ein Session-/Repository-Objekt
    entgegen)."""

    basis = [EinfacheZeile(datum=date(2026, 1, 1), buchung="Mietvorschreibung Jänner 2026", betrag_cent=100_000, offen_cent=100_000)]

    erweitert = erweitere_um_vorschau(
        basis, heute=date(2026, 1, 20), neue_zinsen_delta_cent=200, zins_von=date(2026, 1, 6), zins_bis=date(2026, 1, 20),
        zinssatz_prozent=4, gebuehr_cent=2_000,
    )

    # Die ursprüngliche Liste bleibt unverändert (reine Funktion).
    assert basis == [EinfacheZeile(datum=date(2026, 1, 1), buchung="Mietvorschreibung Jänner 2026", betrag_cent=100_000, offen_cent=100_000)]
    assert len(erweitert) == 3  # Basis + Zinsen + Gebühr
    neue = erweitert[1:]
    assert all(z.ist_vorschau for z in neue)
    assert erweitert[-1].offen_cent == 102_200  # 100.000 + 200 + 2.000
    assert neue[0].betrag_cent == 200 and neue[1].betrag_cent == 2_000


def test_erweitere_um_vorschau_ohne_neue_posten_liefert_liste_unveraendert():
    basis = [EinfacheZeile(datum=date(2026, 1, 1), buchung="Mietvorschreibung Jänner 2026", betrag_cent=100_000, offen_cent=100_000)]
    erweitert = erweitere_um_vorschau(basis, heute=date(2026, 1, 20))
    assert erweitert == basis
    assert not any(z.ist_vorschau for z in erweitert)


# -- Gegenlesen von Commit d52948f (Codex, 23.10.2026): drei echte --------
# Fachfehler in den neuen `erstelle_einfache_tabelle`/`erweitere_um_
# vorschau`-Hilfsfunktionen - gezielte Gegenproben.


def test_verzugszinsen_zeitraum_zeigt_letzten_verzinsten_tag_nicht_exklusives_enddatum():
    """`zins_bis` ist EXKLUSIV (siehe `kosten.py::zins_bis_einschliesslich`-
    Docstring) - ein Zeitraum 04.–08. (exklusiv) verzinst tatsächlich NUR
    bis einschließlich 07. Die vorherige Fassung zeigte das exklusive
    Enddatum unverändert (fälschlich "...–08."). Gilt für BEIDE Quellen:
    eine bereits GEBUCHTE Zinszeile (Ledger) UND einen noch NICHT
    gebuchten Vorschau-Posten (`erweitere_um_vorschau`)."""

    miete = _op(1, "SOLL", 100_000, belegdatum=date(2026, 1, 1))
    zinsen = _op(2, "SOLL", 300, belegdatum=date(2026, 1, 9))
    buchung = _buchung(zinsen_op_position_id=2, zins_von=date(2026, 1, 4), zins_bis=date(2026, 1, 8), zinssatz_prozent=4)
    blatt = _leer(
        saldo_cent=100_300, positionen_aktiv=[miete, zinsen], positionen_alle=[miete, zinsen],
        mahnkosten_buchungen=[buchung],
    )
    zeilen = erstelle_einfache_tabelle(blatt)
    zins_zeile = next(z for z in zeilen if "Verzugszinsen" in z.buchung)
    assert "04.01.2026–07.01.2026" in zins_zeile.buchung
    assert "04.01.2026–08.01.2026" not in zins_zeile.buchung

    # Dieselbe Korrektur für den noch NICHT gebuchten Vorschau-Posten.
    erweitert = erweitere_um_vorschau(
        zeilen, heute=date(2026, 1, 20), neue_zinsen_delta_cent=50,
        zins_von=date(2026, 1, 9), zins_bis=date(2026, 1, 13), zinssatz_prozent=4,
    )
    vorschau_zeile = next(z for z in erweitert if z.ist_vorschau)
    assert "09.01.2026–12.01.2026" in vorschau_zeile.buchung
    assert "09.01.2026–13.01.2026" not in vorschau_zeile.buchung


def test_ruecklastschrift_ohne_bezieht_sich_auf_id_bleibt_neutral_statt_falsches_datum():
    """Fehlt die `bezieht_sich_auf_id`-Bindung (z. B. eine isoliert
    gebuchte Rücklastschrift ohne nachvollziehbare ursprüngliche
    Zahlung), bleibt der Buchungstext NEUTRAL - NIE ein erfundenes/
    falsches Bezugsdatum. Die Datumsspalte der Zeile bleibt trotzdem
    korrekt das tatsächliche Rückbuchungsdatum."""

    rueckbuchung = _op(1, "RUECKLASTSCHRIFT", 50_000, belegdatum=date(2026, 4, 10), bezieht_sich_auf_id=None)
    blatt = _leer(saldo_cent=50_000, positionen_aktiv=[rueckbuchung], positionen_alle=[rueckbuchung])
    zeilen = erstelle_einfache_tabelle(blatt)
    zeile = next(z for z in zeilen if "Rückbuchung" in z.buchung)
    assert zeile.buchung == "Rückbuchung einer Zahlung"
    assert zeile.datum == date(2026, 4, 10)


def test_ruecklastschrift_mit_nicht_aufloesbarer_bezieht_sich_auf_id_bleibt_neutral():
    """Die Bindung verweist auf eine ID, die im Kontoblatt gar nicht
    vorkommt (z. B. eine andere, hier nicht geladene Position) -
    bleibt ebenfalls neutral statt mit einer falschen Position zu raten."""

    rueckbuchung = _op(1, "RUECKLASTSCHRIFT", 50_000, belegdatum=date(2026, 4, 10), bezieht_sich_auf_id=999)
    blatt = _leer(saldo_cent=50_000, positionen_aktiv=[rueckbuchung], positionen_alle=[rueckbuchung])
    zeilen = erstelle_einfache_tabelle(blatt)
    zeile = next(z for z in zeilen if "Rückbuchung" in z.buchung)
    assert zeile.buchung == "Rückbuchung einer Zahlung"


def test_verzugszinsen_zeile_zeigt_zinsbasis_satz_und_zeitraum():
    """Ergänzender fachlicher Abnahmehinweis Markus 23.10.2026: die kurze
    Zinszeile muss neben Zeitraum/Satz auch die tatsächliche Zinsbasis
    erkennen lassen, z. B. "4% aus 1.000,00 EUR Miete September 2026,
    04.–07.09.2026" - ausschließlich aus bereits vorhandenen,
    strukturierten Zinssegmenten/Restbeträgen/Forderungsperioden
    (`MahnkostenBuchungTable.zins_segmente_json`/`zinsen_delta_je_op_
    json`), keine neue Berechnung, keine erfundene Zuordnung, keine OP-Id
    im Text."""

    miete = _op(1, "SOLL", 100_000, belegdatum=date(2026, 9, 1))
    miete.leistungsperiode = "2026-09"
    zinsen = _op(2, "SOLL", 40, belegdatum=date(2026, 9, 8))
    segmente = [{
        "op_position_id": 1, "von": "2026-09-04", "bis": "2026-09-08",
        "satz_prozent": 4, "rest_cent": 100_000, "zinsen_cent": 40,
    }]
    buchung = _buchung(
        zinsen_op_position_id=2, zins_segmente_json=json.dumps(segmente),
        zinsen_delta_je_op_json=json.dumps({"1": 40}),
    )
    blatt = _leer(
        saldo_cent=100_040, positionen_aktiv=[miete, zinsen], positionen_alle=[miete, zinsen],
        mahnkosten_buchungen=[buchung],
    )
    zeilen = erstelle_einfache_tabelle(blatt)
    zins_zeile = next(z for z in zeilen if "Verzugszinsen" in z.buchung)
    assert zins_zeile.buchung == "Verzugszinsen 4% aus 1.000,00 EUR Miete September 2026, 04.09.2026–07.09.2026"
    assert "#" not in zins_zeile.buchung and "op_position" not in zins_zeile.buchung.lower()


def test_verzugszinsen_zeile_zeigt_mehrere_unterschiedliche_grundlagen_getrennt():
    """"Bei mehreren unterschiedlichen Grundlagen kurz getrennt je
    Basis/Zeitraum innerhalb derselben Tabelle ausweisen, Summe
    unverändert" - zwei verschiedene Mietperioden tragen zu DERSELBEN
    Zinsbuchung bei, mit unterschiedlichem Satz/Zeitraum/Restbetrag -
    beide erscheinen als getrennte Klauseln in EINER Zeile, die Summe
    (Betrag-Spalte) bleibt unverändert die tatsächlich gebuchte Summe."""

    miete_august = _op(1, "SOLL", 50_000, belegdatum=date(2026, 8, 1))
    miete_august.leistungsperiode = "2026-08"
    miete_september = _op(2, "SOLL", 100_000, belegdatum=date(2026, 9, 1))
    miete_september.leistungsperiode = "2026-09"
    zinsen = _op(3, "SOLL", 70, belegdatum=date(2026, 9, 8))
    segmente = [
        {"op_position_id": 1, "von": "2026-09-01", "bis": "2026-09-08", "satz_prozent": 4, "rest_cent": 50_000, "zinsen_cent": 30},
        {"op_position_id": 2, "von": "2026-09-04", "bis": "2026-09-08", "satz_prozent": 4, "rest_cent": 100_000, "zinsen_cent": 40},
    ]
    buchung = _buchung(
        zinsen_op_position_id=3, zins_segmente_json=json.dumps(segmente),
        zinsen_delta_je_op_json=json.dumps({"1": 30, "2": 40}),
    )
    blatt = _leer(
        saldo_cent=150_070, positionen_aktiv=[miete_august, miete_september, zinsen],
        positionen_alle=[miete_august, miete_september, zinsen], mahnkosten_buchungen=[buchung],
    )
    zeilen = erstelle_einfache_tabelle(blatt)
    zins_zeile = next(z for z in zeilen if "Verzugszinsen" in z.buchung)
    assert "500,00 EUR Miete August 2026" in zins_zeile.buchung
    assert "1.000,00 EUR Miete September 2026" in zins_zeile.buchung
    assert zins_zeile.buchung.count(";") == 1  # genau zwei getrennte Klauseln
    assert zins_zeile.betrag_cent == 70  # Summe unverändert, keine zweite Berechnung


def test_verzugszinsen_zeile_zeigt_zinsnachtrag_bei_kumulativem_segment_ueber_bereits_gebuchtem_delta():
    """Quellabnahme 3c6c175 (echter Fachfehler): das KUMULATIVE Segment-
    Bild (hier 0,44 EUR für 04.–07., danach 0,55 EUR für 04.–08.) darf
    NICHT so dargestellt werden, als gehörte die gesamte Periode zur
    NEUEN Delta-Buchung (hier nur 0,11 EUR). Erwartet: "Zinsnachtrag:
    insgesamt 0,55 EUR, davon 0,44 EUR bereits gebucht; neu 0,11 EUR"
    plus Basis/Satz/Zeitraum - alle Werte aus dem vorhandenen Snapshot,
    keine neue Berechnung."""

    miete = _op(1, "SOLL", 100_000, belegdatum=date(2026, 9, 1))
    miete.leistungsperiode = "2026-09"
    zinsen = _op(2, "SOLL", 11, belegdatum=date(2026, 9, 9))
    segmente = [{
        "op_position_id": 1, "von": "2026-09-04", "bis": "2026-09-08",
        "satz_prozent": "4.000", "rest_cent": 100_000, "zinsen_cent": 55,
    }]
    buchung = _buchung(
        zinsen_op_position_id=2, zins_segmente_json=json.dumps(segmente),
        zinsen_delta_je_op_json=json.dumps({"1": 11}),
    )
    blatt = _leer(
        saldo_cent=100_011, positionen_aktiv=[miete, zinsen], positionen_alle=[miete, zinsen],
        mahnkosten_buchungen=[buchung],
    )
    zeilen = erstelle_einfache_tabelle(blatt)
    zins_zeile = next(z for z in zeilen if "Verzugszinsen" in z.buchung)
    assert zins_zeile.buchung == (
        "Verzugszinsen Zinsnachtrag: insgesamt 0,55 EUR, davon 0,44 EUR bereits gebucht; neu 0,11 EUR "
        "(4% aus 1.000,00 EUR Miete September 2026, 04.09.2026–07.09.2026)"
    )
    assert zins_zeile.betrag_cent == 11  # tatsächlich gebuchtes Delta, keine zweite Berechnung


def test_satz_text_formatiert_decimal_menschlich_lesbar_ohne_unnoetige_nachkommastellen():
    """Nachtrag Markus 23.10.2026: `Decimal`-Zinssätze mit fester DB-
    Nachkommaskala (`Decimal('4.000')`) dürfen NICHT unverändert als
    "4.000%" angezeigt werden (liesse sich als "viertausend Prozent"
    missverstehen) - Zahlenwert bleibt unverändert, nur die Darstellung
    wird korrigiert (Dezimalkomma, keine unnötigen Nachkommanullen,
    keine Exponentialschreibweise für runde Werte)."""

    assert _satz_text(Decimal("4.000")) == "4"
    assert _satz_text(Decimal("4.300")) == "4,3"
    assert _satz_text(Decimal("100.00")) == "100"
    assert _satz_text(4) == "4"


def test_verzugszinsen_vorschau_zeile_zeigt_denselben_zinsnachtrag_wie_gebuchte_zeile():
    """"Gebuchte und neue Vorschau müssen denselben Fall korrekt
    erklären": derselbe 44/55/11-Cent-Fall wie oben, aber noch NICHT
    gebucht (`erweitere_um_vorschau` mit den strukturierten Vorschau-
    Feldern `zins_segmente`/`neue_zinsen_delta_je_op_position`, exakt
    wie `MahnkostenVorschau` sie bereits liefert) - muss denselben
    "Zinsnachtrag"-Text wie die gebuchte Ledgerzeile zeigen, keine zweite
    Berechnung."""

    segmente = (
        ZinsSegment(
            op_position_id=1, von=date(2026, 9, 4), bis=date(2026, 9, 8),
            rest_cent=100_000, satz_prozent=Decimal("4.000"), quelle="VEREINBART_GEPRUEFT", zinsen_cent=55,
        ),
    )
    erweitert = erweitere_um_vorschau(
        [], heute=date(2026, 9, 9), neue_zinsen_delta_cent=11,
        zins_segmente=segmente, neue_zinsen_delta_je_op_position={1: 11},
    )
    vorschau_zeile = next(z for z in erweitert if z.ist_vorschau)
    assert vorschau_zeile.buchung == (
        "Verzugszinsen Zinsnachtrag: insgesamt 0,55 EUR, davon 0,44 EUR bereits gebucht; neu 0,11 EUR "
        "(4% aus 1.000,00 EUR, 04.09.2026–07.09.2026) (mit diesem Schreiben)"
    )
    assert vorschau_zeile.betrag_cent == 11


def test_verzugszinsen_vorschau_zeile_ohne_vorbetrag_bleibt_einfache_basiszeile():
    """"Bei erster Buchung ohne Vorbetrag reicht die kurze Basiszeile":
    kumulativ == Delta (keine Vorbuchung) -> unverändert die einfache
    Basiszeile, kein erzwungener "Zinsnachtrag"-Text."""

    segmente = (
        ZinsSegment(
            op_position_id=1, von=date(2026, 9, 4), bis=date(2026, 9, 8),
            rest_cent=100_000, satz_prozent=Decimal("4.000"), quelle="VEREINBART_GEPRUEFT", zinsen_cent=44,
        ),
    )
    erweitert = erweitere_um_vorschau(
        [], heute=date(2026, 9, 9), neue_zinsen_delta_cent=44,
        zins_segmente=segmente, neue_zinsen_delta_je_op_position={1: 44},
    )
    vorschau_zeile = next(z for z in erweitert if z.ist_vorschau)
    assert vorschau_zeile.buchung == "Verzugszinsen 4% aus 1.000,00 EUR, 04.09.2026–07.09.2026 (mit diesem Schreiben)"
    assert "Zinsnachtrag" not in vorschau_zeile.buchung


def test_verzugszinsen_vorschau_zeile_ohne_strukturierte_segmente_faellt_auf_skalar_zurueck():
    """Fehlen die neuen strukturierten Felder (älterer Aufrufer, nur die
    bisherigen Skalare), bleibt der bisherige Skalar-Fallback wirksam -
    inklusive der korrigierten, menschlich lesbaren Prozent-Darstellung
    (`_satz_text`, kein "4.000%" mehr)."""

    erweitert = erweitere_um_vorschau(
        [], heute=date(2026, 9, 9), neue_zinsen_delta_cent=44,
        zins_von=date(2026, 9, 4), zins_bis=date(2026, 9, 8), zinssatz_prozent=Decimal("4.000"),
    )
    vorschau_zeile = next(z for z in erweitert if z.ist_vorschau)
    assert vorschau_zeile.buchung == "Verzugszinsen 04.09.2026–07.09.2026, 4% (mit diesem Schreiben)"


def test_hauptforderung_komponente_erkennt_altimport_schema_hauptmiete():
    """Nachtrag Markus 23.10.2026 (Sichtabnahme fdda658): der bekannte
    Altimport-Schematyp "HV VS 9/2026; Hauptmietzins 10 % Mwst; J.12345"
    muss als "Hauptmiete" erkannt werden, OHNE die rohe Belegreferenz
    (Präfix "HV VS 9/2026", Steuerhinweis, Belegkennung "J.12345")
    irgendwo im Ergebnis zu zeigen."""

    p = _op(1, "SOLL", 100_000, beleg_referenz="HV VS 9/2026; Hauptmietzins 10 % Mwst; J.12345")
    p.leistungsperiode = "2026-09"
    assert _hauptforderung_komponente_text(p) == "Hauptmiete September 2026"


def test_hauptforderung_komponente_erkennt_bekannte_whitelist_komponenten_im_hv_vs_bezeichnungsfeld():
    """Originalauftrag: "Miete/BK/Küche/Parkplatz fachlich erkennbar" -
    jede der sieben benannten Komponenten wird über die feste Whitelist
    erkannt, aber AUSSCHLIESSLICH innerhalb des dafür bestimmten
    Bezeichnungsfelds des bekannten HV-VS-Altimport-Schemas ("HV VS
    <Monat>/<Jahr>; <Bezeichnung>; <Referenz>") - inklusive der
    Sonderregel "BK Garage" (auch mit Bindestrich "BK-Garage") VOR der
    generischen Betriebskosten-Erkennung (deren Muster sonst bereits
    innerhalb von "BK Garage" selbst anschlagen würde) und dem bekannten
    Alias "Abstellplatz" für "Stellplatz". Nachtrag Markus 23.10.2026,
    Quellabnahme 329b1fc: eine bloße freie Stichwortsuche über BELIEBIGEN
    Text (ohne das strukturelle HV-VS-Schema) ist NICHT mehr zulässig -
    siehe `test_hauptforderung_komponente_ignoriert_unbekannte_referenz_
    trotz_zufaelliger_whitelist_woerter` unten."""

    faelle = {
        "HV VS 9/2026; Betriebskosten 20%; J.1": "Betriebskosten",
        "HV VS 9/2026; BK Garage 20%; J.1": "BK Garage",
        "HV VS 9/2026; BK-Garage 20%; J.12345": "BK Garage",
        "HV VS 9/2026; Betriebskosten Garage 20%; J.1": "BK Garage",
        "HV VS 9/2026; Heizkosten 20%; J.1": "Heizkosten",
        "HV VS 9/2026; HK-Einnahmen 20%; J.1": "Heizkosten",
        "HV VS 9/2026; Wasser 20%; J.1": "Wasser",
        "HV VS 9/2026; Küche 20%; J.1": "Küche",
        "HV VS 9/2026; Küchen-Einnahmen 20%; J.1": "Küche",
        "HV VS 9/2026; Stellplatz 20%; J.1": "Stellplatz",
        "HV VS 9/2026; Parkplatz 20%; J.1": "Stellplatz",
        "HV VS 9/2026; Abstellplatz 20%; J.12346": "Stellplatz",
    }
    for beleg_referenz, erwartete_komponente in faelle.items():
        p = _op(1, "SOLL", 1_000, beleg_referenz=beleg_referenz)
        p.leistungsperiode = "2026-09"
        assert _hauptforderung_komponente_text(p) == f"{erwartete_komponente} September 2026", beleg_referenz


def test_hauptforderung_komponente_ignoriert_unbekannte_referenz_trotz_zufaelliger_whitelist_woerter():
    """Echter Fachfehler aus Quellabnahme 329b1fc: `.search()` über den
    GESAMTEN Freitext ist KEINE Schemafeld-Whitelist - eine völlig
    unabhängige, nicht als HV-VS-Altimport erkannte Referenz darf NICHT
    fälschlich klassifiziert werden, nur weil irgendwo im Text ein
    Whitelist-Wort ("Wasser") vorkommt."""

    p = _op(1, "SOLL", 1_000, beleg_referenz="Sonderforderung Familie Wasser; HASH1234abcd")
    p.leistungsperiode = "2026-09"
    assert _hauptforderung_komponente_text(p) is None


def test_hauptforderung_komponente_ignoriert_unbekannte_drei_felder_referenz_trotz_abrechnungsfeld():
    """Echter Fachfehler aus Quellabnahme 0c5343c: der vormalige
    `_ABRECHNUNGSFELD_MUSTER` mit `.*?`-Vorsilbe akzeptierte JEDEN Text,
    solange er zufällig mit "Abrechnung<Jahr>" endete - die völlig
    unabhängige Referenz "Sonderforderung Familie Wasser; HASH...;
    Abrechnung2025" wurde dadurch weiterhin fälschlich als
    "Wasserabrechnung 2025" gelesen. Feld 0 dieser 3-Felder-Zeichenkette
    erfüllt das strenge HV-VS-Muster "HV VS <Monat>/<Jahr>" NICHT - die
    GESAMTE Zeichenkette bleibt deshalb unbekannt (`None`), unabhängig
    vom dritten Feld."""

    p = _op(1, "SOLL", 1_000, beleg_referenz="Sonderforderung Familie Wasser; HASH1234abcd; Abrechnung2025")
    p.leistungsperiode = "2026-09"
    assert _hauptforderung_komponente_text(p) is None


def test_hauptforderung_komponente_vier_felder_mit_referenz_und_abrechnungsjahr():
    """Echter Fachfehler aus Quellabnahme 0c5343c: der vormalige
    `_HV_VS_MUSTER` fasste den gesamten Rest nach dem Bezeichnungsfeld
    ungeprüft als EIN Referenzfeld auf (`referenz: str`) - bei einem
    ECHTEN 4-Felder-Datensatz ("HV VS 9/2026; Heizkosten 20%; J.12345;
    Abrechnung2025") wurde "J.12345; Abrechnung2025" komplett als
    Referenz verschluckt und das Abrechnungsjahr IGNORIERT (Ergebnis
    fälschlich "Heizkosten September 2026" statt "Heizkostenabrechnung
    2025"). Jetzt werden Feld 2 (reine Referenz "J.12345") und Feld 3
    (eigenständiges Abrechnungsfeld) getrennt geprüft."""

    p = _op(1, "SOLL", 50_000, beleg_referenz="HV VS 9/2026; Heizkosten 20%; J.12345; Abrechnung2025")
    p.leistungsperiode = "2026-09"  # Buchungsmonat - bewusst ABWEICHEND vom Abrechnungsjahr
    assert _hauptforderung_komponente_text(p) == "Heizkostenabrechnung 2025"


def test_hauptforderung_komponente_vier_felder_mit_unbekannter_komponente_bleibt_neutraler_abrechnungstext():
    """Dieselbe 4-Felder-Struktur, aber ein NICHT in der Whitelist
    erfasstes Bezeichnungsfeld - das Abrechnungsjahr aus Feld 3 bleibt
    trotzdem wirksam ("generisches Abrechnungsergebnis nicht ohne
    Grundlage als BK umdeuten"), die unbekannte Komponente wird NICHT
    erfunden."""

    p = _op(1, "SOLL", 50_000, beleg_referenz="HV VS 9/2026; Sonstiges; J.12345; Abrechnung2025")
    assert _hauptforderung_komponente_text(p) == "Abrechnung 2025"


def test_hauptforderung_komponente_vier_felder_mit_ungueltigem_abrechnungsfeld_bleibt_unbekannt():
    """Ein viertes Feld, das NICHT dem strengen "Abrechnung <Jahr>"-Muster
    entspricht, macht die GESAMTE Zeichenkette unbekannt - es wird NICHT
    stillschweigend auf die 3-Felder-Logik zurückgefallen."""

    p = _op(1, "SOLL", 50_000, beleg_referenz="HV VS 9/2026; Heizkosten 20%; J.12345; Sonstiger Zusatz")
    assert _hauptforderung_komponente_text(p) is None


def test_hauptforderung_komponente_einzelfeld_literal_match_ohne_semikolon():
    """Einzelfeldformat (B, nach Quellabnahme 0c5343c): KEIN Semikolon
    im Text - nur ein VOLLSTÄNDIGER Literal-Treffer auf eine der
    bekannten Formen ist zulässig, kein beliebiger Vorsilbentext."""

    faelle = {
        "Heizkostenabrechnung2025": "Heizkostenabrechnung 2025",
        "Heizkostenabrechnung 2025": "Heizkostenabrechnung 2025",
        "Betriebskostenabrechnung2025": "Betriebskostenabrechnung 2025",
        "Wasserabrechnung2025": "Wasserabrechnung 2025",
        "Abrechnung2025": "Abrechnung 2025",
    }
    for beleg_referenz, erwartetes_ergebnis in faelle.items():
        p = _op(1, "SOLL", 1_000, beleg_referenz=beleg_referenz)
        assert _hauptforderung_komponente_text(p) == erwartetes_ergebnis, beleg_referenz


def test_hauptforderung_komponente_einzelfeld_lehnt_beliebigen_vorsilbentext_ab():
    """Eine beliebige, nicht in der festen Sechser-Alternative enthaltene
    Vorsilbe darf NICHT als Komponente durchgehen, auch wenn der Rest
    des Textes formal wie ein Abrechnungsfeld aussieht."""

    p = _op(1, "SOLL", 1_000, beleg_referenz="SonderforderungAbrechnung2025")
    assert _hauptforderung_komponente_text(p) is None


def test_hauptforderung_komponente_erhaelt_explizites_abrechnungsjahr_statt_buchungsmonat():
    """"Explizites 'Abrechnung 2025' als Abrechnungsjahr erhalten" - eine
    Heizkostenabrechnung 2025, deren OP-Position erst im September 2026
    gebucht wurde, darf NICHT als "Heizkosten September 2026" erscheinen
    (Buchungsmonat != Abrechnungsjahr) - das genannte Abrechnungsjahr hat
    Vorrang vor der `leistungsperiode` des Buchungsmonats."""

    p = _op(1, "SOLL", 50_000, beleg_referenz="Heizkostenabrechnung 2025")
    p.leistungsperiode = "2026-09"  # Buchungsmonat - bewusst ABWEICHEND vom Abrechnungsjahr
    assert _hauptforderung_komponente_text(p) == "Heizkostenabrechnung 2025"


def test_hauptforderung_komponente_generische_abrechnung_bleibt_neutral_ohne_grundlage():
    """"generisches Abrechnungsergebnis nicht ohne Grundlage als BK
    umdeuten" - ein bloßes "Abrechnung 2025" ohne erkennbare Komponente
    bleibt neutral, wird NIE als Betriebskosten/Heizkosten erraten."""

    p = _op(1, "SOLL", 20_000, beleg_referenz="Abrechnung 2025")
    assert _hauptforderung_komponente_text(p) == "Abrechnung 2025"


def test_hauptforderung_komponente_entfernt_korrektur_praefix_ohne_ihn_auszugeben():
    """Der bestehende Korrektur-Präfix (`op/service.py::storniere_und_
    korrigiere`: "Korrektur zu #<id>: <ursprüngliche beleg_referenz>")
    muss sicher erkannt/entfernt werden, OHNE selbst im Ergebnis zu
    erscheinen (keine rohe OP-Id im Kundentext)."""

    p = _op(1, "SOLL", 50_000, beleg_referenz="Korrektur zu #42: Heizkostenabrechnung 2025")
    ergebnis = _hauptforderung_komponente_text(p)
    assert ergebnis == "Heizkostenabrechnung 2025"
    assert "#42" not in ergebnis
    assert "Korrektur" not in ergebnis


def test_hauptforderung_komponente_unbekannte_quelle_bleibt_neutral():
    """Eine unbekannte/technische Quelle (keine Whitelist-Komponente,
    kein Abrechnungsjahr) liefert `None` - der Aufrufer fällt dann
    unverändert auf "Mietvorschreibung ..." zurück, NIE eine erfundene
    Komponente UND NIE ein roher Ausschnitt der Quelle selbst."""

    p = _op(1, "SOLL", 1_000, beleg_referenz="J.998877")
    assert _hauptforderung_komponente_text(p) is None

    p_ohne_referenz = _op(2, "SOLL", 1_000, beleg_referenz=None)
    assert _hauptforderung_komponente_text(p_ohne_referenz) is None


def test_einfache_buchung_text_zeigt_fachliche_komponente_statt_pauschaler_mietvorschreibung():
    """Ende-zu-Ende über die tatsächliche Journal-Textquelle
    (`erstelle_einfache_tabelle`): eine Heizkostenabrechnung 2025 heißt
    NICHT "Mietvorschreibung September 2026", sondern erkennbar
    "Heizkostenabrechnung 2025" - Saldo/Betrag bleiben dabei exakt
    unverändert (reine Textkorrektur)."""

    heizkosten = _op(1, "SOLL", 50_000, belegdatum=date(2026, 9, 5), beleg_referenz="Heizkostenabrechnung 2025")
    heizkosten.leistungsperiode = "2026-09"
    blatt = _leer(saldo_cent=50_000, positionen_aktiv=[heizkosten], positionen_alle=[heizkosten])

    zeilen = erstelle_einfache_tabelle(blatt)
    zeile = next(z for z in zeilen if z.betrag_cent == 50_000)
    assert zeile.buchung == "Heizkostenabrechnung 2025"
    assert "Mietvorschreibung" not in zeile.buchung
    assert zeile.offen_cent == 50_000
