"""Abnahmehinweis (HV-20260913-VERSAND-SOLL): `rechtsprofile`/
`erhoehungsschreiben` existieren produktiv bereits (befüllt) -
`create_all_tables` legt NUR fehlende TABELLEN an, ergänzt aber KEINE
Spalten bestehender Tabellen. Diese Tests bauen deshalb bewusst ein
ECHTES ALTSCHEMA nach (die vollständigen Tabellen wie sie VOR diesem
Auftrag bestanden, ohne die drei neuen Spalten
`rechtsprofile.frist_tage_zugang_bis_wirksamkeit`/`frist_quellenbeleg`
und `erhoehungsschreiben.komponenten_verteilung`), befüllen sie mit
Zeilen und prüfen, dass `ensure_additive_columns`:

- die fehlenden Spalten nachzieht,
- bestehende Zeilen/Spaltenwerte NICHT verändert,
- Rückwärtsverträglichkeit herstellt (ORM-Zugriff auf die migrierte DB
  funktioniert, inkl. korrektem `default`/`server_default` für
  `komponenten_verteilung`),
- idempotent ist (ein zweiter Lauf ändert nichts mehr)."""

from __future__ import annotations

import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

import sqlalchemy as sa
import pytest
from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.infrastructure.db import tables as _tables  # noqa: F401 - registriert ORM-Tabellen
from mietinkasso.infrastructure.db.migrations import ensure_additive_columns
from mietinkasso.infrastructure.db.tables import ErhoehungsschreibenTable, RechtsprofilTable


