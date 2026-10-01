from datetime import datetime
from sqlalchemy import Integer, String, Text, UniqueConstraint, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column
from mietinkasso.infrastructure.db.base import Base


class Vorschrift(Base):
    __tablename__ = "eigentuemer_vorschriften"
    __table_args__ = (UniqueConstraint("kennung", "version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kennung: Mapped[str] = mapped_column(String(80), index=True)
    version: Mapped[int] = mapped_column(Integer)
    payload: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    # Original source travels with the existing SQLite backup/restore chain.
    beleg_base64: Mapped[str | None] = mapped_column(Text, nullable=True, deferred=True)
    akteur: Mapped[str] = mapped_column(String(128))
    erstellt_am: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Datei(Base):
    __tablename__ = "eigentuemer_zahlungsdateien"
    id: Mapped[str] = mapped_column(String(35), primary_key=True)
    monat: Mapped[str] = mapped_column(String(7), index=True)
    bank_konto_id: Mapped[str] = mapped_column(String(48))
    xml: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    # ERSTELLT is never a bank execution; GEORGE_IMPORTIERT is evidence only.
    status: Mapped[str] = mapped_column(String(32))
    nachweis: Mapped[str] = mapped_column(Text, default="")
    erstellt_am: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Reservierung(Base):
    __tablename__ = "eigentuemer_zahlungspositionen"
    __table_args__ = (UniqueConstraint("kennung", "monat"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kennung: Mapped[str] = mapped_column(String(80))
    monat: Mapped[str] = mapped_column(String(7))
    datei_id: Mapped[str] = mapped_column(String(35), index=True)
    snapshot: Mapped[str] = mapped_column(Text)


class Lauf(Base):
    __tablename__ = "eigentuemer_zahlungslaeufe"
    monat: Mapped[str] = mapped_column(String(7), primary_key=True)
    erstellt_am: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    bericht: Mapped[str] = mapped_column(Text)
