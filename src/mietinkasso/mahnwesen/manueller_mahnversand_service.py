"""Auftrag HV-20261022-EIN-KONTOBLATT: Service-Orchestrierung für den
kontrollierten Backoffice-Import tatsächlich manuell (außerhalb des
automatisierten Mahnlaufs) versendeter Mahnungen - siehe
`manueller_mahnversand_repository.py` für die Persistenz-/Idempotenz-
Details.

`importieren` selbst löst NIEMALS einen tatsächlichen Versand/eine
Aktivierung aus - es dokumentiert nur einen BEREITS geschehenen,
außerhalb dieses Systems erfolgten Versand.

**Korrektur nach unabhängiger Rückprüfung Codex 22.10.2026, echter Bug
(ursprünglich automatisch gebucht):** ein echtes manuelles Schreiben
enthält KEINE vom System vorausberechneten Zinsen/Gebühren - `importieren`
bucht deshalb standardmäßig (`sofort_buchen=False`) GAR NICHTS, sondern
dokumentiert nur den Nachweis. Die tatsächliche Buchung (Zinsen-Delta UND
zu diesem Zeitpunkt BEREITS freigegebene Mahnspesen/-pauschale - eine noch
NICHT freigegebene Pauschale wird NIE automatisch angesetzt) erfolgt
GETRENNT über eine explizite Preview→Bestätigen-Aktion
(`vorschau_fuer_bestehenden_import`/`bestaetige_buchung`, NIE per GET) -
EXAKT wie ein echter automatisierter Mahnversand über die bereits
bestehende `MahnkostenService.vorschau`/`buche_vorschau` (KEIN zweites
Buchungsmodul). `sofort_buchen=True` bleibt als Kompatibilitätspfad für
Aufrufer, die beides in einem Schritt wollen (z. B. ein bereits
offensichtlich korrektes, sofort zu verbuchendes Nachtragsprotokoll).

Eine SPÄTER erteilte Mahnpauschale-/§458-Freigabe löst über
`nachbuche_fuer_vertrag_stufe` (von der Freigabe-Route aufgerufen) GENAU
EINE Nachbuchung aus (`kosten_service.py::MahnkostenService.
nachbuche_offene_kosten`) - unabhängig davon, ob die ursprüngliche Buchung
über `sofort_buchen=True` oder `bestaetige_buchung` erfolgte.
`ManuellerMahnversandTable.pauschale_abgegolten` verhindert dabei, dass
EINE SPÄTERE, andere Freigabe (z. B. nach einer Teilzahlung neu auf eine
geänderte Kapitalbasis erteilt) denselben, bereits einmal bepauschalten
Beleg ein zweites Mal belastet - die Nachbuchung ist an den KONKRETEN
Beleg gebunden, nicht an die zuletzt erteilte Freigabe-Id."""

from __future__ import annotations

import hashlib
import io
import json
from datetime import date

from pypdf import PdfReader

from mietinkasso.auth.service import AuthContext, require_gesellschaft_access, require_schreibrecht
from mietinkasso.mahnwesen.kosten import forderungsgruppe_schluessel
from mietinkasso.mahnwesen.kosten_service import MahnkostenService
from mietinkasso.mahnwesen.manueller_mahnversand_repository import ManuellerMahnversandRepository
from mietinkasso.stammdaten.repository import StammdatenRepository

_MAX_BELEG_PDF_BYTES = 15 * 1024 * 1024
_ERLAUBTE_KANAELE = frozenset({"BRIEF", "EMAIL", "PERSOENLICH"})


