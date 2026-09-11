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

from mietinkasso.auth.service import AuthContext
from mietinkasso.domain.enums import OPTyp
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
        betrag_cent = int(round(float(roh["betrag"].strip().replace(",", ".")) * 100))
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
