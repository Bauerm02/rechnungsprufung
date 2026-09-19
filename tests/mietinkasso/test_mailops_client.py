"""Provider-contract tests using synthetic data and an in-memory HTTP transport."""
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone

import httpx
import pytest

from mietinkasso.domain.exceptions import TransportFehlerUngewissError
from mietinkasso.indexautomatik.mailops_client import MailOpsAuftrag, MailOpsClient


@pytest.fixture
def auftrag():
    return MailOpsAuftrag("SYNTHETIC:V-1:2027-04", "INDEX", "muster@example.invalid", "Frau Muster",
                         "Neue Vorschreibung", "Der neue Mietbetrag beträgt 510,00 Euro.", "RECHTSPROFIL:1:1:TESTHASH")


def expected_reference(ref):
    return "JLBHV-" + hashlib.sha256(json.dumps(ref, ensure_ascii=False).encode()).hexdigest()


def response_for(auftrag, status="ANGENOMMEN", **changes):
    data = {"status": status, "externe_referenz": expected_reference(auftrag.referenz),
            "provider_referenz": "SYNTHETIC-DRAFT-1", "versendet_am": None}
    data.update(changes)
    return data


@pytest.fixture
def client_factory(tmp_path):
    token = tmp_path / "test-token"
    token.write_text("SYNTHETIC-NOT-A-REAL-TOKEN", encoding="utf-8")
    def factory(handler):
        return MailOpsClient(token_file=token, transport_factory=lambda: httpx.MockTransport(handler))
    return factory


@pytest.mark.parametrize("kind", ["INDEX", "MAHNUNG", "VERTRAGSENDE", "INDEX_MONATSBERICHT"])
def test_exact_protocol_and_acceptance_is_not_sending(kind, auftrag, client_factory):
    auftrag = replace(auftrag, art=kind,
        empfaenger_email="mb@jlb-immo.at" if kind in ("VERTRAGSENDE", "INDEX_MONATSBERICHT") else auftrag.empfaenger_email)
    seen = []
    def provider(req):
        seen.append(req)
        assert req.method == "POST" and req.url.path == "/v1/send"
        body = json.loads(req.content)
        assert set(body) == {"referenz", "art", "empfaenger_email", "empfaenger_name", "betreff", "text", "freigabe_referenz"}
        assert body["freigabe_referenz"] == auftrag.freigabe_referenz
        assert req.headers["Authorization"] == "Bearer SYNTHETIC-NOT-A-REAL-TOKEN"
        return httpx.Response(200, json=response_for(auftrag))
    result = client_factory(provider).senden(auftrag)
    assert len(seen) == 1 and result.status == "ANGENOMMEN"
    assert result.versendet_am is None and not result.versand_bestaetigt


@pytest.mark.parametrize("kind", ["VERTRAGSENDE", "INDEX_MONATSBERICHT"])
def test_owner_only_kinds_reject_any_other_recipient(kind, auftrag):
    """Codex-Betriebsdetail: "Ownerkonfig muss denselben Empfänger erzwingen
    wie die Provider-Empfängergrenze" - selbst ein technisch gültiger,
    aber falscher Empfänger darf für diese beiden Owner-only-Kinds NIE ein
    versandfähiges Payload erzeugen, unabhängig vom Provider."""

    auftrag = replace(auftrag, art=kind, empfaenger_email="jemand-anderes@example.invalid")
    with pytest.raises(ValueError):
        auftrag.payload()


