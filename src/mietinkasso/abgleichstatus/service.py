"""Zahlungs- und Abgleichstand - REIN LESEND, bei jedem GET live berechnet.

Baut ausschließlich auf einer bereits berechneten, `ctx`-geprüften
`RueckstandsUebersicht` auf (= der autorisierte Scope: nur erlaubte, nicht
ausgeschlossene Objekte/Verträge). Offene Beträge und Guthaben stammen
unverändert aus dieser Übersicht bzw. dem `OPService` - KEINE zweite
Saldo-/Verrechnungslogik.

Fachliche Grenzen:

- Abgleichnachweis = ausdrücklich erfasste Prüfung je Objekt/Bankkonto.
  Maßgeblich ist der zuletzt ERFASSTE Nachweis (`geprueft_am`, dann `id`)
  - eine spätere Korrektur darf ein früheres Prüfdatum setzen. Das
  jüngste Bankbuchungsdatum ist NIE eine Abdeckung. Ein Abgleichnachweis
  ist keine Bankvollständigkeit und keine Mahnfreigabe; die bestehende
  Bankvollständigkeit wird nur getrennt daneben angezeigt.
- Bankkonten erscheinen nur über einen ausdrücklichen Nachweis. Ein
  Nachweis, dessen Bankkonto nicht (mehr) zur Gesellschaft des Objekts
  passt, wird ignoriert und nur gezählt - kein Ersatzkonto aus der
  Gesellschaft.
- Eingereichte Einzüge sind kein Zahlungseingang: sie mindern keinen
  Saldo, markieren nichts als bezahlt und werden nie automatisch mit
  Bankzeilen abgeglichen. Ein älterer EINGEREICHT-Einzug bleibt offen
  sichtbar, auch wenn der Saldo durch einen anderen Eingang gesunken ist.
- "Letzte erfasste Zahlung" ist das Buchungsdatum der jüngsten aktiven
  ZAHLUNG-Buchung - keine Vollständigkeitsaussage. Ein "bezahlt bis" wird
  nicht abgeleitet; gezeigt werden die offenen Zeiträume der gebuchten
  Vorschreibungen."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

from mietinkasso.abgleichstatus.repository import AbgleichNachweisRepository, gespeichert_als_utc_naiv
from mietinkasso.bank.repository import BankRepository
from mietinkasso.bank.service import BankImportService
from mietinkasso.domain.enums import OPPositionStatus, OPTyp
from mietinkasso.indexautomatik.zeit import WIEN, heute_wien
from mietinkasso.op.service import OPService
from mietinkasso.rueckstaende.service import RueckstandsUebersicht
from mietinkasso.stammdaten.repository import StammdatenRepository

EINZUG_STATUS_LABEL = {
    "EINGEREICHT": "Eingereicht, Bankeingang noch offen",
    "BANKBESTAETIGT": "Bankeingang bestätigt",
    "ZURUECKGEGEBEN": "Zurückgegeben",
    "STORNIERT": "Storniert",
}


def iban_letzte4(iban: str | None) -> str:
    kompakt = "".join((iban or "").split())
    return kompakt[-4:] if len(kompakt) >= 4 else ""


@dataclass(frozen=True)
class AbgleichStand:
    """Jüngster gültiger Nachweis EINES Objekt/Bankkonto-Paars."""

    bank_konto_id: str
    bank_bezeichnung: str
    iban_letzte4: str
    geprueft_von: date
    geprueft_bis: date
    geprueft_am: datetime  # Europe/Vienna
    geprueft_durch: str
    umfang: str
    quelle_sha256: str
    fruehere_nachweise: int
    veraltet: bool  # geprueft_bis liegt vor heute
    bankvollstaendigkeit_bis: date | None  # bestehende, getrennte Bestätigung


@dataclass(frozen=True)
class ObjektAbgleich:
    objekt_id: str
    objekt_bezeichnung: str
    gesellschaft_name: str
    staende: tuple[AbgleichStand, ...]  # leer == "Noch kein Abgleichnachweis"
    ignorierte_nachweise: int


@dataclass(frozen=True)
class EinzugZeile:
    referenz: str
    betrag_cent: int
    einzug_am: date
    eingereicht_am: datetime  # Europe/Vienna
    status: str
    status_label: str
    bank_bezeichnung: str
    iban_letzte4: str

    @property
    def offen(self) -> bool:
        return self.status == "EINGEREICHT"


@dataclass(frozen=True)
class OffenerZeitraum:
    bezeichnung: str
    art: str
    rest_cent: int
    faelligkeitsklasse: str


@dataclass(frozen=True)
class VertragZahlungsstand:
    vertrag_id: str
    objekt_id: str
    objekt_bezeichnung: str
    einheit_bezeichnung: str
    debitor_name: str
    konto_id: str | None
    saldo_cent: int | None
    offen_cent: int | None
    guthaben_cent: int | None
    offene_zeitraeume: tuple[OffenerZeitraum, ...]
    letzte_erfasste_zahlung: date | None
    einzuege: tuple[EinzugZeile, ...]
    einzuege_offen_cent: int
    ignorierte_einzuege: int


@dataclass(frozen=True)
class Abgleichstatus:
    objekt_filter: str | None
    stichtag: date
    objekte: tuple[ObjektAbgleich, ...]
    vertraege: tuple[VertragZahlungsstand, ...]
    summe_offen_cent: int
    summe_guthaben_cent: int
    einzuege_offen_cent: int
    einzuege_offen_anzahl: int

    @property
    def objekte_ohne_nachweis(self) -> int:
        return sum(1 for o in self.objekte if not o.staende)

    @property
    def objekte_veraltet(self) -> int:
        return sum(1 for o in self.objekte if o.staende and any(s.veraltet for s in o.staende))

    @property
    def aeltester_stand(self) -> date | None:
        daten = [s.geprueft_bis for o in self.objekte for s in o.staende]
        return min(daten) if daten else None


def _wien(utc_naiv: datetime) -> datetime:
    return gespeichert_als_utc_naiv(utc_naiv).replace(tzinfo=timezone.utc).astimezone(WIEN)


def berechne_abgleichstatus(
    *,
    uebersicht: RueckstandsUebersicht,
    stammdaten_repository: StammdatenRepository,
    op_service: OPService,
    bank_repository: BankRepository,
    bank_service: BankImportService,
    nachweis_repository: AbgleichNachweisRepository,
    heute: date | None = None,
) -> Abgleichstatus:
    heute = heute or heute_wien()
    optionen = {o.id: o for o in uebersicht.objekt_optionen}
    ziel_objekt_ids = [uebersicht.objekt_filter] if uebersicht.objekt_filter else [o.id for o in uebersicht.objekt_optionen]

    bank_cache: dict = {}

    def _bank(bank_konto_id: str):
        if bank_konto_id not in bank_cache:
            bank_cache[bank_konto_id] = bank_repository.get_bank_konto(bank_konto_id)
        return bank_cache[bank_konto_id]

    # -- Abgleichnachweise je Objekt/Bankkonto -----------------------------
    je_objekt: dict[str, dict[str, list]] = {oid: {} for oid in ziel_objekt_ids}
    ignoriert_je_objekt: dict[str, int] = {oid: 0 for oid in ziel_objekt_ids}
    for row in nachweis_repository.list_abgleichnachweise(ziel_objekt_ids):
        bank = _bank(row.bank_konto_id)
        if bank is None or bank.gesellschaft_id != optionen[row.objekt_id].gesellschaft_id:
            ignoriert_je_objekt[row.objekt_id] += 1
            continue
        je_objekt[row.objekt_id].setdefault(row.bank_konto_id, []).append(row)

    objekte: list[ObjektAbgleich] = []
    for oid in ziel_objekt_ids:
        option = optionen[oid]
        staende: list[AbgleichStand] = []
        for bank_konto_id, rows in sorted(je_objekt[oid].items()):
            # Zuletzt ERFASSTER Nachweis gilt (Korrektur möglich) - nicht
            # das späteste geprueft_bis und nie ein Bankbuchungsdatum.
            neueste = max(rows, key=lambda r: (gespeichert_als_utc_naiv(r.geprueft_am), r.id))
            bank = _bank(bank_konto_id)
            staende.append(AbgleichStand(
                bank_konto_id=bank_konto_id, bank_bezeichnung=bank.bezeichnung, iban_letzte4=iban_letzte4(bank.iban),
                geprueft_von=neueste.geprueft_von, geprueft_bis=neueste.geprueft_bis,
                geprueft_am=_wien(neueste.geprueft_am), geprueft_durch=neueste.geprueft_durch,
                umfang=neueste.umfang, quelle_sha256=neueste.quelle_sha256, fruehere_nachweise=len(rows) - 1,
                veraltet=neueste.geprueft_bis < heute,
                bankvollstaendigkeit_bis=bank_service.bankvollstaendigkeit_bestaetigt_bis(bank_konto_id),
            ))
        objekte.append(ObjektAbgleich(
            objekt_id=oid, objekt_bezeichnung=option.bezeichnung, gesellschaft_name=option.gesellschaft_name,
            staende=tuple(staende), ignorierte_nachweise=ignoriert_je_objekt[oid],
        ))

    # -- Einzüge und Zahlungsstand je Vertrag ------------------------------
    vertrag_ids = [z.vertrag_id for z in uebersicht.mietkonten]
    einzuege_je_vertrag: dict[str, list] = {}
    einzug_rows = nachweis_repository.list_einzugnachweise(vertrag_ids)
    aktuelle_status = nachweis_repository.aktuelle_einzugsstatus([row.id for row in einzug_rows])
    for row in einzug_rows:
        einzuege_je_vertrag.setdefault(row.vertrag_id, []).append(row)

    zeitraeume_je_vertrag: dict[str, list[OffenerZeitraum]] = {}
    for p in uebersicht.offene_positionen:
        zeitraeume_je_vertrag.setdefault(p.vertrag_id, []).append(OffenerZeitraum(
            bezeichnung=p.leistungsperiode or f"Beleg vom {p.belegdatum.strftime('%d.%m.%Y')}",
            art=p.art, rest_cent=p.rest_cent, faelligkeitsklasse=p.faelligkeitsklasse,
        ))

    vertraege: list[VertragZahlungsstand] = []
    for z in uebersicht.mietkonten:
        vertrag = stammdaten_repository.get_vertrag(z.vertrag_id)
        gesellschaft_id = optionen[z.objekt_id].gesellschaft_id
        einzuege: list[EinzugZeile] = []
        ignoriert = 0
        for row in einzuege_je_vertrag.get(z.vertrag_id, []):
            bank = _bank(row.bank_konto_id)
            if (
                bank is None or vertrag is None or bank.gesellschaft_id != gesellschaft_id
                or vertrag.gesellschaft_id != gesellschaft_id
            ):
                ignoriert += 1
                continue
            status = aktuelle_status.get(row.id, row.status)
            einzuege.append(EinzugZeile(
                referenz=row.referenz, betrag_cent=row.betrag_cent, einzug_am=row.einzug_am,
                eingereicht_am=_wien(row.eingereicht_am), status=status,
                status_label=EINZUG_STATUS_LABEL.get(status, status),
                bank_bezeichnung=bank.bezeichnung, iban_letzte4=iban_letzte4(bank.iban),
            ))

        letzte_zahlung: date | None = None
        if z.konto_id is not None:
            zahlungen = [
                p.buchungsdatum for p in op_service.list_alle_positionen(z.konto_id)
                if p.typ == OPTyp.ZAHLUNG.value and p.status == OPPositionStatus.AKTIV.value and p.buchungsdatum <= heute
            ]
            letzte_zahlung = max(zahlungen) if zahlungen else None

        hat_konto = z.konto_id is not None and z.saldo_cent is not None
        vertraege.append(VertragZahlungsstand(
            vertrag_id=z.vertrag_id, objekt_id=z.objekt_id, objekt_bezeichnung=z.objekt_bezeichnung,
            einheit_bezeichnung=z.einheit_bezeichnung, debitor_name=z.debitor_name, konto_id=z.konto_id,
            saldo_cent=z.saldo_cent if hat_konto else None,
            offen_cent=max(z.saldo_cent, 0) if hat_konto else None,
            guthaben_cent=max(-z.saldo_cent, 0) if hat_konto else None,
            offene_zeitraeume=tuple(zeitraeume_je_vertrag.get(z.vertrag_id, ())),
            letzte_erfasste_zahlung=letzte_zahlung, einzuege=tuple(einzuege),
            einzuege_offen_cent=sum(e.betrag_cent for e in einzuege if e.offen), ignorierte_einzuege=ignoriert,
        ))

    return Abgleichstatus(
        objekt_filter=uebersicht.objekt_filter, stichtag=heute, objekte=tuple(objekte), vertraege=tuple(vertraege),
        summe_offen_cent=uebersicht.kennzahlen.summe_positiver_kontostaende_cent,
        summe_guthaben_cent=uebersicht.kennzahlen.summe_guthaben_cent,
        einzuege_offen_cent=sum(v.einzuege_offen_cent for v in vertraege),
        einzuege_offen_anzahl=sum(1 for v in vertraege for e in v.einzuege if e.offen),
    )
