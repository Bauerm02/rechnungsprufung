"""Drei automatische Online-Listen des Backoffice (Auftrag 30.09.2026,
HV-20260930-PORTAL-LISTEN): Mieterliste, Zinsliste, Saldenliste.

REIN LESEND - bucht/plant/versendet nichts, legt keine Vorschreibung an
und schreibt kein Audit. Jede Liste entsteht bei JEDEM Aufruf neu und
deterministisch aus dem aktuellen Datenbankstand; es gibt keinen
Zwischenspeicher, keinen Job und keinen KI-Aufruf.

Wiederverwendet ausschließlich Bestehendes:

- `StammdatenRepository` für Gesellschaften/Objekte/Einheiten/Verträge/
  Debitoren/Vertragskomponenten.
- `rueckstaende.service.berechne_rueckstandsuebersicht` für JEDE
  Saldozahl der Saldenliste (und darüber `OPService.berechne_saldo`/
  `offene_forderungen`) - KEIN zweites Ledger, keine eigene Saldo-/
  Verrechnungslogik, nur eine andere Spaltenaufteilung derselben
  bereits berechneten Werte.
- `BankImportService.bankvollstaendigkeit_bestaetigt_bis` bzw.
  `BankRepository.letztes_buchungsdatum` für den TATSÄCHLICH
  eingespielten/bestätigten Bankstand - nie der Anzeigezeitpunkt.
- `VariableAbrechnungService.liste_aktuelle` nur für den STATUS eines
  variablen Monatsberichts (vorhanden/Entwurf/bestätigt) - dessen
  Beträge fließen HIER NIRGENDS ein (kein variabler Erlös als fester
  Mietzins, keine Doppelzählung).
- `KomponentenNettoMietFreigabeService.aktive_freigabe_fuer_monat` für
  einen GEPRÜFTEN Netto-Mietanteil - ohne Freigabe bleibt die
  Netto/USt-Aufteilung ausdrücklich unbekannt.

Fachliche Leitplanken (siehe AGENTS.md):

- Objekt-/Gesellschaftsscope exakt wie in `rueckstaende.service`: nur
  Gesellschaften mit `ctx`-Zugriff, nie ein ausgeschlossenes Objekt; ein
  ausdrücklich angefordertes fremdes/unbekanntes/ausgeschlossenes
  `objekt_id` wird mit demselben einheitlichen
  `UnbekanntesObjektFilterError` abgelehnt. Ein Vertrag, dessen EIGENES
  `gesellschaft_id` nicht im Zugriff liegt, wird übersprungen, selbst
  wenn sein Objekt erlaubt ist.
- Ein Mieter entsteht AUSSCHLIESSLICH über Vertrag -> Debitor. Kein
  Name wird interpretiert, kein technisches Verrechnungskonto wird zum
  Mieter, kein Leerstand wird aus einem Nullbetrag abgeleitet - die
  Nutzung kommt allein aus dem gepflegten `Einheit.nutzungsstatus`.
- Ein beendeter Vertrag beweist KEINEN tatsächlichen Leerstand: sagt
  der Nutzungsstatus weiterhin DAUERVERMIETUNG, wird "Status prüfen"
  ausgewiesen statt Leerstand zu behaupten.
- Fehlende Angaben bleiben `None` (Anzeige "Nicht hinterlegt"/
  "unbekannt") - nie eine erfundene 0 und nie eine erfundene Angabe.
  Eine Zeile ohne numerischen Betrag fließt in KEINE Summe ein.
- Guthaben und Rückstand werden je Konto getrennt geführt und auch in
  den Summen nie gegeneinander verrechnet."""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date

from mietinkasso.auth.service import AuthContext
from mietinkasso.bank.repository import BankRepository
from mietinkasso.bank.service import BankImportService
from mietinkasso.indexautomatik.zeit import heute_wien
from mietinkasso.mahnwesen.repository import MahnFallRepository
from mietinkasso.op.service import OPService
from mietinkasso.rueckstaende.service import (
    ObjektOption,
    UnbekanntesObjektFilterError,
    berechne_rueckstandsuebersicht,
)
from mietinkasso.stammdaten.repository import StammdatenRepository
from mietinkasso.variableabrechnung.komponenten_freigabe import KomponentenNettoMietFreigabeService
from mietinkasso.variableabrechnung.service import VariableAbrechnungService

_MONAT_MUSTER = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

MIETER_STATUS_FILTER = ("aktiv", "beendet", "alle")
SALDEN_STATUS_FILTER = ("", "offen", "guthaben", "ausgeglichen")

#: Spaltenzuordnung der Zinsliste je Komponenten-`art` - dieselben
#: Miet-Arten wie `variableabrechnung/dashboard.py::_MOEGLICHE_MIET_ARTEN`.
#: Jede ANDERE Art (BK-/HK-Vorauszahlung, Wasser, Strom, künftige Arten)
#: landet sichtbar unter "BK / Sonstige" statt zu verschwinden.
_ART_MIETE = frozenset({"HMZ"})
_ART_KUECHE = frozenset({"KUECHE"})
_ART_STELLPLATZ = frozenset({"PARKPLATZ", "STELLPLATZ"})
_MIET_ARTEN = _ART_MIETE | _ART_KUECHE | _ART_STELLPLATZ

