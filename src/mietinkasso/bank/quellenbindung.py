"""Bindung Bankquelle -> Bankkonto -> Mietobjekt (Auftrag
HV-20261005-BANKQUELLENBINDUNG). Details und Grenzen:
docs/hausverwaltung/BANKQUELLENBINDUNG.md.

Drei Zustände je Objekt, ausschließlich aus persistierten Zeilen:

- NIE KONFIGURIERT (keine Bindungsrevision): bisheriges Verhalten bleibt
  unverändert (Legacy, manueller Import/manuelle Zuordnung).
- AKTIV: Importe auf das Bankkonto der Quelle brauchen einen gültigen
  `BankQuellenKontext`; Zahlungen von Mietern des Objekts dürfen nur von
  genau diesem Bankkonto zugeordnet werden.
- WIDERRUFEN (Grabstein): fail-closed - weder Import mit Kontext noch
  Zuordnung an Mieter des Objekts, und das Bankkonto bleibt für
  ungebundene Importe gesperrt.

Jede Nutzung prüft Quelle, Bankkonto, Objekt, Gesellschaft, IBAN, Rolle
und den gespeicherten Fingerprint erneut gegen die DB - innerhalb der
Schreibtransaktion des jeweiligen Aufrufers (`session`), nie über eine
zweite Session. Es wird kein Konto aus Namen, Gesellschaft oder
"erstem Treffer" abgeleitet."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.auth.service import AuthContext, require_gesellschaft_access, require_schreibrecht
from mietinkasso.bank.importer import _normalisiere_iban
from mietinkasso.bank.quellen_models import (
    BINDUNG_AKTIV,
    BINDUNG_WIDERRUFEN,
    KONTOROLLE_MIETE,
    KONTOROLLEN,
    BankQuelleTable,
    BankQuellenBindungTable,
)
from mietinkasso.domain.exceptions import (
    BindungInkonsistentError,
    CrossTenantError,
    MietinkassoError,
    OptimistischerLockKonfliktError,
)
from mietinkasso.infrastructure.db.sqlite_write_lock import schreibgesperrte_session
from mietinkasso.infrastructure.db.tables import (
    AuditEventTable,
    BankKontoTable,
    EinheitTable,
    KontoTable,
    ObjektTable,
    VertragTable,
)

#: Erwarteter Stand für ein Objekt ohne jede Bindungsrevision.
STAND_NEU = "NEU"

#: Nicht geheime Kennungen: kein Leerraum, keine Sonderzeichen außer
#: `._:/-`. Ein Passwort/Schlüssel gehört nie in diese Felder - die Prüfung
#: kann das nicht erkennen, verhindert aber Freitext/mehrzeilige Inhalte.
_REFERENZ = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*")
#: Nur Format (Länderkennung, Prüfziffernstellen, alphanumerisch), keine
#: Prüfsummenrechnung - maßgeblich ist der exakte Vergleich mit dem
#: persistierten Bankkonto.
_IBAN_FORMAT = re.compile(r"[A-Z]{2}[0-9]{2}[A-Z0-9]{1,30}")


class QuellenbindungUngueltigError(MietinkassoError):
    """Eingabe/Scope einer Konfiguration ist ungültig - nichts geschrieben."""


class QuellenbindungKonfliktError(MietinkassoError):
    """Die Konfiguration würde eine bestehende Zuordnung still überschreiben
    (z. B. Anbieter-Tupel gehört bereits zu einem anderen Bankkonto) -
    nichts geschrieben."""


class BankquellenBindungError(BindungInkonsistentError):
    """Import/Zuordnung/Abruf passt nicht (mehr) zur persistierten
    Bankquellenbindung (fehlend, widerrufen, neu gebunden, ungültig oder
    fremdes Bankkonto). Es wird nichts geschrieben."""


@dataclass(frozen=True)
class BankQuellenKontext:
    """Eingefrorener, objektbezogener Bindungsstand - ausschließlich aus der
    DB abgeleitet (`lade_kontext`). Gleichheit vergleicht ALLE Felder; jede
    Nutzung vergleicht gegen einen frisch geladenen Kontext."""

    objekt_id: str
    revision: int
    fingerprint: str
    quelle_id: int
    anbieter: str
    zugang_ref: str
    konto_ref: str
    bank_konto_id: str
    gesellschaft_id: str
    iban_norm: str
    kontorolle: str

    @property
    def token(self) -> str:
        return f"R{self.revision}-{self.fingerprint}"


@dataclass(frozen=True)
class BindungsRevision:
    objekt_id: str
    revision: int
    status: str
    token: str


@dataclass(frozen=True)
class ObjektQuellenStatus:
    """Anzeige je Objekt für `/backoffice/bank/quellen` - IBAN nur maskiert."""

    objekt_id: str
    objekt_bezeichnung: str
    gesellschaft_id: str
    status: str  # NICHT_KONFIGURIERT | AKTIV | WIDERRUFEN | UNGUELTIG
    token: str
    revision: int | None = None
    anbieter: str | None = None
    zugang_ref: str | None = None
    konto_ref: str | None = None
    kontorolle: str | None = None
    bank_konto_id: str | None = None
    iban_maskiert: str | None = None
    nachweis_ref: str | None = None
    akteur: str | None = None
    zeitpunkt: datetime | None = None
    hinweis: str | None = None


def maskiere_iban(iban: str | None) -> str:
    norm = _normalisiere_iban(iban)
    if len(norm) <= 8:
        return "****"
    return f"{norm[:4]} **** {norm[-4:]}"


def stand_token(bindung: BankQuellenBindungTable | None) -> str:
    return STAND_NEU if bindung is None else f"R{bindung.revision}-{bindung.fingerprint}"


def _jetzt_utc_naiv() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _pflicht(wert: object, feld: str, max_laenge: int) -> str:
    if not isinstance(wert, str) or not wert.strip():
        raise QuellenbindungUngueltigError(f"Pflichtfeld '{feld}' fehlt.")
    text = wert.strip()
    if len(text) > max_laenge:
        raise QuellenbindungUngueltigError(f"Feld '{feld}' ist länger als {max_laenge} Zeichen.")
    return text


def _referenz(wert: object, feld: str, max_laenge: int) -> str:
    text = _pflicht(wert, feld, max_laenge)
    if not _REFERENZ.fullmatch(text):
        raise QuellenbindungUngueltigError(
            f"Feld '{feld}' darf nur eine nicht geheime Kennung (Buchstaben, Ziffern, ._:/-) enthalten - "
            "keine Passwörter, Schlüssel oder Tokens."
        )
    return text


def _fingerprint(
    *, objekt_id: str, revision: int, status: str, vorgaenger_id: int | None, nachweis_ref: str,
    quelle: BankQuelleTable,
) -> str:
    werte = {
        "objekt_id": objekt_id, "revision": revision, "status": status, "vorgaenger_id": vorgaenger_id,
        "nachweis_ref": nachweis_ref, "quelle_id": quelle.id, "anbieter": quelle.anbieter,
        "zugang_ref": quelle.zugang_ref, "konto_ref": quelle.konto_ref, "bank_konto_id": quelle.bank_konto_id,
        "gesellschaft_id": quelle.gesellschaft_id, "iban_norm": quelle.iban_norm, "kontorolle": quelle.kontorolle,
    }
    return hashlib.sha256(json.dumps(werte, sort_keys=True).encode("utf-8")).hexdigest()


# -- Prüfungen innerhalb der Session des Aufrufers ------------------------------


def aktuelle_bindung(session: Session, objekt_id: str) -> BankQuellenBindungTable | None:
    return session.execute(
        select(BankQuellenBindungTable)
        .where(BankQuellenBindungTable.objekt_id == objekt_id)
        .order_by(BankQuellenBindungTable.revision.desc())
        .limit(1)
    ).scalars().first()


def _objekt_ausgeschlossen(objekt: ObjektTable) -> bool:
    # Der feste Projektausschluss 107 gilt auch bei einem fehlerhaften
    # Stammdatenkennzeichen, wie im getrennten EBICS-Downloader.
    return objekt.id == "107" or objekt.ausgeschlossen


def bank_ist_quellengebunden(session: Session, bank_konto_id: str) -> bool:
    """True, sobald IRGENDEINE Bankquelle auf dieses Bankkonto zeigt - auch
    wenn alle Objektbindungen inzwischen widerrufen/umgebunden sind. Quellen
    entstehen nur zusammen mit einer Objektbindung und werden nie gelöscht;
    ein solches Bankkonto kehrt also nie zum ungebundenen Import zurück."""

    return session.execute(
        select(BankQuelleTable.id).where(BankQuelleTable.bank_konto_id == bank_konto_id).limit(1)
    ).first() is not None


def _validiere_aktive_bindung(
    session: Session, bindung: BankQuellenBindungTable, objekt: ObjektTable
) -> BankQuelleTable:
    if bindung.status != BINDUNG_AKTIV:
        raise BankquellenBindungError(
            f"Bankquellenbindung von Objekt {objekt.id} ist widerrufen (Revision {bindung.revision}) - "
            "gesperrt, kein Rückfall auf ein anderes Bankkonto."
        )
    quelle = session.get(BankQuelleTable, bindung.quelle_id)
    if quelle is None:
        raise BankquellenBindungError(f"Bankquelle {bindung.quelle_id} von Objekt {objekt.id} fehlt.")
    erwartet = _fingerprint(
        objekt_id=bindung.objekt_id, revision=bindung.revision, status=bindung.status,
        vorgaenger_id=bindung.vorgaenger_id, nachweis_ref=bindung.nachweis_ref, quelle=quelle,
    )
    if bindung.objekt_id != objekt.id or bindung.fingerprint != erwartet:
        raise BankquellenBindungError(
            f"Bankquellenbindung von Objekt {objekt.id} ist in sich inkonsistent (Fingerprint) - gesperrt."
        )
    if quelle.kontorolle != KONTOROLLE_MIETE:
        raise BankquellenBindungError(f"Bankquelle {quelle.id} hat die Rolle {quelle.kontorolle}, nicht MIETE.")
    if _objekt_ausgeschlossen(objekt):
        raise BankquellenBindungError(f"Objekt {objekt.id} ist ausgeschlossen.")
    if quelle.gesellschaft_id != objekt.gesellschaft_id:
        raise BankquellenBindungError(
            f"Objekt {objekt.id} gehört zu Gesellschaft {objekt.gesellschaft_id}, die Bankquelle zu "
            f"{quelle.gesellschaft_id} - gesperrt."
        )
    bank = session.get(BankKontoTable, quelle.bank_konto_id)
    if bank is None or bank.gesellschaft_id != quelle.gesellschaft_id or _normalisiere_iban(bank.iban) != quelle.iban_norm:
        raise BankquellenBindungError(
            f"Bankkonto {quelle.bank_konto_id} entspricht nicht mehr der gebundenen Quelle (Gesellschaft/IBAN "
            "geändert oder Konto fehlt) - gesperrt."
        )
    return quelle


def _kontext_aus(bindung: BankQuellenBindungTable, quelle: BankQuelleTable) -> BankQuellenKontext:
    return BankQuellenKontext(
        objekt_id=bindung.objekt_id, revision=bindung.revision, fingerprint=bindung.fingerprint,
        quelle_id=quelle.id, anbieter=quelle.anbieter, zugang_ref=quelle.zugang_ref, konto_ref=quelle.konto_ref,
        bank_konto_id=quelle.bank_konto_id, gesellschaft_id=quelle.gesellschaft_id, iban_norm=quelle.iban_norm,
        kontorolle=quelle.kontorolle,
    )


def lade_kontext(session: Session, ctx: AuthContext, objekt_id: str) -> BankQuellenKontext:
    objekt = session.get(ObjektTable, objekt_id)
    if objekt is None or _objekt_ausgeschlossen(objekt):
        raise BankquellenBindungError(f"Objekt {objekt_id} ist unbekannt oder ausgeschlossen.")
    require_gesellschaft_access(ctx, objekt.gesellschaft_id)
    bindung = aktuelle_bindung(session, objekt_id)
    if bindung is None:
        raise BankquellenBindungError(
            f"Objekt {objekt_id} hat keine konfigurierte Bankquelle - es wird kein Ersatzkonto verwendet."
        )
    return _kontext_aus(bindung, _validiere_aktive_bindung(session, bindung, objekt))


def pruefe_kontext(session: Session, ctx: AuthContext, kontext: BankQuellenKontext) -> BankQuellenKontext:
    if not isinstance(kontext, BankQuellenKontext):
        raise BankquellenBindungError("Kein gültiger Bankquellen-Kontext übergeben.")
    aktuell = lade_kontext(session, ctx, kontext.objekt_id)
    if aktuell != kontext:
        raise BankquellenBindungError(
            f"Bankquellen-Kontext für Objekt {kontext.objekt_id} ist veraltet oder abweichend (Bindung wurde "
            f"geändert, widerrufen oder neu gebunden; aktuell Revision {aktuell.revision}) - abgelehnt."
        )
    return aktuell


def pruefe_importbindung(
    session: Session, ctx: AuthContext, *, bank_konto_id: str, iban_norm: str, kontext: BankQuellenKontext | None
) -> None:
    """Vor dem Parsen UND erneut in der Schreibtransaktion des Imports."""

    if kontext is None:
        if bank_ist_quellengebunden(session, bank_konto_id):
            raise BankquellenBindungError(
                f"Bankkonto {bank_konto_id} ist an eine Objekt-Bankquelle gebunden (auch widerrufene Bindungen "
                "zählen) - Import nur mit gültigem Bankquellen-Kontext, ungebundener Import abgelehnt."
            )
        return
    gueltig = pruefe_kontext(session, ctx, kontext)
    if gueltig.bank_konto_id != bank_konto_id or gueltig.iban_norm != iban_norm:
        raise BankquellenBindungError(
            f"Bankquellen-Kontext von Objekt {gueltig.objekt_id} gehört zu Bankkonto {gueltig.bank_konto_id}, "
            f"nicht zu {bank_konto_id} - abgelehnt."
        )


def pruefe_zahlungsbindung(session: Session, *, konto_id: str, bank_konto_id: str) -> None:
    """Objekt aus persistiertem Konto -> Vertrag -> Einheit. Nie konfiguriert
    -> unverändertes Verhalten; sonst muss die aktive Bindung gültig sein und
    genau auf `bank_konto_id` zeigen (kein Rückfall auf "gleiche
    Gesellschaft")."""

    konto = session.get(KontoTable, konto_id)
    vertrag = session.get(VertragTable, konto.vertrag_id) if konto is not None else None
    einheit = session.get(EinheitTable, vertrag.einheit_id) if vertrag is not None else None
    objekt = session.get(ObjektTable, einheit.objekt_id) if einheit is not None else None
    if objekt is None:
        # Dieselbe Fehlerart wie die bestehende Konto->Objekt-Auflösung
        # (`StammdatenRepository.objekt_fuer_vertrag`).
        raise ValueError(f"Objekt zu Konto {konto_id} ist nicht auflösbar - abgelehnt.")
    bindung = aktuelle_bindung(session, objekt.id)
    if bindung is None:
        return
    quelle = _validiere_aktive_bindung(session, bindung, objekt)
    if quelle.bank_konto_id != bank_konto_id:
        raise BankquellenBindungError(
            f"Konto {konto_id} gehört zu Objekt {objekt.id}, dessen Bankquelle auf Bankkonto "
            f"{quelle.bank_konto_id} zeigt - eine Zahlung von Bankkonto {bank_konto_id} wird nicht zugeordnet."
        )


# -- Konfiguration --------------------------------------------------------------


class QuellenbindungService:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    # -- Lesen ---------------------------------------------------------------
    def kontext_fuer_objekt(self, *, ctx: AuthContext, objekt_id: str) -> BankQuellenKontext:
        with self._session_factory() as session:
            return lade_kontext(session, ctx, objekt_id)

    def bank_ist_quellengebunden(self, bank_konto_id: str) -> bool:
        with self._session_factory() as session:
            return bank_ist_quellengebunden(session, bank_konto_id)

    def uebersicht(self, *, ctx: AuthContext) -> list[ObjektQuellenStatus]:
        """Alle zugänglichen, nicht ausgeschlossenen Objekte. Reiner Read."""

        ergebnis: list[ObjektQuellenStatus] = []
        with self._session_factory() as session:
            for objekt in session.execute(select(ObjektTable).order_by(ObjektTable.id)).scalars().all():
                if _objekt_ausgeschlossen(objekt) or not ctx.has_zugriff(objekt.gesellschaft_id):
                    continue
                basis = dict(
                    objekt_id=objekt.id, objekt_bezeichnung=objekt.bezeichnung, gesellschaft_id=objekt.gesellschaft_id,
                )
                bindung = aktuelle_bindung(session, objekt.id)
                if bindung is None:
                    ergebnis.append(ObjektQuellenStatus(**basis, status="NICHT_KONFIGURIERT", token=STAND_NEU))
                    continue
                quelle = session.get(BankQuelleTable, bindung.quelle_id)
                status, hinweis = bindung.status, None
                if bindung.status == BINDUNG_AKTIV:
                    try:
                        _validiere_aktive_bindung(session, bindung, objekt)
                    except BankquellenBindungError as exc:
                        status, hinweis = "UNGUELTIG", str(exc)
                ergebnis.append(ObjektQuellenStatus(
                    **basis, status=status, token=stand_token(bindung), revision=bindung.revision,
                    anbieter=quelle.anbieter if quelle else None, zugang_ref=quelle.zugang_ref if quelle else None,
                    konto_ref=quelle.konto_ref if quelle else None, kontorolle=quelle.kontorolle if quelle else None,
                    bank_konto_id=quelle.bank_konto_id if quelle else None,
                    iban_maskiert=maskiere_iban(quelle.iban_norm) if quelle else None,
                    nachweis_ref=bindung.nachweis_ref, akteur=bindung.akteur, zeitpunkt=bindung.zeitpunkt, hinweis=hinweis,
                ))
        return ergebnis

    def aktive_kontexte(self, *, ctx: AuthContext) -> list[BankQuellenKontext]:
        aktive = [status.objekt_id for status in self.uebersicht(ctx=ctx) if status.status == BINDUNG_AKTIV]
        with self._session_factory() as session:
            return [lade_kontext(session, ctx, objekt_id) for objekt_id in aktive]

    # -- Schreiben -------------------------------------------------------------
    def binden(
        self,
        *,
        ctx: AuthContext,
        objekt_id: str,
        anbieter: str,
        zugang_ref: str,
        konto_ref: str,
        bank_konto_id: str,
        gesellschaft_id: str,
        iban: str,
        kontorolle: str,
        nachweis_ref: str,
        erwarteter_stand: str,
    ) -> BankQuellenKontext:
        """Legt Quelle (falls neu) und Objektbindung in EINER Transaktion samt
        Audit an. Ein exakter Retry ist ein No-Op; jede Änderung einer
        bestehenden Bindung verlangt den aktuellen `erwarteter_stand`
        (`stand_token`), sonst `OptimistischerLockKonfliktError`."""

        require_schreibrecht(ctx)
        objekt_id = _pflicht(objekt_id, "objekt_id", 32)
        anbieter = _referenz(anbieter, "anbieter", 64)
        zugang_ref = _referenz(zugang_ref, "zugang_ref", 128)
        konto_ref = _referenz(konto_ref, "konto_ref", 128)
        bank_konto_id = _pflicht(bank_konto_id, "bank_konto_id", 48)
        gesellschaft_id = _pflicht(gesellschaft_id, "gesellschaft_id", 32)
        iban_norm = _normalisiere_iban(_pflicht(iban, "iban", 64))
        if not _IBAN_FORMAT.fullmatch(iban_norm):
            raise QuellenbindungUngueltigError("IBAN hat kein gültiges Format.")
        rolle = _pflicht(kontorolle, "kontorolle", 16).upper()
        if rolle not in KONTOROLLEN:
            raise QuellenbindungUngueltigError(f"Unbekannte Kontorolle '{rolle}'.")
        if rolle != KONTOROLLE_MIETE:
            raise QuellenbindungUngueltigError(
                f"Kontorolle {rolle} darf nicht an ein Mietobjekt gebunden werden - nur MIETE."
            )
        nachweis_ref = _pflicht(nachweis_ref, "nachweis_ref", 256)
        erwarteter_stand = _pflicht(erwarteter_stand, "erwarteter_stand", 128)

        with schreibgesperrte_session(self._session_factory) as session:
            try:
                objekt = self._objekt(session, ctx, objekt_id, erlaube_ausgeschlossen=False)
                if objekt.gesellschaft_id != gesellschaft_id:
                    raise CrossTenantError(
                        f"Objekt {objekt_id} gehört zu Gesellschaft {objekt.gesellschaft_id}, nicht {gesellschaft_id}."
                    )
                bank = session.get(BankKontoTable, bank_konto_id, with_for_update=True)
                if bank is None:
                    raise QuellenbindungUngueltigError(f"Unbekanntes Bankkonto {bank_konto_id}.")
                require_gesellschaft_access(ctx, bank.gesellschaft_id)
                if bank.gesellschaft_id != gesellschaft_id:
                    raise CrossTenantError(
                        f"Bankkonto {bank_konto_id} gehört zu Gesellschaft {bank.gesellschaft_id}, nicht {gesellschaft_id}."
                    )
                if _normalisiere_iban(bank.iban) != iban_norm:
                    raise QuellenbindungUngueltigError(
                        f"IBAN entspricht nicht der gespeicherten IBAN von Bankkonto {bank_konto_id}."
                    )

                quelle = session.execute(
                    select(BankQuelleTable)
                    .where(BankQuelleTable.anbieter == anbieter)
                    .where(BankQuelleTable.zugang_ref == zugang_ref)
                    .where(BankQuelleTable.konto_ref == konto_ref)
                ).scalar_one_or_none()
                if quelle is None:
                    quelle = BankQuelleTable(
                        anbieter=anbieter, zugang_ref=zugang_ref, konto_ref=konto_ref, bank_konto_id=bank_konto_id,
                        gesellschaft_id=gesellschaft_id, iban_norm=iban_norm, kontorolle=rolle,
                        erstellt_von=ctx.user_id, erstellt_am=_jetzt_utc_naiv(),
                    )
                    session.add(quelle)
                    session.flush()
                elif (quelle.bank_konto_id, quelle.gesellschaft_id, quelle.iban_norm, quelle.kontorolle) != (
                    bank_konto_id, gesellschaft_id, iban_norm, rolle,
                ):
                    raise QuellenbindungKonfliktError(
                        f"Anbieter-Tupel ({anbieter}, {zugang_ref}, {konto_ref}) ist bereits Bankkonto "
                        f"{quelle.bank_konto_id} ({quelle.gesellschaft_id}, Rolle {quelle.kontorolle}) zugeordnet - "
                        "keine Umdeutung auf ein anderes Konto."
                    )

                aktuell = aktuelle_bindung(session, objekt_id)
                if aktuell is not None and aktuell.status == BINDUNG_AKTIV and aktuell.quelle_id == quelle.id:
                    if aktuell.nachweis_ref != nachweis_ref:
                        raise QuellenbindungKonfliktError(
                            f"Objekt {objekt_id} ist bereits an diese Bankquelle gebunden (anderer Nachweis) - "
                            "keine Änderung."
                        )
                    ergebnis = _kontext_aus(aktuell, _validiere_aktive_bindung(session, aktuell, objekt))
                    session.commit()
                    return ergebnis
                if erwarteter_stand != stand_token(aktuell):
                    raise OptimistischerLockKonfliktError(
                        f"Bankquellenbindung von Objekt {objekt_id} hat sich geändert (erwartet {erwarteter_stand}, "
                        f"aktuell {stand_token(aktuell)}) - nichts überschrieben."
                    )
                neu = self._neue_revision(session, ctx, objekt_id, aktuell, quelle, BINDUNG_AKTIV, nachweis_ref)
                ergebnis = _kontext_aus(neu, _validiere_aktive_bindung(session, neu, objekt))
                session.commit()
                return ergebnis
            except IntegrityError as exc:
                session.rollback()
                raise QuellenbindungKonfliktError(
                    f"Gleichzeitige Änderung der Bankquellenbindung von Objekt {objekt_id} - nichts geschrieben."
                ) from exc
            except Exception:
                session.rollback()
                raise

    def widerrufen(
        self, *, ctx: AuthContext, objekt_id: str, nachweis_ref: str, erwarteter_stand: str
    ) -> BindungsRevision:
        """Schreibt einen Grabstein (neue Revision WIDERRUFEN) - die bisherigen
        Revisionen bleiben unverändert. Das Objekt bleibt danach gesperrt
        (fail-closed), bis es ausdrücklich neu gebunden wird."""

        require_schreibrecht(ctx)
        objekt_id = _pflicht(objekt_id, "objekt_id", 32)
        nachweis_ref = _pflicht(nachweis_ref, "nachweis_ref", 256)
        erwarteter_stand = _pflicht(erwarteter_stand, "erwarteter_stand", 128)

        with schreibgesperrte_session(self._session_factory) as session:
            try:
                # Widerruf einer falschen Bindung bleibt auch nach späterem
                # Ausschluss des Objekts möglich (macht nichts freier).
                self._objekt(session, ctx, objekt_id, erlaube_ausgeschlossen=True)
                aktuell = aktuelle_bindung(session, objekt_id)
                if aktuell is None:
                    raise QuellenbindungUngueltigError(f"Objekt {objekt_id} hat keine Bankquellenbindung.")
                if aktuell.status == BINDUNG_WIDERRUFEN:
                    vorgaenger = session.get(BankQuellenBindungTable, aktuell.vorgaenger_id) if aktuell.vorgaenger_id else None
                    if erwarteter_stand == stand_token(vorgaenger) and aktuell.nachweis_ref == nachweis_ref:
                        session.commit()
                        return BindungsRevision(objekt_id, aktuell.revision, aktuell.status, stand_token(aktuell))
                    raise OptimistischerLockKonfliktError(
                        f"Bankquellenbindung von Objekt {objekt_id} ist bereits widerrufen (Revision {aktuell.revision})."
                    )
                if erwarteter_stand != stand_token(aktuell):
                    raise OptimistischerLockKonfliktError(
                        f"Bankquellenbindung von Objekt {objekt_id} hat sich geändert (erwartet {erwarteter_stand}, "
                        f"aktuell {stand_token(aktuell)}) - nichts widerrufen."
                    )
                quelle = session.get(BankQuelleTable, aktuell.quelle_id)
                neu = self._neue_revision(session, ctx, objekt_id, aktuell, quelle, BINDUNG_WIDERRUFEN, nachweis_ref)
                ergebnis = BindungsRevision(objekt_id, neu.revision, neu.status, stand_token(neu))
                session.commit()
                return ergebnis
            except IntegrityError as exc:
                session.rollback()
                raise QuellenbindungKonfliktError(
                    f"Gleichzeitige Änderung der Bankquellenbindung von Objekt {objekt_id} - nichts geschrieben."
                ) from exc
            except Exception:
                session.rollback()
                raise

    @staticmethod
    def _objekt(session: Session, ctx: AuthContext, objekt_id: str, *, erlaube_ausgeschlossen: bool) -> ObjektTable:
        objekt = session.get(ObjektTable, objekt_id)
        if objekt is None or (_objekt_ausgeschlossen(objekt) and not erlaube_ausgeschlossen):
            raise QuellenbindungUngueltigError(f"Objekt {objekt_id} ist unbekannt oder ausgeschlossen.")
        require_gesellschaft_access(ctx, objekt.gesellschaft_id)
        return objekt

    @staticmethod
    def _neue_revision(
        session: Session,
        ctx: AuthContext,
        objekt_id: str,
        aktuell: BankQuellenBindungTable | None,
        quelle: BankQuelleTable,
        status: str,
        nachweis_ref: str,
    ) -> BankQuellenBindungTable:
        revision = (aktuell.revision if aktuell is not None else 0) + 1
        vorgaenger_id = aktuell.id if aktuell is not None else None
        row = BankQuellenBindungTable(
            objekt_id=objekt_id, revision=revision, status=status, quelle_id=quelle.id, vorgaenger_id=vorgaenger_id,
            nachweis_ref=nachweis_ref, akteur=ctx.user_id, zeitpunkt=_jetzt_utc_naiv(),
            fingerprint=_fingerprint(
                objekt_id=objekt_id, revision=revision, status=status, vorgaenger_id=vorgaenger_id,
                nachweis_ref=nachweis_ref, quelle=quelle,
            ),
        )
        session.add(row)
        session.flush()
        # Audit in DERSELBEN Transaktion - rollt mit zurück.
        session.add(AuditEventTable(
            entity_typ="bank_quellen_bindung", entity_id=objekt_id,
            aktion="gebunden" if status == BINDUNG_AKTIV else "widerrufen", akteur=ctx.user_id,
            payload={
                "revision": revision, "vorgaenger_revision": aktuell.revision if aktuell is not None else None,
                "quelle_id": quelle.id, "anbieter": quelle.anbieter, "zugang_ref": quelle.zugang_ref,
                "konto_ref": quelle.konto_ref, "bank_konto_id": quelle.bank_konto_id,
                "iban_maskiert": maskiere_iban(quelle.iban_norm), "kontorolle": quelle.kontorolle,
                "nachweis_ref": nachweis_ref,
            },
        ))
        session.flush()
        return row
