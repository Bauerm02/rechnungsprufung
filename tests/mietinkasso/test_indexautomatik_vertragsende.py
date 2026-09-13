from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from mietinkasso.indexautomatik.repository import VertragsendeErinnerungRepository
from mietinkasso.indexautomatik.vertragsende_service import VertragsendeErinnerungService
from mietinkasso.indexautomatik.mailops_client import MailOpsErgebnis


def _test_receipt():
    return MailOpsErgebnis("GESENDET", "SYNTHETIC-REF", "SYNTHETIC-SENT",
        datetime(2026, 9, 30, 10, tzinfo=timezone.utc))


@pytest.fixture
def erinnerung_repo(session_factory) -> VertragsendeErinnerungRepository:
    return VertragsendeErinnerungRepository(session_factory)


@pytest.fixture
def service_mit_owner(erinnerung_repo, stammdaten_repo) -> VertragsendeErinnerungService:
    return VertragsendeErinnerungService(erinnerung_repo, stammdaten_repo, owner_email="markus@jlb-projects.at")


@pytest.fixture
def service_ohne_owner(erinnerung_repo, stammdaten_repo) -> VertragsendeErinnerungService:
    return VertragsendeErinnerungService(erinnerung_repo, stammdaten_repo, owner_email=None)


def _befristeter_vertrag(stammdaten_repo, vertrag, end_datum: date):
    stammdaten_repo.upsert_vertrag(
        id=vertrag.id, einheit_id=vertrag.einheit_id, debitor_id=vertrag.debitor_id,
        gesellschaft_id=vertrag.gesellschaft_id, rechtsordnung=vertrag.rechtsordnung, gueltig_von=vertrag.gueltig_von,
        gueltig_bis=end_datum,
    )
    return stammdaten_repo.get_vertrag(vertrag.id)


def test_faellig_am_ist_end_datum_minus_drei_kalendermonate(admin_ctx, basis_vertrag, service_mit_owner, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    vertrag = _befristeter_vertrag(stammdaten_repo, vertrag, date(2026, 12, 31))
    erinnerung = service_mit_owner.plane_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 1, 1))
    assert erinnerung.faellig_am == date(2026, 9, 30)


def test_unbefristeter_vertrag_erzeugt_keine_erinnerung(admin_ctx, basis_vertrag, service_mit_owner):
    vertrag, _konto = basis_vertrag
    ergebnis = service_mit_owner.plane_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 1, 1))
    assert ergebnis is None


def test_planung_ist_idempotent(admin_ctx, basis_vertrag, service_mit_owner, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    vertrag = _befristeter_vertrag(stammdaten_repo, vertrag, date(2026, 12, 31))
    erste = service_mit_owner.plane_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 1, 1))
    zweite = service_mit_owner.plane_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 2, 1))
    assert erste.id == zweite.id


def test_verlaengerung_invalidiert_alte_erinnerung_und_plant_neue(admin_ctx, basis_vertrag, service_mit_owner, stammdaten_repo, erinnerung_repo):
    vertrag, _konto = basis_vertrag
    vertrag = _befristeter_vertrag(stammdaten_repo, vertrag, date(2026, 12, 31))
    alte = service_mit_owner.plane_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 1, 1))

    vertrag = _befristeter_vertrag(stammdaten_repo, vertrag, date(2027, 12, 31))
    neue = service_mit_owner.plane_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 1, 1))

    assert erinnerung_repo.get(alte.id).status == "UNGUELTIG"
    assert neue.id != alte.id
    assert neue.end_datum == date(2027, 12, 31)


def test_wechsel_auf_unbefristet_invalidiert_alte_erinnerung(admin_ctx, basis_vertrag, service_mit_owner, stammdaten_repo, erinnerung_repo):
    """Unabhängiger Review (3cec004-Folgereview): "Reminder-Invalidierung
    muss auch Wechsel auf unbefristet ... erfassen"."""

    vertrag, _konto = basis_vertrag
    vertrag = _befristeter_vertrag(stammdaten_repo, vertrag, date(2026, 12, 31))
    alte = service_mit_owner.plane_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 1, 1))

    vertrag = _befristeter_vertrag(stammdaten_repo, vertrag, None)
    ergebnis = service_mit_owner.plane_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 1, 1))

    assert ergebnis is None
    assert erinnerung_repo.get(alte.id).status == "UNGUELTIG"