_VARIABLE_NUTZUNG = frozenset({"KURZZEITVERMIETUNG", "SELFSTORAGE"})


# -- Gemeinsamer Scope --------------------------------------------------------


def erlaubte_objekte(ctx: AuthContext, stammdaten_repository: StammdatenRepository) -> tuple[ObjektOption, ...]:
    """Derselbe erlaubte Bestand wie in `berechne_rueckstandsuebersicht`:
    nur Gesellschaften mit `ctx`-Zugriff, nie ausgeschlossene Objekte."""

    optionen: list[ObjektOption] = []
    for gesellschaft in stammdaten_repository.list_gesellschaften():
        if not ctx.has_zugriff(gesellschaft.id):
            continue
        for objekt in stammdaten_repository.list_objekte(gesellschaft_id=gesellschaft.id):
            if objekt.ausgeschlossen:
                continue
            optionen.append(ObjektOption(
                id=objekt.id, bezeichnung=objekt.bezeichnung,
                gesellschaft_id=gesellschaft.id, gesellschaft_name=gesellschaft.name,
            ))
    optionen.sort(key=lambda o: (o.gesellschaft_name, o.bezeichnung))
    return tuple(optionen)


def _ziel_objekte(optionen: tuple[ObjektOption, ...], objekt_id: str | None) -> list[ObjektOption]:
    if not objekt_id:
        return list(optionen)
    treffer = [o for o in optionen if o.id == objekt_id]
    if not treffer:
        # Wortgleich zu `rueckstaende.service` - EIN einheitlicher Fehler
        # für unbekannt/fremd/ausgeschlossen, kein Erkenntnisgewinn.
        raise UnbekanntesObjektFilterError(
            f"Objekt {objekt_id} ist unbekannt, gehört zu keiner zugänglichen Gesellschaft, oder ist "
            "ausgeschlossen."
        )
    return treffer


def _text_oder_none(wert: str | None) -> str | None:
    text = (wert or "").strip()
    return text or None


def _passt_zur_suche(suche: str | None, *felder: str | None) -> bool:
    if not suche:
        return True
    nadel = suche.casefold()
    return any(nadel in (feld or "").casefold() for feld in felder)


def monatsgrenzen(monat: str) -> tuple[date, date]:
    if not _MONAT_MUSTER.match(monat or ""):
        raise ValueError(f"Monat '{monat}' muss dem Format YYYY-MM entsprechen (z. B. 2026-09).")
    jahr, monatszahl = (int(teil) for teil in monat.split("-"))
    if jahr < 1:
        raise ValueError(f"Monat '{monat}' muss dem Format YYYY-MM entsprechen (z. B. 2026-09).")
    return date(jahr, monatszahl, 1), date(jahr, monatszahl, calendar.monthrange(jahr, monatszahl)[1])


# -- 1. Mieterliste -----------------------------------------------------------


@dataclass(frozen=True)
class MieterZeile:
    vertrag_id: str
    debitor_id: str
    debitor_name: str
    objekt_id: str
    objekt_bezeichnung: str
    objekt_adresse: str | None  # Mietobjekt - NIE mit der Korrespondenzadresse gleichgesetzt
    einheit_id: str
    einheit_bezeichnung: str
    korrespondenzadresse: str | None  # `Debitor.adresse`
    telefon: str | None
    email: str | None
    gueltig_von: date
    gueltig_bis: date | None
    status: str  # "AKTIV" | "KUENFTIG" | "BEENDET"


@dataclass(frozen=True)
class Mieterliste:
    objekt_filter: str | None
    objekt_optionen: tuple[ObjektOption, ...]
    status_filter: str
    suche: str | None
    stichtag: date
    zeilen: tuple[MieterZeile, ...]
    anzahl_aktiv: int
    anzahl_kuenftig: int
    anzahl_beendet: int


