"""Additive Nachweistabellen für den Zahlungs- und Abgleichstand
(Auftrag HV-20261005-ABGLEICHSTATUS). Append-only, über das bestehende
`create_all` angelegt - keine Änderung bestehender Zeilen/Tabellen.

- `abgleich_nachweise`: ausdrücklicher Nachweis "Mieteingänge dieses
  Objekts auf diesem Bankkonto wurden für Zeitraum X geprüft". Ist KEINE
  Bankvollständigkeit (`bank_vollstaendigkeit` bleibt unberührt) und
  KEINE Mahnfreigabe.
- `einzug_nachweise`: eingereichter Einzug je Vertrag. Ein eingereichter
  Einzug ist KEIN Zahlungseingang - er verändert keinen Saldo und
  markiert nichts als bezahlt.

Zeitstempel werden als UTC ohne tzinfo gespeichert (SQLite behält keinen
Offset), siehe `repository.py::_utc_naiv`."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from mietinkasso.infrastructure.db.base import Base

#: Ausschließlich von der privaten Quelle gesetzt - nie aus Bankzeilen
#: oder Salden abgeleitet.
EINZUG_STATUS = ("EINGEREICHT", "BANKBESTAETIGT", "ZURUECKGEGEBEN", "STORNIERT")


class AbgleichNachweisTable(Base):
    __tablename__ = "abgleich_nachweise"
    __table_args__ = (UniqueConstraint("import_id", name="uq_abgleich_nachweis_import_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    objekt_id: Mapped[str] = mapped_column(ForeignKey("objekte.id"), index=True)
    bank_konto_id: Mapped[str] = mapped_column(ForeignKey("bank_konten.id"), index=True)
    geprueft_von: Mapped[date] = mapped_column(Date)
    geprueft_bis: Mapped[date] = mapped_column(Date)
    geprueft_am: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    geprueft_durch: Mapped[str] = mapped_column(String(128))
    umfang: Mapped[str] = mapped_column(Text)
    # Private Quellreferenz - wird im Portal nie angezeigt (kann einen
    # Serverpfad enthalten), nur die Prüfsumme.
    quelle_ref: Mapped[str] = mapped_column(Text)
    quelle_sha256: Mapped[str] = mapped_column(String(64))
    import_id: Mapped[str] = mapped_column(String(128))


class EinzugNachweisTable(Base):
    __tablename__ = "einzug_nachweise"
    __table_args__ = (
        UniqueConstraint("referenz", name="uq_einzug_nachweis_referenz"),
        CheckConstraint("betrag_cent > 0", name="ck_einzug_nachweis_betrag_positiv"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vertrag_id: Mapped[str] = mapped_column(ForeignKey("vertraege.id"), index=True)
    bank_konto_id: Mapped[str] = mapped_column(ForeignKey("bank_konten.id"), index=True)
    betrag_cent: Mapped[int] = mapped_column(Integer)
    einzug_am: Mapped[date] = mapped_column(Date)
    eingereicht_am: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    referenz: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(24))
    nachweis: Mapped[str] = mapped_column(Text)


class EinzugStatusNachweisTable(Base):
    """Belegte Statusereignisse; Originaleinzug und Finanzbuchungen bleiben unverändert."""

    __tablename__ = "einzug_status_nachweise"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    einzug_id: Mapped[int] = mapped_column(ForeignKey("einzug_nachweise.id"), index=True)
    status: Mapped[str] = mapped_column(String(24))
    erfasst_am: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    erfasst_durch: Mapped[str] = mapped_column(String(128))
    nachweis: Mapped[str] = mapped_column(Text)
    import_id: Mapped[str] = mapped_column(String(128), unique=True)
