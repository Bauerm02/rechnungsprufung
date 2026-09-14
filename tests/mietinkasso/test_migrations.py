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
from mietinkasso.infrastructure.db.migrations import (
    ensure_additive_columns,
    ensure_mahnkosten_gebuehr_status_backfill,
    ensure_mahnkosten_lauf_unique_key,
)
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


def _altschema_mahnkosten_metadata(*, mit_erstellt_von: bool = True) -> tuple[sa.MetaData, sa.Table]:
    """`mahnkosten_buchungen` WIE VOR der Rückprüfung 14.09.2026 (echter
    Bug, siehe `MahnkostenBuchungTable`-Moduldoc): die Unique-Constraint
    lag auf (`vertrag_id, stufe, zins_bis`) statt auf (`vertrag_id,
    stufe, mahnlauf_schluessel`) - GENAU dieselben Spalten wie das
    aktuelle Modell, nur der Constraint unterscheidet sich.

    `vertrag_id` trägt bewusst `index=True` - unabhängige Abnahme
    2cd09ca, echter Bug: das reale ORM-Modell (`tables.py::
    MahnkostenBuchungTable.vertrag_id`) hat diesen Index tatsächlich
    (`ForeignKey(...), index=True`); ein Rebuild, der diesen Index nicht
    vor dem Anlegen der frischen Tabelle entfernt, scheitert an SQLite
    mit `OperationalError: index ix_mahnkosten_buchungen_vertrag_id
    already exists` - ein früherer Testlauf ohne diesen Index hat genau
    diese reale Kollision NICHT abgebildet.

    `mit_erstellt_von=False` bildet zusätzlich ein (rein synthetisches)
    NOCH älteres Altschema nach, dem eine vom aktuellen Modell als
    NOT NULL erwartete Spalte komplett fehlt - Grundlage für den
    Atomaritäts-Test: das `INSERT INTO ... SELECT ...` schlägt dabei
    zwangsläufig fehl, und genau dieser Fall muss den GESAMTEN Rebuild
    (samt `ALTER TABLE ... RENAME`) rückstandsfrei zurückrollen."""

    meta = sa.MetaData()
    spalten = [
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("vertrag_id", sa.String(64), nullable=False, index=True),
        sa.Column("stufe", sa.Integer, nullable=False),
        sa.Column("mahnlauf_schluessel", sa.String(128), nullable=False),
        sa.Column("forderung_op_position_ids", sa.Text, nullable=False),
        sa.Column("hauptforderung_cent", sa.Integer, nullable=False),
        sa.Column("zinsbasis", sa.String(32), nullable=False),
        sa.Column("zinssatz_prozent", sa.Numeric(6, 3), nullable=False),
        sa.Column("zins_von", sa.Date, nullable=False),
        sa.Column("zins_bis", sa.Date, nullable=False),
        sa.Column("zinsen_cent", sa.Integer, nullable=False),
        sa.Column("gebuehr_cent", sa.Integer, nullable=True),
        sa.Column("rechtsgrundlage_gebuehr", sa.String(256), nullable=True),
        sa.Column("versandnachweis_referenz", sa.String(256), nullable=False),
        sa.Column("zinsen_op_position_id", sa.Integer, nullable=True),
        sa.Column("gebuehr_op_position_id", sa.Integer, nullable=True),
        sa.Column("zins_segmente_json", sa.Text, nullable=False, server_default="[]"),
        sa.Column("zinsen_delta_je_op_json", sa.Text, nullable=False, server_default="{}"),
        sa.Column("gebucht_am", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]
    if mit_erstellt_von:
        spalten.append(sa.Column("erstellt_von", sa.String(128), nullable=False))
    spalten.append(sa.UniqueConstraint("vertrag_id", "stufe", "zins_bis", name="uq_mahnkosten_lauf"))
    tabelle = sa.Table("mahnkosten_buchungen", meta, *spalten)
    return meta, tabelle


@pytest.fixture
def mahnkosten_altschema_engine(tmp_path: Path):
    db_pfad = tmp_path / "altschema_mahnkosten.db"
    engine = sa.create_engine(f"sqlite:///{db_pfad}", future=True)
    meta, tabelle = _altschema_mahnkosten_metadata()
    meta.create_all(engine)

    with engine.begin() as conn:
        conn.execute(
            tabelle.insert().values(
                vertrag_id="V-ALT-MAHNKOSTEN",
                stufe=1,
                mahnlauf_schluessel="ALT-GRUPPE-A",
                forderung_op_position_ids="[1]",
                hauptforderung_cent=50_000,
                zinsbasis="GESETZLICH_ABGB",
                zinssatz_prozent="4.000",
                zins_von=date(2026, 1, 5),
                zins_bis=date(2026, 3, 1),
                zinsen_cent=208,
                gebuehr_cent=None,
                rechtsgrundlage_gebuehr=None,
                versandnachweis_referenz="mahnung:produktiv-alt",
                zinsen_op_position_id=None,
                gebuehr_op_position_id=None,
                erstellt_von="markus",
            )
        )
    yield engine
    engine.dispose()


@pytest.fixture
def mahnkosten_altschema_engine_inkompatibel(tmp_path: Path):
    """Fehlt eine vom aktuellen Modell als NOT NULL erwartete Spalte
    (`erstellt_von`) komplett - das `INSERT INTO ... SELECT ...` des
    Rebuilds MUSS daran scheitern. Grundlage für den Atomaritäts-Test:
    dieser Fehlerfall darf NIE eine leere neue Tabelle neben einer
    verwaisten `..._vor_migration`-Alttabelle zurücklassen."""

    db_pfad = tmp_path / "altschema_mahnkosten_inkompatibel.db"
    engine = sa.create_engine(f"sqlite:///{db_pfad}", future=True)
    meta, tabelle = _altschema_mahnkosten_metadata(mit_erstellt_von=False)
    meta.create_all(engine)

    with engine.begin() as conn:
        conn.execute(
            tabelle.insert().values(
                vertrag_id="V-ALT-MAHNKOSTEN", stufe=1, mahnlauf_schluessel="ALT-GRUPPE-A",
                forderung_op_position_ids="[1]", hauptforderung_cent=50_000, zinsbasis="GESETZLICH_ABGB",
                zinssatz_prozent="4.000", zins_von=date(2026, 1, 5), zins_bis=date(2026, 3, 1),
                zinsen_cent=208, gebuehr_cent=None, rechtsgrundlage_gebuehr=None,
                versandnachweis_referenz="mahnung:produktiv-alt", zinsen_op_position_id=None,
                gebuehr_op_position_id=None,
            )
        )
    yield engine
    engine.dispose()


def test_ensure_mahnkosten_lauf_unique_key_rebuild_ist_atomar_bei_fehler(mahnkosten_altschema_engine_inkompatibel):
    """Unabhängige Abnahme 2cd09ca: ein fehlschlagender Rebuild (hier
    provoziert durch eine fehlende, vom neuen Modell als NOT NULL
    erwartete Spalte) darf NIE eine leere neue Tabelle neben einer
    verwaisten `mahnkosten_buchungen__vor_migration`-Alttabelle
    zurücklassen - der GESAMTE Rebuild (RENAME/DROP INDEX/CREATE/INSERT)
    läuft in EINER Transaktion und rollt bei einem Fehler vollständig
    zurück; die Originaltabelle bleibt unter ihrem ursprünglichen Namen
    mit ihrer ursprünglichen Zeile unverändert erhalten."""

    engine = mahnkosten_altschema_engine_inkompatibel
    with pytest.raises(sa.exc.OperationalError):
        ensure_mahnkosten_lauf_unique_key(engine)

    inspector = sa.inspect(engine)
    tabellen = set(inspector.get_table_names())
    assert "mahnkosten_buchungen" in tabellen
    assert "mahnkosten_buchungen__vor_migration" not in tabellen  # kein verwaister Rest

    with engine.begin() as conn:
        zeile = conn.execute(sa.text(
            "SELECT vertrag_id, mahnlauf_schluessel, zinsen_cent FROM mahnkosten_buchungen WHERE id = 1"
        )).mappings().one()
    assert zeile["vertrag_id"] == "V-ALT-MAHNKOSTEN"
    assert zeile["mahnlauf_schluessel"] == "ALT-GRUPPE-A"
    assert zeile["zinsen_cent"] == 208  # unveränderte Originalzeile, nicht verloren

    # Die alte Eindeutigkeit ist unverändert noch da - der Fehler hat
    # NICHTS am ursprünglichen Zustand verändert.
    constraints = inspector.get_unique_constraints("mahnkosten_buchungen")
    assert any(set(c["column_names"]) == {"vertrag_id", "stufe", "zins_bis"} for c in constraints)


def test_ensure_mahnkosten_lauf_unique_key_migriert_altes_schema(mahnkosten_altschema_engine):
    """Unabhängige Abnahme auf Commit 1328f2d, echter Bug: die alte
    Eindeutigkeit (`vertrag_id, stufe, zins_bis`) verhinderte zwei
    disjunkte Gruppen desselben Vertrags/derselben Stufe am selben
    Stichtag. Nach der Migration greift die korrekte Eindeutigkeit
    (`vertrag_id, stufe, mahnlauf_schluessel`), die bestehende Zeile
    bleibt dabei unverändert erhalten."""

    geaendert = ensure_mahnkosten_lauf_unique_key(mahnkosten_altschema_engine)
    assert geaendert is True

    inspector = sa.inspect(mahnkosten_altschema_engine)
    constraints = inspector.get_unique_constraints("mahnkosten_buchungen")
    assert any(set(c["column_names"]) == {"vertrag_id", "stufe", "mahnlauf_schluessel"} for c in constraints)
    assert not any(set(c["column_names"]) == {"vertrag_id", "stufe", "zins_bis"} for c in constraints)

    with mahnkosten_altschema_engine.begin() as conn:
        alte_zeile = conn.execute(
            sa.text(
                "SELECT vertrag_id, mahnlauf_schluessel, zinsen_cent, erstellt_von "
                "FROM mahnkosten_buchungen WHERE id = 1"
            )
        ).mappings().one()
    assert alte_zeile["vertrag_id"] == "V-ALT-MAHNKOSTEN"
    assert alte_zeile["mahnlauf_schluessel"] == "ALT-GRUPPE-A"
    assert alte_zeile["zinsen_cent"] == 208
    assert alte_zeile["erstellt_von"] == "markus"

    # Der eigentliche Beweis: eine ZWEITE, disjunkte Gruppe desselben
    # Vertrags/derselben Stufe mit DEMSELBEN `zins_bis`-Stichtag darf
    # jetzt erfolgreich eingefügt werden - mit der alten Eindeutigkeit
    # hätte das mit `IntegrityError` fehlgeschlagen.
    with mahnkosten_altschema_engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO mahnkosten_buchungen "
            "(vertrag_id, stufe, mahnlauf_schluessel, forderung_op_position_ids, hauptforderung_cent, "
            " zinsbasis, zinssatz_prozent, zins_von, zins_bis, zinsen_cent, versandnachweis_referenz, erstellt_von) "
            "VALUES ('V-ALT-MAHNKOSTEN', 1, 'ALT-GRUPPE-B', '[2]', 30000, 'GESETZLICH_ABGB', 4.000, "
            " '2026-02-05', '2026-03-01', 150, 'mahnung:produktiv-alt-b', 'markus')"
        ))

    # Gegenprobe: eine ECHTE Wiederholung derselben Gruppe (identischer
    # `mahnlauf_schluessel`) bleibt weiterhin durch die Constraint verhindert.
    with pytest.raises(sa.exc.IntegrityError):
        with mahnkosten_altschema_engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO mahnkosten_buchungen "
                "(vertrag_id, stufe, mahnlauf_schluessel, forderung_op_position_ids, hauptforderung_cent, "
                " zinsbasis, zinssatz_prozent, zins_von, zins_bis, zinsen_cent, versandnachweis_referenz, erstellt_von) "
                "VALUES ('V-ALT-MAHNKOSTEN', 1, 'ALT-GRUPPE-A', '[1]', 50000, 'GESETZLICH_ABGB', 4.000, "
                " '2026-01-05', '2026-03-01', 999, 'mahnung:doppelt', 'markus')"
            ))