def berechne_mieterliste(
    *,
    ctx: AuthContext,
    objekt_id: str | None,
    stammdaten_repository: StammdatenRepository,
    status: str = "aktiv",
    suche: str | None = None,
    heute: date | None = None,
) -> Mieterliste:
    """`status="aktiv"` (Standard) zeigt laufende und bereits angelegte
    künftige Mietverhältnisse; beendete erscheinen NUR unter "beendet"/
    "alle" und tragen dann ihren Status - ein ehemaliger Mieter steht
    nie unmarkiert zwischen den aktiven Kontakten. Ein künftiges
    Mietverhältnis ist NIE "AKTIV": es trägt `status="KUENFTIG"` und
    wird getrennt gezählt (`anzahl_kuenftig`)."""

    status = status or "aktiv"
    if status not in MIETER_STATUS_FILTER:
        raise ValueError(f"Unbekannter Statusfilter '{status}' - erlaubt: aktiv, beendet, alle.")
    heute = heute or heute_wien()
    suche = _text_oder_none(suche)
    optionen = erlaubte_objekte(ctx, stammdaten_repository)

    zeilen: list[MieterZeile] = []
    for option in _ziel_objekte(optionen, objekt_id):
        objekt = stammdaten_repository.get_objekt(option.id)
        if objekt is None:
            continue
        for vertrag in stammdaten_repository.list_vertraege_fuer_objekt(option.id):
            if not ctx.has_zugriff(vertrag.gesellschaft_id):
                continue
            einheit = stammdaten_repository.get_einheit(vertrag.einheit_id)
            debitor = stammdaten_repository.get_debitor(vertrag.debitor_id)
            if einheit is None or debitor is None:
                continue
            if vertrag.gueltig_bis is not None and vertrag.gueltig_bis < heute:
                vertrag_status = "BEENDET"
            elif vertrag.gueltig_von > heute:
                vertrag_status = "KUENFTIG"
            else:
                vertrag_status = "AKTIV"
            zeilen.append(MieterZeile(
                vertrag_id=vertrag.id, debitor_id=debitor.id, debitor_name=debitor.name,
                objekt_id=objekt.id, objekt_bezeichnung=objekt.bezeichnung,
                objekt_adresse=_text_oder_none(objekt.adresse),
                einheit_id=einheit.id, einheit_bezeichnung=einheit.bezeichnung,
                korrespondenzadresse=_text_oder_none(debitor.adresse),
                # Optionales `Debitor.telefon` - fehlt es, bleibt `None`
                # ("Nicht hinterlegt"), nie geraten/abgeleitet.
                telefon=_text_oder_none(debitor.telefon),
                email=_text_oder_none(debitor.email),
                gueltig_von=vertrag.gueltig_von, gueltig_bis=vertrag.gueltig_bis, status=vertrag_status,
            ))

    zeilen = [
        z for z in zeilen
        if _passt_zur_suche(
            suche, z.debitor_name, z.objekt_bezeichnung, z.objekt_adresse, z.einheit_bezeichnung, z.email,
            z.telefon, z.vertrag_id,
        )
    ]
    anzahl_aktiv = sum(1 for z in zeilen if z.status == "AKTIV")
    anzahl_kuenftig = sum(1 for z in zeilen if z.status == "KUENFTIG")
    anzahl_beendet = sum(1 for z in zeilen if z.status == "BEENDET")
    if status == "aktiv":
        zeilen = [z for z in zeilen if z.status != "BEENDET"]
    elif status == "beendet":
        zeilen = [z for z in zeilen if z.status == "BEENDET"]
    zeilen.sort(key=lambda z: (z.debitor_name.casefold(), z.objekt_bezeichnung, z.einheit_bezeichnung, z.vertrag_id))

    return Mieterliste(
        objekt_filter=objekt_id or None, objekt_optionen=optionen, status_filter=status, suche=suche,
        stichtag=heute, zeilen=tuple(zeilen), anzahl_aktiv=anzahl_aktiv, anzahl_kuenftig=anzahl_kuenftig,
        anzahl_beendet=anzahl_beendet,
    )


# -- 2. Zinsliste -------------------------------------------------------------


@dataclass(frozen=True)
class KomponentenBetrag:
    id: str
    art: str
    bezeichnung: str
    betrag_cent: int


@dataclass(frozen=True)
class ZinsZeile:
    """EINE Einheit im gewählten Monat - mit Vertrag (dann je Vertrag
    eine Zeile) oder ohne. Jeder Betrag ist `None`, wenn dazu nichts
    gespeichert ist; `None` ist NIE 0 und fließt in keine Summe ein."""

    objekt_id: str
    objekt_bezeichnung: str
    einheit_id: str
    einheit_bezeichnung: str
    nutzungsstatus: str  # AKTUELL gepflegter Status, keine Historie
    vertrag_id: str | None
    debitor_name: str | None
    vertrag_von: date | None
    vertrag_bis: date | None
    miete_cent: int | None
    kueche_cent: int | None
    stellplatz_cent: int | None
    nebenkosten_cent: int | None  # BK/HK/sonstige gespeicherte Komponenten
    brutto_gesamt_cent: int | None
    netto_miete_geprueft_cent: int | None  # nur bei vollständiger Netto-Freigabe aller Mietkomponenten
    komponenten: tuple[KomponentenBetrag, ...]
    variabel: bool
    variabler_bericht_status: str | None  # None == kein Monatsbericht vorhanden
    letzter_vertrag_id: str | None  # zuletzt beendeter Vertrag, nur bei Zeilen OHNE Vertrag
    letzter_vertrag_bis: date | None
    letzter_vertrag_debitor_name: str | None
    # Vertrag oder eine Komponente beginnt/endet INNERHALB des Monats:
    # der ausgewiesene Betrag ist dann nur der Stand am Monatsersten,
    # KEIN geklärter Betrag für den ganzen Monat (keine Aliquotierung).
    aenderung_im_monat: bool
    pruefbedarf: bool
    hinweise: tuple[str, ...]