def _altschema_metadata() -> tuple[sa.MetaData, sa.Table, sa.Table]:
    """Vollständiger Spaltensatz von `rechtsprofile`/`erhoehungsschreiben`
    WIE VOR diesem Auftrag (Stand 6a8d183) - jede hier fehlende Spalte
    ist absichtlich exakt eine der drei neuen Spalten dieses Auftrags."""

    meta = sa.MetaData()
    rechtsprofile = sa.Table(
        "rechtsprofile",
        meta,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("vertrag_id", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("rechtsordnung", sa.String(48), nullable=False),
        sa.Column("ist_wohnungsnutzung", sa.Boolean, nullable=True),
        sa.Column("mrg_zinsbeschraenkung", sa.Boolean, nullable=False),
        sa.Column("ist_altvertrag", sa.Boolean, nullable=False),
        sa.Column("ist_hauptmiete", sa.Boolean, nullable=True),
        sa.Column("foerderbindung", sa.Boolean, nullable=False),
        sa.Column("mietzinsobergrenze_cent", sa.Integer, nullable=True),
        sa.Column("mietzinsobergrenze_quellenbeleg", sa.String(256), nullable=True),
        sa.Column("mietzinsobergrenze_gueltig_bis", sa.Date, nullable=True),
        sa.Column("bezugsjahr", sa.Integer, nullable=True),
        sa.Column("bezugsmonat", sa.Integer, nullable=True),
        sa.Column("letzte_basis_war_jahresdurchschnitt", sa.Boolean, nullable=False),
        sa.Column("basis_komponenten_ids", sa.JSON, nullable=False),
        sa.Column("vpi_reihe", sa.String(32), nullable=False),
        sa.Column("vertraglich_zulaessiger_betrag_cent", sa.Integer, nullable=True),
        sa.Column("vertraglicher_quellenbeleg", sa.String(256), nullable=True),
        sa.Column("vertraglicher_fruehestmoeglicher_termin", sa.Date, nullable=True),
        sa.Column("vertragsklausel_id", sa.Integer, nullable=True),
        sa.Column("vertrag_beleg_referenz", sa.String(256), nullable=False),
        sa.Column("klausel_referenz", sa.String(256), nullable=True),
        # frist_tage_zugang_bis_wirksamkeit/frist_quellenbeleg: NEU, bewusst NICHT hier.
        sa.Column("quelle_hash", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("freigegeben_von", sa.String(128), nullable=True),
        sa.Column("freigegeben_am", sa.DateTime, nullable=True),
        sa.Column("erstellt_von", sa.String(128), nullable=False),
        sa.Column("erstellt_am", sa.DateTime, nullable=False),
    )
    erhoehungsschreiben = sa.Table(
        "erhoehungsschreiben",
        meta,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("vertrag_id", sa.String(64), nullable=False),
        sa.Column("ziel_bewertungsjahr", sa.Integer, nullable=True),
        sa.Column("rechtsprofil_id", sa.Integer, nullable=False),
        sa.Column("rechtsprofil_version", sa.Integer, nullable=False),
        sa.Column("mieweg_vorschau_id", sa.Integer, nullable=True),
        sa.Column("mieweg_vorschau_final_id", sa.Integer, nullable=True),
        sa.Column("index_anpassung_id", sa.Integer, nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("massgeblicher_termin", sa.Date, nullable=False),
        sa.Column("erhoehung_cent", sa.Integer, nullable=False),
        sa.Column("schreiben_text", sa.Text, nullable=False),
        sa.Column("schreiben_snapshot", sa.JSON, nullable=False),
        sa.Column("idempotenzschluessel", sa.String(128), nullable=False),
        sa.Column("versandkanal", sa.String(64), nullable=True),
        sa.Column("versendet_am", sa.DateTime, nullable=True),
        sa.Column("versand_beansprucht_am", sa.DateTime, nullable=True),
        sa.Column("externe_versandreferenz", sa.String(128), nullable=True),
        sa.Column("zugangsform", sa.String(48), nullable=True),
        sa.Column("zugang_bestaetigt_am", sa.Date, nullable=True),
        sa.Column("zugang_beleg", sa.String(256), nullable=True),
        sa.Column("zahlungspflicht_ab", sa.Date, nullable=True),
        sa.Column("fehlergrund", sa.Text, nullable=True),
        sa.Column("blockiert_gruende", sa.JSON, nullable=False),
        sa.Column("empfaenger_snapshot", sa.JSON, nullable=False),
        # komponenten_verteilung: NEU, bewusst NICHT hier.
        sa.Column("erstellt_am", sa.DateTime, nullable=False),
        sa.Column("aktualisiert_am", sa.DateTime, nullable=False),
    )
    return meta, rechtsprofile, erhoehungsschreiben


@pytest.fixture
def altschema_engine(tmp_path: Path):
    db_pfad = tmp_path / "altschema.db"
    engine = sa.create_engine(f"sqlite:///{db_pfad}", future=True)
    meta, rechtsprofile, erhoehungsschreiben = _altschema_metadata()
    meta.create_all(engine)

    jetzt = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    with engine.begin() as conn:
        conn.execute(
            rechtsprofile.insert().values(
                vertrag_id="V-ALT",
                version=1,
                rechtsordnung="OESTERREICH_MRG_VOLL",
                ist_wohnungsnutzung=True,
                mrg_zinsbeschraenkung=False,
                ist_altvertrag=False,
                ist_hauptmiete=True,
                foerderbindung=False,
                letzte_basis_war_jahresdurchschnitt=False,
                basis_komponenten_ids=["K-V-ALT-HMZ"],
                vpi_reihe="VPI20C18",
                vertraglich_zulaessiger_betrag_cent=150000,
                vertrag_beleg_referenz="BELEG-PRODUKTIV-1",
                quelle_hash="altehash123",
                status="FREIGEGEBEN",
                freigegeben_von="markus",
                freigegeben_am=jetzt,
                erstellt_von="markus",
                erstellt_am=jetzt,
            )
        )
        conn.execute(
            erhoehungsschreiben.insert().values(
                vertrag_id="V-ALT",
                ziel_bewertungsjahr=2026,
                rechtsprofil_id=1,
                rechtsprofil_version=1,
                status="SOLL_UMSETZUNG_OFFEN",
                massgeblicher_termin=date(2026, 4, 1),
                erhoehung_cent=500,
                schreiben_text="Bestehendes produktives Schreiben",
                schreiben_snapshot={"info": "alt"},
                idempotenzschluessel="V-ALT:mieweg:2026",
                zugangsform="EINSCHREIBEN",
                zugang_bestaetigt_am=date(2026, 3, 1),
                zugang_beleg="RSb-123",
                zahlungspflicht_ab=date(2026, 4, 15),
                blockiert_gruende=[],
                empfaenger_snapshot={"debitor_id": "D-ALT"},
                erstellt_am=jetzt,
                aktualisiert_am=jetzt,
            )
        )
    return engine


def test_ensure_additive_columns_ergaenzt_fehlende_spalten(altschema_engine):
    ausgefuehrt = ensure_additive_columns(altschema_engine)
    assert any("rechtsprofile" in a and "frist_tage_zugang_bis_wirksamkeit" in a for a in ausgefuehrt)
    assert any("rechtsprofile" in a and "frist_quellenbeleg" in a for a in ausgefuehrt)
    assert any("erhoehungsschreiben" in a and "komponenten_verteilung" in a for a in ausgefuehrt)

    inspector = sa.inspect(altschema_engine)
    rechtsprofile_spalten = {s["name"] for s in inspector.get_columns("rechtsprofile")}
    erhoehungsschreiben_spalten = {s["name"] for s in inspector.get_columns("erhoehungsschreiben")}
    assert {"frist_tage_zugang_bis_wirksamkeit", "frist_quellenbeleg"} <= rechtsprofile_spalten
    assert "komponenten_verteilung" in erhoehungsschreiben_spalten


def test_ensure_additive_columns_laesst_bestehende_werte_unveraendert(altschema_engine):
    ensure_additive_columns(altschema_engine)
    with altschema_engine.begin() as conn:
        rechtsprofil_row = conn.execute(
            sa.text(
                "SELECT vertrag_id, status, quelle_hash, vertrag_beleg_referenz, "
                "frist_tage_zugang_bis_wirksamkeit, frist_quellenbeleg FROM rechtsprofile WHERE id = 1"
            )
        ).mappings().one()
        schreiben_row = conn.execute(
            sa.text(
                "SELECT vertrag_id, idempotenzschluessel, zahlungspflicht_ab, komponenten_verteilung "
                "FROM erhoehungsschreiben WHERE id = 1"
            )
        ).mappings().one()

    assert rechtsprofil_row["vertrag_id"] == "V-ALT"
    assert rechtsprofil_row["status"] == "FREIGEGEBEN"
    assert rechtsprofil_row["quelle_hash"] == "altehash123"
    assert rechtsprofil_row["vertrag_beleg_referenz"] == "BELEG-PRODUKTIV-1"
    # Neue Spalten bei einer VOR der Migration bereits vorhandenen Zeile:
    # NULL (kein rückwirkend erfundener Wert für eine unbekannte Frist).
    assert rechtsprofil_row["frist_tage_zugang_bis_wirksamkeit"] is None
    assert rechtsprofil_row["frist_quellenbeleg"] is None

    assert schreiben_row["vertrag_id"] == "V-ALT"
    assert schreiben_row["idempotenzschluessel"] == "V-ALT:mieweg:2026"
    assert schreiben_row["zahlungspflicht_ab"] == "2026-04-15"
    # server_default '{}' greift auch rückwirkend für die Altzeile (NOT NULL
    # ohne erfundenen Fachwert - ein leeres, aber valides JSON-Objekt).
    assert schreiben_row["komponenten_verteilung"] == "{}"


def test_ensure_additive_columns_orm_lesbar_nach_migration(altschema_engine):
    ensure_additive_columns(altschema_engine)
    session_factory = sessionmaker(bind=altschema_engine, future=True, expire_on_commit=False, class_=Session)
    with session_factory() as session:
        profil = session.get(RechtsprofilTable, 1)
        schreiben = session.get(ErhoehungsschreibenTable, 1)

    assert profil is not None
    assert profil.frist_tage_zugang_bis_wirksamkeit is None
    assert profil.frist_quellenbeleg is None
    assert profil.vertrag_beleg_referenz == "BELEG-PRODUKTIV-1"

    assert schreiben is not None
    assert schreiben.komponenten_verteilung == {}
    assert schreiben.idempotenzschluessel == "V-ALT:mieweg:2026"

    # Eine NEUE, über den ORM angelegte Zeile bekommt weiterhin den
    # Python-seitigen default=dict (unverändert für frische Fälle).
    with session_factory() as session:
        neues_profil = session.get(RechtsprofilTable, 1)
        neues_profil.frist_tage_zugang_bis_wirksamkeit = 30
        neues_profil.frist_quellenbeleg = "Vertrag Punkt 7, Jännerklausel"
        session.commit()
    with session_factory() as session:
        aktualisiert = session.get(RechtsprofilTable, 1)
        assert aktualisiert.frist_tage_zugang_bis_wirksamkeit == 30
        assert aktualisiert.frist_quellenbeleg == "Vertrag Punkt 7, Jännerklausel"


def test_ensure_additive_columns_ist_idempotent(altschema_engine):
    erster_lauf = ensure_additive_columns(altschema_engine)
    assert erster_lauf  # beim ersten Lauf gibt es tatsächlich etwas zu tun
    zweiter_lauf = ensure_additive_columns(altschema_engine)
    assert zweiter_lauf == []


def test_ensure_additive_columns_bei_frischer_db_no_op():
    """Eine frisch über `Base.metadata.create_all()` angelegte DB hat
    bereits alle Spalten - hier gibt es nichts nachzuziehen (reine
    Regression gegen den in create_all_tables() üblichen Normalfall)."""

    with tempfile.TemporaryDirectory() as tmp:
        engine = sa.create_engine(f"sqlite:///{tmp}/frisch.db", future=True)
        from mietinkasso.infrastructure.db.base import Base

        Base.metadata.create_all(engine)
        assert ensure_additive_columns(engine) == []