def test_entschiedene_aber_ungesendete_erinnerung_wird_bei_enddatum_aenderung_auch_invalidiert(
    admin_ctx, basis_vertrag, service_mit_owner, stammdaten_repo, erinnerung_repo
):
    vertrag, _konto = basis_vertrag
    vertrag = _befristeter_vertrag(stammdaten_repo, vertrag, date(2026, 12, 31))
    erinnerung = service_mit_owner.plane_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 1, 1))
    service_mit_owner.entscheiden(ctx=admin_ctx, erinnerung_id=erinnerung.id, entscheidung="VERLAENGERN_PRUEFEN", entschieden_von="markus")

    vertrag = _befristeter_vertrag(stammdaten_repo, vertrag, date(2028, 1, 1))
    service_mit_owner.plane_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 1, 1))

    assert erinnerung_repo.get(erinnerung.id).status == "UNGUELTIG"


def test_owner_ist_einziger_empfaenger_kein_mieter_fallback(admin_ctx, basis_vertrag, service_mit_owner, stammdaten_repo, erinnerung_repo):
    vertrag, _konto = basis_vertrag
    vertrag = _befristeter_vertrag(stammdaten_repo, vertrag, date(2026, 12, 31))
    service_mit_owner.plane_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 1, 1))

    empfaenger = []
    benachrichtigt = service_mit_owner.benachrichtige_faellige(
        heute=date(2026, 9, 30), send_enabled=True, versand_fn=lambda auftrag: (empfaenger.append(auftrag["empfaenger"]), _test_receipt())[1]
    )
    assert len(benachrichtigt) == 1
    assert empfaenger == ["markus@jlb-projects.at"]


def test_deaktivierter_versand_belaesst_status_offen_statt_benachrichtigt(admin_ctx, basis_vertrag, service_mit_owner, stammdaten_repo, erinnerung_repo):
    """Unabhängiger Review (fd8c2b2-Folgereview, synthetisch reproduziert:
    "0 Senderaufrufe, Status trotzdem BENACHRICHTIGT statt OFFEN"): bei
    send_enabled=False darf NICHTS als benachrichtigt gelten, sonst geht
    die Erinnerung für einen künftig echten Versand verloren."""

    vertrag, _konto = basis_vertrag
    vertrag = _befristeter_vertrag(stammdaten_repo, vertrag, date(2026, 12, 31))
    erinnerung = service_mit_owner.plane_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 1, 1))

    aufrufe = []
    ergebnis = service_mit_owner.benachrichtige_faellige(heute=date(2026, 9, 30), send_enabled=False, versand_fn=lambda a: aufrufe.append(a))

    assert ergebnis == []
    assert aufrufe == []
    assert erinnerung_repo.get(erinnerung.id).status == "OFFEN"


def test_transportfehler_setzt_unklar_kein_blinder_retry(admin_ctx, basis_vertrag, service_mit_owner, stammdaten_repo, erinnerung_repo):
    from mietinkasso.domain.exceptions import TransportFehlerUngewissError

    vertrag, _konto = basis_vertrag
    vertrag = _befristeter_vertrag(stammdaten_repo, vertrag, date(2026, 12, 31))
    erinnerung = service_mit_owner.plane_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 1, 1))

    def _kaputter_versand(auftrag):
        raise TransportFehlerUngewissError("Fake-Timeout")

    ergebnis = service_mit_owner.benachrichtige_faellige(heute=date(2026, 9, 30), send_enabled=True, versand_fn=_kaputter_versand)
    assert ergebnis == []
    assert erinnerung_repo.get(erinnerung.id).status == "UNKLAR"


def test_entscheidung_nach_geaendertem_enddatum_wird_abgelehnt(admin_ctx, basis_vertrag, service_mit_owner, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    vertrag = _befristeter_vertrag(stammdaten_repo, vertrag, date(2026, 12, 31))
    erinnerung = service_mit_owner.plane_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 1, 1))

    # Vertragsende ändert sich DIREKT in den Stammdaten, ohne dass
    # invalidiere_veraltete zwischenzeitlich erneut gelaufen ist.
    stammdaten_repo.upsert_vertrag(
        id=vertrag.id, einheit_id=vertrag.einheit_id, debitor_id=vertrag.debitor_id,
        gesellschaft_id=vertrag.gesellschaft_id, rechtsordnung=vertrag.rechtsordnung, gueltig_von=vertrag.gueltig_von,
        gueltig_bis=date(2028, 1, 1),
    )
    with pytest.raises(ValueError):
        service_mit_owner.entscheiden(ctx=admin_ctx, erinnerung_id=erinnerung.id, entscheidung="VERLAENGERN_PRUEFEN", entschieden_von="markus")


