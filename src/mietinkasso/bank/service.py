"""Bankabgleich: Import, automatische/manuelle Zuordnung, Rücklastschrift,
Bankvollständigkeit. Fachregel 4: Zahlung und Zuordnung sind getrennte
Schritte; automatisch wird nur bei eindeutiger Referenz UND eindeutigem
Konto zugeordnet, niemals allein über Namensgleichheit oder gleichen
Betrag.

OP-Buchung, Zuordnung und Audit-Eintrag laufen für jede Zuordnung in
EINER gemeinsamen DB-Transaktion: schlägt irgendein Schritt fehl (auch
ein unerwarteter Fehler mitten in der Zuordnungserstellung), wird die
GESAMTE Transaktion zurückgerollt - es bleibt nie eine gebuchte Zahlung
ohne zugehörige Zuordnung übrig. Datei-Importe (CAMT.053/CSV) sind
ebenfalls atomar je Aufruf: schlägt eine Zeile fehl, wird der gesamte
Importlauf zurückgerollt statt unsichtbare Teilergebnisse zu
hinterlassen; ein erneuter, korrigierter Lauf ist dank Idempotenz immer
gefahrlos möglich.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from mietinkasso.auth.service import AuthContext, require_gesellschaft_access, require_schreibrecht
from mietinkasso.bank.importer import CsvSpaltenMapping, RohTransaktion, parse_camt053, parse_csv
from mietinkasso.bank.repository import BankRepository
from mietinkasso.domain.enums import OPTyp, ZahlungsMatchTyp
from mietinkasso.domain.exceptions import (
    BindungInkonsistentError,
    CrossTenantError,
    FremdwaehrungNichtUnterstuetztError,
    MehrfachbuchungsKonfliktError,
    ZuordnungUngueltigError,
)
from mietinkasso.infrastructure.db.tables import BankKontoTable, BankTransaktionTable, KontoTable, OPPositionTable
from mietinkasso.op.service import OPService
from mietinkasso.stammdaten.repository import StammdatenRepository

_VERTRAG_REFERENZ = re.compile(r"VERTRAG:([A-Za-z0-9\-]+)")


def _hash(fields: dict) -> str:
    canonical = json.dumps(fields, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _fingerprint(bank_konto_id: str, roh: RohTransaktion) -> str:
    """Formatierungsunabhängiger, konto-gescopter Fingerabdruck der
    wirtschaftlich relevanten Felder - für Zeilen OHNE bankseitig
    eindeutige Kennung. Zeilennummern/Roh-Layout fließen bewusst NICHT
    ein, damit ein überlappender Re-Export mit anderer Zeilenreihenfolge
    dieselbe Zahlung wiedererkennbar macht."""

    return _hash(
        {
            "bank_konto_id": bank_konto_id,
            "buchungsdatum": str(roh.buchungsdatum),
            "betrag_cent": roh.betrag_cent,
            "waehrung": roh.waehrung,
            "referenz": (roh.referenz or "").strip().lower(),
            "gegenkonto_iban": (roh.gegenkonto_iban or "").strip().upper(),
        }
    )


@dataclass(frozen=True)
class ZuordnungsErgebnis:
    zugeordnet: bool
    grund: str
    zuordnung_id: int | None = None
    op_position_id: int | None = None


class BankImportService:
    def __init__(
        self,
        repository: BankRepository,
        stammdaten_repository: StammdatenRepository,
        op_service: OPService,
    ):
        self._repository = repository
        self._stammdaten_repository = stammdaten_repository
        self._op_service = op_service
        self._session_factory = repository.session_factory

    # -- Import -----------------------------------------------------------
    def importiere_camt053(
        self, *, ctx: AuthContext, bank_konto: BankKontoTable, xml_bytes: bytes
    ) -> list[BankTransaktionTable]:
        require_gesellschaft_access(ctx, bank_konto.gesellschaft_id)
        require_schreibrecht(ctx)
        rohdaten = parse_camt053(xml_bytes)
        return self._importiere_atomar(rohdaten, bank_konto, "CAMT053")

    def importiere_csv(
        self, *, ctx: AuthContext, bank_konto: BankKontoTable, text: str, mapping: CsvSpaltenMapping
    ) -> list[BankTransaktionTable]:
        require_gesellschaft_access(ctx, bank_konto.gesellschaft_id)
        require_schreibrecht(ctx)
        rohdaten = parse_csv(text, mapping)
        return self._importiere_atomar(rohdaten, bank_konto, "CSV")

    def _importiere_atomar(
        self, rohdaten: list[RohTransaktion], bank_konto: BankKontoTable, quelle_typ: str
    ) -> list[BankTransaktionTable]:
        """Der gesamte Dateiimport ist EINE DB-Transaktion: scheitert eine
        Zeile (Konflikt, Formatfehler, ...), wird der GESAMTE Aufruf
        zurückgerollt - keine Zeile aus diesem Aufruf bleibt hängen. Ein
        erneuter Lauf nach Korrektur der Quelle ist dank Idempotenz
        (import_id/Fingerprint) immer gefahrlos, weil nichts Teilweises
        übrig bleibt, auf das man Rücksicht nehmen müsste."""

        with self._session_factory() as session:
            ergebnisse: list[BankTransaktionTable] = []
            try:
                for index, roh in enumerate(rohdaten):
                    try:
                        ergebnisse.append(self._speichere_roh(bank_konto, roh, quelle_typ, session=session))
                    except Exception as exc:
                        # Ursprünglichen Fehlertyp NICHT verschlucken (Aufrufer
                        # unterscheiden z. B. MehrfachbuchungsKonfliktError von
                        # CamtUnvollstaendigError) - nur mit Zeilenkontext
                        # anreichern und weiterreichen. Die Transaktion wird
                        # trotzdem vollständig zurückgerollt (siehe unten):
                        # NICHTS aus diesem Aufruf bleibt hängen.
                        exc.args = (
                            f"Import von Bankkonto {bank_konto.id} ({quelle_typ}) abgebrochen bei Zeile "
                            f"{index + 1} von {len(rohdaten)}: {exc}. Die gesamte Datei wurde NICHT gebucht "
                            "(atomarer Import); nach Korrektur kann der volle Lauf gefahrlos wiederholt werden.",
                        )
                        raise
                session.commit()
            except Exception:
                session.rollback()
                raise
            for row in ergebnisse:
                session.refresh(row)
            return ergebnisse

    def _speichere_roh(
        self, bank_konto: BankKontoTable, roh: RohTransaktion, quelle_typ: str, *, session: Session | None = None
    ) -> BankTransaktionTable:
        content_hash = _hash(
            {
                "bank_konto_id": bank_konto.id,
                "betrag_cent": roh.betrag_cent,
                "waehrung": roh.waehrung,
                "buchungsdatum": str(roh.buchungsdatum),
                "valuta": str(roh.valuta) if roh.valuta else None,
                "referenz": roh.referenz,
                "gegenkonto_iban": roh.gegenkonto_iban,
                "gegenkonto_name": roh.gegenkonto_name,
                "native_id": roh.native_id,
            }
        )
        if roh.native_id:
            hat_native_id = True
            fingerprint_hash = None
            import_id = f"{bank_konto.id}:{quelle_typ}:{roh.native_id}"
        else:
            hat_native_id = False
            fingerprint_hash = _fingerprint(bank_konto.id, roh)
            bestehende = self._repository.find_by_fingerprint(bank_konto.id, fingerprint_hash, session=session)
            if bestehende is not None:
                raise MehrfachbuchungsKonfliktError(
                    f"Banktransaktion ohne bankseitig eindeutige Kennung auf Bankkonto {bank_konto.id}: "
                    f"Betrag/Datum/Referenz sind identisch zur bereits importierten Transaktion "
                    f"#{bestehende.id}. Das kann dieselbe Zahlung aus einem überlappenden Export sein ODER "
                    f"eine zweite, echte Zahlung mit zufällig identischen Merkmalen - das lässt sich nicht "
                    f"automatisch entscheiden und wird daher NICHT still zusammengelegt oder dupliziert, "
                    f"sondern zur manuellen Klärung verweigert."
                )
            # Bewusst kein wiederverwendbarer Schlüssel: ohne native ID kann
            # ein Replay nicht von einer echten zweiten Zahlung
            # unterschieden werden, daher entscheidet ausschließlich der
            # obige Fingerprint-Konflikt, nicht die import_id-Idempotenz.
            import_id = f"{bank_konto.id}:{quelle_typ}:NOID:{uuid.uuid4()}"

        row = BankTransaktionTable(
            bank_konto_id=bank_konto.id,
            betrag_cent=roh.betrag_cent,
            waehrung=roh.waehrung,
            buchungsdatum=roh.buchungsdatum,
            valuta=roh.valuta,
            referenz=roh.referenz,
            gegenkonto_iban=roh.gegenkonto_iban,
            gegenkonto_name=roh.gegenkonto_name,
            quelle_typ=quelle_typ,
            quelle_hash=content_hash,
            import_id=import_id,
            hat_native_id=hat_native_id,
            fingerprint_hash=fingerprint_hash,
            roh_zeile=roh.roh_zeile,
        )
        return self._repository.insert_transaktion_idempotent(row, session=session)

    # -- Zuordnung (OP-Buchung + Zuordnung + Audit als EINE Transaktion) ----
    def _ist_automatisch_zuordenbar(self, transaktion: BankTransaktionTable) -> tuple[KontoTable | None, str]:
        """Reine Vorprüfung für den GRACEFUL-SKIP-Pfad von
        `automatisch_zuordnen` (kein DB-Schreibzugriff): entscheidet, ob
        überhaupt ein Versuch unternommen wird. Die eigentliche,
        maßgebliche Validierung passiert danach innerhalb derselben
        Transaktion wie die Buchung selbst (`BankRepository.create_zuordnung`)."""

        if transaktion.betrag_cent <= 0:
            return None, "Nur Zahlungseingänge (positiver Betrag) werden automatisch zugeordnet."
        if not transaktion.referenz:
            return None, "Keine Referenz vorhanden; nur manuelle Zuordnung möglich."
        treffer = _VERTRAG_REFERENZ.search(transaktion.referenz)
        if not treffer:
            return None, "Referenz enthält keine eindeutige Vertragskennung; Name/Betrag allein reichen nicht."
        vertrag_id = treffer.group(1)
        konto = self._stammdaten_repository.get_konto_by_vertrag(vertrag_id)
        if konto is None:
            return None, f"Referenzierter Vertrag {vertrag_id} hat kein Konto."
        verbleibend = transaktion.betrag_cent - self._repository.zugeordneter_betrag(transaktion.id)
        if verbleibend <= 0:
            return None, "Transaktion ist bereits vollständig zugeordnet."
        return konto, "eindeutig"

    def automatisch_zuordnen(self, *, ctx: AuthContext, transaktion: BankTransaktionTable) -> ZuordnungsErgebnis:
        konto, grund = self._ist_automatisch_zuordenbar(transaktion)
        if konto is None:
            return ZuordnungsErgebnis(False, grund)

        verbleibend = transaktion.betrag_cent - self._repository.zugeordneter_betrag(transaktion.id)
        # Deterministische Vorgangs-ID: ein Retry DERSELBEN automatischen
        # Zuordnung für dieselbe Transaktion ist ein No-Op, kein
        # Doppelversuch.
        vorgang_id = f"AUTO-{transaktion.id}"
        op_row, zuordnung = self._zuordnen_atomar(
            ctx=ctx,
            konto=konto,
            transaktion=transaktion,
            betrag_cent=verbleibend,
            beleg_referenz=f"Bankzahlung {transaktion.referenz}",
            match_typ=ZahlungsMatchTyp.AUTOMATISCH_EINDEUTIG.value,
            vorgang_id=vorgang_id,
            quelle_system="bank_auto_match",
        )
        return ZuordnungsErgebnis(True, "Eindeutige Vertragsreferenz gefunden.", zuordnung.id, op_row.id)

    def zuordnen_manuell(
        self,
        *,
        ctx: AuthContext,
        transaktion: BankTransaktionTable,
        konto: KontoTable,
        betrag_cent: int,
        beleg_referenz: str,
        vorgang_id: str,
    ):
        """`vorgang_id` MUSS vom Aufrufer explizit und bewusst vergeben
        werden: derselbe Wert bei einem Retry (z. B. nach einem
        Netzwerk-Timeout) macht den erneuten Aufruf zu einem sicheren
        No-Op; ein NEUER Wert für eine tatsächlich neue, unabhängige
        Teilzuordnung (auch mit zufällig identischem Betrag) erzeugt
        garantiert eine eigene Buchung statt mit einer bestehenden
        Zuordnung verwechselt zu werden."""

        require_gesellschaft_access(ctx, konto.gesellschaft_id)
        require_schreibrecht(ctx)
        op_row, zuordnung = self._zuordnen_atomar(
            ctx=ctx,
            konto=konto,
            transaktion=transaktion,
            betrag_cent=betrag_cent,
            beleg_referenz=beleg_referenz,
            match_typ=ZahlungsMatchTyp.MANUELL.value,
            vorgang_id=vorgang_id,
            quelle_system="bank_manuell",
        )
        return zuordnung

    def _zuordnen_atomar(
        self,
        *,
        ctx: AuthContext,
        konto: KontoTable,
        transaktion: BankTransaktionTable,
        betrag_cent: int,
        beleg_referenz: str,
        match_typ: str,
        vorgang_id: str,
        quelle_system: str,
    ):
        """OP-Buchung + Zuordnung in EINER DB-Transaktion: schlägt die
        Zuordnungserstellung nach der Buchung fehl (Validierungsfehler ODER
        ein unerwarteter Fehler), wird die Buchung mit zurückgerollt - es
        bleibt nie eine Zahlung ohne zugehörige Zuordnung im Ledger stehen."""

        with self._session_factory() as session:
            try:
                op_row = self._op_service.buchen(
                    ctx=ctx,
                    konto=konto,
                    typ=OPTyp.ZAHLUNG,
                    betrag_cent=betrag_cent,
                    belegdatum=transaktion.buchungsdatum,
                    buchungsdatum=transaktion.buchungsdatum,
                    faelligkeit=None,
                    beleg_referenz=beleg_referenz,
                    import_id=f"ZAHLUNG-BANK-{transaktion.id}-{vorgang_id}",
                    quelle_system=quelle_system,
                    bank_transaktion_id=transaktion.id,
                    session=session,
                )
                zuordnung = self._repository.create_zuordnung(
                    bank_transaktion_id=transaktion.id,
                    op_position_id=op_row.id,
                    betrag_cent=betrag_cent,
                    match_typ=match_typ,
                    vorgang_id=vorgang_id,
                    session=session,
                )
                session.commit()
            except Exception:
                session.rollback()
                raise
            session.refresh(op_row)
            session.refresh(zuordnung)
            return op_row, zuordnung

    def verarbeite_ruecklastschrift(
        self,
        *,
        ctx: AuthContext,
        transaktion: BankTransaktionTable,
        original_op_position: OPPositionTable,
        konto: KontoTable,
        betrag_cent: int | None = None,
    ) -> OPPositionTable:
        """Bucht eine Rücklastschrift als eigene RUECKLASTSCHRIFT-Zeile.
        Verlangt:

        - `transaktion` ist ein ECHTER negativer Bankeingang (Belastung),
          nicht irgendeine (insbesondere nicht dieselbe positive) Transaktion.
        - `transaktion` liegt auf dem GLEICHEN Bankkonto wie mindestens eine
          Zuordnung der ursprünglichen Zahlung (passendes Bankkonto).
        - Gesellschaft und Währung von Transaktion und Konto stimmen überein.
        - Der angeforderte Betrag überschreitet weder den verfügbaren
          Belastungsbetrag DIESER Transaktion (abzüglich bereits über sie
          gebuchter Rückbuchungen) noch die kumulative Rückbuchungsgrenze
          der Ursprungszahlung (abzüglich bereits gegen SIE gebuchter
          Rückbuchungen).
        """

        require_gesellschaft_access(ctx, konto.gesellschaft_id)
        require_schreibrecht(ctx)

        if original_op_position.konto_id != konto.id:
            raise BindungInkonsistentError(
                f"OPPosition {original_op_position.id} gehört zu Konto {original_op_position.konto_id}, "
                f"nicht zu {konto.id}."
            )
        if OPTyp(original_op_position.typ) is not OPTyp.ZAHLUNG:
            raise ZuordnungUngueltigError(
                f"Rücklastschrift muss sich auf eine Zahlung beziehen, OPPosition {original_op_position.id} "
                f"ist aber vom Typ {original_op_position.typ}."
            )
        if original_op_position.status != "AKTIV":
            raise ZuordnungUngueltigError(
                f"OPPosition {original_op_position.id} ist bereits storniert und kann nicht zurückgebucht werden."
            )
        zuordnungen = self._repository.list_zuordnungen_fuer_op(original_op_position.id)
        if not zuordnungen:
            raise ZuordnungUngueltigError(
                f"Zur ursprünglichen Zahlung {original_op_position.id} existiert keine Bank-Zuordnung; "
                "eine Rücklastschrift ohne belegte Ursprungszahlung wird abgelehnt."
            )

        # Muss ein ECHTER negativer Eingang sein - nicht dieselbe (oder eine
        # beliebige andere) positive Transaktion, die als "Rücklastschrift"
        # untergeschoben wird.
        if transaktion.betrag_cent >= 0:
            raise ZuordnungUngueltigError(
                f"Transaktion {transaktion.id} ist kein negativer Bankeingang (Betrag {transaktion.betrag_cent}); "
                "eine Rücklastschrift erfordert eine tatsächliche Belastung."
            )

        bank_konto = self._repository.get_bank_konto(transaktion.bank_konto_id)
        if bank_konto is None:
            raise ValueError(f"Unbekanntes Bankkonto {transaktion.bank_konto_id}")
        if bank_konto.gesellschaft_id != konto.gesellschaft_id:
            raise CrossTenantError(
                f"Rücklastschrift-Transaktion (Gesellschaft {bank_konto.gesellschaft_id}) darf nicht mit Konto "
                f"{konto.id} (Gesellschaft {konto.gesellschaft_id}) verrechnet werden."
            )
        # Passendes Bankkonto: die Rücklastschrift muss auf demselben
        # Bankkonto eingehen, auf dem auch die Ursprungszahlung verbucht wurde.
        original_bank_konto_ids = set()
        for zuordnung in zuordnungen:
            original_transaktion = self._repository.get_transaktion(zuordnung.bank_transaktion_id)
            if original_transaktion is not None:
                original_bank_konto_ids.add(original_transaktion.bank_konto_id)
        if transaktion.bank_konto_id not in original_bank_konto_ids:
            raise ZuordnungUngueltigError(
                f"Rücklastschrift-Transaktion {transaktion.id} liegt auf Bankkonto {transaktion.bank_konto_id}, "
                f"die Ursprungszahlung aber auf {sorted(original_bank_konto_ids)}; passendes Bankkonto erforderlich."
            )
        if transaktion.waehrung != konto.waehrung:
            raise FremdwaehrungNichtUnterstuetztError(
                f"Rücklastschrift-Transaktion {transaktion.id} ({transaktion.waehrung}) und Konto {konto.id} "
                f"({konto.waehrung}) haben unterschiedliche Währungen."
            )

        ursprungsbetrag = abs(original_op_position.betrag_cent)
        effektiver_betrag = betrag_cent if betrag_cent is not None else ursprungsbetrag
        if effektiver_betrag <= 0:
            raise ZuordnungUngueltigError("Rücklastschriftbetrag muss positiv sein.")

        # Kumulative Rückbuchungsgrenze der Ursprungszahlung: nie mehr
        # zurückbuchen als ursprünglich bezahlt wurde, auch nicht über
        # mehrere Teil-Rücklastschriften hinweg.
        bereits_zurueckgebucht = self._repository.kumulativ_zurueckgebucht(original_op_position.id)
        verbleibende_ruecklastgrenze = ursprungsbetrag - bereits_zurueckgebucht
        if effektiver_betrag > verbleibende_ruecklastgrenze:
            raise ZuordnungUngueltigError(
                f"Rücklastschriftbetrag {effektiver_betrag} überschreitet die verbleibende Rückbuchungsgrenze "
                f"der Ursprungszahlung {original_op_position.id} ({verbleibende_ruecklastgrenze} von "
                f"{ursprungsbetrag}, bereits {bereits_zurueckgebucht} zurückgebucht)."
            )

        # Verfügbarer Belastungsbetrag DIESER Transaktion: eine
        # Sammel-Rücklastschrift kann mehrere Ursprungszahlungen abdecken,
        # aber nie mehr als ihren eigenen (negativen) Betrag.
        bereits_verwendet = self._repository.verwendeter_betrag_rueckbuchung(transaktion.id)
        verfuegbar_auf_transaktion = abs(transaktion.betrag_cent) - bereits_verwendet
        if effektiver_betrag > verfuegbar_auf_transaktion:
            raise ZuordnungUngueltigError(
                f"Rücklastschriftbetrag {effektiver_betrag} überschreitet den verfügbaren Belastungsbetrag der "
                f"Transaktion {transaktion.id} ({verfuegbar_auf_transaktion} von {abs(transaktion.betrag_cent)}, "
                f"bereits {bereits_verwendet} verwendet)."
            )

        return self._op_service.buchen(
            ctx=ctx,
            konto=konto,
            typ=OPTyp.RUECKLASTSCHRIFT,
            betrag_cent=effektiver_betrag,
            belegdatum=transaktion.buchungsdatum,
            buchungsdatum=transaktion.buchungsdatum,
            faelligkeit=transaktion.buchungsdatum,
            beleg_referenz=f"Rücklastschrift zu OP #{original_op_position.id}",
            import_id=f"RUECKLASTSCHRIFT-{transaktion.id}-{original_op_position.id}",
            quelle_system="bank_ruecklastschrift",
            bank_transaktion_id=transaktion.id,
            bezieht_sich_auf_id=original_op_position.id,
        )

    # -- Bankvollständigkeit -------------------------------------------------
    def bankstand_alter_tage(self, bank_konto_id: str, *, heute: date | None = None) -> int | None:
        """Nur ein Hinweis (Datum der letzten importierten Zeile), KEIN
        Vollständigkeitsnachweis - ein unvollständiger Import kann trotzdem
        ein aktuelles Datum zeigen. Für Mahnentscheidungen ist
        `letzte_bankvollstaendigkeit`/`bestaetige_bankvollstaendigkeit`
        maßgeblich, nicht diese Methode."""

        letztes = self._repository.letztes_buchungsdatum(bank_konto_id)
        if letztes is None:
            return None
        return ((heute or date.today()) - letztes).days

    def bestaetige_bankvollstaendigkeit(self, *, bank_konto_id: str, bestaetigt_bis: date, bestaetigt_von: str) -> None:
        """Explizite menschliche/prozessuale Bestätigung "der Import für
        dieses Bankkonto ist lückenlos bis einschließlich `bestaetigt_bis`".
        Erst DAS darf das Mahnwesen als Beleg für Bankvollständigkeit
        akzeptieren, nicht das bloße Vorhandensein irgendeiner Zeile."""

        self._repository.bestaetige_bankvollstaendigkeit(
            bank_konto_id=bank_konto_id, bestaetigt_bis=bestaetigt_bis, bestaetigt_von=bestaetigt_von
        )

    def bankvollstaendigkeit_bestaetigt_bis(self, bank_konto_id: str) -> date | None:
        return self._repository.letzte_bankvollstaendigkeit(bank_konto_id)

    def hat_ungeklaerte_relevante_eingaenge(self, *, bank_konto_id: str, vertrag_id: str) -> bool:
        return self._repository.hat_ungeklaerte_relevante_eingaenge(bank_konto_id=bank_konto_id, vertrag_id=vertrag_id)