def test_ensure_mahnkosten_lauf_unique_key_ist_idempotent(mahnkosten_altschema_engine):
    erster_lauf = ensure_mahnkosten_lauf_unique_key(mahnkosten_altschema_engine)
    assert erster_lauf is True
    zweiter_lauf = ensure_mahnkosten_lauf_unique_key(mahnkosten_altschema_engine)
    assert zweiter_lauf is False


def test_ensure_mahnkosten_lauf_unique_key_bei_frischer_db_no_op():
    with tempfile.TemporaryDirectory() as tmp:
        engine = sa.create_engine(f"sqlite:///{tmp}/frisch_mahnkosten.db", future=True)
        from mietinkasso.infrastructure.db.base import Base

        try:
            Base.metadata.create_all(engine)
            assert ensure_mahnkosten_lauf_unique_key(engine) is False
            inspector = sa.inspect(engine)
            constraints = inspector.get_unique_constraints("mahnkosten_buchungen")
            assert any(set(c["column_names"]) == {"vertrag_id", "stufe", "mahnlauf_schluessel"} for c in constraints)
        finally:
            engine.dispose()


def test_ensure_mahnkosten_lauf_unique_key_ohne_tabelle_no_op():
    with tempfile.TemporaryDirectory() as tmp:
        engine = sa.create_engine(f"sqlite:///{tmp}/leer.db", future=True)
        try:
            assert ensure_mahnkosten_lauf_unique_key(engine) is False
        finally:
            engine.dispose()


