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

    if engine.dialect.name == "sqlite":
        # Echte, per `CREATE INDEX` realisierte Indizes (z. B. aus
        # `index=True`, wie `vertrag_id`) VOR dem Rebuild einsammeln -
        # unabhängige Abnahme 2cd09ca, echter Bug: SQLite benennt einen
        # Index global (nicht je Tabelle) und `ALTER TABLE ... RENAME`
        # lässt den Index unter seinem ALTEN Namen an der umbenannten
        # Tabelle hängen. Ohne explizites Löschen kollidiert
        # `zieltabelle.create()` gleich beim Anlegen der frischen Tabelle
        # mit `OperationalError: index ... already exists`, weil
        # SQLAlchemy für die (unveränderte) `vertrag_id`-Spalte denselben
        # Indexnamen neu vergeben will. `get_indexes()` liefert bewusst
        # NUR echte benannte Indizes, keine impliziten
        # `sqlite_autoindex_*` einer UNIQUE-Constraint (die werden separat
        # über `get_unique_constraints()` behandelt und beim Rebuild der
        # Tabelle ohnehin automatisch durch die NEUE Constraint ersetzt).
        alte_indexe = [i["name"] for i in inspector.get_indexes("mahnkosten_buchungen") if i.get("name")]
        spaltennamen = ", ".join(c.name for c in zieltabelle.columns)
        # KEIN `engine.begin()` auf der übergebenen Engine - unabhängige
        # Abnahme, echter Bug: pysqlite committet unter dem Standard-
        # `isolation_level` jede DDL-Anweisung (RENAME/CREATE/DROP TABLE)
        # implizit VOR ihrer Ausführung, eine gewöhnliche `engine.begin()`-
        # Transaktion rollt so etwas NICHT zurück - ein fehlgeschlagenes
        # `INSERT` danach ließ RENAME+CREATE bereits committet zurück
        # (leere neue Tabelle + verwaiste `..._vor_migration`-Alttabelle).
        # `_sqlite_engine_mit_echter_ddl_transaktion` erzwingt ECHTES
        # transaktionales DDL für GENAU diesen Rebuild.
        ddl_engine = _sqlite_engine_mit_echter_ddl_transaktion(engine.url)
        try:
            with ddl_engine.begin() as connection:
                connection.execute(text("ALTER TABLE mahnkosten_buchungen RENAME TO mahnkosten_buchungen__vor_migration"))
                for index_name in alte_indexe:
                    connection.execute(text(f"DROP INDEX IF EXISTS {index_name}"))
                zieltabelle.create(connection)
                connection.execute(text(
                    f"INSERT INTO mahnkosten_buchungen ({spaltennamen}) "
                    f"SELECT {spaltennamen} FROM mahnkosten_buchungen__vor_migration"
                ))
                connection.execute(text("DROP TABLE mahnkosten_buchungen__vor_migration"))
                # Alles bisher Ausgeführte (RENAME/DROP INDEX/CREATE/INSERT/
                # DROP TABLE) läuft in DIESER EINEN, echten DDL-Transaktion
                # auf DERSELBEN Connection - ein Fehler IRGENDWO in diesem
                # Block (z. B. ein abweichendes Spaltenschema, das das
                # INSERT...SELECT scheitern lässt) rollt jetzt TATSÄCHLICH
                # alles gemeinsam zurück. Es bleibt dadurch NIE eine leere
                # neue Tabelle neben einer verwaisten `..._vor_migration`-
                # Alttabelle zurück - entweder gelingt der gesamte Rebuild,
                # oder die Original-Tabelle ist unverändert unter ihrem
                # ursprünglichen Namen vorhanden.
        finally:
            ddl_engine.dispose()
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
