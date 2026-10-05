"""Anbieterneutraler, geschützter Abruf-Einstieg für eine gebundene
Objekt-Bankquelle (Auftrag HV-20261005-BANKQUELLENBINDUNG).

Es gibt hier KEINE echte Bankanbindung, keinen Scheduler und keine
Zugangsdaten: der Aufrufer injiziert einen nur lesenden Adapter
(`BankAnbieterAdapter`). Produktiv existiert derzeit kein solcher
Adapter; Tests verwenden ausschließlich synthetische Fakes. Das separate
PHP-Modul `ebics-downloader` bleibt unberührt.

Ablauf (`GeschuetzterBankabruf.abrufen`):

1. Kontext ausschließlich aus der aktiven, persistierten Objektbindung
   (`QuellenbindungService.kontext_fuer_objekt`) - der Aufrufer wählt kein
   Konto und keine Anbieter-Kennung.
2. `adapter.kontoinfo` für GENAU dieses Tupel; Tupel, IBAN und Rolle
   (`MIETE`) müssen exakt passen, BEVOR Umsätze angefordert werden.
3. `adapter.umsaetze_abrufen`; die Lieferung muss wieder dasselbe Tupel
   und dieselbe IBAN tragen und CAMT.053 sein.
4. Import ausschließlich über `BankImportService.importiere_camt053` mit
   demselben Kontext - der prüft die Bindung vor dem Parsen und erneut in
   der Schreibtransaktion (Widerruf/Neubindung während des Abrufs ->
   Abbruch ohne Schreibzugriff). Kein zweiter Schreibpfad."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from mietinkasso.auth.service import AuthContext, require_schreibrecht
from mietinkasso.bank.importer import _normalisiere_iban
from mietinkasso.bank.quellen_models import KONTOROLLE_MIETE
from mietinkasso.bank.quellenbindung import BankQuellenKontext, BankquellenBindungError
from mietinkasso.bank.repository import BankRepository
from mietinkasso.bank.service import BankImportService
from mietinkasso.infrastructure.db.tables import BankTransaktionTable


class AnbieterAntwortError(BankquellenBindungError):
    """Die Anbieterantwort passt nicht zur gebundenen Quelle (anderes
    Konto/Tupel, andere IBAN, andere Rolle, falsches Format). Es wurde
    nichts importiert."""


@dataclass(frozen=True)
class AnbieterKontoInfo:
    anbieter: str
    zugang_ref: str
    konto_ref: str
    iban: str
    kontorolle: str


@dataclass(frozen=True)
class AnbieterLieferung:
    anbieter: str
    zugang_ref: str
    konto_ref: str
    iban: str
    format: str  # nur "CAMT053" wird angenommen
    inhalt: bytes


class BankAnbieterAdapter(Protocol):
    """Nur lesend. Erhält ausschließlich das aus der Bindung abgeleitete Tupel."""

    def kontoinfo(self, *, anbieter: str, zugang_ref: str, konto_ref: str) -> AnbieterKontoInfo: ...

    def umsaetze_abrufen(
        self, *, anbieter: str, zugang_ref: str, konto_ref: str, von: date, bis: date
    ) -> AnbieterLieferung: ...


def _tupel(wert: AnbieterKontoInfo | AnbieterLieferung | BankQuellenKontext) -> tuple[str, str, str]:
    return (wert.anbieter, wert.zugang_ref, wert.konto_ref)


class GeschuetzterBankabruf:
    def __init__(self, bank_service: BankImportService, bank_repository: BankRepository):
        self._bank_service = bank_service
        self._bank_repository = bank_repository

    def abrufen(
        self, *, ctx: AuthContext, objekt_id: str, adapter: BankAnbieterAdapter, von: date, bis: date
    ) -> list[BankTransaktionTable]:
        require_schreibrecht(ctx)
        if von > bis:
            raise ValueError("Abrufzeitraum: 'von' liegt nach 'bis'.")
        kontext = self._bank_service.quellenbindung.kontext_fuer_objekt(ctx=ctx, objekt_id=objekt_id)
        anfrage = dict(anbieter=kontext.anbieter, zugang_ref=kontext.zugang_ref, konto_ref=kontext.konto_ref)

        info = adapter.kontoinfo(**anfrage)
        if _tupel(info) != _tupel(kontext):
            raise AnbieterAntwortError(f"Anbieter meldet ein anderes Konto {_tupel(info)} als gebunden {_tupel(kontext)}.")
        if _normalisiere_iban(info.iban) != kontext.iban_norm:
            raise AnbieterAntwortError("Anbieter meldet eine andere IBAN als die gebundene Quelle.")
        if info.kontorolle != KONTOROLLE_MIETE or info.kontorolle != kontext.kontorolle:
            raise AnbieterAntwortError(f"Anbieter meldet Kontorolle {info.kontorolle}, gebunden ist nur MIETE.")

        # Die Kontoinfo kann lange dauern. Einen inzwischen bestätigten
        # Widerruf/Kontowechsel bereits VOR dem Umsatzabruf beachten.
        aktuell = self._bank_service.quellenbindung.kontext_fuer_objekt(ctx=ctx, objekt_id=objekt_id)
        if aktuell != kontext:
            raise BankquellenBindungError("Bankquellenbindung während der Kontoabfrage geändert; kein Umsatzabruf.")

        lieferung = adapter.umsaetze_abrufen(**anfrage, von=von, bis=bis)
        if _tupel(lieferung) != _tupel(kontext) or _normalisiere_iban(lieferung.iban) != kontext.iban_norm:
            raise AnbieterAntwortError("Lieferung trägt eine andere Kontoidentität als die gebundene Quelle.")
        if lieferung.format != "CAMT053":
            raise AnbieterAntwortError(f"Lieferformat {lieferung.format} wird nicht angenommen (nur CAMT053).")

        bank_konto = self._bank_repository.get_bank_konto(kontext.bank_konto_id)
        if bank_konto is None:
            raise BankquellenBindungError(f"Bankkonto {kontext.bank_konto_id} fehlt.")
        return self._bank_service.importiere_camt053(
            ctx=ctx, bank_konto=bank_konto, xml_bytes=lieferung.inhalt, quellen_kontext=kontext
        )
