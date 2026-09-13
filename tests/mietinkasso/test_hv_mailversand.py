"""Real business services and HTTP protocol, exclusively synthetic in-memory data."""
import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from mietinkasso.domain.enums import OPTyp, MahnStatus
from mietinkasso.indexautomatik.bootstrap import bauen
from mietinkasso.indexautomatik.mailops_client import MailOpsClient, MailOpsErgebnis
from mietinkasso.indexautomatik.mailversand_service import HVMailversandService
from mietinkasso.infrastructure.config import Settings
from mietinkasso.infrastructure.db.tables import AuditEventTable, MahnFallTable


@pytest.fixture
def hv(session_factory, basis_vertrag, tmp_path):
    token = tmp_path / "synthetic-token"
    token.write_text("SYNTHETIC-TOKEN", encoding="utf-8")
    state = {"status": "ANGENOMMEN", "calls": [], "sent": "2026-09-14T22:30:00Z"}
    def provider(request):
        state["calls"].append(request)
        payload = json.loads(request.content) if request.method == "POST" else None
        ref = payload["referenz"] if payload else request.url.params["referenz"]
        external = "JLBHV-" + hashlib.sha256(json.dumps(ref, ensure_ascii=False).encode()).hexdigest()
        return httpx.Response(200, json={"status": state["status"], "externe_referenz": external,
            "provider_referenz": "SYNTHETIC-SENT-1", "versendet_am": state["sent"] if state["status"] == "GESENDET" else None})
    client = MailOpsClient(token_file=token, transport_factory=lambda: httpx.MockTransport(provider))
    settings = Settings(owner_email="mb@jlb-immo.at", send_enabled=True, hv_mail_allowlist_bestaetigt=True,
        indexautomatik_send_enabled=True, vertragsende_erinnerung_send_enabled=True)
    bundle = bauen(session_factory, settings)
    return HVMailversandService(session_factory, bundle, settings, client=client), state


def _seed_debt(hv, contract, account, ctx, day=date(2026, 9, 13)):
    service, _ = hv
    policy = service.policy_repo.anlegen(stufe1_tage_nach_faelligkeit=7,
        stufe2_mindesttage_nach_stufe1_versand=14, zinsen_prozent=Decimal("0"), gebuehr_cent=0, status="ENTWURF")
    service.policy_repo.freigeben(policy.id)
    service.op_service.buchen(ctx=ctx, konto=account, typ=OPTyp.SOLL, betrag_cent=83000,
        belegdatum=date(2026, 9, 1), buchungsdatum=date(2026, 9, 1), faelligkeit=date(2026, 9, 5),
        beleg_referenz="SYNTHETIC September")
    service.bank_repo.upsert_bank_konto(id="SYNTHETIC-BANK", gesellschaft_id=contract.gesellschaft_id,
        iban="SYNTHETIC-NOT-A-REAL-IBAN", bezeichnung="Testkonto")
    service.bank_service.bestaetige_bankvollstaendigkeit(bank_konto_id="SYNTHETIC-BANK",
        bestaetigt_bis=day, bestaetigt_von="SYNTHETIC-TEST")


def test_mahnung_acceptance_then_actual_sending_get_only_and_stage2_clock(hv, basis_vertrag, admin_ctx, session_factory):
    service, state = hv
    contract, account = basis_vertrag
    _seed_debt(hv, contract, account, admin_ctx)
    result = service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))
    assert result["gesendet"] == 0 and result["geplant"] == 1
    row = service.mahn_repo.list_fuer_vertrag(contract.id)[0]
    assert row.status == "UNSICHER" and row.gesendet_am is None
    body = json.loads(state["calls"][0].content)
    assert body["art"] == "MAHNUNG" and "830,00 EUR" in body["text"]
    assert body["empfaenger_email"] == "mieter@example.at"
    assert len(state["calls"]) == 1
    service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 14))
    assert len(state["calls"]) == 1  # no repeated POST for an uncertain case
    state["status"] = "GESENDET"
    assert service.status_abgleichen(ctx=admin_ctx) == 1
    assert [r.method for r in state["calls"]] == ["POST", "GET"]
    sent = service.mahn_repo.get(row.id)
    assert sent.status == "GESENDET" and sent.gesendet_am.isoformat() == "2026-09-14T22:30:00"
    assert service.status_abgleichen(ctx=admin_ctx) == 0
    with session_factory() as s:
        evidence = list(s.execute(select(AuditEventTable).where(AuditEventTable.aktion == "MAILVERSAND_BESTAETIGT")).scalars())
    assert len(evidence) == 1 and evidence[0].payload["zugang_bestaetigt"] is False
    service.bank_service.bestaetige_bankvollstaendigkeit(bank_konto_id="SYNTHETIC-BANK",
        bestaetigt_bis=date(2026, 9, 28), bestaetigt_von="SYNTHETIC-TEST")
    # Actual sending was 15 September in Vienna, not the 13 September dispatch attempt.
    too_early = service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 28))
    assert too_early["geplant"] == 0 and len(state["calls"]) == 2
    state["sent"] = "2026-09-29T08:00:00Z"
    service.bank_service.bestaetige_bankvollstaendigkeit(bank_konto_id="SYNTHETIC-BANK",
        bestaetigt_bis=date(2026, 9, 29), bestaetigt_von="SYNTHETIC-TEST")
    assert service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 29))["gesendet"] == 1
    assert service.mahn_repo.list_fuer_vertrag(contract.id)[0].stufe == 2
    assert len(state["calls"]) == 3
    service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 30))
    assert len(state["calls"]) == 3


