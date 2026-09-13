"""Gemeinsames Wiring für Indexautomatik-Services - von CLI-Skripten
(`scripts/indexautomatik_*.py`) UND vom Backoffice
(`backoffice/app.py`) genutzt, damit beide exakt dieselben
Repository-/Service-Instanzen aufbauen und nicht auseinanderlaufen
können."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.index.repository import IndexRepository
from mietinkasso.index.service import IndexService
from mietinkasso.indexautomatik.outbox_service import ErhoehungsschreibenOutboxService
from mietinkasso.indexautomatik.rechtsprofil import RechtsprofilService
from mietinkasso.indexautomatik.repository import (
    ErhoehungsschreibenRepository,
    IndexautomatikLaufRepository,
    RechtsprofilRepository,
    VertragsendeErinnerungRepository,
    VpiRepository,
)
from mietinkasso.indexautomatik.service import IndexautomatikService
from mietinkasso.indexautomatik.umsetzung_service import IndexSollUmsetzungService
from mietinkasso.indexautomatik.vertragsende_service import VertragsendeErinnerungService
from mietinkasso.infrastructure.config import Settings
from mietinkasso.mieweg_vorschau.repository import MieWegVorschauRepository
from mietinkasso.mieweg_vorschau.service import MieWegVorschauService
from mietinkasso.stammdaten.repository import StammdatenRepository


@dataclass
class IndexautomatikBundle:
    stammdaten_repository: StammdatenRepository
    rechtsprofil_repository: RechtsprofilRepository
    rechtsprofil_service: RechtsprofilService
    lauf_repository: IndexautomatikLaufRepository
    outbox_repository: ErhoehungsschreibenRepository
    outbox_service: ErhoehungsschreibenOutboxService
    vpi_repository: VpiRepository
    index_repository: IndexRepository
    index_service: IndexService
    index_automatik_service: IndexautomatikService
    vertragsende_repository: VertragsendeErinnerungRepository
    vertragsende_service: VertragsendeErinnerungService
    soll_umsetzung_service: IndexSollUmsetzungService


def bauen(session_factory: sessionmaker[Session], settings: Settings) -> IndexautomatikBundle:
    stammdaten_repository = StammdatenRepository(session_factory)
    rechtsprofil_repository = RechtsprofilRepository(session_factory)
    index_repository = IndexRepository(session_factory)
    rechtsprofil_service = RechtsprofilService(rechtsprofil_repository, stammdaten_repository, index_repository)

    lauf_repository = IndexautomatikLaufRepository(session_factory)
    outbox_repository = ErhoehungsschreibenRepository(session_factory)
    vpi_repository = VpiRepository(session_factory)
    mieweg_repository = MieWegVorschauRepository(session_factory)
    mieweg_service = MieWegVorschauService(mieweg_repository, stammdaten_repository)
    index_service = IndexService(index_repository, stammdaten_repository)
    outbox_service = ErhoehungsschreibenOutboxService(
        outbox_repository, stammdaten_repository, rechtsprofil_repository, rechtsprofil_service,
        jlb_signatur=settings.indexautomatik_jlb_signatur,
    )
    index_automatik_service = IndexautomatikService(
        stammdaten_repository=stammdaten_repository,
        rechtsprofil_service=rechtsprofil_service,
        lauf_repository=lauf_repository,
        outbox_repository=outbox_repository,
        vpi_repository=vpi_repository,
        mieweg_service=mieweg_service,
        index_repository=index_repository,
        index_service=index_service,
        outbox_service=outbox_service,
    )

    vertragsende_repository = VertragsendeErinnerungRepository(session_factory)
    vertragsende_service = VertragsendeErinnerungService(
        vertragsende_repository, stammdaten_repository, owner_email=settings.owner_email
    )

    soll_umsetzung_service = IndexSollUmsetzungService(
        session_factory=session_factory,
        stammdaten_repository=stammdaten_repository,
        rechtsprofil_service=rechtsprofil_service,
        erhoehungsschreiben_repository=outbox_repository,
    )

    return IndexautomatikBundle(
        stammdaten_repository=stammdaten_repository,
        rechtsprofil_repository=rechtsprofil_repository,
        rechtsprofil_service=rechtsprofil_service,
        lauf_repository=lauf_repository,
        outbox_repository=outbox_repository,
        outbox_service=outbox_service,
        vpi_repository=vpi_repository,
        index_repository=index_repository,
        index_service=index_service,
        index_automatik_service=index_automatik_service,
        vertragsende_repository=vertragsende_repository,
        vertragsende_service=vertragsende_service,
        soll_umsetzung_service=soll_umsetzung_service,
    )