def _altschema_mahnkosten_gebuehren_metadata(meta: sa.MetaData) -> sa.Table:
    """`mahnkosten_gebuehren` WIE VOR der Rückprüfung 14.09.2026 (echter
    Bug) - noch OHNE die Spalten `status`/`reserviert_fuer_mahnlauf_id`
    des zweistufigen RESERVIERT/GEBUCHT-Lebenszyklus (siehe
    `MahnkostenGebuehrTable`-Moduldoc), aber bereits mit dem echten
    Fremdschlüssel auf `mahnkosten_buchungen.id` - Grundlage für BEIDE
    unabhängig gemeldeten Bugs: den FK-Rebuild-Fehler UND das falsche
    Status-Backfill einer bereits abgeschlossenen Altzeile."""

    return sa.Table(
        "mahnkosten_gebuehren",
        meta,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("vertrag_id", sa.String(64), nullable=False, index=True),
        sa.Column("entgeltforderung_schluessel", sa.String(200), nullable=False),
        sa.Column("betrag_cent", sa.Integer, nullable=False),
        sa.Column("rechtsgrundlage", sa.String(256), nullable=False),
        sa.Column("mahnkosten_buchung_id", sa.Integer, sa.ForeignKey("mahnkosten_buchungen.id"), nullable=True),
        sa.Column("gebuehr_op_position_id", sa.Integer, nullable=True),
        sa.Column("erhoben_am", sa.DateTime(timezone=True), nullable=False),
        sa.Column("erstellt_von", sa.String(128), nullable=False),
        sa.UniqueConstraint("vertrag_id", "entgeltforderung_schluessel", name="uq_mahnkosten_gebuehr_forderung"),
    )