@dataclass(frozen=True)
class ZinsSummen:
    miete_cent: int
    kueche_cent: int
    stellplatz_cent: int
    nebenkosten_cent: int
    brutto_gesamt_cent: int
    anzahl_zeilen: int
    anzahl_mit_betrag: int
    anzahl_ohne_betrag: int
    # Teilmenge von `anzahl_mit_betrag`/`brutto_gesamt_cent`: Zeilen mit
    # `aenderung_im_monat` - in der Summe enthalten, aber getrennt
    # ausgewiesen, damit sie nicht als geklärter Ganzmonatsbetrag gilt.
    anzahl_aenderung_im_monat: int
    brutto_aenderung_im_monat_cent: int


@dataclass(frozen=True)
class Zinsliste:
    monat: str
    monatsanfang: date
    monatsende: date
    objekt_filter: str | None
    objekt_optionen: tuple[ObjektOption, ...]
    suche: str | None
    zeilen: tuple[ZinsZeile, ...]
    summen: ZinsSummen


def _summe_oder_none(komponenten, arten: frozenset[str] | None) -> int | None:
    treffer = [k for k in komponenten if (k.art in arten if arten is not None else k.art not in _MIET_ARTEN)]
    if not treffer:
        return None
    return sum(k.betrag_cent for k in treffer)


