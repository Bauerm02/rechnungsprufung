"""Debitoren-Ledger: Eröffnung, Nachbuchung, Zahlungsverbuchung, Storno.

OP-Saldo = Eröffnung + Soll/Nachbelastung - Gutschrift - zugeordnete
Zahlung + Rücklastschrift (Fachregel 4). Implemented as a per-typ signed
sum over all AKTIV OPPosition rows for a Konto; STORNIERT rows are kept
for audit but excluded from the balance.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date

from mietinkasso.auth.service import AuthContext, require_gesellschaft_access, require_schreibrecht
from mietinkasso.domain.enums import OPPositionStatus, OPTyp
from mietinkasso.domain.exceptions import DoppelteEroeffnungsartError
from mietinkasso.infrastructure.db.tables import KontoTable, OPPositionTable
from mietinkasso.op.repository import OPRepository
from mietinkasso.stammdaten.repository import StammdatenRepository

_POSITIVE_TYPEN = {OPTyp.EROEFFNUNG, OPTyp.SOLL, OPTyp.RUECKLASTSCHRIFT}
_NEGATIVE_TYPEN = {OPTyp.GUTSCHRIFT, OPTyp.ZAHLUNG}


def compute_content_hash(fields: dict) -> str:
    canonical = json.dumps(fields, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _effect_cent(row: OPPositionTable) -> int:
    typ = OPTyp(row.typ)
    if typ in _POSITIVE_TYPEN:
        return row.betrag_cent
    if typ in _NEGATIVE_TYPEN:
        return -row.betrag_cent
    if typ is OPTyp.KORREKTUR:
        return row.betrag_cent  # sign carried explicitly by the caller
    raise ValueError(f"Unbekannter OP-Typ {row.typ}")


@dataclass(frozen=True)
class OPSaldo:
    konto_id: str
    saldo_cent: int
    faelliger_unstrittiger_rest_cent: int
    positionen: list[OPPositionTable]


class OPService:
    def __init__(
        self,
        op_repository: OPRepository,
        stammdaten_repository: StammdatenRepository,
    ):
        self._op_repository = op_repository
        self._stammdaten_repository = stammdaten_repository

    # -- Eröffnung ------------------------------------------------------
    def eroeffnen_gesamtsaldo(
        self,
        *,
        ctx: AuthContext,
        konto: KontoTable,
        betrag_cent: int,
        stichtag: date,
        import_id: str,
        akteur: str,
    ) -> OPPositionTable:
        require_gesellschaft_access(ctx, konto.gesellschaft_id)
        require_schreibrecht(ctx)
        self._pruefe_und_setze_eroeffnungsmodus(konto, "GESAMTSALDO", stichtag)
        content_hash = compute_content_hash(
            {"konto_id": konto.id, "modus": "GESAMTSALDO", "betrag_cent": betrag_cent, "stichtag": str(stichtag)}
        )
        row = OPPositionTable(
            konto_id=konto.id,
            typ=OPTyp.EROEFFNUNG.value,
            betrag_cent=betrag_cent,
            leistungsperiode=None,
            belegdatum=stichtag,
            buchungsdatum=stichtag,
            faelligkeit=None,
            faelligkeit_bekannt=False,
            beleg_referenz="Eröffnung Gesamtsaldo",
            quelle_hash=content_hash,
            import_id=import_id,
            quelle_system="eroeffnung_gesamtsaldo",
        )
        return self._op_repository.insert_idempotent(row)

    def eroeffnen_einzel_op(
        self,
        *,
        ctx: AuthContext,
        konto: KontoTable,
        stichtag: date,
        import_id: str,
        typ: OPTyp,
        betrag_cent: int,
        belegdatum: date,
        faelligkeit: date | None,
        beleg_referenz: str,
        akteur: str,
    ) -> OPPositionTable:
        require_gesellschaft_access(ctx, konto.gesellschaft_id)
        require_schreibrecht(ctx)
        self._pruefe_und_setze_eroeffnungsmodus(konto, "EINZEL_OP", stichtag)
        content_hash = compute_content_hash(
            {
                "konto_id": konto.id,
                "modus": "EINZEL_OP",
                "typ": typ.value,
                "betrag_cent": betrag_cent,
                "belegdatum": str(belegdatum),
                "beleg_referenz": beleg_referenz,
            }
        )
        row = OPPositionTable(
            konto_id=konto.id,
            typ=typ.value,
            betrag_cent=betrag_cent,
            leistungsperiode=None,
            belegdatum=belegdatum,
            buchungsdatum=stichtag,
            faelligkeit=faelligkeit,
            faelligkeit_bekannt=faelligkeit is not None,
            beleg_referenz=beleg_referenz,
            quelle_hash=content_hash,
            import_id=import_id,
            quelle_system="eroeffnung_einzel_op",
        )
        return self._op_repository.insert_idempotent(row)

    def _pruefe_und_setze_eroeffnungsmodus(self, konto: KontoTable, modus: str, stichtag: date) -> None:
        if konto.eroeffnung_modus is None:
            self._stammdaten_repository.set_eroeffnung_modus(konto_id=konto.id, modus=modus, stichtag=stichtag)
            konto.eroeffnung_modus = modus
            konto.eroeffnung_stichtag = stichtag
            return
        if konto.eroeffnung_modus != modus:
            raise DoppelteEroeffnungsartError(
                f"Konto {konto.id} wurde bereits mit Modus {konto.eroeffnung_modus} eröffnet; "
                f"Modus {modus} würde dasselbe alte Journal doppelt buchen."
            )

    def pruefe_kein_altjournal_in_gesamtsaldo(self, konto: KontoTable, belegdatum: date) -> None:
        """Guard used before importing a historical SOLL/GUTSCHRIFT row: if the
        Konto was opened via a confirmed Gesamtsaldo, any row dated on/before
        that Stichtag is already contained in the total and must not also be
        booked individually."""

        if konto.eroeffnung_modus == "GESAMTSALDO" and konto.eroeffnung_stichtag is not None:
            if belegdatum <= konto.eroeffnung_stichtag:
                raise DoppelteEroeffnungsartError(
                    f"Konto {konto.id}: Beleg vom {belegdatum} liegt vor/auf dem Gesamtsaldo-Stichtag "
                    f"{konto.eroeffnung_stichtag} und ist darin bereits enthalten."
                )

    # -- Nachbuchung / Vorschreibung / Zahlung ---------------------------
    def buchen(
        self,
        *,
        ctx: AuthContext,
        konto: KontoTable,
        typ: OPTyp,
        betrag_cent: int,
        belegdatum: date,
        buchungsdatum: date,
        faelligkeit: date | None,
        beleg_referenz: str,
        aenderungsgrund: str | None = None,
        leistungsperiode: str | None = None,
        import_id: str | None = None,
        quelle_system: str | None = None,
    ) -> OPPositionTable:
        require_gesellschaft_access(ctx, konto.gesellschaft_id)
        require_schreibrecht(ctx)
        if typ in (OPTyp.SOLL, OPTyp.GUTSCHRIFT):
            self.pruefe_kein_altjournal_in_gesamtsaldo(konto, belegdatum)
        content_hash = compute_content_hash(
            {
                "konto_id": konto.id,
                "typ": typ.value,
                "betrag_cent": betrag_cent,
                "belegdatum": str(belegdatum),
                "leistungsperiode": leistungsperiode,
                "beleg_referenz": beleg_referenz,
            }
        )
        row = OPPositionTable(
            konto_id=konto.id,
            typ=typ.value,
            betrag_cent=betrag_cent,
            leistungsperiode=leistungsperiode,
            belegdatum=belegdatum,
            buchungsdatum=buchungsdatum,
            faelligkeit=faelligkeit,
            faelligkeit_bekannt=faelligkeit is not None,
            beleg_referenz=beleg_referenz,
            aenderungsgrund=aenderungsgrund,
            quelle_hash=content_hash,
            import_id=import_id,
            quelle_system=quelle_system,
        )
        return self._op_repository.insert_idempotent(row)

    def storniere_und_korrigiere(
        self,
        *,
        ctx: AuthContext,
        konto: KontoTable,
        original_id: int,
        aenderungsgrund: str,
        neuer_betrag_cent: int | None = None,
        neue_faelligkeit: date | None = None,
        heute: date | None = None,
    ) -> OPPositionTable | None:
        require_gesellschaft_access(ctx, konto.gesellschaft_id)
        require_schreibrecht(ctx)
        heute = heute or date.today()
        original = self._op_repository.get(original_id)
        if original is None or original.konto_id != konto.id:
            raise ValueError(f"OPPosition {original_id} gehört nicht zu Konto {konto.id}")
        neue_row = None
        if neuer_betrag_cent is not None:
            neue_row = OPPositionTable(
                konto_id=konto.id,
                typ=original.typ,
                betrag_cent=neuer_betrag_cent,
                leistungsperiode=original.leistungsperiode,
                belegdatum=original.belegdatum,
                buchungsdatum=heute,
                faelligkeit=neue_faelligkeit if neue_faelligkeit is not None else original.faelligkeit,
                faelligkeit_bekannt=(neue_faelligkeit or original.faelligkeit) is not None,
                beleg_referenz=f"Korrektur zu #{original.id}: {original.beleg_referenz}",
                aenderungsgrund=aenderungsgrund,
                quelle_hash=compute_content_hash(
                    {"korrektur_von": original.id, "betrag_cent": neuer_betrag_cent, "grund": aenderungsgrund}
                ),
                quelle_system="korrektur",
            )
        return self._op_repository.storno(original_id=original_id, neue_row=neue_row, akteur=ctx.user_id)

    # -- Saldo --------------------------------------------------------------
    def berechne_saldo(self, konto_id: str, *, stichtag: date | None = None) -> OPSaldo:
        positionen = self._op_repository.list_aktiv(konto_id)
        if stichtag is not None:
            positionen = [p for p in positionen if p.buchungsdatum <= stichtag]
        saldo_cent = sum(_effect_cent(p) for p in positionen)
        heute = stichtag or date.today()

        # Der "fällige unstrittige Rest" ist die Teilmenge des Saldos, die
        # bemahnt werden darf: Forderungen (Eröffnung/Soll/Rücklastschrift)
        # zählen nur mit bekannter, bereits verstrichener Fälligkeit
        # ("Saldo ohne bekannte Fälligkeit sichtbar, aber nicht automatisch
        # mahnen"). Zahlungen und Gutschriften mindern die Forderung immer,
        # sobald sie gebucht sind - eine Zahlung hat selbst keine eigene
        # Fälligkeit und darf dafür nicht ausgeschlossen werden.
        faellig_rest = 0
        for position in positionen:
            typ = OPTyp(position.typ)
            if typ in (OPTyp.GUTSCHRIFT, OPTyp.ZAHLUNG, OPTyp.KORREKTUR):
                faellig_rest += _effect_cent(position)
            elif position.faelligkeit_bekannt and position.faelligkeit is not None and position.faelligkeit <= heute:
                faellig_rest += _effect_cent(position)

        return OPSaldo(
            konto_id=konto_id,
            saldo_cent=saldo_cent,
            faelliger_unstrittiger_rest_cent=max(faellig_rest, 0),
            positionen=positionen,
        )
