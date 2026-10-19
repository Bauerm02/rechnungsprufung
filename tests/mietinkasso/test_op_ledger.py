from __future__ import annotations

from datetime import date

import pytest

from mietinkasso.domain.enums import OPTyp, Rolle
from mietinkasso.domain.exceptions import (
    CrossTenantError,
    DoppelteEroeffnungsartError,
    ImportConflictError,
    ObjektAusgeschlossenError,
    StornierungKonfliktError,
    ZahlungsbindungInkonsistentError,
)


def test_eroeffnen_gesamtsaldo_sperrt_objekt_107_auch_fuer_admin(op_service, stammdaten_repo, ctx_factory):
    """Regression (Codex-Rückprüfung): `pruefe_objekt_erlaubt` existierte
    zuvor nur als isolierte, nie aufgerufene Hilfsfunktion. Codex' Gegenprobe
    legte Objekt 107 mit ausgeschlossen=True an und rief
    OPService.eroeffnen_gesamtsaldo(50000) auf - es wurden trotzdem 500 EUR
    gebucht. Der Ausschluss muss zentral aus dem persistierten
    Konto->Vertrag->Einheit->Objekt erzwungen werden, rollenunabhängig
    (ADMIN eingeschlossen)."""

    stammdaten_repo.upsert_gesellschaft(id="7DI", name="7D Immobilien GmbH")
    stammdaten_repo.upsert_objekt(id="107", gesellschaft_id="7DI", bezeichnung="Sieben Dörfer", ausgeschlossen=True)
    stammdaten_repo.upsert_einheit(id="107-TOP1", objekt_id="107", bezeichnung="Top 1", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten_repo.upsert_debitor(id="DEB-107", name="Mieterin 107")
    stammdaten_repo.upsert_vertrag(
        id="V-107-1", einheit_id="107-TOP1", debitor_id="DEB-107", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    vertrag = stammdaten_repo.get_vertrag("V-107-1")
    konto = stammdaten_repo.get_or_create_konto(vertrag=vertrag)
    ctx_admin = ctx_factory("7DI", rolle=Rolle.ADMIN)

    with pytest.raises(ObjektAusgeschlossenError):
        op_service.eroeffnen_gesamtsaldo(
            ctx=ctx_admin, konto=konto, betrag_cent=50_000, stichtag=date(2026, 1, 1),
            import_id="ERO-107", akteur="admin-gegenprobe",
        )

    assert op_service.berechne_saldo(konto.id).saldo_cent == 0


def test_eroeffnung_soll_zahlung_ergibt_erwarteten_saldo(op_service, basis_vertrag, ctx_factory):
    """Eröffnung 100 + Soll 600 - Zahlung 200 = 500."""

    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")

    op_service.eroeffnen_gesamtsaldo(
        ctx=ctx, konto=konto, betrag_cent=10_000, stichtag=date(2026, 1, 1), import_id="ERO-1", akteur="test"
    )
    op_service.buchen(
        ctx=ctx,
        konto=konto,
        typ=OPTyp.SOLL,
        betrag_cent=60_000,
        belegdatum=date(2026, 2, 1),
        buchungsdatum=date(2026, 2, 1),
        faelligkeit=date(2026, 2, 5),
        beleg_referenz="Miete Februar",
        leistungsperiode="2026-02",
    )
    op_service.buchen(
        ctx=ctx,
        konto=konto,
        typ=OPTyp.ZAHLUNG,
        betrag_cent=20_000,
        belegdatum=date(2026, 2, 10),
        buchungsdatum=date(2026, 2, 10),
        faelligkeit=None,
        beleg_referenz="Teilzahlung Bank",
    )

    saldo = op_service.berechne_saldo(konto.id)
    assert saldo.saldo_cent == 50_000


def test_doppelimport_ist_wirkungslos(op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    for _ in range(2):
        op_service.buchen(
            ctx=ctx,
            konto=konto,
            typ=OPTyp.SOLL,
            betrag_cent=60_000,
            belegdatum=date(2026, 3, 1),
            buchungsdatum=date(2026, 3, 1),
            faelligkeit=date(2026, 3, 5),
            beleg_referenz="Miete März",
            import_id="ZINSLISTE-2026-03-V601-3",
        )
    saldo = op_service.berechne_saldo(konto.id)
    assert saldo.saldo_cent == 60_000
    assert len(saldo.positionen) == 1


def test_geaenderte_gleiche_import_id_ist_konflikt(op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    op_service.buchen(
        ctx=ctx,
        konto=konto,
        typ=OPTyp.SOLL,
        betrag_cent=60_000,
        belegdatum=date(2026, 3, 1),
        buchungsdatum=date(2026, 3, 1),
        faelligkeit=date(2026, 3, 5),
        beleg_referenz="Miete März",
        import_id="ZINSLISTE-2026-03-V601-3",
    )
    with pytest.raises(ImportConflictError):
        op_service.buchen(
            ctx=ctx,
            konto=konto,
            typ=OPTyp.SOLL,
            betrag_cent=61_000,  # geänderter Inhalt, gleiche import_id
            belegdatum=date(2026, 3, 1),
            buchungsdatum=date(2026, 3, 1),
            faelligkeit=date(2026, 3, 5),
            beleg_referenz="Miete März",
            import_id="ZINSLISTE-2026-03-V601-3",
        )


def test_saldo_und_enthaltenes_altjournal_nicht_doppelt_gebucht(op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    op_service.eroeffnen_gesamtsaldo(
        ctx=ctx, konto=konto, betrag_cent=10_000, stichtag=date(2026, 1, 31), import_id="ERO-1", akteur="test"
    )
    with pytest.raises(DoppelteEroeffnungsartError):
        op_service.buchen(
            ctx=ctx,
            konto=konto,
            typ=OPTyp.SOLL,
            betrag_cent=5_000,
            belegdatum=date(2026, 1, 15),  # liegt vor dem Gesamtsaldo-Stichtag
            buchungsdatum=date(2026, 1, 15),
            faelligkeit=date(2026, 1, 20),
            beleg_referenz="Altes Journal Jänner",
        )


# ---------------------------------------------------------------------------
# Eröffnungskorrektur (Auftrag HV-20260912-ECHTBETRIEB, Ergänzung): ein im
# bestätigten Gesamtsaldo nachweislich fehlender Posten, dessen echtes Datum
# vor/auf dem Eröffnungsstichtag liegt - ohne das allgemeine Altjournal-Tor
# zu öffnen.
# ---------------------------------------------------------------------------


def test_eroeffnungskorrektur_bewahrt_original_belegdatum_und_bucht_am_uebernahmetag(op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    op_service.eroeffnen_gesamtsaldo(
        ctx=ctx, konto=konto, betrag_cent=150_000, stichtag=date(2026, 8, 31), import_id="ERO-1", akteur="test"
    )
    zeile = op_service.eroeffnungskorrektur_buchen(
        ctx=ctx, konto=konto, typ=OPTyp.ZAHLUNG, betrag_cent=20_000,
        original_belegdatum=date(2026, 8, 20),  # VOR dem Stichtag
        uebernahmetag=date(2026, 9, 12),
        grund="Zahlung im Original-Gesamtsaldo nachweislich nicht enthalten (Bankbeleg 2026-08-20)",
        quelle_referenz="BANKBELEG-2026-08-20-XY",
        import_id="KORR-1",
    )
    assert zeile.belegdatum == date(2026, 8, 20)  # echtes historisches Datum bewahrt
    assert zeile.buchungsdatum == date(2026, 9, 12)  # Übernahmetag, nicht das historische Datum
    assert zeile.quelle_system == "eroeffnungskorrektur"
    assert zeile.aenderungsgrund.startswith("Zahlung im Original-Gesamtsaldo")

    saldo = op_service.berechne_saldo(konto.id)
    assert saldo.saldo_cent == 150_000 - 20_000  # Original-Eröffnung bleibt unverändert sichtbar, Korrektur ergänzt
    eroeffnung = [p for p in saldo.positionen if p.typ == "EROEFFNUNG"][0]
    assert eroeffnung.betrag_cent == 150_000


def test_eroeffnungskorrektur_ohne_gesamtsaldo_eroeffnung_wird_blockiert(op_service, basis_vertrag, ctx_factory):
    """Der Korrekturpfad setzt eine bereits GESAMTSALDO-eröffnete Eröffnung
    voraus - bei EINZEL_OP (oder noch keiner Eröffnung) gibt es kein "im
    Saldo bereits enthalten", eine gewöhnliche Nachbuchung ist dort der
    richtige Weg."""

    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    with pytest.raises(DoppelteEroeffnungsartError):
        op_service.eroeffnungskorrektur_buchen(
            ctx=ctx, konto=konto, typ=OPTyp.ZAHLUNG, betrag_cent=20_000,
            original_belegdatum=date(2026, 8, 20), uebernahmetag=date(2026, 9, 12),
            grund="Test", quelle_referenz="Q1", import_id="KORR-1",
        )


def test_eroeffnungskorrektur_verlangt_grund_und_quelle_referenz(op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    op_service.eroeffnen_gesamtsaldo(
        ctx=ctx, konto=konto, betrag_cent=150_000, stichtag=date(2026, 8, 31), import_id="ERO-1", akteur="test"
    )
    with pytest.raises(ValueError, match="grund"):
        op_service.eroeffnungskorrektur_buchen(
            ctx=ctx, konto=konto, typ=OPTyp.ZAHLUNG, betrag_cent=20_000,
            original_belegdatum=date(2026, 8, 20), uebernahmetag=date(2026, 9, 12),
            grund="", quelle_referenz="Q1", import_id="KORR-1",
        )
    with pytest.raises(ValueError, match="quelle_referenz"):
        op_service.eroeffnungskorrektur_buchen(
            ctx=ctx, konto=konto, typ=OPTyp.ZAHLUNG, betrag_cent=20_000,
            original_belegdatum=date(2026, 8, 20), uebernahmetag=date(2026, 9, 12),
            grund="Grund vorhanden", quelle_referenz="", import_id="KORR-1",
        )


def test_negativer_betrag_wird_bei_typisierten_buchungen_ueberall_geblockt(op_service, basis_vertrag, ctx_factory):
    """Codex-Rückprüfung: SOLL/GUTSCHRIFT/ZAHLUNG/RUECKLASTSCHRIFT leiten
    ihr Vorzeichen aus `typ` ab - ein negativer `betrag_cent` würde sonst
    ein zweites Mal negiert (z. B. eine GUTSCHRIFT erhöht dann die Schuld
    statt sie zu mindern). Nur `eroeffnen_gesamtsaldo` (Nettosumme, siehe
    `test_guthaben_eroeffnung_...` oben) darf negativ (Guthaben) sein."""

    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    with pytest.raises(ValueError, match="positiv"):
        op_service.buchen(
            ctx=ctx, konto=konto, typ=OPTyp.GUTSCHRIFT, betrag_cent=-5_000,
            belegdatum=date(2026, 1, 15), buchungsdatum=date(2026, 1, 15), faelligkeit=None,
            beleg_referenz="sollte scheitern",
        )
    with pytest.raises(ValueError, match="positiv"):
        op_service.eroeffnen_einzel_op(
            ctx=ctx, konto=konto, stichtag=date(2026, 1, 1), import_id="ERO-NEG", typ=OPTyp.SOLL,
            betrag_cent=-1_000, belegdatum=date(2026, 1, 1), faelligkeit=None, beleg_referenz="sollte scheitern",
            akteur="test",
        )
    op_service.eroeffnen_gesamtsaldo(
        ctx=ctx, konto=konto, betrag_cent=150_000, stichtag=date(2026, 8, 31), import_id="ERO-1", akteur="test"
    )
    with pytest.raises(ValueError, match="positiv"):
        op_service.eroeffnungskorrektur_buchen(
            ctx=ctx, konto=konto, typ=OPTyp.ZAHLUNG, betrag_cent=-20_000,
            original_belegdatum=date(2026, 8, 20), uebernahmetag=date(2026, 9, 12),
            grund="Test", quelle_referenz="Q1", import_id="KORR-NEG",
        )
    # Nichts davon wurde gebucht.
    assert len(op_service.list_alle_positionen(konto.id)) == 1  # nur die Gesamtsaldo-Eröffnung


def test_eroeffnungskorrektur_replay_ist_wirkungslos_geaenderter_inhalt_ist_konflikt(op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    op_service.eroeffnen_gesamtsaldo(
        ctx=ctx, konto=konto, betrag_cent=150_000, stichtag=date(2026, 8, 31), import_id="ERO-1", akteur="test"
    )
    kwargs = dict(
        ctx=ctx, konto=konto, typ=OPTyp.ZAHLUNG, betrag_cent=20_000,
        original_belegdatum=date(2026, 8, 20), grund="Grund", quelle_referenz="Q1", import_id="KORR-1",
    )
    op_service.eroeffnungskorrektur_buchen(uebernahmetag=date(2026, 9, 12), **kwargs)
    # Replay (auch an einem SPÄTEREN Übernahmetag) mit identischem Inhalt -> No-Op, keine Verdopplung
    op_service.eroeffnungskorrektur_buchen(uebernahmetag=date(2026, 9, 15), **kwargs)
    saldo = op_service.berechne_saldo(konto.id)
    assert saldo.saldo_cent == 150_000 - 20_000
    assert len(op_service.list_alle_positionen(konto.id)) == 2  # Eröffnung + genau EINE Korrektur

    with pytest.raises(ImportConflictError):
        op_service.eroeffnungskorrektur_buchen(
            ctx=ctx, konto=konto, typ=OPTyp.ZAHLUNG, betrag_cent=99_999,  # abweichender Inhalt, gleiche import_id
            original_belegdatum=date(2026, 8, 20), uebernahmetag=date(2026, 9, 12),
            grund="Grund", quelle_referenz="Q1", import_id="KORR-1",
        )


def test_eroeffnung_gesamtsaldo_gleicher_fakt_mit_anderer_import_id_ist_replay(op_service, basis_vertrag, ctx_factory):
    """Regression (Codex-Fund #3): zwei Importe mit identischem Konto/
    Stichtag/Betrag aber UNTERSCHIEDLICHEN import_ids (z. B. zwei Dateien
    mit verschiedenem Namen für dieselbe Eröffnung) dürfen die Eröffnung
    NICHT verdoppeln - Eröffnung ist einmalig je Konto, nicht je Dateiname."""

    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    op_service.eroeffnen_gesamtsaldo(
        ctx=ctx, konto=konto, betrag_cent=60_000, stichtag=date(2026, 8, 31), import_id="A", akteur="test"
    )
    op_service.eroeffnen_gesamtsaldo(
        ctx=ctx, konto=konto, betrag_cent=60_000, stichtag=date(2026, 8, 31), import_id="B", akteur="test"
    )
    saldo = op_service.berechne_saldo(konto.id)
    assert saldo.saldo_cent == 60_000  # NICHT 120_000
    assert len([p for p in saldo.positionen if p.typ == "EROEFFNUNG"]) == 1


def test_eroeffnung_gesamtsaldo_widersprechender_zweitimport_wird_blockiert(op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    op_service.eroeffnen_gesamtsaldo(
        ctx=ctx, konto=konto, betrag_cent=60_000, stichtag=date(2026, 8, 31), import_id="A", akteur="test"
    )
    with pytest.raises(DoppelteEroeffnungsartError):
        op_service.eroeffnen_gesamtsaldo(
            ctx=ctx, konto=konto, betrag_cent=99_999, stichtag=date(2026, 8, 31), import_id="B", akteur="test"
        )
    saldo = op_service.berechne_saldo(konto.id)
    assert saldo.saldo_cent == 60_000  # unverändert, der Zweitimport hat nichts gebucht


def test_gesamtsaldo_und_einzel_op_gleichzeitig_ist_konflikt(op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    op_service.eroeffnen_gesamtsaldo(
        ctx=ctx, konto=konto, betrag_cent=10_000, stichtag=date(2026, 1, 31), import_id="ERO-1", akteur="test"
    )
    with pytest.raises(DoppelteEroeffnungsartError):
        op_service.eroeffnen_einzel_op(
            ctx=ctx,
            konto=konto,
            stichtag=date(2026, 1, 31),
            import_id="ERO-EINZEL-1",
            typ=OPTyp.SOLL,
            betrag_cent=4_000,
            belegdatum=date(2026, 1, 10),
            faelligkeit=date(2026, 1, 15),
            beleg_referenz="Alt-OP Jänner",
            akteur="test",
        )


def test_teilzahlung(op_service, basis_vertrag, ctx_factory):
    """700 - 300 = 400."""

    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    op_service.buchen(
        ctx=ctx,
        konto=konto,
        typ=OPTyp.SOLL,
        betrag_cent=70_000,
        belegdatum=date(2026, 4, 1),
        buchungsdatum=date(2026, 4, 1),
        faelligkeit=date(2026, 4, 5),
        beleg_referenz="Miete April",
    )
    op_service.buchen(
        ctx=ctx,
        konto=konto,
        typ=OPTyp.ZAHLUNG,
        betrag_cent=30_000,
        belegdatum=date(2026, 4, 12),
        buchungsdatum=date(2026, 4, 12),
        faelligkeit=None,
        beleg_referenz="Teilzahlung",
    )
    saldo = op_service.berechne_saldo(konto.id)
    assert saldo.saldo_cent == 40_000


def test_ueberzahlung_ergibt_guthaben(op_service, basis_vertrag, ctx_factory):
    """800 auf 700 = 100 Guthaben (negativer Saldo)."""

    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    op_service.buchen(
        ctx=ctx,
        konto=konto,
        typ=OPTyp.SOLL,
        betrag_cent=70_000,
        belegdatum=date(2026, 5, 1),
        buchungsdatum=date(2026, 5, 1),
        faelligkeit=date(2026, 5, 5),
        beleg_referenz="Miete Mai",
    )
    op_service.buchen(
        ctx=ctx,
        konto=konto,
        typ=OPTyp.ZAHLUNG,
        betrag_cent=80_000,
        belegdatum=date(2026, 5, 6),
        buchungsdatum=date(2026, 5, 6),
        faelligkeit=None,
        beleg_referenz="Überzahlung",
    )
    saldo = op_service.berechne_saldo(konto.id)
    assert saldo.saldo_cent == -10_000  # 100,00 EUR Guthaben


def test_ruecklastschrift_macht_op_wieder_offen(op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    op_service.buchen(
        ctx=ctx,
        konto=konto,
        typ=OPTyp.SOLL,
        betrag_cent=60_000,
        belegdatum=date(2026, 6, 1),
        buchungsdatum=date(2026, 6, 1),
        faelligkeit=date(2026, 6, 5),
        beleg_referenz="Miete Juni",
    )
    op_service.buchen(
        ctx=ctx,
        konto=konto,
        typ=OPTyp.ZAHLUNG,
        betrag_cent=60_000,
        belegdatum=date(2026, 6, 5),
        buchungsdatum=date(2026, 6, 5),
        faelligkeit=None,
        beleg_referenz="Zahlung Juni",
    )
    saldo_ausgeglichen = op_service.berechne_saldo(konto.id)
    assert saldo_ausgeglichen.saldo_cent == 0

    op_service.buchen(
        ctx=ctx,
        konto=konto,
        typ=OPTyp.RUECKLASTSCHRIFT,
        betrag_cent=60_000,
        belegdatum=date(2026, 6, 8),
        buchungsdatum=date(2026, 6, 8),
        faelligkeit=date(2026, 6, 8),
        beleg_referenz="Rücklastschrift Zahlung Juni",
    )
    saldo_wieder_offen = op_service.berechne_saldo(konto.id)
    assert saldo_wieder_offen.saldo_cent == 60_000


def test_cross_tenant_buchung_wird_blockiert(op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    fremde_ctx = ctx_factory("ANDERE-GESELLSCHAFT")
    with pytest.raises(CrossTenantError):
        op_service.buchen(
            ctx=fremde_ctx,
            konto=konto,
            typ=OPTyp.SOLL,
            betrag_cent=60_000,
            belegdatum=date(2026, 6, 1),
            buchungsdatum=date(2026, 6, 1),
            faelligkeit=date(2026, 6, 5),
            beleg_referenz="Unbefugte Buchung",
        )


def test_korrektur_ersetzt_original_ohne_historie_zu_ueberschreiben(op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    original = op_service.buchen(
        ctx=ctx,
        konto=konto,
        typ=OPTyp.SOLL,
        betrag_cent=60_000,
        belegdatum=date(2026, 7, 1),
        buchungsdatum=date(2026, 7, 1),
        faelligkeit=date(2026, 7, 5),
        beleg_referenz="Miete Juli (Tippfehler)",
    )
    op_service.storniere_und_korrigiere(
        ctx=ctx,
        konto=konto,
        original_id=original.id,
        aenderungsgrund="Tippfehler: falscher Betrag erfasst",
        neuer_betrag_cent=65_000,
    )
    alle = op_service._op_repository.list_alle(konto.id)
    assert len(alle) == 2  # Original bleibt erhalten (storniert), neue Zeile kommt dazu
    original_row = next(p for p in alle if p.id == original.id)
    assert original_row.status == "STORNIERT"
    saldo = op_service.berechne_saldo(konto.id)
    assert saldo.saldo_cent == 65_000


def test_doppelte_korrektur_desselben_originals_verdoppelt_nicht_den_saldo(op_service, basis_vertrag, ctx_factory):
    """Regression (Codex-Gegenprobe): SOLL 10000 anlegen, dieselbe
    original_id zweimal identisch auf neuer_betrag_cent=12000 korrigieren.
    Ein bereits storniertes Original darf NICHT ein zweites Mal eine
    aktive Ersatzzeile bekommen (Saldo darf nicht auf 24000 springen) -
    ein echter Retry mit derselben vorgang_id ist ein sicherer No-Op,
    jeder abweichende zweite Aufruf ein Konflikt."""

    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    original = op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=10_000,
        belegdatum=date(2026, 8, 1), buchungsdatum=date(2026, 8, 1), faelligkeit=date(2026, 8, 5),
        beleg_referenz="Miete August",
    )

    erster_aufruf = op_service.storniere_und_korrigiere(
        ctx=ctx, konto=konto, original_id=original.id, aenderungsgrund="Tippfehler",
        neuer_betrag_cent=12_000, vorgang_id="KORREKTUR-1",
    )
    assert op_service.berechne_saldo(konto.id).saldo_cent == 12_000

    # Echter Retry (z. B. doppelte Formularbestätigung): gleiche vorgang_id,
    # identischer Inhalt -> sicherer No-Op, KEINE zweite Ersatzzeile.
    zweiter_aufruf_gleicher_vorgang = op_service.storniere_und_korrigiere(
        ctx=ctx, konto=konto, original_id=original.id, aenderungsgrund="Tippfehler",
        neuer_betrag_cent=12_000, vorgang_id="KORREKTUR-1",
    )
    assert zweiter_aufruf_gleicher_vorgang.id == erster_aufruf.id
    assert op_service.berechne_saldo(konto.id).saldo_cent == 12_000

    # Ein DRITTER, abweichender Versuch (andere vorgang_id) auf dasselbe
    # bereits korrigierte Original ist ein Konflikt, kein stiller Erfolg.
    with pytest.raises(StornierungKonfliktError):
        op_service.storniere_und_korrigiere(
            ctx=ctx, konto=konto, original_id=original.id, aenderungsgrund="Tippfehler",
            neuer_betrag_cent=12_000, vorgang_id="KORREKTUR-ANDERS",
        )
    assert op_service.berechne_saldo(konto.id).saldo_cent == 12_000
    assert len(op_service._op_repository.list_alle(konto.id)) == 2  # Original (storniert) + genau EINE Ersatzzeile


def test_guthaben_eroeffnung_mindert_offene_forderung_und_faelligen_rest(op_service, basis_vertrag, ctx_factory):
    """Regression (Codex-Gegenprobe): Eröffnung GESAMTSALDO -10000 Cent
    (Guthaben) zum 31.08., danach SOLL 60000 Cent fällig 05.09.
    berechne_saldo lieferte schon korrekt 50000, aber offene_forderungen
    zeigte weiterhin den vollen Rest 60000 und faelliger_unstrittiger_rest
    ignorierte das Guthaben ebenfalls - ein Guthaben (negativer Betrag in
    einer forderungsseitigen Zeile) muss wie eine Zahlung/Gutschrift immer
    mindern, unabhängig von der eigenen (oft unbekannten) Fälligkeit."""

    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    op_service.eroeffnen_gesamtsaldo(
        ctx=ctx, konto=konto, betrag_cent=-10_000, stichtag=date(2026, 8, 31),
        import_id="ERO-GUTHABEN", akteur="test",
    )
    op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=60_000,
        belegdatum=date(2026, 9, 1), buchungsdatum=date(2026, 9, 1), faelligkeit=date(2026, 9, 5),
        beleg_referenz="Miete September",
    )

    saldo = op_service.berechne_saldo(konto.id, stichtag=date(2026, 9, 10))
    assert saldo.saldo_cent == 50_000
    assert saldo.faelliger_unstrittiger_rest_cent == 50_000  # Guthaben mindert den fälligen Rest, nicht nur den Saldo

    forderungen = op_service.offene_forderungen(konto.id, heute=date(2026, 9, 10))
    assert len(forderungen) == 1  # das Guthaben selbst ist KEINE eigene (negative) Forderung
    assert forderungen[0].rest_cent == 50_000  # nicht die vollen 60000 - das Guthaben wurde bereits verrechnet


# ---------------------------------------------------------------------------
# HV-20260919-GEORGE-CODE, Fix 2: explizite Zahlungszweckbindung
# (bezieht_sich_auf_id) hat Vorrang vor generischem FIFO - Codex-Fund
# anhand des belegten anonymisierten Musters: Anfangsforderung ohne
# Fälligkeit 548,10 + September-Soll 1.224,68 + Zahlung 1.224,86 mit
# leistungsperiode=2026-09 UND expliziter Bindung an das September-Soll.
# Gesamtsaldo 547,92 war schon vorher richtig - NUR die Verteilung auf
# die einzelnen Forderungen (und damit der Mahnzyklus je Forderung) war
# falsch, weil das reine FIFO die ältere Anfangsforderung zuerst bediente.
# ---------------------------------------------------------------------------


def test_offene_forderungen_explizite_bindung_deckt_zielforderung_zuerst(op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    anfang = op_service.eroeffnen_gesamtsaldo(
        ctx=ctx, konto=konto, betrag_cent=54_810, stichtag=date(2026, 8, 1),
        import_id="ERO-ANFANG", akteur="test",
    )
    september_soll = op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=122_468,
        belegdatum=date(2026, 9, 1), buchungsdatum=date(2026, 9, 1), faelligkeit=date(2026, 9, 5),
        leistungsperiode="2026-09", beleg_referenz="Miete September",
    )
    op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.ZAHLUNG, betrag_cent=122_486,
        belegdatum=date(2026, 9, 6), buchungsdatum=date(2026, 9, 6), faelligkeit=None,
        leistungsperiode="2026-09", beleg_referenz="Zahlung September",
        bezieht_sich_auf_id=september_soll.id,
    )

    saldo = op_service.berechne_saldo(konto.id, stichtag=date(2026, 9, 10))
    assert saldo.saldo_cent == 54_792  # Gesamtsaldo war schon vorher richtig (548,10+1224,68-1224,86)

    forderungen = op_service.offene_forderungen(konto.id, heute=date(2026, 9, 10))
    # September-Soll ist (bis auf den 0,18-Überschuss) explizit gedeckt und
    # verschwindet daher aus der Liste; NUR die Anfangsforderung bleibt mit
    # dem tatsächlich verbleibenden Rest offen - NICHT umgekehrt, wie es
    # das alte reine FIFO (älteste zuerst) ergeben hätte.
    assert [f.op_position_id for f in forderungen] == [anfang.id]
    assert forderungen[0].rest_cent == 54_792


def test_offene_forderungen_ohne_bindung_bleibt_klassisches_fifo(op_service, basis_vertrag, ctx_factory):
    """Regressionsschutz: exakt dasselbe Muster, aber OHNE
    bezieht_sich_auf_id, ergibt weiterhin das alte (unveränderte)
    FIFO-Verhalten - die Zahlung deckt zuerst die ÄLTERE Anfangsforderung,
    der Rest bleibt auf dem September-Soll offen. Zeigt zugleich, dass der
    Fix wirklich etwas ändert (anderes Ergebnis als der gebundene Fall)."""

    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    op_service.eroeffnen_gesamtsaldo(
        ctx=ctx, konto=konto, betrag_cent=54_810, stichtag=date(2026, 8, 1),
        import_id="ERO-ANFANG-FIFO", akteur="test",
    )
    september_soll = op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=122_468,
        belegdatum=date(2026, 9, 1), buchungsdatum=date(2026, 9, 1), faelligkeit=date(2026, 9, 5),
        leistungsperiode="2026-09", beleg_referenz="Miete September",
    )
    op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.ZAHLUNG, betrag_cent=122_486,
        belegdatum=date(2026, 9, 6), buchungsdatum=date(2026, 9, 6), faelligkeit=None,
        beleg_referenz="Zahlung ohne Bindung",
    )

    forderungen = op_service.offene_forderungen(konto.id, heute=date(2026, 9, 10))
    assert [f.op_position_id for f in forderungen] == [september_soll.id]
    assert forderungen[0].rest_cent == 54_792


def test_offene_forderungen_periode_ohne_explizite_id_deckt_alle_komponenten_derselben_periode_zuerst(
    op_service, basis_vertrag, ctx_factory,
):
    """Konkreter Codex-Fund (Abnahme b8d700d): eine Zahlung MIT
    `leistungsperiode`, aber OHNE eindeutige `bezieht_sich_auf_id` (z. B.
    weil zwei Komponenten - HMZ und BK - separat als eigene Forderungen
    DERSELBEN Periode geführt werden), wurde bisher rein chronologisch
    über ALLE Forderungen verteilt und konnte dabei eine ÄLTERE,
    unbeteiligte Periode tilgen, während die tatsächlich gemeinte Periode
    offen blieb. Belegtes Muster: Alt=8.000 (älter, andere/keine
    Periode) + zwei September-Komponenten 9.000/1.000 + Zahlung 10.000
    mit `leistungsperiode=2026-09` ohne Ziel-ID -> muss BEIDE September-
    Komponenten decken (9.000+1.000=10.000) und die Alt-Forderung
    UNBERÜHRT lassen, nicht umgekehrt."""

    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    alt = op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=8_000,
        belegdatum=date(2026, 6, 1), buchungsdatum=date(2026, 6, 1), faelligkeit=date(2026, 6, 5),
        beleg_referenz="Alte Forderung ohne Bezug zu September",
    )
    sept_a = op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=9_000,
        belegdatum=date(2026, 9, 1), buchungsdatum=date(2026, 9, 1), faelligkeit=date(2026, 9, 5),
        leistungsperiode="2026-09", beleg_referenz="HMZ September",
    )
    sept_b = op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=1_000,
        belegdatum=date(2026, 9, 1), buchungsdatum=date(2026, 9, 1), faelligkeit=date(2026, 9, 5),
        leistungsperiode="2026-09", beleg_referenz="BK September",
    )
    op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.ZAHLUNG, betrag_cent=10_000,
        belegdatum=date(2026, 9, 6), buchungsdatum=date(2026, 9, 6), faelligkeit=None,
        leistungsperiode="2026-09", beleg_referenz="Zahlung September (Periode ohne eindeutige Ziel-ID)",
    )

    forderungen = {f.op_position_id: f for f in op_service.offene_forderungen(konto.id, heute=date(2026, 9, 10))}
    assert sept_a.id not in forderungen  # vollständig gedeckt
    assert sept_b.id not in forderungen  # vollständig gedeckt
    assert forderungen[alt.id].rest_cent == 8_000  # UNBERÜHRT - nicht blind FIFO-getilgt


def test_offene_forderungen_periode_ohne_jede_forderung_dieser_periode_bleibt_ungeklaertes_guthaben(
    op_service, basis_vertrag, ctx_factory,
):
    """Codex-Abnahme 543dab8, Punkt 2: existiert für die auf der Zahlung
    vermerkte `leistungsperiode` GAR KEINE Forderung (nicht nur "bereits
    ausgeglichene"), darf der Betrag NICHT blind in den generischen
    FIFO-Pool fallen und eine ÄLTERE, unbeteiligte Periode tilgen. Alt-Soll
    (2026-08) = 8.000, Zahlung 5.000 mit `leistungsperiode=2026-09` ohne
    Ziel-ID, obwohl KEINE Forderung mit Periode 2026-09 existiert -> Alt
    muss vollständig UNBERÜHRT bei 8.000 bleiben."""

    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    alt = op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=8_000,
        belegdatum=date(2026, 8, 1), buchungsdatum=date(2026, 8, 1), faelligkeit=date(2026, 8, 5),
        leistungsperiode="2026-08", beleg_referenz="Miete August",
    )
    op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.ZAHLUNG, betrag_cent=5_000,
        belegdatum=date(2026, 9, 6), buchungsdatum=date(2026, 9, 6), faelligkeit=None,
        leistungsperiode="2026-09", beleg_referenz="Zahlung ohne jede September-Forderung",
    )

    forderungen = {f.op_position_id: f for f in op_service.offene_forderungen(konto.id, heute=date(2026, 9, 10))}
    assert forderungen[alt.id].rest_cent == 8_000


def test_offene_forderungen_generischer_rest_wird_je_ereignis_verteilt_konsistent_mit_zinsberechnung(
    op_service, basis_vertrag, ctx_factory,
):
    """Codex-Abnahme 543dab8, Punkt 3: `offene_forderungen` verteilte den
    generischen Rest bisher erst NACH allen Ereignissen gesammelt, während
    `mahnwesen.kosten.balance_zeitreihe_fuer_forderung` bereits JE EREIGNIS
    verteilte - bei mehreren gleichrangigen Forderungen (identische
    Fälligkeit) ergab das UNTERSCHIEDLICHE Ergebnisse. Drei Soll-Positionen
    je 10.000 (Perioden September/August/September, identische Fälligkeit
    05.09.), eine ungebundene Zahlung 10.000 am 06.09. und eine
    September-gebundene (ohne Ziel-ID) Zahlung 5.000 am 10.09. müssen
    beide Funktionen zum IDENTISCHEN Ergebnis führen: ID2 (August)
    unberührt bei 10.000, ID3 (September) bei 5.000 (ID1 vollständig
    getilgt)."""

    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    id1 = op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=10_000,
        belegdatum=date(2026, 9, 1), buchungsdatum=date(2026, 9, 1), faelligkeit=date(2026, 9, 5),
        leistungsperiode="2026-09", beleg_referenz="September Komponente A",
    )
    id2 = op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=10_000,
        belegdatum=date(2026, 9, 1), buchungsdatum=date(2026, 9, 1), faelligkeit=date(2026, 9, 5),
        leistungsperiode="2026-08", beleg_referenz="August-Restforderung, gleiche Fälligkeit",
    )
    id3 = op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=10_000,
        belegdatum=date(2026, 9, 1), buchungsdatum=date(2026, 9, 1), faelligkeit=date(2026, 9, 5),
        leistungsperiode="2026-09", beleg_referenz="September Komponente B",
    )
    op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.ZAHLUNG, betrag_cent=10_000,
        belegdatum=date(2026, 9, 6), buchungsdatum=date(2026, 9, 6), faelligkeit=None,
        beleg_referenz="Ungebundene Zahlung",
    )
    op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.ZAHLUNG, betrag_cent=5_000,
        belegdatum=date(2026, 9, 10), buchungsdatum=date(2026, 9, 10), faelligkeit=None,
        leistungsperiode="2026-09", beleg_referenz="September-Teilzahlung ohne Ziel-ID",
    )

    forderungen = {f.op_position_id: f for f in op_service.offene_forderungen(konto.id, heute=date(2026, 9, 15))}
    assert id1.id not in forderungen  # vollständig durch die ungebundene Zahlung getilgt
    assert forderungen[id2.id].rest_cent == 10_000  # August unberührt
    assert forderungen[id3.id].rest_cent == 5_000