def berechne_zinsliste(
    *,
    ctx: AuthContext,
    monat: str,
    objekt_id: str | None,
    stammdaten_repository: StammdatenRepository,
    variable_service: VariableAbrechnungService | None = None,
    komponenten_freigabe_service: KomponentenNettoMietFreigabeService | None = None,
    suche: str | None = None,
) -> Zinsliste:
    """Monatliche VERTRAGLICHE Beträge laut gespeicherter Komponenten -
    erzeugt KEINE Vorschreibung und keine Sollstellung.

    Gezählt werden die zum MONATSERSTEN gültigen Komponenten eines zum
    Monatsersten gültigen Vertrags - exakt der Stichtag, den auch
    `VorschreibungService.entwurf_erstellen` verwendet. Beginnt der
    Vertrag erst im Monat, wird KEIN (anteiliger) Betrag erfunden; endet
    er im Monat, bleibt der volle Monatsbetrag mit Hinweis stehen (keine
    Aliquotierung). Beginnt oder endet Vertrag bzw. Komponente innerhalb
    des Monats, trägt die Zeile `aenderung_im_monat` - der Betrag ist
    dann ausdrücklich nur der Stand am Monatsersten und wird in den
    Summen getrennt ausgewiesen. Im Folgemonat eines Vertragsendes gibt es keine
    Vertragszeile mehr, nur die Einheit mit ihrem gepflegten
    Nutzungsstatus."""

    monatsanfang, monatsende = monatsgrenzen(monat)
    suche = _text_oder_none(suche)
    optionen = erlaubte_objekte(ctx, stammdaten_repository)

    # NUR der Status eines variablen Monatsberichts - nie dessen Beträge.
    bericht_status: dict[str, str] = {}
    if variable_service is not None:
        for bericht in variable_service.liste_aktuelle(ctx=ctx, leistungsmonat=monat):
            bisher = bericht_status.get(bericht.einheit_id)
            if bisher is None or bericht.status == "BESTAETIGT":
                bericht_status[bericht.einheit_id] = bericht.status

    zeilen: list[ZinsZeile] = []
    for option in _ziel_objekte(optionen, objekt_id):
        objekt = stammdaten_repository.get_objekt(option.id)
        if objekt is None:
            continue
        vertraege_je_einheit: dict[str, list] = {}
        einheiten_mit_fremdvertrag: set[str] = set()
        for vertrag in stammdaten_repository.list_vertraege_fuer_objekt(option.id):
            if not ctx.has_zugriff(vertrag.gesellschaft_id):
                einheiten_mit_fremdvertrag.add(vertrag.einheit_id)
                continue
            vertraege_je_einheit.setdefault(vertrag.einheit_id, []).append(vertrag)

        for einheit in stammdaten_repository.list_einheiten_fuer_objekt(option.id):
            alle_vertraege = sorted(vertraege_je_einheit.get(einheit.id, []), key=lambda v: (v.gueltig_von, v.id))
            im_monat = [
                v for v in alle_vertraege
                if v.gueltig_von <= monatsende and (v.gueltig_bis is None or v.gueltig_bis >= monatsanfang)
            ]
            variabel = einheit.nutzungsstatus in _VARIABLE_NUTZUNG
            variabler_status = bericht_status.get(einheit.id)

            if not im_monat:
                if einheit.id in einheiten_mit_fremdvertrag:
                    # Wie in `rueckstaende.service`: eine Einheit mit einem
                    # Vertrag außerhalb des ctx-Zugriffs wird NICHT als
                    # vertragslos/prüfbedürftig ausgegeben.
                    continue
                beendete = [v for v in alle_vertraege if v.gueltig_bis is not None and v.gueltig_bis < monatsanfang]
                letzter = max(beendete, key=lambda v: (v.gueltig_bis, v.id)) if beendete else None
                letzter_debitor = stammdaten_repository.get_debitor(letzter.debitor_id) if letzter else None
                hinweise: list[str] = []
                pruefbedarf = False
                if einheit.nutzungsstatus == "DAUERVERMIETUNG":
                    pruefbedarf = True
                    if letzter is not None:
                        hinweise.append(
                            f"Vertrag {letzter.id} endete am {letzter.gueltig_bis.isoformat()}; Einheit laut "
                            "Stammdaten weiterhin „vermietet“ - tatsächliche Nutzung prüfen (ein Vertragsende "
                            "beweist keinen Leerstand)."
                        )
                    else:
                        hinweise.append(
                            "Einheit laut Stammdaten „vermietet“, aber kein im Monat gültiger Vertrag "
                            "hinterlegt - Status prüfen."
                        )
                elif einheit.nutzungsstatus == "LEERSTAND":
                    hinweise.append("Leerstand laut gepflegtem Nutzungsstatus (nicht aus einem Betrag abgeleitet).")
                elif variabel:
                    hinweise.append("Variable Erlöse laut Monatsbericht - kein fester Mietzins, hier nicht summiert.")
                elif einheit.nutzungsstatus == "EIGENNUTZUNG":
                    hinweise.append("Eigennutzung laut gepflegtem Nutzungsstatus - kein Mietzins.")
                zeilen.append(ZinsZeile(
                    objekt_id=objekt.id, objekt_bezeichnung=objekt.bezeichnung, einheit_id=einheit.id,
                    einheit_bezeichnung=einheit.bezeichnung, nutzungsstatus=einheit.nutzungsstatus,
                    vertrag_id=None, debitor_name=None, vertrag_von=None, vertrag_bis=None,
                    miete_cent=None, kueche_cent=None, stellplatz_cent=None, nebenkosten_cent=None,
                    brutto_gesamt_cent=None, netto_miete_geprueft_cent=None, komponenten=(),
                    variabel=variabel, variabler_bericht_status=variabler_status,
                    letzter_vertrag_id=letzter.id if letzter else None,
                    letzter_vertrag_bis=letzter.gueltig_bis if letzter else None,
                    letzter_vertrag_debitor_name=letzter_debitor.name if letzter_debitor else None,
                    aenderung_im_monat=False, pruefbedarf=pruefbedarf, hinweise=tuple(hinweise),
                ))
                continue

            for vertrag in im_monat:
                debitor = stammdaten_repository.get_debitor(vertrag.debitor_id)
                hinweise = []
                pruefbedarf = False
                aenderung_im_monat = False
                gilt_am_monatsersten = vertrag.gueltig_von <= monatsanfang

                ueberlappende = stammdaten_repository.list_komponenten_im_zeitraum(vertrag.id, monatsanfang, monatsende)
                gezaehlt = []
                if gilt_am_monatsersten:
                    gezaehlt = sorted(
                        stammdaten_repository.list_aktive_komponenten(vertrag.id, monatsanfang), key=lambda k: k.id
                    )
                else:
                    pruefbedarf = True
                    hinweise.append(
                        f"Vertrag beginnt am {vertrag.gueltig_von.isoformat()} (im Monat) - kein anteiliger "
                        "Betrag berechnet, Betrag für diesen Monat unbekannt."
                    )
                gezaehlte_ids = {k.id for k in gezaehlt}
                if gilt_am_monatsersten:
                    for komponente in sorted(ueberlappende, key=lambda k: k.id):
                        if komponente.id not in gezaehlte_ids:
                            pruefbedarf = True
                            aenderung_im_monat = True
                            hinweise.append(
                                f"Komponente {komponente.art} „{komponente.bezeichnung}“ gilt erst ab "
                                f"{komponente.gueltig_von.isoformat()} (im Monat) - nicht im Monatsbetrag enthalten."
                            )
                    for komponente in gezaehlt:
                        if komponente.gueltig_bis is not None and komponente.gueltig_bis < monatsende:
                            pruefbedarf = True
                            aenderung_im_monat = True
                            hinweise.append(
                                f"Komponente {komponente.art} „{komponente.bezeichnung}“ endet am "
                                f"{komponente.gueltig_bis.isoformat()} - voller Monatsbetrag, keine Aliquotierung."
                            )
                    if not gezaehlt:
                        pruefbedarf = True
                        hinweise.append(
                            "Keine zum Monatsersten gültige Vertragskomponente hinterlegt - Betrag unbekannt "
                            "(nicht 0)."
                        )

                if vertrag.gueltig_bis is not None and vertrag.gueltig_bis <= monatsende:
                    if vertrag.gueltig_bis < monatsende:
                        aenderung_im_monat = True
                    hinweise.append(
                        f"Vertrag endet am {vertrag.gueltig_bis.isoformat()} - Monatsbetrag laut Komponenten "
                        "zum Monatsersten, keine Aliquotierung."
                    )
                    hat_folgevertrag = any(v.gueltig_von > vertrag.gueltig_bis for v in alle_vertraege)
                    if not hat_folgevertrag and einheit.nutzungsstatus == "DAUERVERMIETUNG":
                        pruefbedarf = True
                        hinweise.append(
                            "Kein Folgevertrag hinterlegt; Einheit laut Stammdaten weiterhin „vermietet“ - "
                            "tatsächliche Nutzung nach Vertragsende prüfen (kein automatischer Leerstand)."
                        )
                if len(im_monat) > 1:
                    pruefbedarf = True
                    hinweise.append("Mehrere Verträge dieser Einheit überlappen den Monat - Zuordnung prüfen.")
                if einheit.nutzungsstatus in ("LEERSTAND", "EIGENNUTZUNG"):
                    pruefbedarf = True
                    hinweise.append(
                        "Nutzungsstatus laut Stammdaten widerspricht einem im Monat gültigen Vertrag - prüfen."
                    )
                if variabel and gezaehlt and variabler_status is not None:
                    pruefbedarf = True
                    hinweise.append(
                        "Zusätzlich liegt ein variabler Monatsbericht vor - hier NICHT addiert; mögliche "
                        "Doppelerfassung prüfen."
                    )

                netto_geprueft: int | None = None
                miet_komponenten = [k for k in gezaehlt if k.art in _MIET_ARTEN]
                if komponenten_freigabe_service is not None and miet_komponenten:
                    freigaben = [
                        komponenten_freigabe_service.aktive_freigabe_fuer_monat(
                            k.id, monatsanfang=monatsanfang, monatsende=monatsende
                        )
                        for k in miet_komponenten
                    ]
                    if all(f is not None for f in freigaben):
                        netto_geprueft = sum(f.bestaetigter_netto_betrag_cent for f in freigaben)

                zeilen.append(ZinsZeile(
                    objekt_id=objekt.id, objekt_bezeichnung=objekt.bezeichnung, einheit_id=einheit.id,
                    einheit_bezeichnung=einheit.bezeichnung, nutzungsstatus=einheit.nutzungsstatus,
                    vertrag_id=vertrag.id, debitor_name=debitor.name if debitor else None,
                    vertrag_von=vertrag.gueltig_von, vertrag_bis=vertrag.gueltig_bis,
                    miete_cent=_summe_oder_none(gezaehlt, _ART_MIETE),
                    kueche_cent=_summe_oder_none(gezaehlt, _ART_KUECHE),
                    stellplatz_cent=_summe_oder_none(gezaehlt, _ART_STELLPLATZ),
                    nebenkosten_cent=_summe_oder_none(gezaehlt, None),
                    brutto_gesamt_cent=sum(k.betrag_cent for k in gezaehlt) if gezaehlt else None,
                    netto_miete_geprueft_cent=netto_geprueft,
                    komponenten=tuple(
                        KomponentenBetrag(id=k.id, art=k.art, bezeichnung=k.bezeichnung, betrag_cent=k.betrag_cent)
                        for k in gezaehlt
                    ),
                    variabel=variabel, variabler_bericht_status=variabler_status,
                    letzter_vertrag_id=None, letzter_vertrag_bis=None, letzter_vertrag_debitor_name=None,
                    # Nur wo überhaupt ein Betrag steht - ohne Betrag gibt es
                    # nichts, das als Ganzmonatsbetrag missverstanden würde.
                    aenderung_im_monat=aenderung_im_monat and bool(gezaehlt),
                    pruefbedarf=pruefbedarf or (aenderung_im_monat and bool(gezaehlt)), hinweise=tuple(hinweise),
                ))

    zeilen = [
        z for z in zeilen
        if _passt_zur_suche(
            suche, z.debitor_name, z.objekt_bezeichnung, z.einheit_bezeichnung, z.vertrag_id,
            z.letzter_vertrag_debitor_name,
        )
    ]
    zeilen.sort(key=lambda z: (z.objekt_bezeichnung, z.einheit_bezeichnung, z.vertrag_von or date.min, z.vertrag_id or ""))

    # Summen aus GENAU den angezeigten (gefilterten) Zeilen - eine Zeile
    # ohne numerischen Betrag zählt nicht als 0, sondern wird ausgewiesen.
    mit_betrag = [z for z in zeilen if z.brutto_gesamt_cent is not None]
    mit_aenderung = [z for z in mit_betrag if z.aenderung_im_monat]
    summen = ZinsSummen(
        anzahl_aenderung_im_monat=len(mit_aenderung),
        brutto_aenderung_im_monat_cent=sum(z.brutto_gesamt_cent for z in mit_aenderung),
        miete_cent=sum(z.miete_cent or 0 for z in mit_betrag),
        kueche_cent=sum(z.kueche_cent or 0 for z in mit_betrag),
        stellplatz_cent=sum(z.stellplatz_cent or 0 for z in mit_betrag),
        nebenkosten_cent=sum(z.nebenkosten_cent or 0 for z in mit_betrag),
        brutto_gesamt_cent=sum(z.brutto_gesamt_cent for z in mit_betrag),
        anzahl_zeilen=len(zeilen), anzahl_mit_betrag=len(mit_betrag),
        anzahl_ohne_betrag=len(zeilen) - len(mit_betrag),
    )
    return Zinsliste(
        monat=monat, monatsanfang=monatsanfang, monatsende=monatsende, objekt_filter=objekt_id or None,
        objekt_optionen=optionen, suche=suche, zeilen=tuple(zeilen), summen=summen,
    )