@pytest.fixture
def mahnkosten_voller_altschema_upgrade_engine(tmp_path: Path):
    """Echter vollständiger Alt-ORM-Upgrade (unabhängige Abnahme Codex
    14.09.2026, Commit 7376ab8): `mahnkosten_buchungen` im ALTEN Schema
    (grobe Eindeutigkeit `vertrag_id, stufe, zins_bis`, siehe
    `_altschema_mahnkosten_metadata`) UND `mahnkosten_gebuehren` im
    ALTEN Schema (ohne `status`/`reserviert_fuer_mahnlauf_id`), mit
    einer bereits über die alte, einstufige `gebuehr_erheben()`
    tatsächlich abgeschlossenen Gebühren-Kindzeile (`mahnkosten_
    buchung_id`+`gebuehr_op_position_id` beide gesetzt) - genau der vom
    Nutzer unabhängig reproduzierte Fall."""

    db_pfad = tmp_path / "altschema_mahnkosten_voll.db"
    engine = sa.create_engine(f"sqlite:///{db_pfad}", future=True)
    meta, buchungen = _altschema_mahnkosten_metadata()
    gebuehren = _altschema_mahnkosten_gebuehren_metadata(meta)
    meta.create_all(engine)

    jetzt = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    with engine.begin() as conn:
        conn.execute(
            buchungen.insert().values(
                vertrag_id="V-ALT-MAHNKOSTEN", stufe=1, mahnlauf_schluessel="ALT-GRUPPE-A",
                forderung_op_position_ids="[1]", hauptforderung_cent=50_000, zinsbasis="GESETZLICH_ABGB",
                zinssatz_prozent="4.000", zins_von=date(2026, 1, 5), zins_bis=date(2026, 3, 1),
                zinsen_cent=208, gebuehr_cent=4000, rechtsgrundlage_gebuehr="§458 UGB",
                versandnachweis_referenz="mahnung:produktiv-alt", zinsen_op_position_id=None,
                gebuehr_op_position_id=1, erstellt_von="markus",
            )
        )
        conn.execute(
            gebuehren.insert().values(
                vertrag_id="V-ALT-MAHNKOSTEN", entgeltforderung_schluessel="V-ALT-MAHNKOSTEN:2026-01",
                betrag_cent=4000, rechtsgrundlage="§458 UGB",
                mahnkosten_buchung_id=1, gebuehr_op_position_id=1,
                erhoben_am=jetzt, erstellt_von="markus",
            )
        )
    yield engine
    engine.dispose()


