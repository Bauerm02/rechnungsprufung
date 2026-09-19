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
    # Auftrag HV-20260914-MAHNUNG-BRIEF: eine vorhandene `adresse` allein
    # ist KEINE "geprüfte Postadresse" (dieselbe Nie-Ableiten-Regel wie
    # überall - ein bloß erfasster String könnte unvollständig/veraltet
    # sein). Stufe 2 (Kanal BRIEF laut Kanalregel) verlangt explizit
    # DIESES Flag, sonst bleibt der Briefkanal blockiert, OHNE einen
    # E-Mail-Ersatz zu versenden (siehe `mahnwesen/service.py::
    # _pruefe_frisch_versandbereit`). Wird über `stammdaten/repository.py::
    # StammdatenRepository.upsert_debitor(postadresse_geprueft=...)`
    # bewusst NUR bei explizitem Setzen verändert - ein routinemäßiges
    # Update von Name/E-Mail darf eine einmal erteilte Prüfung nicht
    # stillschweigend zurücksetzen.
    postadresse_geprueft: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))


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


class MietvertragsprofilTable(Base):
    """Zusätzliche, VERSIONIERTE Verwaltungs-/Anzeigefelder je Vertrag
    (Auftrag HV-20260913-VERTRAGSANLAGE) - bewusst GETRENNT von
    `RechtsprofilTable` (das ist die Freigabe-pflichtige Rechtsgrundlage
    der Indexautomatik) und von `VertragTable.gueltig_von` (das bleibt
    unverändert die für Sollstellung/OP maßgebliche technische
    Vertragslaufzeit).

    `urspruenglicher_mietbeginn`/`verwaltungsuebernahme_am` sind ein
    Auftrag Markus (13.09., Rückprüfung): bei Altobjekten ist
    `VertragTable.gueltig_von` häufig das Datum der VERWALTUNGS-
    ÜBERNAHME, nicht der tatsächliche Mietbeginn - das darf niemals als
    Indexstart/Rechenstart verwendet oder damit verwechselt werden,
    deshalb ein separates, rein dokumentarisches Feldpaar hier.

    Append-only wie überall in diesem Repository: eine inhaltliche
    Änderung legt eine NEUE Version an (siehe `intake/planner.py`),
    NIE ein In-Place-Update - abweichend von der sonst strikten
    Stammdaten-Konfliktregel ist das hier ABSICHTLICH KEIN Konflikt,
    weil laufende Datenpflege/Korrektur dieser rein beschreibenden
    Felder normal ist (siehe `docs/hausverwaltung/IMPORT_VERTRAG.md`,
    Abschnitt `mietvertragsprofile[]`)."""

    __tablename__ = "mietvertragsprofile"
    __table_args__ = (UniqueConstraint("vertrag_id", "version", name="uq_mietvertragsprofil_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vertrag_id: Mapped[str] = mapped_column(ForeignKey("vertraege.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    # WOHNUNG|BUERO|GESCHAEFTSLOKAL|SONSTIGE|UNGEKLAERT - NIEMALS aus
    # `rechtsordnung`/`ist_wohnungsnutzung` abgeleitet (kein "Büro =
    # MRG-frei"-Rateversuch, siehe mieweg_vorschau/service.py).
    nutzungsart: Mapped[str] = mapped_column(String(24), default="UNGEKLAERT", server_default=text("'UNGEKLAERT'"))
    urspruenglicher_mietbeginn: Mapped[date | None] = mapped_column(Date, nullable=True)
    verwaltungsuebernahme_am: Mapped[date | None] = mapped_column(Date, nullable=True)
    verwaltung_bezeichnung: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Der VEREINBARTE Betrag laut Vertrag - strukturell GETRENNT von
    # `KautionTable.betrag_cent` (der TATSÄCHLICH eingegangene, bestätigte
    # Betrag). Ein vereinbarter Betrag ist NIEMALS ein Zahlungsbeleg
    # (Auftrag Markus: "Kaution-Soll aus Vertrag ist kein Zahlungsbeleg!").
    vertragliche_kaution_cent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vertragliche_kaution_quellenbeleg: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # Nur mit Klausel/Quelle erfassbar, NIE automatisch berechnet oder
    # verzinst. NULLABLE, KEIN Default 0 (Auftrag Markus, Präzisierung:
    # "mahngebuehr_cent möglichst null für unbekannt, 0 bedeutet explizit
    # keine Gebühr; keine erfundene Null durch fehlenden Fund") - `None`
    # = nicht erfasst/unbekannt, `0` = eine ausdrücklich belegte
    # vertragliche Aussage "keine Mahngebühr". Diese Unterscheidung geht
    # verloren, sobald ein fehlender PDF-Fund stillschweigend als 0
    # eingetragen würde - deshalb kein Default hier.
    mahngebuehr_cent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mahngebuehr_quellenbeleg: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # Strukturierte, AUSDRÜCKLICH UNVERBINDLICHE Index-QUELLFELDER (Auftrag
    # Markus, Präzisierung 13.09.: "im Zusatzprofil strukturierte,
    # ausdrücklich unverbindliche Index-Quellfelder speichern ... Die
    # bestätigte Übernahme darf daraus KEINE aktive Indexklausel/Freigabe
    # oder Solländerung erzeugen"). Reine STAGING-Ablage dessen, was im
    # Vertragstext zur Wertsicherung gefunden wurde - erzeugt NIEMALS
    # automatisch eine `IndexKlauselTable`-Zeile, keine Freigabe, keine
    # Sollstellung. Die tatsächlich WIRKSAME Klausel bleibt ausschließlich
    # über den bestehenden, eigenen Weg (`index/service.py::
    # klausel_anlegen` + `klausel_freigeben`, Backoffice-Formular
    # `/vertrag/{id}/indexklauseln`) erzeugt - diese Felder hier sind ein
    # Gedächtnisstütze/Vorbefüll-Vorschlag für GENAU dieses bestehende
    # Formular, keine eigene Berechnungsgrundlage. Immer zusammen mit dem
    # tatsächlich freigegebenen Regelprofil (falls vorhanden) UND einem
    # eventuellen Rekonstruktionsmodell getrennt lesbar anzuzeigen, nie
    # vermischt (Auftrag Markus: "Vorhandene wirksame Klausel und
    # Rekonstruktionsmodell separat lesbar").
    index_reihe: Mapped[str | None] = mapped_column(String(64), nullable=True)
    index_urspruenglicher_basismonat: Mapped[str | None] = mapped_column(String(7), nullable=True)
    index_urspruenglicher_basiswert: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    index_schwelle_prozent: Mapped[Decimal | None] = mapped_column(Numeric(6, 3), nullable=True)
    # Tri-State: `None` = im Vertragstext nicht eindeutig festgestellt
    # (weder "ab" noch "über" belegt) - NIE geraten.
    index_schwelle_inklusive: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    index_anpassungsmonat: Mapped[int | None] = mapped_column(Integer, nullable=True)
    index_mindestintervall_monate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    index_klauseltext_auszug: Mapped[str | None] = mapped_column(Text, nullable=True)
    index_klauseltext_seite: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # PDF_EXTRAKTION|MANUELL|IMPORT_SCHEMA - wie diese Version entstand,
    # NIE stillschweigend geraten.
    quelle_typ: Mapped[str] = mapped_column(String(24))
    quelle_referenz: Mapped[str | None] = mapped_column(String(256), nullable=True)
    erstellt_von: Mapped[str] = mapped_column(String(128))
    erstellt_am: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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


class MahnKanalregelTable(Base):
    """Versionierte Kanalregel für den automatischen Mahnversand
    (Auftrag HV-20260914-MAHNUNG-BRIEF, Nutzerentscheidung 14.09.2026:
    "Stufe 1 EMAIL, Stufe 2 BRIEF"). Mirrors `MahnPolicyTable`s
    Governance-Muster (ENTWURF -> FREIGEGEBEN); `mahnwesen/service.py::
    MahnwesenService.plane_mahnlauf` verwendet AUSSCHLIESSLICH die
    aktuell freigegebene Regel, NIE einen im Code hartkodierten
    Kanal - eine künftige Änderung der Kanalzuordnung braucht dadurch
    keinen Deploy, sondern nur eine neue freigegebene Version.

    Fehlt jede freigegebene Regel, bleibt der automatische Mahnversand
    komplett blockiert (dieselbe "kein GESENDET ohne geprüfte
    Grundlage"-Regel wie bei `MahnPolicyTable`) - es wird NIE
    stillschweigend ein Default-Kanal unterstellt."""

    __tablename__ = "mahn_kanalregeln"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version: Mapped[int] = mapped_column(Integer, unique=True)
    stufe1_kanal: Mapped[str] = mapped_column(String(16), default="EMAIL")
    stufe2_kanal: Mapped[str] = mapped_column(String(16), default="BRIEF")
    status: Mapped[str] = mapped_column(String(16), default="ENTWURF", server_default=text("'ENTWURF'"))
    erstellt_von: Mapped[str] = mapped_column(String(128))
    erstellt_am: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    geprueft_von: Mapped[str | None] = mapped_column(String(128), nullable=True)
    geprueft_am: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BriefAnbieterProfilTable(Base):
    """Versioniertes, geprüftes Preis-/Tarifprofil eines Briefversand-
    Anbieters (Auftrag HV-20260914-MAHNUNG-BRIEF) - mirrors
    `ZinsprofilTable`s Governance-Muster. Fehlt jede geprüfte Version
    für die gewünschte `briefart`, bleibt der Briefkanal EXPLIZIT
    blockiert (siehe `mahnwesen/service.py::_pruefe_frisch_
    versandbereit`) - es wird NIE ein erfundener Standardpreis
    unterstellt. Die Preisfelder bilden den TATSÄCHLICHEN
    Anbieteraufwand ab (was der Anbieter JLB/Markus in Rechnung
    stellt); ob/wieviel davon gegenüber dem Mieter nach §1333 Abs 2
    ABGB tatsächlich ERSATZFÄHIG ist, entscheidet ZUSÄTZLICH
    `ZinsprofilTable.versandkosten_ersatzfaehig_geprueft` (getrennte
    Prüfung, siehe dortiger Docstring) - `ersatzfaehiger_hoechstbetrag_
    cent` deckelt den ersatzfähigen Anteil optional nach unten, falls
    der volle Anbieterpreis nicht 1:1 weiterverrechnet werden darf."""

    __tablename__ = "brief_anbieterprofile"
    __table_args__ = (UniqueConstraint("briefart", "version", name="uq_brief_anbieterprofil_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version: Mapped[int] = mapped_column(Integer)
    briefart: Mapped[str] = mapped_column(String(24), default="STANDARD", server_default=text("'STANDARD'"))
    status: Mapped[str] = mapped_column(String(16), default="ENTWURF", server_default=text("'ENTWURF'"))
    anbieter_name: Mapped[str] = mapped_column(String(256))
    quelle_beleg: Mapped[str] = mapped_column(String(256))
    preis_druck_cent: Mapped[int] = mapped_column(Integer, default=0)
    preis_kuvert_cent: Mapped[int] = mapped_column(Integer, default=0)
    preis_porto_cent: Mapped[int] = mapped_column(Integer, default=0)
    preis_nachweis_cent: Mapped[int | None] = mapped_column(Integer, nullable=True)  # z. B. Einschreiben-Zuschlag
    # Optionale Deckelung des NACH §1333 Abs 2 ABGB ersatzfähigen Anteils
    # (siehe Klassendoc) - `None` bedeutet: der volle Anbieteraufwand ist
    # (vorbehaltlich `versandkosten_ersatzfaehig_geprueft`) der Ansatz.
    ersatzfaehiger_hoechstbetrag_cent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    erstellt_von: Mapped[str] = mapped_column(String(128))
    erstellt_am: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    geprueft_von: Mapped[str | None] = mapped_column(String(128), nullable=True)
    geprueft_am: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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


class MahnLaufTable(Base):
    """Persistente, ATOMARE Gruppensperre für einen gebündelten
    Mahnversand (Auftrag HV-20260914-MAHNUNG-BRIEF, löst die frühere
    "kleinste-Id"-Leader-Heuristik ab - siehe Rückprüfung 14.09.2026,
    Risiko 1+2). GENAU EIN `MahnLaufTable`-Eintrag je (`vertrag_id`,
    `stufe`, exakte Mitgliedermenge) - `mitglieder_mahnfall_ids` ist ein
    beim Bilden der Gruppe EINGEFRORENER Snapshot der zu diesem
    Zeitpunkt tatsächlich sendeberechtigten (nicht bloß "irgendwie
    geplanten") `MahnFallTable`-Ids, siehe
    `mahnwesen/service.py::MahnwesenService.plane_mahnlauf`.

    Der atomare Compare-and-Swap GEPLANT -> IN_VERSAND läuft über GENAU
    DIESE Zeile (nicht mehr über einen einzelnen "führenden" MahnFall) -
    solange ein Mahnlauf IN_VERSAND, UNSICHER oder GESENDET ist, darf
    KEIN Kanal (E-Mail, künftig Brief - `kanal`-Spalte) für dieselben
    Mitglieder einen weiteren Versand versuchen, siehe
    `versende_mahnlauf`. Dasselbe Wiederanlauf-/Recovery-Prinzip wie bei
    `MahnFallTable`: ein zwischen Claim und Ergebnis abgestürzter Lauf
    bleibt IN_VERSAND und wird NUR über
    `markiere_verwaiste_mahnlaeufe_als_unsicher` (nie automatisch
    erneut versucht) auf UNSICHER aufgelöst."""

    __tablename__ = "mahnlaeufe"
    __table_args__ = (UniqueConstraint("outbox_key", name="uq_mahnlauf_outbox_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    outbox_key: Mapped[str] = mapped_column(String(256))
    vertrag_id: Mapped[str] = mapped_column(ForeignKey("vertraege.id"), index=True)
    gesellschaft_id: Mapped[str] = mapped_column(ForeignKey("gesellschaften.id"), index=True)
    stufe: Mapped[int] = mapped_column(Integer)
    mitglieder_mahnfall_ids: Mapped[str] = mapped_column(Text)  # JSON-Liste, sortiert
    kanal: Mapped[str] = mapped_column(String(16), default="EMAIL", server_default=text("'EMAIL'"))
    status: Mapped[str] = mapped_column(String(16), default="GEPLANT", server_default=text("'GEPLANT'"))
    geplant_am: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    versand_beansprucht_am: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    gesendet_am: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fehlergrund: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Recovery-Paket 14.09.2026 (Nutzerauftrag: "Recovery nach bestätigtem
    # Versand mit eingefrorenem Kosten-/Inhaltssnapshot"): wird ATOMAR IN
    # DERSELBEN UPDATE-Anweisung wie der Übergang auf GESENDET geschrieben
    # (siehe `mahnwesen/service.py::MahnwesenService.versende_mahnlauf` /
    # `indexautomatik/mailnachweis.py::versand_belegen`-`zusatz`-Parameter) -
    # NIEMALS erst danach in einem zweiten Schritt. Enthält die exakt
    # gleiche `MahnkostenVorschau`, die auch den tatsächlich gesendeten
    # Brief-/Mailtext gespeist hat (`kosten.py::snapshot_zu_json`), oder
    # das JSON-Literal `null`, wenn kein Kostenservice konfiguriert ist/
    # nichts zu berechnen war. Ein Absturz ZWISCHEN bestätigtem Versand
    # und der eigentlichen Kostenbuchung kann dadurch die Buchung IMMER
    # anhand DERSELBEN eingefrorenen Zahlen nachholen (nie eine neu
    # berechnete, ggf. abweichende Vorschau) - siehe
    # `MahnwesenService.vervollstaendige_gesendete_mahnlaeufe_ohne_
    # kostenabschluss`. `NULL` (Spaltenwert, nicht JSON-`null`) bedeutet:
    # noch gar nicht bis zum Versand gekommen.
    mahnkosten_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Gesetzt, sobald die Kostenbuchung für diesen Mahnlauf ABGESCHLOSSEN
    # ist (tatsächlich gebucht ODER bewusst als "nichts zu buchen"
    # erkannt) - NIE vorher. `status == "GESENDET" AND mahnkosten_
    # snapshot_json IS NOT NULL AND mahnkosten_verarbeitet_am IS NULL`
    # markiert genau die Recovery-Lücke.
    mahnkosten_verarbeitet_am: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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
    # Additiv (Auftrag HV-20260919-INDEX-MONATSBERICHT, Codex-Korrektur:
    # "bereits 34 Drafts vorhanden ... keine unnötigen dritten identischen
    # Drafts. Bei unveränderten Daten wiederholter Import ohne neue
    # Version"). `import_inhalt_hash` ist der Hash über GENAU die vom
    # generischen Quellenimport (`indexautomatik/rechtsprofil_import.py`)
    # übergebenen Eingabefelder dieser Zeile - VOR jedem Anlegen einer
    # neuen Version prüft der Import gegen ALLE bestehenden Versionen
    # desselben Vertrags; ein Treffer überspringt den Import ersatzlos
    # (kein Duplikat). NIE für manuell im Portal erstellte Profile
    # gesetzt (bleibt dort `None`) - unterscheidet importierte von
    # manuell erfassten Zeilen, ohne deren Verhalten zu ändern.
    import_quelle: Mapped[str | None] = mapped_column(String(256), nullable=True)
    import_inhalt_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


class IndexQuellenFaktenTable(Base):
    """Versionierte, rein INFORMATIVE Quellenfakten je Vertrag (Auftrag
    HV-20260919-INDEX-MONATSBERICHT, Codex-Korrektur: "Ein weiterer
    ENTWURF allein reicht nicht ... neue Quellenfakten müssen im Bericht
    tatsächlich nutzbar/sichtbar sein"). Diese Zeile ist NIEMALS Eingabe
    für `IndexautomatikService`/`mieweg_vorschau_service`/`index/service.py`
    - sie fließt AUSSCHLIESSLICH in die Textanreicherung des
    `IndexMonatsberichtZeileTable`-Berichts ein (Grund/Vertragsbasis-
    Anzeige), wenn (noch) kein freigegebenes `RechtsprofilTable` existiert
    und der reale Monatslauf deshalb nur die generische "kein Profil"-
    Meldung liefert. Deckt bewusst einen unvollständigen Zwischenstand ab
    (z. B. eine bestätigte AKTUELLE Gesamtmiete OHNE jede erfasste
    `VertragsKomponenteTable`-Zeile) - eine solche Lücke wird hier
    dokumentiert sichtbar gemacht, NIE stillschweigend durch einen
    erfundenen Komponentenwert oder eine Nullmiete ersetzt.

    Versioniert wie `RechtsprofilTable` (`naechste_version`), aber ohne
    Freigabemechanismus - jede Version ist von Anfang an "wirksam" für
    die Berichtsanreicherung (die neueste Version je Vertrag zählt).
    `inhalt_hash` ist der Hash über GENAU die importierten Fachfelder
    (siehe `indexautomatik/rechtsprofil_import.py`) - identischer
    Re-Import erzeugt KEINE neue Version."""

    __tablename__ = "index_quellen_fakten"
    __table_args__ = (UniqueConstraint("vertrag_id", "version", name="uq_index_quellen_fakten_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vertrag_id: Mapped[str] = mapped_column(ForeignKey("vertraege.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    quelle: Mapped[str] = mapped_column(String(256))
    inhalt_hash: Mapped[str] = mapped_column(String(64))
    # Codex-Korrektur: `VertragTable.rechtsordnung` (MRG_VOLL/MRG_TEIL)
    # allein sagt NICHTS über tatsächliche Wohnungsnutzung aus - ein
    # MRG_TEIL-Objekt kann Büro/Geschäftsraum sein. DREIWERTIG wie
    # `RechtsprofilTable.ist_hauptmiete`: `None` (ungeklärt) sperrt den
    # Gewerbe-Rechenvorschlag GENAUSO wie `True` (Wohnung) - NUR eine
    # explizit verifizierte `False` (geprüft: KEINE Wohnungsnutzung)
    # erlaubt ihn (siehe `monatsbericht_service.py::
    # _gewerbe_rechenvorschlag`, fail-closed).
    ist_wohnungsnutzung: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Ursprüngliche, laut Vertragswortlaut dokumentierte Klauselbasis -
    # KEINE aktuell freigegebene `IndexKlauselTable` (die bleibt der
    # einzige Weg zu einer echten Berechnung).
    urspruengliche_klauselbasis_reihe: Mapped[str | None] = mapped_column(String(32), nullable=True)
    urspruengliche_klauselbasis_monat: Mapped[str | None] = mapped_column(String(7), nullable=True)
    urspruengliche_klauselbasis_wert: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Betrag ZUM ZEITPUNKT der ursprünglichen Klauselbasis - NIE der
    # heute tatsächlich verrechnete (ggf. bereits erhöhte) Betrag
    # (Codex: "nie den heutigen bereits erhöhten Betrag mit
    # ursprünglicher Basis multiplizieren"). `betrag_basisbindung_belegt`
    # ist eine EXPLIZITE, gesondert bestätigte Aussage, dass dieser
    # Betrag tatsächlich zur obigen Klauselbasis gehört - ohne dieses
    # Flag wird der Betrag NIE für den Gewerbe-Rechenvorschlag verwendet
    # (siehe `monatsbericht_service.py::_gewerbe_rechenvorschlag`).
    urspruenglicher_indexbetrag_cent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    betrag_basisbindung_belegt: Mapped[bool] = mapped_column(Boolean, default=False)
    # Schwellen-/Dämpfungsparameter der dokumentierten Gewerbeklausel -
    # AUSSCHLIESSLICH für den read-only Gewerbe-Rechenvorschlag, NIEMALS
    # für eine echte `IndexKlauselTable`/Buchung. `schwelle_inklusive`
    # bleibt bewusst NULLABLE (kein Default) - ohne explizite Angabe
    # bleibt der Rechenvorschlag gesperrt statt eine Vertragsauslegung
    # zu raten.
    schwelle_prozent: Mapped[str | None] = mapped_column(String(32), nullable=True)
    schwelle_inklusive: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    daempfung_prozent: Mapped[str | None] = mapped_column(String(32), nullable=True)
    vertragliche_grenze_prozent: Mapped[str | None] = mapped_column(String(32), nullable=True)
    klauselregel_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Bestätigte AKTUELLE Gesamtmiete - bewusst GETRENNT von
    # `VertragsKomponenteTable` (die kann fehlen, siehe Klassendoc).
    bestaetigte_gesamtmiete_cent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bestaetigte_gesamtmiete_quelle: Mapped[str | None] = mapped_column(String(256), nullable=True)
    bestaetigte_gesamtmiete_stichtag: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Letzte TATSÄCHLICHE Indexbasis - NIE aus `VertragTable.gueltig_von`
    # abgeleitet (Codex: "Wohnungsbasis-Vertragsmonat ist nicht
    # automatisch bezugsmonat letzter Erhöhung"), bleibt `None`, wenn
    # unbekannt.
    letzte_tatsaechliche_basis_jahr: Mapped[int | None] = mapped_column(Integer, nullable=True)
    letzte_tatsaechliche_basis_monat: Mapped[int | None] = mapped_column(Integer, nullable=True)
    letzte_tatsaechliche_basis_hinweis: Mapped[str | None] = mapped_column(Text, nullable=True)
    bereits_enthaltene_erhoehungen_hinweis: Mapped[str | None] = mapped_column(Text, nullable=True)
    pruefhinweis: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Strukturierter, WIEDERKEHRENDER Kalendermonat (1-12, z. B. 1 für
    # eine Jännerklausel) statt eines eingefrorenen Datums (Codex:
    # "Januar-/Apriltermine aus Regeln dynamisch ableiten, nicht 2027
    # ewig als Festtext konservieren") - der Bericht berechnet daraus
    # JEDEN Lauf erneut den nächsten tatsächlichen Kalendertermin
    # (`indexautomatik/service.py::_naechster_gueltiger_kalendermonat`,
    # wiederverwendet, keine zweite Formel). `None`, wenn keinerlei
    # Periodizität bekannt ist - dann bleibt nur der Freitexthinweis.
    bedingter_naechster_monat: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bedingter_fruehester_termin_hinweis: Mapped[str | None] = mapped_column(Text, nullable=True)
    quellenreferenzen: Mapped[list] = mapped_column(JSON, default=list)
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


class IndexMonatsberichtTable(Base):
    """Persistenter, deterministischer Owner-Monatsbericht (Auftrag
    HV-20260919-INDEX-MONATSBERICHT) - GENAU eine Kopfzeile je Kalender-
    monat "YYYY-MM", vom monatlichen Indexautomatik-Lauf erzeugt
    (`scripts/indexautomatik_monatslauf.py`, alleiniger Erzeuger - kein
    neuer Scheduler). Trägt den Versandstatus der EINEN Owner-Sammelmail
    für diesen Monat (Auftrag Umfang B); Claim/Recovery/Status-Abgleich
    laufen exakt wie bei `VertragsendeErinnerungTable` über die
    bestehende tägliche Pflege."""

    __tablename__ = "index_monatsberichte"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    periode: Mapped[str] = mapped_column(String(7), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(24), default="BEREIT")
    anzahl_vertraege: Mapped[int] = mapped_column(Integer, default=0)
    anzahl_moeglich: Mapped[int] = mapped_column(Integer, default=0)
    anzahl_noch_nicht_moeglich: Mapped[int] = mapped_column(Integer, default=0)
    anzahl_pruefung_noetig: Mapped[int] = mapped_column(Integer, default=0)
    versand_beansprucht_am: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    versendet_am: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    externe_versandreferenz: Mapped[str | None] = mapped_column(String(128), nullable=True)
    fehlergrund: Mapped[str | None] = mapped_column(Text, nullable=True)
    erstellt_am: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IndexMonatsberichtZeileTable(Base):
    """Eine Zeile je (Bericht, Vertrag) - der eigentliche fachliche
    Snapshot für Umfang A. Wird AUSSCHLIESSLICH aus dem bereits real
    berechneten `IndexautomatikLaufTable`/`ErhoehungsschreibenTable`
    dieses Monats abgeleitet (keine zweite Berechnung); `IndexQuellen
    FaktenTable` liefert NUR Text-/Anzeige-Anreicherung, wenn (noch)
    kein freigegebenes Profil existiert. `vorschlag_cent`/
    `differenz_cent`/`gesamtvorschreibung_cent`/`indexierbarer_
    mietanteil_cent` bleiben bewusst NULLABLE ohne Default - eine fehlende
    Zahl wird NIE durch 0 ersetzt (Auftrag: "kein 0-EUR-Ersatz")."""

    __tablename__ = "index_monatsbericht_zeilen"
    __table_args__ = (UniqueConstraint("vertrag_id", "periode", name="uq_index_monatsbericht_zeile"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bericht_id: Mapped[int] = mapped_column(ForeignKey("index_monatsberichte.id"), index=True)
    vertrag_id: Mapped[str] = mapped_column(ForeignKey("vertraege.id"), index=True)
    periode: Mapped[str] = mapped_column(String(7))
    # "MOEGLICH" | "NOCH_NICHT_MOEGLICH" | "PRUEFUNG_NOETIG" - siehe
    # `monatsbericht_service.py::_status_aus_lauf`.
    status: Mapped[str] = mapped_column(String(24))
    status_grund: Mapped[str] = mapped_column(Text)
    vorschlag_cent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    differenz_cent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fruehester_termin: Mapped[date | None] = mapped_column(Date, nullable=True)
    # "BEDINGT" (nächste gesetzlich/vertraglich mögliche Grenze, keine
    # Individualzusage) | "GEPRUEFT" (konkreter Termin dieses Vertrags,
    # z. B. aus einem bereits erzeugten Erhöhungsschreiben).
    fruehester_termin_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    gesamtvorschreibung_cent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # "KOMPONENTEN" (aus aktiven VertragsKomponenteTable-Zeilen summiert)
    # | "BESTAETIGT_OHNE_KOMPONENTEN" (aus IndexQuellenFaktenTable, keine
    # Komponentenzeilen vorhanden - Codex-Beispiel Pietsch/IMG).
    gesamtvorschreibung_quelle: Mapped[str | None] = mapped_column(String(32), nullable=True)
    indexierbarer_mietanteil_cent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Codex-Korrektur: "15 Ist-Komponentensätze haben indexierbar=false
    # bis Freigabe; daraus nicht 'indexierbarer Mietanteil 0 EUR' als
    # materielle Aussage" - wenn `indexierbarer_mietanteil_cent` NULL
    # ist, WEIL keine aktive Komponente (noch) als indexierbar markiert
    # ist (nicht weil keine Komponenten existieren), erklärt dieses Feld
    # das explizit statt einen stillen Nullwert zu zeigen.
    indexierbarer_mietanteil_hinweis: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Mieter/Objekt/Einheit-Anzeige, Vertragsbasis (Reihe/Monat/Wert),
    # letzte tatsächliche Indexierung, Quellenfakten-Hinweise - reine
    # Anzeigedaten, siehe `monatsbericht_service.py::_snapshot`.
    snapshot_json: Mapped[dict] = mapped_column(JSON, default=dict)
    rechtsprofil_id: Mapped[int | None] = mapped_column(ForeignKey("rechtsprofile.id"), nullable=True)
    rechtsprofil_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quellen_fakten_id: Mapped[int | None] = mapped_column(ForeignKey("index_quellen_fakten.id"), nullable=True)
    indexautomatik_lauf_id: Mapped[int | None] = mapped_column(ForeignKey("indexautomatik_laeufe.id"), nullable=True)
    erhoehungsschreiben_id: Mapped[int | None] = mapped_column(ForeignKey("erhoehungsschreiben.id"), nullable=True)
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


class ZinsprofilTable(Base):
    """Versioniertes, EINMALIG geprüftes und freigegebenes Zins-/
    Gebührenprofil je Vertrag (Auftrag Markus 13.09.2026: "pro Mahnlauf
    Mahngebühren, und die Zinsen dazu, soviel wie gesetzlich erlaubt
    ist"). Mirrors `RechtsprofilTable`s Governance-Muster: `ENTWURF`
    ändert NICHTS an der wirksamen Kostenberechnung, erst `GEPRUEFT`
    (menschliche Fachprüfung) treibt `mahnwesen/kosten.py` an.

    `vereinbarter_zinssatz_prozent` wird NUR verwendet, wenn
    `vereinbarung_geprueft=True` - eine gelesene Vertragsklausel ist
    laut KSchG §6 Abs 1 Z 13/OGH 7Ob111/25m KEINE automatische
    Wirksamkeitsfreigabe, insbesondere bei einem Verbraucher-Mieter
    (`ist_b2b=False`). `mahngebuehr_kostenbasis_cent` ist die laut
    §1333 Abs 2 ABGB / §458 UGB geprüfte, tatsächliche/zweckmäßige
    Kostenbasis - NIE eine erfundene Pauschale.

    `gueltig_ab` (Rückprüfung 14.09.2026, Risiko 3): das EXPLIZIT
    belegte Datum, AB DEM diese konkrete Version tatsächlich gilt - NICHT
    zu verwechseln mit `geprueft_am` (nur der interne Freigabezeitpunkt).
    Existiert für einen Vertrag NUR EINE JE GEPRÜFTE Version, gilt sie
    (wie bisher, unverändertes Verhalten) für die gesamte berechenbare
    Periode. Existieren MEHRERE geprüfte Versionen, wird NUR
    periodengerecht (je Tag die zu diesem Tag gültige Version) gerechnet,
    wenn ALLE beteiligten Versionen ein `gueltig_ab` tragen - fehlt es
    bei auch nur einer, bleibt der betroffene Zinsanteil explizit
    "unberechenbar" statt rückwirkend die aktuell/zuletzt geprüfte
    Version zu verwenden (siehe `mahnwesen/kosten.py::
    _zinsprofil_segmente`).

    `verzugsverantwortung_geprueft` (unabhängige Rückprüfung Codex
    14.09.2026): §456 UGB (der ERHÖHTE Zinssatz) setzt neben B2B/Datum
    voraus, dass der Zahlungsverzug dem Schuldner zuzurechnen/von ihm zu
    verantworten ist und dies auch BELEGT geprüft wurde - ein bloß
    unterstellter, nicht belegter Verzug reicht NICHT für den erhöhten
    Satz. Ist dies (noch) nicht geprüft, fällt die VERZINSUNG (nicht die
    §458-Pauschale, die laut Gesetzesmaterialien verschuldensunabhängig
    ist und davon unberührt bleibt) konservativ auf die gesetzlichen 4 %
    ABGB zurück, statt automatisch den höheren UGB-Satz anzusetzen
    (siehe `mahnwesen/kosten.py::_ugb_zinssatz_anwendbar`)."""

    __tablename__ = "zinsprofile"
    __table_args__ = (UniqueConstraint("vertrag_id", "version", name="uq_zinsprofil_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vertrag_id: Mapped[str] = mapped_column(ForeignKey("vertraege.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="ENTWURF", server_default=text("'ENTWURF'"))
    # Beiderseits unternehmensbezogenes Geschäft - Voraussetzung für §456
    # UGB. NIE aus Rechtsordnung/Nutzungsart abgeleitet (dieselbe
    # Nie-Ableiten-Regel wie überall in diesem Repository).
    ist_b2b: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    vertragsdatum: Mapped[date | None] = mapped_column(Date, nullable=True)
    gueltig_ab: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Siehe Klassendoc oben - separat von `ist_b2b`/`vertragsdatum`, weil
    # B2B+Datum allein nur die §458-Pauschale rechtfertigt, NICHT den
    # erhöhten §456-Zinssatz.
    verzugsverantwortung_geprueft: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    # §1333 Abs 2 ABGB (Auftrag HV-20260914-MAHNUNG-BRIEF, Nutzerentscheidung
    # 14.09.2026): nur tatsächlich erwachsene, schuldhaft verursachte,
    # notwendige/zweckmäßige/angemessene Betreibungskosten (z. B. echte
    # Brief-Versandkosten) dürfen dem Mieter weiterverrechnet werden -
    # gilt (anders als §456/§458 UGB) AUCH für einen Verbraucher-Mieter.
    # Ein bloß vorhandenes Anbieterprofil reicht NICHT - diese
    # Voraussetzungen müssen gebündelt menschlich geprüft/belegt sein,
    # sonst bleibt die Versandkosten-Position bei 0 (siehe
    # `mahnwesen/kosten.py::berechne_mahnkosten_vorschau`).
    versandkosten_ersatzfaehig_geprueft: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    vereinbarter_zinssatz_prozent: Mapped[Decimal | None] = mapped_column(Numeric(6, 3), nullable=True)
    vereinbarung_geprueft: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    vereinbarung_beleg: Mapped[str | None] = mapped_column(String(256), nullable=True)
    mahngebuehr_kostenbasis_cent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mahngebuehr_kostenbasis_beleg: Mapped[str | None] = mapped_column(String(256), nullable=True)
    erstellt_von: Mapped[str] = mapped_column(String(128))
    erstellt_am: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    geprueft_von: Mapped[str | None] = mapped_column(String(128), nullable=True)
    geprueft_am: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OenbBasiszinssatzTable(Base):
    """Amtlicher OeNB-Basiszinssatz je Halbjahr (§456 UGB) - manuell mit
    Quellenbeleg erfasst, wie `VpiMonatswertTable`. OHNE erfassten
    Eintrag für das benötigte Halbjahr bleibt eine B2B-Zinsberechnung
    nach §456 UGB explizit "Basis ungeklärt" blockiert - NIE wird der
    letzte bekannte Wert für ein neues Halbjahr stillschweigend
    fortgeschrieben (siehe `mahnwesen/kosten.py::bestimme_zinssatz`)."""

    __tablename__ = "oenb_basiszinssaetze"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)  # z. B. "2026-1", "2026-2"
    gueltig_von: Mapped[date] = mapped_column(Date)
    gueltig_bis: Mapped[date] = mapped_column(Date)
    basiszinssatz_prozent: Mapped[Decimal] = mapped_column(Numeric(6, 3))
    erfasst_von: Mapped[str] = mapped_column(String(128))
    erfasst_am: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    quelle_referenz: Mapped[str] = mapped_column(String(256))


class MahnkostenBuchungTable(Base):
    """EIN Ledger-Eintrag je tatsächlich abgeschlossenem Mahnlauf-
    Kostenvorgang (Vertrag+Stufe) - garantiert GENAU EINE gebuchte
    Gebühr/Zinsposition pro Mahnlauf, unabhängig davon, wie viele
    einzelne OP-Zeilen/Mietkomponenten (HMZ/BK/HK/Küche/Parkplatz)
    diesen Mahnlauf technisch zusammensetzen (siehe
    `mahnwesen/kosten.py`-Moduldoc: "keine 5-10 Gebühren pro Monat").

    Wird AUSSCHLIESSLICH von `mahnwesen/kosten_service.py::MahnkostenService.
    buche_bei_versand` angelegt, NIEMALS bei einer bloßen Vorschau, einer
    fehlgeschlagenen Sendung oder einem Retry - siehe dort. Die
    Verzugszinsen werden dabei IMMER nur als DELTA zur Summe aller
    bereits für diesen Vertrag gebuchten Zinsen (über ALLE Mahnstufen
    hinweg, siehe `MahnkostenRepository.bereits_gebuchte_zinsen_cent`)
    angesetzt - Stufe 2 rechnet dieselben, bei Stufe 1 bereits
    fakturierten Tage NIE erneut ab; ein Delta von 0 (z. B. ein zweiter
    Lauf am selben Tag ohne neu verstrichene Zeit) bucht nichts, ohne
    dass dafür ein eigener Sperrmechanismus nötig ist. `uq_mahnkosten_
    lauf` fängt nur eine ECHTE gleichzeitige Doppelausführung für
    DENSELBEN eingefrorenen Mahnlauf ab (Idempotenz-Sicherheitsnetz,
    kein fachliches Gate).

    Eindeutigkeit hängt BEWUSST an `mahnlauf_schluessel` (dem Hash der
    exakten, eingefrorenen Forderungs-Mitgliedermenge), NICHT an
    `zins_bis` - unabhängige Rückprüfung Codex 14.09.2026, echter Bug:
    zwei DISJUNKTE Gruppen desselben Vertrags/derselben Stufe können am
    selben Kalendertag denselben `zins_bis`-Stichtag haben, obwohl sie
    völlig unterschiedliche Forderungen betreffen. Mit einer
    Eindeutigkeit auf `zins_bis` würde der zweite `INSERT` fälschlich
    als "derselbe Vorgang, bereits gebucht" abgelehnt und der
    IntegrityError-Handler in `kosten_service.py::buche_vorschau` hätte
    den FREMDEN Ledger der ANDEREN Gruppe als vermeintlich eigenen
    Kostenbeleg zurückgegeben - der behauptete und der tatsächlich
    gebuchte Betrag wären für die zweite Gruppe auseinandergelaufen."""

    __tablename__ = "mahnkosten_buchungen"
    __table_args__ = (UniqueConstraint("vertrag_id", "stufe", "mahnlauf_schluessel", name="uq_mahnkosten_lauf"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vertrag_id: Mapped[str] = mapped_column(ForeignKey("vertraege.id"), index=True)
    stufe: Mapped[int] = mapped_column(Integer)
    # Rein deskriptiv/für Audit: welche Forderungs-OP-IDs zum
    # Buchungszeitpunkt in die Hauptforderung eingeflossen sind - NICHT
    # Teil der Idempotenz-/Delta-Logik (die läuft ausschließlich über die
    # vertragsweite Zinsensumme, siehe oben).
    mahnlauf_schluessel: Mapped[str] = mapped_column(String(128))
    forderung_op_position_ids: Mapped[str] = mapped_column(Text)  # JSON-Liste von OPPositionTable.id
    hauptforderung_cent: Mapped[int] = mapped_column(Integer)
    zinsbasis: Mapped[str] = mapped_column(String(32))
    zinssatz_prozent: Mapped[Decimal] = mapped_column(Numeric(6, 3))
    zins_von: Mapped[date] = mapped_column(Date)
    zins_bis: Mapped[date] = mapped_column(Date)
    zinsen_cent: Mapped[int] = mapped_column(Integer)
    gebuehr_cent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rechtsgrundlage_gebuehr: Mapped[str | None] = mapped_column(String(256), nullable=True)
    versandnachweis_referenz: Mapped[str] = mapped_column(String(256))
    zinsen_op_position_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gebuehr_op_position_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Vollständiges Zinssegment-Bild (Halbjahres-/Basiszinssatz-Teilperioden,
    # siehe `mahnwesen/kosten.py::ZinsSegment`) ZUM ZEITPUNKT dieser Buchung -
    # rein deskriptiv/Audit, NICHT auf das an diesem Tag NEU gebuchte Delta
    # isoliert (das wäre bei mehreren Segmenten nicht eindeutig zuordenbar).
    # JSON-Liste von {von, bis, rest_cent, satz_prozent, quelle, zinsen_cent}.
    zins_segmente_json: Mapped[str] = mapped_column(Text, default="[]", server_default=text("'[]'"))
    # Das GENAU an diesem Tag NEU gebuchte Zinsdelta, JE `op_position_id`
    # (unabhängige Rückprüfung Codex 14.09.2026, echter Bug): NIEMALS aus
    # `zins_segmente_json` ableiten (siehe dortiger Docstring) - das wäre
    # die volle, ab der Fälligkeit neu berechnete Periode, nicht das an
    # DIESEM Tag zusätzlich gebuchte Delta, und würde bei einer späteren
    # Stufe/einem Retry zu doppelt gezählten Zinsen führen
    # (`MahnkostenRepository.bereits_gebuchte_zinsen_je_op_position`
    # summiert ausschließlich DIESES Feld über alle Buchungen). JSON-Objekt
    # {op_position_id (als String): delta_cent}.
    zinsen_delta_je_op_json: Mapped[str] = mapped_column(Text, default="{}", server_default=text("'{}'"))
    gebucht_am: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    erstellt_von: Mapped[str] = mapped_column(String(128))


class MahnkostenGebuehrTable(Base):
    """Permanente, EINMALIGE Erhebung der §458-UGB-Mahnspesen-Pauschale je
    zugrunde liegender qualifizierter Entgeltforderung (Auftrag Markus
    14.09.2026 - Rückprüfung: "§458 UGB gilt nicht je Mahnlauf oder Brief
    und nicht je Mietkomponente ... Anspruch auf zugrunde liegende
    qualifizierte fällige Unternehmerforderung beziehen, bereits erhobenen
    Ansatz dauerhaft erkennen").

    `entgeltforderung_schluessel` gruppiert alle OP-Zeilen EINER
    Vorschreibungsperiode (z. B. HMZ+BK+HK desselben Monats teilen sich
    dieselbe `leistungsperiode`) zu EINER zugrunde liegenden
    Entgeltforderung - eine bereits hier erfasste Zeile lässt eine
    identische Forderung NIE wieder eine zweite Pauschale auslösen, egal
    wie viele weitere Mahnläufe/Stufen/Wiederholungen später folgen (siehe
    `mahnwesen/kosten.py::_entgeltforderung_schluessel`). Die
    Unique-Constraint ist hier - anders als bei `MahnkostenBuchungTable` -
    das FACHLICHE Gate selbst, nicht nur ein Race-Sicherheitsnetz: ein
    zweiter Versuch für dieselbe Forderung MUSS scheitern.

    `status`/`reserviert_fuer_mahnlauf_id` (Auftrag Markus 14.09.2026,
    unabhängige Rückprüfung, echter Bug): eine Zeile entsteht ZWEISTUFIG
    - `RESERVIERT` (angelegt SOFORT nach gewonnenem Gruppen-Claim, VOR
    Text-/Kostenfreeze und jedem Providerkontakt, siehe
    `MahnkostenService.reserviere_und_kuerze_vorschau`) und erst NACH
    bestätigtem Versand auf `GEBUCHT` umgeschrieben (`MahnkostenRepository.
    finalisiere_reservierte_gebuehr`, NIE ein zweiter `INSERT`). Ohne
    diese frühe Reservierung konnten zwei DISJUNKTE, gleichzeitig in
    Arbeit befindliche Mahnlauf-Gruppen (z. B. zwei Komponenten
    derselben Vorschreibungsperiode in unterschiedlichen Gruppen) BEIDE
    unabhängig voneinander dieselbe, noch unbestätigte Pauschale in
    ihrem jeweiligen Brief-/Mailtext ankündigen - die Unique-Constraint
    hätte erst bei der ZWEITEN tatsächlichen Buchung gegriffen, als der
    fälschlich doppelt angekündigte Brief längst versendet war. Die
    Reservierung bleibt über einen UNSICHER-Zustand hinweg bestehen
    (der Provider könnte bereits angenommen haben); bei einer sauberen
    Blockade VOR jedem Providerkontakt wird sie wieder freigegeben
    (`MahnkostenRepository.gib_reservierung_frei`)."""

    __tablename__ = "mahnkosten_gebuehren"
    __table_args__ = (UniqueConstraint("vertrag_id", "entgeltforderung_schluessel", name="uq_mahnkosten_gebuehr_forderung"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vertrag_id: Mapped[str] = mapped_column(ForeignKey("vertraege.id"), index=True)
    entgeltforderung_schluessel: Mapped[str] = mapped_column(String(200))
    betrag_cent: Mapped[int] = mapped_column(Integer)
    rechtsgrundlage: Mapped[str] = mapped_column(String(256))
    mahnkosten_buchung_id: Mapped[int | None] = mapped_column(ForeignKey("mahnkosten_buchungen.id"), nullable=True)
    gebuehr_op_position_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # RESERVIERT (noch nicht bestätigt gesendet/gebucht) | GEBUCHT
    # (tatsächlicher Versand bestätigt, Buchungsreferenzen oben gesetzt).
    status: Mapped[str] = mapped_column(String(16), default="RESERVIERT", server_default=text("'RESERVIERT'"))
    # Bewusst PLAIN Integer ohne echten `ForeignKey` (Präzedenzfall in
    # dieser Tabelle selbst: `gebuehr_op_position_id` ist ebenfalls eine
    # lose Referenz ohne FK) - hält `ensure_additive_columns`s
    # FK-Spalten-Sonderfall aus dieser rein additiven Erweiterung heraus.
    reserviert_fuer_mahnlauf_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    erhoben_am: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    erstellt_von: Mapped[str] = mapped_column(String(128))