# -- 3. Saldenliste -----------------------------------------------------------


@dataclass(frozen=True)
class SaldoZeile:
    """Andere Spaltenaufteilung EINER `rueckstaende.MietkontoZeile` -
    keine neue Rechnung. Ohne Mietkonto sind ALLE Beträge `None`
    (Saldo unbekannt, NICHT ausgeglichen)."""

    objekt_id: str
    objekt_bezeichnung: str
    gesellschaft_id: str
    vertrag_id: str
    einheit_bezeichnung: str
    nutzungsstatus: str
    debitor_name: str
    konto_id: str | None
    saldo_cent: int | None  # Rohwert aus OPSaldo (positiv offen, negativ Guthaben)
    offen_cent: int | None  # max(saldo, 0)
    guthaben_cent: int | None  # max(-saldo, 0) - positiv dargestellter Betrag
    faellig_cent: int | None  # offene Einzelpositionen mit bekannter, verstrichener Fälligkeit
    nicht_faellig_cent: int | None  # offene Einzelpositionen mit bekannter, künftiger Fälligkeit
    faelligkeit_unbekannt_cent: int | None  # offene Einzelpositionen ohne erfasste Fälligkeit
    faellig_kontoberechnung_cent: int | None  # OPSaldo.faelliger_unstrittiger_rest_cent
    abweichung_cent: int | None  # Kontostand vs. Einzelpositionen, unverändert übernommen
    status: str  # "OFFEN" | "GUTHABEN" | "AUSGEGLICHEN" | "KEIN_KONTO"
    historisch: bool
    sperrgruende: tuple[str, ...]
    mahnfaelle_anzahl: int