def _buche_historische_fehlbindung(op_service, session_factory, **kwargs):
    """Neue Fehler bereits beim Schreiben ablehnen; Altbestand weiter beim Lesen erkennen."""
    from mietinkasso.infrastructure.db.tables import OPPositionTable

    with pytest.raises(ZahlungsbindungInkonsistentError):
        op_service.buchen(**kwargs)
    ziel_id = kwargs.pop("bezieht_sich_auf_id")
    row = op_service.buchen(**kwargs)
    with session_factory() as session:
        session.get(OPPositionTable, row.id).bezieht_sich_auf_id = ziel_id
        session.commit()


def test_offene_forderungen_bindung_an_fremdes_konto_wird_abgelehnt(op_service, stammdaten_repo, ctx_factory, session_factory):
    ctx = ctx_factory("7DI")
    stammdaten_repo.upsert_objekt(id="601", gesellschaft_id="7DI", bezeichnung="Am Corso")
    stammdaten_repo.upsert_einheit(id="601-TOP-X", objekt_id="601", bezeichnung="Top X", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten_repo.upsert_debitor(id="DEB-X", name="Anderer Mieter")
    stammdaten_repo.upsert_vertrag(
        id="V-601-X", einheit_id="601-TOP-X", debitor_id="DEB-X", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    fremdes_konto = stammdaten_repo.get_or_create_konto(vertrag=stammdaten_repo.get_vertrag("V-601-X"))
    fremde_forderung = op_service.buchen(
        ctx=ctx, konto=fremdes_konto, typ=OPTyp.SOLL, betrag_cent=100_000,
        belegdatum=date(2026, 9, 1), buchungsdatum=date(2026, 9, 1), faelligkeit=date(2026, 9, 5),
        beleg_referenz="Miete anderes Konto",
    )

    stammdaten_repo.upsert_einheit(id="601-TOP-Y", objekt_id="601", bezeichnung="Top Y", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten_repo.upsert_debitor(id="DEB-Y", name="Mieter Y")
    stammdaten_repo.upsert_vertrag(
        id="V-601-Y", einheit_id="601-TOP-Y", debitor_id="DEB-Y", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    eigenes_konto = stammdaten_repo.get_or_create_konto(vertrag=stammdaten_repo.get_vertrag("V-601-Y"))
    op_service.buchen(
        ctx=ctx, konto=eigenes_konto, typ=OPTyp.SOLL, betrag_cent=50_000,
        belegdatum=date(2026, 9, 1), buchungsdatum=date(2026, 9, 1), faelligkeit=date(2026, 9, 5),
        beleg_referenz="Miete eigenes Konto",
    )
    _buche_historische_fehlbindung(op_service, session_factory,
        ctx=ctx, konto=eigenes_konto, typ=OPTyp.ZAHLUNG, betrag_cent=50_000,
        belegdatum=date(2026, 9, 6), buchungsdatum=date(2026, 9, 6), faelligkeit=None,
        beleg_referenz="Zahlung mit fremder Bindung",
        bezieht_sich_auf_id=fremde_forderung.id,
    )

    with pytest.raises(ZahlungsbindungInkonsistentError):
        op_service.offene_forderungen(eigenes_konto.id, heute=date(2026, 9, 10))


def test_offene_forderungen_bindung_an_unbekannte_id_wird_abgelehnt(op_service, basis_vertrag, ctx_factory, session_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=50_000,
        belegdatum=date(2026, 9, 1), buchungsdatum=date(2026, 9, 1), faelligkeit=date(2026, 9, 5),
        beleg_referenz="Miete September",
    )
    _buche_historische_fehlbindung(op_service, session_factory,
        ctx=ctx, konto=konto, typ=OPTyp.ZAHLUNG, betrag_cent=50_000,
        belegdatum=date(2026, 9, 6), buchungsdatum=date(2026, 9, 6), faelligkeit=None,
        beleg_referenz="Zahlung mit erfundener Bindung",
        bezieht_sich_auf_id=999_999,
    )

    with pytest.raises(ZahlungsbindungInkonsistentError):
        op_service.offene_forderungen(konto.id, heute=date(2026, 9, 10))


def test_offene_forderungen_bindung_mit_abweichender_leistungsperiode_wird_abgelehnt(op_service, basis_vertrag, ctx_factory, session_factory):
    """Eine Bindung mit widersprüchlicher Leistungsperiode deutet auf eine
    verwechselte OP-ID hin - wird nicht stillschweigend akzeptiert."""

    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    august_soll = op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=50_000,
        belegdatum=date(2026, 8, 1), buchungsdatum=date(2026, 8, 1), faelligkeit=date(2026, 8, 5),
        leistungsperiode="2026-08", beleg_referenz="Miete August",
    )
    _buche_historische_fehlbindung(op_service, session_factory,
        ctx=ctx, konto=konto, typ=OPTyp.ZAHLUNG, betrag_cent=50_000,
        belegdatum=date(2026, 9, 6), buchungsdatum=date(2026, 9, 6), faelligkeit=None,
        leistungsperiode="2026-09", beleg_referenz="Zahlung mit abweichender Periode",
        bezieht_sich_auf_id=august_soll.id,
    )

    with pytest.raises(ZahlungsbindungInkonsistentError):
        op_service.offene_forderungen(konto.id, heute=date(2026, 9, 10))


def test_offene_forderungen_ueberschuss_der_bindung_deckt_andere_forderung(op_service, basis_vertrag, ctx_factory):
    """Eine gezielte Zahlung, die IHRE Zielforderung übersteigt, darf den
    Überschuss nicht verlieren (Doppelverbrauch) UND darf ihn nicht der
    schon vollständig gedeckten Zielforderung ein zweites Mal gutschreiben
    - er fließt in den generischen Pool für die ÜBRIGEN Forderungen."""

    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    aeltere_forderung = op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=30_000,
        belegdatum=date(2026, 8, 1), buchungsdatum=date(2026, 8, 1), faelligkeit=date(2026, 8, 5),
        beleg_referenz="Miete August",
    )
    september_soll = op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=50_000,
        belegdatum=date(2026, 9, 1), buchungsdatum=date(2026, 9, 1), faelligkeit=date(2026, 9, 5),
        beleg_referenz="Miete September",
    )
    op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.ZAHLUNG, betrag_cent=60_000,  # 10.000 Cent Überschuss
        belegdatum=date(2026, 9, 6), buchungsdatum=date(2026, 9, 6), faelligkeit=None,
        beleg_referenz="Überzahlung mit Bindung", bezieht_sich_auf_id=september_soll.id,
    )

    forderungen = {f.op_position_id: f for f in op_service.offene_forderungen(konto.id, heute=date(2026, 9, 10))}
    assert september_soll.id not in forderungen  # vollständig gedeckt
    assert forderungen[aeltere_forderung.id].rest_cent == 20_000  # 30.000 - 10.000 Überschuss
    assert sum(f.rest_cent for f in forderungen.values()) == 20_000  # kein Doppelverbrauch, korrekter Gesamtrest


