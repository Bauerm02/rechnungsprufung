"""Gemeinsames Wiring für die variable Monatsabrechnung
(KURZZEITVERMIETUNG/SELFSTORAGE, Auftrag 13.09., HV-20260913-DASHBOARD)
- vom Backoffice genutzt, analog zu `indexautomatik/bootstrap.py`."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.stammdaten.repository import StammdatenRepository
from mietinkasso.variableabrechnung.komponenten_freigabe import (
    KomponentenNettoMietFreigabeRepository,
    KomponentenNettoMietFreigabeService,
)
from mietinkasso.variableabrechnung.repository import VariableAbrechnungRepository
from mietinkasso.variableabrechnung.service import VariableAbrechnungService


@dataclass
class VariableAbrechnungBundle:
    repository: VariableAbrechnungRepository
    service: VariableAbrechnungService
    komponenten_freigabe_repository: KomponentenNettoMietFreigabeRepository
    komponenten_freigabe_service: KomponentenNettoMietFreigabeService


def bauen(session_factory: sessionmaker[Session], stammdaten_repository: StammdatenRepository) -> VariableAbrechnungBundle:
    repository = VariableAbrechnungRepository(session_factory)
    service = VariableAbrechnungService(repository, stammdaten_repository)
    komponenten_freigabe_repository = KomponentenNettoMietFreigabeRepository(session_factory)
    komponenten_freigabe_service = KomponentenNettoMietFreigabeService(komponenten_freigabe_repository, stammdaten_repository)
    return VariableAbrechnungBundle(
        repository=repository, service=service,
        komponenten_freigabe_repository=komponenten_freigabe_repository,
        komponenten_freigabe_service=komponenten_freigabe_service,
    )