@dataclass(frozen=True)
class SaldenSummen:
    """Getrennte Summen - Guthaben wird NIE von offenen Beträgen anderer
    Mieter abgezogen."""

    offen_cent: int
    faellig_cent: int
    nicht_faellig_cent: int
    faelligkeit_unbekannt_cent: int
    guthaben_cent: int
    anzahl_zeilen: int
    anzahl_ohne_konto: int


@dataclass(frozen=True)
class BankStandZeile:
    """Tatsächlich eingespielter/bestätigter Bankstand EINES Bankkontos
    der Gesellschaft - `bank_konto_id=None` heißt: für diese Gesellschaft
    ist gar kein Bankkonto hinterlegt."""

    gesellschaft_id: str
    gesellschaft_name: str
    bank_konto_id: str | None
    bank_konto_bezeichnung: str | None
    bestaetigt_bis: date | None  # ausdrückliche Vollständigkeitsbestätigung
    letzte_buchung: date | None  # Buchungsdatum der jüngsten importierten Zeile (kein Vollständigkeitsnachweis)


@dataclass(frozen=True)
class Saldenliste:
    objekt_filter: str | None
    objekt_optionen: tuple[ObjektOption, ...]
    status_filter: str
    stichtag: date
    zeilen: tuple[SaldoZeile, ...]
    summen: SaldenSummen
    bankstaende: tuple[BankStandZeile, ...]


