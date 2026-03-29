from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from invoice_automation.infrastructure.config import Settings
from invoice_automation.infrastructure.db.base import Base
from invoice_automation.infrastructure.db import tables as _tables  # noqa: F401


def build_engine(database_url: str):
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine_kwargs = {"future": True, "connect_args": connect_args}
    if ":memory:" in database_url:
        engine_kwargs["poolclass"] = StaticPool
    return create_engine(database_url, **engine_kwargs)


def build_session_factory(database_url: str) -> sessionmaker[Session]:
    engine = build_engine(database_url)
    return sessionmaker(bind=engine, future=True, expire_on_commit=False, class_=Session)


def create_all_tables(settings: Settings) -> None:
    engine = build_engine(settings.database_url)
    Base.metadata.create_all(engine)
