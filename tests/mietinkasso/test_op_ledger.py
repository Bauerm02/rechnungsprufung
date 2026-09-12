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
