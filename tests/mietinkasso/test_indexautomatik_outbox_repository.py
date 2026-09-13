from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from mietinkasso.indexautomatik.repository import ErhoehungsschreibenRepository, RechtsprofilRepository
from mietinkasso.infrastructure.db.tables import ErhoehungsschreibenTable, RechtsprofilTable


@pytest.fixture
def outbox_repo(session_factory) -> ErhoehungsschreibenRepository:
    return ErhoehungsschreibenRepository(session_factory)


@pytest.fixture
def rechtsprofil_repo(session_factory) -> RechtsprofilRepository:
    return RechtsprofilRepository(session_factory)


def _bereites_schreiben(outbox_repo, rechtsprofil_repo, stammdaten_repo, vertrag) -> ErhoehungsschreibenTable:
    profil = rechtsprofil_repo.anlegen(
        RechtsprofilTable(
            vertrag_id=vertrag.id,
            version=1,
            rechtsordnung=vertrag.rechtsordnung,
            bezugsjahr=2024,
            bezugsmonat=1,
            basis_komponenten_ids=[],
            vertrag_beleg_referenz="Beleg",
            erstellt_von="test",
        )
    )
    row = outbox_repo.anlegen(
        ErhoehungsschreibenTable(
            vertrag_id=vertrag.id,
            ziel_bewertungsjahr=2026,
            rechtsprofil_id=profil.id,
            rechtsprofil_version=profil.version,
            mieweg_vorschau_id=None,
            status="BEREIT",
            massgeblicher_termin=date(2026, 4, 1),
            erhoehung_cent=1000,
            schreiben_text="Testschreiben",
            idempotenzschluessel=f"{vertrag.id}:2026",
        )
    )
    return row


def test_claim_ist_exklusiv_bei_zwei_gleichzeitigen_aufrufen(outbox_repo, rechtsprofil_repo, stammdaten_repo, basis_vertrag):
    """Unabhängiger Review (0d65e2b): der bisherige `claim_fuer_versand`
    liess den Status auf BEREIT stehen, sodass ein zweiter, praktisch
    gleichzeitiger Aufruf denselben Fall claimen konnte
    (actual=[True, True] statt [True, False]). Reproduziert exakt den
    von Codex synthetisch nachgestellten Fall."""

    vertrag, _konto = basis_vertrag
    schreiben = _bereites_schreiben(outbox_repo, rechtsprofil_repo, stammdaten_repo, vertrag)

    ergebnis_a = outbox_repo.claim_fuer_versand(schreiben.id)
    ergebnis_b = outbox_repo.claim_fuer_versand(schreiben.id)

    assert [ergebnis_a, ergebnis_b] == [True, False]
    aktualisiert = outbox_repo.get(schreiben.id)
    assert aktualisiert.status == "IN_VERSAND"


def test_verwaiste_in_versand_werden_gefunden(outbox_repo, rechtsprofil_repo, stammdaten_repo, basis_vertrag):
    vertrag, _konto = basis_vertrag
    schreiben = _bereites_schreiben(outbox_repo, rechtsprofil_repo, stammdaten_repo, vertrag)
    alt = datetime.now(timezone.utc) - timedelta(minutes=30)
    assert outbox_repo.claim_fuer_versand(schreiben.id, jetzt=alt) is True

    verwaiste = outbox_repo.verwaiste_in_versand(aelter_als=datetime.now(timezone.utc) - timedelta(minutes=15))
    assert [r.id for r in verwaiste] == [schreiben.id]