def _pruefe_beleg_pdf(pdf_bytes: bytes) -> None:
    """Wie `mahnbrief_archiv.py::_pruefe_anbieterbeleg_pdf` (bewusst
    EIGENSTÄNDIG dupliziert, nicht importiert - siehe dortige Begründung
    für ähnliche, absichtlich getrennte Validierungen in diesem Modul):
    ein bloßer Referenztext/eine bloße `%PDF-`-Kopfzeile ist KEIN Beleg -
    verlangt wird ein tatsächlich mit `pypdf` lesbares, nicht
    verschlüsseltes PDF mit mindestens einer Seite, unterhalb der
    Größengrenze."""

    if not pdf_bytes or pdf_bytes[:5] != b"%PDF-":
        raise ValueError("Ein Manualimport verlangt das tatsächliche Belegdokument (PDF) - ein Referenztext allein ist kein Beleg.")
    if len(pdf_bytes) > _MAX_BELEG_PDF_BYTES:
        raise ValueError(f"Belegdatei überschreitet die zulässige Größe ({_MAX_BELEG_PDF_BYTES // (1024 * 1024)} MB).")
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        if reader.is_encrypted:
            raise ValueError("Beleg-PDF ist verschlüsselt - kein lesbarer Beleg.")
        if len(reader.pages) < 1:
            raise ValueError("Beleg-PDF hat keine lesbare Seite - kein gültiger Beleg.")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Beleg-PDF ist nicht lesbar/beschädigt ({type(exc).__name__}) - kein gültiger Beleg.") from exc