def test_ensure_mahnkosten_lauf_unique_key_erhaelt_fremdschluessel_der_gebuehren_kindtabelle(
    mahnkosten_voller_altschema_upgrade_engine,
):
    """Unabhängige Abnahme Codex auf Commit 7376ab8, echter Bug: der
    frühere Rebuild benannte die ORIGINAL-Tabelle zuerst um (`RENAME TO
    ..._vor_migration`); SQLite schreibt dabei automatisch JEDE
    Fremdschlüsseldefinition ANDERER Tabellen, die auf sie verweisen
    (hier `mahnkosten_gebuehren.mahnkosten_buchung_id`), auf den neuen
    (temporären) Namen um - nach dem `DROP TABLE ..._vor_migration` war
    die Kindzeile mit einer Referenz auf eine nicht mehr existierende
    Tabelle verwaist (`pragma foreign_key_check =>
    [(mahnkosten_gebuehren,1,mahnkosten_buchungen__vor_migration,0)]`).
    Nach dem Fix (neue Tabelle unter Temp-Namen, Original DROPPEN statt
    umbenennen, dann neue Tabelle auf den Original-Namen umbenennen)
    bleibt die Fremdschlüssel-Referenz durchgängig intakt."""

    engine = mahnkosten_voller_altschema_upgrade_engine
    geaendert = ensure_mahnkosten_lauf_unique_key(engine)
    assert geaendert is True

    with engine.begin() as conn:
        # Bewusst NUR die Kindtabelle dieses Rebuilds geprüft, nicht die
        # gesamte (in dieser Fixture bewusst minimalen, ohne `vertraege`
        # aufgebauten) synthetischen Test-DB - siehe Migrations-Docstring.
        verletzungen = conn.execute(sa.text("PRAGMA foreign_key_check(mahnkosten_gebuehren)")).fetchall()
        assert verletzungen == []

        gebuehr_zeile = conn.execute(sa.text(
            "SELECT g.mahnkosten_buchung_id, b.vertrag_id, b.mahnlauf_schluessel "
            "FROM mahnkosten_gebuehren g JOIN mahnkosten_buchungen b "
            "ON g.mahnkosten_buchung_id = b.id WHERE g.id = 1"
        )).mappings().one()
    assert gebuehr_zeile["vertrag_id"] == "V-ALT-MAHNKOSTEN"
    assert gebuehr_zeile["mahnlauf_schluessel"] == "ALT-GRUPPE-A"

    inspector = sa.inspect(engine)
    constraints = inspector.get_unique_constraints("mahnkosten_buchungen")
    assert any(set(c["column_names"]) == {"vertrag_id", "stufe", "mahnlauf_schluessel"} for c in constraints)


