"""Additive Tabellen für die Bindung Bankquelle -> Bankkonto -> Objekt
(Auftrag HV-20261005-BANKQUELLENBINDUNG). Über das bestehende
`create_all` angelegt - bestehende Finanztabellen (`bank_konten`,
`bank_transaktionen`, `zuordnungen`, `op_positionen`) werden weder
verändert noch nachbefüllt.

- `bank_quellen`: eine Bankquelle = Tupel (Anbieter, Zugangsreferenz,
  Anbieter-Kontoreferenz) mit GENAU einem internen Bankkonto, der
  normalisierten eigenen IBAN, der Gesellschaft und der Kontorolle. Die
  Referenzen sind nicht geheime Kennungen (z. B. Teilnehmer-/Kunden-ID),
  NIE Passwörter, Schlüssel oder Tokens. Unveränderlich nach dem Anlegen;
  dasselbe Tupel kann nie auf ein zweites Bankkonto zeigen (UNIQUE).
- `bank_quellen_bindungen`: append-only Revisionen je Objekt. Die Zeile
  mit der höchsten Revision ist die aktuelle Bindung; `WIDERRUFEN` ist ein
  Grabstein (fail-closed), kein Rückfall auf das Legacy-Verhalten.
  Mehrere Objekte dürfen dieselbe Quelle referenzieren.

Zeitstempel: UTC ohne tzinfo (wie `abgleichstatus/models.py`)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from mietinkasso.infrastructure.db.base import Base

#: Nur `MIETE` darf an ein Mietobjekt gebunden werden; die übrigen Werte
#: existieren, damit eine Kautions-/Kredit-/unklare Rolle ausdrücklich
#: erkannt und abgelehnt wird (z. B. aus einer Anbieterantwort).
KONTOROLLE_MIETE = "MIETE"
KONTOROLLEN = (KONTOROLLE_MIETE, "KAUTION", "KREDIT", "UNBEKANNT")

BINDUNG_AKTIV = "AKTIV"
BINDUNG_WIDERRUFEN = "WIDERRUFEN"


class BankQuelleTable(Base):
    __tablename__ = "bank_quellen"
    __table_args__ = (
        UniqueConstraint("anbieter", "zugang_ref", "konto_ref", name="uq_bank_quelle_anbieter_tupel"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    anbieter: Mapped[str] = mapped_column(String(64))
    zugang_ref: Mapped[str] = mapped_column(String(128))
    konto_ref: Mapped[str] = mapped_column(String(128))
    bank_konto_id: Mapped[str] = mapped_column(ForeignKey("bank_konten.id"), index=True)
    gesellschaft_id: Mapped[str] = mapped_column(ForeignKey("gesellschaften.id"), index=True)
    iban_norm: Mapped[str] = mapped_column(String(34))
    kontorolle: Mapped[str] = mapped_column(String(16))
    erstellt_von: Mapped[str] = mapped_column(String(128))
    erstellt_am: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class BankQuellenBindungTable(Base):
    __tablename__ = "bank_quellen_bindungen"
    __table_args__ = (
        # Backstop für die optimistische Prüfung: zwei gleichzeitige
        # Änderungen gegen denselben Stand können nie beide Revision n+1
        # schreiben.
        UniqueConstraint("objekt_id", "revision", name="uq_bank_quellen_bindung_revision"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    objekt_id: Mapped[str] = mapped_column(ForeignKey("objekte.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    # Auch ein Grabstein behält die zuletzt gebundene Quelle (Audit).
    quelle_id: Mapped[int] = mapped_column(ForeignKey("bank_quellen.id"), index=True)
    vorgaenger_id: Mapped[int | None] = mapped_column(ForeignKey("bank_quellen_bindungen.id"), nullable=True)
    nachweis_ref: Mapped[str] = mapped_column(String(256))
    akteur: Mapped[str] = mapped_column(String(128))
    zeitpunkt: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fingerprint: Mapped[str] = mapped_column(String(64))
