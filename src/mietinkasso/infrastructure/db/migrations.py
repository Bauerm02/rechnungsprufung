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

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateColumn

from mietinkasso.infrastructure.db.base import Base


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
