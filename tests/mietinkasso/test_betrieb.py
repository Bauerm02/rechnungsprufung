from __future__ import annotations

import os
from datetime import date

from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.bank.repository import BankRepository
from mietinkasso.bank.service import BankImportService
from mietinkasso.domain.enums import OPTyp
from mietinkasso.infrastructure.config import Settings
from mietinkasso.infrastructure.db.base import Base
from mietinkasso.infrastructure.db.session import build_engine, create_all_tables
from mietinkasso.jobs.runner import JobRunner
from mietinkasso.mahnwesen.repository import MahnFallRepository, MahnPolicyRepository
from mietinkasso.mahnwesen.service import MahnwesenService
from mietinkasso.op.repository import OPRepository
from mietinkasso.op.service import OPService
from mietinkasso.stammdaten.repository import StammdatenRepository
from mietinkasso.vorschreibung.repository import VorschreibungRepository
from mietinkasso.vorschreibung.service import VorschreibungService


def test_restore_ist_wiederholbar(tmp_path):
    db_pfad = tmp_path / "mietinkasso_restore_test.db"
    database_url = f"sqlite:///{db_pfad}"
    settings = Settings(database_url=database_url)

    create_all_tables(settings)  # 1. "Backup/Restore"-Lauf: Schema anlegen
    engine = build_engine(database_url)
    factory = sessionmaker(bind=engine, future=True, expire_on_commit=False, class_=Session)
    repo = StammdatenRepository(factory)
    repo.upsert_gesellschaft(id="7DI", name="7D Immobilien GmbH")

    create_all_tables(settings)  # 2. Restore/Wiederanlauf: erneut anlegen, darf nicht crashen oder Daten verlieren
    gesellschaft = repo.get_gesellschaft("7DI")
    assert gesellschaft is not None
    assert gesellschaft.name == "7D Immobilien GmbH"


def test_zwei_worker_gleicher_job_laeuft_nur_einmal(session_factory):
    runner = JobRunner(session_factory)
    aufrufe = {"count": 0}

    def arbeit():
        aufrufe["count"] += 1
        return {"verarbeitet": 42}

    ergebnis_a = runner.einmalig_ausfuehren(job_name="taeglicher_lauf", fachschluessel="2026-04-20", fn=arbeit)
    ergebnis_b = runner.einmalig_ausfuehren(job_name="taeglicher_lauf", fachschluessel="2026-04-20", fn=arbeit)

    assert ergebnis_a == {"verarbeitet": 42}
    assert ergebnis_b is None  # zweiter (paralleler oder nach Neustart erneuter) Aufruf führt NICHT erneut aus
    assert aufrufe["count"] == 1


def test_standardfunktionen_laufen_komplett_ohne_ki_key(session_factory, stammdaten_repo, monkeypatch, ctx_factory):
    """Vorschreibung -> Sollstellung -> Bankzuordnung -> Mahnwesen-Planung
    laufen End-to-End ohne jeden LLM-/KI-API-Key. Entfernt vorsorglich
    gängige Provider-Env-Variablen, um eine versteckte Abhängigkeit
    auszuschließen."""

    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AZURE_OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    stammdaten_repo.upsert_gesellschaft(id="7DI", name="7D Immobilien GmbH")
    stammdaten_repo.upsert_objekt(id="601", gesellschaft_id="7DI", bezeichnung="Am Corso")
    stammdaten_repo.upsert_einheit(id="601-TOP9", objekt_id="601", bezeichnung="Top 9", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten_repo.upsert_debitor(id="DEB-9", name="Ohne-KI Mieterin")
    stammdaten_repo.upsert_vertrag(
        id="V-601-9", einheit_id="601-TOP9", debitor_id="DEB-9", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    vertrag = stammdaten_repo.get_vertrag("V-601-9")
    konto = stammdaten_repo.get_or_create_konto(vertrag=vertrag)
    stammdaten_repo.add_komponente(
        id="K-9-HMZ", vertrag_id=vertrag.id, art="HMZ", bezeichnung="Hauptmietzins", betrag_cent=60_000,
        gueltig_von=date(2024, 1, 1),
    )

    op_service = OPService(OPRepository(session_factory), stammdaten_repo)
    vorschreibung_service = VorschreibungService(VorschreibungRepository(session_factory), stammdaten_repo, op_service)
    bank_repo = BankRepository(session_factory)
    bank_service = BankImportService(bank_repo, stammdaten_repo, op_service)
    mahn_policy_repo = MahnPolicyRepository(session_factory)
    mahn_service = MahnwesenService(MahnFallRepository(session_factory), stammdaten_repo, op_service)

    ctx = ctx_factory("7DI")
    vorschreibung_service.entwurf_erstellen(ctx=ctx, vertrag=vertrag, monat="2026-04")
    vorschreibung_service.sollstellen(ctx=ctx, vertrag=vertrag, konto=konto, monat="2026-04", heute=date(2026, 4, 5))

    bank_repo.upsert_bank_konto(id="BK-9", gesellschaft_id="7DI", iban="AT000000000000000009", bezeichnung="7DI")
    bank_konto = bank_repo.get_bank_konto("BK-9")
    from mietinkasso.bank.importer import CsvSpaltenMapping

    csv_text = f"betrag,datum,referenz\n300.00,2026-04-10,VERTRAG:{vertrag.id}\n"
    mapping = CsvSpaltenMapping(betrag="betrag", buchungsdatum="datum", referenz="referenz")
    transaktion = bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=csv_text, mapping=mapping)[0]
    bank_service.automatisch_zuordnen(ctx=ctx, transaktion=transaktion)

    policy = mahn_policy_repo.freigeben(mahn_policy_repo.anlegen(status="ENTWURF").id)
    ergebnis = mahn_service.planen(
        ctx=ctx, vertrag=vertrag, konto=konto, policy=policy, heute=date(2026, 4, 20), bank_stand_alter_tage=0,
    )

    # 600,00 Soll - 300,00 Zahlung = 300,00 offen -> Stufe 1 wird geplant, alles ohne einen einzigen KI-Aufruf
    assert ergebnis.status == "GEPLANT"
    assert op_service.berechne_saldo(konto.id).saldo_cent == 30_000
