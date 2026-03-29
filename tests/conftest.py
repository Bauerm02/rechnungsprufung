import pytest
from sqlalchemy.orm import Session, sessionmaker

from invoice_automation.infrastructure.db.base import Base
from invoice_automation.infrastructure.db.session import build_engine
from invoice_automation.infrastructure.db import tables as _tables  # noqa: F401


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    engine = build_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False, class_=Session)
