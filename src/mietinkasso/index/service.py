"""Index-/Richtwertanpassung nach Fachregel 5.

Ausdrücklich NICHT enthalten: eine "zertifizierte" Rechtsauskunft. Diese
Klasse rechnet nur, was eine versionierte, fachlich freigegebene
IndexKlausel vorgibt (Basisreihe/-wert/-monat, Schwelle, Dämpfung,
vertragliche Grenze, indexierbare Komponenten). Ohne freigegebene
Klausel gibt es keine Berechnung. BK-Positionen und Vorauszahlungen sind
strukturell von der indexierbaren Menge ausgeschlossen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

from mietinkasso.domain.enums import IndexAnpassungStatus, IndexKlauselStatus
from mietinkasso.domain.exceptions import IndexKlauselFehltError
from mietinkasso.domain.money import cents_to_decimal, round_index_half_cent_down, to_cents
from mietinkasso.index.repository import IndexRepository
from mietinkasso.infrastructure.db.tables import IndexAnpassungTable, IndexKlauselTable
from mietinkasso.stammdaten.repository import StammdatenRepository

_NIE_INDEXIERBARE_ARTEN = {"BK_VORAUSZAHLUNG", "HEIZ_WW_VORAUSZAHLUNG"}


@dataclass(frozen=True)
class IndexVorschlag:
    id: int
    veraenderung_prozent: Decimal
    erhoehung_cent: int
    status: str


class IndexService:
    def __init__(self, repository: IndexRepository, stammdaten_repository: StammdatenRepository):
        self._repository = repository
        self._stammdaten_repository = stammdaten_repository

    def klausel_anlegen(
        self,
        *,
        vertrag_id: str,
        rechtsordnung: str,
        abschlussdatum: date,
        basis_reihe: str,
        basis_wert: Decimal,
        basis_monat: str,
        schwelle_prozent: Decimal = Decimal("0"),
        daempfung_prozent: Decimal | None = None,
        vertragliche_grenze_prozent: Decimal | None = None,
        indexierbare_komponenten: list[str] | None = None,
        klausel_text: str | None = None,
    ) -> IndexKlauselTable:
        version = self._repository.naechste_version(vertrag_id)
        klausel = IndexKlauselTable(
            vertrag_id=vertrag_id,
            version=version,
            rechtsordnung=rechtsordnung,
            klausel_text=klausel_text,
            abschlussdatum=abschlussdatum,
            basis_reihe=basis_reihe,
            basis_wert=basis_wert,
            basis_monat=basis_monat,
            schwelle_prozent=schwelle_prozent,
            daempfung_prozent=daempfung_prozent,
            vertragliche_grenze_prozent=vertragliche_grenze_prozent,
            indexierbare_komponenten=list(indexierbare_komponenten or []),
            status=IndexKlauselStatus.ENTWURF.value,
        )
        return self._repository.anlegen(klausel)

    def klausel_freigeben(self, klausel_id: int, *, freigegeben_von: str) -> IndexKlauselTable:
        return self._repository.freigeben(klausel_id, freigegeben_von=freigegeben_von)

    def berechne_vorschlag(
        self,
        *,
        vertrag_id: str,
        stichtag: date,
        neuer_wert: Decimal,
        quelle_referenz: str,
    ) -> IndexVorschlag:
        klausel = self._repository.freigegebene_klausel(vertrag_id)
        if klausel is None:
            raise IndexKlauselFehltError(
                f"Vertrag {vertrag_id}: keine freigegebene IndexKlausel vorhanden; "
                "eine Erhöhung ist gesperrt, solange die Klassifizierung fehlt."
            )

        rohe_veraenderung = (neuer_wert - klausel.basis_wert) / klausel.basis_wert * 100
        effektive_veraenderung = rohe_veraenderung
        if klausel.daempfung_prozent is not None and effektive_veraenderung > klausel.daempfung_prozent:
            effektive_veraenderung = klausel.daempfung_prozent
        if (
            klausel.vertragliche_grenze_prozent is not None
            and effektive_veraenderung > klausel.vertragliche_grenze_prozent
        ):
            effektive_veraenderung = klausel.vertragliche_grenze_prozent
        if effektive_veraenderung < klausel.schwelle_prozent:
            effektive_veraenderung = Decimal("0")

        indexierbare_basis_cent = self._indexierbare_basis_cent(vertrag_id, klausel, stichtag)
        erhoehung_decimal = cents_to_decimal(indexierbare_basis_cent) * effektive_veraenderung / 100
        erhoehung_gerundet = round_index_half_cent_down(erhoehung_decimal)
        erhoehung_cent = to_cents(erhoehung_gerundet)

        anpassung = IndexAnpassungTable(
            index_klausel_id=klausel.id,
            index_klausel_version=klausel.version,
            vertrag_id=vertrag_id,
            stichtag=stichtag,
            alter_wert=klausel.basis_wert,
            neuer_wert=neuer_wert,
            veraenderung_prozent=effektive_veraenderung,
            erhoehung_cent=erhoehung_cent,
            status=IndexAnpassungStatus.VORSCHLAG.value,
            quelle_referenz=quelle_referenz,
            berechnungs_snapshot={
                "rohe_veraenderung_prozent": str(rohe_veraenderung),
                "effektive_veraenderung_prozent": str(effektive_veraenderung),
                "indexierbare_basis_cent": indexierbare_basis_cent,
                "schwelle_prozent": str(klausel.schwelle_prozent),
                "daempfung_prozent": str(klausel.daempfung_prozent) if klausel.daempfung_prozent is not None else None,
            },
        )
        gespeichert = self._repository.speichere_anpassung(anpassung)
        return IndexVorschlag(
            id=gespeichert.id,
            veraenderung_prozent=effektive_veraenderung,
            erhoehung_cent=erhoehung_cent,
            status=gespeichert.status,
        )

    def _indexierbare_basis_cent(self, vertrag_id: str, klausel: IndexKlauselTable, stichtag: date) -> int:
        komponenten = self._stammdaten_repository.list_aktive_komponenten(vertrag_id, stichtag)
        erlaubte_arten = set(klausel.indexierbare_komponenten) if klausel.indexierbare_komponenten else None
        summe = 0
        for komponente in komponenten:
            if not komponente.indexierbar:
                continue
            if komponente.art in _NIE_INDEXIERBARE_ARTEN:
                continue
            if erlaubte_arten is not None and komponente.art not in erlaubte_arten:
                continue
            summe += komponente.betrag_cent
        return summe

    def anpassung_freigeben(self, anpassung_id: int) -> IndexAnpassungTable:
        anpassung = self._repository.get_anpassung(anpassung_id)
        if anpassung is None:
            raise ValueError(f"Unbekannte IndexAnpassung {anpassung_id}")
        with self._repository._session_factory() as session:
            row = session.get(IndexAnpassungTable, anpassung_id)
            row.status = IndexAnpassungStatus.FREIGEGEBEN.value
            session.commit()
            session.refresh(row)
            return row
