"""Monatliche Mietvorschreibung: Entwurf -> Sollstellung -> Dokumentzustellung
-> Hauptbuch-Export -> Zahlungsabgleich, strikt in dieser Reihenfolge und
strikt einmal pro Vertrag/Monat (Fachregel 3)."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date

from mietinkasso.auth.service import AuthContext, require_gesellschaft_access, require_schreibrecht
from mietinkasso.domain.enums import OPTyp, VorschreibungStatus
from mietinkasso.domain.exceptions import BindungInkonsistentError, NachweisFehltError
from mietinkasso.infrastructure.db.tables import KontoTable, VertragTable
from mietinkasso.op.service import OPService
from mietinkasso.stammdaten.repository import StammdatenRepository
from mietinkasso.vorschreibung.repository import VorschreibungRepository


def faelligkeitsdatum(monat: str, faelligkeit_tag: int) -> date:
    jahr, monatszahl = (int(part) for part in monat.split("-"))
    letzter_tag = calendar.monthrange(jahr, monatszahl)[1]
    tag = min(faelligkeit_tag, letzter_tag)
    return date(jahr, monatszahl, tag)


@dataclass(frozen=True)
class VorschreibungsErgebnis:
    vorschreibung_id: int
    status: str
    summe_cent: int


class VorschreibungService:
    def __init__(
        self,
        repository: VorschreibungRepository,
        stammdaten_repository: StammdatenRepository,
        op_service: OPService,
    ):
        self._repository = repository
        self._stammdaten_repository = stammdaten_repository
        self._op_service = op_service

    def entwurf_erstellen(
        self, *, ctx: AuthContext, vertrag: VertragTable, monat: str
    ) -> VorschreibungsErgebnis:
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        faelligkeit = faelligkeitsdatum(monat, vertrag.faelligkeit_tag)
        vorschreibung = self._repository.get_or_create_entwurf(
            vertrag_id=vertrag.id, monat=monat, faelligkeit=faelligkeit
        )
        bestehende_positionen = self._repository.list_positionen(vorschreibung.id)
        if not bestehende_positionen:
            stichtag = faelligkeitsdatum(monat, 1)
            komponenten = self._stammdaten_repository.list_aktive_komponenten(vertrag.id, stichtag)
            for komponente in komponenten:
                self._repository.add_position(
                    vorschreibung_id=vorschreibung.id,
                    art=komponente.art,
                    bezeichnung=komponente.bezeichnung,
                    betrag_cent=komponente.betrag_cent,
                    ust_satz_promille=komponente.ust_satz_promille,
                )
            bestehende_positionen = self._repository.list_positionen(vorschreibung.id)
        summe = sum(p.betrag_cent for p in bestehende_positionen)
        return VorschreibungsErgebnis(vorschreibung_id=vorschreibung.id, status=vorschreibung.status, summe_cent=summe)

    def sollstellen(
        self, *, ctx: AuthContext, vertrag: VertragTable, konto: KontoTable, monat: str, heute: date | None = None
    ) -> VorschreibungsErgebnis:
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)
        if konto.vertrag_id != vertrag.id:
            raise BindungInkonsistentError(f"Konto {konto.id} gehört zu Vertrag {konto.vertrag_id}, nicht zu {vertrag.id}.")
        heute = heute or date.today()
        vorschreibung = self._repository.get(vertrag.id, monat)
        if vorschreibung is None:
            raise ValueError(f"Kein Entwurf für Vertrag {vertrag.id} / {monat} vorhanden.")
        positionen = self._repository.list_positionen(vorschreibung.id)
        summe = sum(p.betrag_cent for p in positionen)
        if vorschreibung.status == VorschreibungStatus.ENTWURF.value:
            op_row = self._op_service.buchen(
                ctx=ctx,
                konto=konto,
                typ=OPTyp.SOLL,
                betrag_cent=summe,
                belegdatum=vorschreibung.faelligkeit,
                buchungsdatum=heute,
                faelligkeit=vorschreibung.faelligkeit,
                beleg_referenz=f"Vorschreibung {monat}",
                leistungsperiode=monat,
                import_id=f"VORSCHREIBUNG-{vertrag.id}-{monat}",
                quelle_system="vorschreibung",
            )
            vorschreibung = self._repository.update_status(
                vorschreibung.id, status=VorschreibungStatus.SOLLGESTELLT.value, op_position_id=op_row.id
            )
        return VorschreibungsErgebnis(vorschreibung_id=vorschreibung.id, status=vorschreibung.status, summe_cent=summe)

    def dokument_zustellen(self, *, ctx: AuthContext, vorschreibung_id: int, zustellnachweis: dict) -> None:
        """`zustellnachweis` muss die tatsächliche Bestätigung eines
        Versandadapters sein (z. B. {"kanal": "email", "provider_referenz":
        "..."}), niemals ein Platzhalter. Ohne echten Nachweis wird der
        Status NICHT auf ZUGESTELLT gesetzt - MVP1 hat keinen Adapter, also
        ruft aktuell niemand diese Methode ohne einen von außen erbrachten
        Nachweis auf (siehe OFFENE_PUNKTE.md)."""

        from datetime import datetime, timezone

        if not zustellnachweis:
            raise NachweisFehltError(
                f"Vorschreibung {vorschreibung_id}: Dokumentzustellung ohne Zustellnachweis wird abgelehnt."
            )
        vorschreibung = self._get_or_raise(vorschreibung_id)
        vertrag = self._stammdaten_repository.get_vertrag(vorschreibung.vertrag_id)
        if vertrag is None:
            raise ValueError(f"Unbekannter Vertrag {vorschreibung.vertrag_id}")
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)
        if vorschreibung.status not in (VorschreibungStatus.SOLLGESTELLT.value, VorschreibungStatus.ZUGESTELLT.value):
            raise ValueError("Dokumentzustellung erst nach Sollstellung möglich.")
        if vorschreibung.dokument_zugestellt_am is None:
            self._repository.update_status(
                vorschreibung_id,
                status=VorschreibungStatus.ZUGESTELLT.value,
                dokument_zugestellt_am=datetime.now(timezone.utc),
                zustellnachweis=zustellnachweis,
            )

    def hauptbuch_exportieren(self, *, ctx: AuthContext, vorschreibung_id: int, export_nachweis: dict) -> None:
        """Wie `dokument_zustellen`: erfordert einen echten Nachweis (z. B.
        {"zielsystem": "...", "export_referenz": "..."}) und erst NACH
        ZUGESTELLT - ein Export direkt aus ENTWURF/SOLLGESTELLT wird
        abgelehnt."""

        from datetime import datetime, timezone

        if not export_nachweis:
            raise NachweisFehltError(
                f"Vorschreibung {vorschreibung_id}: Hauptbuch-Export ohne Exportnachweis wird abgelehnt."
            )
        vorschreibung = self._get_or_raise(vorschreibung_id)
        vertrag = self._stammdaten_repository.get_vertrag(vorschreibung.vertrag_id)
        if vertrag is None:
            raise ValueError(f"Unbekannter Vertrag {vorschreibung.vertrag_id}")
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)
        if vorschreibung.status not in (VorschreibungStatus.ZUGESTELLT.value, VorschreibungStatus.EXPORTIERT.value):
            raise ValueError(
                f"Vorschreibung {vorschreibung_id} ist im Status {vorschreibung.status}; ein Hauptbuch-Export "
                "ist erst nach ZUGESTELLT zulässig."
            )
        if vorschreibung.hauptbuch_exportiert_am is None:
            self._repository.update_status(
                vorschreibung_id,
                status=VorschreibungStatus.EXPORTIERT.value,
                hauptbuch_exportiert_am=datetime.now(timezone.utc),
                export_nachweis=export_nachweis,
            )

    def _get_or_raise(self, vorschreibung_id: int):
        with self._repository._session_factory() as session:  # thin, read-only lookup
            from mietinkasso.infrastructure.db.tables import VorschreibungTable

            row = session.get(VorschreibungTable, vorschreibung_id)
            if row is None:
                raise ValueError(f"Unbekannte Vorschreibung {vorschreibung_id}")
            return row
