"""Monatsübersicht "Nettomieterlös laut Vorschreibung und
Monatsabrechnungen" (Auftrag 13.09., HV-20260913-DASHBOARD) - REIN
LESEND, bucht/ändert nichts. Kombiniert:

- Dauermiet-Soll NETTO: Summe von EXPLIZIT bestätigten Netto-
  Mietanteilen (`KomponentenNettoMietFreigabeTable`, siehe
  `komponenten_freigabe.py`) für Vertragskomponenten, die den Monat
  VOLLSTÄNDIG (nicht nur untermonatlich) abdecken. `VertragsKomponenteTable
  .betrag_cent` allein wird NIE als Netto-Mietbasis übernommen - der
  Bestand enthält historisch auch BRUTTO gespeicherte Beträge (auch bei
  art=HMZ/KUECHE/PARKPLATZ) und pauschale Nebenleistungen; Art und
  `ust_satz_promille` allein sind kein Beleg. Nur eine geprüfte,
  eigenständige Freigabe mit Quellenbeleg zählt.
- Bestätigte (`status=BESTAETIGT`) Kurzzeit-/Selfstorage-Nettoanteile
  aus `VariableAbrechnungTable` für denselben Monat - eine Einheit mit
  Berichten für MEHRERE Arten im selben Monat ist ein Klassifizierungs-
  Konflikt und wird komplett von der Summe ausgeschlossen.

Nie Bank-Ist behaupten: `tatsaechlicher_zahlungseingang_cent` fließt
HIER NIRGENDS ein. Ausgeschlossene Objekte (Pilotausschluss) werden an
JEDER Stelle (Lesen/Summieren/Hinweise) übersprungen. Datenlücken
(fehlender Monatsbericht, nur ENTWURF, ungeprüfte Bestandskomponente,
untermonatliche Vertrags-/Komponentengültigkeit, Arten- oder
Doppelzählungs-Konflikt) werden explizit aufgelistet statt eine
scheinbar vollständige Summe zu zeigen - die Summe enthält NUR, was
tatsächlich geprüft/belegt vorliegt.

Historische Periode: die Vertragsgültigkeit (`VertragTable.gueltig_von`/
`gueltig_bis`) entscheidet, ob ein Dauermiete-Soll für den gewählten
Monat zählt - NICHT der aktuelle `EinheitTable.nutzungsstatus` (der hat
keine Historie und würde für einen vergangenen Monat eine erfundene
Nutzungsverteilung unterstellen)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from mietinkasso.auth.service import AuthContext
from mietinkasso.stammdaten.repository import StammdatenRepository
from mietinkasso.variableabrechnung.komponenten_freigabe import KomponentenNettoMietFreigabeService
from mietinkasso.variableabrechnung.service import VariableAbrechnungService

#: Bewusst eine ENGE, positive Liste plausibler Miet-Komponentenarten
#: (kein Blacklist-Ausschluss von Ertragsarten) - Bestandsdaten enthalten
#: u. a. BK_VZ, HK_VZ, BK_PARKPLATZ, WASSER, STROM als NICHT-Miet-Arten,
#: die hier bewusst NICHT gelistet sind. Mitgliedschaft in dieser Liste
#: ist NOTWENDIG, aber NICHT hinreichend - zusätzlich ist immer eine
#: aktive `KomponentenNettoMietFreigabeTable`-Zeile erforderlich (Art
#: allein, auch HMZ, ist kein Beleg für einen tatsächlich Netto
#: gespeicherten Betrag).
_MOEGLICHE_MIET_ARTEN = frozenset({"HMZ", "KUECHE", "PARKPLATZ", "STELLPLATZ"})


def _monatsgrenzen(leistungsmonat: str) -> tuple[date, date]:
    jahr, monat = (int(teil) for teil in leistungsmonat.split("-"))
    anfang = date(jahr, monat, 1)
    ende = date(jahr + 1, 1, 1) if monat == 12 else date(jahr, monat + 1, 1)
    return anfang, ende - timedelta(days=1)


def _deckt_vollen_monat(gueltig_von: date, gueltig_bis: date | None, monatsanfang: date, monatsende: date) -> bool:
    return gueltig_von <= monatsanfang and (gueltig_bis is None or gueltig_bis >= monatsende)


def _ueberlappt_monat(gueltig_von: date, gueltig_bis: date | None, monatsanfang: date, monatsende: date) -> bool:
    return gueltig_von <= monatsende and (gueltig_bis is None or gueltig_bis >= monatsanfang)


@dataclass(frozen=True)
class MonatsUebersicht:
    leistungsmonat: str
    dauermiete_soll_netto_cent: int
    kurzzeit_netto_anteil_cent: int
    selfstorage_netto_anteil_cent: int
    nettomieterloes_cent: int
    datenluecken: tuple[str, ...] = field(default_factory=tuple)

    @property
    def vollstaendig(self) -> bool:
        return not self.datenluecken


def berechne_monatsuebersicht(
    *,
    ctx: AuthContext,
    leistungsmonat: str,
    stammdaten_repository: StammdatenRepository,
    variable_service: VariableAbrechnungService,
    komponenten_freigabe_service: KomponentenNettoMietFreigabeService,
    gesellschaft_id: str | None = None,
) -> MonatsUebersicht:
    monatsanfang, monatsende = _monatsgrenzen(leistungsmonat)
    datenluecken: list[str] = []

    dauermiete_soll_netto_cent = 0
    dauermiete_einheiten_diesen_monat: set[str] = set()
    for vertrag in stammdaten_repository.list_alle_vertraege():
        if gesellschaft_id is not None and vertrag.gesellschaft_id != gesellschaft_id:
            continue
        if not ctx.has_zugriff(vertrag.gesellschaft_id):
            continue
        try:
            objekt = stammdaten_repository.objekt_fuer_einheit(vertrag.einheit_id)
        except ValueError:
            continue
        if objekt.ausgeschlossen:
            continue
        if not _ueberlappt_monat(vertrag.gueltig_von, vertrag.gueltig_bis, monatsanfang, monatsende):
            continue
        if not _deckt_vollen_monat(vertrag.gueltig_von, vertrag.gueltig_bis, monatsanfang, monatsende):
            datenluecken.append(
                f"Vertrag '{vertrag.id}' (Einheit '{vertrag.einheit_id}'): deckt {leistungsmonat} nur "
                "UNTERmonatlich ab (Beginn/Ende innerhalb des Monats) - kein automatisch berechneter "
                "anteiliger Wert, nicht in der Summe enthalten."
            )
            continue

        komponenten = stammdaten_repository.list_aktive_komponenten(vertrag.id, monatsanfang)
        kandidaten = [
            k for k in komponenten
            if k.art in _MOEGLICHE_MIET_ARTEN and _deckt_vollen_monat(k.gueltig_von, k.gueltig_bis, monatsanfang, monatsende)
        ]
        if not kandidaten:
            datenluecken.append(
                f"Vertrag '{vertrag.id}' (Einheit '{vertrag.einheit_id}'): aktiv in {leistungsmonat}, aber "
                "keine für den vollen Monat gültige Mietkomponente (HMZ/Küche/Parkplatz/Stellplatz) - "
                "kein Dauermiete-Soll gezählt."
            )
            continue

        vertrag_summe = 0
        hatte_freigabe = False
        for komponente in kandidaten:
            freigabe = komponenten_freigabe_service.aktive_freigabe_fuer_monat(
                komponente.id, monatsanfang=monatsanfang, monatsende=monatsende
            )
            if freigabe is None:
                datenluecken.append(
                    f"Komponente '{komponente.id}' (Vertrag '{vertrag.id}', Art {komponente.art}): keine "
                    f"geprüfte Netto-Mietanteil-Freigabe für {leistungsmonat} - Betrag/Art allein sind "
                    "kein Beleg, nicht in der Summe enthalten."
                )
                continue
            vertrag_summe += freigabe.bestaetigter_netto_betrag_cent
            hatte_freigabe = True

        if hatte_freigabe:
            dauermiete_soll_netto_cent += vertrag_summe
            dauermiete_einheiten_diesen_monat.add(vertrag.einheit_id)

    aktuelle_berichte = variable_service.liste_aktuelle(ctx=ctx, leistungsmonat=leistungsmonat, gesellschaft_id=gesellschaft_id)

    # Artenkonflikt: dieselbe Einheit kann nicht gleichzeitig
    # KURZZEITVERMIETUNG UND SELFSTORAGE für denselben Monat sein -
    # unabhängiger Review, synthetisch reproduziert (450+450=900 ohne
    # Warnung). Beide/alle Arten werden gesperrt, bis eine eindeutige
    # Zuordnung vorliegt, statt stillschweigend zu addieren.
    arten_je_einheit: dict[str, set[str]] = {}
    for bericht in aktuelle_berichte:
        arten_je_einheit.setdefault(bericht.einheit_id, set()).add(bericht.art)
    konflikt_einheiten = {einheit_id for einheit_id, arten in arten_je_einheit.items() if len(arten) > 1}
    for einheit_id in sorted(konflikt_einheiten):
        datenluecken.append(
            f"Einheit '{einheit_id}': Monatsberichte für MEHRERE Arten "
            f"({', '.join(sorted(arten_je_einheit[einheit_id]))}) in {leistungsmonat} vorhanden - eine "
            "Einheit kann nicht gleichzeitig mehrfach klassifiziert sein. Alle betroffenen Berichte "
            "werden gesperrt, bis eine eindeutige, periodengültige Zuordnung vorliegt."
        )

    kurzzeit_netto_anteil_cent = 0
    selfstorage_netto_anteil_cent = 0
    berichtete_einheiten: set[tuple[str, str]] = set()
    for bericht in aktuelle_berichte:
        berichtete_einheiten.add((bericht.einheit_id, bericht.art))
        if bericht.einheit_id in konflikt_einheiten:
            continue
        if bericht.einheit_id in dauermiete_einheiten_diesen_monat:
            datenluecken.append(
                f"Einheit '{bericht.einheit_id}': sowohl Dauermiete-Soll ALS AUCH ein "
                f"{bericht.art}-Monatsbericht für {leistungsmonat} vorhanden - Doppelzählung vermieden, "
                "Report wird NICHT in die Summe aufgenommen. Bitte Vertrags-/Nutzungsstatus prüfen."
            )
            continue
        if bericht.status != "BESTAETIGT":
            datenluecken.append(
                f"Einheit '{bericht.einheit_id}' ({bericht.art}, {leistungsmonat}): nur ENTWURF ohne "
                "bestätigten Nettoanteil - nicht in der Summe enthalten."
            )
            continue
        if bericht.unser_netto_anteil_cent is None:
            # Sollte bei status=BESTAETIGT durch die Service-Validierung
            # nie vorkommen - defensiv trotzdem als Datenlücke sichtbar,
            # statt stillschweigend 0 zu addieren.
            datenluecken.append(
                f"Einheit '{bericht.einheit_id}' ({bericht.art}, {leistungsmonat}): BESTAETIGT ohne "
                "Nettoanteil - unerwarteter Datenzustand, bitte prüfen."
            )
            continue
        if bericht.art == "KURZZEITVERMIETUNG":
            kurzzeit_netto_anteil_cent += bericht.unser_netto_anteil_cent
        elif bericht.art == "SELFSTORAGE":
            selfstorage_netto_anteil_cent += bericht.unser_netto_anteil_cent

    # Hinweis auf fehlende Monatsberichte - AUSDRÜCKLICH nur ein Hinweis
    # anhand des AKTUELLEN Nutzungsstatus (keine Tatsachenbehauptung über
    # die Vergangenheit): eine Einheit, die HEUTE als KURZZEITVERMIETUNG/
    # SELFSTORAGE geführt wird, aber für den gewählten Monat weder einen
    # Dauermiete-Vertrag noch einen Report hat, wird sichtbar gemacht.
    # Ausgeschlossene Objekte werden dabei komplett übersprungen.
    alle_objekte = [
        o for o in stammdaten_repository.list_objekte(gesellschaft_id=gesellschaft_id)
        if not o.ausgeschlossen and ctx.has_zugriff(o.gesellschaft_id)
    ]
    for objekt in alle_objekte:
        for einheit in stammdaten_repository.list_einheiten_fuer_objekt(objekt.id):
            if einheit.nutzungsstatus not in ("KURZZEITVERMIETUNG", "SELFSTORAGE"):
                continue
            if einheit.id in dauermiete_einheiten_diesen_monat:
                continue
            if (einheit.id, einheit.nutzungsstatus) in berichtete_einheiten:
                continue
            datenluecken.append(
                f"Einheit '{einheit.id}' (aktueller Nutzungsstatus {einheit.nutzungsstatus}): kein "
                f"Monatsbericht für {leistungsmonat} vorhanden - Hinweis anhand AKTUELLEM Status, keine "
                "rückwirkende Tatsachenbehauptung."
            )

    nettomieterloes_cent = dauermiete_soll_netto_cent + kurzzeit_netto_anteil_cent + selfstorage_netto_anteil_cent
    return MonatsUebersicht(
        leistungsmonat=leistungsmonat,
        dauermiete_soll_netto_cent=dauermiete_soll_netto_cent,
        kurzzeit_netto_anteil_cent=kurzzeit_netto_anteil_cent,
        selfstorage_netto_anteil_cent=selfstorage_netto_anteil_cent,
        nettomieterloes_cent=nettomieterloes_cent,
        datenluecken=tuple(datenluecken),
    )
