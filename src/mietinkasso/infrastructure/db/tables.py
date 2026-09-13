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
    # Additiv, nullable (ensure_additive_columns-sicher): explizite
    # Historisierungs-Kette statt einer ID-String-Heuristik. Wird NUR von
    # `indexautomatik/umsetzung_service.py::umsetzen` gesetzt, wenn eine
    # Umsetzung die ALTE Zeile schließt und eine NEUE für dieselbe,
    # ununterbrochen fortbestehende vertragliche Verpflichtung anlegt (append-
    # only Historisierung, siehe dortiger Docstring). Erlaubt
    # `mieweg_vorschau/service.py`, für die "existierte die Komponente zum
    # Bezugszeitpunkt bereits"-Prüfung bis zur URSPRÜNGLICHEN Zeile
    # zurückzuverfolgen, statt eine bloß technisch neu vergebene ID mit einer
    # fachlich neuen/nie zuvor existierten Komponente zu verwechseln (AGENTS.md:
    # keine Namens-/ID-Heuristik - dies ist eine explizite, von unserem
    # eigenen Code gesetzte Fremdschlüsselbeziehung, keine geratene Ableitung
    # aus fachlichen Daten).
    historisiert_von_id: Mapped[str | None] = mapped_column(String(48), nullable=True)


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


