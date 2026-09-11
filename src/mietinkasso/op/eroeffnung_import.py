"""CSV-Import für die Eröffnung eines Kontos (Fachregel 2).

Zwei Modi, die sich gegenseitig ausschließen (siehe
`op.service.OPService._pruefe_und_setze_eroeffnungsmodus`):

- ``GESAMTSALDO``: eine Zeile pro Konto mit dem bestätigten Gesamtsaldo
  zum Stichtag (z. B. aus den Deb-/Kred-/Sachkontensalden).
- ``EINZEL_OP``: mehrere Zeilen je Konto, eine je offenem Posten, mit
  eigenem Belegdatum/Fälligkeit (z. B. aus dem Journal).

Beide Modi tragen eine `import_id`-Spalte für die Idempotenz-Garantie;
fehlt sie, wird sie deterministisch aus dem Zeileninhalt abgeleitet.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.auth.service import AuthContext
from mietinkasso.domain.enums import OPTyp
from mietinkasso.domain.money import to_cents
from mietinkasso.infrastructure.db.tables import KontoTable, OPPositionTable
from mietinkasso.op.service import OPService


@dataclass(frozen=True)
class EroeffnungCsvZeile:
    konto_id: str
    modus: str
    betrag_cent: int
    stichtag: date
    typ: str | None
    belegdatum: date | None
    faelligkeit: date | None
    beleg_referenz: str
    import_id: str


def _parse_date(value: str) -> date:
    return datetime.strptime(value.strip(), "%Y-%m-%d").date()


def parse_eroeffnung_csv(text: str) -> list[EroeffnungCsvZeile]:
    zeilen: list[EroeffnungCsvZeile] = []
    reader = csv.DictReader(io.StringIO(text))
    for nummer, roh in enumerate(reader, start=1):
        modus = roh["modus"].strip().upper()
        # Decimal/Integer, niemals Float (Fließkomma-Rundungsfehler bei
        # Geldbeträgen sind kein akzeptables Risiko).
        betrag_cent = to_cents(roh["betrag"].strip().replace(",", "."))
        stichtag = _parse_date(roh["stichtag"])
        belegdatum = _parse_date(roh["belegdatum"]) if roh.get("belegdatum") else None
        faelligkeit = _parse_date(roh["faelligkeit"]) if roh.get("faelligkeit") else None
        import_id = roh.get("import_id", "").strip()
        if not import_id:
            fingerprint = hashlib.sha256(
                f"{roh['konto_id']}:{modus}:{betrag_cent}:{stichtag}:{belegdatum}:{nummer}".encode("utf-8")
            ).hexdigest()[:16]
            import_id = f"EROEFFNUNG-CSV:{fingerprint}"
        zeilen.append(
            EroeffnungCsvZeile(
                konto_id=roh["konto_id"].strip(),
                modus=modus,
                betrag_cent=betrag_cent,
                stichtag=stichtag,
                typ=roh.get("typ", "").strip() or None,
                belegdatum=belegdatum,
                faelligkeit=faelligkeit,
                beleg_referenz=roh.get("beleg_referenz", "").strip() or "Eröffnungsimport",
                import_id=import_id,
            )
        )
    return zeilen


def importiere_eroeffnung_csv(
    *,
    ctx: AuthContext,
    op_service: OPService,
    text: str,
    konten_je_id: dict[str, KontoTable],
    akteur: str,
) -> list[OPPositionTable]:
    ergebnisse: list[OPPositionTable] = []
    for zeile in parse_eroeffnung_csv(text):
        konto = konten_je_id[zeile.konto_id]
        if zeile.modus == "GESAMTSALDO":
            ergebnisse.append(
                op_service.eroeffnen_gesamtsaldo(
                    ctx=ctx, konto=konto, betrag_cent=zeile.betrag_cent, stichtag=zeile.stichtag,
                    import_id=zeile.import_id, akteur=akteur,
                )
            )
        elif zeile.modus == "EINZEL_OP":
            ergebnisse.append(
                op_service.eroeffnen_einzel_op(
                    ctx=ctx, konto=konto, stichtag=zeile.stichtag, import_id=zeile.import_id,
                    typ=OPTyp(zeile.typ or "SOLL"), betrag_cent=zeile.betrag_cent,
                    belegdatum=zeile.belegdatum or zeile.stichtag, faelligkeit=zeile.faelligkeit,
                    beleg_referenz=zeile.beleg_referenz, akteur=akteur,
                )
            )
        else:
            raise ValueError(f"Unbekannter Eröffnungsmodus '{zeile.modus}' (erwartet GESAMTSALDO oder EINZEL_OP).")
    return ergebnisse


def importiere_eroeffnung_csv_atomar(
    *,
    ctx: AuthContext,
    op_service: OPService,
    text: str,
    konten_je_id: dict[str, KontoTable],
    akteur: str,
    session_factory: sessionmaker[Session],
) -> list[OPPositionTable]:
    """Wie `importiere_eroeffnung_csv`, aber die GESAMTE Datei läuft in
    EINER DB-Transaktion (analog zu `BankImportService._importiere_atomar`):
    scheitert eine Zeile (unbekanntes Konto, Objektausschluss,
    widersprüchliche Wiedereröffnung, ...), wird der GESAMTE Aufruf
    zurückgerollt - keine Zeile aus dieser Datei bleibt teilweise gebucht
    stehen. Ein erneuter, korrigierter Lauf ist dank Idempotenz
    (import_id) immer gefahrlos."""

    zeilen = parse_eroeffnung_csv(text)
    # `OPService._pruefe_und_setze_eroeffnungsmodus` mutiert das übergebene
    # KontoTable-Python-Objekt SOFORT in-memory (nötig, damit ein Konflikt
    # zwischen zwei Zeilen DERSELBEN Datei für DASSELBE Konto erkannt wird,
    # bevor überhaupt committet ist). Ein Rollback der DB darf diese
    # In-Memory-Mutation nicht überleben lassen - sonst zeigt das
    # aufrufende Objekt einen Eröffnungsmodus, der in der DB nie existiert
    # hat. Zustand vor dem Lauf sichern und bei jedem Fehler zurücksetzen.
    urspruenglicher_zustand = {
        konto_id: (konto.eroeffnung_modus, konto.eroeffnung_stichtag) for konto_id, konto in konten_je_id.items()
    }
    with session_factory() as session:
        ergebnisse: list[OPPositionTable] = []
        try:
            for index, zeile in enumerate(zeilen):
                try:
                    konto = konten_je_id.get(zeile.konto_id)
                    if konto is None:
                        raise ValueError(f"Unbekanntes Konto '{zeile.konto_id}'.")
                    if zeile.modus == "GESAMTSALDO":
                        ergebnisse.append(
                            op_service.eroeffnen_gesamtsaldo(
                                ctx=ctx, konto=konto, betrag_cent=zeile.betrag_cent, stichtag=zeile.stichtag,
                                import_id=zeile.import_id, akteur=akteur, session=session,
                            )
                        )
                    elif zeile.modus == "EINZEL_OP":
                        ergebnisse.append(
                            op_service.eroeffnen_einzel_op(
                                ctx=ctx, konto=konto, stichtag=zeile.stichtag, import_id=zeile.import_id,
                                typ=OPTyp(zeile.typ or "SOLL"), betrag_cent=zeile.betrag_cent,
                                belegdatum=zeile.belegdatum or zeile.stichtag, faelligkeit=zeile.faelligkeit,
                                beleg_referenz=zeile.beleg_referenz, akteur=akteur, session=session,
                            )
                        )
                    else:
                        raise ValueError(
                            f"Unbekannter Eröffnungsmodus '{zeile.modus}' (erwartet GESAMTSALDO oder EINZEL_OP)."
                        )
                except Exception as exc:
                    exc.args = (
                        f"Eröffnungsimport abgebrochen bei Zeile {index + 1} von {len(zeilen)} "
                        f"(Konto '{zeile.konto_id}'): {exc}. Die gesamte Datei wurde NICHT gebucht (atomarer "
                        "Import); nach Korrektur kann der volle Lauf gefahrlos wiederholt werden.",
                    )
                    raise
            session.commit()
        except Exception:
            session.rollback()
            for konto_id, (modus, stichtag) in urspruenglicher_zustand.items():
                konto = konten_je_id.get(konto_id)
                if konto is not None:
                    konto.eroeffnung_modus = modus
                    konto.eroeffnung_stichtag = stichtag
            raise
        for row in ergebnisse:
            session.refresh(row)
        return ergebnisse
