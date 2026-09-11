from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.auth.service import AuthContext
from mietinkasso.domain.enums import Rolle
from mietinkasso.infrastructure.db.base import Base
from mietinkasso.infrastructure.db.session import build_engine
from mietinkasso.infrastructure.db import tables as _tables  # noqa: F401
from mietinkasso.op.repository import OPRepository
from mietinkasso.op.service import OPService
from mietinkasso.stammdaten.repository import StammdatenRepository


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    engine = build_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False, class_=Session)


@pytest.fixture
def stammdaten_repo(session_factory) -> StammdatenRepository:
    return StammdatenRepository(session_factory)


@pytest.fixture
def op_repo(session_factory) -> OPRepository:
    return OPRepository(session_factory)


@pytest.fixture
def op_service(op_repo, stammdaten_repo) -> OPService:
    return OPService(op_repo, stammdaten_repo)


@pytest.fixture
def admin_ctx() -> AuthContext:
    return AuthContext(user_id="markus", rolle=Rolle.ADMIN, gesellschaft_ids=None)


def _make_ctx(gesellschaft_id: str, rolle: Rolle = Rolle.BUCHHALTUNG) -> AuthContext:
    return AuthContext(user_id="buchhaltung-user", rolle=rolle, gesellschaft_ids=frozenset({gesellschaft_id}))


@pytest.fixture
def ctx_factory():
    return _make_ctx


@pytest.fixture
def basis_vertrag(stammdaten_repo: StammdatenRepository):
    """Minimal Pilotobjekt 601 Am Corso, Top 3, ein Dauervermietungs-Vertrag."""

    stammdaten_repo.upsert_gesellschaft(id="7DI", name="7D Immobilien GmbH")
    stammdaten_repo.upsert_objekt(id="601", gesellschaft_id="7DI", bezeichnung="Am Corso")
    stammdaten_repo.upsert_einheit(
        id="601-TOP3", objekt_id="601", bezeichnung="Top 3", nutzungsstatus="DAUERVERMIETUNG"
    )
    stammdaten_repo.upsert_debitor(id="DEB-1001", name="Max Mustermieter", email="mieter@example.at")
    stammdaten_repo.upsert_vertrag(
        id="V-601-3",
        einheit_id="601-TOP3",
        debitor_id="DEB-1001",
        gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL",
        gueltig_von=date(2024, 1, 1),
    )
    vertrag = stammdaten_repo.get_vertrag("V-601-3")
    konto = stammdaten_repo.get_or_create_konto(vertrag=vertrag)
    return vertrag, konto
