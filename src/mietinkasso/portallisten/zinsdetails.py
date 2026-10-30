"""Rein lesende Detailanreicherung der Zinsliste (HV-20261030-ZINSLISTE).

`portallisten.service.berechne_zinsliste` bleibt führend: Zeilen, Scope, Filter
und Bruttosummen stammen unverändert von dort. Komponentenbeträge sind BRUTTO
(`domain.money.zerlege_brutto_cent`); Netto und USt werden nur daraus abgeleitet,
nie zusätzlich aufgeschlagen. Unbekanntes bleibt `None` und zählt nie als 0.
Dieses Modul schreibt nichts und rechnet keinen Index."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from mietinkasso.domain.exceptions import UStSatzUngueltigError
from mietinkasso.domain.money import zerlege_brutto_cent
from mietinkasso.portallisten.service import _MIET_ARTEN, ZinsZeile, Zinsliste

KATEGORIEN: tuple[tuple[str, str], ...] = (
    ("HAUPTMIETZINS", "Hauptmietzins"),
    ("KUECHE_INVENTAR", "Küche/Inventar"),
    ("BK", "Betriebskosten"),
    ("WASSER", "Wasser"),
    ("HEIZUNG", "Heizung"),
    ("STROM", "Strom"),
    ("SAT_INTERNET", "SAT/Internet"),
    ("PARK_GARAGE", "Park/Garage"),
    ("SONSTIGE", "Sonstige"),
)

_ART_ZU_KATEGORIE = {
    "HMZ": "HAUPTMIETZINS",
    "KUECHE": "KUECHE_INVENTAR",
    "KMI": "KUECHE_INVENTAR",
    "BK_VZ": "BK",
    "BK_PARKPLATZ": "BK",
    "BK": "BK",
    "WASSER": "WASSER",
    "HK_VZ": "HEIZUNG",
    "HEIZUNG": "HEIZUNG",
    "STROM": "STROM",
    "SAT": "SAT_INTERNET",
    "SATELLIT": "SAT_INTERNET",
    "INTERNET": "SAT_INTERNET",
    "PARKPLATZ": "PARK_GARAGE",
    "STELLPLATZ": "PARK_GARAGE",
    "GARAGE": "PARK_GARAGE",
}

STEUERSAETZE: tuple[tuple[int, str], ...] = ((0, "0 %"), (10_000, "10 %"), (20_000, "20 %"))

_NUTZUNGSARTEN = {
    "WOHNUNG": "Wohnung",
    "BUERO": "Büro",
    "GESCHAEFTSLOKAL": "Geschäftslokal",
    "SONSTIGE": "Sonstige",
    "UNGEKLAERT": "ungeklärt",
}
_STATUS_NAMEN = {
    "LEERSTAND": "Leerstand",
    "KURZZEITVERMIETUNG": "Kurzzeitvermietung",
    "SELFSTORAGE": "Selfstorage",
    "EIGENNUTZUNG": "Eigennutzung",
}


@dataclass(frozen=True)
class Betraege:
    """Invariante: Netto + USt + Brutto ohne Aufteilung == Brutto."""

    brutto_cent: int
    netto_bekannt_cent: int
    ust_bekannt_cent: int
    brutto_ohne_aufteilung_cent: int
    anzahl_positionen: int
    anzahl_ohne_aufteilung: int

    @property
    def vollstaendig(self) -> bool:
        return self.anzahl_ohne_aufteilung == 0

    @property
    def netto_cent(self) -> int | None:
        return self.netto_bekannt_cent if self.vollstaendig else None

    @property
    def ust_cent(self) -> int | None:
        return self.ust_bekannt_cent if self.vollstaendig else None


@dataclass(frozen=True)
class PositionDetail:
    komponente_id: str
    art: str
    bezeichnung: str
    kategorie: str
    brutto_cent: int
    netto_cent: int | None
    ust_cent: int | None
    ust_satz_promille: int | None
    unbekannte_art: bool


@dataclass(frozen=True)
class KategorieBetrag:
    kategorie: str
    bezeichnung: str
    betraege: Betraege | None  # None: keine Komponente dieser Kategorie, nicht 0


@dataclass(frozen=True)
class VpiAngabe:
    status: str  # FREIGEGEBEN | ERFASST_UNGEPRUEFT
    reihe: str | None
    basismonat: str | None
    basiswert: Decimal | None
    letzte_anpassung: str | None


@dataclass(frozen=True)
class ZinsDetailZeile:
    basis: ZinsZeile
    top: str
    anzeige_name: str
    anzeige_art: str
    vertragspartner: str | None
    mietbeginn: date | None
    mietbeginn_quelle: str | None  # PROFIL | VERTRAG
    befristung: str | None  # BEFRISTET | UNBEFRISTET | UNGEKLAERT
    befristet_bis: date | None
    nutzungsart: str | None
    flaeche_qm: Decimal | None
    geschoss: str | None
    positionen: tuple[PositionDetail, ...]
    kategorien: tuple[KategorieBetrag, ...]
    gesamt: Betraege | None
    variabel_ohne_fix: bool
    vpi: VpiAngabe | None
    hinweise: tuple[str, ...]
    bk_hk_inklusive: bool = False


@dataclass(frozen=True)
class KategorieSumme:
    kategorie: str
    bezeichnung: str
    betraege: Betraege
    anzahl_zeilen: int


@dataclass(frozen=True)
class SteuersatzSumme:
    ust_satz_promille: int
    bezeichnung: str
    betraege: Betraege


@dataclass(frozen=True)
class ZinsDetailSummen:
    kategorien: tuple[KategorieSumme, ...]
    steuersaetze: tuple[SteuersatzSumme, ...]
    ohne_gueltigen_satz: Betraege
    gesamt: Betraege


@dataclass(frozen=True)
class ZinsDetailListe:
    basis: Zinsliste
    zeilen: tuple[ZinsDetailZeile, ...]
    summen: ZinsDetailSummen


def summiere(positionen) -> Betraege:
    bekannt = [p for p in positionen if p.netto_cent is not None and p.ust_cent is not None]
    offen = [p for p in positionen if p.netto_cent is None or p.ust_cent is None]
    return Betraege(
        brutto_cent=sum(p.brutto_cent for p in positionen),
        netto_bekannt_cent=sum(p.netto_cent for p in bekannt),
        ust_bekannt_cent=sum(p.ust_cent for p in bekannt),
        brutto_ohne_aufteilung_cent=sum(p.brutto_cent for p in offen),
        anzahl_positionen=len(positionen),
        anzahl_ohne_aufteilung=len(offen),
    )


def _lese(quelle, methode: str, *argumente):
    """Alte Fixtures ohne Profil-/Index-Methode bleiben lauffähig."""

    lesen = getattr(quelle, methode, None)
    return lesen(*argumente) if callable(lesen) else None


def _eur_text(cent: int) -> str:
    vorzeichen = "-" if cent < 0 else ""
    return f"{vorzeichen}{abs(cent) // 100},{abs(cent) % 100:02d} €"


def _position(komponente, repository) -> tuple[PositionDetail, list[str]]:
    hinweise: list[str] = []
    kategorie = _ART_ZU_KATEGORIE.get(komponente.art, "SONSTIGE")
    unbekannte_art = komponente.art not in _ART_ZU_KATEGORIE
    if unbekannte_art:
        hinweise.append(f"Art „{komponente.art}“ („{komponente.bezeichnung}“) ist unter Sonstige ausgewiesen.")
    quelle = _lese(repository, "get_komponente", komponente.id)
    satz = getattr(quelle, "ust_satz_promille", None)
    try:
        netto, ust = zerlege_brutto_cent(komponente.betrag_cent, satz)
    except (UStSatzUngueltigError, TypeError):
        netto, ust = None, None
        hinweise.append(
            f"USt-Satz „{satz}“ bei {komponente.art} „{komponente.bezeichnung}“ ungültig oder nicht lesbar - "
            "Brutto bleibt erhalten, Netto/USt unbekannt."
        )
    return PositionDetail(
        komponente_id=komponente.id, art=komponente.art, bezeichnung=komponente.bezeichnung, kategorie=kategorie,
        brutto_cent=komponente.betrag_cent, netto_cent=netto, ust_cent=ust,
        ust_satz_promille=satz if netto is not None else None, unbekannte_art=unbekannte_art,
    ), hinweise


def _kategorien(positionen) -> tuple[KategorieBetrag, ...]:
    ergebnis = []
    for kategorie, bezeichnung in KATEGORIEN:
        treffer = [p for p in positionen if p.kategorie == kategorie]
        ergebnis.append(KategorieBetrag(kategorie, bezeichnung, summiere(treffer) if treffer else None))
    return tuple(ergebnis)


def _anzeige(zeile: ZinsZeile) -> tuple[str, str, str | None]:
    """Der gepflegte Status führt; aus Nullbeträgen oder Namen wird nichts hergeleitet."""

    status = zeile.nutzungsstatus
    if status in _STATUS_NAMEN:
        return _STATUS_NAMEN[status], status, zeile.debitor_name
    if zeile.vertrag_id is not None:
        return zeile.debitor_name or "Mieter nicht hinterlegt", "MIETER", None
    if status == "DAUERVERMIETUNG":
        return "Kein gültiger Vertrag - Status prüfen", "STATUS_PRUEFEN", None
    return status or "Status unbekannt", "UNBEKANNT", None


def _vpi(vertrag_id: str, profil, index_repository) -> VpiAngabe | None:
    klausel = _lese(index_repository, "freigegebene_klausel", vertrag_id)
    if klausel is not None:
        return VpiAngabe(
            status="FREIGEGEBEN", reihe=klausel.basis_reihe, basismonat=klausel.basis_monat,
            basiswert=klausel.basis_wert, letzte_anpassung=klausel.letzte_anpassung_monat,
        )
    reihe = getattr(profil, "index_reihe", None)
    basismonat = getattr(profil, "index_urspruenglicher_basismonat", None)
    basiswert = getattr(profil, "index_urspruenglicher_basiswert", None)
    if reihe is None and basismonat is None and basiswert is None:
        return None
    return VpiAngabe(
        status="ERFASST_UNGEPRUEFT", reihe=reihe, basismonat=basismonat, basiswert=basiswert, letzte_anpassung=None,
    )


def _netto_pruefhinweis(zeile: ZinsZeile, positionen) -> str | None:
    geprueft = zeile.netto_miete_geprueft_cent
    if geprueft is None:
        return None
    miete = [p for p in positionen if p.art in _MIET_ARTEN]
    summe = summiere(miete)
    if not summe.vollstaendig:
        return "Geprüfter Netto-Mietanteil ist mangels gültiger USt nicht mit der Rechnung abgleichbar."
    if summe.netto_bekannt_cent == geprueft:
        return None
    return (
        f"Geprüfter Netto-Mietanteil {_eur_text(geprueft)} weicht von der rechnerischen Summe "
        f"{_eur_text(summe.netto_bekannt_cent)} ab - prüfen."
    )


def _zeile(zeile: ZinsZeile, stammdaten_repository, index_repository, einheiten: dict) -> ZinsDetailZeile:
    if zeile.einheit_id not in einheiten:
        einheiten[zeile.einheit_id] = _lese(stammdaten_repository, "get_einheit", zeile.einheit_id)
    einheit = einheiten[zeile.einheit_id]
    hinweise = list(zeile.hinweise)
    positionen = []
    for komponente in zeile.komponenten:
        position, neue_hinweise = _position(komponente, stammdaten_repository)
        positionen.append(position)
        hinweise.extend(neue_hinweise)
    netto_hinweis = _netto_pruefhinweis(zeile, positionen)
    if netto_hinweis:
        hinweise.append(netto_hinweis)

    profil = None
    if zeile.vertrag_id is not None:
        profil = _lese(stammdaten_repository, "neuestes_mietvertragsprofil", zeile.vertrag_id)
    original_beginn = getattr(profil, "urspruenglicher_mietbeginn", None)
    mietbeginn = original_beginn or zeile.vertrag_von
    quelle = "PROFIL" if original_beginn else ("VERTRAG" if zeile.vertrag_von else None)
    befristung = None
    if zeile.vertrag_id is not None:
        # Nie aus einem leeren Vertragsende abgeleitet - ohne Beleg bleibt es ungeklärt.
        befristung = getattr(profil, "befristungsstatus", None) or "UNGEKLAERT"
    if befristung == "BEFRISTET" and zeile.vertrag_bis is None:
        hinweise.append("Befristet laut Profil, aber kein Vertragsende hinterlegt.")
    if befristung == "UNBEFRISTET" and zeile.vertrag_bis is not None:
        hinweise.append("Unbefristet laut Profil, aber Vertragsende hinterlegt - prüfen.")
    art = getattr(profil, "nutzungsart", None)
    geschoss = (getattr(einheit, "geschoss", None) or "").strip() or None
    name, anzeige_art, vertragspartner = _anzeige(zeile)
    return ZinsDetailZeile(
        basis=zeile, top=zeile.einheit_bezeichnung, anzeige_name=name, anzeige_art=anzeige_art,
        vertragspartner=vertragspartner, mietbeginn=mietbeginn, mietbeginn_quelle=quelle, befristung=befristung,
        befristet_bis=zeile.vertrag_bis, nutzungsart=_NUTZUNGSARTEN.get(art, art) if art else None,
        flaeche_qm=getattr(einheit, "flaeche_qm", None), geschoss=geschoss, positionen=tuple(positionen),
        kategorien=_kategorien(positionen), gesamt=summiere(positionen) if positionen else None,
        variabel_ohne_fix=zeile.variabel and not positionen,
        vpi=_vpi(zeile.vertrag_id, profil, index_repository) if zeile.vertrag_id is not None else None,
        hinweise=tuple(hinweise),
        bk_hk_inklusive=getattr(profil, "bk_hk_abrechnungsart", None) == "PAUSCHALE_INKLUSIVMIETE",
    )


def berechne_zinsdetails(*, liste: Zinsliste, stammdaten_repository, index_repository=None) -> ZinsDetailListe:
    """`liste` stammt aus `berechne_zinsliste`; Scope und Filter sind damit bereits angewendet."""

    einheiten: dict = {}
    zeilen = tuple(_zeile(z, stammdaten_repository, index_repository, einheiten) for z in liste.zeilen)
    alle = [p for z in zeilen for p in z.positionen]
    kategorien = tuple(
        KategorieSumme(
            kategorie=kategorie, bezeichnung=bezeichnung,
            betraege=summiere([p for p in alle if p.kategorie == kategorie]),
            anzahl_zeilen=sum(1 for z in zeilen if any(p.kategorie == kategorie for p in z.positionen)),
        )
        for kategorie, bezeichnung in KATEGORIEN
    )
    steuersaetze = tuple(
        SteuersatzSumme(satz, bezeichnung, summiere([p for p in alle if p.ust_satz_promille == satz]))
        for satz, bezeichnung in STEUERSAETZE
    )
    summen = ZinsDetailSummen(
        kategorien=kategorien, steuersaetze=steuersaetze,
        ohne_gueltigen_satz=summiere([p for p in alle if p.ust_satz_promille is None]),
        gesamt=summiere(alle),
    )
    return ZinsDetailListe(basis=liste, zeilen=zeilen, summen=summen)
