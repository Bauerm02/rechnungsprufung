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

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mietinkasso.auth.service import AuthContext, require_gesellschaft_access, require_schreibrecht
from mietinkasso.domain.enums import OPTyp
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


@dataclass(frozen=True)
class OffeneForderung:
    """Eine einzelne, individuell verfolgbare Forderung (nicht der
    Kontosaldo). `op_position_id` ist die stabile Identität, an die das
    Mahnwesen seinen Stufe1->Stufe2-Zyklus bindet."""

    op_position_id: int
    art: str
    betrag_cent: int
    rest_cent: int
    belegdatum: date
    faelligkeit: date | None
    faelligkeit_bekannt: bool
    leistungsperiode: str | None


class OPService:
    def __init__(
        self,
        op_repository: OPRepository,
        stammdaten_repository: StammdatenRepository,
    ):
        self._op_repository = op_repository
        self._stammdaten_repository = stammdaten_repository

    @property
    def session_factory(self):
        return self._op_repository.session_factory

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
        session: Session | None = None,
    ) -> OPPositionTable:
        """`session`: siehe `buchen` - übergeben, um diese Eröffnungszeile
        Teil eines größeren, vom Aufrufer verwalteten Mehrzeilen-Imports zu
        machen (z. B. eine ganze Eröffnungssalden-CSV-Datei atomar: eine
        spätere Fehlerzeile rollt dann auch bereits verarbeitete frühere
        Zeilen desselben Aufrufs zurück)."""

        require_gesellschaft_access(ctx, konto.gesellschaft_id)
        require_schreibrecht(ctx)
        self._stammdaten_repository.pruefe_konto_nicht_ausgeschlossen(konto, session=session)
        self._pruefe_und_setze_eroeffnungsmodus(konto, "GESAMTSALDO", stichtag, session=session)

        # Eröffnung ist fachlich EINMALIG je Konto/Stichtag - unabhängig vom
        # import_id/Dateinamen der jeweiligen Quelle. Ein zweiter Import mit
        # identischem Betrag/Stichtag (z. B. dieselbe Datei mit anderem
        # Namen) ist ein Replay derselben Tatsache; ein zweiter Import mit
        # ABWEICHENDEM Betrag/Stichtag ist eine widersprüchliche
        # Wiedereröffnung und wird blockiert, statt den Saldo zu verdoppeln.
        bestehende = self._op_repository.find_eroeffnung(konto.id, session=session)
        if bestehende is not None:
            if bestehende.betrag_cent == betrag_cent and bestehende.belegdatum == stichtag:
                return bestehende
            raise DoppelteEroeffnungsartError(
                f"Konto {konto.id} wurde bereits mit Gesamtsaldo {bestehende.betrag_cent} Cent zum "
                f"{bestehende.belegdatum} eröffnet (import_id={bestehende.import_id}). Eine widersprüchliche "
                f"Wiedereröffnung mit {betrag_cent} Cent zum {stichtag} (import_id={import_id}) wird blockiert."
            )

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
        try:
            return self._op_repository.insert_idempotent(row, session=session)
        except IntegrityError as exc:
            # Zweite, gleichzeitige Eröffnung desselben Kontos (Race) - vom
            # partiellen Unique-Index auf DB-Ebene abgefangen.
            raise DoppelteEroeffnungsartError(
                f"Konto {konto.id}: gleichzeitige Eröffnung erkannt (DB-Constraint uq_op_eroeffnung_pro_konto)."
            ) from exc

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
        session: Session | None = None,
    ) -> OPPositionTable:
        """`session`: siehe `eroeffnen_gesamtsaldo`."""

        require_gesellschaft_access(ctx, konto.gesellschaft_id)
        require_schreibrecht(ctx)
        self._stammdaten_repository.pruefe_konto_nicht_ausgeschlossen(konto, session=session)
        self._pruefe_und_setze_eroeffnungsmodus(konto, "EINZEL_OP", stichtag, session=session)
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
        return self._op_repository.insert_idempotent(row, session=session)

    def _pruefe_und_setze_eroeffnungsmodus(
        self, konto: KontoTable, modus: str, stichtag: date, *, session: Session | None = None
    ) -> None:
        if konto.eroeffnung_modus is None:
            self._stammdaten_repository.set_eroeffnung_modus(
                konto_id=konto.id, modus=modus, stichtag=stichtag, session=session
            )
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

    def eroeffnungskorrektur_buchen(
        self,
        *,
        ctx: AuthContext,
        konto: KontoTable,
        typ: OPTyp,
        betrag_cent: int,
        original_belegdatum: date,
        uebernahmetag: date,
        grund: str,
        quelle_referenz: str,
        import_id: str,
        beleg_referenz: str = "Eröffnungskorrektur",
        session: Session | None = None,
    ) -> OPPositionTable:
        """Schmaler Sonderpfad für GENAU EINEN Fall (Auftrag
        HV-20260912-ECHTBETRIEB, Ergänzung): der bestätigte
        Eröffnungs-Gesamtsaldo enthält nachweislich einen Posten NICHT
        (z. B. eine Zahlung, deren tatsächliches Datum vor dem
        Eröffnungsstichtag liegt), und das muss NACHTRÄGLICH belegt
        korrigiert werden - OHNE das allgemeine Altjournal-Tor
        (`pruefe_kein_altjournal_in_gesamtsaldo`) für gewöhnliche
        Nachbuchungen zu öffnen. Deshalb:

        - nur für ein Konto zulässig, das bereits mit GESAMTSALDO eröffnet
          wurde (bei EINZEL_OP gibt es kein "im Saldo bereits enthalten" -
          dort ist eine gewöhnliche `buchen()`-Nachbuchung der richtige Weg);
        - `original_belegdatum` bleibt das ECHTE, historische Datum (bewahrt,
          nicht verschoben) und darf/soll auf/vor dem Eröffnungsstichtag
          liegen - genau das ist der Zweck dieses Pfads;
        - `buchungsdatum` der entstehenden Zeile ist immer `uebernahmetag`
          (der Tag DIESER Korrekturbuchung), nie das historische Datum;
        - `grund` und `quelle_referenz` sind Pflichtangaben (kein
          Platzhalter) und werden als `aenderungsgrund` bzw. Teil des
          `beleg_referenz`-Textes gespeichert; `quelle_system` trägt den
          festen Wert "eroeffnungskorrektur" - das IST das geforderte
          eigene Flag (eine eigene Bool-Spalte wäre hier nur eine weitere
          Bezeichnung für dieselbe Unterscheidung);
        - Idempotenz/Doppelbuchungsschutz laufen exakt wie bei jeder
          anderen Buchung über `import_id` + Content-Hash
          (`OPRepository.insert_idempotent`) - ein Replay derselben
          `import_id` mit identischem Inhalt ist ein No-Op, ein abweichender
          Inhalt derselben `import_id` ein Konflikt."""

        require_gesellschaft_access(ctx, konto.gesellschaft_id)
        require_schreibrecht(ctx)
        self._stammdaten_repository.pruefe_konto_nicht_ausgeschlossen(konto, session=session)
        if not (grund or "").strip():
            raise ValueError("Eröffnungskorrektur ohne 'grund' ist nicht zulässig.")
        if not (quelle_referenz or "").strip():
            raise ValueError("Eröffnungskorrektur ohne 'quelle_referenz' ist nicht zulässig.")
        if konto.eroeffnung_modus != "GESAMTSALDO":
            raise DoppelteEroeffnungsartError(
                f"Konto {konto.id}: Eröffnungskorrektur setzt eine bereits mit GESAMTSALDO eröffnete "
                f"Eröffnung voraus (aktueller Modus: {konto.eroeffnung_modus})."
            )
        content_hash = compute_content_hash(
            {
                "konto_id": konto.id,
                "modus": "EROEFFNUNGSKORREKTUR",
                "typ": typ.value,
                "betrag_cent": betrag_cent,
                "original_belegdatum": str(original_belegdatum),
                "grund": grund,
                "quelle_referenz": quelle_referenz,
            }
        )
        row = OPPositionTable(
            konto_id=konto.id,
            typ=typ.value,
            betrag_cent=betrag_cent,
            leistungsperiode=None,
            belegdatum=original_belegdatum,
            buchungsdatum=uebernahmetag,
            faelligkeit=None,
            faelligkeit_bekannt=False,
            beleg_referenz=f"{beleg_referenz} (Quelle: {quelle_referenz})",
            aenderungsgrund=grund,
            quelle_hash=content_hash,
            import_id=import_id,
            quelle_system="eroeffnungskorrektur",
        )
        return self._op_repository.insert_idempotent(row, session=session)

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
        bank_transaktion_id: int | None = None,
        bezieht_sich_auf_id: int | None = None,
        session: Session | None = None,
    ) -> OPPositionTable:
        """`session`: siehe `OPRepository.insert_idempotent` - übergeben,
        um diese Buchung Teil einer größeren, vom Aufrufer verwalteten
        Transaktion zu machen (z. B. Bank-Zuordnung: OP-Buchung +
        Zuordnung + Audit in einer DB-Transaktion mit gemeinsamem
        Rollback)."""

        require_gesellschaft_access(ctx, konto.gesellschaft_id)
        require_schreibrecht(ctx)
        self._stammdaten_repository.pruefe_konto_nicht_ausgeschlossen(konto, session=session)
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
            bank_transaktion_id=bank_transaktion_id,
            bezieht_sich_auf_id=bezieht_sich_auf_id,
        )
        return self._op_repository.insert_idempotent(row, session=session)

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
        vorgang_id: str | None = None,
    ) -> OPPositionTable | None:
        """`vorgang_id`: vom Aufrufer vergebene Kennung für DIESE
        Korrektur-Anfrage (z. B. gegen eine doppelte Formularbestätigung im
        Backoffice). Ein zweiter Aufruf mit DERSELBEN `vorgang_id` (und
        sonst identischem Inhalt) gegen ein bereits korrigiertes Original
        ist ein sicherer No-Op; jeder andere zweite Aufruf auf ein bereits
        storniertes Original wird als `StornierungKonfliktError`
        abgelehnt, statt eine zweite aktive Ersatzzeile anzulegen."""

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
                    {
                        "korrektur_von": original.id,
                        "betrag_cent": neuer_betrag_cent,
                        "faelligkeit": str(neue_faelligkeit) if neue_faelligkeit else None,
                        "grund": aenderungsgrund,
                    }
                ),
                quelle_system="korrektur",
                import_id=f"KORREKTUR-{original_id}-{vorgang_id}" if vorgang_id is not None else None,
            )
        return self._op_repository.storno(
            original_id=original_id, neue_row=neue_row, akteur=ctx.user_id, vorgang_id=vorgang_id
        )

    def get_position(self, op_position_id: int) -> OPPositionTable | None:
        return self._op_repository.get(op_position_id)

    def bestehende_eroeffnung(self, konto_id: str) -> OPPositionTable | None:
        return self._op_repository.find_eroeffnung(konto_id)

    def list_alle_positionen(self, konto_id: str) -> list[OPPositionTable]:
        return self._op_repository.list_alle(konto_id)

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
        # Fälligkeit und darf dafür nicht ausgeschlossen werden. Ein
        # GUTHABEN in einer eigentlich forderungsseitigen Zeile (z. B. ein
        # negativer Eröffnungssaldo) ist wirtschaftlich dasselbe wie eine
        # Zahlung/Gutschrift und mindert den fälligen Rest daher ebenfalls
        # IMMER, unabhängig von der (bei EROEFFNUNG oft unbekannten)
        # eigenen Fälligkeit - sonst bliebe ein Guthaben unberücksichtigt
        # und ein späteres Soll würde in voller Höhe als fällig ausgewiesen.
        faellig_rest = 0
        for position in positionen:
            typ = OPTyp(position.typ)
            effekt = _effect_cent(position)
            if typ in (OPTyp.GUTSCHRIFT, OPTyp.ZAHLUNG, OPTyp.KORREKTUR):
                faellig_rest += effekt
            elif typ in _POSITIVE_TYPEN and effekt < 0:
                faellig_rest += effekt
            elif position.faelligkeit_bekannt and position.faelligkeit is not None and position.faelligkeit <= heute:
                faellig_rest += effekt

        return OPSaldo(
            konto_id=konto_id,
            saldo_cent=saldo_cent,
            faelliger_unstrittiger_rest_cent=max(faellig_rest, 0),
            positionen=positionen,
        )

    # -- Forderungen (für das Mahnwesen) -------------------------------------
    def offene_forderungen(self, konto_id: str, *, heute: date | None = None) -> list["OffeneForderung"]:
        """Ordnet Zahlungen/Gutschriften den ältesten offenen Forderungen
        FIFO zu, damit jede Forderung (Eröffnung/Soll/Rücklastschrift) ihren
        EIGENEN Reststand und damit ihren eigenen Mahnzyklus hat - eine
        Nettosumme über das ganze Konto ist keine Mahngrundlage (Fachregel:
        Soll-/Habensalden je Forderung getrennt führen). Nur Forderungen mit
        rest_cent > 0 werden zurückgegeben."""

        positionen = self._op_repository.list_aktiv(konto_id)

        # Nur ECHTE Forderungen (positiver Betrag) zählen als Forderungszeile;
        # eine "positive Typ"-Zeile mit NEGATIVEM Betrag (z. B. ein
        # Guthaben-Eröffnungssaldo) ist ein Guthaben, keine Forderung, und
        # landet stattdessen im Minderungs-Pool (siehe unten) - sonst würde
        # sie weder als Forderung noch als Guthaben gezählt und würde
        # spurlos verschwinden, ohne spätere Forderungen zu mindern.
        forderungs_rows = [p for p in positionen if OPTyp(p.typ) in _POSITIVE_TYPEN and p.betrag_cent > 0]
        forderungs_rows.sort(key=lambda p: (p.faelligkeit or p.belegdatum, p.belegdatum, p.id))

        minderungs_pool = 0
        for p in positionen:
            typ = OPTyp(p.typ)
            if typ in _NEGATIVE_TYPEN:
                minderungs_pool += p.betrag_cent
            elif typ in _POSITIVE_TYPEN and p.betrag_cent < 0:
                minderungs_pool += -p.betrag_cent
            elif typ is OPTyp.KORREKTUR:
                effekt = _effect_cent(p)
                if effekt < 0:
                    minderungs_pool += -effekt

        ergebnisse: list[OffeneForderung] = []
        for p in forderungs_rows:
            rest = p.betrag_cent
            if minderungs_pool > 0:
                abzug = min(minderungs_pool, rest)
                rest -= abzug
                minderungs_pool -= abzug
            if rest > 0:
                ergebnisse.append(
                    OffeneForderung(
                        op_position_id=p.id,
                        art=p.typ,
                        betrag_cent=p.betrag_cent,
                        rest_cent=rest,
                        belegdatum=p.belegdatum,
                        faelligkeit=p.faelligkeit,
                        faelligkeit_bekannt=p.faelligkeit_bekannt,
                        leistungsperiode=p.leistungsperiode,
                    )
                )
        return ergebnisse
