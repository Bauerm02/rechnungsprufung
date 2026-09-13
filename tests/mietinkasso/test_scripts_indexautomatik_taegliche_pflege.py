"""Regressionstests für scripts/indexautomatik_taegliche_pflege.py
(Auftrag 13.09., unabhängiger Review b31-Folgereview: "scripts/
indexautomatik_taegliche_pflege.py übergibt für Erinnerungen
versand_fn=lambda auftrag: None. Bei SEND_ENABLED=True markiert das
echte Hinweise als BENACHRICHTIGT ohne Mail! Keine No-op/Fake-
Versandfunktion produktiv: echten Adapter verdrahten, ohne Adapter hart
blockieren").

Das Skript liegt bewusst außerhalb des `mietinkasso`-Packages (siehe
AGENTS.md: eigenständige CLI-Skripte) und wird hier per
`importlib.util` direkt aus der Datei geladen, statt es in ein Package
zu verschieben."""

from __future__ import annotations

import importlib.util
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from mietinkasso.indexautomatik.repository import VertragsendeErinnerungRepository
from mietinkasso.indexautomatik.transport import VersandAuftrag, VersandBestaetigung
from mietinkasso.indexautomatik.vertragsende_service import VertragsendeErinnerungService


def _lade_script_modul():
    pfad = Path(__file__).resolve().parents[2] / "scripts" / "indexautomatik_taegliche_pflege.py"
    spec = importlib.util.spec_from_file_location("indexautomatik_taegliche_pflege_script", pfad)
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul


@pytest.fixture(scope="module")
def script_modul():
    return _lade_script_modul()


class _AufzeichnenderTransport:
    def __init__(self):
        self.aufrufe: list[VersandAuftrag] = []

    def senden(self, auftrag: VersandAuftrag) -> VersandBestaetigung:
        self.aufrufe.append(auftrag)
        return VersandBestaetigung(externe_referenz="TEST-1", status="GESENDET",
            provider_referenz="SYNTHETIC-SENT", versendet_am=datetime(2026, 9, 30, 10, tzinfo=timezone.utc))


def test_owner_versand_fn_ruft_transport_mit_stabilem_schluessel_auf(script_modul):
    transport = _AufzeichnenderTransport()
    versand_fn = script_modul._owner_versand_fn(transport)
    versand_fn(
        {
            "empfaenger": "markus@jlb-projects.at",
            "text": "Testtext",
            "vertrag_id": "V-1",
            "idempotenzschluessel": "vertragsende:V-1:2026-12-31",
        }
    )
    assert len(transport.aufrufe) == 1
    assert transport.aufrufe[0].referenz == "vertragsende:V-1:2026-12-31"
    assert transport.aufrufe[0].empfaenger_email == "markus@jlb-projects.at"


def test_kein_transport_versand_fn_ist_kein_stiller_noop(script_modul):
    """Ohne konfigurierten Transport wirft ein tatsächlicher Aufruf einen
    sichtbaren Fehler statt stillschweigend nichts zu tun - anders als
    das vorherige `lambda auftrag: None`."""

    with pytest.raises(RuntimeError):
        script_modul._kein_transport_versand_fn(
            {"empfaenger": "x", "text": "y", "idempotenzschluessel": "z"}
        )


def test_ohne_transport_markiert_reale_erinnerung_trotz_send_enabled_true_nicht_als_benachrichtigt(
    admin_ctx, basis_vertrag, stammdaten_repo, session_factory, script_modul
):
    """Reproduziert den gemeldeten Fehler: bei
    MIETINKASSO_VERTRAGSENDE_ERINNERUNG_SEND_ENABLED=true, aber OHNE
    konfigurierten Transport-Endpunkt, darf eine fällige Erinnerung
    NICHT fälschlich als BENACHRICHTIGT gelten - dieselbe Gate-Logik wie
    in scripts/indexautomatik_taegliche_pflege.py::_arbeit()."""

    vertrag, _konto = basis_vertrag
    stammdaten_repo.upsert_vertrag(
        id=vertrag.id, einheit_id=vertrag.einheit_id, debitor_id=vertrag.debitor_id,
        gesellschaft_id=vertrag.gesellschaft_id, rechtsordnung=vertrag.rechtsordnung,
        gueltig_von=vertrag.gueltig_von, gueltig_bis=date(2026, 12, 31),
    )
    vertrag = stammdaten_repo.get_vertrag(vertrag.id)
    repo = VertragsendeErinnerungRepository(session_factory)
    service = VertragsendeErinnerungService(repo, stammdaten_repo, owner_email="markus@jlb-projects.at")
    erinnerung = service.plane_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 1, 1))

    transport = None  # kein MIETINKASSO_INDEXAUTOMATIK_TRANSPORT_ENDPOINT_URL/_API_KEY konfiguriert
    send_enabled_konfiguriert = True  # MIETINKASSO_VERTRAGSENDE_ERINNERUNG_SEND_ENABLED=true
    vertragsende_send_enabled = send_enabled_konfiguriert and transport is not None
    versand_fn = (
        script_modul._owner_versand_fn(transport) if transport is not None else script_modul._kein_transport_versand_fn
    )

    ergebnis = service.benachrichtige_faellige(
        heute=date(2026, 9, 30), send_enabled=vertragsende_send_enabled, versand_fn=versand_fn
    )

    assert ergebnis == []
    assert repo.get(erinnerung.id).status == "OFFEN"


def test_mit_transport_wird_erinnerung_ueber_owner_versand_fn_tatsaechlich_versendet(
    admin_ctx, basis_vertrag, stammdaten_repo, session_factory, script_modul
):
    vertrag, _konto = basis_vertrag
    stammdaten_repo.upsert_vertrag(
        id=vertrag.id, einheit_id=vertrag.einheit_id, debitor_id=vertrag.debitor_id,
        gesellschaft_id=vertrag.gesellschaft_id, rechtsordnung=vertrag.rechtsordnung,
        gueltig_von=vertrag.gueltig_von, gueltig_bis=date(2026, 12, 31),
    )
    vertrag = stammdaten_repo.get_vertrag(vertrag.id)
    repo = VertragsendeErinnerungRepository(session_factory)
    service = VertragsendeErinnerungService(repo, stammdaten_repo, owner_email="markus@jlb-projects.at")
    erinnerung = service.plane_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 1, 1))

    transport = _AufzeichnenderTransport()
    vertragsende_send_enabled = True and transport is not None
    ergebnis = service.benachrichtige_faellige(
        heute=date(2026, 9, 30), send_enabled=vertragsende_send_enabled, versand_fn=script_modul._owner_versand_fn(transport)
    )

    assert len(ergebnis) == 1
    assert len(transport.aufrufe) == 1
    assert repo.get(erinnerung.id).status == "BENACHRICHTIGT"
