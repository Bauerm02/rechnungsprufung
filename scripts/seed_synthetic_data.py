#!/usr/bin/env python3
"""Befüllt eine (leere) Mietinkasso-Datenbank mit rein synthetischen
Demodaten für die vier Pilotobjekte. Keine echten Namen, Adressen, IBANs
oder Beträge aus den realen Windows-Dokumenten — alles frei erfunden.

Aufruf:
    MIETINKASSO_DATABASE_URL=sqlite:///./data/mietinkasso_demo.db \\
        python scripts/seed_synthetic_data.py

Idempotent: mehrfacher Aufruf auf derselben DB überschreibt Stammdaten
(upsert) und lässt bereits importierte Ledger-Zeilen unverändert
(gleiche import_id).
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mietinkasso.auth.service import AuthContext  # noqa: E402
from mietinkasso.bank.repository import BankRepository  # noqa: E402
from mietinkasso.domain.enums import Nutzungsstatus, OPTyp, Rolle  # noqa: E402
from mietinkasso.index.repository import IndexRepository  # noqa: E402
from mietinkasso.index.service import IndexService  # noqa: E402
from mietinkasso.infrastructure.config import get_settings  # noqa: E402
from mietinkasso.infrastructure.db.session import build_session_factory, create_all_tables  # noqa: E402
from mietinkasso.mahnwesen.repository import MahnPolicyRepository  # noqa: E402
from mietinkasso.op.repository import OPRepository  # noqa: E402
from mietinkasso.op.service import OPService  # noqa: E402
from mietinkasso.stammdaten.repository import StammdatenRepository  # noqa: E402

ADMIN_CTX = AuthContext(user_id="seed-script", rolle=Rolle.ADMIN, gesellschaft_ids=None)

OBJEKTE = [
    {"id": "601", "gesellschaft_id": "7DI", "bezeichnung": "Am Corso (synthetisch)", "ausgeschlossen": False},
    {"id": "616", "gesellschaft_id": "MABAU", "bezeichnung": "Fockygasse (synthetisch)", "ausgeschlossen": False},
    {"id": "617", "gesellschaft_id": "MABAU", "bezeichnung": "Primelweg (synthetisch)", "ausgeschlossen": False},
    {"id": "619", "gesellschaft_id": "7DI", "bezeichnung": "Gutenberg (synthetisch)", "ausgeschlossen": False},
    {"id": "107", "gesellschaft_id": "7DI", "bezeichnung": "Sieben Dörfer (ausgeschlossen)", "ausgeschlossen": True},
]

EINHEITEN = [
    {"id": "601-TOP1", "objekt_id": "601", "bezeichnung": "Top 1", "status": Nutzungsstatus.DAUERVERMIETUNG},
    {"id": "601-TOP2", "objekt_id": "601", "bezeichnung": "Top 2 (Selfstorage)", "status": Nutzungsstatus.SELFSTORAGE},
    {"id": "616-TOP1", "objekt_id": "616", "bezeichnung": "Top 1", "status": Nutzungsstatus.DAUERVERMIETUNG},
    {"id": "617-TOP1", "objekt_id": "617", "bezeichnung": "Top 1 (Kurzzeit)", "status": Nutzungsstatus.KURZZEITVERMIETUNG},
    {"id": "619-TOP1", "objekt_id": "619", "bezeichnung": "Top 1 (Leerstand)", "status": Nutzungsstatus.LEERSTAND},
]

DEBITOREN = [
    {"id": "DEB-SYN-1", "name": "Testmieterin Anna Beispiel", "email": "anna.beispiel@example.at"},
    {"id": "DEB-SYN-2", "name": "Testmieter Ben Muster", "email": "ben.muster@example.at"},
]

VERTRAEGE = [
    {
        "id": "V-601-1", "einheit_id": "601-TOP1", "debitor_id": "DEB-SYN-1", "gesellschaft_id": "7DI",
        "rechtsordnung": "OESTERREICH_MRG_VOLL", "gueltig_von": date(2023, 1, 1),
        "komponenten": [
            ("HMZ", "Hauptmietzins", 55_000, True),
            ("KUECHE", "Küche", 4_000, False),
            ("PARKPLATZ", "Parkplatz", 6_000, False),
            ("BK_VORAUSZAHLUNG", "BK-Vorauszahlung", 11_000, False),
        ],
    },
    {
        "id": "V-616-1", "einheit_id": "616-TOP1", "debitor_id": "DEB-SYN-2", "gesellschaft_id": "MABAU",
        "rechtsordnung": "OESTERREICH_MRG_TEIL", "gueltig_von": date(2024, 6, 1),
        "komponenten": [
            ("HMZ", "Hauptmietzins", 48_000, True),
            ("KELLER_GARTEN", "Keller", 2_500, False),
            ("HEIZ_WW_VORAUSZAHLUNG", "Heiz-/WW-Vorauszahlung", 9_000, False),
        ],
    },
]


def main() -> None:
    settings = get_settings()
    print(f"Seed-Datenbank: {settings.database_url}")
    create_all_tables(settings)
    session_factory = build_session_factory(settings.database_url)

    stammdaten = StammdatenRepository(session_factory)
    op_service = OPService(OPRepository(session_factory), stammdaten)
    bank_repo = BankRepository(session_factory)
    mahn_policy_repo = MahnPolicyRepository(session_factory)
    index_service = IndexService(IndexRepository(session_factory), stammdaten)

    stammdaten.upsert_gesellschaft(id="7DI", name="7D Immobilien GmbH (synthetisch)")
    stammdaten.upsert_gesellschaft(id="MABAU", name="MaBau Beteiligungs GmbH (synthetisch)")

    for objekt in OBJEKTE:
        stammdaten.upsert_objekt(
            id=objekt["id"], gesellschaft_id=objekt["gesellschaft_id"], bezeichnung=objekt["bezeichnung"],
            ausgeschlossen=objekt["ausgeschlossen"],
        )

    for einheit in EINHEITEN:
        stammdaten.upsert_einheit(
            id=einheit["id"], objekt_id=einheit["objekt_id"], bezeichnung=einheit["bezeichnung"],
            nutzungsstatus=einheit["status"].value,
        )

    for debitor in DEBITOREN:
        stammdaten.upsert_debitor(id=debitor["id"], name=debitor["name"], email=debitor["email"])

    for vertrag_daten in VERTRAEGE:
        stammdaten.upsert_vertrag(
            id=vertrag_daten["id"], einheit_id=vertrag_daten["einheit_id"], debitor_id=vertrag_daten["debitor_id"],
            gesellschaft_id=vertrag_daten["gesellschaft_id"], rechtsordnung=vertrag_daten["rechtsordnung"],
            gueltig_von=vertrag_daten["gueltig_von"],
        )
        for art, bezeichnung, betrag_cent, indexierbar in vertrag_daten["komponenten"]:
            komponente_id = f"K-{vertrag_daten['id']}-{art}"
            if stammdaten.get_komponente(komponente_id) is not None:
                continue  # Komponenten sind Zeitversionen (append-only), nicht erneut anlegen
            stammdaten.add_komponente(
                id=komponente_id, vertrag_id=vertrag_daten["id"], art=art, bezeichnung=bezeichnung,
                betrag_cent=betrag_cent, indexierbar=indexierbar, gueltig_von=vertrag_daten["gueltig_von"],
            )
        vertrag = stammdaten.get_vertrag(vertrag_daten["id"])
        konto = stammdaten.get_or_create_konto(vertrag=vertrag)
        op_service.eroeffnen_gesamtsaldo(
            ctx=ADMIN_CTX, konto=konto, betrag_cent=0, stichtag=date(2026, 8, 31),
            import_id=f"SEED-ERO-{vertrag.id}", akteur="seed-script",
        )
        stammdaten.set_kaution(
            id=f"KAU-{vertrag.id}", vertrag_id=vertrag.id, betrag_cent=200_000, stichtag=date(2026, 8, 31),
            referenz="Synthetische Kaution",
        )

    bank_repo.upsert_bank_konto(
        id="BK-7DI-1", gesellschaft_id="7DI", iban="AT483200000012345864", bezeichnung="7DI Mietkonto (synthetisch)"
    )
    bank_repo.upsert_bank_konto(
        id="BK-MABAU-1", gesellschaft_id="MABAU", iban="AT483200000098765432", bezeichnung="MaBau Mietkonto (synthetisch)"
    )

    if mahn_policy_repo.aktuelle_freigegebene() is None:
        policy = mahn_policy_repo.anlegen(
            stufe1_tage_nach_faelligkeit=settings.mahn_stufe1_tage_nach_faelligkeit,
            stufe2_mindesttage_nach_stufe1_versand=settings.mahn_stufe2_mindesttage_nach_stufe1,
            zinsen_prozent=Decimal("0"), gebuehr_cent=0, status="ENTWURF",
        )
        mahn_policy_repo.freigeben(policy.id)

    if index_service._repository.freigegebene_klausel("V-601-1") is None:
        klausel = index_service.klausel_anlegen(
            vertrag_id="V-601-1", rechtsordnung="OESTERREICH_MRG_VOLL", abschlussdatum=date(2023, 1, 1),
            basis_reihe="VPI2020 (synthetisch)", basis_wert=Decimal("100.0"), basis_monat="2023-01",
            schwelle_prozent=Decimal("0"), daempfung_prozent=Decimal("3"),
            indexierbare_komponenten=["HMZ"],
        )
        index_service.klausel_freigeben(klausel.id, freigegeben_von="seed-script")

    print("Seed abgeschlossen.")
    print("Pilotobjekte:", ", ".join(o["id"] for o in OBJEKTE if not o["ausgeschlossen"]))
    print("Ausgeschlossen:", ", ".join(o["id"] for o in OBJEKTE if o["ausgeschlossen"]))


if __name__ == "__main__":
    main()
