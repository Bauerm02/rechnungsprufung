"""Monatsübersicht "Nettomieterlös laut Vorschreibung und
Monatsabrechnungen" (Auftrag 13.09., HV-20260913-DASHBOARD) - REIN
LESEND, bucht/ändert nichts. Kombiniert:

- Dauermiet-Soll NETTO (Summe aktiver Vertragskomponenten OHNE BK/HK/
  USt, über Verträge, die den gewählten Monat GÜLTIGKEITSMÄSSIG
  abdecken - "Küchen-/Parkplatzmiete soweit explizite Mietkomponenten"
  fließt automatisch mit ein, da nur BK/HK-Arten ausgeschlossen werden).
- Bestätigte (`status=BESTAETIGT`) Kurzzeit-/Selfstorage-Nettoanteile
  aus `VariableAbrechnungTable` für denselben Monat.

Nie Bank-Ist behaupten: `tatsaechlicher_zahlungseingang_cent` fließt
HIER NIRGENDS ein. Datenlücken (fehlender Monatsbericht, nur ENTWURF
ohne bestätigten Nettoanteil, Doppelzählungs-Konflikt Dauermiete+Report
für dieselbe Einheit/Periode) werden explizit aufgelistet statt eine
scheinbar vollständige Summe zu zeigen - die Summe enthält NUR, was
tatsächlich geprüft/bestätigt vorliegt.

Historische Periode: die Vertragsgültigkeit (`VertragTable.gueltig_von`/
`gueltig_bis`) entscheidet, ob ein Dauermiete-Soll für den gewählten
Monat zählt - NICHT der aktuelle `EinheitTable.nutzungsstatus` (der hat
keine Historie und würde für einen vergangenen Monat eine erfundene
Nutzungsverteilung unterstellen)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from mietinkasso.index.service import _NIE_INDEXIERBARE_ARTEN as _BK_HK_ARTEN
from mietinkasso.stammdaten.repository import StammdatenRepository
from mietinkasso.variableabrechnung.service import VariableAbrechnungService


def _monatsgrenzen(leistungsmonat: str) -> tuple[date, date]:
    jahr, monat = (int(teil) for teil in leistungsmonat.split("-"))
    anfang = date(jahr, monat, 1)
    ende = date(jahr + 1, 1, 1) if monat == 12 else date(jahr, monat + 1, 1)
    return anfang, ende - __import__("datetime").timedelta(days=1)


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
    leistungsmonat: str,
    stammdaten_repository: StammdatenRepository,
    variable_service: VariableAbrechnungService,
    gesellschaft_id: str | None = None,
) -> MonatsUebersicht:
    monatsanfang, monatsende = _monatsgrenzen(leistungsmonat)
    datenluecken: list[str] = []

    dauermiete_soll_netto_cent = 0
    dauermiete_einheiten_diesen_monat: set[str] = set()
    for vertrag in stammdaten_repository.list_alle_vertraege():
        if gesellschaft_id is not None and vertrag.gesellschaft_id != gesellschaft_id:
            continue
        if vertrag.gueltig_von > monatsende:
            continue
        if vertrag.gueltig_bis is not None and vertrag.gueltig_bis < monatsanfang:
            continue
        komponenten = stammdaten_repository.list_aktive_komponenten(vertrag.id, monatsanfang)
        miet_komponenten = [k for k in komponenten if k.art not in _BK_HK_ARTEN]
        if not miet_komponenten:
            continue
        dauermiete_soll_netto_cent += sum(k.betrag_cent for k in miet_komponenten)
        dauermiete_einheiten_diesen_monat.add(vertrag.einheit_id)

    aktuelle_berichte = variable_service.liste_aktuelle(leistungsmonat=leistungsmonat, gesellschaft_id=gesellschaft_id)
    kurzzeit_netto_anteil_cent = 0
    selfstorage_netto_anteil_cent = 0
    berichtete_einheiten: set[tuple[str, str]] = set()
    for bericht in aktuelle_berichte:
        berichtete_einheiten.add((bericht.einheit_id, bericht.art))
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
    alle_objekte = stammdaten_repository.list_objekte(gesellschaft_id=gesellschaft_id)
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