class MieWegVorschauTable(Base):
    """Versionierte, unveränderliche MieWeG-2026-Berechnungsvorschau
    (Auftrag 12.09., Paket C) - AUSDRÜCKLICH nur eine Vorschau, nie eine
    Freigabe/Buchung: löst keine Vorschreibung/Mahnung/keinen Versand
    aus und schreibt nichts auf `VertragTable`/`VertragsKomponenteTable`/
    `OPPositionTable` zurück (siehe `mieweg_vorschau/service.py`).

    Bewusst zwei JSON-Textspalten statt eines breiten Spaltensatzes
    (additiv, kompakt): `eingaben_json` hält ALLE erfassten Eingaben
    (Rechtsprofil, VPI-Jahresdurchschnitte samt Quelle/Datum,
    Vertragsspur-Eingaben, referenzierte indexierbare Komponenten,
    Zustellnachweis-Referenz) unveränderlich fest; `ergebnis_json` die
    vollständige, nachvollziehbare Jahresschritt-für-Jahresschritt-
    Herleitung samt offener Nachweise. Die wenigen typisierten Spalten
    sind nur für Sortierung/Anzeige gedacht, nicht als alleinige
    Quelle der Wahrheit."""

    __tablename__ = "mieweg_vorschauen"
    __table_args__ = (UniqueConstraint("vertrag_id", "version", name="uq_mieweg_vorschau_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vertrag_id: Mapped[str] = mapped_column(ForeignKey("vertraege.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    rechtsordnung: Mapped[str] = mapped_column(String(48))
    ist_wohnungsrechner_fall: Mapped[bool] = mapped_column(Boolean)
    ziel_bewertungsjahr: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vollstaendig: Mapped[bool] = mapped_column(Boolean, default=False)
    massgeblicher_hoechstbetrag_cent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fruehester_termin: Mapped[date | None] = mapped_column(Date, nullable=True)
    eingaben_json: Mapped[str] = mapped_column(Text)
    ergebnis_json: Mapped[str] = mapped_column(Text)
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
    # Additiv, nullable: explizites Terminmodell (Codex-Rückprüfung zu
    # 5535ae2 - "anpassungsmonat ist zwingend und damit reine
    # Schwellenklauseln ohne festen Monat sowie maximal-einmal-jährlich
    # ohne fixen Monat nicht darstellbar"). GENAU EINER von drei Modi,
    # `None` bleibt gesperrt (kein erfundener Termin ohne belegtes
    # Modell) - siehe `indexautomatik/service.py::_monatslauf_klausel`
    # für die vollständige Fachlogik je Modus:
    #   "FIXER_MONAT"  - Anpassung nur in `anpassungsmonat` (1-12),
    #                    Mindestabstand zur letzten Anpassung über
    #                    `mindestintervall_monate` (beide Felder Pflicht).
    #   "INTERVALL"    - kein fixer Kalendermonat, nur ein
    #                    Mindestabstand (`mindestintervall_monate`) seit
    #                    Vertragsbeginn/letzter Anpassung - z. B.
    #                    "maximal einmal jährlich" OHNE Monatsbindung.
    #   "BEI_SCHWELLE" - reine Schwellenklausel ohne Kalenderbindung,
    #                    jeden Monat prüfbar; optionaler
    #                    `mindestintervall_monate` verhindert nur ein
    #                    sofortiges erneutes Auslösen im Folgemonat.
    terminmodus: Mapped[str | None] = mapped_column(String(16), nullable=True)
    anpassungsmonat: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mindestintervall_monate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Optional: bekannte vertragliche Rundung des amtlichen Indexwerts auf
    # N Nachkommastellen VOR dem Schwellenvergleich (z. B. "eine
    # Dezimalstelle") - explizit zu erfassen, sonst bleibt der volle,
    # ungerundete amtliche Wert maßgeblich (unverändertes Verhalten).
    # Betrifft NUR den amtlichen Indexwert selbst - eine gesonderte
    # Rundung des daraus berechneten Schwellenkorridors (der
    # Veränderungsprozentzahl) ist ein ANDERES, separat zu erfassendes
    # Feld (`schwellenkorridor_rundung_dezimalstellen`); die eine
    # Rundung ist NICHT automatisch die andere (Codex-Rückprüfung zu
    # 5535ae2: "Indexwertrundung ist nicht automatisch Rundung des
    # Schwellenkorridors").
    indexwert_rundung_dezimalstellen: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Optional: bekannte vertragliche Rundung der SCHWELLENKORRIDOR-
    # GRENZWERTE (Ober-/Untergrenze in INDEXPUNKTEN, `basis_wert*
    # (1±schwelle/100)`) auf N Nachkommastellen VOR dem Vergleich mit dem
    # tatsächlichen amtlichen Indexwert - NICHT eine Rundung der daraus
    # berechneten Veränderungsprozentzahl (Präzisierung Markus: Basis 300,
    # Schwelle 3% strikt -> oberer Grenzwert 309,0; ein amtlicher Wert
    # 309,1 überschreitet ihn direkt, obwohl die gerundete Veränderung
    # selbst fälschlich noch 3,0% ergäbe und NICHT auslösen würde). Der
    # für eine ausgelöste Erhöhung tatsächlich verwendete Betrag bleibt
    # IMMER aus dem vollen, unverkürzten Indexquotienten berechnet - diese
    # Rundung wirkt NUR auf die Trigger-Entscheidung (siehe
    # `IndexService.schwellenkorridor_grenzwerte`/
    # `ueberschreitet_schwelle_grenzwerte`). `None` bedeutet: keine
    # Korridor-Grenzwertrundung, der Schwellenvergleich läuft wie bisher
    # über die (ggf. gedämpfte/vertraglich gedeckelte) Prozentzahl.
    schwellenkorridor_rundung_dezimalstellen: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Optional: zusätzliche vertragliche Wartefrist in KALENDERMONATEN
    # nach dem maßgeblichen Indexereignis - NIE über eine Tage-Umrechnung
    # (2 Monate != 60 Tage), aber TAGGENAU addiert (15.11. + 2 Monate =
    # 15.01., nicht auf den 1. gekürzt - Codex-Rückprüfung zu 5535ae2).
    # `wartefrist_bezug` legt EINDEUTIG fest, ob sich die Frist auf die
    # VPI-Periode selbst ("VPI_PERIODE") oder auf deren belegte amtliche
    # Veröffentlichung ("VEROEFFENTLICHUNG", siehe
    # `VpiMonatswertTable.veroeffentlicht_am` - NICHT `abgerufen_am`,
    # das ist nur der eigene Abrufzeitpunkt) bezieht - beide zusammen
    # oder keines, nie nur eines (sonst gesperrt, kein Rateversuch bei
    # fehlendem Ereignis-/Datumsbeleg).
    wartefrist_monate_nach_indexereignis: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wartefrist_bezug: Mapped[str | None] = mapped_column(String(24), nullable=True)
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
    # Additiv, nullable: der tatsächliche VPI-QUELLMONAT von `neuer_wert`
    # (Codex-Rückprüfung zu c01ceb2: "basis_monat=Anspruchsmonat ist kein
    # VPI-Quellmonat" - `umsetzung_service.py`s Klausel-Basisfortschreibung
    # braucht den echten Bezugsmonat des verwendeten amtlichen Werts, NICHT
    # den Monat, in dem die neue Miete wirksam wird). `None` bei älteren,
    # vor dieser Nachverfolgung erzeugten Zeilen.
    vpi_jahr: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vpi_monat: Mapped[int | None] = mapped_column(Integer, nullable=True)
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


class VpiJahreswertTable(Base):
    """Manuell im Backoffice erfasster, mit einem konkreten
    Publikations-Beleg belegter VPI-JAHRESDURCHSCHNITT-Override
    (Auftrag 13.09., Indexautomatik) - z. B. wenn Statistik Austria
    bereits einen offiziellen Jahresdurchschnitt veröffentlicht hat,
    bevor/ohne dass alle 12 Monatswerte einzeln importiert wurden.
    `VpiRepository.jahresdurchschnitt` bevorzugt diesen Override, sonst
    wird aus `VpiMonatswertTable` abgeleitet (siehe dort). KEIN
    automatischer Statistik-Austria-Abruf (Claude greift nie auf
    externe Server zu) - fehlt ein benötigtes Jahr auf beiden Wegen,
    bricht `mieweg_vorschau/berechnung.py::berechne_gesetzliche_
    hoechstgrenze` an dieser Stelle ab (kein erfundener Wert)."""

    __tablename__ = "vpi_jahreswerte"
    __table_args__ = (UniqueConstraint("reihe", "jahr", name="uq_vpi_jahreswert"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    reihe: Mapped[str] = mapped_column(String(32))
    jahr: Mapped[int] = mapped_column(Integer)
    wert: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    # Unabhängiger Review (fd8c2b2-Folgereview): ein in einer OGD-Datei
    # enthaltener Jahresdurchschnitt (VPIZR-YYYY) ist ERST dann amtlich
    # ENDGUELTIG, wenn dieselbe Publikationsreihe auch den Jänner des
    # FOLGEJahres enthält ("Jahresdurchschnitt endgültig mit
    # Jänner-Publikation im Februar") - vorher bleibt er VORLAEUFIG und
    # darf NICHT als verwendbarer Wert durchgereicht werden, selbst wenn
    # er numerisch bereits vorliegt. Ein manuell erfasster Override
    # (`VpiRepository.jahreswert_erfassen`) ist per Default ENDGUELTIG
    # (Operator bestätigt damit ausdrücklich eine amtliche Publikation).
    finalitaet: Mapped[str] = mapped_column(String(16), default="ENDGUELTIG")
    quelle: Mapped[str] = mapped_column(String(256))
    quelle_datum: Mapped[date] = mapped_column(Date)
    erfasst_von: Mapped[str] = mapped_column(String(128))
    erfasst_am: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VpiMonatswertTable(Base):
    """Amtlicher VPI-Monatswert je (Reihe, Jahr, Monat) - Zielformat für
    einen künftigen Statistik-Austria-OGD-Import (Auftrag 13.09.,
    Betriebspräzisierung). `finalitaet` unterscheidet einen bereits
    ENDGUELTIGen von einem noch VORLAEUFIGen Veröffentlichungsstand;
    `VpiRepository.jahresdurchschnitt` verlangt für einen abgeleiteten
    Jahresdurchschnitt ALLE 12 Monate ENDGUELTIG - ein einzelner
    vorläufiger oder fehlender Monat blockiert den gesamten Jahreswert
    (kein teilweise erfundener Durchschnitt). Das exakte Spaltenlayout
    der realen OGD-CSV-Dateien (data.statistik.gv.at) wurde in dieser
    Sitzung NICHT gegen eine echte Datei verifiziert (kein
    Server-/Internetzugriff) - siehe `indexautomatik/vpi_import.py` und
    OFFENE_PUNKTE.md."""

    __tablename__ = "vpi_monatswerte"
    __table_args__ = (UniqueConstraint("reihe", "jahr", "monat", name="uq_vpi_monatswert"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    reihe: Mapped[str] = mapped_column(String(32))
    jahr: Mapped[int] = mapped_column(Integer)
    monat: Mapped[int] = mapped_column(Integer)
    wert: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    finalitaet: Mapped[str] = mapped_column(String(16))
    quelle_datei: Mapped[str] = mapped_column(String(256))
    quelle_zeile: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Betriebspräzisierung 13.09.: "Kalenderdatum allein garantiert
    # keine tatsächliche Veröffentlichung" - `quelle_hash` (SHA256 des
    # importierten Datei-INHALTS) belegt, WELCHE konkrete Dateiversion
    # importiert wurde; `abgerufen_am` ist ein vom Aufrufer explizit
    # anzugebender Zeitpunkt (WANN die Datei tatsächlich von Statistik
    # Austria abgerufen wurde) - wird NIE aus der Systemuhr zum
    # Importzeitpunkt geraten, weil eine Datei auch lange nach dem
    # eigentlichen Abruf importiert werden kann.
    quelle_hash: Mapped[str] = mapped_column(String(64))
    abgerufen_am: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Additiv, nullable: das amtliche VERÖFFENTLICHUNGSDATUM dieses
    # Monatswerts (Codex-Rückprüfung zu 5535ae2: "VPI.abgerufen_am ist
    # NUR Abruf, niemals amtlicher Veröffentlichungstag!"). `abgerufen_am`
    # bleibt WANN WIR importiert haben - `veroeffentlicht_am` ist WANN
    # Statistik Austria den Wert tatsächlich publiziert hat, mit eigener
    # belegter Quelle (`veroeffentlichung_quelle`, z. B. URL/Referenz der
    # Pressemitteilung). Eine vertragliche Wartefrist mit
    # `wartefrist_bezug="VEROEFFENTLICHUNG"` darf NUR dieses Feld
    # verwenden, nie `abgerufen_am` - fehlt es, bleibt die Wartefrist
    # gesperrt statt geraten (siehe `indexautomatik/service.py::
    # _monatslauf_klausel`).
    veroeffentlicht_am: Mapped[date | None] = mapped_column(Date, nullable=True)
    veroeffentlichung_quelle: Mapped[str | None] = mapped_column(String(256), nullable=True)
    importiert_von: Mapped[str] = mapped_column(String(128))
    importiert_am: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RechtsprofilTable(Base):
    """Versioniertes, vom Eigentümer einmalig freigegebenes
    Rechtsprofil je Vertrag (Auftrag 13.09., Indexautomatik) - liefert
    der monatlichen Indexautomatik alle Eingaben für
    `mieweg_vorschau_service.vorschau_erstellen`, OHNE dass jeden Monat
    erneut manuell bestätigt werden muss. `quelle_hash` bindet die
    Freigabe an den Stand von Vertrag/referenzierten Komponenten zum
    Freigabezeitpunkt; ändert sich dieser Stand, gilt die Freigabe als
    invalidiert (siehe `indexautomatik/rechtsprofil_service.py::ist_noch_gueltig`
    - "Änderungen an Quelle/Basis/Vertrag/Profil entwerten alte
    Freigabe"). `ist_hauptmiete=False`/`None` blockiert die Automatik
    bewusst (keine Rechtsannahme zur Untermiete-Anwendbarkeit)."""

    __tablename__ = "rechtsprofile"
    __table_args__ = (UniqueConstraint("vertrag_id", "version", name="uq_rechtsprofil_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vertrag_id: Mapped[str] = mapped_column(ForeignKey("vertraege.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    rechtsordnung: Mapped[str] = mapped_column(String(48))
    ist_wohnungsnutzung: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    mrg_zinsbeschraenkung: Mapped[bool] = mapped_column(Boolean, default=False)
    # Codex-Rückprüfung (499c36f): `mrg_zinsbeschraenkung`/`foerderbindung`
    # sind PRODUKTIV BEREITS BESTEHENDE NOT-NULL-Spalten - die additive
    # Migration (`infrastructure/db/migrations.py::
    # ensure_additive_columns`) ändert NIE Constraints bestehender
    # Spalten, ein reines `nullable=True` hier würde deshalb nur auf
    # einer frischen Test-/CI-DB funktionieren, nie auf der echten
    # Produktions-DB. Der dreiwertige Zustand "unbekannt" wird deshalb
    # NICHT über `None` auf der bestehenden Spalte abgebildet, sondern
    # über dieses separate, additive, unverändert NOT-NULL/Default-False
    # Flag: `False` = noch nicht geprüft ("unbekannt", sperrt die
    # Freigabe - siehe `rechtsprofil.py::
    # _validiere_vollstaendigkeit_fuer_freigabe`), `True` = eine geprüfte
    # Angabe liegt vor (der Wert von `mrg_zinsbeschraenkung` selbst kann
    # dann `True` ODER `False` sein - beides ist eine geklärte Aussage).
    # Ein `ENTWURF` darf mit `False` (ungeklärt) bleiben; eine Freigabe
    # verlangt `True`. Kein Automatik-Freigabe-Bypass durch den Default.
    mrg_zinsbeschraenkung_geprueft: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    ist_altvertrag: Mapped[bool] = mapped_column(Boolean, default=False)
    # DREIWERTIG, kein Boolean-Ersatz für "ungeklärt": `None` = noch
    # nicht geprüft (sperrt die Automatik, keine Rechtsannahme);
    # `False` = GEPRÜFTE, bestätigte Untermiete - MieWeG erfasst
    # Wohnungsuntermiete ausdrücklich mit, wird deshalb NICHT gesperrt
    # und läuft über denselben Wohnungsrechner-Pfad wie Hauptmiete
    # (Modellreview 13.09.: "ist_hauptmiete is not True sperrt pauschal
    # UNTERMIETEN... None=ungeklärt von False=geprüfte Untermiete
    # unterscheiden"); `True` = geprüfte Hauptmiete.
    ist_hauptmiete: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    foerderbindung: Mapped[bool] = mapped_column(Boolean, default=False)
    # Siehe `mrg_zinsbeschraenkung_geprueft` oben - identisches additiv-
    # sicheres Tri-State-Muster für Förderbindung.
    foerderbindung_geprueft: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    # BRUTTO (verbindliche Konvention dieses Repositories, siehe
    # domain/money.py::zerlege_brutto_cent) - wirkt als zusätzliche
    # harte Kappung von `massgeblicher_hoechstbetrag_cent` im
    # MieWeG-Wohnungsrechner-Pfad (indexautomatik/service.py).
    mietzinsobergrenze_cent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mietzinsobergrenze_quellenbeleg: Mapped[str | None] = mapped_column(String(256), nullable=True)
    mietzinsobergrenze_gueltig_bis: Mapped[date | None] = mapped_column(Date, nullable=True)
    bezugsjahr: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bezugsmonat: Mapped[int | None] = mapped_column(Integer, nullable=True)
    letzte_basis_war_jahresdurchschnitt: Mapped[bool] = mapped_column(Boolean, default=False)
    basis_komponenten_ids: Mapped[list] = mapped_column(JSON, default=list)
    # Additiv (Codex-Rückprüfung: "Historisierungskette löst noch nicht
    # initial importierte Bestandskomponenten" - ein Mietvertrag/eine
    # belegte Indexbasis kann zeitlich VOR der erst später importierten
    # Komponentenzeile liegen, z. B. Vertragsbeginn 1.4., belegte
    # Indexbasis Februar, eine unveränderte Pauschale aber erst ab
    # August als `VertragsKomponenteTable`-Zeile importiert).
    # `StammdatenRepository.ursprungs_gueltig_von` kann eine SPÄTER
    # importierte Zeile nicht von einer tatsächlich fehlenden Historie
    # unterscheiden - eine reine Zeitprüfung würde deshalb einen
    # tatsächlich bestehenden, nur spät importierten Altbestand
    # fälschlich blockieren. Dieses Feld erlaubt einen EXPLIZITEN,
    # geprüften Ausnahmenachweis je Komponenten-ID: `{komponente_id:
    # {"betrag_cent": int, "datum": "YYYY-MM-DD", "quellenbeleg": str}}`.
    # Nur ein VOLLSTÄNDIGER Eintrag (alle drei Felder gesetzt, `datum`
    # <= Bezugszeitpunkt) darf die Existenzprüfung für GENAU diese
    # Komponente ersetzen - siehe `mieweg_vorschau/service.py::
    # belegte_historische_basis_gueltig`. Ändert NIEMALS die technische
    # Komponentenzeile selbst (kein Zurückdatieren von `gueltig_von`) -
    # rein dokumentarischer Ausnahmenachweis, fließt in KEINE Berechnung
    # ein (der tatsächlich verrechnete Betrag bleibt immer
    # `komponente.betrag_cent` der aktuellen Zeile).
    historische_basis_belege: Mapped[dict] = mapped_column(JSON, default=dict, server_default=text("'{}'"))
    # VPI-Reihe für die GESETZLICHE (MieWeG-)Spur dieses Vertrags (z. B.
    # "VPI20C18") - siehe `indexautomatik/vpi_import.py`/`VpiMonatswertTable`.
    vpi_reihe: Mapped[str] = mapped_column(String(32), default="VPI20C18")
    # Vertragliche Spur: ENTWEDER ein manuell erfasster, statischer
    # Zielbetrag (für Verträge mit einer tatsächlich fixen Obergrenze)
    # ODER eine Referenz auf eine versionierte, bereits freigegebene
    # `IndexKlauselTable`-Klausel, aus der die vertragliche Spur JEDEN
    # Zyklus NEU aus amtlichen Werten berechnet wird (Fachlicher
    # Abnahmepunkt Codex: "ein fixer manuell eingetragener
    # vertraglich_zulaessiger_betrag_cent ist keine dauerhafte
    # Vertragsformel"). `RechtsprofilService.freigeben` verlangt GENAU
    # eine der beiden Varianten. `vertragsklausel_id` ist AUCH der
    # alleinige Antrieb für einen Geschäftsraum-/Nicht-Wohnungsrechner-
    # Fall (kein MieWeG, siehe `indexautomatik/service.py`).
    vertraglich_zulaessiger_betrag_cent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vertraglicher_quellenbeleg: Mapped[str | None] = mapped_column(String(256), nullable=True)
    vertraglicher_fruehestmoeglicher_termin: Mapped[date | None] = mapped_column(Date, nullable=True)
    vertragsklausel_id: Mapped[int | None] = mapped_column(ForeignKey("index_klauseln.id"), nullable=True)
    vertrag_beleg_referenz: Mapped[str] = mapped_column(String(256))
    klausel_referenz: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # Auftrag HV-20260913-VERSAND-SOLL, Punkt 2: EXPLIZIT konfigurierbares
    # Fristenprofil zwischen bestätigtem Zugang und Wirksamkeit -
    # ausschließlich additive Infrastruktur, KEINE Fachentscheidung durch
    # Claude. Ist `frist_tage_zugang_bis_wirksamkeit` gesetzt, verwendet
    # `outbox_service.zugang_bestaetigen` DIESE Frist (mit Pflicht-
    # `frist_quellenbeleg`) statt der bisherigen pauschalen 14-Tage-
    # Annahme (§ 16 Abs 9 MRG) - z. B. für eine belegte Gewerbe-/
    # Jännerklausel mit abweichender vertraglicher Frist. Ist NICHTS
    # gesetzt, bleibt das bisherige Verhalten (nur MRG_VOLL, 14 Tage)
    # unverändert bestehen - keine Verhaltensänderung für bestehende
    # Fälle. Die tatsächlichen Werte je Vertragstyp liefert Codex anhand
    # geprüfter Vertragsprofile, nicht diese Codebasis.
    frist_tage_zugang_bis_wirksamkeit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    frist_quellenbeleg: Mapped[str | None] = mapped_column(String(256), nullable=True)
    quelle_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="ENTWURF")
    freigegeben_von: Mapped[str | None] = mapped_column(String(128), nullable=True)
    freigegeben_am: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    erstellt_von: Mapped[str] = mapped_column(String(128))
    erstellt_am: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IndexautomatikLaufTable(Base):
    """Ein Eintrag je (Vertrag, Kalendermonat "YYYY-MM") - der
    Unique-Constraint ist die Idempotenzgrenze gegen einen doppelten
    Monatslauf (Wiederholung/Absturz/Parallelstart), analog zu
    `MahnFallTable.outbox_key`/`JobLockTable`."""

    __tablename__ = "indexautomatik_laeufe"
    __table_args__ = (UniqueConstraint("vertrag_id", "periode", name="uq_indexautomatik_lauf_periode"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vertrag_id: Mapped[str] = mapped_column(ForeignKey("vertraege.id"), index=True)
    periode: Mapped[str] = mapped_column(String(7))
    rechtsprofil_id: Mapped[int | None] = mapped_column(ForeignKey("rechtsprofile.id"), nullable=True)
    rechtsprofil_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mieweg_vorschau_id: Mapped[int | None] = mapped_column(ForeignKey("mieweg_vorschauen.id"), nullable=True)
    erhoehungsschreiben_id: Mapped[int | None] = mapped_column(ForeignKey("erhoehungsschreiben.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(32))
    blockiert_gruende: Mapped[list] = mapped_column(JSON, default=list)
    erstellt_am: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ErhoehungsschreibenTable(Base):
    """Persistente Outbox für Erhöhungsschreiben (Auftrag 13.09.) - trägt
    ZWEI unabhängige Berechnungspfade:

    - MieWeG-Wohnungsrechner-Pfad (`mieweg_vorschau_id` gesetzt,
      `ziel_bewertungsjahr` gesetzt): pro Vertrag und gesetzlichem
      April-Zyklus höchstens EIN Fall - das ist für DIESEN Pfad
      fachlich korrekt, weil § 1 Abs 4 MieWeG selbst nur einen
      jährlichen 1.-April-Termin kennt. Der partielle Unique-Index
      unten gilt daher NUR, wenn `ziel_bewertungsjahr` gesetzt ist.
    - Geschäftsraum-/generischer-Klausel-Pfad (`index_anpassung_id`
      gesetzt, `ziel_bewertungsjahr` NULL): reine Vertragsklausel über
      `index/service.py`/`IndexKlauselTable`, OHNE April-Bindung und
      OHNE künstliche Einmal-pro-Jahr-Grenze (Fachlicher Abnahmepunkt
      Codex: "Geschäftsraum-Indexierungen dürfen ... nicht durch
      Unique(Vertrag,Bewertungsjahr) dauerhaft auf einmal pro Jahr
      begrenzt werden, falls ihre geprüfte Klausel mehr zulässt") - die
      Idempotenz für DIESEN Pfad kommt stattdessen aus dem
      (vertrag_id, periode)-Unique-Constraint auf
      `IndexautomatikLaufTable` (höchstens ein Berechnungsversuch pro
      Kalendermonat) plus der natürlichen 1:1-Bindung an eine konkrete
      `IndexAnpassungTable`-Zeile.

    `versendet_am` (Transport hat angenommen) und `zugang_bestaetigt_am`
    (fachlich bestätigter Empfang) sind bewusst getrennte Felder - ein
    technisch angenommener Versand ist NIE automatisch ein bestätigter
    Zugang (siehe `indexautomatik/outbox_service.py`)."""

    __tablename__ = "erhoehungsschreiben"
    __table_args__ = (
        Index(
            "uq_erhoehungsschreiben_ziel_mieweg",
            "vertrag_id",
            "ziel_bewertungsjahr",
            unique=True,
            sqlite_where=text("ziel_bewertungsjahr IS NOT NULL"),
            postgresql_where=text("ziel_bewertungsjahr IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vertrag_id: Mapped[str] = mapped_column(ForeignKey("vertraege.id"), index=True)
    ziel_bewertungsjahr: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rechtsprofil_id: Mapped[int] = mapped_column(ForeignKey("rechtsprofile.id"))
    rechtsprofil_version: Mapped[int] = mapped_column(Integer)
    mieweg_vorschau_id: Mapped[int | None] = mapped_column(ForeignKey("mieweg_vorschauen.id"), nullable=True)
    mieweg_vorschau_final_id: Mapped[int | None] = mapped_column(ForeignKey("mieweg_vorschauen.id"), nullable=True)
    index_anpassung_id: Mapped[int | None] = mapped_column(ForeignKey("index_anpassungen.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="ENTWURF")
    massgeblicher_termin: Mapped[date] = mapped_column(Date)
    erhoehung_cent: Mapped[int] = mapped_column(Integer)
    schreiben_text: Mapped[str] = mapped_column(Text)
    schreiben_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    idempotenzschluessel: Mapped[str] = mapped_column(String(128))
    versandkanal: Mapped[str | None] = mapped_column(String(64), nullable=True)
    versendet_am: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    versand_beansprucht_am: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    externe_versandreferenz: Mapped[str | None] = mapped_column(String(128), nullable=True)
    zugangsform: Mapped[str | None] = mapped_column(String(48), nullable=True)
    zugang_bestaetigt_am: Mapped[date | None] = mapped_column(Date, nullable=True)
    zugang_beleg: Mapped[str | None] = mapped_column(String(256), nullable=True)
    zahlungspflicht_ab: Mapped[date | None] = mapped_column(Date, nullable=True)
    fehlergrund: Mapped[str | None] = mapped_column(Text, nullable=True)
    blockiert_gruende: Mapped[list] = mapped_column(JSON, default=list)
    empfaenger_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    # `server_default` (statt nur `default=dict`) ist Pflicht, damit die
    # additive Spaltenmigration (`infrastructure/db/migrations.py::
    # ensure_additive_columns`) diese NOT-NULL-Spalte per
    # `ALTER TABLE ... ADD COLUMN` auf einer bereits befüllten
    # `erhoehungsschreiben`-Tabelle nachziehen kann, ohne bestehende
    # Zeilen zu verletzen - ein reiner Python-seitiger `default` gilt
    # nur für neue, über den ORM eingefügte Zeilen.
    komponenten_verteilung: Mapped[dict] = mapped_column(JSON, default=dict, server_default=text("'{}'"))
    """Centgenaue Zuordnung der Erhöhung auf GENAU EINE Vertragskomponente
    (`mehrkomponenten_blockiert` in outbox_service.py garantiert das für
    jedes nicht blockierte Schreiben) - Form
    {"komponente_id": str, "alter_betrag_cent": int, "neuer_betrag_cent": int}.
    Leer ({}) bei blockierten/älteren Schreiben ohne eindeutige Komponente;
    `umsetzung_service.py` verweigert die Soll-Umsetzung dann statt zu
    schätzen."""
    erstellt_am: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    aktualisiert_am: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class IndexSollUmsetzungTable(Base):
    """Ausführungsnachweis für die atomare Übernahme eines zugegangenen,
    wirksamen Erhöhungsschreibens in Vertragskomponenten und
    Rechtsprofil-Basis (Auftrag HV-20260913-VERSAND-SOLL). Genau eine Zeile
    je `ErhoehungsschreibenTable` (UNIQUE) - das ist der Beleg, dass
    `umsetzung_service.umsetzen()` für dieses Schreiben bereits (genau)
    einmal gelaufen ist, unabhängig vom aktuellen Status der referenzierten
    Zeilen. `blockiert_gruende` bleibt auch bei Status "UMGESETZT" leer;
    bei "BLOCKIERT" trägt die Zeile den Blockiergrund, damit ein erneuter
    Anlauf (nach Behebung der Ursache) nachvollziehbar ist, ohne den
    vorherigen Blockierversuch zu überschreiben - jeder Anlauf erzeugt
    daher KEINE neue Zeile, sondern aktualisiert diese eine Zeile je
    Erhöhungsschreiben (kein Unique-Konflikt bei Wiederholung)."""

    __tablename__ = "index_soll_umsetzungen"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    erhoehungsschreiben_id: Mapped[int] = mapped_column(
        ForeignKey("erhoehungsschreiben.id"), unique=True, index=True
    )
    vertrag_id: Mapped[str] = mapped_column(ForeignKey("vertraege.id"), index=True)
    status: Mapped[str] = mapped_column(String(24), default="OFFEN")
    blockiert_gruende: Mapped[list] = mapped_column(JSON, default=list)
    quelle_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    neue_komponente_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    beendete_komponente_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Additiv (ensure_additive_columns-sicher, keine FK): Mehrkomponenten-
    # Umsetzung (HV-20260913-VERSAND-SOLL, Mehrkomponentenverteilung) kann
    # MEHR als eine Komponente historisieren - `neue_komponente_id`/
    # `beendete_komponente_id` bleiben für den (weiterhin häufigsten)
    # Ein-Komponenten-Fall unverändert bequem gefüllt, tragen bei mehreren
    # betroffenen Komponenten aber `None`. Diese beiden Listenfelder sind
    # IMMER vollständig (auch im Ein-Komponenten-Fall) und damit die
    # verbindliche, generische Quelle für den Ausführungsnachweis.
    neue_komponenten_ids: Mapped[list] = mapped_column(JSON, default=list, server_default=text("'[]'"))
    beendete_komponenten_ids: Mapped[list] = mapped_column(JSON, default=list, server_default=text("'[]'"))
    neues_rechtsprofil_id: Mapped[int | None] = mapped_column(ForeignKey("rechtsprofile.id"), nullable=True)
    wirksam_ab: Mapped[date | None] = mapped_column(Date, nullable=True)
    akteur: Mapped[str | None] = mapped_column(String(128), nullable=True)
    erstellt_am: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    aktualisiert_am: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class VertragsendeErinnerungTable(Base):
    """Eine stabile Aufgabe je (Vertrag, tatsächlichem Enddatum) -
    ändert sich `VertragTable.gueltig_bis` (Verlängerung/Verkürzung),
    bleibt die alte Zeile stehen (Audit), wird aber von
    `indexautomatik/vertragsende_service.py` als `UNGUELTIG` markiert
    und eine neue Zeile für das neue Enddatum geplant. Empfänger ist
    IMMER `Settings.owner_email` - nie `DebitorTable.email` (kein
    Mieter-Fallback/CC bei dieser internen Erinnerung)."""

    __tablename__ = "vertragsende_erinnerungen"
    __table_args__ = (UniqueConstraint("vertrag_id", "end_datum", name="uq_vertragsende_erinnerung"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vertrag_id: Mapped[str] = mapped_column(ForeignKey("vertraege.id"), index=True)
    end_datum: Mapped[date] = mapped_column(Date)
    faellig_am: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(24), default="OFFEN")
    versand_beansprucht_am: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fehlergrund: Mapped[str | None] = mapped_column(Text, nullable=True)
    benachrichtigt_am: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    entscheidung: Mapped[str | None] = mapped_column(String(32), nullable=True)
    entschieden_von: Mapped[str | None] = mapped_column(String(128), nullable=True)
    entschieden_am: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mieterentwurf_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    mieterentwurf_erstellt_am: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    erstellt_am: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VariableAbrechnungTable(Base):
    """Monatliche variable Nutzungsentgelt-Meldung für KURZZEITVERMIETUNG/
    SELFSTORAGE-Einheiten (Auftrag 13.09., HV-20260913-DASHBOARD) - diese
    Einheiten haben KEINE feste Dauervermietungs-Vorschreibung, sondern
    einen berichtsbasierten, monatlich schwankenden Anteil.

    Versioniert und UNVERÄNDERLICH wie `MieWegVorschauTable`/
    `RechtsprofilTable`: eine Korrektur legt IMMER eine neue Zeile mit
    `version + 1` an, nie ein In-Place-Update. Ein partieller Unique-
    Index erzwingt genau eine `ist_aktuell=True`-Zeile je
    (einheit_id, art, leistungsmonat) - "keine Summierung desselben
    Reports aus Import und manueller Erfassung".

    Quellenbedingte Präzisierung (13.09.): ein Monatsbericht liegt oft
    zuerst nur als roher Buchungsumsatz vor, ohne bestätigten
    Eigentümer-Nettoanteil. `berichteter_betrag_cent`/
    `berichteter_betragsart` (BRUTTO/NETTO/UNGEKLAERT) halten GENAU das
    fest, WIE es gemeldet wurde - es gibt HIER keine automatische
    Netto-Umrechnung über einen angenommenen USt-Satz. Erst wenn
    `unser_netto_anteil_cent` tatsächlich geprüft/bestätigt ist
    (`status=BESTAETIGT`), fließt der Fall in eine Erlössumme ein; eine
    `status=ENTWURF`-Zeile bleibt sichtbar, aber unbestätigt.

    `betriebskosten_hinweis_cent`/`reinigungskosten_hinweis_cent`/
    `verwaltungskosten_hinweis_cent` sind REIN ERLÄUTERND - diese Kosten
    können bereits im ausgewiesenen `unser_netto_anteil_cent` enthalten
    sein und werden von keinem Code-Pfad nochmals abgezogen.
    `tatsaechlicher_zahlungseingang_cent` (die tatsächliche Überweisung
    an den Eigentümer) kann Kostenersatz enthalten und ist NICHT
    gleichzusetzen mit dem Nettomieterlös - beide Felder bleiben
    strikt getrennt und werden nie miteinander verrechnet. Weder dieses
    Feld noch `unser_netto_anteil_cent` ersetzt eine OP-/Bankbuchung;
    `OPPositionTable` bleibt von diesem Modul vollständig unberührt."""

    __tablename__ = "variable_abrechnungen"
    __table_args__ = (
        UniqueConstraint("import_id", name="uq_variable_abrechnung_import_id"),
        # Genau eine AKTUELLE Version je (Einheit, Art, Leistungsmonat) -
        # analog zu `uq_op_eroeffnung_pro_konto` oben.
        Index(
            "uq_variable_abrechnung_aktuell",
            "einheit_id", "art", "leistungsmonat",
            unique=True,
            sqlite_where=text("ist_aktuell = 1"),
            postgresql_where=text("ist_aktuell IS TRUE"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    einheit_id: Mapped[str] = mapped_column(ForeignKey("einheiten.id"), index=True)
    gesellschaft_id: Mapped[str] = mapped_column(ForeignKey("gesellschaften.id"), index=True)
    art: Mapped[str] = mapped_column(String(32))
    leistungsmonat: Mapped[str] = mapped_column(String(7))
    version: Mapped[int] = mapped_column(Integer)
    ist_aktuell: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(16), default="ENTWURF")
    belegdatum: Mapped[date] = mapped_column(Date)
    quelle_referenz: Mapped[str] = mapped_column(String(256))
    quelle_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    berichteter_betrag_cent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    berichteter_betragsart: Mapped[str | None] = mapped_column(String(16), nullable=True)
    unser_netto_anteil_cent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    betriebskosten_hinweis_cent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reinigungskosten_hinweis_cent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    verwaltungskosten_hinweis_cent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tatsaechlicher_zahlungseingang_cent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vermietete_einheiten: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vermietete_flaeche_qm: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    aenderungsgrund: Mapped[str | None] = mapped_column(Text, nullable=True)
    quelle_system: Mapped[str] = mapped_column(String(32), default="MANUELL")
    import_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    erstellt_von: Mapped[str] = mapped_column(String(128))
    erstellt_am: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KomponentenNettoMietFreigabeTable(Base):
    """Separate, additive Bestätigung eines geprüften NETTO-Mietanteils
    für GENAU EINE `VertragsKomponenteTable`-Zeile, ausschließlich für
    die Nettomieterlös-Monatsübersicht (`variableabrechnung/
    dashboard.py`) - Auftrag 13.09., unabhängiger Review: "Bestehende
    Vertragskomponenten sind historisch teilweise BRUTTO gespeichert,
    auch bei art=HMZ/KUECHE/PARKPLATZ; ust_satz_promille ist vorhanden,
    beweist allein aber keine Betragsbasis."

    Ändert NIE `VertragsKomponenteTable.betrag_cent` oder
    `OPPositionTable` - Altbeträge/OP bleiben unverändert.
    `bestaetigter_netto_betrag_cent` kann von `betrag_cent` ABWEICHEN
    (z. B. wenn `betrag_cent` tatsächlich brutto ist). `quelle_hash`
    bindet die Freigabe an den Stand der referenzierten Komponente zum
    Freigabezeitpunkt (Betrag/Art/Gültigkeit/USt-Satz) - jede spätere
    Änderung entwertet die Freigabe automatisch (siehe
    `komponenten_freigabe.py::ist_noch_gueltig`, analog
    `RechtsprofilTable`). `gueltig_von`/`gueltig_bis` grenzen
    zusätzlich ein, für welchen Zeitraum der bestätigte Nettoanteil
    TATSÄCHLICH gilt - eine Freigabe ist keine dauerhafte
    Blankettermächtigung. Eine ungeprüfte Bestandskomponente OHNE
    aktive Freigabe bleibt in der Monatsübersicht eine Datenlücke,
    NIEMALS ein pauschal angenommener Nettobetrag."""

    __tablename__ = "komponenten_netto_miet_freigaben"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    komponente_id: Mapped[str] = mapped_column(ForeignKey("vertrags_komponenten.id"), index=True)
    bestaetigter_netto_betrag_cent: Mapped[int] = mapped_column(Integer)
    quellenbeleg_referenz: Mapped[str] = mapped_column(String(256))
    gueltig_von: Mapped[date] = mapped_column(Date)
    gueltig_bis: Mapped[date | None] = mapped_column(Date, nullable=True)
    quelle_hash: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="FREIGEGEBEN")
    # Pflicht, sobald diese Freigabe eine ZEITLICH ÜBERLAPPENDE frühere
    # Freigabe derselben Komponente entwertet (siehe
    # `komponenten_freigabe.py::freigeben`) - eine neue, nicht
    # überlappende Freigabe (z. B. ein anderer Monat) braucht keinen.
    aenderungsgrund: Mapped[str | None] = mapped_column(String(500), nullable=True)
    freigegeben_von: Mapped[str] = mapped_column(String(128))
    freigegeben_am: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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