def test_ensure_mahnkosten_gebuehr_status_backfill_korrigiert_bereits_gebuchte_altzeilen(
    mahnkosten_voller_altschema_upgrade_engine,
):
    """Unabhängige Abnahme Codex auf Commit 7376ab8, echter Bug:
    `ensure_additive_columns` zieht `status`/`reserviert_fuer_mahnlauf_
    id` additiv nach; die bereits VOR dem zweistufigen Lebenszyklus über
    die alte `gebuehr_erheben()` abgeschlossen gebuchte Zeile (mit
    gesetztem `mahnkosten_buchung_id`+`gebuehr_op_position_id`) bekäme
    dabei ausschließlich den `server_default('RESERVIERT')` - obwohl sie
    tatsächlich längst GEBUCHT ist. Das Backfill korrigiert GENAU diese
    Zeilen; eine (hier zusätzlich angelegte) echte NEUE, noch offene
    Reservierung ohne Buchungsreferenzen bleibt unverändert RESERVIERT."""

    engine = mahnkosten_voller_altschema_upgrade_engine
    ausgefuehrt = ensure_additive_columns(engine)
    assert any("mahnkosten_gebuehren" in a and "status" in a for a in ausgefuehrt)

    with engine.begin() as conn:
        vor_backfill = conn.execute(sa.text("SELECT status FROM mahnkosten_gebuehren WHERE id = 1")).scalar_one()
    assert vor_backfill == "RESERVIERT"  # der Bug: server_default trifft auch die bereits gebuchte Altzeile

    # Eine echte neue, noch offene Reservierung (kein Bug-Kandidat) zum
    # Gegenprobe-Vergleich anlegen.
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO mahnkosten_gebuehren "
            "(vertrag_id, entgeltforderung_schluessel, betrag_cent, rechtsgrundlage, "
            " mahnkosten_buchung_id, gebuehr_op_position_id, status, reserviert_fuer_mahnlauf_id, "
            " erhoben_am, erstellt_von) "
            "VALUES ('V-ALT-MAHNKOSTEN', 'V-ALT-MAHNKOSTEN:2026-02', 4000, '§458 UGB', "
            " NULL, NULL, 'RESERVIERT', 99, '2026-09-01 12:00:00', 'markus')"
        ))

    geaendert = ensure_mahnkosten_gebuehr_status_backfill(engine)
    assert geaendert is True

    with engine.begin() as conn:
        zeilen = {
            row["id"]: row["status"]
            for row in conn.execute(sa.text("SELECT id, status FROM mahnkosten_gebuehren")).mappings()
        }
    assert zeilen[1] == "GEBUCHT"  # bereits gebuchte Altzeile korrigiert
    assert zeilen[2] == "RESERVIERT"  # echte offene Reservierung bleibt unberührt

    # Idempotent: ein zweiter Lauf ändert nichts mehr.
    assert ensure_mahnkosten_gebuehr_status_backfill(engine) is False


def test_ensure_additive_columns_bei_frischer_db_no_op():
    """Eine frisch über `Base.metadata.create_all()` angelegte DB hat
    bereits alle Spalten - hier gibt es nichts nachzuziehen (reine
    Regression gegen den in create_all_tables() üblichen Normalfall)."""

    with tempfile.TemporaryDirectory() as tmp:
        engine = sa.create_engine(f"sqlite:///{tmp}/frisch.db", future=True)
        from mietinkasso.infrastructure.db.base import Base

        try:
            Base.metadata.create_all(engine)
            assert ensure_additive_columns(engine) == []
        finally:
            # Ohne dispose() bleibt die Datei-Handle unter Windows offen und
            # blockiert TemporaryDirectory beim Aufräumen (PermissionError).
            engine.dispose()
