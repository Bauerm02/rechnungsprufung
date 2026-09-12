"""SQLAlchemy schema for the Hausverwaltung & Mietinkasso ledger.

Design invariants enforced here via constraints (see docs/hausverwaltung
/RAHMENPROGRAMM.md for the Fachregeln these map to):

- Gesellschaft, Objekt, Einheit, Vertrag, Debitor and Konto are separate
  ID spaces (no reuse of an Objekt-Kennzeichen like "617" as a Konto- or
  Bank-identifier).
- OPPosition rows are append-only; corrections are new rows referencing
  the row they storno/correct, never in-place edits.
- Idempotent import: unique `import_id` per source system + a content
  hash so replays are no-ops and altered-content-same-id is a detectable
  conflict.
- Kaution lives in its own table, structurally incapable of netting
  against a Konto balance because no code path joins them.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from mietinkasso.infrastructure.db.base import Base


class GesellschaftTable(Base):
    __tablename__ = "gesellschaften"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ObjektTable(Base):
    __tablename__ = "objekte"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    gesellschaft_id: Mapped[str] = mapped_column(ForeignKey("gesellschaften.id"), index=True)
    bezeichnung: Mapped[str] = mapped_column(String(256))
    adresse: Mapped[str | None] = mapped_column(String(512), nullable=True)
    ausgeschlossen: Mapped[bool] = mapped_column(Boolean, default=False)


class EinheitTable(Base):
    __tablename__ = "einheiten"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    objekt_id: Mapped[str] = mapped_column(ForeignKey("objekte.id"), index=True)
    bezeichnung: Mapped[str] = mapped_column(String(64))
    nutzungsstatus: Mapped[str] = mapped_column(String(32))
    flaeche_qm: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    miteigentumsanteile: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)


class DebitorTable(Base):
    __tablename__ = "debitoren"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    adresse: Mapped[str | None] = mapped_column(String(512), nullable=True)


class VertragTable(Base):
    __tablename__ = "vertraege"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    einheit_id: Mapped[str] = mapped_column(ForeignKey("einheiten.id"), index=True)
    debitor_id: Mapped[str] = mapped_column(ForeignKey("debitoren.id"), index=True)
    gesellschaft_id: Mapped[str] = mapped_column(ForeignKey("gesellschaften.id"), index=True)
    rechtsordnung: Mapped[str] = mapped_column(String(48))
    gueltig_von: Mapped[date] = mapped_column(Date)
    gueltig_bis: Mapped[date | None] = mapped_column(Date, nullable=True)
    faelligkeit_tag: Mapped[int] = mapped_column(Integer, default=5)
    zahlungsfrist_tage: Mapped[int] = mapped_column(Integer, default=14)


class VertragsKomponenteTable(Base):
    """Time-bound contract line item (HMZ, Küche, Parkplatz, BK-VZ, ...)."""

    __tablename__ = "vertrags_komponenten"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    vertrag_id: Mapped[str] = mapped_column(ForeignKey("vertraege.id"), index=True)
    art: Mapped[str] = mapped_column(String(48))
    bezeichnung: Mapped[str] = mapped_column(String(128))
    betrag_cent: Mapped[int] = mapped_column(Integer)
    ust_satz_promille: Mapped[int] = mapped_column(Integer, default=10000)
    indexierbar: Mapped[bool] = mapped_column(Boolean, default=False)
    gueltig_von: Mapped[date] = mapped_column(Date)
    gueltig_bis: Mapped[date | None] = mapped_column(Date, nullable=True)


class KautionTable(Base):
    __tablename__ = "kautionen"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    vertrag_id: Mapped[str] = mapped_column(ForeignKey("vertraege.id"), index=True, unique=True)
    betrag_cent: Mapped[int] = mapped_column(Integer)
    stichtag: Mapped[date] = mapped_column(Date)
    referenz: Mapped[str | None] = mapped_column(String(256), nullable=True)


class SperreTable(Base):
    __tablename__ = "sperren"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vertrag_id: Mapped[str] = mapped_column(ForeignKey("vertraege.id"), index=True)
    grund: Mapped[str] = mapped_column(String(48))
    gesetzt_am: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    aufgehoben_am: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    kommentar: Mapped[str | None] = mapped_column(Text, nullable=True)


class VertragPruefungTable(Base):
    """Versionierte, nachvollziehbare Vertragsprüfung (Auftrag 12.09.,
    Paket B): jede Prüfung ist eine neue, unveränderliche Version mit
    Pflicht-Quellenbeleg ("keine beleglose Klassifizierung") - nur
    `fachstatus == GEPRUEFT` schreibt die gewählte `rechtsordnung`
    tatsächlich auf `VertragTable.rechtsordnung` zurück
    (`vertragspruefung/service.py`), ein `ENTWURF` bleibt wirkungslos
    sichtbar."""

    __tablename__ = "vertrag_pruefungen"
    __table_args__ = (UniqueConstraint("vertrag_id", "version", name="uq_vertrag_pruefung_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vertrag_id: Mapped[str] = mapped_column(ForeignKey("vertraege.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    rechtsordnung: Mapped[str] = mapped_column(String(48))
    fachstatus: Mapped[str] = mapped_column(String(16))
    quellenbeleg_referenz: Mapped[str] = mapped_column(String(256))
    kommentar: Mapped[str | None] = mapped_column(Text, nullable=True)
    erstellt_von: Mapped[str] = mapped_column(String(128))
    erstellt_am: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IndexPruefbedarfTable(Base):
    """Bewusst von `IndexKlauselTable` GETRENNTE Ablage für unvollständige
    Indexangaben (Auftrag 12.09., Paket B) - alle Fachfelder sind
    nullable, es gibt HIER keinen Freigabemechanismus/keine Wirkung auf
    Buchungen. Eine spätere, tatsächlich vollständige Klausel entsteht
    weiterhin ausschließlich über `index/service.py::klausel_anlegen`
    (dort bleiben alle Pflichtfelder unverändert Pflicht)."""

    __tablename__ = "index_pruefbedarf"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vertrag_id: Mapped[str] = mapped_column(ForeignKey("vertraege.id"), index=True)
    rechtsordnung: Mapped[str | None] = mapped_column(String(48), nullable=True)
    basis_reihe: Mapped[str | None] = mapped_column(String(64), nullable=True)
    basis_wert: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    basis_monat: Mapped[str | None] = mapped_column(String(7), nullable=True)
    kommentar: Mapped[str | None] = mapped_column(Text, nullable=True)
    erstellt_von: Mapped[str] = mapped_column(String(128))
    erstellt_am: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KontoTable(Base):
    """One Mietkonto per Vertrag. Deliberately its own ID space."""

    __tablename__ = "konten"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    vertrag_id: Mapped[str] = mapped_column(ForeignKey("vertraege.id"), index=True, unique=True)
    debitor_id: Mapped[str] = mapped_column(ForeignKey("debitoren.id"), index=True)
    gesellschaft_id: Mapped[str] = mapped_column(ForeignKey("gesellschaften.id"), index=True)
    waehrung: Mapped[str] = mapped_column(String(3), default="EUR")
    eroeffnung_modus: Mapped[str | None] = mapped_column(String(32), nullable=True)
    eroeffnung_stichtag: Mapped[date | None] = mapped_column(Date, nullable=True)


class OPPositionTable(Base):
    """Append-only open-item ledger line. Never updated after insert except
    for `status` transitioning AKTIV -> STORNIERT via a Korrektur row."""

    __tablename__ = "op_positionen"
    __table_args__ = (
        UniqueConstraint("import_id", name="uq_op_import_id"),
        # Eröffnung ist fachlich einmalig je Konto (Fachregel 2): höchstens
        # eine AKTIVE EROEFFNUNG-Zeile pro Konto, unabhängig vom import_id/
        # Dateinamen der jeweiligen Quelle. DB-seitig statt nur im Service
        # erzwungen, damit auch ein zweiter Import mit abweichender
        # import_id keine zweite Eröffnung erzeugen kann.
        Index(
            "uq_op_eroeffnung_pro_konto",
            "konto_id",
            unique=True,
            sqlite_where=text("typ = 'EROEFFNUNG' AND status = 'AKTIV'"),
            postgresql_where=text("typ = 'EROEFFNUNG' AND status = 'AKTIV'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    konto_id: Mapped[str] = mapped_column(ForeignKey("konten.id"), index=True)
    typ: Mapped[str] = mapped_column(String(32))
    betrag_cent: Mapped[int] = mapped_column(Integer)
    leistungsperiode: Mapped[str | None] = mapped_column(String(7), nullable=True)
    belegdatum: Mapped[date] = mapped_column(Date)
    buchungsdatum: Mapped[date] = mapped_column(Date)
    faelligkeit: Mapped[date | None] = mapped_column(Date, nullable=True)
    faelligkeit_bekannt: Mapped[bool] = mapped_column(Boolean, default=True)
    beleg_referenz: Mapped[str | None] = mapped_column(String(256), nullable=True)
    aenderungsgrund: Mapped[str | None] = mapped_column(Text, nullable=True)
    quelle_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    import_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="AKTIV")
    storniert_durch_id: Mapped[int | None] = mapped_column(
        ForeignKey("op_positionen.id"), nullable=True
    )
    quelle_system: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Herkunfts-/Bezugstracking für Bankbuchungen: welche Banktransaktion hat
    # diese Zeile erzeugt (ZAHLUNG/RUECKLASTSCHRIFT), und auf welche andere
    # OP-Zeile bezieht sie sich (z. B. RUECKLASTSCHRIFT -> die zurückgebuchte
    # ZAHLUNG). Ermöglicht die kumulative Rückbuchungsgrenze je Ursprungs-
    # zahlung und die verfügbare Belastungshöhe je Rücklastschrift-Transaktion
    # exakt nachzurechnen, statt sie zu erraten.
    bank_transaktion_id: Mapped[int | None] = mapped_column(ForeignKey("bank_transaktionen.id"), nullable=True, index=True)
    bezieht_sich_auf_id: Mapped[int | None] = mapped_column(ForeignKey("op_positionen.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VorschreibungTable(Base):
    __tablename__ = "vorschreibungen"
    __table_args__ = (UniqueConstraint("vertrag_id", "monat", name="uq_vorschreibung_vertrag_monat"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vertrag_id: Mapped[str] = mapped_column(ForeignKey("vertraege.id"), index=True)
    monat: Mapped[str] = mapped_column(String(7))
    status: Mapped[str] = mapped_column(String(32), default="ENTWURF")
    faelligkeit: Mapped[date | None] = mapped_column(Date, nullable=True)
    op_position_id: Mapped[int | None] = mapped_column(ForeignKey("op_positionen.id"), nullable=True)
    dokument_zugestellt_am: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    zustellnachweis: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    hauptbuch_exportiert_am: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    export_nachweis: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VorschreibungPositionTable(Base):
    __tablename__ = "vorschreibung_positionen"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vorschreibung_id: Mapped[int] = mapped_column(ForeignKey("vorschreibungen.id"), index=True)
    art: Mapped[str] = mapped_column(String(48))
    bezeichnung: Mapped[str] = mapped_column(String(128))
    betrag_cent: Mapped[int] = mapped_column(Integer)
    ust_satz_promille: Mapped[int] = mapped_column(Integer)


class BankKontoTable(Base):
    __tablename__ = "bank_konten"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    gesellschaft_id: Mapped[str] = mapped_column(ForeignKey("gesellschaften.id"), index=True)
    iban: Mapped[str] = mapped_column(String(34))
    bezeichnung: Mapped[str] = mapped_column(String(128))


class BankTransaktionTable(Base):
    __tablename__ = "bank_transaktionen"
    __table_args__ = (
        UniqueConstraint("import_id", name="uq_bank_import_id"),
        # Nur relevant für Zeilen ohne bankseitig eindeutige Kennung
        # (hat_native_id=False): verhindert, dass eine wirtschaftlich
        # identisch aussehende Zeile aus einem überlappenden Re-Export
        # unter einer NEUEN import_id (z. B. weil sie an anderer
        # Zeilennummer steht) als zweite, echte Transaktion durchrutscht.
        Index("ix_bank_fingerprint", "bank_konto_id", "fingerprint_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bank_konto_id: Mapped[str] = mapped_column(ForeignKey("bank_konten.id"), index=True)
    betrag_cent: Mapped[int] = mapped_column(Integer)
    waehrung: Mapped[str] = mapped_column(String(3), default="EUR")
    buchungsdatum: Mapped[date] = mapped_column(Date)
    valuta: Mapped[date | None] = mapped_column(Date, nullable=True)
    referenz: Mapped[str | None] = mapped_column(String(256), nullable=True)
    gegenkonto_iban: Mapped[str | None] = mapped_column(String(34), nullable=True)
    gegenkonto_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    quelle_typ: Mapped[str] = mapped_column(String(16))
    quelle_hash: Mapped[str] = mapped_column(String(128))
    import_id: Mapped[str] = mapped_column(String(128))
    hat_native_id: Mapped[bool] = mapped_column(Boolean, default=False)
    fingerprint_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    roh_zeile: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ZuordnungTable(Base):
    __tablename__ = "zuordnungen"
    __table_args__ = (
        # Die Idempotenz hängt an einer vom Aufrufer explizit vergebenen
        # Vorgangs-ID, NICHT an (Transaktion, OP, Betrag): zwei echte,
        # unabhängige Teilzuordnungen mit zufällig identischem Betrag auf
        # dieselbe (Transaktion, OP)-Kombination müssen möglich sein, ein
        # bloßer Retry derselben Anfrage (gleiche Vorgangs-ID) darf aber
        # nie ein zweites Mal buchen.
        UniqueConstraint("vorgang_id", name="uq_zuordnung_vorgang_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bank_transaktion_id: Mapped[int] = mapped_column(ForeignKey("bank_transaktionen.id"), index=True)
    op_position_id: Mapped[int] = mapped_column(ForeignKey("op_positionen.id"), index=True)
    betrag_cent: Mapped[int] = mapped_column(Integer)
    match_typ: Mapped[str] = mapped_column(String(32))
    vorgang_id: Mapped[str] = mapped_column(String(128))
    erstellt_am: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BankVollstaendigkeitTable(Base):
    """Explizite Bestätigung "Bankstand ist vollständig bis Datum X" je
    Bankkonto - im Unterschied zum bloßen Datum der letzten importierten
    Zeile (das nur beweist, dass IRGENDEINE Zeile bis dahin existiert,
    nicht dass der Import lückenlos/vollständig war). Nur eine solche
    Bestätigung darf das Mahnwesen als "Bank ist aktuell" akzeptieren."""

    __tablename__ = "bank_vollstaendigkeit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bank_konto_id: Mapped[str] = mapped_column(ForeignKey("bank_konten.id"), index=True)
    bestaetigt_bis: Mapped[date] = mapped_column(Date)
    bestaetigt_von: Mapped[str] = mapped_column(String(128))
    bestaetigt_am: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IndexKlauselTable(Base):
    __tablename__ = "index_klauseln"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vertrag_id: Mapped[str] = mapped_column(ForeignKey("vertraege.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    rechtsordnung: Mapped[str] = mapped_column(String(48))
    # Muss exakt einem tatsächlich implementierten Rechenprofil entsprechen
    # (siehe index/service.py::UNTERSTUETZTE_BERECHNUNGSPROFILE); alles
    # andere (insb. fehlende April-/Jahresdurchschnitts-/Erstvalorisierungs-
    # /Altvertragsübergangslogik) bleibt gesperrt statt stillschweigend mit
    # der einfachen Formel gerechnet zu werden.
    berechnungsprofil: Mapped[str | None] = mapped_column(String(64), nullable=True)
    klausel_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    abschlussdatum: Mapped[date] = mapped_column(Date)
    basis_reihe: Mapped[str] = mapped_column(String(64))
    basis_wert: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    basis_monat: Mapped[str] = mapped_column(String(7))
    letzte_anpassung_monat: Mapped[str | None] = mapped_column(String(7), nullable=True)
    schwelle_prozent: Mapped[Decimal] = mapped_column(Numeric(6, 3), default=Decimal("0"))
    # Viele reale Verträge verlangen "streng über 3%"/"über 2%" statt "ab
    # 3%"/"ab 2%" - das ist keine reine Rundungsfrage, sondern eine explizit
    # zu erfassende Vertragsklausel. True = Grenzfall (Veränderung == Schwelle)
    # löst aus; False = Grenzfall löst NICHT aus, erst strikt darüber.
    schwelle_inklusive: Mapped[bool] = mapped_column(Boolean, default=True)
    daempfung_prozent: Mapped[Decimal | None] = mapped_column(Numeric(6, 3), nullable=True)
    vertragliche_grenze_prozent: Mapped[Decimal | None] = mapped_column(Numeric(6, 3), nullable=True)
    indexierbare_komponenten: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="ENTWURF")
    freigegeben_am: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    freigegeben_von: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ersetzt_id: Mapped[int | None] = mapped_column(ForeignKey("index_klauseln.id"), nullable=True)


class IndexAnpassungTable(Base):
    __tablename__ = "index_anpassungen"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    index_klausel_id: Mapped[int] = mapped_column(ForeignKey("index_klauseln.id"), index=True)
    index_klausel_version: Mapped[int] = mapped_column(Integer)
    vertrag_id: Mapped[str] = mapped_column(ForeignKey("vertraege.id"), index=True)
    stichtag: Mapped[date] = mapped_column(Date)
    alter_wert: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    neuer_wert: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    veraenderung_prozent: Mapped[Decimal] = mapped_column(Numeric(8, 4))
    erhoehung_cent: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="VORSCHLAG")
    quelle_referenz: Mapped[str | None] = mapped_column(String(256), nullable=True)
    berechnungs_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BKAbrechnungTable(Base):
    __tablename__ = "bk_abrechnungen"
    __table_args__ = (UniqueConstraint("objekt_id", "abrechnungsjahr", name="uq_bk_objekt_jahr"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    objekt_id: Mapped[str] = mapped_column(ForeignKey("objekte.id"), index=True)
    abrechnungsjahr: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="ENTWURF")
    freigegeben_am: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BKPositionTable(Base):
    __tablename__ = "bk_positionen"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bk_abrechnung_id: Mapped[int] = mapped_column(ForeignKey("bk_abrechnungen.id"), index=True)
    bezeichnung: Mapped[str] = mapped_column(String(256))
    betrag_cent: Mapped[int] = mapped_column(Integer)
    art: Mapped[str] = mapped_column(String(32))
    quelle: Mapped[str | None] = mapped_column(String(256), nullable=True)
    profil_referenz: Mapped[str | None] = mapped_column(String(128), nullable=True)


class BKVertragsAnteilTable(Base):
    __tablename__ = "bk_vertrags_anteile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bk_abrechnung_id: Mapped[int] = mapped_column(ForeignKey("bk_abrechnungen.id"), index=True)
    vertrag_id: Mapped[str] = mapped_column(ForeignKey("vertraege.id"), index=True)
    anteil_prozent: Mapped[Decimal] = mapped_column(Numeric(8, 5))
    umlage_cent: Mapped[int] = mapped_column(Integer)
    vorauszahlung_cent: Mapped[int] = mapped_column(Integer)
    ergebnis_cent: Mapped[int] = mapped_column(Integer)
    op_position_id: Mapped[int | None] = mapped_column(ForeignKey("op_positionen.id"), nullable=True)


class MahnPolicyTable(Base):
    __tablename__ = "mahn_policies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version: Mapped[int] = mapped_column(Integer, unique=True)
    stufe1_tage_nach_faelligkeit: Mapped[int] = mapped_column(Integer, default=7)
    stufe2_mindesttage_nach_stufe1_versand: Mapped[int] = mapped_column(Integer, default=14)
    zinsen_prozent: Mapped[Decimal] = mapped_column(Numeric(6, 3), default=Decimal("0"))
    gebuehr_cent: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="ENTWURF")
    freigegeben_am: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MahnFallTable(Base):
    __tablename__ = "mahn_faelle"
    __table_args__ = (UniqueConstraint("outbox_key", name="uq_mahn_outbox_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vertrag_id: Mapped[str] = mapped_column(ForeignKey("vertraege.id"), index=True)
    gesellschaft_id: Mapped[str] = mapped_column(ForeignKey("gesellschaften.id"), index=True)
    # Anker-OP (die Forderung), an die dieser Mahnzyklus gebunden ist. Eine
    # NEUE Forderung (anderer op_position_id) bekommt ihren eigenen
    # Stufe1->Stufe2-Zyklus, unabhängig davon, wie weit ältere Forderungen
    # desselben Vertrags schon gediehen sind.
    forderung_op_position_id: Mapped[int] = mapped_column(ForeignKey("op_positionen.id"), index=True)
    forderungsumfang_hash: Mapped[str] = mapped_column(String(64))
    stufe: Mapped[int] = mapped_column(Integer)
    outbox_key: Mapped[str] = mapped_column(String(256))
    policy_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="GEPLANT")
    betrag_cent: Mapped[int] = mapped_column(Integer)
    bank_stand_datum: Mapped[date] = mapped_column(Date)
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    geplant_am: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    versand_beansprucht_am: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    gesendet_am: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    letzter_versuch_am: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditEventTable(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_typ: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[str] = mapped_column(String(64))
    aktion: Mapped[str] = mapped_column(String(64))
    akteur: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserTable(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(256), unique=True)
    rolle: Mapped[str] = mapped_column(String(32))
    gesellschaft_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)


class JobLockTable(Base):
    """Mutual-exclusion row: a unique (job_name, fachschluessel) prevents a
    second worker (or a restarted first worker) from repeating a job that
    already ran, without requiring a distributed lock service."""

    __tablename__ = "job_locks"
    __table_args__ = (UniqueConstraint("job_name", "fachschluessel", name="uq_job_lock"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_name: Mapped[str] = mapped_column(String(128))
    fachschluessel: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(16), default="LAEUFT")
    ergebnis: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    beendet_am: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
