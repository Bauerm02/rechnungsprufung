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

from mietinkasso.op.kontoblatt import (
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
    erstelle_kontoblatt,
)


def _op(
    id: int, typ: str, betrag_cent: int, *, status: str = "AKTIV", quelle_system: str | None = None,
    storniert_durch_id: int | None = None, belegdatum: date = date(2026, 9, 1), beleg_referenz: str | None = None,
) -> OPPositionTable:
    return OPPositionTable(
        id=id, konto_id="K-1", typ=typ, betrag_cent=betrag_cent, belegdatum=belegdatum, buchungsdatum=belegdatum,
        faelligkeit=None, faelligkeit_bekannt=False, status=status, quelle_system=quelle_system,
        storniert_durch_id=storniert_durch_id, beleg_referenz=beleg_referenz,
    )


def _buchung(
    *, vertrag_id: str = "V-1", stufe: int = 1, zinsen_op_position_id: int | None = None,
    gebuehr_op_position_id: int | None = None, mahnpauschale_op_position_id: int | None = None,
    versandnachweis_referenz: str = "mahnung:X", forderung_op_position_ids: str = "[]",
    zins_segmente_json: str = "[]", zinsen_delta_je_op_json: str = "{}",
) -> MahnkostenBuchungTable:
    return MahnkostenBuchungTable(
        vertrag_id=vertrag_id, stufe=stufe, mahnlauf_schluessel="HASH", forderung_op_position_ids=forderung_op_position_ids,
        hauptforderung_cent=0, zinsbasis="VEREINBART_GEPRUEFT", zinssatz_prozent=0, zins_von=date(2026, 9, 1),
        zins_bis=date(2026, 9, 30), zinsen_cent=0, versandnachweis_referenz=versandnachweis_referenz, erstellt_von="codex-test",
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
