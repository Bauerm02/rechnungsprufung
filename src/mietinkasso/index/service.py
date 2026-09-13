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
from decimal import ROUND_HALF_UP, Decimal

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

#: Eindeutiger Bezug einer vertraglichen Wartefrist nach dem "maßgeblichen
#: Indexereignis" (Auftrag Markus) - entweder die VPI-PERIODE selbst
#: (der referenzierte Kalendermonat) oder deren amtliche VEROEFFENTLICHUNG/
#: Abruf. Kein Rateversuch: ohne einen dieser beiden eindeutigen Bezüge
#: bleibt eine konfigurierte Wartefrist gesperrt (siehe
#: `indexautomatik/service.py::_monatslauf_klausel`).
_WARTEFRIST_BEZUEGE = frozenset({"VPI_PERIODE", "VEROEFFENTLICHUNG"})

#: Explizites Terminmodell (Codex-Rückprüfung zu 5535ae2 - "anpassungsmonat
#: ist zwingend und damit reine Schwellenklauseln ohne festen Monat sowie
#: maximal-einmal-jährlich ohne fixen Monat nicht darstellbar"). Siehe
#: `IndexKlauselTable.terminmodus`-Docstring in `infrastructure/db/tables.py`
#: für die vollständige Beschreibung je Modus und
#: `indexautomatik/service.py::_monatslauf_klausel` für die Fachlogik.
_TERMINMODI = frozenset({"FIXER_MONAT", "BEI_SCHWELLE", "INTERVALL"})