@pytest.mark.parametrize("receipt", [None, MailOpsErgebnis("ANGENOMMEN", "TEST", "TEST")])
def test_noop_or_accepted_callback_cannot_complete_dunning(receipt, hv, basis_vertrag, admin_ctx):
    service, state = hv
    contract, account = basis_vertrag
    _seed_debt(hv, contract, account, admin_ctx)
    planned = service.mahn_service.plane_alle_offenen_forderungen(ctx=admin_ctx, vertrag=contract, konto=account,
        policy=service.policy_repo.aktuelle_freigegebene(), heute=date(2026, 9, 13), bank_bestaetigt_bis=date(2026, 9, 13))[0]
    result = service.mahn_service.versenden(ctx=admin_ctx, mahnfall_id=planned.mahnfall_id,
        heute=date(2026, 9, 13), bank_bestaetigt_bis=date(2026, 9, 13), ungeklaerte_eingaenge_vorhanden=False,
        send_enabled=True, versand_fn=lambda _: receipt)
    assert result.status == "UNSICHER" and service.mahn_repo.get(planned.mahnfall_id).gesendet_am is None
    with pytest.raises(ValueError):
        service.mahn_service.manuell_abklaeren(mahnfall_id=planned.mahnfall_id, neuer_status=MahnStatus.GESENDET)


def test_owner_notice_waits_for_actual_receipt_and_never_goes_to_tenant(hv, basis_vertrag, admin_ctx):
    service, state = hv
    contract, _ = basis_vertrag
    st = service.bundle.stammdaten_repository
    st.upsert_vertrag(id=contract.id, einheit_id=contract.einheit_id, debitor_id=contract.debitor_id,
        gesellschaft_id=contract.gesellschaft_id, rechtsordnung=contract.rechtsordnung,
        gueltig_von=contract.gueltig_von, gueltig_bis=date(2026, 12, 31))
    service.bundle.vertragsende_service.plane_alle(ctx=admin_ctx, heute=date(2026, 9, 30))
    result = service.bundle.vertragsende_service.benachrichtige_faellige(
        heute=date(2026, 9, 30), send_enabled=True, versand_fn=service.owner_senden)
    assert result == []
    row = service.bundle.vertragsende_repository.liste_alle()[0]
    assert row.status == "UNKLAR" and row.benachrichtigt_am is None
    body = json.loads(state["calls"][0].content)
    assert body["art"] == "VERTRAGSENDE" and body["empfaenger_email"] == "mb@jlb-immo.at"
    state.update(status="GESENDET", sent="2026-09-30T11:42:12Z")
    assert service.status_abgleichen(ctx=admin_ctx) == 1
    assert service.bundle.vertragsende_repository.get(row.id).benachrichtigt_am.isoformat() == "2026-09-30T11:42:12"
    assert service.status_abgleichen(ctx=admin_ctx) == 0
    assert [r.method for r in state["calls"]] == ["POST", "GET"]


@pytest.mark.parametrize("flag", ["send_enabled", "hv_mail_allowlist_bestaetigt"])
def test_disabled_dunning_never_calls_provider(flag, hv, basis_vertrag, admin_ctx):
    service, state = hv
    contract, account = basis_vertrag
    _seed_debt(hv, contract, account, admin_ctx)
    setattr(service.settings, flag, False)
    assert service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))["gesendet"] == 0
    assert state["calls"] == []


