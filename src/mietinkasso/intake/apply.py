"""Schreibender Einstieg des generischen Echtbetrieb-Intakes (Auftrag
HV-20260912-ECHTBETRIEB). Siehe `docs/hausverwaltung/IMPORT_VERTRAG.md`.

`wende_an` bindet sich an den Plan-Hash aus dem vorherigen `plan()`-Lauf,
prüft das Paket UNMITTELBAR vor dem Schreiben ERNEUT (nie einen alten
Plan blind vertrauen) und schreibt die GESAMTE Datei in EINER
DB-Transaktion - schlägt irgendeine Zeile fehl, wird ALLES
zurückgerollt (analog zu `op/eroeffnung_import.py::
importiere_eroeffnung_csv_atomar` und `bank/service.py::
BankImportService._importiere_atomar`, demselben etablierten Muster
dieser Codebasis)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.auth.service import AuthContext
from mietinkasso.domain.enums import OPTyp, Rolle
from mietinkasso.domain.exceptions import IntakeNichtAnwendbarError
from mietinkasso.infrastructure.db.tables import VertragsKomponenteTable, VertragTable
from mietinkasso.intake.planner import IntakePlan, pruefe_paket, sperre_ist_bereits_aktiv
from mietinkasso.intake.schema import IntakePaket, paket_hash
from mietinkasso.op.service import OPService
from mietinkasso.stammdaten.repository import StammdatenRepository

_STANDARD_AUSGESCHLOSSENE_OBJEKTE = frozenset({"107"})


@dataclass(frozen=True)
class IntakeErgebnis:
    paket_hash: str
    plan: IntakePlan
    anzahl_gesellschaften: int
    anzahl_objekte: int
    anzahl_einheiten: int
    anzahl_debitoren: int
    anzahl_vertraege: int
    anzahl_eroeffnungen: int
    anzahl_nachbuchungen: int
    anzahl_eroeffnungskorrekturen: int
    anzahl_sperren: int
    anzahl_komponenten: int


def wende_an(
    paket: IntakePaket,
    *,
    bestaetigter_hash: str,
    stammdaten_repo: StammdatenRepository,
    op_service: OPService,
    session_factory: sessionmaker[Session],
    akteur: str,
    ausgeschlossene_objekte: frozenset[str] = _STANDARD_AUSGESCHLOSSENE_OBJEKTE,
    heute: date | None = None,
) -> IntakeErgebnis:
    aktueller_hash = paket_hash(paket)
    if aktueller_hash != bestaetigter_hash:
        raise ValueError(
            f"Bestätigter Hash ({bestaetigter_hash}) stimmt nicht mit dem aktuellen Paketinhalt "
            f"({aktueller_hash}) überein - die Datei hat sich seit dem letzten plan()-Lauf geändert. "
            "Nichts wurde eingespielt; bitte erneut planen und den NEUEN Hash bestätigen."
        )

    ctx = AuthContext(user_id=akteur, rolle=Rolle.ADMIN, gesellschaft_ids=None)
    uebernahmetag = heute or date.today()

    with session_factory() as session:
        try:
            plan = pruefe_paket(paket, session=session, ausgeschlossene_objekte=ausgeschlossene_objekte)
            if not plan.anwendbar:
                gruende = "; ".join(f"{b.entitaet} {b.id}: {b.grund}" for b in (*plan.konflikte, *plan.gesperrt))
                raise IntakeNichtAnwendbarError(
                    f"Paket enthält Konflikte/Sperren, nichts wurde eingespielt: {gruende}"
                )

            for z in paket.gesellschaften:
                stammdaten_repo.upsert_gesellschaft(id=z.id, name=z.name, session=session)

            for z in paket.objekte:
                stammdaten_repo.upsert_objekt(
                    id=z.id, gesellschaft_id=z.gesellschaft_id, bezeichnung=z.bezeichnung,
                    adresse=z.adresse, ausgeschlossen=z.ausgeschlossen, session=session,
                )

            for z in paket.einheiten:
                stammdaten_repo.upsert_einheit(
                    id=z.id, objekt_id=z.objekt_id, bezeichnung=z.bezeichnung,
                    nutzungsstatus=z.nutzungsstatus, flaeche_qm=z.flaeche_qm,
                    miteigentumsanteile=z.miteigentumsanteile, session=session,
                )

            for z in paket.debitoren:
                stammdaten_repo.upsert_debitor(id=z.id, name=z.name, email=z.email, adresse=z.adresse, session=session)

            for z in paket.vertraege:
                stammdaten_repo.upsert_vertrag(
                    id=z.id, einheit_id=z.einheit_id, debitor_id=z.debitor_id, gesellschaft_id=z.gesellschaft_id,
                    rechtsordnung=z.rechtsordnung, gueltig_von=z.gueltig_von, gueltig_bis=z.gueltig_bis,
                    faelligkeit_tag=z.faelligkeit_tag, zahlungsfrist_tage=z.zahlungsfrist_tage, session=session,
                )

            konten_je_vertrag: dict[str, object] = {}

            def _konto_fuer(vertrag_id: str):
                if vertrag_id in konten_je_vertrag:
                    return konten_je_vertrag[vertrag_id]
                vertrag = session.get(VertragTable, vertrag_id)
                if vertrag is None:
                    raise ValueError(f"Vertrag '{vertrag_id}' nicht auffindbar (inkonsistenter Zustand nach Stammdaten-Anlage).")
                konto = stammdaten_repo.get_or_create_konto(vertrag=vertrag, session=session)
                konten_je_vertrag[vertrag_id] = konto
                return konto

            for z in paket.eroeffnungen:
                konto = _konto_fuer(z.vertrag_id)
                if z.modus == "GESAMTSALDO":
                    op_service.eroeffnen_gesamtsaldo(
                        ctx=ctx, konto=konto, betrag_cent=z.betrag_cent, stichtag=z.stichtag,
                        import_id=z.import_id, akteur=akteur, session=session,
                    )
                else:
                    op_service.eroeffnen_einzel_op(
                        ctx=ctx, konto=konto, stichtag=z.stichtag, import_id=z.import_id,
                        typ=OPTyp(z.typ), betrag_cent=z.betrag_cent, belegdatum=z.belegdatum or z.stichtag,
                        faelligkeit=z.faelligkeit, beleg_referenz=z.beleg_referenz, akteur=akteur, session=session,
                    )

            for z in paket.nachbuchungen:
                konto = _konto_fuer(z.vertrag_id)
                op_service.buchen(
                    ctx=ctx, konto=konto, typ=OPTyp(z.typ), betrag_cent=z.betrag_cent,
                    belegdatum=z.belegdatum, buchungsdatum=z.buchungsdatum, faelligkeit=z.faelligkeit,
                    beleg_referenz=z.beleg_referenz, aenderungsgrund=z.aenderungsgrund,
                    leistungsperiode=z.leistungsperiode, import_id=z.import_id,
                    quelle_system="intake_import", session=session,
                )

            for z in paket.eroeffnungskorrekturen:
                konto = _konto_fuer(z.vertrag_id)
                op_service.eroeffnungskorrektur_buchen(
                    ctx=ctx, konto=konto, typ=OPTyp(z.typ), betrag_cent=z.betrag_cent,
                    original_belegdatum=z.original_belegdatum, uebernahmetag=uebernahmetag,
                    grund=z.grund, quelle_referenz=z.quelle_referenz, import_id=z.import_id,
                    beleg_referenz=z.beleg_referenz, session=session,
                )

            for z in paket.sperren:
                if not sperre_ist_bereits_aktiv(session, vertrag_id=z.vertrag_id, grund=z.grund, kommentar=z.kommentar):
                    stammdaten_repo.sperre_setzen(
                        vertrag_id=z.vertrag_id, grund=z.grund, kommentar=z.kommentar, session=session,
                    )

            for z in paket.komponenten:
                if session.get(VertragsKomponenteTable, z.id) is None:
                    stammdaten_repo.add_komponente(
                        id=z.id, vertrag_id=z.vertrag_id, art=z.art, bezeichnung=z.bezeichnung,
                        betrag_cent=z.betrag_cent, ust_satz_promille=z.ust_satz_promille,
                        indexierbar=z.indexierbar, gueltig_von=z.gueltig_von, gueltig_bis=z.gueltig_bis,
                        session=session,
                    )

            session.commit()
        except Exception as exc:
            session.rollback()
            exc.args = (
                f"Echtbetrieb-Intake abgebrochen ({paket.quelle}): {exc}. Die gesamte Datei wurde NICHT "
                "eingespielt (atomarer Lauf); nach Korrektur kann der volle Lauf gefahrlos wiederholt werden "
                "(erneut planen, neuen Hash bestätigen).",
            )
            raise

    return IntakeErgebnis(
        paket_hash=aktueller_hash,
        plan=plan,
        anzahl_gesellschaften=len(paket.gesellschaften),
        anzahl_objekte=len(paket.objekte),
        anzahl_einheiten=len(paket.einheiten),
        anzahl_debitoren=len(paket.debitoren),
        anzahl_vertraege=len(paket.vertraege),
        anzahl_eroeffnungen=len(paket.eroeffnungen),
        anzahl_nachbuchungen=len(paket.nachbuchungen),
        anzahl_eroeffnungskorrekturen=len(paket.eroeffnungskorrekturen),
        anzahl_sperren=len(paket.sperren),
        anzahl_komponenten=len(paket.komponenten),
    )