def test_ohne_konfigurierten_owner_wird_nicht_versendet(admin_ctx, basis_vertrag, service_ohne_owner, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    vertrag = _befristeter_vertrag(stammdaten_repo, vertrag, date(2026, 12, 31))
    service_ohne_owner.plane_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 1, 1))

    aufrufe = []
    ergebnis = service_ohne_owner.benachrichtige_faellige(heute=date(2026, 9, 30), send_enabled=True, versand_fn=lambda a: aufrufe.append(a))
    assert ergebnis == []
    assert aufrufe == []


def test_faellige_wird_nicht_zweimal_benachrichtigt(admin_ctx, basis_vertrag, service_mit_owner, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    vertrag = _befristeter_vertrag(stammdaten_repo, vertrag, date(2026, 12, 31))
    service_mit_owner.plane_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 1, 1))

    erster_lauf = service_mit_owner.benachrichtige_faellige(heute=date(2026, 9, 30), send_enabled=True, versand_fn=lambda a: _test_receipt())
    zweiter_lauf = service_mit_owner.benachrichtige_faellige(heute=date(2026, 10, 15), send_enabled=True, versand_fn=lambda a: _test_receipt())
    assert len(erster_lauf) == 1
    assert zweiter_lauf == []


def test_verspaeteter_lauf_holt_faellige_erinnerung_einmal_nach(admin_ctx, basis_vertrag, service_mit_owner, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    vertrag = _befristeter_vertrag(stammdaten_repo, vertrag, date(2026, 12, 31))
    service_mit_owner.plane_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 1, 1))

    # Der Job lief erst deutlich NACH dem faelligen Datum (30.09.) wieder.
    nachgeholt = service_mit_owner.benachrichtige_faellige(heute=date(2026, 11, 1), send_enabled=True, versand_fn=lambda a: _test_receipt())
    assert len(nachgeholt) == 1


def test_mieterentwurf_erfordert_vorherige_entscheidung(admin_ctx, basis_vertrag, service_mit_owner, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    vertrag = _befristeter_vertrag(stammdaten_repo, vertrag, date(2026, 12, 31))
    erinnerung = service_mit_owner.plane_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 1, 1))
    with pytest.raises(ValueError):
        service_mit_owner.mieterentwurf_erzeugen(ctx=admin_ctx, erinnerung_id=erinnerung.id)


def test_mieterentwurf_erst_nach_entscheidung_und_niemals_automatisch_versendet(admin_ctx, basis_vertrag, service_mit_owner, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    vertrag = _befristeter_vertrag(stammdaten_repo, vertrag, date(2026, 12, 31))
    erinnerung = service_mit_owner.plane_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 1, 1))
    service_mit_owner.entscheiden(ctx=admin_ctx, erinnerung_id=erinnerung.id, entscheidung="NICHT_VERLAENGERN_PRUEFEN", entschieden_von="markus")

    ergebnis = service_mit_owner.mieterentwurf_erzeugen(ctx=admin_ctx, erinnerung_id=erinnerung.id)
    assert ergebnis.mieterentwurf_text is not None
    assert "ENTWURF - NICHT VERSENDET" in ergebnis.mieterentwurf_text


def test_unbekannte_entscheidung_wird_abgelehnt(admin_ctx, basis_vertrag, service_mit_owner, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    vertrag = _befristeter_vertrag(stammdaten_repo, vertrag, date(2026, 12, 31))
    erinnerung = service_mit_owner.plane_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 1, 1))
    with pytest.raises(ValueError):
        service_mit_owner.entscheiden(ctx=admin_ctx, erinnerung_id=erinnerung.id, entscheidung="IRGENDWAS", entschieden_von="markus")


def test_plane_alle_ueberspringt_fremde_gesellschaft_ohne_erinnerung_zu_schreiben(
    admin_ctx, ctx_factory, basis_vertrag, service_mit_owner, stammdaten_repo, erinnerung_repo
):
    """Unabhängiger Review (b31-Folgereview): "Erinnerungsbatch wirft
    CrossTenantError statt scoped skip" - ein Aufrufer ohne Zugriff auf
    die Gesellschaft des Vertrags darf weder eine Exception bekommen
    noch eine Erinnerungszeile geschrieben sehen; der fremde Vertrag
    wird VOR jedem Zugriff übersprungen."""

    vertrag, _konto = basis_vertrag
    vertrag = _befristeter_vertrag(stammdaten_repo, vertrag, date(2026, 12, 31))

    fremder_ctx = ctx_factory("ANDERE-GESELLSCHAFT")
    ergebnisse = service_mit_owner.plane_alle(ctx=fremder_ctx, heute=date(2026, 1, 1))

    assert ergebnisse == []
    assert erinnerung_repo.get_by_enddatum(vertrag.id, vertrag.gueltig_bis) is None