def berechne_saldenliste(
    *,
    ctx: AuthContext,
    objekt_id: str | None,
    stammdaten_repository: StammdatenRepository,
    op_service: OPService,
    mahn_fall_repository: MahnFallRepository,
    bank_repository: BankRepository,
    bank_service: BankImportService,
    status: str = "",
    heute: date | None = None,
) -> Saldenliste:
    status = status or ""
    if status not in SALDEN_STATUS_FILTER:
        raise ValueError(f"Unbekannter Statusfilter '{status}' - erlaubt: offen, guthaben, ausgeglichen.")
    heute = heute or heute_wien()

    # Scope, Objektfilter-Ablehnung, Salden, Sperren und Mahnfall-Anzahl
    # kommen vollständig aus der bestehenden Rückstandsberechnung (reiner
    # Read, plant keine Mahnung).
    uebersicht = berechne_rueckstandsuebersicht(
        ctx=ctx, objekt_id=objekt_id or None, stammdaten_repository=stammdaten_repository,
        op_service=op_service, mahn_fall_repository=mahn_fall_repository, heute=heute,
    )
    option_je_objekt = {o.id: o for o in uebersicht.objekt_optionen}

    nicht_faellig_je_vertrag: dict[str, int] = {}
    unbekannt_je_vertrag: dict[str, int] = {}
    for position in uebersicht.offene_positionen:
        if position.faelligkeitsklasse == "NICHT_FAELLIG":
            nicht_faellig_je_vertrag[position.vertrag_id] = (
                nicht_faellig_je_vertrag.get(position.vertrag_id, 0) + position.rest_cent
            )
        elif position.faelligkeitsklasse == "UNBEKANNT":
            unbekannt_je_vertrag[position.vertrag_id] = unbekannt_je_vertrag.get(position.vertrag_id, 0) + position.rest_cent

    zeilen: list[SaldoZeile] = []
    for z in uebersicht.mietkonten:
        hat_konto = z.konto_id is not None and z.saldo_cent is not None
        if not hat_konto:
            zeilen_status = "KEIN_KONTO"
        elif z.saldo_cent > 0:
            zeilen_status = "OFFEN"
        elif z.saldo_cent < 0:
            zeilen_status = "GUTHABEN"
        else:
            zeilen_status = "AUSGEGLICHEN"
        zeilen.append(SaldoZeile(
            objekt_id=z.objekt_id, objekt_bezeichnung=z.objekt_bezeichnung,
            gesellschaft_id=option_je_objekt[z.objekt_id].gesellschaft_id,
            vertrag_id=z.vertrag_id, einheit_bezeichnung=z.einheit_bezeichnung, nutzungsstatus=z.nutzungsstatus,
            debitor_name=z.debitor_name, konto_id=z.konto_id, saldo_cent=z.saldo_cent if hat_konto else None,
            offen_cent=max(z.saldo_cent, 0) if hat_konto else None,
            guthaben_cent=max(-z.saldo_cent, 0) if hat_konto else None,
            faellig_cent=z.positionen_faelliger_rest_cent if hat_konto else None,
            nicht_faellig_cent=nicht_faellig_je_vertrag.get(z.vertrag_id, 0) if hat_konto else None,
            faelligkeit_unbekannt_cent=unbekannt_je_vertrag.get(z.vertrag_id, 0) if hat_konto else None,
            faellig_kontoberechnung_cent=z.faelliger_unstrittiger_rest_cent if hat_konto else None,
            abweichung_cent=z.abweichung_saldo_zu_positionen_cent if hat_konto else None,
            status=zeilen_status, historisch=z.historisch, sperrgruende=z.sperrgruende,
            mahnfaelle_anzahl=z.mahnfaelle_anzahl,
        ))

    if status:
        zeilen = [z for z in zeilen if z.status == status.upper()]
    zeilen.sort(key=lambda z: (z.objekt_bezeichnung, z.einheit_bezeichnung, z.vertrag_id))

    summen = SaldenSummen(
        offen_cent=sum(z.offen_cent or 0 for z in zeilen),
        faellig_cent=sum(z.faellig_cent or 0 for z in zeilen),
        nicht_faellig_cent=sum(z.nicht_faellig_cent or 0 for z in zeilen),
        faelligkeit_unbekannt_cent=sum(z.faelligkeit_unbekannt_cent or 0 for z in zeilen),
        guthaben_cent=sum(z.guthaben_cent or 0 for z in zeilen),
        anzahl_zeilen=len(zeilen), anzahl_ohne_konto=sum(1 for z in zeilen if z.status == "KEIN_KONTO"),
    )

    # Bankstand je Gesellschaft des Objekt-Scopes (nicht des Statusfilters):
    # ein Bankkonto gehört einer Gesellschaft, nicht einem einzelnen Mietkonto.
    gesellschaften: dict[str, str] = {}
    for option in uebersicht.objekt_optionen:
        if uebersicht.objekt_filter is None or option.id == uebersicht.objekt_filter:
            gesellschaften[option.gesellschaft_id] = option.gesellschaft_name
    bankstaende: list[BankStandZeile] = []
    for gesellschaft_id, gesellschaft_name in sorted(gesellschaften.items(), key=lambda paar: (paar[1], paar[0])):
        bank_konten = bank_repository.list_bank_konten(gesellschaft_id=gesellschaft_id)
        if not bank_konten:
            bankstaende.append(BankStandZeile(
                gesellschaft_id=gesellschaft_id, gesellschaft_name=gesellschaft_name, bank_konto_id=None,
                bank_konto_bezeichnung=None, bestaetigt_bis=None, letzte_buchung=None,
            ))
            continue
        for bank_konto in bank_konten:
            bankstaende.append(BankStandZeile(
                gesellschaft_id=gesellschaft_id, gesellschaft_name=gesellschaft_name,
                bank_konto_id=bank_konto.id, bank_konto_bezeichnung=bank_konto.bezeichnung,
                bestaetigt_bis=bank_service.bankvollstaendigkeit_bestaetigt_bis(bank_konto.id),
                letzte_buchung=bank_repository.letztes_buchungsdatum(bank_konto.id),
            ))

    return Saldenliste(
        objekt_filter=uebersicht.objekt_filter, objekt_optionen=uebersicht.objekt_optionen, status_filter=status,
        stichtag=heute, zeilen=tuple(zeilen), summen=summen, bankstaende=tuple(bankstaende),
    )
