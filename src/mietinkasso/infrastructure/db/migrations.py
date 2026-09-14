"""Additive Spalten-Migration für bereits produktiv befüllte Datenbanken
(Auftrag HV-20260913-VERSAND-SOLL, Codex-Rückmeldung: "rechtsprofile/
erhoehungsschreiben existieren produktiv bereits ... create_all_tables
ergänzt KEINE Spalten bestehender Tabellen").

`Base.metadata.create_all(engine)` legt NUR fehlende TABELLEN an (z. B.
die neue `index_soll_umsetzungen`-Tabelle) - eine bereits existierende
Tabelle bleibt dabei unverändert, auch wenn ihr SQLAlchemy-Modell
inzwischen neue Spalten hat (hier: `RechtsprofilTable.
frist_tage_zugang_bis_wirksamkeit`/`frist_quellenbeleg`,
`ErhoehungsschreibenTable.komponenten_verteilung`). `ensure_
additive_columns` schließt GENAU diese Lücke: für jede in der DB
bereits existierende Tabelle wird jede im Modell vorhandene, in der DB
aber fehlende Spalte per `ALTER TABLE ... ADD COLUMN ...` nachgezogen.

Grenzen (bewusst, additive-only): ändert/löscht NIEMALS eine bestehende
Spalte oder Zeile - eine bestehende Zeile erhält für eine neu
hinzugefügte Spalte ausschließlich NULL bzw. den im Modell erfassten
`server_default`. Unterstützt NUR einfache, FK-freie Spalten (keine der
bisher tatsächlich nachgezogenen Spalten ist ein Fremdschlüssel) - eine
neue Spalte MIT `ForeignKey` wird übersprungen und stattdessen als
verbleibender Punkt gemeldet, statt eine für SQLite/Postgres
unterschiedlich robuste inline-REFERENCES-Klausel zu riskieren. Legt
NIE eine fehlende Tabelle selbst an (das bleibt `create_all_tables`)."""

from __future__ import annotations

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateColumn

from mietinkasso.infrastructure.db.base import Base


def _sqlite_engine_mit_echter_ddl_transaktion(url) -> Engine:
    """Pysqlite committet DDL-Anweisungen (`ALTER`/`CREATE`/`DROP TABLE`)
    unter dem STANDARD-`isolation_level` implizit VOR jeder Ausführung -
    eine `engine.begin()`-Transaktion auf einer gewöhnlichen Engine
    schützt DDL deshalb NICHT wirklich vor einem Teilfehler (unabhängige
    Abnahme Codex, echter Bug: ein fehlgeschlagenes `INSERT` NACH
    `RENAME`+`CREATE TABLE` ließ beide DDL-Änderungen bereits committet
    zurück - GENAU die "leere neue Tabelle + verwaiste Alttabelle"-
    Situation, die `ensure_mahnkosten_lauf_unique_key` ausschließen
    muss). Mirrors `sqlite_write_lock.py::_neue_begin_immediate_engine` -
    eine EIGENE, kurzlebige Engine zur SELBEN Datei mit abgeschaltetem
    implizitem Commit UND explizitem `BEGIN IMMEDIATE`, sodass
    `engine.begin()` DDL tatsächlich zurückrollt. Nur für Datei-SQLite
    relevant: eine bereits FRISCH über `create_all` angelegte `:memory:`-
    Datenbank hat nie ein migrationsbedürftiges Altschema und erreicht
    diesen Pfad praktisch nie."""

    ddl_engine = create_engine(url, future=True, connect_args={"check_same_thread": False})

    @event.listens_for(ddl_engine, "connect")
    def _kein_impliziter_commit(dbapi_connection, connection_record):  # noqa: ARG001
        dbapi_connection.isolation_level = None

    @event.listens_for(ddl_engine, "begin")
    def _begin_immediate(connection):
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    return ddl_engine


