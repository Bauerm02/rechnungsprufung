"""Kontoabdeckung und Vertrags-Prüfseite - REIN LESEND (Auftrag Markus 01.11.2026).

Erweitert `abgleichstatus.service` um zwei Dinge:

- `pruefe_kontoabdeckung`: vereinigt bestätigte Perioden GENAU EINER Konto-ID
  und weist konkrete Lücken im angefragten Zeitraum aus. Als Nachweis zählt nur
  eine `BelegtePeriode` mit Beleg-ID, gültigem SHA-256 und vollständigem Erfolg.
  Teilfehler, jüngste Bankbuchung und Dateiname sind kein Nachweis. Was nicht
  belegt ist, bleibt UNBEKANNT und ist keine Aussage "keine Zahlung".
- `berechne_vertragscheck`: stellt für einen autorisierten Vertrag vorhandene
  Daten und konkrete Beleg-/Zuordnungslücken nebeneinander. Der Saldo bleibt das
  unveränderte `OPService`-Ergebnis aus `berechne_abgleichstatus`.

Derzeit speichert das Schema KEINE beweisbaren Kontoperioden:
`bank_vollstaendigkeit` kennt nur ein "bestätigt bis" ohne Beginn und Beleg, ein
`abgleich_nachweise`-Eintrag ist je Objekt eine Prüfung der Mieteingänge ohne
Erfolgsangabe und ausdrücklich keine Bankvollständigkeit. Beide werden getrennt
angezeigt und NICHT in Perioden umgedeutet; der Aufrufer übergibt deshalb heute
keine Perioden und die Seite zeigt "belegte Perioden fehlen".

Hinweise sind rein berechnet: der Schlüssel hängt nur an den fachlichen Eckdaten,
dedupliziert die Anzeige und wird nirgends gespeichert. Hier entsteht weder eine
Aufgabe noch eine Mahnfreigabe, und es gibt bewusst kein Fremdzahler-Urteil:
der Zahlername laut Bank wird angezeigt, aber nie mit dem Mieter verglichen."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from hashlib import sha256

from mietinkasso.abgleichstatus.repository import AbgleichNachweisRepository
from mietinkasso.abgleichstatus.service import (
    AbgleichStand,
    VertragZahlungsstand,
    berechne_abgleichstatus,
    iban_letzte4,
)
from mietinkasso.auth.service import AuthContext
from mietinkasso.bank.referenzen import parse_vertragskennungen
from mietinkasso.bank.repository import BankRepository
from mietinkasso.bank.service import BankImportService
from mietinkasso.domain.enums import OPPositionStatus, OPTyp
from mietinkasso.domain.exceptions import MietinkassoError
from mietinkasso.indexautomatik.zeit import heute_wien
from mietinkasso.mahnwesen.repository import MahnFallRepository, VertragMitmieterRepository
from mietinkasso.op.service import OPService
from mietinkasso.rueckstaende.service import (
    MietkontoZeile,
    UnbekanntesObjektFilterError,
    berechne_rueckstandsuebersicht,
)
from mietinkasso.stammdaten.repository import StammdatenRepository

_SHA256 = re.compile(r"[0-9a-f]{64}")

STATUS_VOLLSTAENDIG_BELEGT = "VOLLSTAENDIG_BELEGT"
STATUS_TEILWEISE_BELEGT = "TEILWEISE_BELEGT"
STATUS_UNBEKANNT = "UNBEKANNT"


class VertragNichtVerfuegbarError(MietinkassoError):
    """Eine Meldung für unbekannt, fremd und ausgeschlossen - wie
    `rueckstaende.service.UnbekanntesObjektFilterError`."""


@dataclass(frozen=True)
class BelegtePeriode:
    """Bestätigte Periode eines Kontos. `vollstaendig_erfolgreich` muss exakt
    `True` sein; ein Lauf mit Teilfehler belegt gar nichts."""

    bank_konto_id: str
    von: date
    bis: date
    beleg_id: str
    beleg_sha256: str
    vollstaendig_erfolgreich: bool


@dataclass(frozen=True)
class VerworfenePeriode:
    beleg_id: str
    grund: str  # FREMDES_KONTO | TEILFEHLER | BELEG_FEHLT | HASH_UNGUELTIG | ZEITRAUM_UNGUELTIG | HASH_KONFLIKT


@dataclass(frozen=True)
class Abdeckungsintervall:
    von: date
    bis: date
    belege: tuple[tuple[str, str], ...]  # (Beleg-ID, SHA-256), sortiert


@dataclass(frozen=True)
class Abdeckungsluecke:
    von: date
    bis: date


@dataclass(frozen=True)
class Kontoabdeckung:
    bank_konto_id: str
    angefragt_von: date
    angefragt_bis: date
    status: str
    abgedeckt: tuple[Abdeckungsintervall, ...]
    luecken: tuple[Abdeckungsluecke, ...]
    verworfen: tuple[VerworfenePeriode, ...]


def _verwerfungsgrund(periode: BelegtePeriode, bank_konto_id: str) -> str | None:
    if periode.bank_konto_id != bank_konto_id:
        return "FREMDES_KONTO"
    if periode.vollstaendig_erfolgreich is not True:
        return "TEILFEHLER"
    if not isinstance(periode.beleg_id, str) or not periode.beleg_id.strip():
        return "BELEG_FEHLT"
    if not isinstance(periode.beleg_sha256, str) or not _SHA256.fullmatch(periode.beleg_sha256):
        return "HASH_UNGUELTIG"
    if periode.von > periode.bis:
        return "ZEITRAUM_UNGUELTIG"
    return None


def pruefe_kontoabdeckung(
    *, bank_konto_id: str, von: date, bis: date, perioden: Iterable[BelegtePeriode],
) -> Kontoabdeckung:
    """Rein funktional. Der Kontovergleich ist exakt: keine Normalisierung, kein
    Präfix, kein Ersatzkonto. Überlappende und direkt anschließende Perioden
    werden vereinigt. Eine Beleg-ID mit widersprüchlichem Hash zählt gar nicht."""

    if von > bis:
        raise ValueError("Zeitraum: 'von' liegt nach 'bis'.")

    verworfen: list[VerworfenePeriode] = []
    gueltig: list[BelegtePeriode] = []
    for periode in perioden:
        grund = _verwerfungsgrund(periode, bank_konto_id)
        if grund is None:
            gueltig.append(periode)
        else:
            verworfen.append(VerworfenePeriode(str(periode.beleg_id), grund))

    hashes_je_beleg: dict[str, set[str]] = {}
    for periode in gueltig:
        hashes_je_beleg.setdefault(periode.beleg_id, set()).add(periode.beleg_sha256)
    konflikte = {beleg for beleg, hashes in hashes_je_beleg.items() if len(hashes) > 1}
    verworfen.extend(VerworfenePeriode(beleg, "HASH_KONFLIKT") for beleg in sorted(konflikte))
    gueltig = [p for p in gueltig if p.beleg_id not in konflikte]

    im_fenster = sorted(
        ((max(p.von, von), min(p.bis, bis), p) for p in gueltig if p.von <= bis and p.bis >= von),
        key=lambda eintrag: (eintrag[0], eintrag[1], eintrag[2].beleg_id),
    )
    abgedeckt: list[Abdeckungsintervall] = []
    for start, ende, periode in im_fenster:
        beleg = (periode.beleg_id, periode.beleg_sha256)
        if abgedeckt and start.toordinal() <= abgedeckt[-1].bis.toordinal() + 1:
            letztes = abgedeckt[-1]
            abgedeckt[-1] = Abdeckungsintervall(
                letztes.von, max(letztes.bis, ende), tuple(sorted({*letztes.belege, beleg})),
            )
        else:
            abgedeckt.append(Abdeckungsintervall(start, ende, (beleg,)))

    luecken: list[Abdeckungsluecke] = []
    # Ordinals can represent the sentinel after date.max without overflow.
    naechster_tag = von.toordinal()
    for intervall in abgedeckt:
        if intervall.von.toordinal() > naechster_tag:
            luecken.append(Abdeckungsluecke(date.fromordinal(naechster_tag), intervall.von - timedelta(days=1)))
        naechster_tag = intervall.bis.toordinal() + 1
    if naechster_tag <= bis.toordinal():
        luecken.append(Abdeckungsluecke(date.fromordinal(naechster_tag), bis))

    if not abgedeckt:
        status = STATUS_UNBEKANNT
    elif luecken:
        status = STATUS_TEILWEISE_BELEGT
    else:
        status = STATUS_VOLLSTAENDIG_BELEGT
    return Kontoabdeckung(
        bank_konto_id=bank_konto_id, angefragt_von=von, angefragt_bis=bis, status=status,
        abgedeckt=tuple(abgedeckt), luecken=tuple(luecken), verworfen=tuple(verworfen),
    )


# -- Vertrags-Prüfseite -------------------------------------------------------


@dataclass(frozen=True)
class Hinweis:
    schluessel: str
    art: str
    text: str


def _hinweis(vertrag_id: str, art: str, referenz: str, text: str) -> Hinweis:
    roh = "\x1f".join((vertrag_id, art, referenz)).encode("utf-8")
    return Hinweis(schluessel=sha256(roh).hexdigest()[:16], art=art, text=text)


@dataclass(frozen=True)
class Teilzahlung:
    bezeichnung: str
    betrag_cent: int
    rest_cent: int


@dataclass(frozen=True)
class ZahlungMitBank:
    """Gebuchte Zahlung mit Bankbezug. `zahler_laut_bank` ist die ungeprüfte
    Bankangabe; ob es ein Fremdzahler ist, entscheidet ein Mensch."""

    op_position_id: int
    buchungsdatum: date
    betrag_cent: int
    zahler_laut_bank: str | None
    weitere_konten_derselben_bankzeile: int


@dataclass(frozen=True)
class Bankkandidat:
    """Noch nicht zugeordnete Bankzeile, deren Referenz diese Vertragskennung
    exakt nennt. Kein Zahlungseingang im Saldo."""

    bank_transaktion_id: int
    buchungsdatum: date
    betrag_cent: int
    offener_rest_unbestimmt: bool
    genannte_vertraege: int


@dataclass(frozen=True)
class Ruecklastschrift:
    op_position_id: int
    buchungsdatum: date
    betrag_cent: int
    beleg_referenz: str | None


@dataclass(frozen=True)
class KautionStand:
    """Vereinbart und eingegangen bleiben getrennt und außerhalb des Saldos."""

    vereinbart_cent: int | None
    vereinbart_beleg: str | None
    eingang_cent: int | None
    eingang_stichtag: date | None
    eingang_referenz: str | None
    abgeschlossen_am: date | None


@dataclass(frozen=True)
class WeiterePartei:
    rolle: str
    name: str | None
    zustand: str


@dataclass(frozen=True)
class VertragsCheck:
    vertrag_id: str
    stichtag: date
    von: date
    bis: date
    zahlungsstand: VertragZahlungsstand
    faelliger_unstrittiger_rest_cent: int | None
    abweichung_saldo_zu_positionen_cent: int | None
    bank_konto_id: str | None
    bank_bezeichnung: str | None
    bank_iban_letzte4: str | None
    abdeckung: Kontoabdeckung | None
    bankvollstaendigkeit_bis: date | None
    abgleich_staende: tuple[AbgleichStand, ...]
    teilzahlungen: tuple[Teilzahlung, ...]
    zahlungen_mit_bank: tuple[ZahlungMitBank, ...]
    zahlungen_ohne_bankbezug: int
    kandidaten: tuple[Bankkandidat, ...]
    ruecklastschriften: tuple[Ruecklastschrift, ...]
    kaution: KautionStand
    weitere_parteien: tuple[WeiterePartei, ...]
    weitere_vertraege_desselben_debitors: tuple[str, ...]
    hinweise: tuple[Hinweis, ...]


def _bank_konto_fuer_vertrag(bank_repository: BankRepository, zahlungsziel_konto_id: str | None, gesellschaft_id: str):
    """`zahlungsziel_konto_id` ist das Ergebnis der bestehenden eindeutigen
    Auflösung; es wird erneut gegen die Gesellschaft geprüft."""

    if zahlungsziel_konto_id is None:
        return None
    bank = bank_repository.get_bank_konto(zahlungsziel_konto_id)
    return bank if bank is not None and bank.gesellschaft_id == gesellschaft_id else None


def berechne_vertragscheck(
    *,
    ctx: AuthContext,
    vertrag_id: str,
    stammdaten_repository: StammdatenRepository,
    op_service: OPService,
    bank_repository: BankRepository,
    bank_service: BankImportService,
    nachweis_repository: AbgleichNachweisRepository,
    mahn_fall_repository: MahnFallRepository,
    mitmieter_repository: VertragMitmieterRepository,
    zahlungsziel_konto_id: str | None,
    von: date | None = None,
    bis: date | None = None,
    belegte_perioden: Sequence[BelegtePeriode] = (),
    heute: date | None = None,
) -> VertragsCheck:
    heute = heute or heute_wien()
    nicht_verfuegbar = VertragNichtVerfuegbarError("Vertrag ist unbekannt, nicht zugänglich oder ausgeschlossen.")

    vertrag = stammdaten_repository.get_vertrag(vertrag_id)
    if vertrag is None or not ctx.has_zugriff(vertrag.gesellschaft_id):
        raise nicht_verfuegbar
    einheit = stammdaten_repository.get_einheit(vertrag.einheit_id)
    objekt = stammdaten_repository.get_objekt(einheit.objekt_id) if einheit else None
    if objekt is None or objekt.gesellschaft_id != vertrag.gesellschaft_id:
        raise nicht_verfuegbar
    try:
        uebersicht = berechne_rueckstandsuebersicht(
            ctx=ctx, objekt_id=objekt.id, stammdaten_repository=stammdaten_repository, op_service=op_service,
            mahn_fall_repository=mahn_fall_repository, heute=heute,
        )
    except UnbekanntesObjektFilterError:
        raise nicht_verfuegbar from None
    status = berechne_abgleichstatus(
        uebersicht=uebersicht, stammdaten_repository=stammdaten_repository, op_service=op_service,
        bank_repository=bank_repository, bank_service=bank_service, nachweis_repository=nachweis_repository, heute=heute,
    )
    stand = next((v for v in status.vertraege if v.vertrag_id == vertrag_id), None)
    zeile: MietkontoZeile | None = next((z for z in uebersicht.mietkonten if z.vertrag_id == vertrag_id), None)
    if stand is None or zeile is None:
        raise nicht_verfuegbar

    von = von or vertrag.gueltig_von
    bis = bis or heute
    if von > bis:
        raise ValueError("Zeitraum: 'von' liegt nach 'bis'.")

    hinweise: dict[str, Hinweis] = {}

    def melde(art: str, referenz: str, text: str) -> None:
        hinweis = _hinweis(vertrag_id, art, referenz, text)
        hinweise.setdefault(hinweis.schluessel, hinweis)

    # -- Kontoabdeckung -----------------------------------------------------
    bank = _bank_konto_fuer_vertrag(bank_repository, zahlungsziel_konto_id, vertrag.gesellschaft_id)
    abdeckung: Kontoabdeckung | None = None
    bankvollstaendigkeit_bis: date | None = None
    if bank is None:
        melde(
            "BANKKONTO_NICHT_EINDEUTIG", "",
            "Kein eindeutig zugeordnetes Bankkonto: Bankabdeckung bleibt unbekannt, es wird kein Konto angenommen.",
        )
    else:
        abdeckung = pruefe_kontoabdeckung(bank_konto_id=bank.id, von=von, bis=bis, perioden=belegte_perioden)
        bankvollstaendigkeit_bis = bank_service.bankvollstaendigkeit_bestaetigt_bis(bank.id)
        if abdeckung.status == STATUS_UNBEKANNT:
            melde(
                "BELEGTE_PERIODEN_FEHLEN", bank.id,
                "Belegte Perioden fehlen: für dieses Bankkonto liegt im angefragten Zeitraum kein Periodennachweis vor.",
            )
        else:
            for luecke in abdeckung.luecken:
                melde(
                    "ABDECKUNGSLUECKE", f"{bank.id}|{luecke.von}|{luecke.bis}",
                    f"Bankabdeckung unbekannt von {luecke.von:%d.%m.%Y} bis {luecke.bis:%d.%m.%Y}.",
                )
        for verworfen in abdeckung.verworfen:
            melde(
                "PERIODE_VERWORFEN", f"{verworfen.beleg_id}|{verworfen.grund}",
                f"Periodennachweis „{verworfen.beleg_id}“ nicht berücksichtigt ({verworfen.grund}).",
            )

    # -- OP-Daten (nur Anzeige; Saldo bleibt das OPService-Ergebnis) --------
    positionen = op_service.list_alle_positionen(stand.konto_id) if stand.konto_id else []
    aktiv = [p for p in positionen if p.status == OPPositionStatus.AKTIV.value and p.buchungsdatum <= heute]

    teilzahlungen = tuple(
        Teilzahlung(p.leistungsperiode or f"Beleg vom {p.belegdatum:%d.%m.%Y}", p.betrag_cent, p.rest_cent)
        for p in uebersicht.offene_positionen
        if p.vertrag_id == vertrag_id and 0 < p.rest_cent < p.betrag_cent
    )
    for p in uebersicht.offene_positionen:
        if p.vertrag_id == vertrag_id and p.faelligkeitsklasse == "UNBEKANNT":
            melde(
                "FAELLIGKEIT_UNBEKANNT", str(p.op_position_id),
                f"Position {p.leistungsperiode or p.beleg_referenz}: Fälligkeit nicht bekannt.",
            )

    def vertrag_sichtbar(v) -> bool:
        if v is None or not ctx.has_zugriff(v.gesellschaft_id):
            return False
        e = stammdaten_repository.get_einheit(v.einheit_id)
        o = stammdaten_repository.get_objekt(e.objekt_id) if e else None
        return bool(o and not o.ausgeschlossen and o.gesellschaft_id == v.gesellschaft_id)

    zahlungen_mit_bank: list[ZahlungMitBank] = []
    zahlungen_ohne_bankbezug = 0
    for p in aktiv:
        if p.typ != OPTyp.ZAHLUNG.value:
            continue
        zuordnungen = bank_repository.list_zuordnungen_fuer_op(p.id)
        transaktion_ids = {z.bank_transaktion_id for z in zuordnungen}
        if p.bank_transaktion_id is not None:
            transaktion_ids.add(p.bank_transaktion_id)
        if not transaktion_ids:
            zahlungen_ohne_bankbezug += 1
            continue
        zahler: set[str] = set()
        fremde_konten: set[str] = set()
        for transaktion_id in transaktion_ids:
            transaktion = bank_repository.get_transaktion(transaktion_id)
            quellkonto = bank_repository.get_bank_konto(transaktion.bank_konto_id) if transaktion else None
            if quellkonto is None or quellkonto.gesellschaft_id != vertrag.gesellschaft_id:
                melde("BANKBEZUG_UNGUELTIG", f"{p.id}|{transaktion_id}",
                      "Eine gebuchte Zahlung hat einen nicht verifizierbaren Bankbezug; Metadaten werden nicht angezeigt.")
                continue
            if transaktion.gegenkonto_name:
                zahler.add(transaktion.gegenkonto_name)
            for zuordnung in bank_repository.list_zuordnungen(transaktion_id):
                ziel = op_service.get_position(zuordnung.op_position_id)
                if ziel is not None and ziel.konto_id != stand.konto_id:
                    zielkonto = stammdaten_repository.get_konto(ziel.konto_id)
                    zielvertrag = stammdaten_repository.get_vertrag(zielkonto.vertrag_id) if zielkonto else None
                    if vertrag_sichtbar(zielvertrag) and zielvertrag.gesellschaft_id == vertrag.gesellschaft_id:
                        fremde_konten.add(ziel.konto_id)
                    else:
                        melde("ZUORDNUNGSBEZUG_UNGEKLAERT", str(p.id),
                              "Eine weitere Zahlungszuordnung ist nicht im zugänglichen Verwaltungsbestand verifizierbar.")
        zahlungen_mit_bank.append(ZahlungMitBank(
            op_position_id=p.id, buchungsdatum=p.buchungsdatum, betrag_cent=p.betrag_cent,
            zahler_laut_bank=", ".join(sorted(zahler)) or None, weitere_konten_derselben_bankzeile=len(fremde_konten),
        ))
        if fremde_konten:
            melde(
                "SAMMELZAHLUNG", str(p.id),
                f"Zahlung vom {p.buchungsdatum:%d.%m.%Y}: dieselbe Bankzeile ist auch {len(fremde_konten)} anderem/n Mietkonto/en zugeordnet.",
            )

    if zahlungen_ohne_bankbezug:
        melde(
            "ZAHLUNG_OHNE_BANKBEZUG", str(zahlungen_ohne_bankbezug),
            f"{zahlungen_ohne_bankbezug} gebuchte Zahlung(en) ohne verknüpfte Bankzeile.",
        )

    ruecklastschriften = tuple(
        Ruecklastschrift(p.id, p.buchungsdatum, p.betrag_cent, p.beleg_referenz)
        for p in aktiv if p.typ == OPTyp.RUECKLASTSCHRIFT.value
    )

    kandidaten: list[Bankkandidat] = []
    for konto in bank_repository.list_bank_konten(gesellschaft_id=vertrag.gesellschaft_id):
        for transaktion in bank_repository.list_unzugeordnet(konto.id):
            befund = parse_vertragskennungen(transaktion.referenz)
            if vertrag_id not in befund.ids:
                continue
            kandidaten.append(Bankkandidat(
                bank_transaktion_id=transaktion.id, buchungsdatum=transaktion.buchungsdatum,
                betrag_cent=transaktion.betrag_cent, offener_rest_unbestimmt=befund.unklar,
                genannte_vertraege=len(befund.ids),
            ))
            melde(
                "BANKKANDIDAT", str(transaktion.id),
                f"Bankzeile vom {transaktion.buchungsdatum:%d.%m.%Y} nennt diesen Vertrag, ist aber nicht vollständig zugeordnet.",
            )

    # -- Kaution ------------------------------------------------------------
    profil = stammdaten_repository.neuestes_mietvertragsprofil(vertrag_id)
    kaution = stammdaten_repository.get_kaution(vertrag_id)
    kaution_stand = KautionStand(
        vereinbart_cent=profil.vertragliche_kaution_cent if profil else None,
        vereinbart_beleg=profil.vertragliche_kaution_quellenbeleg if profil else None,
        eingang_cent=kaution.betrag_cent if kaution else None,
        eingang_stichtag=kaution.stichtag if kaution else None,
        eingang_referenz=kaution.referenz if kaution else None,
        abgeschlossen_am=kaution.abgeschlossen_am if kaution else None,
    )
    if kaution is None:
        melde("KAUTION_EINGANG_UNBEKANNT", "", "Kein bestätigter Kautionseingang erfasst (unbekannt, nicht 0).")
    if kaution_stand.vereinbart_cent is None:
        melde("KAUTION_SOLL_UNBEKANNT", "", "Keine vereinbarte Kaution im Mietvertragsprofil erfasst (unbekannt, nicht 0).")

    # -- Parteien -----------------------------------------------------------
    weitere_parteien = tuple(
        WeiterePartei(p.rolle, p.name, p.zustand)
        for p in mitmieter_repository.list_aktiv(vertrag_id) if p.rolle != "HAUPTMIETER"
    )
    for partei in weitere_parteien:
        if partei.zustand != "BESTAETIGT":
            melde(
                "PARTEI_UNBESTAETIGT", f"{partei.rolle}|{partei.name}",
                f"{partei.rolle} {partei.name or '(ohne Name)'}: Zustand {partei.zustand}, nicht bestätigt.",
            )
    # Exakt über die Debitor-ID, nie über den Namen, und nur im Zugriffsbereich.
    weitere_vertraege = tuple(sorted(
        v.id for v in stammdaten_repository.list_alle_vertraege()
        if v.debitor_id == vertrag.debitor_id and v.id != vertrag_id and vertrag_sichtbar(v)
    ))

    if stand.konto_id is None:
        melde("KEIN_MIETKONTO", "", "Für diesen Vertrag gibt es kein Mietkonto: kein Saldo vorhanden (nicht 0).")
    if zeile.abweichung_saldo_zu_positionen_cent:
        melde(
            "ABWEICHUNG_SALDO_POSITIONEN", str(zeile.abweichung_saldo_zu_positionen_cent),
            "Kontostand und Summe der offenen Einzelpositionen weichen voneinander ab.",
        )

    objekt_abgleich = next((o for o in status.objekte if o.objekt_id == objekt.id), None)
    return VertragsCheck(
        vertrag_id=vertrag_id, stichtag=heute, von=von, bis=bis, zahlungsstand=stand,
        faelliger_unstrittiger_rest_cent=zeile.faelliger_unstrittiger_rest_cent,
        abweichung_saldo_zu_positionen_cent=zeile.abweichung_saldo_zu_positionen_cent,
        bank_konto_id=bank.id if bank else None, bank_bezeichnung=bank.bezeichnung if bank else None,
        bank_iban_letzte4=iban_letzte4(bank.iban) if bank else None,
        abdeckung=abdeckung, bankvollstaendigkeit_bis=bankvollstaendigkeit_bis,
        abgleich_staende=objekt_abgleich.staende if objekt_abgleich else (),
        teilzahlungen=teilzahlungen, zahlungen_mit_bank=tuple(zahlungen_mit_bank),
        zahlungen_ohne_bankbezug=zahlungen_ohne_bankbezug, kandidaten=tuple(kandidaten),
        ruecklastschriften=ruecklastschriften, kaution=kaution_stand, weitere_parteien=weitere_parteien,
        weitere_vertraege_desselben_debitors=weitere_vertraege,
        hinweise=tuple(sorted(hinweise.values(), key=lambda h: (h.art, h.schluessel))),
    )