def _ist_gueltiges_jahr_monat(wert: str) -> bool:
    """Formatprüfung für 'YYYY-MM' (z. B. `basis_monat`, `letzte_anpassung_
    monat`) - kein Rateversuch bei einem unbrauchbaren/unparsbaren Wert."""

    teile = wert.split("-")
    if len(teile) != 2 or len(teile[0]) != 4:
        return False
    try:
        jahr, monat = int(teile[0]), int(teile[1])
    except ValueError:
        return False
    return jahr > 0 and 1 <= monat <= 12


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
        letzte_anpassung_monat: str | None = None,
        schwelle_prozent: Decimal = Decimal("0"),
        schwelle_inklusive: bool = True,
        daempfung_prozent: Decimal | None = None,
        vertragliche_grenze_prozent: Decimal | None = None,
        indexierbare_komponenten: list[str] | None = None,
        klausel_text: str | None = None,
        terminmodus: str | None = None,
        anpassungsmonat: int | None = None,
        mindestintervall_monate: int | None = None,
        indexwert_rundung_dezimalstellen: int | None = None,
        schwellenkorridor_rundung_dezimalstellen: int | None = None,
        wartefrist_monate_nach_indexereignis: int | None = None,
        wartefrist_bezug: str | None = None,
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
        if terminmodus is not None and terminmodus not in _TERMINMODI:
            raise ValueError(f"terminmodus muss einer von {sorted(_TERMINMODI)} sein, war {terminmodus!r}.")
        if letzte_anpassung_monat is not None and not _ist_gueltiges_jahr_monat(letzte_anpassung_monat):
            raise ValueError(f"letzte_anpassung_monat muss das Format 'YYYY-MM' haben, war {letzte_anpassung_monat!r}.")
        if anpassungsmonat is not None and not (1 <= anpassungsmonat <= 12):
            raise ValueError(f"anpassungsmonat muss 1-12 sein, war {anpassungsmonat}.")
        if mindestintervall_monate is not None and mindestintervall_monate <= 0:
            raise ValueError(f"mindestintervall_monate muss positiv sein, war {mindestintervall_monate}.")
        # Jeder Terminmodus stellt EIGENE Anforderungen an
        # anpassungsmonat/mindestintervall_monate (Codex-Rückprüfung zu
        # 5535ae2) - kein impliziter Rateversuch, welche Kombination
        # gemeint sein könnte.
        if terminmodus == "FIXER_MONAT" and (anpassungsmonat is None or mindestintervall_monate is None):
            raise ValueError(
                "Terminmodell FIXER_MONAT verlangt sowohl anpassungsmonat als auch mindestintervall_monate."
            )
        if terminmodus == "INTERVALL":
            if mindestintervall_monate is None:
                raise ValueError("Terminmodell INTERVALL verlangt mindestintervall_monate.")
            if anpassungsmonat is not None:
                raise ValueError(
                    "Terminmodell INTERVALL darf keinen fixen anpassungsmonat haben (sonst FIXER_MONAT verwenden)."
                )
        if terminmodus == "BEI_SCHWELLE" and anpassungsmonat is not None:
            raise ValueError("Terminmodell BEI_SCHWELLE darf keinen fixen anpassungsmonat haben.")
        if terminmodus is None and (anpassungsmonat is not None or mindestintervall_monate is not None):
            raise ValueError(
                "anpassungsmonat/mindestintervall_monate sind nur zusammen mit einem gesetzten terminmodus "
                "sinnvoll - ohne Terminmodell bleibt der automatische Wirksamkeitstermin ohnehin gesperrt."
            )
        if indexwert_rundung_dezimalstellen is not None and indexwert_rundung_dezimalstellen < 0:
            raise ValueError(
                f"indexwert_rundung_dezimalstellen darf nicht negativ sein, war {indexwert_rundung_dezimalstellen}."
            )
        if schwellenkorridor_rundung_dezimalstellen is not None and schwellenkorridor_rundung_dezimalstellen < 0:
            raise ValueError(
                "schwellenkorridor_rundung_dezimalstellen darf nicht negativ sein, war "
                f"{schwellenkorridor_rundung_dezimalstellen}."
            )
        # Beide zusammen oder keines - eine Wartefrist ohne eindeutigen
        # Bezug (VPI-Periode ODER Veröffentlichung) wäre ein Rateversuch,
        # ein Bezug ohne Fristlänge eine wirkungslose Angabe.
        if (wartefrist_monate_nach_indexereignis is None) != (wartefrist_bezug is None):
            raise ValueError(
                "wartefrist_monate_nach_indexereignis und wartefrist_bezug müssen zusammen (oder gar nicht) "
                "gesetzt werden - kein Rateversuch bei nur teilweise erfasster Wartefrist."
            )
        if wartefrist_monate_nach_indexereignis is not None and wartefrist_monate_nach_indexereignis <= 0:
            raise ValueError(
                f"wartefrist_monate_nach_indexereignis muss positiv sein, war {wartefrist_monate_nach_indexereignis}."
            )
        if wartefrist_bezug is not None and wartefrist_bezug not in _WARTEFRIST_BEZUEGE:
            raise ValueError(f"wartefrist_bezug muss einer von {sorted(_WARTEFRIST_BEZUEGE)} sein, war {wartefrist_bezug!r}.")
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
            # Auftrag Markus (Portal-Anforderung): eine belegte BESTEHENDE
            # Anpassungsperiode muss bei Neuanlage (z. B. Migration eines
            # Altvertrags mit bereits erfolgten Anpassungen) explizit
            # eingegeben werden können - ohne diese Angabe bleibt es bei
            # `None` (unbekannte/keine Vorbasis), NIE eine Annahme.
            letzte_anpassung_monat=letzte_anpassung_monat,
            schwelle_prozent=schwelle_prozent,
            schwelle_inklusive=schwelle_inklusive,
            daempfung_prozent=daempfung_prozent,
            vertragliche_grenze_prozent=vertragliche_grenze_prozent,
            indexierbare_komponenten=list(indexierbare_komponenten or []),
            terminmodus=terminmodus,
            anpassungsmonat=anpassungsmonat,
            mindestintervall_monate=mindestintervall_monate,
            indexwert_rundung_dezimalstellen=indexwert_rundung_dezimalstellen,
            schwellenkorridor_rundung_dezimalstellen=schwellenkorridor_rundung_dezimalstellen,
            wartefrist_monate_nach_indexereignis=wartefrist_monate_nach_indexereignis,
            wartefrist_bezug=wartefrist_bezug,
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
        neuer_wert_jahr: int | None = None,
        neuer_wert_monat: int | None = None,
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
        effektive_veraenderung = self.effektive_veraenderung_prozent(
            alter_wert=klausel.basis_wert,
            neuer_wert=neuer_wert,
            daempfung_prozent=klausel.daempfung_prozent,
            vertragliche_grenze_prozent=klausel.vertragliche_grenze_prozent,
        )
        if klausel.schwellenkorridor_rundung_dezimalstellen is not None:
            obergrenze, untergrenze = self.schwellenkorridor_grenzwerte(
                basis_wert=klausel.basis_wert, schwelle_prozent=klausel.schwelle_prozent,
                rundung_dezimalstellen=klausel.schwellenkorridor_rundung_dezimalstellen,
            )
            ueberschritten = self.ueberschreitet_schwelle_grenzwerte(
                neuer_wert, obergrenze=obergrenze, untergrenze=untergrenze, schwelle_inklusive=klausel.schwelle_inklusive
            )
        else:
            ueberschritten = self.ueberschreitet_schwelle(
                effektive_veraenderung, schwelle_prozent=klausel.schwelle_prozent, schwelle_inklusive=klausel.schwelle_inklusive
            )
        if not ueberschritten:
            # "Berechnungsbetrag weiterhin aus unverkürztem Indexquotienten"
            # (Präzisierung Markus): die Korridor-Grenzwertrundung
            # entscheidet NUR, OB überhaupt ausgelöst wird - der Betrag
            # selbst bleibt IMMER die volle, unverkürzte Veränderung.
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
            # Codex-Rückprüfung zu c01ceb2: der tatsächliche VPI-
            # Quellmonat von `neuer_wert` (falls vom Aufrufer bekannt) -
            # NICHT der Anspruchsmonat der später wirksamen Miete. Wird
            # z. B. von `umsetzung_service.py`s Klausel-
            # Basisfortschreibung benötigt.
            vpi_jahr=neuer_wert_jahr,
            vpi_monat=neuer_wert_monat,
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

    @staticmethod
    def effektive_veraenderung_prozent(
        *,
        alter_wert: Decimal,
        neuer_wert: Decimal,
        daempfung_prozent: Decimal | None,
        vertragliche_grenze_prozent: Decimal | None,
    ) -> Decimal:
        """Rohe Veränderung -> Dämpfung -> vertragliche Grenze, IN DIESER
        REIHENFOLGE - identisch zu `berechne_vorschlag`s bisheriger
        Inline-Logik, hier als wiederverwendbarer statischer Baustein
        (Codex-Rückprüfung zu 5535ae2: `indexautomatik/service.py`s Suche
        nach dem maßgeblichen Überschreitungsereignis braucht GENAU
        dieselbe Berechnung, nicht eine zweite, potenziell abweichende
        Kopie). Liefert IMMER den vollen, unverkürzten Wert - eine
        etwaige Schwellenkorridor-Rundung (siehe `schwellenkorridor_
        grenzwerte`) betrifft NUR die TRIGGER-Entscheidung, NIEMALS den
        für die Erhöhung tatsächlich verwendeten Betrag (Präzisierung
        Markus: "Berechnungsbetrag weiterhin aus unverkürztem
        Indexquotienten")."""

        rohe_veraenderung = (neuer_wert - alter_wert) / alter_wert * 100
        effektive_veraenderung = IndexService._daempfe(rohe_veraenderung, daempfung_prozent)
        if vertragliche_grenze_prozent is not None and effektive_veraenderung > vertragliche_grenze_prozent:
            effektive_veraenderung = vertragliche_grenze_prozent
        return effektive_veraenderung

    @staticmethod
    def ueberschreitet_schwelle(effektive_veraenderung: Decimal, *, schwelle_prozent: Decimal, schwelle_inklusive: bool) -> bool:
        """Schwelle wirkt auf den BETRAG der Veränderung (Betragsschwelle
        absolut, nicht gerichtet): eine Senkung um 5% ist bei Schwelle 3%
        genauso wirksam wie eine Erhöhung um 5%. Inklusive/exklusive
        Grenze ist eine explizite Vertragsklausel: "ab X%" (inklusive)
        löst schon EXAKT bei der Schwelle aus, "über X%" (exklusiv) erst
        STRIKT darüber - viele reale Verträge verlangen Letzteres."""

        if schwelle_inklusive:
            return abs(effektive_veraenderung) >= schwelle_prozent
        return abs(effektive_veraenderung) > schwelle_prozent

    @staticmethod
    def schwellenkorridor_grenzwerte(
        *, basis_wert: Decimal, schwelle_prozent: Decimal, rundung_dezimalstellen: int
    ) -> tuple[Decimal, Decimal]:
        """Ober-/Untergrenze des Schwellenkorridors in INDEXPUNKTEN (NICHT
        Prozent) - `basis_wert*(1±schwelle/100)`, gerundet auf
        `rundung_dezimalstellen` Nachkommastellen. Präzisierung Markus
        (vor Commit korrigiert): eine belegte Altvertragsklausel rundet
        die GRENZWERTE des Korridors, NICHT die daraus berechnete
        Veränderungsprozentzahl - Beispiel Basis 300, Schwelle 3% strikt:
        oberer Grenzwert = 300*1,03 = 309,0 (bereits exakt eine
        Dezimalstelle); ein amtlicher Wert 309,1 überschreitet ihn direkt
        (309,1 > 309,0), obwohl die gerundete Veränderung selbst
        fälschlich noch 3,0% ergäbe und NICHT auslösen würde - die
        Rundung der Prozentzahl ist NICHT dieselbe Rundung wie die der
        Grenzwerte (siehe `ueberschreitet_schwelle_grenzwerte`, die
        NUR gegen diese gerundeten Grenzwerte vergleicht, nie gegen eine
        gerundete Prozentzahl)."""

        quant = Decimal(1).scaleb(-rundung_dezimalstellen)
        faktor = schwelle_prozent / 100
        obergrenze = (basis_wert * (1 + faktor)).quantize(quant, rounding=ROUND_HALF_UP)
        untergrenze = (basis_wert * (1 - faktor)).quantize(quant, rounding=ROUND_HALF_UP)
        return obergrenze, untergrenze

    @staticmethod
    def ueberschreitet_schwelle_grenzwerte(
        neuer_wert: Decimal, *, obergrenze: Decimal, untergrenze: Decimal, schwelle_inklusive: bool
    ) -> bool:
        """Vergleicht den TATSÄCHLICHEN (unveränderten) amtlichen
        Indexwert direkt gegen die gerundeten Korridor-Grenzwerte - siehe
        `schwellenkorridor_grenzwerte`-Docstring für das Beispiel, warum
        das NICHT dasselbe Ergebnis liefert wie ein Vergleich der
        gerundeten Veränderungsprozentzahl gegen `schwelle_prozent`."""

        if schwelle_inklusive:
            return neuer_wert >= obergrenze or neuer_wert <= untergrenze
        return neuer_wert > obergrenze or neuer_wert < untergrenze

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
