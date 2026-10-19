"""Zahlungs- und Abgleichstand (HV-20261005-ABGLEICHSTATUS) - nur synthetische Daten."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select

from mietinkasso.abgleichstatus.models import AbgleichNachweisTable, EinzugNachweisTable
from mietinkasso.abgleichstatus.repository import AbgleichNachweisRepository, NachweisUngueltigError
from mietinkasso.abgleichstatus.service import berechne_abgleichstatus
from mietinkasso.bank.repository import BankRepository
from mietinkasso.bank.service import BankImportService
from mietinkasso.domain.enums import OPTyp
from mietinkasso.domain.exceptions import ImportConflictError
from mietinkasso.indexautomatik.zeit import WIEN
from mietinkasso.infrastructure.db.base import Base
from mietinkasso.infrastructure.db.tables import BankTransaktionTable
from mietinkasso.mahnwesen.repository import MahnFallRepository
from mietinkasso.rueckstaende.service import UnbekanntesObjektFilterError, berechne_rueckstandsuebersicht

_HEUTE = date(2026, 10, 5)
_SHA = "a" * 64
_IBAN_A = "XX00SYNTHETISCH00001234"
_QUELLE_PFAD = "/srv/privat/synthetisch/abgleich-o1.pdf"

# UI dependencies initialize once at import. This module runs after the
# existing backoffice integration bootstrap, and patches all shared services.


def test_einzugsstatus_bleibt_belegt_und_bucht_nicht(umgebung, stammdaten_repo, op_service, admin_ctx, session_factory):
    original = _einzug(umgebung, admin_ctx)
    repo = umgebung["nachweise"]
    vorher = _alle_zeilen(session_factory)
    args = dict(ctx=admin_ctx, referenz=original.referenz, status="BANKBESTAETIGT",
                erfasst_am=datetime(2026, 10, 5, 8, tzinfo=timezone.utc),
                nachweis="Synthetischer Original-Bankbeleg, eindeutige Referenz", import_id="status-1")
    event = repo.erfasse_einzugsstatus(**args)
    assert repo.erfasse_einzugsstatus(**args).id == event.id
    with pytest.raises(ImportConflictError):
        repo.erfasse_einzugsstatus(**dict(args, status="STORNIERT"))
    _, status = _status(umgebung, stammdaten_repo, op_service, admin_ctx, "O1")
    assert status.einzuege_offen_cent == 0
    assert status.summe_offen_cent == 50_000
    nachher = _alle_zeilen(session_factory)
    assert all(nachher[t] == value for t, value in vorher.items() if t != "einzug_status_nachweise")
    assert repo.list_einzugnachweise(["V1"])[0].status == "EINGEREICHT"
    repo.erfasse_einzugsstatus(**dict(args, status="ZURUECKGEGEBEN", import_id="status-2",
                                    erfasst_am=datetime(2026, 10, 5, 9, tzinfo=timezone.utc)))
    _, status = _status(umgebung, stammdaten_repo, op_service, admin_ctx, "O1")
    assert status.vertraege[0].einzuege[0].status == "ZURUECKGEGEBEN"


def test_statuswechsel_fremder_mandant_abgelehnt(umgebung, admin_ctx, ctx_factory):
    original = _einzug(umgebung, admin_ctx)
    with pytest.raises(NachweisUngueltigError):
        umgebung["nachweise"].erfasse_einzugsstatus(
            ctx=ctx_factory("SYN-B"), referenz=original.referenz, status="STORNIERT",
            erfasst_am=datetime(2026, 10, 5, 8, tzinfo=timezone.utc), nachweis="synthetisch", import_id="fremd",
        )


@pytest.fixture
def umgebung(session_factory, stammdaten_repo, op_service, admin_ctx):
    """Gesellschaft SYN-A (Objekte O1, O2, ausgeschlossenes O7) und eine
    fremde Gesellschaft SYN-B (Objekt O9), je ein eigenes Bankkonto."""

    stammdaten_repo.upsert_gesellschaft(id="SYN-A", name="Synthetik A GmbH")
    stammdaten_repo.upsert_gesellschaft(id="SYN-B", name="Synthetik B GmbH")
    stammdaten_repo.upsert_objekt(id="O1", gesellschaft_id="SYN-A", bezeichnung="Testobjekt Eins")
    stammdaten_repo.upsert_objekt(id="O2", gesellschaft_id="SYN-A", bezeichnung="Testobjekt Zwei")
    stammdaten_repo.upsert_objekt(id="O7", gesellschaft_id="SYN-A", bezeichnung="Ausgeschlossen", ausgeschlossen=True)
    stammdaten_repo.upsert_objekt(id="O9", gesellschaft_id="SYN-B", bezeichnung="Fremdobjekt")

    konten = {}
    for vid, oid, gid, deb in (
        ("V1", "O1", "SYN-A", "Mieter Synthetisch Eins"),
        ("V2", "O1", "SYN-A", "Mieter Synthetisch Zwei"),
        ("V9", "O9", "SYN-B", "Mieter Fremd"),
    ):
        stammdaten_repo.upsert_einheit(id=f"E-{vid}", objekt_id=oid, bezeichnung=f"Top {vid}", nutzungsstatus="DAUERVERMIETUNG")
        stammdaten_repo.upsert_debitor(id=f"D-{vid}", name=deb)
        stammdaten_repo.upsert_vertrag(
            id=vid, einheit_id=f"E-{vid}", debitor_id=f"D-{vid}", gesellschaft_id=gid,
            rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
        )
        konten[vid] = stammdaten_repo.get_or_create_konto(vertrag=stammdaten_repo.get_vertrag(vid))

    def soll(vid, monat, betrag):
        op_service.buchen(
            ctx=admin_ctx, konto=konten[vid], typ=OPTyp.SOLL, betrag_cent=betrag, belegdatum=date(2026, monat, 1),
            buchungsdatum=date(2026, monat, 1), faelligkeit=date(2026, monat, 5), beleg_referenz=f"Miete {monat}",
            leistungsperiode=f"2026-{monat:02d}",
        )

    # V1: August + September je 500,00, eine Zahlung 500,00 -> September offen.
    soll("V1", 8, 50_000)
    soll("V1", 9, 50_000)
    op_service.buchen(
        ctx=admin_ctx, konto=konten["V1"], typ=OPTyp.ZAHLUNG, betrag_cent=50_000, belegdatum=date(2026, 8, 10),
        buchungsdatum=date(2026, 8, 10), faelligkeit=None, beleg_referenz="Zahlung August",
    )
    # V2: Guthaben 100,00.
    soll("V2", 9, 30_000)
    op_service.buchen(
        ctx=admin_ctx, konto=konten["V2"], typ=OPTyp.ZAHLUNG, betrag_cent=40_000, belegdatum=date(2026, 9, 3),
        buchungsdatum=date(2026, 9, 3), faelligkeit=None, beleg_referenz="Überzahlung",
    )
    soll("V9", 9, 70_000)

    bank_repo = BankRepository(session_factory)
    bank_repo.upsert_bank_konto(id="BK-A", gesellschaft_id="SYN-A", iban=_IBAN_A, bezeichnung="Mietkonto Synthetik A")
    bank_repo.upsert_bank_konto(id="BK-B", gesellschaft_id="SYN-B", iban="XX00SYNTHETISCH00009999", bezeichnung="Konto B")
    return dict(
        konten=konten, bank_repo=bank_repo, bank_service=BankImportService(bank_repo, stammdaten_repo, op_service),
        nachweise=AbgleichNachweisRepository(session_factory), mahn=MahnFallRepository(session_factory),
    )


def _status(u, stammdaten_repo, op_service, ctx, objekt_id=None):
    uebersicht = berechne_rueckstandsuebersicht(
        ctx=ctx, objekt_id=objekt_id, stammdaten_repository=stammdaten_repo, op_service=op_service,
        mahn_fall_repository=u["mahn"], heute=_HEUTE,
    )
    status = berechne_abgleichstatus(
        uebersicht=uebersicht, stammdaten_repository=stammdaten_repo, op_service=op_service,
        bank_repository=u["bank_repo"], bank_service=u["bank_service"], nachweis_repository=u["nachweise"], heute=_HEUTE,
    )
    return uebersicht, status


def _abgleich(u, ctx, **abweichung):
    werte = dict(
        ctx=ctx, objekt_id="O1", bank_konto_id="BK-A", geprueft_von=date(2026, 9, 1), geprueft_bis=date(2026, 9, 30),
        geprueft_am=datetime(2026, 10, 1, 9, 0, tzinfo=WIEN), geprueft_durch="synthetik-pruefer",
        umfang="Mieteingänge Testobjekt Eins", quelle_ref=_QUELLE_PFAD, quelle_sha256=_SHA, import_id="ABG-1",
    )
    werte.update(abweichung)
    return u["nachweise"].erfasse_abgleichnachweis(**werte)


def _einzug(u, ctx, **abweichung):
    werte = dict(
        ctx=ctx, vertrag_id="V1", bank_konto_id="BK-A", betrag_cent=50_000, einzug_am=date(2026, 9, 20),
        eingereicht_am=datetime(2026, 9, 18, 10, 0, tzinfo=WIEN), referenz="EZ-1", status="EINGEREICHT",
        nachweis="Synthetische Einreichung",
    )
    werte.update(abweichung)
    return u["nachweise"].erfasse_einzugnachweis(**werte)


def _alle_zeilen(session_factory):
    with session_factory() as s:
        return {
            t.name: [tuple(r) for r in s.execute(select(t).order_by(*t.primary_key.columns)).all()]
            for t in Base.metadata.sorted_tables
        }


def test_ohne_nachweis_kein_bankkonto_aus_gesellschaft_abgeleitet(umgebung, stammdaten_repo, op_service, admin_ctx):
    _, status = _status(umgebung, stammdaten_repo, op_service, admin_ctx)
    o1 = next(o for o in status.objekte if o.objekt_id == "O1")
    assert o1.staende == () and status.objekte_ohne_nachweis == len(status.objekte)
    assert {o.objekt_id for o in status.objekte} == {"O1", "O2", "O9"}  # O7 ausgeschlossen


def test_neuester_erfasster_nachweis_gilt_nicht_max_datum(umgebung, stammdaten_repo, op_service, admin_ctx, session_factory):
    _abgleich(umgebung, admin_ctx)
    # Korrektur: später erfasst, aber FRÜHERES Prüfende.
    _abgleich(umgebung, admin_ctx, import_id="ABG-2", geprueft_bis=date(2026, 9, 15),
              geprueft_am=datetime(2026, 10, 2, 9, 0, tzinfo=WIEN))
    # Jüngere Bankbuchung ist keine Abdeckung.
    with session_factory() as s:
        s.add(BankTransaktionTable(
            bank_konto_id="BK-A", betrag_cent=50_000, buchungsdatum=date(2026, 10, 4), quelle_typ="CSV",
            quelle_hash="b" * 64, import_id="SYN-TX-1",
        ))
        s.commit()
    _, status = _status(umgebung, stammdaten_repo, op_service, admin_ctx, objekt_id="O1")
    (stand,) = status.objekte[0].staende
    assert stand.geprueft_bis == date(2026, 9, 15) and stand.fruehere_nachweise == 1
    assert stand.veraltet and stand.iban_letzte4 == "1234" and stand.bankvollstaendigkeit_bis is None
    assert stand.geprueft_am.tzinfo is not None and stand.geprueft_am.hour == 9

    # Gleicher Prüfzeitpunkt: höhere id entscheidet.
    _abgleich(umgebung, admin_ctx, import_id="ABG-3", geprueft_bis=date(2026, 9, 20),
              geprueft_am=datetime(2026, 10, 2, 9, 0, tzinfo=WIEN))
    _, status = _status(umgebung, stammdaten_repo, op_service, admin_ctx, objekt_id="O1")
    assert status.objekte[0].staende[0].geprueft_bis == date(2026, 9, 20)


def test_abgleich_ist_keine_bankvollstaendigkeit(umgebung, stammdaten_repo, op_service, admin_ctx):
    _abgleich(umgebung, admin_ctx, geprueft_bis=_HEUTE, geprueft_am=datetime(2026, 10, 5, 18, 0, tzinfo=WIEN))
    assert umgebung["bank_service"].bankvollstaendigkeit_bestaetigt_bis("BK-A") is None
    umgebung["bank_service"].bestaetige_bankvollstaendigkeit(
        bank_konto_id="BK-A", bestaetigt_bis=date(2026, 8, 31), bestaetigt_von="synthetik",
    )
    _, status = _status(umgebung, stammdaten_repo, op_service, admin_ctx, objekt_id="O1")
    stand = status.objekte[0].staende[0]
    assert not stand.veraltet and stand.bankvollstaendigkeit_bis == date(2026, 8, 31)


def test_idempotent_und_geaenderter_inhalt_wird_abgelehnt(umgebung, admin_ctx, session_factory):
    erste = _abgleich(umgebung, admin_ctx)
    # Derselbe Zeitpunkt in UTC ausgedrückt ist derselbe Inhalt.
    gleiche = _abgleich(umgebung, admin_ctx, geprueft_am=datetime(2026, 10, 1, 7, 0, tzinfo=timezone.utc))
    assert gleiche.id == erste.id
    with pytest.raises(ImportConflictError):
        _abgleich(umgebung, admin_ctx, umfang="geändert")
    e1 = _einzug(umgebung, admin_ctx)
    assert _einzug(umgebung, admin_ctx).id == e1.id
    with pytest.raises(ImportConflictError):
        _einzug(umgebung, admin_ctx, status="BANKBESTAETIGT")
    with session_factory() as s:
        assert len(s.execute(select(AbgleichNachweisTable)).all()) == 1
        assert s.execute(select(EinzugNachweisTable.status)).scalars().all() == ["EINGEREICHT"]


@pytest.mark.parametrize("abweichung", [
    dict(bank_konto_id="BK-B"),  # Bankkonto fremder Gesellschaft
    dict(bank_konto_id="BK-FEHLT"),
    dict(objekt_id="O7"),  # ausgeschlossen
    dict(objekt_id="UNBEKANNT"),
    dict(geprueft_von=date(2026, 10, 1)),
    dict(geprueft_bis=date(2026, 10, 2)),  # nach Prüfzeitpunkt
    dict(geprueft_am=datetime(2026, 10, 1, 9, 0)),  # ohne Zeitzone
    dict(quelle_sha256="xyz"),
    dict(umfang=" "),
])
def test_abgleich_erfassung_validiert(umgebung, admin_ctx, session_factory, abweichung):
    with pytest.raises(NachweisUngueltigError):
        _abgleich(umgebung, admin_ctx, **abweichung)
    with session_factory() as s:
        assert s.execute(select(AbgleichNachweisTable)).all() == []


@pytest.mark.parametrize("abweichung", [
    dict(bank_konto_id="BK-B"), dict(vertrag_id="V9"), dict(vertrag_id="FEHLT"),
    dict(betrag_cent=0), dict(betrag_cent=-1), dict(betrag_cent=True), dict(betrag_cent=1.5),
    dict(status="BEZAHLT"), dict(eingereicht_am=datetime(2026, 9, 18)), dict(nachweis=""),
])
def test_einzug_erfassung_validiert(umgebung, admin_ctx, session_factory, abweichung):
    with pytest.raises(NachweisUngueltigError):
        _einzug(umgebung, admin_ctx, **abweichung)
    with session_factory() as s:
        assert s.execute(select(EinzugNachweisTable)).all() == []


def test_fremde_gesellschaft_kann_weder_schreiben_noch_lesen(umgebung, stammdaten_repo, op_service, ctx_factory, admin_ctx):
    ctx_b = ctx_factory("SYN-B")
    with pytest.raises(NachweisUngueltigError):
        _abgleich(umgebung, ctx_b)
    with pytest.raises(NachweisUngueltigError):
        _einzug(umgebung, ctx_b)
    _abgleich(umgebung, admin_ctx)
    _einzug(umgebung, admin_ctx)
    _, status = _status(umgebung, stammdaten_repo, op_service, ctx_b)
    assert [o.objekt_id for o in status.objekte] == ["O9"]
    assert [v.vertrag_id for v in status.vertraege] == ["V9"]
    assert status.einzuege_offen_cent == 0 and not status.objekte[0].staende
    for verboten in ("O1", "O7"):
        with pytest.raises(UnbekanntesObjektFilterError):
            _status(umgebung, stammdaten_repo, op_service, ctx_b, objekt_id=verboten)


def test_lesepfad_ignoriert_nachweise_mit_unpassendem_bankkonto(umgebung, stammdaten_repo, op_service, admin_ctx, session_factory):
    # Direkt eingefügte (am Repository vorbei) inkonsistente Zeilen.
    with session_factory() as s:
        s.add(AbgleichNachweisTable(
            objekt_id="O1", bank_konto_id="BK-B", geprueft_von=date(2026, 9, 1), geprueft_bis=date(2026, 9, 30),
            geprueft_am=datetime(2026, 10, 1, 7, 0), geprueft_durch="x", umfang="x", quelle_ref="x",
            quelle_sha256=_SHA, import_id="INKONSISTENT",
        ))
        s.add(EinzugNachweisTable(
            vertrag_id="V1", bank_konto_id="BK-B", betrag_cent=100, einzug_am=date(2026, 9, 20),
            eingereicht_am=datetime(2026, 9, 18, 8, 0), referenz="EZ-INKONSISTENT", status="EINGEREICHT", nachweis="x",
        ))
        s.commit()
    _, status = _status(umgebung, stammdaten_repo, op_service, admin_ctx, objekt_id="O1")
    assert status.objekte[0].staende == () and status.objekte[0].ignorierte_nachweise == 1
    v1 = next(v for v in status.vertraege if v.vertrag_id == "V1")
    assert v1.einzuege == () and v1.ignorierte_einzuege == 1 and status.einzuege_offen_cent == 0


def test_eingereicht_ist_nicht_bezahlt_und_bleibt_offen(umgebung, stammdaten_repo, op_service, admin_ctx):
    _einzug(umgebung, admin_ctx)
    _einzug(umgebung, admin_ctx, referenz="EZ-2", betrag_cent=12_300, status="BANKBESTAETIGT")
    _, status = _status(umgebung, stammdaten_repo, op_service, admin_ctx)
    v1 = next(v for v in status.vertraege if v.vertrag_id == "V1")
    assert v1.saldo_cent == 50_000 and v1.offen_cent == 50_000
    assert [z.bezeichnung for z in v1.offene_zeitraeume] == ["2026-09"]
    assert v1.einzuege_offen_cent == 50_000 and status.einzuege_offen_cent == 50_000
    assert status.einzuege_offen_anzahl == 1
    assert {e.status_label for e in v1.einzuege} == {"Eingereicht, Bankeingang noch offen", "Bankeingang bestätigt"}

    # Anderer Eingang deckt den Saldo - der ältere Einzug wird NICHT automatisch abgeglichen.
    op_service.buchen(
        ctx=admin_ctx, konto=umgebung["konten"]["V1"], typ=OPTyp.ZAHLUNG, betrag_cent=50_000,
        belegdatum=date(2026, 10, 2), buchungsdatum=date(2026, 10, 2), faelligkeit=None, beleg_referenz="Überweisung",
    )
    _, status = _status(umgebung, stammdaten_repo, op_service, admin_ctx)
    v1 = next(v for v in status.vertraege if v.vertrag_id == "V1")
    assert v1.saldo_cent == 0 and v1.offene_zeitraeume == ()
    assert v1.einzuege_offen_cent == 50_000 and status.einzuege_offen_anzahl == 1
    assert v1.letzte_erfasste_zahlung == date(2026, 10, 2)


def test_summen_konsistent_mit_rueckstandsuebersicht(umgebung, stammdaten_repo, op_service, admin_ctx):
    for objekt_id in (None, "O1", "O2"):
        uebersicht, status = _status(umgebung, stammdaten_repo, op_service, admin_ctx, objekt_id=objekt_id)
        k = uebersicht.kennzahlen
        assert status.summe_offen_cent == k.summe_positiver_kontostaende_cent == sum(v.offen_cent or 0 for v in status.vertraege)
        assert status.summe_guthaben_cent == k.summe_guthaben_cent == sum(v.guthaben_cent or 0 for v in status.vertraege)
        assert [v.vertrag_id for v in status.vertraege] == [z.vertrag_id for z in uebersicht.mietkonten]
    _, status = _status(umgebung, stammdaten_repo, op_service, admin_ctx, objekt_id="O1")
    v2 = next(v for v in status.vertraege if v.vertrag_id == "V2")
    assert v2.guthaben_cent == 10_000 and v2.offen_cent == 0 and v2.letzte_erfasste_zahlung == date(2026, 9, 3)


def test_lesen_veraendert_keine_daten(umgebung, stammdaten_repo, op_service, admin_ctx, session_factory):
    _abgleich(umgebung, admin_ctx)
    _einzug(umgebung, admin_ctx)
    vorher = _alle_zeilen(session_factory)
    _status(umgebung, stammdaten_repo, op_service, admin_ctx)
    _status(umgebung, stammdaten_repo, op_service, admin_ctx, objekt_id="O1")
    assert _alle_zeilen(session_factory) == vorher


def test_routen_rendern_live_ohne_mutation(umgebung, stammdaten_repo, op_service, admin_ctx, session_factory, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from mietinkasso.backoffice import dependencies as deps
    from mietinkasso.backoffice.routes import dashboard as ui
    from mietinkasso.backoffice.security import SessionStore

    monkeypatch.setattr(ui, "heute_wien", lambda: _HEUTE)
    for name, wert in (
        ("_stammdaten_repo", stammdaten_repo), ("_op_service", op_service), ("_mahn_fall_repo", umgebung["mahn"]),
        ("_bank_repo", umgebung["bank_repo"]), ("_bank_service", umgebung["bank_service"]),
        ("_abgleich_nachweis_repo", umgebung["nachweise"]),
        ("_settings", deps._settings.model_copy(update={"backoffice_password_hash": "configured"})),
        ("_sessions", SessionStore(ttl_sekunden=1000)),
    ):
        monkeypatch.setattr(deps, name, wert)
    app = FastAPI()
    app.include_router(ui.router, prefix="/backoffice")
    client = TestClient(app)
    assert client.get("/backoffice/abgleichstatus", follow_redirects=False).status_code == 303
    sid, _csrf = deps._sessions.erstellen("synthetik-operator")
    client.cookies.set(deps._COOKIE_NAME, sid)

    ohne = client.get("/backoffice/abgleichstatus", params={"objekt_id": "O1"})
    assert ohne.status_code == 200 and "Noch kein Abgleichnachweis" in ohne.text
    assert "Mietkonto Synthetik A" not in ohne.text  # kein Bankkonto ohne Nachweis

    _abgleich(umgebung, admin_ctx)
    _einzug(umgebung, admin_ctx)
    vorher = _alle_zeilen(session_factory)

    start = client.get("/backoffice/", params={"objekt_id": "O1"})
    assert start.status_code == 200
    assert "Zahlungs- und Abgleichstand" in start.text
    assert 'href="/backoffice/abgleichstatus?objekt_id=O1"' in start.text
    assert start.text.index('class="kpi-grid"') < start.text.index('id="abgleichstand"') < start.text.index("<h2>Das ist zu erledigen</h2>")

    seite = client.get("/backoffice/abgleichstatus", params={"objekt_id": "O1"})
    assert seite.status_code == 200
    text = seite.text
    assert "Stand vom 30.09.2026; neuere Eingänge noch prüfen" in text
    assert "Mietkonto Synthetik A" in text and "IBAN endet auf 1234" in text
    assert _IBAN_A not in text and _QUELLE_PFAD not in text and "/srv/" not in text
    assert "Eingereicht, Bankeingang noch offen" in text and "500,00 €" in text
    assert "Letzte erfasste Zahlung" in text and "10.08.2026" in text
    assert "2026-09 (SOLL): 500,00 €" in text  # offener Zeitraum statt "bezahlt bis"
    assert "Keine offenen Posten aus gebuchten Vorschreibungen" in text  # V2 (Guthaben)
    assert "Bezahlt bis" not in text
    assert "Mieter Fremd" not in text
    assert 'value="O7"' not in text  # ausgeschlossenes Objekt nicht wählbar

    for verboten in ("O7", "UNBEKANNT"):
        assert client.get("/backoffice/abgleichstatus", params={"objekt_id": verboten}).status_code == 400
    assert _alle_zeilen(session_factory) == vorher