class ManuellerMahnversandService:
    def __init__(
        self, *, repository: ManuellerMahnversandRepository, stammdaten_repository: StammdatenRepository,
        op_service, mahnkosten_service: MahnkostenService,
    ):
        self._repository = repository
        self._stammdaten_repository = stammdaten_repository
        self._op_service = op_service
        self._mahnkosten_service = mahnkosten_service

    def importieren(
        self, *, ctx: AuthContext, vertrag_id: str, stufe: int, forderung_op_position_ids: list[int],
        versanddatum: date, kanal: str, empfaenger_name: str, empfaenger_adresse: str, beleg_referenz: str,
        beleg_pdf_bytes: bytes, erfasst_von: str, sofort_buchen: bool = False,
    ):
        """Dokumentiert den Nachweis. Bucht standardmäßig NICHTS
        (`sofort_buchen=False`, siehe Moduldoc) - ein echtes manuelles
        Schreiben enthält selbst keine vom System vorausberechneten
        Zinsen. `sofort_buchen=True` bucht zusätzlich sofort (Zinsen-Delta
        + bereits freigegebene Kosten) - für Aufrufer, die beides explizit
        in einem Schritt wollen; die eigentliche, empfohlene Buchung
        erfolgt über `bestaetige_buchung` NACH einer Vorschau."""

        vertrag = self._stammdaten_repository.get_vertrag(vertrag_id)
        konto = self._stammdaten_repository.get_konto_by_vertrag(vertrag_id)
        if vertrag is None or konto is None:
            raise ValueError(f"Unbekannter Vertrag {vertrag_id} oder kein zugehöriges Konto.")
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)

        if stufe not in (1, 2, 3, 4):
            raise ValueError("Stufe muss zwischen 1 und 4 liegen.")
        if kanal not in _ERLAUBTE_KANAELE:
            raise ValueError(f"Unbekannter Kanal {kanal!r} - zulässig: {sorted(_ERLAUBTE_KANAELE)}.")
        if not forderung_op_position_ids:
            raise ValueError("Mindestens eine Forderungs-OP-Id ist erforderlich (Forderungsbindung).")
        if not beleg_referenz or not beleg_referenz.strip():
            raise ValueError("Eine Belegreferenz ist Pflicht.")
        if not empfaenger_name.strip() or not empfaenger_adresse.strip():
            raise ValueError("Empfänger (Name und Adresse/Zugang) sind Pflicht.")

        # Cross-Contract-/Cross-Gesellschaft-Ablehnung: JEDE angegebene
        # Forderung muss tatsächlich zu GENAU diesem Vertrag/Konto
        # gehören - kein stiller Import einer fremden Forderung.
        for op_id in forderung_op_position_ids:
            position = self._op_service.get_position(op_id)
            if position is None or position.konto_id != konto.id:
                raise ValueError(
                    f"Forderung #{op_id} gehört nicht zu Vertrag {vertrag_id}/Konto {konto.id} - "
                    "Cross-Contract-/Cross-Gesellschaft-Import wird abgelehnt."
                )

        _pruefe_beleg_pdf(beleg_pdf_bytes)
        beleg_sha256 = hashlib.sha256(beleg_pdf_bytes).hexdigest()

        row = self._repository.importieren(
            vertrag_id=vertrag_id, stufe=stufe, forderung_op_position_ids=forderung_op_position_ids,
            versanddatum=versanddatum, kanal=kanal, empfaenger_name=empfaenger_name.strip(),
            empfaenger_adresse=empfaenger_adresse.strip(), beleg_referenz=beleg_referenz.strip(),
            beleg_pdf_bytes=beleg_pdf_bytes, beleg_sha256=beleg_sha256, erfasst_von=erfasst_von,
        )
        if row.mahnkosten_buchung_id is not None:
            return row  # echter Retry (identischer Nachweis) - bereits verarbeitet, kein zweiter Buchungsversuch
        if sofort_buchen:
            self._buche_und_markiere(ctx=ctx, row=row, heute=versanddatum, akteur=erfasst_von)
            row = self._repository.get(row.id)  # frische Zeile NACH der Buchung zurückgeben, nicht die veraltete
        return row

    def vorschau_fuer_bestehenden_import(self, *, row_id: int, heute: date):
        """Reine Lesevorschau (GET, bucht NICHTS) der Buchung, die
        `bestaetige_buchung` für DIESEN bereits importierten Beleg
        auslösen würde - Zinsen-Delta + zu diesem Zeitpunkt bereits
        freigegebene Kosten, exakt dieselbe `MahnkostenService.vorschau`
        wie überall sonst."""

        row = self._repository.get(row_id)
        if row is None:
            return None
        return self._mahnkosten_service.vorschau(
            vertrag_id=row.vertrag_id, stufe=row.stufe, heute=heute,
            nur_op_position_ids=frozenset(json.loads(row.forderung_op_position_ids_json)),
            kanal=row.kanal if row.kanal in ("EMAIL", "BRIEF") else "EMAIL",
        )

    def bestaetige_buchung(self, *, ctx: AuthContext, row_id: int, heute: date, akteur: str):
        """Explizite Preview→Bestätigen-Buchung (NIE per GET) für einen
        bereits dokumentierten, bislang NICHT gebuchten Manualimport -
        siehe Moduldoc. Bucht GENAU EINMAL je Beleg (ein erneuter Aufruf
        nach bereits erfolgter Buchung ist ein sicherer No-Op, `None`) -
        eine spätere, über eine Mahnpauschale-Freigabe ausgelöste
        Nachbuchung läuft GETRENNT über `nachbuche_fuer_vertrag_stufe`."""

        row = self._repository.get(row_id)
        if row is None:
            raise ValueError(f"Unbekannte Manualimport-Zeile {row_id}.")
        vertrag = self._stammdaten_repository.get_vertrag(row.vertrag_id)
        if vertrag is None:
            raise ValueError(f"Unbekannter Vertrag {row.vertrag_id}.")
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)
        if row.mahnkosten_buchung_id is not None:
            return None  # bereits gebucht - kein zweiter Versuch
        return self._buche_und_markiere(ctx=ctx, row=row, heute=heute, akteur=akteur)

    def _buche_und_markiere(self, *, ctx: AuthContext, row, heute: date, akteur: str):
        """Buchung EXAKT wie bei einem echten Versand (kein neues/zweites
        Buchungsmodul): Zinsen-Delta + zu DIESEM Zeitpunkt bereits
        freigegebene Kosten. Eine noch nicht freigegebene Pauschale wird
        hier bewusst NICHT gebucht (bleibt "in Prüfung" sichtbar) - das
        ist KEIN Fehler, sondern der Normalfall vor einer Freigabe.

        `mahnlauf_schluessel_override` ist DAUERHAFT an `row.
        idempotenz_schluessel` gebunden (unabhängige Rückprüfung Codex
        22.10.2026, echter Bug: ohne Override kollidierten ZWEI echte,
        unterschiedliche manuelle Mahnungen für DIESELBE Forderungsmenge/
        Stufe über den generischen, nur forderungsmengen-basierten
        Schlüssel - ein zweites, echtes Schreiben konnte dadurch gar
        nicht gebucht werden). Setzt `pauschale_abgegolten`, sobald
        TATSÄCHLICH eine Gebühr/Pauschale gebucht wurde (unabhängig
        davon, ob das hier oder später über eine Nachbuchung geschah)."""

        forderung_ids = json.loads(row.forderung_op_position_ids_json)
        vorschau = self._mahnkosten_service.vorschau(
            vertrag_id=row.vertrag_id, stufe=row.stufe, heute=heute,
            nur_op_position_ids=frozenset(forderung_ids), kanal=row.kanal if row.kanal in ("EMAIL", "BRIEF") else "EMAIL",
        )
        if vorschau is None or not vorschau.forderung_op_position_ids:
            return None
        ledger = self._mahnkosten_service.buche_vorschau(
            ctx=ctx, vorschau=vorschau, heute=heute,
            versandnachweis_referenz=f"MANUALIMPORT:{row.idempotenz_schluessel}", akteur=akteur,
            mahnlauf_schluessel_override=f"MANUALIMPORT:{row.idempotenz_schluessel}",
        )
        if ledger is not None:
            pauschale_jetzt_abgegolten = bool(ledger.gebuehr_cent or ledger.mahnpauschale_cent)
            self._repository.setze_mahnkosten_buchung_id(
                id_=row.id, mahnkosten_buchung_id=ledger.id, pauschale_abgegolten=pauschale_jetzt_abgegolten,
            )
        return ledger

    def nachbuche_fuer_vertrag_stufe(
        self, *, ctx: AuthContext, vertrag_id: str, stufe: int, forderungsgruppe_schluessel_neu: str,
        heute: date, akteur: str, freigabe_id: int,
    ) -> list:
        """Von der Mahnpauschale-Freigabe-Route aufgerufen (Auftrag
        HV-20261022-EIN-KONTOBLATT, Punkt 3 - verbindlicher Teil des
        Auftrags): sucht JEDEN Manualimport-Datensatz für (vertrag_id,
        stufe), dessen eigene, eingefrorene Forderungsmenge GENAU zu der
        soeben freigegebenen Forderungsgruppe passt, und löst für jeden
        GENAU EINE Nachbuchung aus (`MahnkostenService.
        nachbuche_offene_kosten`). Gibt die Liste der tatsächlich neu
        gebuchten Ledger-Zeilen zurück (leer, wenn nichts zu buchen war).

        **Korrektur nach unabhängiger Rückprüfung Codex 22.10.2026, echter
        Bug:** die Idempotenz war bisher an die AUSLÖSENDE `freigabe_id`
        gebunden - eine ZWEITE, später für DIESELBE Forderungsgruppe
        erteilte Freigabe (z. B. nach einer Teilzahlung neu auf eine
        geänderte Kapitalbasis) hätte denselben, bereits einmal
        bepauschalten Beleg dadurch ein ZWEITES Mal belastet. Jede Zeile
        mit `pauschale_abgegolten=True` wird deshalb HIER VORAB
        übersprungen, unabhängig von `freigabe_id` - GENAU EINE
        Mahnspesen-/Mahnkostenpauschale je Beleg, für immer."""

        gebuchte = []
        for row in self._repository.list_fuer_vertrag(vertrag_id):
            if row.stufe != stufe or row.pauschale_abgegolten:
                continue
            eigene_ids = json.loads(row.forderung_op_position_ids_json)
            if forderungsgruppe_schluessel(eigene_ids) != forderungsgruppe_schluessel_neu:
                continue
            ledger = self._mahnkosten_service.nachbuche_offene_kosten(
                ctx=ctx, vertrag_id=vertrag_id, stufe=stufe, forderung_op_position_ids=eigene_ids,
                heute=heute, akteur=akteur, ausloeser_referenz=f"FREIGABE:{freigabe_id}:MANUALIMPORT:{row.id}",
            )
            if ledger is not None:
                pauschale_jetzt_abgegolten = bool(ledger.gebuehr_cent or ledger.mahnpauschale_cent)
                self._repository.setze_mahnkosten_buchung_id(
                    id_=row.id, mahnkosten_buchung_id=ledger.id, pauschale_abgegolten=pauschale_jetzt_abgegolten,
                )
                gebuchte.append(ledger)
        return gebuchte
