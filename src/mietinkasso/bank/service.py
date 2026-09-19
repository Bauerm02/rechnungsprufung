"""Bankabgleich: Import, automatische/manuelle Zuordnung, Rücklastschrift,
Bankvollständigkeit. Fachregel 4: Zahlung und Zuordnung sind getrennte
Schritte; automatisch wird nur bei eindeutiger Referenz UND eindeutigem
Konto zugeordnet, niemals allein über Namensgleichheit oder gleichen
Betrag.

OP-Buchung, Zuordnung und Audit-Eintrag laufen für jede Zuordnung in
EINER gemeinsamen DB-Transaktion: schlägt irgendein Schritt fehl (auch
ein unerwarteter Fehler mitten in der Zuordnungserstellung), wird die
GESAMTE Transaktion zurückgerollt - es bleibt nie eine gebuchte Zahlung
ohne zugehörige Zuordnung übrig. Datei-Importe (CAMT.053/CSV) sind
ebenfalls atomar je Aufruf: schlägt eine Zeile fehl, wird der gesamte
Importlauf zurückgerollt statt unsichtbare Teilergebnisse zu
hinterlassen; ein erneuter, korrigierter Lauf ist dank Idempotenz immer
gefahrlos möglich.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from mietinkasso.auth.service import AuthContext, require_gesellschaft_access, require_schreibrecht
from mietinkasso.bank.importer import CsvSpaltenMapping, RohTransaktion, parse_camt053, parse_csv
from mietinkasso.bank.repository import BankRepository
from mietinkasso.domain.enums import OPTyp, ZahlungsMatchTyp
from mietinkasso.domain.exceptions import (
    BindungInkonsistentError,
    CrossTenantError,
    FremdwaehrungNichtUnterstuetztError,
    LeistungsperiodeMehrdeutigError,
    MehrfachbuchungsKonfliktError,
    ZuordnungUngueltigError,
)
from mietinkasso.infrastructure.db.sqlite_write_lock import schreibgesperrte_session
from mietinkasso.infrastructure.db.tables import (
    BankKontoTable,
    BankTransaktionTable,
    KontoTable,
    OPPositionTable,
    ZuordnungTable,
)
from mietinkasso.op.service import OPService
from mietinkasso.stammdaten.repository import StammdatenRepository

_VERTRAG_REFERENZ = re.compile(r"VERTRAG:([A-Za-z0-9\-]+)")

# -- Auftrag HV-20260919-GEORGE-CODE: deterministische Mietmonat-Erkennung ------
#
# Fachregel (wie `_VERTRAG_REFERENZ`/Fachregel 4): NIE eine Jahresvermutung
# ohne explizite 4-stellige Jahresangabe im Text, NIE eine automatische
# FIFO-Zuordnung bei mehrdeutigem/mehrmonatigem Zweck - ein solcher Zweck
# bleibt bewusst UNGEBUNDEN (die Referenz selbst bleibt am Beleg sichtbar
# und damit weiterhin manuell prüfbar), statt geraten zu werden.
_MONATSNAMEN = {
    "jänner": 1, "janner": 1, "januar": 1, "februar": 2, "märz": 3, "maerz": 3,
    "april": 4, "mai": 5, "juni": 6, "juli": 7, "august": 8, "september": 9,
    "oktober": 10, "november": 11, "dezember": 12,
}
# Codex-Rückprüfung b8d700d: Bankexporte trennen Zahl/Trenner/Jahr nicht
# immer eng ("09/ 2026", "09 / 2026") - der Trenner (Punkt/Schrägstrich)
# toleriert deshalb beidseitig Whitespace, ohne die Ziffernfolge selbst
# aufzuweichen (kein zusätzliches Ratepotenzial).
_MONAT_JAHR_NUMERISCH = re.compile(r"\b(0[1-9]|1[0-2])\s*[./]\s*(\d{4})\b")
_MONAT_NAME_JAHR = re.compile(
    r"\b(" + "|".join(_MONATSNAMEN) + r")\b\s*\D{0,3}(\d{4})\b", re.IGNORECASE
)
# ISO-Monat "YYYY-MM" (z. B. "Miete 2026-09") - NIE ein volles Tagesdatum
# "YYYY-MM-DD" als Mietmonat missverstehen (negative Lookahead auf ein
# unmittelbar folgendes "-DD").
_MONAT_JAHR_ISO = re.compile(r"\b(\d{4})-(0[1-9]|1[0-2])\b(?!\s*-\s*\d{2})")
# JEDE Erwähnung eines Monatsnamens (auch OHNE Jahr) - allein das Vorkommen
# zählt schon als "Monat gemeint, aber ggf. nicht eindeutig auflösbar".
_MONATSNAME_MUSTER = re.compile(r"\b(" + "|".join(_MONATSNAMEN) + r")\b", re.IGNORECASE)


def erkenne_leistungsperiode(referenz: str | None) -> str | None:
    """Erkennt EINEN eindeutigen, explizit genannten Mietmonat aus dem
    Bank-Verwendungszweck - unterstützte Formate: "MM.YYYY"/"MM/YYYY"
    (auch mit Whitespace um den Trenner, z. B. "09/ 2026"), ISO "YYYY-MM"
    (nie ein volles Tagesdatum "YYYY-MM-DD"), oder ein voller deutscher
    Monatsname gefolgt von einer 4-stelligen Jahreszahl (z. B.
    "September 2026"). Liefert `None`, wenn der Text GAR KEIN
    Monats-Signal enthält (der normale, unauffällige Fall - die
    allermeisten Bankbewegungen erwähnen keinen Mietmonat).

    Codex-Rückprüfung b8d700d: KEINE Angabe (kein Monats-Signal
    überhaupt) ist NICHT dasselbe wie MEHRDEUTIG (ein Monat wird zwar
    erwähnt, aber nicht sicher EINER Periode zuordenbar) - beides gab die
    Vorversion fälschlich als identisches `None` zurück. Ein erwähnter,
    aber nicht eindeutig auflösbarer Monat (Monatsname OHNE Jahresangabe,
    z. B. "Miete August", ODER mehr als EIN Monats-Signal im selben Text,
    z. B. "Miete August und September 2026" oder "Miete 08/2026 und
    09/2026") wird NICHT stillschweigend verworfen, sondern lehnt die
    gesamte Zuordnung über `LeistungsperiodeMehrdeutigError` ab - siehe
    `_zuordnen_atomar`/`_resolve_periode_und_forderung`. Reine
    Textanalyse, kein DB-Zugriff, keine Kontierung."""

    if not referenz:
        return None

    numerische_treffer = _MONAT_JAHR_NUMERISCH.findall(referenz)
    iso_treffer = _MONAT_JAHR_ISO.findall(referenz)
    namens_treffer = _MONAT_NAME_JAHR.findall(referenz)
    alle_monatsnamen = _MONATSNAME_MUSTER.findall(referenz)

    anzahl_monatssignale = len(numerische_treffer) + len(iso_treffer) + len(alle_monatsnamen)
    if anzahl_monatssignale == 0:
        return None  # kein Hinweis auf einen Mietmonat - unauffälliger Regelfall

    eindeutige_perioden: set[str] = set()
    for monat, jahr in numerische_treffer:
        eindeutige_perioden.add(f"{jahr}-{monat}")
    for jahr, monat in iso_treffer:
        eindeutige_perioden.add(f"{jahr}-{monat}")
    for name, jahr in namens_treffer:
        eindeutige_perioden.add(f"{jahr}-{_MONATSNAMEN[name.lower()]:02d}")

    # Eindeutig NUR, wenn GENAU EIN Monats-Signal im gesamten Text
    # vorkommt UND dieses genau eine, eindeutig auflösbare Periode ergibt
    # (ein Monatsname ganz ohne Jahr zählt als Signal, ergibt aber KEINE
    # Periode und macht den Fall damit mehrdeutig statt "keine Angabe").
    if anzahl_monatssignale == 1 and len(eindeutige_perioden) == 1:
        return eindeutige_perioden.pop()

    raise LeistungsperiodeMehrdeutigError(
        f"Bank-Verwendungszweck '{referenz}' erwähnt einen Mietmonat nicht eindeutig (Monatsname ohne "
        "Jahresangabe oder mehr als eine unterschiedliche Monats-/Jahresangabe) - wird NICHT automatisch "
        "gebucht, sondern zur manuellen Klärung zurückgehalten."
    )

# -- Auftrag HV-20260914-BANKUEBERSICHT: reine Anzeigekategorisierung -----------
#
# Fachregel 4 gilt auch hier: NIEMALS Lieferanten-/Personennamen
# hartcodieren, NIEMALS allein über Namensgleichheit oder allein über
# das Vorzeichen des Betrags entscheiden. Alle Muster sind generische,
# aus dem Banktext (Referenz/Gegenkonto-Name) ablesbare Signalwörter.
#
# Schutzsignale haben IMMER Vorrang vor einer "harmlosen" Umbuchungs-
# oder Betriebsausgaben-Einordnung - bei konkurrierenden Signalen (z. B.
# gleichzeitig "Umbuchung" UND "Miete" im Text) ist das Ergebnis IMMER
# der konservative Klärfall-Pfad.
KATEGORIE_EINGANG_PRUEFEN = "EINGANG_PRUEFEN"
KATEGORIE_RUECKLASTSCHRIFT_KLAERFALL = "RUECKLASTSCHRIFT_KLAERFALL"
KATEGORIE_UMBUCHUNG = "UMBUCHUNG"
KATEGORIE_AUSGANG_BETRIEBSAUSGABE = "AUSGANG_BETRIEBSAUSGABE"

_SCHUTZ_MUSTER = re.compile(
    r"R(Ü|UE)CKLASTSCHRIFT|RETOURE|STORNO|MIET|KAUTION|MANDAT|VERTRAG",
    re.IGNORECASE,
)
_UMBUCHUNG_MUSTER = re.compile(r"UMBUCHUNG", re.IGNORECASE)
_RECHNUNGSAUSGANG_MUSTER = re.compile(r"RG[-\s]?NR|RECHNUNG(SNUMMER)?", re.IGNORECASE)


def kategorisiere_bewegung(transaktion: BankTransaktionTable) -> tuple[str, str]:
    """Reine, seiteneffektfreie Anzeigekategorisierung für
    `/backoffice/bank/unzugeordnet` - KEINE abschließende Kontierung/
    Buchung, verändert/verschiebt/löscht keine Rohdaten. Liest
    ausschließlich `betrag_cent`/`referenz`/`gegenkonto_name` der
    übergebenen Transaktion und trifft keine DB-Zugriffe.

    Ein bloß negativer Betrag genügt NIE für eine Betriebsausgaben-
    Einordnung - ohne erkennbaren Rechnungsbezug im Banktext bleibt eine
    unklare negative Bewegung immer ein Klärfall."""

    # Codex-Rückprüfung db3755a: eine Nullbewegung braucht eine EIGENE,
    # ausdrückliche Kategorie - sie darf weder als Eingang/Mietzahlung
    # noch als "harmlose" Umbuchung durchgehen, sondern bleibt (wie eine
    # Rücklastschrift) ein Klärfall ohne normales Zahlungsformular.
    if transaktion.betrag_cent == 0:
        return (
            KATEGORIE_RUECKLASTSCHRIFT_KLAERFALL,
            "Nullbetrag ohne Zahlungsrichtung; Prüffall, keine automatische Kontierung, kein "
            "Zahlungsformular.",
        )

    text = " ".join(teil for teil in (transaktion.referenz, transaktion.gegenkonto_name) if teil)
    hat_schutzsignal = bool(_SCHUTZ_MUSTER.search(text))

    if transaktion.betrag_cent > 0:
        if not hat_schutzsignal and _UMBUCHUNG_MUSTER.search(text):
            return KATEGORIE_UMBUCHUNG, "Banktext enthält 'Umbuchung' ohne Miet-/Rücklastschriftbezug."
        return (
            KATEGORIE_EINGANG_PRUEFEN,
            "Zahlungseingang noch nicht zugeordnet; Referenz/Gegenkonto allein reichen für keine "
            "automatische Kontierung.",
        )

    if hat_schutzsignal:
        return (
            KATEGORIE_RUECKLASTSCHRIFT_KLAERFALL,
            "Banktext enthält ein Rücklastschrift-/Retoure-/Storno-/Miet-/Kautions-/Mandats-/"
            "Vertragssignal; Prüffall, keine automatische Kontierung.",
        )
    if _UMBUCHUNG_MUSTER.search(text):
        return KATEGORIE_UMBUCHUNG, "Banktext enthält 'Umbuchung' ohne Miet-/Rücklastschriftbezug."
    if _RECHNUNGSAUSGANG_MUSTER.search(text):
        return (
            KATEGORIE_AUSGANG_BETRIEBSAUSGABE,
            "Banktext verweist auf eine Rechnungsnummer ohne Miet-/Rücklastschriftbezug; vermutlich "
            "Betriebsausgabe (reiner Anzeigehinweis, keine Buchung).",
        )
    return (
        KATEGORIE_RUECKLASTSCHRIFT_KLAERFALL,
        "Negativer Betrag ohne eindeutigen Banktext-Hinweis; ein negativer Betrag allein genügt nicht "
        "für eine Betriebsausgaben-Einordnung - Prüffall.",
    )


def _hash(fields: dict) -> str:
    canonical = json.dumps(fields, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _fingerprint(bank_konto_id: str, roh: RohTransaktion) -> str:
    """Formatierungsunabhängiger, konto-gescopter Fingerabdruck der
    wirtschaftlich relevanten Felder - für Zeilen OHNE bankseitig
    eindeutige Kennung. Zeilennummern/Roh-Layout fließen bewusst NICHT
    ein, damit ein überlappender Re-Export mit anderer Zeilenreihenfolge
    dieselbe Zahlung wiedererkennbar macht."""

    return _hash(
        {
            "bank_konto_id": bank_konto_id,
            "buchungsdatum": str(roh.buchungsdatum),
            "betrag_cent": roh.betrag_cent,
            "waehrung": roh.waehrung,
            "referenz": (roh.referenz or "").strip().lower(),
            "gegenkonto_iban": (roh.gegenkonto_iban or "").strip().upper(),
        }
    )


def _legacy_fingerprint(bank_konto_id: str, roh: RohTransaktion) -> str | None:
    """Rekonstruiert den Fingerprint, den `_fingerprint` VOR dem
    CAMT-Gegenpartei-/Referenz-Fix (Codex-Rückprüfung b8d700d) für
    dieselbe Rohzeile geliefert hätte - AUSSCHLIESSLICH zur konservativen
    Erkennung eines sonst stillen Doppelimports (siehe `_speichere_roh`):
    eine Zeile ohne bankseitig eindeutige `native_id`, die VOR diesem Fix
    bereits importiert wurde, trägt in der DB noch den ALTEN Fingerprint
    (alte Gegenpartei-Ermittlung/nur erste Ustrd-Zeile) - der NEUE
    Fingerprint dieser exakt gleichen Zahlung kann davon abweichen, ohne
    dass sich an der Zahlung selbst irgendetwas geändert hätte. Liefert
    `None`, wenn der Parser keine Legacy-Felder gesetzt hat (z. B.
    CSV-Zeilen - von diesem Fix nicht betroffen) - dann entfällt der
    Zusatzvergleich ersatzlos."""

    if roh.legacy_referenz is None and roh.legacy_gegenkonto_iban is None:
        return None
    return _hash(
        {
            "bank_konto_id": bank_konto_id,
            "buchungsdatum": str(roh.buchungsdatum),
            "betrag_cent": roh.betrag_cent,
            "waehrung": roh.waehrung,
            "referenz": (roh.legacy_referenz or "").strip().lower(),
            "gegenkonto_iban": (roh.legacy_gegenkonto_iban or "").strip().upper(),
        }
    )


@dataclass(frozen=True)
class ZuordnungsErgebnis:
    zugeordnet: bool
    grund: str
    zuordnung_id: int | None = None
    op_position_id: int | None = None


@dataclass(frozen=True)
class ZuordnungsVorschauZeile:
    roh: RohTransaktion
    vorgeschlagenes_konto_id: str | None
    grund: str


class BankImportService:
    def __init__(
        self,
        repository: BankRepository,
        stammdaten_repository: StammdatenRepository,
        op_service: OPService,
    ):
        self._repository = repository
        self._stammdaten_repository = stammdaten_repository
        self._op_service = op_service
        self._session_factory = repository.session_factory

    # -- Import -----------------------------------------------------------
    def importiere_camt053(
        self, *, ctx: AuthContext, bank_konto: BankKontoTable, xml_bytes: bytes
    ) -> list[BankTransaktionTable]:
        require_gesellschaft_access(ctx, bank_konto.gesellschaft_id)
        require_schreibrecht(ctx)
        # erwartete_iban erzwingt, dass jedes Stmt/Acct in der Datei zum
        # explizit ausgewählten Bankkonto passt (siehe
        # CamtKontoMismatchError) - kein fremdes/gemischtes Konto wird
        # pauschal diesem Bankkonto zugeordnet.
        rohdaten = parse_camt053(xml_bytes, erwartete_iban=bank_konto.iban)
        return self._importiere_atomar(rohdaten, bank_konto, "CAMT053")

    def importiere_csv(
        self, *, ctx: AuthContext, bank_konto: BankKontoTable, text: str, mapping: CsvSpaltenMapping
    ) -> list[BankTransaktionTable]:
        require_gesellschaft_access(ctx, bank_konto.gesellschaft_id)
        require_schreibrecht(ctx)
        rohdaten = parse_csv(text, mapping)
        return self._importiere_atomar(rohdaten, bank_konto, "CSV")

    def _importiere_atomar(
        self, rohdaten: list[RohTransaktion], bank_konto: BankKontoTable, quelle_typ: str
    ) -> list[BankTransaktionTable]:
        """Der gesamte Dateiimport ist EINE DB-Transaktion: scheitert eine
        Zeile (Konflikt, Formatfehler, ...), wird der GESAMTE Aufruf
        zurückgerollt - keine Zeile aus diesem Aufruf bleibt hängen. Ein
        erneuter Lauf nach Korrektur der Quelle ist dank Idempotenz
        (import_id/Fingerprint) immer gefahrlos, weil nichts Teilweises
        übrig bleibt, auf das man Rücksicht nehmen müsste.

        `schreibgesperrte_session` (statt `self._session_factory()`
        direkt): für Zeilen MIT bankseitig eindeutiger `native_id` ist die
        Idempotenz bereits durch den echten DB-UNIQUE-Constraint auf
        `import_id` geschützt (unabhängig vom Locking - eine zweite,
        echt gleichzeitige Transaktion mit demselben `import_id` schlägt
        dort so oder so mit einem Constraint-Fehler fehl statt still zu
        duplizieren). Für Zeilen OHNE `native_id` hängt die Dublettenprüfung
        dagegen ausschließlich an `find_by_fingerprint` - einem
        SELECT-dann-Entscheiden OHNE DB-Backstop (`fingerprint_hash` ist
        nur indiziert, nicht `UNIQUE`) - und unter Datei-SQLite ist
        `with_for_update` dafür ohnehin ein Kein-Op. Zwei echt
        gleichzeitige Importe DERSELBEN Datei (identischer Inhalt, keine
        `native_id`) auf dasselbe Bankkonto könnten sich sonst gegenseitig
        als "noch nicht vorhanden" sehen und beide dieselbe wirtschaftliche
        Zahlung als je eigene Zeile einbuchen (dieselbe Fehlerklasse wie
        Codex' Paket-B-Befund bei `verknuepfe_mit_bestehender_zahlung`,
        siehe `infrastructure/db/sqlite_write_lock.py`)."""

        with schreibgesperrte_session(self._session_factory) as session:
            ergebnisse: list[BankTransaktionTable] = []
            try:
                for index, roh in enumerate(rohdaten):
                    try:
                        ergebnisse.append(self._speichere_roh(bank_konto, roh, quelle_typ, session=session))
                    except Exception as exc:
                        # Ursprünglichen Fehlertyp NICHT verschlucken (Aufrufer
                        # unterscheiden z. B. MehrfachbuchungsKonfliktError von
                        # CamtUnvollstaendigError) - nur mit Zeilenkontext
                        # anreichern und weiterreichen. Die Transaktion wird
                        # trotzdem vollständig zurückgerollt (siehe unten):
                        # NICHTS aus diesem Aufruf bleibt hängen.
                        exc.args = (
                            f"Import von Bankkonto {bank_konto.id} ({quelle_typ}) abgebrochen bei Zeile "
                            f"{index + 1} von {len(rohdaten)}: {exc}. Die gesamte Datei wurde NICHT gebucht "
                            "(atomarer Import); nach Korrektur kann der volle Lauf gefahrlos wiederholt werden.",
                        )
                        raise
                session.commit()
            except Exception:
                session.rollback()
                raise
            for row in ergebnisse:
                session.refresh(row)
            return ergebnisse

    def _speichere_roh(
        self, bank_konto: BankKontoTable, roh: RohTransaktion, quelle_typ: str, *, session: Session | None = None
    ) -> BankTransaktionTable:
        content_hash = _hash(
            {
                "bank_konto_id": bank_konto.id,
                "betrag_cent": roh.betrag_cent,
                "waehrung": roh.waehrung,
                "buchungsdatum": str(roh.buchungsdatum),
                "valuta": str(roh.valuta) if roh.valuta else None,
                "referenz": roh.referenz,
                "gegenkonto_iban": roh.gegenkonto_iban,
                "gegenkonto_name": roh.gegenkonto_name,
                "native_id": roh.native_id,
            }
        )
        if roh.native_id:
            hat_native_id = True
            fingerprint_hash = None
            import_id = f"{bank_konto.id}:{quelle_typ}:{roh.native_id}"
        else:
            hat_native_id = False
            fingerprint_hash = _fingerprint(bank_konto.id, roh)
            bestehende = self._repository.find_by_fingerprint(bank_konto.id, fingerprint_hash, session=session)
            if bestehende is not None:
                raise MehrfachbuchungsKonfliktError(
                    f"Banktransaktion ohne bankseitig eindeutige Kennung auf Bankkonto {bank_konto.id}: "
                    f"Betrag/Datum/Referenz sind identisch zur bereits importierten Transaktion "
                    f"#{bestehende.id}. Das kann dieselbe Zahlung aus einem überlappenden Export sein ODER "
                    f"eine zweite, echte Zahlung mit zufällig identischen Merkmalen - das lässt sich nicht "
                    f"automatisch entscheiden und wird daher NICHT still zusammengelegt oder dupliziert, "
                    f"sondern zur manuellen Klärung verweigert."
                )
            # Codex-Rückprüfung b8d700d: der Fingerprint hängt an
            # referenz/gegenkonto_iban - Felder, die ein Parser-Update
            # (z. B. der CAMT-Gegenpartei-Fix selbst) ändern kann, OHNE
            # dass sich an der zugrunde liegenden Zahlung etwas geändert
            # hätte. Ohne diesen Zusatzvergleich würde eine VOR einem
            # solchen Update bereits importierte Zeile beim erneuten
            # Einlesen derselben Datei NICHT mehr gefunden (neuer
            # Fingerprint != alter, gespeicherter Fingerprint) und
            # STILLSCHWEIGEND ein zweites Mal eingefügt. Konservativ: nur
            # relevant, wenn der Parser überhaupt Legacy-Felder geliefert
            # hat (z. B. CAMT, nicht CSV) UND sich Alt-/Neu-Fingerprint
            # tatsächlich unterscheiden.
            legacy_fingerprint_hash = _legacy_fingerprint(bank_konto.id, roh)
            if legacy_fingerprint_hash is not None and legacy_fingerprint_hash != fingerprint_hash:
                legacy_bestehende = self._repository.find_by_fingerprint(
                    bank_konto.id, legacy_fingerprint_hash, session=session
                )
                if legacy_bestehende is not None:
                    raise MehrfachbuchungsKonfliktError(
                        f"Banktransaktion ohne bankseitig eindeutige Kennung auf Bankkonto {bank_konto.id}: "
                        f"Datum/Betrag/Währung stimmen mit der bereits importierten Transaktion "
                        f"#{legacy_bestehende.id} überein, die noch mit der VOR diesem Parser-Update "
                        "gültigen Gegenpartei-/Referenzermittlung gespeichert wurde. Um einen stillen "
                        "Doppelimport nach dem Update zu vermeiden, wird das NICHT automatisch als neue, "
                        "unabhängige Zahlung eingefügt, sondern zur manuellen Klärung verweigert."
                    )
            # Bewusst kein wiederverwendbarer Schlüssel: ohne native ID kann
            # ein Replay nicht von einer echten zweiten Zahlung
            # unterschieden werden, daher entscheidet ausschließlich der
            # obige Fingerprint-Konflikt, nicht die import_id-Idempotenz.
            import_id = f"{bank_konto.id}:{quelle_typ}:NOID:{uuid.uuid4()}"

        row = BankTransaktionTable(
            bank_konto_id=bank_konto.id,
            betrag_cent=roh.betrag_cent,
            waehrung=roh.waehrung,
            buchungsdatum=roh.buchungsdatum,
            valuta=roh.valuta,
            referenz=roh.referenz,
            gegenkonto_iban=roh.gegenkonto_iban,
            gegenkonto_name=roh.gegenkonto_name,
            quelle_typ=quelle_typ,
            quelle_hash=content_hash,
            import_id=import_id,
            hat_native_id=hat_native_id,
            fingerprint_hash=fingerprint_hash,
            roh_zeile=roh.roh_zeile,
        )
        return self._repository.insert_transaktion_idempotent(row, session=session)

    # -- Zuordnung (OP-Buchung + Zuordnung + Audit als EINE Transaktion) ----
    def _resolve_konto_fuer_referenz(self, referenz: str | None) -> tuple[KontoTable | None, str]:
        """Gemeinsame Auflösung "Referenz -> eindeutiges Zielkonto", die
        sowohl der bereits persistierten automatischen Zuordnung als auch
        der reinen Vorab-Vorschau (`vorschau_zuordnungsvorschlaege`) auf
        noch NICHT importierten Rohzeilen zugrunde liegt. Ausschließlich
        eine eindeutige `VERTRAG:<id>`-Kennung zählt; Name/Betrag allein
        reichen nie."""

        if not referenz:
            return None, "Keine Referenz vorhanden; nur manuelle Zuordnung möglich."
        treffer = _VERTRAG_REFERENZ.search(referenz)
        if not treffer:
            return None, "Referenz enthält keine eindeutige Vertragskennung; Name/Betrag allein reichen nicht."
        vertrag_id = treffer.group(1)
        konto = self._stammdaten_repository.get_konto_by_vertrag(vertrag_id)
        if konto is None:
            return None, f"Referenzierter Vertrag {vertrag_id} hat kein Konto."
        return konto, "eindeutig"

    def _ist_automatisch_zuordenbar(self, transaktion: BankTransaktionTable) -> tuple[KontoTable | None, str]:
        """Reine Vorprüfung für den GRACEFUL-SKIP-Pfad von
        `automatisch_zuordnen` (kein DB-Schreibzugriff): entscheidet, ob
        überhaupt ein Versuch unternommen wird. Die eigentliche,
        maßgebliche Validierung passiert danach innerhalb derselben
        Transaktion wie die Buchung selbst (`BankRepository.create_zuordnung`)."""

        if transaktion.betrag_cent <= 0:
            return None, "Nur Zahlungseingänge (positiver Betrag) werden automatisch zugeordnet."
        konto, grund = self._resolve_konto_fuer_referenz(transaktion.referenz)
        if konto is None:
            return None, grund
        verbleibend = transaktion.betrag_cent - self._repository.zugeordneter_betrag(transaktion.id)
        if verbleibend <= 0:
            return None, "Transaktion ist bereits vollständig zugeordnet."
        return konto, "eindeutig"

    def vorschau_zuordnungsvorschlaege(self, rohdaten: list[RohTransaktion]) -> list["ZuordnungsVorschauZeile"]:
        """Reine Lesevorschau auf NOCH NICHT importierten Rohzeilen (kein
        DB-Schreibzugriff, keine persistierte Banktransaktion nötig):
        schlägt je Zeile ein Zielkonto nach denselben Regeln wie
        `automatisch_zuordnen` vor (nur eindeutige Vertragsreferenz,
        niemals Name/Betrag). Die maßgebliche Zuordnung entsteht erst nach
        ausdrücklicher Bestätigung, gegen die dann tatsächlich importierte
        und persistierte Transaktion (`automatisch_zuordnen`/
        `zuordnen_manuell`) - diese Vorschau ist nur Anzeige."""

        ergebnisse: list[ZuordnungsVorschauZeile] = []
        for roh in rohdaten:
            if roh.betrag_cent <= 0:
                ergebnisse.append(ZuordnungsVorschauZeile(roh, None, "Kein Zahlungseingang (Betrag <= 0)."))
                continue
            konto, grund = self._resolve_konto_fuer_referenz(roh.referenz)
            ergebnisse.append(ZuordnungsVorschauZeile(roh, konto.id if konto is not None else None, grund))
        return ergebnisse

    def schlage_konto_vor(self, transaktion: BankTransaktionTable) -> tuple[KontoTable | None, str]:
        """Reine Lesevorschau für eine BEREITS persistierte, aber noch
        unzugeordnete Transaktion (z. B. für eine Backoffice-Liste
        offener Zuordnungen) - bucht nichts."""

        if transaktion.betrag_cent <= 0:
            return None, "Nur Zahlungseingänge (positiver Betrag) können automatisch vorgeschlagen werden."
        return self._resolve_konto_fuer_referenz(transaktion.referenz)

    def automatisch_zuordnen(self, *, ctx: AuthContext, transaktion: BankTransaktionTable) -> ZuordnungsErgebnis:
        konto, grund = self._ist_automatisch_zuordenbar(transaktion)
        if konto is None:
            return ZuordnungsErgebnis(False, grund)

        verbleibend = transaktion.betrag_cent - self._repository.zugeordneter_betrag(transaktion.id)
        # Deterministische Vorgangs-ID: ein Retry DERSELBEN automatischen
        # Zuordnung für dieselbe Transaktion ist ein No-Op, kein
        # Doppelversuch.
        vorgang_id = f"AUTO-{transaktion.id}"
        op_row, zuordnung = self._zuordnen_atomar(
            ctx=ctx,
            konto=konto,
            transaktion=transaktion,
            betrag_cent=verbleibend,
            beleg_referenz=f"Bankzahlung {transaktion.referenz}",
            match_typ=ZahlungsMatchTyp.AUTOMATISCH_EINDEUTIG.value,
            vorgang_id=vorgang_id,
            quelle_system="bank_auto_match",
        )
        return ZuordnungsErgebnis(True, "Eindeutige Vertragsreferenz gefunden.", zuordnung.id, op_row.id)

    def zuordnen_manuell(
        self,
        *,
        ctx: AuthContext,
        transaktion: BankTransaktionTable,
        konto: KontoTable,
        betrag_cent: int,
        beleg_referenz: str,
        vorgang_id: str,
    ):
        """`vorgang_id` MUSS vom Aufrufer explizit und bewusst vergeben
        werden: derselbe Wert bei einem Retry (z. B. nach einem
        Netzwerk-Timeout) macht den erneuten Aufruf zu einem sicheren
        No-Op; ein NEUER Wert für eine tatsächlich neue, unabhängige
        Teilzuordnung (auch mit zufällig identischem Betrag) erzeugt
        garantiert eine eigene Buchung statt mit einer bestehenden
        Zuordnung verwechselt zu werden."""

        require_gesellschaft_access(ctx, konto.gesellschaft_id)
        require_schreibrecht(ctx)
        op_row, zuordnung = self._zuordnen_atomar(
            ctx=ctx,
            konto=konto,
            transaktion=transaktion,
            betrag_cent=betrag_cent,
            beleg_referenz=beleg_referenz,
            match_typ=ZahlungsMatchTyp.MANUELL.value,
            vorgang_id=vorgang_id,
            quelle_system="bank_manuell",
        )
        return zuordnung

    def _resolve_periode_und_forderung(
        self, konto_id: str, referenz: str | None, *, session: Session
    ) -> tuple[str | None, int | None]:
        """Deterministische End-zu-Ende-Verbindung Bank-Verwendungszweck ->
        `OP.leistungsperiode` -> `offene_forderungen` (Auftrag
        HV-20260919-GEORGE-CODE, ergänzt bei Codex-Abnahme von 8a677e7/
        db3755a): löst `erkenne_leistungsperiode` gegen die AKTUELL offenen
        Forderungen DIESES Kontos auf.

        Eine erkannte Periode wird IMMER auf der neuen ZAHLUNG vermerkt
        (`leistungsperiode`) - das bleibt am Beleg sichtbar/prüfbar, auch
        wenn keine automatische Bindung entsteht. Eine verbindliche
        `bezieht_sich_auf_id`-Bindung entsteht dagegen NUR, wenn GENAU EINE
        offene Forderung (keine Mahnkosten-Nebenforderung) exakt diese
        Periode trägt - mehrere Treffer (z. B. zwei Komponenten derselben
        Periode wurden separat als eigene Forderungen geführt) oder gar
        keiner bleiben bewusst UNGEBUNDEN statt per FIFO geraten zu werden.
        Dieselbe Fachregel wie `op.service.offene_forderungen`/
        `resolve_zahlungsziel` - eine hier entstehende Bindung wird dort
        wie jede andere geprüft (Konto-/Periodenkonsistenz), ein Überhang
        über die Zielforderung hinaus fließt identisch in den generischen
        Pool, der auch für die Zinsberechnung (`mahnwesen.kosten.
        balance_zeitreihe_fuer_forderung`) maßgeblich ist.

        Gilt IDENTISCH für automatische (`automatisch_zuordnen`) und
        manuelle (`zuordnen_manuell`) Zuordnung, da beide über
        `_zuordnen_atomar` laufen; eine explizite manuelle Verknüpfung mit
        einer bereits BESTEHENDEN Zahlung (`verknuepfe_mit_bestehender_
        zahlung`) bucht ohnehin keine neue OP-Zeile und ist von dieser
        Erkennung unberührt - dort validiert bereits die bestehende
        Konto-/Typ-/Status-Prüfung die explizite menschliche Auswahl.

        Codex-Rückprüfung b8d700d: läuft INNERHALB derselben
        schreibgesperrten Transaktion wie die anschließende OP-Buchung
        (`session` Pflichtparameter, kein eigener Vorab-Read mehr VOR dem
        Lock) - eine Auflösung VOR dem Lock wäre ein TOCTOU-Fenster
        (zwischenzeitliche Tilgung/Änderung der Zielforderung durch eine
        andere, echt gleichzeitige Transaktion). Ein exakter Retry
        DERSELBEN `vorgang_id` bleibt trotzdem ein sicherer No-Op: die
        eigentliche Buchung entscheidet ausschließlich über `import_id`/
        `quelle_hash` (siehe `OPRepository.insert_idempotent`), der hier
        neu aufgelöste `bezieht_sich_auf_id`-Wert eines Retries wird bei
        einem Treffer auf die bereits bestehende Zeile NICHT mehr
        geschrieben (die bestehende Zeile wird unverändert zurückgegeben)
        - eine zwischenzeitlich geänderte Ziel-ID erzeugt dabei NIE einen
        Konflikt, weil `bezieht_sich_auf_id` bewusst kein Bestandteil des
        Inhalts-Hashs ist."""

        leistungsperiode = erkenne_leistungsperiode(referenz)
        if leistungsperiode is None:
            return None, None
        treffer = [
            f for f in self._op_service.offene_forderungen(konto_id, session=session)
            if f.leistungsperiode == leistungsperiode and f.quelle_system != "mahnkosten"
        ]
        if len(treffer) != 1:
            return leistungsperiode, None
        return leistungsperiode, treffer[0].op_position_id

    def _zuordnen_atomar(
        self,
        *,
        ctx: AuthContext,
        konto: KontoTable,
        transaktion: BankTransaktionTable,
        betrag_cent: int,
        beleg_referenz: str,
        match_typ: str,
        vorgang_id: str,
        quelle_system: str,
    ):
        """OP-Buchung + Zuordnung in EINER DB-Transaktion: schlägt die
        Zuordnungserstellung nach der Buchung fehl (Validierungsfehler ODER
        ein unerwarteter Fehler), wird die Buchung mit zurückgerollt - es
        bleibt nie eine Zahlung ohne zugehörige Zuordnung im Ledger stehen.

        `schreibgesperrte_session` (statt `self._session_factory()` direkt):
        unter Datei-SQLite nimmt diese Transaktion ihren Schreib-Lock bereits
        bei ihrem ersten Statement, nicht erst beim Insert - siehe
        `infrastructure/db/sqlite_write_lock.py` (Codex-Rückprüfung Paket B,
        `with_for_update` schützt SQLite nicht). Die Perioden-/Zielauflösung
        (`_resolve_periode_und_forderung`) läuft INNERHALB dieser Sperre,
        nicht davor (Codex-Rückprüfung b8d700d) - siehe dortige
        Docstring-Begründung."""

        with schreibgesperrte_session(self._session_factory) as session:
            try:
                leistungsperiode, bezieht_sich_auf_id = self._resolve_periode_und_forderung(
                    konto.id, transaktion.referenz, session=session,
                )
                op_row = self._op_service.buchen(
                    ctx=ctx,
                    konto=konto,
                    typ=OPTyp.ZAHLUNG,
                    betrag_cent=betrag_cent,
                    belegdatum=transaktion.buchungsdatum,
                    buchungsdatum=transaktion.buchungsdatum,
                    faelligkeit=None,
                    beleg_referenz=beleg_referenz,
                    leistungsperiode=leistungsperiode,
                    bezieht_sich_auf_id=bezieht_sich_auf_id,
                    import_id=f"ZAHLUNG-BANK-{transaktion.id}-{vorgang_id}",
                    quelle_system=quelle_system,
                    bank_transaktion_id=transaktion.id,
                    session=session,
                )
                zuordnung = self._repository.create_zuordnung(
                    bank_transaktion_id=transaktion.id,
                    op_position_id=op_row.id,
                    betrag_cent=betrag_cent,
                    match_typ=match_typ,
                    vorgang_id=vorgang_id,
                    session=session,
                )
                session.commit()
            except Exception:
                session.rollback()
                raise
            session.refresh(op_row)
            session.refresh(zuordnung)
            return op_row, zuordnung

    def verknuepfe_mit_bestehender_zahlung(
        self,
        *,
        ctx: AuthContext,
        transaktion: BankTransaktionTable,
        op_position: OPPositionTable,
        konto: KontoTable,
        betrag_cent: int,
        vorgang_id: str,
    ) -> ZuordnungTable:
        """Verknüpft eine bereits eingelesene Rohtransaktion mit einer
        EXPLIZIT gewählten, bereits BESTEHENDEN ZAHLUNG-OP-Position -
        bucht KEINE neue OP-Zeile (im Unterschied zu `zuordnen_manuell`/
        `automatisch_zuordnen`, die beide eine neue ZAHLUNG erzeugen und
        dann DIESE neue Zeile zuordnen).

        Gedacht für den Fall, dass ein Zahlungseingang bereits VOR dem
        echten Bankfeed (z. B. über den Intake oder eine frühere manuelle
        Buchung) als ZAHLUNG gebucht wurde: eine später eingelesene
        Rohtransaktion bestätigt/erklärt diese bereits existierende
        Zahlung, statt sie ein zweites Mal gutzuschreiben (Fachregel:
        bestehende Mieterkonto-Buchungen dürfen bei einem späteren
        Rohbankimport nicht doppelt gutgeschrieben werden).

        Prüfung UND Verknüpfung laufen in EINER gesperrten DB-Transaktion,
        Transaktion/OP/Konto werden per ID frisch aus der DB geladen
        (`with_for_update`, wo unterstützt; unter Datei-SQLite zusätzlich
        `schreibgesperrte_session` - `with_for_update` ist dort ein Kein-Op,
        siehe `infrastructure/db/sqlite_write_lock.py`) - dieselbe Vorsicht
        wie bei `verarbeite_ruecklastschrift`, da die Aufrufer-Objekte
        veraltet sein können:

        - `op_position` muss laut DB eine AKTIVE ZAHLUNG sein und zum
          übergebenen `konto` gehören.
        - Gesellschaft/Währung von Transaktion und Konto müssen
          übereinstimmen (`BankRepository.create_zuordnung` prüft dies
          zusätzlich autoritativ).
        - `betrag_cent` darf weder den noch nicht zugeordneten Restbetrag
          der TRANSAKTION noch den noch nicht "erklärten" Restbetrag der
          ZAHLUNG selbst überschreiten (beide Seiten getrennt begrenzt).
        - Ein Link-Duplikat (dasselbe Transaktion/OP-Paar unter einer
          ANDEREN `vorgang_id`) wird abgelehnt; ein exakter Retry MIT
          derselben `vorgang_id` bleibt ein sicherer No-Op
          (`BankRepository.create_zuordnung`).
        - Bestehende Ledger-Zeilen bleiben unverändert (append-only) -
          diese Methode fügt ausschließlich eine neue `ZuordnungTable`-
          Zeile hinzu, nie eine neue/geänderte `OPPositionTable`-Zeile.

        Codex-Rückprüfung Paket B: der Replay-Kurzschluss für eine
        WIEDERHOLTE `vorgang_id` darf NIEMALS vor den Bindungs-/Mandanten-/
        Währungsprüfungen liegen - sonst könnte ein Aufrufer mit einem
        eigenen, an sich berechtigten Konto über die IDs einer FREMDEN
        Transaktion/OP plus derselben `vorgang_id`/demselben Betrag die
        FREMDE Zuordnung als vermeintlichen "eigenen Replay" zurück-
        bekommen, ohne dass deren tatsächliche Konto-/Mandanten-/
        Währungszugehörigkeit je geprüft wurde. Nur die NACHFOLGENDEN
        Restbetrags-/Link-Duplikat-Prüfungen dürfen für einen echten
        Replay übersprungen werden (siehe unten, direkt vor ihnen)."""

        transaktion_id = transaktion.id
        op_position_id = op_position.id
        konto_id = konto.id

        with schreibgesperrte_session(self._session_factory) as session:
            try:
                frisches_konto = session.get(KontoTable, konto_id)
                if frisches_konto is None:
                    raise ValueError(f"Unbekanntes Konto {konto_id}")
                require_gesellschaft_access(ctx, frisches_konto.gesellschaft_id)
                require_schreibrecht(ctx)

                frische_transaktion = session.get(BankTransaktionTable, transaktion_id, with_for_update=True)
                if frische_transaktion is None:
                    raise ValueError(f"Unbekannte Banktransaktion {transaktion_id}")
                frische_op = session.get(OPPositionTable, op_position_id, with_for_update=True)
                if frische_op is None:
                    raise ValueError(f"Unbekannte OPPosition {op_position_id}")

                # -- Reine Identitäts-/Bindungsprüfungen - gelten AUCH für
                # einen exakten Replay derselben vorgang_id (siehe Docstring
                # oben); nur die Restbetrags-/Link-Duplikat-Prüfungen weiter
                # unten dürfen für einen Replay übersprungen werden.
                if frische_op.konto_id != frisches_konto.id:
                    raise BindungInkonsistentError(
                        f"OPPosition {op_position_id} gehört zu Konto {frische_op.konto_id}, nicht zu {konto_id}."
                    )
                if OPTyp(frische_op.typ) is not OPTyp.ZAHLUNG:
                    raise ZuordnungUngueltigError(
                        f"Verknüpfung mit einer bestehenden Zahlung setzt eine ZAHLUNG-OP voraus; "
                        f"OPPosition {op_position_id} ist vom Typ {frische_op.typ}."
                    )
                if frische_op.status != "AKTIV":
                    raise ZuordnungUngueltigError(
                        f"OPPosition {op_position_id} ist storniert und kann nicht verknüpft werden."
                    )
                if frische_transaktion.betrag_cent <= 0:
                    raise ZuordnungUngueltigError(
                        "Nur ein tatsächlicher Zahlungseingang (positiver Transaktionsbetrag) kann mit einer "
                        "bestehenden Zahlung verknüpft werden."
                    )

                bank_konto = session.get(BankKontoTable, frische_transaktion.bank_konto_id)
                if bank_konto is None:
                    raise ValueError(f"Unbekanntes Bankkonto {frische_transaktion.bank_konto_id}")
                if bank_konto.gesellschaft_id != frisches_konto.gesellschaft_id:
                    raise CrossTenantError(
                        f"Banktransaktion (Gesellschaft {bank_konto.gesellschaft_id}) darf nicht mit Konto "
                        f"{konto_id} (Gesellschaft {frisches_konto.gesellschaft_id}) verknüpft werden."
                    )
                if frische_transaktion.waehrung != frisches_konto.waehrung:
                    raise FremdwaehrungNichtUnterstuetztError(
                        f"Transaktion {transaktion_id} ({frische_transaktion.waehrung}) und Konto {konto_id} "
                        f"({frisches_konto.waehrung}) haben unterschiedliche Währungen."
                    )
                if betrag_cent <= 0:
                    raise ZuordnungUngueltigError("Verknüpfungsbetrag muss positiv sein.")

                # Ein exakter Retry DERSELBEN vorgang_id ist JETZT (nach
                # ALLEN obigen Bindungsprüfungen) ein sicherer No-Op - nur
                # die nachfolgenden Restbetrags-Prüfungen werden für ihn
                # übersprungen, sonst würde die bereits durch DIESEN
                # Vorgang belegte Menge den Retry selbst als "überschreitet
                # den Restbetrag" ablehnen. Ein Konflikt (andere
                # vorgang_id-Zuordnung mit abweichendem Inhalt) wird
                # weiterhin autoritativ von `BankRepository.create_zuordnung`
                # unten erkannt.
                bestehender_vorgang = session.execute(
                    select(ZuordnungTable).where(ZuordnungTable.vorgang_id == vorgang_id)
                ).scalar_one_or_none()
                if (
                    bestehender_vorgang is not None
                    and bestehender_vorgang.bank_transaktion_id == transaktion_id
                    and bestehender_vorgang.op_position_id == op_position_id
                    and bestehender_vorgang.betrag_cent == betrag_cent
                ):
                    session.commit()
                    return bestehender_vorgang

                zugeordnet_transaktion = self._repository.zugeordneter_betrag(transaktion_id, session=session)
                verbleibend_transaktion = frische_transaktion.betrag_cent - zugeordnet_transaktion
                if betrag_cent > verbleibend_transaktion:
                    raise ZuordnungUngueltigError(
                        f"Verknüpfungsbetrag {betrag_cent} überschreitet den verbleibenden, noch nicht "
                        f"zugeordneten Betrag der Transaktion {transaktion_id} ({verbleibend_transaktion} von "
                        f"{frische_transaktion.betrag_cent})."
                    )

                bereits_verknuepft_op = self._repository.verknuepfter_betrag_fuer_op(op_position_id, session=session)
                verbleibend_op = abs(frische_op.betrag_cent) - bereits_verknuepft_op
                if betrag_cent > verbleibend_op:
                    raise ZuordnungUngueltigError(
                        f"Verknüpfungsbetrag {betrag_cent} überschreitet den noch nicht erklärten Restbetrag der "
                        f"Zahlung {op_position_id} ({verbleibend_op} von {abs(frische_op.betrag_cent)})."
                    )

                bestehende_links = session.execute(
                    select(ZuordnungTable)
                    .where(ZuordnungTable.bank_transaktion_id == transaktion_id)
                    .where(ZuordnungTable.op_position_id == op_position_id)
                ).scalars().all()
                for link in bestehende_links:
                    if link.vorgang_id != vorgang_id:
                        raise ZuordnungUngueltigError(
                            f"Transaktion {transaktion_id} ist bereits mit OPPosition {op_position_id} verknüpft "
                            f"(Zuordnung #{link.id}, vorgang_id '{link.vorgang_id}') - ein Link-Duplikat mit "
                            f"neuer vorgang_id '{vorgang_id}' wird abgelehnt."
                        )

                zuordnung = self._repository.create_zuordnung(
                    bank_transaktion_id=transaktion_id,
                    op_position_id=op_position_id,
                    betrag_cent=betrag_cent,
                    match_typ=ZahlungsMatchTyp.MANUELL.value,
                    vorgang_id=vorgang_id,
                    session=session,
                )
                session.commit()
            except Exception:
                session.rollback()
                raise
            session.refresh(zuordnung)
            return zuordnung

    def verarbeite_ruecklastschrift(
        self,
        *,
        ctx: AuthContext,
        transaktion: BankTransaktionTable,
        original_op_position: OPPositionTable,
        konto: KontoTable,
        betrag_cent: int | None = None,
    ) -> OPPositionTable:
        """Bucht eine Rücklastschrift als eigene RUECKLASTSCHRIFT-Zeile.

        Codex-Rückprüfung: die Aufrufer-Objekte `transaktion`/
        `original_op_position`/`konto` können vom Repository abgelöste,
        veraltete oder manipulierte Python-Objekte sein (nur ihre IDs sind
        vertrauenswürdig). Prüfung UND Buchung laufen deshalb in EINER
        gesperrten DB-Transaktion, die Transaktion, Original-OP, Konto und
        Bankkonto per ID frisch aus der DB lädt (`with_for_update`, wo
        unterstützt) und ausschließlich auf Basis dieser frischen Zeilen
        validiert:

        - `transaktion` ist laut DB ein ECHTER negativer Bankeingang
          (Belastung), nicht irgendeine (insbesondere nicht dieselbe
          positive) Transaktion.
        - `transaktion` liegt auf dem GLEICHEN Bankkonto wie mindestens eine
          Zuordnung der ursprünglichen Zahlung (passendes Bankkonto).
        - Gesellschaft und Währung von Transaktion und Konto stimmen überein.
        - Der angeforderte Betrag überschreitet weder den verfügbaren
          Belastungsbetrag DIESER Transaktion (abzüglich bereits über sie
          gebuchter Rückbuchungen) noch die kumulative Rückbuchungsgrenze
          der Ursprungszahlung (abzüglich bereits gegen SIE gebuchter
          Rückbuchungen).

        `schreibgesperrte_session` (statt `self._session_factory()` direkt):
        unter Datei-SQLite ist `with_for_update` unwirksam (kein echtes
        Zeilen-Locking) - siehe `infrastructure/db/sqlite_write_lock.py`
        (Codex-Rückprüfung Paket B).
        """

        transaktion_id = transaktion.id
        original_op_position_id = original_op_position.id
        konto_id = konto.id

        with schreibgesperrte_session(self._session_factory) as session:
            try:
                frisches_konto = session.get(KontoTable, konto_id)
                if frisches_konto is None:
                    raise ValueError(f"Unbekanntes Konto {konto_id}")
                require_gesellschaft_access(ctx, frisches_konto.gesellschaft_id)
                require_schreibrecht(ctx)

                frische_transaktion = session.get(BankTransaktionTable, transaktion_id, with_for_update=True)
                if frische_transaktion is None:
                    raise ValueError(f"Unbekannte Banktransaktion {transaktion_id}")
                frisches_original_op = session.get(OPPositionTable, original_op_position_id, with_for_update=True)
                if frisches_original_op is None:
                    raise ValueError(f"Unbekannte OPPosition {original_op_position_id}")

                if frisches_original_op.konto_id != frisches_konto.id:
                    raise BindungInkonsistentError(
                        f"OPPosition {original_op_position_id} gehört zu Konto {frisches_original_op.konto_id}, "
                        f"nicht zu {konto_id}."
                    )
                if OPTyp(frisches_original_op.typ) is not OPTyp.ZAHLUNG:
                    raise ZuordnungUngueltigError(
                        f"Rücklastschrift muss sich auf eine Zahlung beziehen, OPPosition "
                        f"{original_op_position_id} ist aber vom Typ {frisches_original_op.typ}."
                    )
                if frisches_original_op.status != "AKTIV":
                    raise ZuordnungUngueltigError(
                        f"OPPosition {original_op_position_id} ist bereits storniert und kann nicht "
                        "zurückgebucht werden."
                    )
                zuordnungen = list(
                    session.execute(
                        select(ZuordnungTable).where(ZuordnungTable.op_position_id == original_op_position_id)
                    ).scalars().all()
                )
                if not zuordnungen:
                    raise ZuordnungUngueltigError(
                        f"Zur ursprünglichen Zahlung {original_op_position_id} existiert keine Bank-Zuordnung; "
                        "eine Rücklastschrift ohne belegte Ursprungszahlung wird abgelehnt."
                    )

                # Muss laut DB ein ECHTER negativer Eingang sein - nicht
                # dieselbe (oder eine beliebige andere) positive Transaktion,
                # die als "Rücklastschrift" untergeschoben wird.
                if frische_transaktion.betrag_cent >= 0:
                    raise ZuordnungUngueltigError(
                        f"Transaktion {transaktion_id} ist laut DB kein negativer Bankeingang "
                        f"(Betrag {frische_transaktion.betrag_cent}); eine Rücklastschrift erfordert eine "
                        "tatsächliche Belastung."
                    )

                bank_konto = session.get(BankKontoTable, frische_transaktion.bank_konto_id)
                if bank_konto is None:
                    raise ValueError(f"Unbekanntes Bankkonto {frische_transaktion.bank_konto_id}")
                if bank_konto.gesellschaft_id != frisches_konto.gesellschaft_id:
                    raise CrossTenantError(
                        f"Rücklastschrift-Transaktion (Gesellschaft {bank_konto.gesellschaft_id}) darf nicht mit "
                        f"Konto {konto_id} (Gesellschaft {frisches_konto.gesellschaft_id}) verrechnet werden."
                    )
                # Passendes Bankkonto: die Rücklastschrift muss auf demselben
                # Bankkonto eingehen, auf dem auch die Ursprungszahlung verbucht wurde.
                original_bank_konto_ids = set()
                for zuordnung in zuordnungen:
                    original_transaktion = session.get(BankTransaktionTable, zuordnung.bank_transaktion_id)
                    if original_transaktion is not None:
                        original_bank_konto_ids.add(original_transaktion.bank_konto_id)
                if frische_transaktion.bank_konto_id not in original_bank_konto_ids:
                    raise ZuordnungUngueltigError(
                        f"Rücklastschrift-Transaktion {transaktion_id} liegt auf Bankkonto "
                        f"{frische_transaktion.bank_konto_id}, die Ursprungszahlung aber auf "
                        f"{sorted(original_bank_konto_ids)}; passendes Bankkonto erforderlich."
                    )
                if frische_transaktion.waehrung != frisches_konto.waehrung:
                    raise FremdwaehrungNichtUnterstuetztError(
                        f"Rücklastschrift-Transaktion {transaktion_id} ({frische_transaktion.waehrung}) und "
                        f"Konto {konto_id} ({frisches_konto.waehrung}) haben unterschiedliche Währungen."
                    )

                ursprungsbetrag = abs(frisches_original_op.betrag_cent)
                effektiver_betrag = betrag_cent if betrag_cent is not None else ursprungsbetrag
                if effektiver_betrag <= 0:
                    raise ZuordnungUngueltigError("Rücklastschriftbetrag muss positiv sein.")

                # Kumulative Rückbuchungsgrenze der Ursprungszahlung: nie mehr
                # zurückbuchen als ursprünglich bezahlt wurde, auch nicht über
                # mehrere Teil-Rücklastschriften hinweg.
                bereits_zurueckgebucht = self._repository.kumulativ_zurueckgebucht(
                    original_op_position_id, session=session
                )
                verbleibende_ruecklastgrenze = ursprungsbetrag - bereits_zurueckgebucht
                if effektiver_betrag > verbleibende_ruecklastgrenze:
                    raise ZuordnungUngueltigError(
                        f"Rücklastschriftbetrag {effektiver_betrag} überschreitet die verbleibende "
                        f"Rückbuchungsgrenze der Ursprungszahlung {original_op_position_id} "
                        f"({verbleibende_ruecklastgrenze} von {ursprungsbetrag}, bereits "
                        f"{bereits_zurueckgebucht} zurückgebucht)."
                    )

                # Verfügbarer Belastungsbetrag DIESER Transaktion: eine
                # Sammel-Rücklastschrift kann mehrere Ursprungszahlungen
                # abdecken, aber nie mehr als ihren eigenen (negativen) Betrag.
                bereits_verwendet = self._repository.verwendeter_betrag_rueckbuchung(
                    transaktion_id, session=session
                )
                verfuegbar_auf_transaktion = abs(frische_transaktion.betrag_cent) - bereits_verwendet
                if effektiver_betrag > verfuegbar_auf_transaktion:
                    raise ZuordnungUngueltigError(
                        f"Rücklastschriftbetrag {effektiver_betrag} überschreitet den verfügbaren "
                        f"Belastungsbetrag der Transaktion {transaktion_id} ({verfuegbar_auf_transaktion} von "
                        f"{abs(frische_transaktion.betrag_cent)}, bereits {bereits_verwendet} verwendet)."
                    )

                op_row = self._op_service.buchen(
                    ctx=ctx,
                    konto=frisches_konto,
                    typ=OPTyp.RUECKLASTSCHRIFT,
                    betrag_cent=effektiver_betrag,
                    belegdatum=frische_transaktion.buchungsdatum,
                    buchungsdatum=frische_transaktion.buchungsdatum,
                    faelligkeit=frische_transaktion.buchungsdatum,
                    beleg_referenz=f"Rücklastschrift zu OP #{original_op_position_id}",
                    import_id=f"RUECKLASTSCHRIFT-{transaktion_id}-{original_op_position_id}",
                    quelle_system="bank_ruecklastschrift",
                    bank_transaktion_id=transaktion_id,
                    bezieht_sich_auf_id=original_op_position_id,
                    session=session,
                )
                session.commit()
            except Exception:
                session.rollback()
                raise
            session.refresh(op_row)
            return op_row

    # -- Bankvollständigkeit -------------------------------------------------
    def bankstand_alter_tage(self, bank_konto_id: str, *, heute: date | None = None) -> int | None:
        """Nur ein Hinweis (Datum der letzten importierten Zeile), KEIN
        Vollständigkeitsnachweis - ein unvollständiger Import kann trotzdem
        ein aktuelles Datum zeigen. Für Mahnentscheidungen ist
        `letzte_bankvollstaendigkeit`/`bestaetige_bankvollstaendigkeit`
        maßgeblich, nicht diese Methode."""

        letztes = self._repository.letztes_buchungsdatum(bank_konto_id)
        if letztes is None:
            return None
        return ((heute or date.today()) - letztes).days

    def bestaetige_bankvollstaendigkeit(self, *, bank_konto_id: str, bestaetigt_bis: date, bestaetigt_von: str) -> None:
        """Explizite menschliche/prozessuale Bestätigung "der Import für
        dieses Bankkonto ist lückenlos bis einschließlich `bestaetigt_bis`".
        Erst DAS darf das Mahnwesen als Beleg für Bankvollständigkeit
        akzeptieren, nicht das bloße Vorhandensein irgendeiner Zeile."""

        self._repository.bestaetige_bankvollstaendigkeit(
            bank_konto_id=bank_konto_id, bestaetigt_bis=bestaetigt_bis, bestaetigt_von=bestaetigt_von
        )

    def bankvollstaendigkeit_bestaetigt_bis(self, bank_konto_id: str) -> date | None:
        return self._repository.letzte_bankvollstaendigkeit(bank_konto_id)

    def hat_ungeklaerte_relevante_eingaenge(self, *, bank_konto_id: str, vertrag_id: str) -> bool:
        return self._repository.hat_ungeklaerte_relevante_eingaenge(bank_konto_id=bank_konto_id, vertrag_id=vertrag_id)