def test_status_scope_blocks_other_company(hv, basis_vertrag, admin_ctx, ctx_factory):
    service, state = hv
    contract, account = basis_vertrag
    _seed_debt(hv, contract, account, admin_ctx)
    service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 13))
    assert service.status_abgleichen(ctx=ctx_factory("OTHER")) == 0
    assert len(state["calls"]) == 1


def _seed_index(service, contract, ctx):
    b = service.bundle
    b.stammdaten_repository.upsert_debitor(id=contract.debitor_id, name="Max Mustermieter",
        email="mieter@example.at", adresse="Mustergasse 1, 1010 Wien")
    b.stammdaten_repository.add_komponente(id="SYNTHETIC-HMZ", vertrag_id=contract.id,
        art="HMZ", bezeichnung="Hauptmietzins", betrag_cent=100000,
        indexierbar=True, gueltig_von=date(2024, 1, 1))
    profile = b.rechtsprofil_service.entwurf_anlegen(ctx=ctx, vertrag_id=contract.id,
        rechtsordnung="OESTERREICH_MRG_VOLL", ist_wohnungsnutzung=True,
        mrg_zinsbeschraenkung=False, mrg_zinsbeschraenkung_geprueft=True,
        foerderbindung=False, foerderbindung_geprueft=True, ist_altvertrag=False, ist_hauptmiete=True,
        mietzinsobergrenze_cent=None, mietzinsobergrenze_quellenbeleg=None, mietzinsobergrenze_gueltig_bis=None,
        bezugsjahr=2024, bezugsmonat=1, letzte_basis_war_jahresdurchschnitt=False,
        basis_komponenten_ids=["SYNTHETIC-HMZ"], vertraglich_zulaessiger_betrag_cent=200000,
        vertraglicher_quellenbeleg="SYNTHETIC Mietvertrag Punkt 5",
        vertraglicher_fruehestmoeglicher_termin=date(2026, 4, 1),
        vertrag_beleg_referenz="SYNTHETIC Mietvertrag", klausel_referenz="Punkt 5", erstellt_von="TEST")
    b.rechtsprofil_service.freigeben(profile.id, ctx=ctx, freigegeben_von="TEST")
    for year, value in ((2023, "100"), (2024, "102"), (2025, "104")):
        b.vpi_repository.jahreswert_erfassen(reihe="VPI20C18", jahr=year, wert=Decimal(value),
            quelle="SYNTHETIC Statistik", quelle_datum=date(year + 1, 2, 17), erfasst_von="TEST")
    result = b.index_automatik_service.monatslauf_fuer_vertrag(ctx=ctx, vertrag=contract,
        heute=date(2026, 4, 5), akteur="TEST")
    assert result.status == "ERHOEHUNG_ERZEUGT"
    return b.outbox_repository.get(result.erhoehungsschreiben_id)


def test_index_real_mail_protocol_acceptance_is_not_sending_or_rent_change(hv, basis_vertrag, admin_ctx):
    service, state = hv
    contract, _ = basis_vertrag
    row = _seed_index(service, contract, admin_ctx)
    result = service.index_senden(ctx=admin_ctx, row_id=row.id, heute=date(2026, 4, 5))
    assert result.status == "UNKLAR"
    pending = service.bundle.outbox_repository.get(row.id)
    assert pending.versendet_am is None and pending.zugang_bestaetigt_am is None
    body = json.loads(state["calls"][0].content)
    assert body["art"] == "INDEX" and body["freigabe_referenz"].startswith("RECHTSPROFIL:")
    assert service.bundle.stammdaten_repository.get_komponente("SYNTHETIC-HMZ").betrag_cent == 100000
    service.index_senden(ctx=admin_ctx, row_id=row.id, heute=date(2026, 4, 6))
    assert len(state["calls"]) == 1
    state.update(status="GESENDET", sent="2026-04-06T11:12:13Z")
    assert service.status_abgleichen(ctx=admin_ctx) == 1
    confirmed = service.bundle.outbox_repository.get(row.id)
    assert confirmed.versendet_am.isoformat() == "2026-04-06T11:12:13"
    assert confirmed.zugang_bestaetigt_am is None and confirmed.status == "GESENDET"
    assert [r.method for r in state["calls"]] == ["POST", "GET"]