def test_offene_forderungen_ruecklastschrift_bindung_an_zahlung_bleibt_unberuehrt(op_service, basis_vertrag, ctx_factory):
    """Regressionsschutz: RUECKLASTSCHRIFT nutzt bezieht_sich_auf_id mit
    einer ANDEREN Bedeutung (Verweis auf die zurückgebuchte ZAHLUNG, kein
    Forderungsziel) - die neue Bindungsprüfung darf das nicht mit einer
    Zahlungszweckbindung verwechseln oder fälschlich ablehnen."""

    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=60_000,
        belegdatum=date(2026, 6, 1), buchungsdatum=date(2026, 6, 1), faelligkeit=date(2026, 6, 5),
        beleg_referenz="Miete Juni",
    )
    zahlung = op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.ZAHLUNG, betrag_cent=60_000,
        belegdatum=date(2026, 6, 5), buchungsdatum=date(2026, 6, 5), faelligkeit=None,
        beleg_referenz="Zahlung Juni",
    )
    op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.RUECKLASTSCHRIFT, betrag_cent=60_000,
        belegdatum=date(2026, 6, 8), buchungsdatum=date(2026, 6, 8), faelligkeit=date(2026, 6, 8),
        beleg_referenz="Rücklastschrift Zahlung Juni", bezieht_sich_auf_id=zahlung.id,
    )

    forderungen = op_service.offene_forderungen(konto.id, heute=date(2026, 6, 10))
    assert len(forderungen) == 1
    assert forderungen[0].rest_cent == 60_000