def test_uncertain_send_is_resolved_by_get_only(auftrag, client_factory):
    methods = []
    def provider(req):
        methods.append(req.method)
        if req.method == "POST":
            raise httpx.ReadTimeout("SYNTHETIC-NOT-A-REAL-TOKEN must not leak")
        assert req.url.path == "/v1/status" and req.url.params["referenz"] == auftrag.referenz
        return httpx.Response(200, json=response_for(auftrag, "GESENDET",
            provider_referenz="SYNTHETIC-SENT-1", versendet_am="2027-04-01T09:31:40Z"))
    client = client_factory(provider)
    with pytest.raises(TransportFehlerUngewissError) as error:
        client.senden(auftrag)
    assert "SYNTHETIC-NOT-A-REAL-TOKEN" not in str(error.value)
    assert methods == ["POST"]
    result = client.status_abfragen(auftrag.referenz)
    assert methods == ["POST", "GET"] and result.versand_bestaetigt
    assert result.versendet_am == datetime(2027, 4, 1, 9, 31, 40, tzinfo=timezone.utc)
    assert not hasattr(result, "zugang_bestaetigt_am")


@pytest.mark.parametrize("status", ["ANGENOMMEN", "UNKLAR", "IN_BEARBEITUNG", "FEHLER"])
def test_nonterminal_or_failed_status_does_not_claim_sending(status, auftrag, client_factory):
    result = client_factory(lambda _: httpx.Response(200, json=response_for(auftrag, status))).status_abfragen(auftrag.referenz)
    assert not result.versand_bestaetigt and result.versendet_am is None


@pytest.mark.parametrize("status", ["DEAKTIVIERT", "NICHT_GEFUNDEN"])
def test_disabled_and_absent_are_not_sent(status, auftrag, client_factory):
    result = client_factory(lambda _: httpx.Response(200, json={"status": status, "externe_referenz": ""})).status_abfragen(auftrag.referenz)
    assert result.status == status and not result.versand_bestaetigt


@pytest.mark.parametrize("changes", [
    {"versendet_am": None}, {"versendet_am": "2027-04-01T09:31:40"}, {"versendet_am": "not-a-date"},
    {"provider_referenz": ""}, {"externe_referenz": "JLBHV-other-operation"}, {"status": "unknown-state"},
    {"status": "ANGENOMMEN"}, {"status": "DEAKTIVIERT"}, {"status": "NICHT_GEFUNDEN"},
])
def test_inconsistent_receipt_fails_closed(changes, auftrag, client_factory):
    data = response_for(auftrag, "GESENDET", provider_referenz="SYNTHETIC-SENT", versendet_am="2027-04-01T09:31:40Z")
    data.update(changes)
    with pytest.raises(TransportFehlerUngewissError):
        client_factory(lambda _: httpx.Response(200, json=data)).status_abfragen(auftrag.referenz)


@pytest.mark.parametrize("status", [301, 302, 400, 401, 403, 409, 429, 500, 503])
def test_no_retry_or_redirect_on_provider_error(status, auftrag, client_factory):
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(status, headers={"Location": "https://example.invalid"}, text="SYNTHETIC-SECRET-DETAIL")
    with pytest.raises(TransportFehlerUngewissError) as error:
        client_factory(handler).senden(auftrag)
    assert len(calls) == 1 and "SYNTHETIC-SECRET-DETAIL" not in str(error.value)


@pytest.mark.parametrize("changes", [{"freigabe_referenz": ""}, {"referenz": "x\nBcc:"},
    {"empfaenger_email": "a@example.invalid;b@example.invalid"}, {"art": "OTHER"},
    {"art": "VERTRAGSENDE"}, {"text": "Hallo {{name}}"}, {"text": ""}, {"text": "界" * 15000}])
def test_invalid_request_never_reaches_provider(changes, auftrag, client_factory):
    calls = []
    with pytest.raises(ValueError):
        client_factory(lambda req: calls.append(req)).senden(replace(auftrag, **changes))
    assert not calls


def test_missing_token_never_reaches_provider(tmp_path, auftrag):
    calls = []
    client = MailOpsClient(token_file=tmp_path / "missing", transport_factory=lambda: httpx.MockTransport(lambda req: calls.append(req)))
    with pytest.raises(ValueError) as error:
        client.senden(auftrag)
    assert not calls and str(tmp_path) not in str(error.value)