def ensure_additive_columns(engine: Engine) -> list[str]:
    """Idempotent: ein bereits vollständig migrierter Stand führt zu
    KEINER weiteren Änderung (jede bereits vorhandene Spalte wird
    übersprungen). Gibt die Liste der tatsächlich ausgeführten
    `ALTER TABLE`-Anweisungen zurück (leer, wenn nichts zu tun war) -
    ausschließlich für Betriebsprotokoll/Tests, kein Fachrückgabewert."""

    inspector = inspect(engine)
    vorhandene_tabellen = set(inspector.get_table_names())
    ausgefuehrt: list[str] = []
    uebersprungen_fk: list[str] = []

    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if table.name not in vorhandene_tabellen:
                continue  # von create_all_tables() bereits vollständig neu angelegt
            vorhandene_spalten = {spalte["name"] for spalte in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in vorhandene_spalten:
                    continue
                if column.foreign_keys:
                    uebersprungen_fk.append(f"{table.name}.{column.name}")
                    continue
                ddl_spalte = CreateColumn(column).compile(dialect=connection.dialect)
                anweisung = f"ALTER TABLE {table.name} ADD COLUMN {ddl_spalte}"
                connection.execute(text(anweisung))
                ausgefuehrt.append(anweisung)

    if uebersprungen_fk:
        raise RuntimeError(
            "ensure_additive_columns: neue Fremdschlüsselspalte(n) ohne unterstützte additive Migration "
            f"gefunden ({', '.join(uebersprungen_fk)}) - bitte manuell klären, keine automatische "
            "inline-REFERENCES-Ergänzung."
        )

    return ausgefuehrt


def ensure_mahnkosten_lauf_unique_key(engine: Engine) -> bool:
    """Ersetzt eine zu grob geratene ältere Eindeutigkeit auf
    `mahnkosten_buchungen` (`vertrag_id, stufe, zins_bis`) durch die
    korrekte, an den tatsächlich eingefrorenen Mahnlauf gebundene
    Eindeutigkeit (`vertrag_id, stufe, mahnlauf_schluessel`) -
    unabhängige Rückprüfung Codex 14.09.2026, echter Bug: zwei
    DISJUNKTE Gruppen desselben Vertrags/derselben Stufe am selben
    Kalendertag (identisches `zins_bis`) wurden mit der alten
    Eindeutigkeit fälschlich als derselbe Vorgang behandelt - der
    `IntegrityError`-Pfad in `kosten_service.py::MahnkostenService.
    buche_vorschau` gab dann den FREMDEN Ledger der jeweils ANDEREN
    Gruppe als vermeintlich eigenen Kostenbeleg zurück.

    Reine Struktur-/Constraint-Migration (keine fehlende Spalte) - liegt
    deshalb bewusst NICHT in `ensure_additive_columns` (das ist
    ausschließlich für fehlende SPALTEN gedacht, siehe dortige
    Moduldoc, die NIE Constraints bestehender Spalten ändert).
    Idempotent: eine bereits korrekt migrierte oder noch gar nicht
    angelegte Tabelle bleibt unverändert (`create_all_tables` legt eine
    fehlende Tabelle ohnehin gleich mit der korrekten Eindeutigkeit an).

    `mahnkosten_buchungen` ist eine Tabelle DIESES Auftrags (Markus
    13.09.2026), die im unveränderten Produktionsvorfahren `b70a20c`
    noch gar nicht existiert - ein bereits echt produktiv befülltes
    Vorkommen mit der alten Eindeutigkeit ist nach aktuellem
    Kenntnisstand nicht zu erwarten. Die SQLite-Variante (Rebuild per
    Tabellenkopie, da SQLite kein `ALTER TABLE ... DROP CONSTRAINT`
    kennt) deckt eine synthetische Testfixture UND ein versehentlich
    bereits einmal angelegtes Entwicklungsschema gleichermaßen ab; die
    Postgres-Variante nutzt das dort verfügbare direkte
    `DROP CONSTRAINT`/`ADD CONSTRAINT`."""

    inspector = inspect(engine)
    if "mahnkosten_buchungen" not in inspector.get_table_names():
        return False

    ziel_spalten = {"vertrag_id", "stufe", "mahnlauf_schluessel"}
    alte_spalten = {"vertrag_id", "stufe", "zins_bis"}
    bestehende = inspector.get_unique_constraints("mahnkosten_buchungen")
    if any(set(uc["column_names"]) == ziel_spalten for uc in bestehende):
        return False  # bereits korrekt migriert

    zieltabelle = Base.metadata.tables["mahnkosten_buchungen"]
    # Tabellen, die EINEN Fremdschlüssel auf "mahnkosten_buchungen"
    # halten (aktuell nur `mahnkosten_gebuehren.mahnkosten_buchung_id`,
    # aber bewusst dynamisch ermittelt statt hartkodiert, falls künftig
    # weitere Kindtabellen hinzukommen) - GENAU diese Referenzen dürfen
    # der Rebuild niemals verwaisen lassen (siehe unten).
    kind_tabellen = [
        t for t in inspector.get_table_names()
        if any(fk.get("referred_table") == "mahnkosten_buchungen" for fk in inspector.get_foreign_keys(t))
    ]

    if engine.dialect.name == "sqlite":
        # Reihenfolge unabhängige Abnahme Codex 14.09.2026, echter Bug
        # (reproduziert mit befüllter `mahnkosten_gebuehren`-Kindzeile,
        # `mahnkosten_gebuehren.mahnkosten_buchung_id -> mahnkosten_
        # buchungen.id`): die FRÜHERE Fassung dieser Migration benannte
        # zuerst die ORIGINAL-Tabelle um (`RENAME TO ..._vor_migration`)
        # und legte danach die neue Tabelle unter dem ORIGINAL-Namen an.
        # SQLite schreibt bei `ALTER TABLE ... RENAME` jedoch automatisch
        # JEDE Fremdschlüsseldefinition ANDERER Tabellen, die auf die
        # umbenannte Tabelle verweisen, auf deren NEUEN (temporären) Namen
        # um (Standardverhalten seit SQLite 3.25, `legacy_alter_table`
        # ist hier nicht gesetzt) - die Kindzeile in `mahnkosten_
        # gebuehren` zeigte danach fälschlich auf `mahnkosten_buchungen__
        # vor_migration`, die anschließend gedroppt wurde:
        # `PRAGMA foreign_key_check` meldete genau diese verwaiste
        # Referenz. Offizielle sichere Reihenfolge (https://www.sqlite.
        # org/lang_altertable.html, Abschnitt 7 "Making Other Kinds Of
        # Table Schema Changes", Schritte 4-7): NEUE Tabelle unter einem
        # TEMPORÄREN Namen anlegen, Daten kopieren, die ALTE (Original-
        # benannte) Tabelle DROPPEN (nicht umbenennen), dann die neue
        # Tabelle AUF den Original-Namen umbenennen. Die ORIGINAL-Tabelle
        # wird dabei NIE umbenannt, also schreibt SQLite auch NIE eine
        # Kindtabellen-Fremdschlüsseldefinition um - sie verweist die
        # ganze Zeit unverändert auf den Namen "mahnkosten_buchungen".
        spaltennamen = ", ".join(c.name for c in zieltabelle.columns)
        temp_name = "mahnkosten_buchungen__migriert_neu"
        # `to_metadata` MUSS auf `Base.metadata` selbst zielen (nicht auf
        # eine leere `MetaData()`) - `zieltabelle` trägt einen echten
        # Fremdschlüssel auf `vertraege.id`, dessen Auflösung beim
        # Kopieren die referenzierte Tabelle im ZIEL-Metadata-Objekt
        # verlangt. Der temporäre Tabellen-Klon wird deshalb im
        # `finally` wieder aus `Base.metadata` entfernt - er soll die
        # gemeinsame, langlebige Registry nie dauerhaft verunreinigen
        # (u. a. damit ein zweiter Aufruf, z. B. in Tests, nicht an
        # einer bereits registrierten gleichnamigen Tabelle scheitert).
        temp_table = zieltabelle.to_metadata(Base.metadata, name=temp_name)
        # KEIN `engine.begin()` auf der übergebenen Engine - unabhängige
        # Abnahme, echter Bug: pysqlite committet unter dem Standard-
        # `isolation_level` jede DDL-Anweisung (CREATE/DROP/RENAME TABLE)
        # implizit VOR ihrer Ausführung, eine gewöhnliche `engine.begin()`-
        # Transaktion rollt so etwas NICHT zurück - ein fehlgeschlagenes
        # `INSERT` danach ließ CREATE bereits committet zurück (leere neue
        # Tabelle neben einer dann fehlenden/verwaisten Alttabelle).
        # `_sqlite_engine_mit_echter_ddl_transaktion` erzwingt ECHTES
        # transaktionales DDL für GENAU diesen Rebuild.
        ddl_engine = _sqlite_engine_mit_echter_ddl_transaktion(engine.url)
        try:
            with ddl_engine.begin() as connection:
                temp_table.create(connection)
                connection.execute(text(
                    f"INSERT INTO {temp_name} ({spaltennamen}) "
                    f"SELECT {spaltennamen} FROM mahnkosten_buchungen"
                ))
                connection.execute(text("DROP TABLE mahnkosten_buchungen"))
                connection.execute(text(f"ALTER TABLE {temp_name} RENAME TO mahnkosten_buchungen"))
                # Schritt 8 der offiziellen Reihenfolge: Indizes unter dem
                # TEMPORÄREN Namen (von `temp_table.create()` automatisch
                # mit eigenem, vom temporären Tabellennamen abgeleiteten
                # Namen mitangelegt) entfernen und mit dem ursprünglichen,
                # kanonischen Namen auf der jetzt umbenannten Tabelle neu
                # anlegen - rein kosmetisch für die Namensgleichheit mit
                # dem ORM-Modell, funktional bereits durch die Umbenennung
                # korrekt zugeordnet.
                for idx in temp_table.indexes:
                    connection.execute(text(f"DROP INDEX IF EXISTS {idx.name}"))
                for idx in zieltabelle.indexes:
                    idx.create(connection)
                # Schritt 10 der offiziellen Reihenfolge: VOR dem Commit
                # verifizieren, dass der Rebuild KEINE Fremdschlüssel-
                # Referenz EINER KINDTABELLE auf "mahnkosten_buchungen"
                # verwaist zurücklässt - schlägt fehl, rollt die gesamte
                # Transaktion (inkl. DROP TABLE) zurück, statt eine
                # inkonsistente Datenbank zu committen. Bewusst NUR auf
                # die tatsächlichen Kindtabellen DIESES Rebuilds
                # beschränkt (nicht ein pauschaler `PRAGMA foreign_key_
                # check` über die GESAMTE Datenbank) - ein von diesem
                # Rebuild unabhängiger, bereits VORHER bestehender
                # Fremdschlüssel-Defekt anderswo (z. B. eine unvollständig
                # aufgebaute synthetische Testdatenbank ohne `vertraege`-
                # Tabelle) ist NICHT das, was diese Migration verifizieren
                # soll oder darf blockieren.
                for kind in kind_tabellen:
                    verletzungen = connection.execute(text(f"PRAGMA foreign_key_check({kind})")).fetchall()
                    if verletzungen:
                        raise RuntimeError(
                            f"ensure_mahnkosten_lauf_unique_key: Rebuild hinterlässt verwaiste "
                            f"Fremdschlüssel-Referenzen von {kind!r}: {verletzungen!r}"
                        )
                # Alles bisher Ausgeführte (CREATE/INSERT/DROP/RENAME/
                # Index-Rekonstruktion/foreign_key_check) läuft in DIESER
                # EINEN, echten DDL-Transaktion auf DERSELBEN Connection -
                # ein Fehler IRGENDWO in diesem Block (z. B. ein
                # abweichendes Spaltenschema, das das INSERT...SELECT
                # scheitern lässt, oder eine verwaiste Fremdschlüssel-
                # Referenz) rollt jetzt TATSÄCHLICH alles gemeinsam zurück.
                # Es bleibt dadurch NIE eine leere neue Tabelle neben einer
                # fehlenden/inkonsistenten Alttabelle zurück - entweder
                # gelingt der gesamte Rebuild, oder die Original-Tabelle
                # ist unverändert unter ihrem ursprünglichen Namen mit
                # intakten Fremdschlüsseln vorhanden.
        finally:
            ddl_engine.dispose()
            Base.metadata.remove(temp_table)
        return True

    alte_namen = [uc["name"] for uc in bestehende if set(uc["column_names"]) == alte_spalten and uc["name"]]
    with engine.begin() as connection:
        for name in alte_namen:
            connection.execute(text(f"ALTER TABLE mahnkosten_buchungen DROP CONSTRAINT {name}"))
        connection.execute(text(
            "ALTER TABLE mahnkosten_buchungen ADD CONSTRAINT uq_mahnkosten_lauf "
            "UNIQUE (vertrag_id, stufe, mahnlauf_schluessel)"
        ))
    return True


def ensure_mahnkosten_gebuehr_status_backfill(engine: Engine) -> bool:
    """Backfill für `mahnkosten_gebuehren.status` (unabhängige Rückprüfung
    Codex 14.09.2026, echter Bug, reproduziert an einem vollständigen
    Alt-ORM-Upgrade mit einer bereits befüllten Gebühren-Kindzeile):
    `ensure_additive_columns` zieht die neue Spalte `status` für eine
    bereits VOR dem zweistufigen RESERVIERT/GEBUCHT-Lebenszyklus (siehe
    `MahnkostenGebuehrTable`-Moduldoc) über die ALTE, einstufige
    `gebuehr_erheben()` tatsächlich abgeschlossen gebuchte Zeile
    ausschließlich mit ihrem `server_default('RESERVIERT')` nach - eine
    solche Altzeile bliebe damit fälschlich für immer im Zustand "noch
    nicht bestätigt", obwohl sie bereits mit gesetztem `mahnkosten_
    buchung_id`/`gebuehr_op_position_id` vollständig gebucht ist.

    Setzt GENAU die Zeilen, die BEIDE Buchungsreferenzen bereits gesetzt
    haben, von `RESERVIERT` auf `GEBUCHT` zurück. Eine ECHTE neue
    Reservierung (angelegt über `MahnkostenRepository.reserviere_
    gebuehr`, IMMER mit `reserviert_fuer_mahnlauf_id` gesetzt und OHNE
    Buchungsreferenzen, bis `finalisiere_reservierte_gebuehr` sie nach
    bestätigtem Versand abschließt) erfüllt diese Bedingung nie und
    bleibt unberührt.

    Idempotent: ein bereits korrekter Stand (keine solche Zeile mehr)
    ändert nichts; eine (noch) fehlende Tabelle/Spalte ist ein No-Op.
    Muss NACH `ensure_additive_columns` laufen (die Spalte muss bereits
    existieren)."""

    inspector = inspect(engine)
    if "mahnkosten_gebuehren" not in inspector.get_table_names():
        return False
    vorhandene_spalten = {s["name"] for s in inspector.get_columns("mahnkosten_gebuehren")}
    if "status" not in vorhandene_spalten:
        return False
    with engine.begin() as connection:
        ergebnis = connection.execute(text(
            "UPDATE mahnkosten_gebuehren SET status = 'GEBUCHT' "
            "WHERE status = 'RESERVIERT' AND mahnkosten_buchung_id IS NOT NULL "
            "AND gebuehr_op_position_id IS NOT NULL"
        ))
    return (ergebnis.rowcount or 0) > 0
