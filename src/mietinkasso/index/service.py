"""Index-/Richtwertanpassung nach Fachregel 5.

Ausdrücklich NICHT enthalten: eine "zertifizierte" Rechtsauskunft. Diese
Klasse rechnet nur, was eine versionierte, fachlich freigegebene
IndexKlausel vorgibt (Basisreihe/-wert/-monat, Schwelle, Dämpfung,
vertragliche Grenze, indexierbare Komponenten) UND deren
`berechnungsprofil` einem hier tatsächlich implementierten Modus
entspricht. Ohne freigegebene Klausel und ohne unterstütztes Profil gibt
es keine Berechnung - insbesondere die MieWeG-2026-Spezifika
(April-Termine, Jahresdurchschnittsbildung, anteilige Erstvalorisierung,
Altvertragsübergang) sind NICHT implementiert und dürfen nicht durch die
generische Schwellen-/Dämpfungsformel überspielt werden. BK-Positionen
und Vorauszahlungen sind strukturell von der indexierbaren Menge
ausgeschlossen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from mietinkasso.auth.service import AuthContext, require_gesellschaft_access, require_schreibrecht
from mietinkasso.domain.enums import IndexAnpassungStatus, IndexKlauselStatus, rechtsordnung_geklaert
from mietinkasso.domain.exceptions import (
    IndexKlauselFehltError,
    RechtsordnungUngeklaertError,
    RechtsprofilNichtImplementiertError,
)
from mietinkasso.domain.money import cents_to_decimal, round_index_half_cent_down, to_cents
from mietinkasso.index.repository import IndexRepository
from mietinkasso.infrastructure.db.tables import IndexAnpassungTable, IndexKlauselTable
from mietinkasso.stammdaten.repository import StammdatenRepository

_NIE_INDEXIERBARE_ARTEN = {"BK_VORAUSZAHLUNG", "HEIZ_WW_VORAUSZAHLUNG"}

#: Einziges tatsächlich implementiertes Rechenprofil in MVP1: reiner
#: Schwellen-/Dämpfungsvergleich zwischen Basiswert und neuem Wert, ohne
#: April-Stichtage, Jahresdurchschnittsbildung, anteilige
#: Erstvalorisierung oder Altvertragsübergang. Jede IndexKlausel MUSS
#: dieses Profil explizit tragen, sonst wird die Berechnung verweigert.
EINFACHER_SCHWELLENVERGLEICH = "EINFACHER_SCHWELLENVERGLEICH"
UNTERSTUETZTE_BERECHNUNGSPROFILE = frozenset({EINFACHER_SCHWELLENVERGLEICH})


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

    def _vertrag_oder_fehler(self, vertrag_id: str):
        vertrag = self._stammdaten_repository.get_vertrag(vertrag_id)
        if vertrag is None:
            raise ValueError(f"Unbekannter Vertrag {vertrag_id}")
        return vertrag

    def klausel_anlegen(
        self,
        *,
        ctx: AuthContext,
        vertrag_id: str,
        rechtsordnung: str,
        berechnungsprofil: str,
        abschlussdatum: date,
        basis_reihe: str,
        basis_wert: Decimal,
        basis_monat: str,
        schwelle_prozent: Decimal = Decimal("0"),
        schwelle_inklusive: bool = True,
        daempfung_prozent: Decimal | None = None,
        vertragliche_grenze_prozent: Decimal | None = None,
        indexierbare_komponenten: list[str] | None = None,
        klausel_text: str | None = None,
    ) -> IndexKlauselTable:
        vertrag = self._vertrag_oder_fehler(vertrag_id)
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)
        self._stammdaten_repository.pruefe_vertrag_nicht_ausgeschlossen(vertrag_id)
        if not rechtsordnung_geklaert(vertrag.rechtsordnung):
            raise RechtsordnungUngeklaertError(
                f"Vertrag {vertrag_id}: Rechtsordnung ist UNGEKLAERT; eine IndexKlausel darf nicht "
                "angelegt werden, bis die rechtliche Einordnung feststeht."
            )
        version = self._repository.naechste_version(vertrag_id)
        klausel = IndexKlauselTable(
            vertrag_id=vertrag_id,
            version=version,
            rechtsordnung=rechtsordnung,
            berechnungsprofil=berechnungsprofil,
            klausel_text=klausel_text,
            abschlussdatum=abschlussdatum,
            basis_reihe=basis_reihe,
            basis_wert=basis_wert,
            basis_monat=basis_monat,
            schwelle_prozent=schwelle_prozent,
            schwelle_inklusive=schwelle_inklusive,
            daempfung_prozent=daempfung_prozent,
            vertragliche_grenze_prozent=vertragliche_grenze_prozent,
            indexierbare_komponenten=list(indexierbare_komponenten or []),
            status=IndexKlauselStatus.ENTWURF.value,
        )
        return self._repository.anlegen(klausel)

    def klausel_freigeben(self, klausel_id: int, *, ctx: AuthContext, freigegeben_von: str) -> IndexKlauselTable:
        klausel = self._repository.get_klausel(klausel_id)
        if klausel is None:
            raise ValueError(f"Unbekannte IndexKlausel {klausel_id}")
        vertrag = self._vertrag_oder_fehler(klausel.vertrag_id)
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)
        return self._repository.freigeben(klausel_id, freigegeben_von=freigegeben_von)

    def berechne_vorschlag(
        self,
        *,
        ctx: AuthContext,
        vertrag_id: str,
        stichtag: date,
        neuer_wert: Decimal,
        quelle_referenz: str,
    ) -> IndexVorschlag:
        vertrag = self._vertrag_oder_fehler(vertrag_id)
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)
        self._stammdaten_repository.pruefe_vertrag_nicht_ausgeschlossen(vertrag_id)
        if not rechtsordnung_geklaert(vertrag.rechtsordnung):
            raise RechtsordnungUngeklaertError(
                f"Vertrag {vertrag_id}: Rechtsordnung ist UNGEKLAERT; eine Index-Erhöhung ist gesperrt, "
                "bis die rechtliche Einordnung feststeht."
            )

        klausel = self._repository.freigegebene_klausel(vertrag_id)
        if klausel is None:
            raise IndexKlauselFehltError(
                f"Vertrag {vertrag_id}: keine freigegebene IndexKlausel vorhanden; "
                "eine Erhöhung ist gesperrt, solange die Klassifizierung fehlt."
            )
        if klausel.berechnungsprofil not in UNTERSTUETZTE_BERECHNUNGSPROFILE:
            raise RechtsprofilNichtImplementiertError(
                f"Vertrag {vertrag_id}: Berechnungsprofil "
                f"'{klausel.berechnungsprofil}' ist in MVP1 nicht implementiert "
                f"(z. B. April-Termine, Jahresdurchschnitt, anteilige Erstvalorisierung, "
                f"Altvertragsübergang fehlen). Unterstützt: {sorted(UNTERSTUETZTE_BERECHNUNGSPROFILE)}."
            )

        rohe_veraenderung = (neuer_wert - klausel.basis_wert) / klausel.basis_wert * 100
        effektive_veraenderung = self._daempfe(rohe_veraenderung, klausel.daempfung_prozent)

        if (
            klausel.vertragliche_grenze_prozent is not None
            and effektive_veraenderung > klausel.vertragliche_grenze_prozent
        ):
            effektive_veraenderung = klausel.vertragliche_grenze_prozent

        # Schwelle wirkt auf den BETRAG der Veränderung (Betragsschwelle
        # absolut, nicht gerichtet): eine Senkung um 5% ist bei Schwelle 3%
        # genauso wirksam wie eine Erhöhung um 5%. Inklusive/exklusive
        # Grenze ist eine explizite Vertragsklausel: "ab X%" (inklusive)
        # löst schon EXAKT bei der Schwelle aus, "über X%" (exklusiv)
        # erst STRIKT darüber - viele reale Verträge verlangen Letzteres.
        if klausel.schwelle_inklusive:
            unterhalb_schwelle = abs(effektive_veraenderung) < klausel.schwelle_prozent
        else:
            unterhalb_schwelle = abs(effektive_veraenderung) <= klausel.schwelle_prozent
        if unterhalb_schwelle:
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
                "berechnungsprofil": klausel.berechnungsprofil,
                "rohe_veraenderung_prozent": str(rohe_veraenderung),
                "effektive_veraenderung_prozent": str(effektive_veraenderung),
                "indexierbare_basis_cent": indexierbare_basis_cent,
                "schwelle_prozent": str(klausel.schwelle_prozent),
                "schwelle_inklusive": klausel.schwelle_inklusive,
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

    @staticmethod
    def _daempfe(rohe_veraenderung: Decimal, daempfung_schwelle_prozent: Decimal | None) -> Decimal:
        """Gesetzliche Dämpfung (z. B. "3%-Dämpfung"): bis zur Schwelle
        wirkt die Veränderung voll durch; JEDER Anteil darüber wird nur zur
        Hälfte wirksam ("3% plus Hälfte des darüberliegenden Anstiegs"),
        statt die Veränderung hart bei der Schwelle zu deckeln. Wirkt nur
        auf Erhöhungen (positive Veränderung); eine Senkung wird nicht
        gedämpft."""

        if daempfung_schwelle_prozent is None or rohe_veraenderung <= daempfung_schwelle_prozent:
            return rohe_veraenderung
        ueberschuss = rohe_veraenderung - daempfung_schwelle_prozent
        return daempfung_schwelle_prozent + (ueberschuss / 2)

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

    def anpassung_freigeben(self, anpassung_id: int, *, ctx: AuthContext) -> IndexAnpassungTable:
        anpassung = self._repository.get_anpassung(anpassung_id)
        if anpassung is None:
            raise ValueError(f"Unbekannte IndexAnpassung {anpassung_id}")
        vertrag = self._vertrag_oder_fehler(anpassung.vertrag_id)
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)
        if anpassung.status != IndexAnpassungStatus.VORSCHLAG.value:
            raise ValueError(
                f"IndexAnpassung {anpassung_id} ist im Status {anpassung.status}; nur ein VORSCHLAG "
                "(insbesondere kein bereits INVALIDIERTER Vorschlag) darf freigegeben werden."
            )
        with self._repository._session_factory() as session:
            row = session.get(IndexAnpassungTable, anpassung_id)
            row.status = IndexAnpassungStatus.FREIGEGEBEN.value
            session.commit()
            session.refresh(row)
            return row
