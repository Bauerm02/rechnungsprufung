"""Rein lesender Vorschau-Adapter für den George-Business-CSV-Export.

WICHTIG — Abgrenzung zu `bank/importer.py` und `bank/service.py`:

- Dieses Modul ist bewusst NICHT mit dem bestehenden Bankimport
  verdrahtet. Es liest keine Datenbank, schreibt keine Datenbank, ordnet
  keine Zahlungen einem Vertrag zu und löst keinen Mahnlauf/Versand aus.
  Es ist ein reiner Format-/Struktur-Prüfschritt für einen echten
  George-Business-CSV-Export, gedacht als Vorstufe VOR einem eventuellen
  späteren, eigenen Import-Baustein.
- Das bestehende konfigurierbare CSV-Format (`bank/importer.py::parse_csv`,
  `CsvSpaltenMapping`) bleibt unverändert und unberührt. Wer schreibende
  Bankimporte braucht, verwendet weiterhin `bank/service.py`.
- `Zahlungsreferenz`/`Buchungs-Details`/`Auftraggeber-Referenz` werden
  ausschließlich als Anzeige-/Audit-Text behandelt, NIEMALS als
  Anweisung interpretiert oder ausgeführt. Es gibt hier keine
  VERTRAG:<id>-Erkennung, keine automatische Mieterlös-/Mieterkonto-
  Ableitung aus Gutschriften und keine Umbuchungs-/Darlehens-/
  Drittzahler-Logik — jede Zuordnung zu einem Vertrag/Mieter bleibt ein
  separater, bestätigter Schritt außerhalb dieses Moduls.

Format (reale Spaltennamen, siehe `importtemplates/README.md`):
UTF-8 mit optionalem BOM, Komma als Trennzeichen, gequotete Felder,
Beträge in österreichischer Notation (Punkt=Tausender, Komma=Dezimal,
genau zwei Nachkommastellen), Buchungsdatum als TT.MM.JJJJ, nur EUR.

Sammel-Summenzeilen (kritischer Punkt, nach unabhängiger Codeprüfung
korrigiert): Der Export kann sowohl Sammelüberweisungs-Summenzeilen als
auch deren Einzelposten enthalten. Die sichtbare Spalte "(Sammel-)
Überweisung ID" ist dabei KEIN zuverlässiger Gruppenschlüssel — die
Summenzeile trägt laut Fachprüfung dieselbe ID wie NUR der erste
Einzelposten; die übrigen Detailzeilen tragen jeweils EIGENE
(Sammel-) Überweisung IDs. Weder diese Spalte noch die Buchungsreferenz
sind daher allein ein eindeutiger Transaktions-/Gruppenschlüssel.

Stattdessen wird eine Sammelgruppe über (Eigene IBAN, Währung,
Buchungsdatum, eine ECHTE/brauchbare Buchungsreferenz) zusammengeführt
und NUR dann als vollständig aufgelöst behandelt, wenn zusätzlich:

1. jede beteiligte Zeile über "Enthaltene Überweisung ID" eindeutig als
   Detail (D) oder Summe (S) erkennbar ist (siehe `_sammel_id_analyse` -
   enges, strukturell bestätigtes 107-Zeichen-Profil, siehe unten),
2. alle beteiligten Zeilen denselben eingebetteten 9-stelligen
   "Gruppenpräfix" tragen (Konsistenzprüfung, siehe unten),
3. die Summenzeile eine "(Sammel-) Überweisung ID" trägt, die mit der
   ID MINDESTENS einer Detailzeile übereinstimmt (spiegelt "dieselbe ID
   wie der erste Einzelposten" ab, ohne eine Zeilenreihenfolge
   vorauszusetzen — Summen können vor oder nach den Details stehen),
4. die Detailbeträge sich centgenau exakt auf die Summenzeile addieren.

Jede Abweichung (fehlende Gegenstücke, uneindeutige Markierung,
inkonsistentes Gruppenpräfix, abweichende Summe, eine isolierte S- oder
D-Zeile ohne vollständige Gegengruppe) führt NICHT zu einer geratenen
Klassifizierung, sondern zu einem Prüffall für die GESAMTE betroffene
Gruppe.

Enthaltene-Überweisung-ID-Schema (S/D), aus einer unabhängigen
Strukturprüfung bestätigter LÄNGEN/AUFBAU, aber ohne echte Produktions-
IDs verifiziert — siehe `docs/hausverwaltung/OFFENE_PUNKTE.md`:

    [Eigene IBAN, 20 Zeichen][14 Nullen][Währung, 3 Zeichen]
    [numerisches Präfix, 9 Ziffern][Jahr, 4 Ziffern][Marker S/D]
    [Hex-Hash, 56 Zeichen]                                    = 107 Zeichen

Das 9-stellige Präfix wird NICHT als Datum oder sonstiger Fachwert
interpretiert, sondern ausschließlich als opaker Konsistenzwert
innerhalb einer Gruppe verglichen.

WICHTIG (nach unabhängiger Gegenprobe korrigiert): eine ID, die zwar
lang genug ist, um plausibel ein VERSUCH dieses 107-Zeichen-Profils zu
sein, aber inhaltlich davon abweicht (falsches Konto/Währung/Padding/
Jahr/Markerzeichen/Hex-Suffix, abgeschnittener Suffix), wird NIEMALS
stillschweigend wie ein gewöhnlicher, andersartiger Einzelumsatz
behandelt — das wäre ein kaputtes/inkonsistentes Sammelprofil, kein
normaler Einzelumsatz, und wird als Prüffall ausgewiesen (siehe
`_sieht_wie_sammel_id_versuch_aus`). Ein solches Mitglied "poisoned"
außerdem jede Sammelgruppe mit gleichem (Konto, Währung, Datum,
Buchungsreferenz): die übrigen, für sich genommen sauber aussehenden
S/D-Mitglieder werden NICHT auf Basis der dadurch verkleinerten Gruppe
validiert. Nur eine ID, die für dieses Profil erkennbar zu KURZ ist, gilt
als andersartiges, unabhängiges Einzelumsatz-Format und wird normal
(mit eigener Dublettenprüfung) behandelt.

Unabhängige Identität (nach unabhängiger Gegenprobe ergänzt): eine Zeile
ohne S/D-Sammelmarkierung braucht trotzdem MINDESTENS eine brauchbare
Kennung - "Enthaltene Überweisung ID" ODER "(Sammel-) Überweisung ID" -
um automatisch Kandidat zu werden. Sind BEIDE leer/NOTPROVIDED, wird die
Zeile ein Prüffall, unabhängig davon, ob die Datei eine oder mehrere
Zeilen enthält; eine Buchungsreferenz allein zählt NICHT als eindeutiger
Schlüssel (siehe `_hat_unabhaengige_identitaet`).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from mietinkasso.domain.exceptions import MietinkassoError
from mietinkasso.domain.money import to_cents

#: Exakte Spaltenmenge des echten George-Business-CSV-Exports. Eine
#: abweichende Kopfzeile (fehlende ODER zusätzliche Spalte) wird als
#: falsches/verändertes Format abgelehnt statt spekulativ mit
#: Teildaten weiterzuarbeiten.
ERWARTETE_SPALTEN = (
    "(Sammel-) Überweisung ID",
    "Enthaltene Überweisung ID",
    "Eigene IBAN",
    "Eigener Kontoname",
    "Buchungsdatum",
    "Durchführungsdatum",
    "Durchführungszeit",
    "Kontoauszug / Rechnung",
    "Partner Name",
    "Partner IBAN",
    "Partner BIC",
    "Partner Kontonummer",
    "Partner Bankleitzahl",
    "Betrag",
    "Währung",
    "Buchungs-Details",
    "Buchungsreferenz",
    "Valutadatum",
    "Zahlungsreferenz",
    "Auftraggeber-Referenz",
)

_UNTERSTUETZTE_WAEHRUNG = "EUR"
_MAX_BETRAG_CENT = 2**63 - 1
#: Ziffern VOR dem Komma - deutlich unter der Decimal-Standardpräzision
#: (28 signifikante Stellen), damit ein absichtlich überlanger Betrag
#: kontrolliert abgelehnt wird statt eine unbehandelte
#: decimal.InvalidOperation auszulösen.
_MAX_BETRAG_ZIFFERN_VOR_KOMMA = 17

_OES_BETRAG_MUSTER = re.compile(r"^[+-]?(?:[0-9]{1,3}(?:\.[0-9]{3})*|[0-9]+),[0-9]{2}$")
_DATUM_MUSTER = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")

#: Beobachtetes, unabhängig strukturell bestätigtes Sammel-S/D-ID-Profil
#: (siehe Moduldocstring). NICHT als Kundendatum interpretieren.
_SD_IBAN_LAENGE = 20
_SD_PADDING = "0" * 14
_SD_WAEHRUNG_LAENGE = 3
_SD_PRAEFIX_LAENGE = 9
_SD_JAHR_LAENGE = 4
_SD_HASH_LAENGE = 56
_SD_MARKER_POSITION = _SD_IBAN_LAENGE + len(_SD_PADDING) + _SD_WAEHRUNG_LAENGE + _SD_PRAEFIX_LAENGE + _SD_JAHR_LAENGE
_SD_GESAMTLAENGE = _SD_MARKER_POSITION + 1 + _SD_HASH_LAENGE
_HEX_MUSTER = re.compile(r"^[0-9A-F]+$")
#: Längenbasiertes Shape-Signal, UNABHÄNGIG davon ob einzelne Segmente
#: (Konto/Währung/Padding/Jahr/Hex) tatsächlich korrekt sind: Eine ID,
#: die lang genug ist, um den vollen Kopfbereich des 107-Zeichen-Profils
#: (bis inkl. Markerposition) zu tragen, gilt als VERSUCH dieses
#: Profils - selbst wenn Konto/Währung/Padding/Jahr/Marker/Hash-Suffix
#: falsch oder abgeschnitten sind. So ein Versuch wird NIE stillschweigend
#: wie ein gewöhnlicher (kurzer, andersartiger) Einzelumsatz behandelt,
#: sondern als Prüffall ausgewiesen (siehe `_INKONSISTENTES_SAMMELPROFIL_GRUND`).
_SD_MINDESTLAENGE_FUER_PROFILVERSUCH = _SD_MARKER_POSITION + 1

#: Werte, die NIEMALS als eindeutiger Schlüssel (Dublettenprüfung,
#: Sammelgruppen-Referenz) verwendet werden dürfen - eine leere Spalte
#: oder der Platzhalter "NOTPROVIDED" bedeuten "keine brauchbare ID",
#: nicht "diese Zeilen gehören zusammen".
_UNBRAUCHBARE_SCHLUESSELWERTE = {"", "NOTPROVIDED"}

#: Fixer Hinweis, der in jeder Vorschau unverändert mitgeliefert wird —
#: kein berechneter Wert, sondern eine bewusste Warnung gegen eine
#: naheliegende Fehlschlussfigur (siehe RAHMENPROGRAMM/OFFENE_PUNKTE:
#: "Bankvollständigkeit ist eine explizite Bestätigung, kein
#: Datumsschluss").
WARNUNG_BANKVOLLSTAENDIGKEIT = (
    "Das jüngste Buchungsdatum in dieser Datei ist KEIN Beweis für eine "
    "vollständige Bankanbindung bis zum Abrufdatum. Diese Vorschau prüft "
    "nur die vorliegende Datei, nicht ob dazwischen Buchungen fehlen."
)


class GeorgeFormatFehlerError(MietinkassoError):
    """Die Datei entspricht strukturell nicht dem erwarteten
    George-Business-CSV-Export (falscher Dateityp, unlesbare Kodierung,
    leere/doppelte/abweichende Kopfzeile, defekte CSV-Struktur). Wird
    abgelehnt statt spekulativ als CSV mit Lücken weiterverarbeitet."""


@dataclass(frozen=True)
class GeorgeKandidat:
    """Ein normalisierter, geprüfter Buchungskandidat (Einzelumsatz oder
    validierter Sammel-Detailposten). Noch keine Buchung, keine
    Vertrags-/Mieterzuordnung — reine Vorschau. Jeder Kandidat liegt auf
    dem erwarteten Konto UND im erwarteten Zeitraum - andernfalls ist er
    kein Kandidat, sondern ABGELEHNT/PRUEFFALL (siehe `erstelle_preview`).

    `kandidaten_id` ist eine vom Inhalt (nicht von der Position in der
    Datei) abgeleitete, stabile Kennung - bei geänderter Zeilenreihenfolge
    in einer sonst inhaltsgleichen Datei bleibt sie unverändert."""

    kandidaten_id: str
    zeile_nr: int
    eigene_iban: str
    eigener_kontoname: str
    buchungsdatum: date
    valuta: date | None
    betrag_cent: int
    waehrung: str
    partner_name: str | None
    partner_iban: str | None
    partner_bic: str | None
    buchungsreferenz: str | None
    zahlungsreferenz: str | None
    auftraggeber_referenz: str | None
    sammel_id: str | None
    enthaltene_id: str | None


@dataclass(frozen=True)
class GeorgeZeilenErgebnis:
    """Ergebnis genau einer Datenzeile — jede Zeile der Datei taucht hier
    genau einmal auf, unabhängig davon ob sie ein Kandidat, eine erkannte
    Summenzeile, ein Prüffall oder abgelehnt ist. Nichts wird still
    weggelassen.

    `zeile_nr` ist der logische Datenindex (1-basiert, ohne Kopfzeile und
    ohne vollständig leere Zeilen) - `csv_zeile` ist davon bewusst
    getrennt die PHYSISCHE Zeilennummer, an der der Datensatz laut
    `csv.reader.line_num` ENDET (bei einem mehrzeiligen gequoteten Feld
    also die letzte konsumierte physische Zeile, nicht die erste - in
    diesem Format nicht erwartet, aber nicht stillschweigend falsch
    benannt).

    `felder` sind die EXAKTEN dekodierten CSV-Feldwerte der Originaldatei
    (kein `.strip()`, keine sonstige Normalisierung) - `sha256_zeile` ist
    darüber via kanonischem JSON gebildet. Bei einer strukturell kaputten
    Zeile (Status ABGELEHNT wegen abweichender Feldanzahl) ist `felder`
    stattdessen eine bestmögliche, NICHT notwendigerweise bytegenaue
    Rekonstruktion (siehe `_sichere_felder`) - die Originaldatei bleibt
    in diesem Fall maßgeblich.

    status:
      - "KANDIDAT": normalisierter, buchbarer Vorschau-Kandidat - liegt
        auf dem erwarteten Konto und im erwarteten Zeitraum.
      - "SAMMEL_SUMME": strukturell eindeutig als Sammel-Summenzeile
        erkannt (Detailsumme stimmt exakt); bewusst kein Kandidat.
      - "PRUEFFALL": Sammelgruppe/Dublette/Zeitraum nicht eindeutig oder
        nicht im erwarteten Rahmen; erfordert manuelle Klärung, wird
        NICHT automatisch normalisiert.
      - "ABGELEHNT": Zeile technisch ungültig (Format-/Wertefehler) oder
        gehört strukturell nicht zum erwarteten Konto.
    """

    zeile_nr: int
    csv_zeile: int
    status: str
    grund: str | None
    sha256_zeile: str
    felder: dict[str, str]
    kandidat: GeorgeKandidat | None = None


@dataclass(frozen=True)
class SaldoKontrolle:
    anfangssaldo_cent: int
    endsaldo_cent: int
    berechneter_endsaldo_cent: int
    stimmt_ueberein: bool


@dataclass(frozen=True)
class GeorgeBusinessPreview:
    datei_sha256: str
    erwartetes_konto_iban: str
    von: date
    bis: date
    zeilen: tuple[GeorgeZeilenErgebnis, ...]
    eingaenge_cent: int
    ausgaenge_cent: int
    saldo_kontrolle: SaldoKontrolle | None
    warnungen: tuple[str, ...] = field(default_factory=lambda: (WARNUNG_BANKVOLLSTAENDIGKEIT,))

    @property
    def kandidaten(self) -> list[GeorgeZeilenErgebnis]:
        return [z for z in self.zeilen if z.status == "KANDIDAT"]

    @property
    def sammel_summen(self) -> list[GeorgeZeilenErgebnis]:
        return [z for z in self.zeilen if z.status == "SAMMEL_SUMME"]

    @property
    def pruefffaelle(self) -> list[GeorgeZeilenErgebnis]:
        return [z for z in self.zeilen if z.status == "PRUEFFALL"]

    @property
    def abgelehnt(self) -> list[GeorgeZeilenErgebnis]:
        return [z for z in self.zeilen if z.status == "ABGELEHNT"]

    @property
    def vollstaendig(self) -> bool:
        """False, sobald IRGENDETWAS eine manuelle Klärung braucht:
        Prüffälle, abgelehnte Zeilen, oder eine geprüfte Saldenkontrolle,
        die nicht aufgeht. Ein rein rechnerisch aufgehender Saldo bei
        gleichzeitig vorhandenen Prüffällen/Ablehnungen gilt NICHT als
        vollständig - das wäre ein Erfolgssignal auf Basis fehlender
        statt geprüfter Zeilen."""

        if self.pruefffaelle or self.abgelehnt:
            return False
        if self.saldo_kontrolle is not None and not self.saldo_kontrolle.stimmt_ueberein:
            return False
        return True


@dataclass(frozen=True)
class _ZeilenKontext:
    zeile_nr: int
    csv_zeile: int
    werte: dict[str, str]
    rohfelder: dict[str, str]
    eigene_iban: str
    betrag_cent: int
    waehrung: str
    buchungsdatum: date
    valuta: date | None
    feld_hash: str


@dataclass(frozen=True)
class _GruppenSchluessel:
    eigene_iban: str
    waehrung: str
    buchungsdatum: date
    buchungsreferenz: str


def _sha256_bytes(rohbytes: bytes) -> str:
    return hashlib.sha256(rohbytes).hexdigest()


def _sha256_felder(felder: dict[str, str]) -> str:
    # Kanonisches JSON statt einer selbstgebauten "key=value,key=value"-
    # Verkettung: Ein Feldwert, der selbst ein Komma oder "Spalte=" enthält
    # (z. B. Partner Name="alpha,Partner IBAN=beta"), kollidierte bei der
    # alten Verkettung mit einer völlig anderen Zeile (Partner Name="alpha",
    # Partner IBAN="beta,Partner IBAN=gamma"). json.dumps escaped jeden
    # Feldwert eindeutig, sort_keys macht das Ergebnis ordnungsunabhängig.
    kanonisch = json.dumps(felder, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(kanonisch.encode("utf-8")).hexdigest()


def _sichere_felder(rohzeile: dict) -> dict[str, str]:
    """Baut aus einer (ggf. strukturell kaputten) csv.DictReader-Zeile
    defensiv einen reinen str->str-Dict für Audit-/Fehlerzwecke auf -
    NIE für die eigentliche fachliche Auswertung. `None` (fehlendes Feld)
    wird zu "", eine Liste (zu viele Felder, restkey=None) wird zu einem
    Anzeige-String; nirgends wird `.strip()`/`.upper()` auf einem
    Nicht-String aufgerufen (das war die AttributeError-Quelle).

    Bei einer strukturell kaputten Zeile (abweichende Feldanzahl) ist
    dieses Ergebnis eine BESTMÖGLICHE, aber NICHT notwendigerweise
    bytegenaue Rekonstruktion - insbesondere die zu einer Liste
    zusammengefassten Überschussfelder sind eine Anzeigehilfe, keine
    Behauptung vollständiger Originaltreue. Bei Zweifeln bleibt die
    Originaldatei die maßgebliche Quelle, nicht dieser Rückgabewert."""

    ergebnis: dict[str, str] = {}
    for spalte in ERWARTETE_SPALTEN:
        wert = rohzeile.get(spalte)
        if wert is None:
            ergebnis[spalte] = ""
        elif isinstance(wert, list):
            ergebnis[spalte] = ",".join(str(v) for v in wert)
        else:
            ergebnis[spalte] = str(wert)
    return ergebnis


def _ist_brauchbarer_schluessel(text: str) -> bool:
    return text.strip().upper() not in _UNBRAUCHBARE_SCHLUESSELWERTE


def _hat_unabhaengige_identitaet(kontext: _ZeilenKontext) -> bool:
    """Eine Zeile braucht MINDESTENS eine unabhängige Kennung - eine
    brauchbare 'Enthaltene Überweisung ID' ODER eine brauchbare
    '(Sammel-) Überweisung ID' - um automatisch Kandidat zu werden.
    Eine Buchungsreferenz allein zählt NICHT (siehe
    `_KEINE_UNABHAENGIGE_IDENTITAET_GRUND`); das gilt unabhängig davon,
    ob die Datei eine oder mehrere Zeilen enthält."""

    return _ist_brauchbarer_schluessel(kontext.werte["Enthaltene Überweisung ID"]) or _ist_brauchbarer_schluessel(
        kontext.werte["(Sammel-) Überweisung ID"]
    )


def _pruefe_kein_xlsx(rohbytes: bytes) -> None:
    # XLSX-Dateien (und jedes andere ZIP-Container-Format) beginnen mit
    # der ZIP-Signatur. Eine .xlsx-Datei "irrtümlich" als Text/CSV zu
    # dekodieren würde Binärmüll oder eine leere/kaputte Kopfzeile
    # liefern statt eines klaren Fehlers — deshalb explizit vorab prüfen.
    if rohbytes[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        raise GeorgeFormatFehlerError(
            "Diese Datei ist eine XLSX-/ZIP-Datei, kein CSV. Dieser Adapter liest ausschließlich den "
            "George-Business-CSV-Export; bitte als CSV exportieren."
        )


def _dekodiere(rohbytes: bytes) -> str:
    try:
        return rohbytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise GeorgeFormatFehlerError(
            "Datei ist nicht als UTF-8 (mit oder ohne BOM) lesbar; kein gültiger George-Business-CSV-Export."
        ) from exc


def _lese_csv_zeilen(text: str) -> list[tuple[int, int, dict[str, str], str | None]]:
    """Liest die Datenzeilen roh ein. Rückgabe je Zeile:
    (zeile_nr, csv_zeile, rohfelder, strukturfehler_oder_None).

    `rohfelder` enthält die EXAKTEN dekodierten CSV-Feldwerte, INSBESONDERE
    OHNE `.strip()` - führende/nachfolgende Leerzeichen im Original bleiben
    erhalten, damit `felder`/`sha256_zeile` im Ergebnis die Originaldatei
    unverändert abbilden. Normalisierung (z. B. für Konto-/Datums-/
    Betragsvergleiche) passiert getrennt und NUR für die fachliche
    Auswertung, siehe `_validiere_basis`.

    `csv_zeile` ist die physische Zeilennummer, an der der Datensatz laut
    `csv.reader.line_num` ENDET - bei einem (in diesem Format nicht
    erwarteten) mehrzeiligen gequoteten Feld also die LETZTE konsumierte
    physische Zeile, nicht die erste.

    Wenn `strukturfehler_oder_None` gesetzt ist, ist die Zeile technisch
    kaputt (abweichende Feldanzahl) - `rohfelder` enthält dann trotzdem
    eine defensiv aufgebaute, NIE crashende Bestenfalls-Rekonstruktion für
    den Audit-Trail (siehe `_sichere_felder`), wird aber nicht fachlich
    weiterverarbeitet; bei Zweifeln bleibt die Originaldatei maßgeblich."""

    try:
        reader = csv.DictReader(io.StringIO(text), restval=None, strict=True)
        spalten_liste = list(reader.fieldnames or [])
    except csv.Error as exc:
        raise GeorgeFormatFehlerError(f"Kopfzeile nicht lesbar (CSV-Struktur defekt): {exc}") from exc

    if not spalten_liste:
        raise GeorgeFormatFehlerError("Datei ist leer oder enthält keine lesbare Kopfzeile.")
    if len(spalten_liste) != len(set(spalten_liste)):
        duplikate = sorted({s for s in spalten_liste if spalten_liste.count(s) > 1})
        raise GeorgeFormatFehlerError(f"Kopfzeile enthält doppelte Spalten: {duplikate}.")
    if set(spalten_liste) != set(ERWARTETE_SPALTEN):
        fehlend = sorted(set(ERWARTETE_SPALTEN) - set(spalten_liste))
        zusaetzlich = sorted(set(spalten_liste) - set(ERWARTETE_SPALTEN))
        raise GeorgeFormatFehlerError(
            "Kopfzeile entspricht nicht dem erwarteten George-Business-CSV-Format. "
            f"Fehlende Spalten: {fehlend or '-'}; unerwartete Spalten: {zusaetzlich or '-'}."
        )

    ergebnis: list[tuple[int, int, dict[str, str], str | None]] = []
    zeile_nr = 0
    try:
        for rohzeile in reader:
            csv_zeile = reader.line_num
            hat_ueberzaehlige_felder = None in rohzeile
            werte_ohne_restkey = [wert for schluessel, wert in rohzeile.items() if schluessel is not None]

            if not hat_ueberzaehlige_felder and all(wert is None for wert in werte_ohne_restkey):
                # Vollständig leere physische Zeile (z. B. eine
                # abschließende Leerzeile durch \r\n\r\n am Dateiende) -
                # kein Datensatz, kein Fehler.
                continue

            zeile_nr += 1
            if hat_ueberzaehlige_felder:
                ergebnis.append((
                    zeile_nr, csv_zeile, _sichere_felder(rohzeile),
                    "Zeile hat mehr Felder als Spalten in der Kopfzeile; wird nicht stillschweigend gekürzt.",
                ))
                continue
            if any(wert is None for wert in werte_ohne_restkey):
                ergebnis.append((
                    zeile_nr, csv_zeile, _sichere_felder(rohzeile),
                    "Zeile hat weniger Felder als Spalten in der Kopfzeile; wird nicht stillschweigend aufgefüllt.",
                ))
                continue

            rohfelder = {spalte: rohzeile[spalte] for spalte in ERWARTETE_SPALTEN}
            ergebnis.append((zeile_nr, csv_zeile, rohfelder, None))
    except csv.Error as exc:
        raise GeorgeFormatFehlerError(f"CSV-Struktur nicht lesbar (z. B. defekte Anführungszeichen): {exc}") from exc
    return ergebnis


def parse_oesterreichischen_betrag(text: str) -> int:
    if not _OES_BETRAG_MUSTER.fullmatch(text):
        raise ValueError(
            f"Betrag '{text}' ist nicht im erwarteten österreichischen Format (z. B. 1.234,56 oder "
            "-123,45 mit genau zwei Nachkommastellen)."
        )
    normalisiert = text.replace(".", "").replace(",", ".")
    ganzzahl_teil = normalisiert.lstrip("+-").split(".", 1)[0]
    if len(ganzzahl_teil) > _MAX_BETRAG_ZIFFERN_VOR_KOMMA:
        # Muss VOR jeder Decimal-Operation geprüft werden: die
        # Standard-Decimal-Kontextpräzision (28 signifikante Stellen)
        # würde bei einer absichtlich überlangen Zahl (z. B. 100 Ziffern)
        # eine unbehandelte decimal.InvalidOperation auslösen statt einer
        # kontrollierten Ablehnung.
        raise ValueError(f"Betrag '{text}' überschreitet den zulässigen Speicherbereich.")
    try:
        cent = to_cents(Decimal(normalisiert))
    except (InvalidOperation, ArithmeticError) as exc:
        raise ValueError(f"Betrag '{text}' ist kein gültiger Geldbetrag.") from exc
    if abs(cent) > _MAX_BETRAG_CENT:
        raise ValueError(f"Betrag '{text}' überschreitet den zulässigen Speicherbereich.")
    return cent


def _parse_datum(text: str, feld: str) -> date:
    if not _DATUM_MUSTER.fullmatch(text):
        raise ValueError(f"{feld} '{text}' entspricht nicht dem erwarteten Format TT.MM.JJJJ.")
    try:
        return datetime.strptime(text, "%d.%m.%Y").date()
    except ValueError as exc:
        raise ValueError(f"{feld} '{text}' ist kein gültiges Kalenderdatum.") from exc


def _sammel_id_analyse(
    enthaltene_id: str,
    *,
    iban: str,
    waehrung: str,
    buchungsdatum: date,
) -> tuple[str, str] | None:
    """Liefert `(marker, gruppenpraefix)` NUR wenn `enthaltene_id` exakt
    dem bestätigten 107-Zeichen-Profil entspricht (siehe Moduldocstring)
    UND Konto/Währung/Jahr darin mit der eigenen Zeile übereinstimmen.
    Sonst None - eine andersartige ID (normale Einzelumsätze haben ein
    anderes Format) wird NIE geraten als S/D interpretiert."""

    if len(enthaltene_id) != _SD_GESAMTLAENGE:
        return None

    iban_teil = enthaltene_id[:_SD_IBAN_LAENGE]
    padding_start = _SD_IBAN_LAENGE
    padding_teil = enthaltene_id[padding_start:padding_start + len(_SD_PADDING)]
    waehrung_start = padding_start + len(_SD_PADDING)
    waehrung_teil = enthaltene_id[waehrung_start:waehrung_start + _SD_WAEHRUNG_LAENGE]
    praefix_start = waehrung_start + _SD_WAEHRUNG_LAENGE
    praefix_teil = enthaltene_id[praefix_start:praefix_start + _SD_PRAEFIX_LAENGE]
    jahr_teil = enthaltene_id[praefix_start + _SD_PRAEFIX_LAENGE:_SD_MARKER_POSITION]
    marker = enthaltene_id[_SD_MARKER_POSITION]
    hash_teil = enthaltene_id[_SD_MARKER_POSITION + 1:]

    eigene_iban_kern = re.sub(r"\s", "", iban).upper()
    if len(eigene_iban_kern) != _SD_IBAN_LAENGE or iban_teil.upper() != eigene_iban_kern:
        return None
    if padding_teil != _SD_PADDING:
        return None
    if waehrung_teil.upper() != waehrung.upper():
        return None
    if not praefix_teil.isdigit():
        return None
    if jahr_teil != f"{buchungsdatum.year:04d}":
        return None
    if marker not in ("S", "D"):
        return None
    if len(hash_teil) != _SD_HASH_LAENGE or not _HEX_MUSTER.fullmatch(hash_teil.upper()):
        return None
    return marker, praefix_teil


def _sieht_wie_sammel_id_versuch_aus(enthaltene_id: str) -> bool:
    """Rein längenbasiertes Shape-Signal (siehe
    `_SD_MINDESTLAENGE_FUER_PROFILVERSUCH`): unterscheidet "diese ID
    versucht erkennbar das 107-Zeichen-Sammelprofil zu sein, ist aber
    inhaltlich kaputt (falsches Konto/Währung/Padding/Jahr/Hex,
    abgeschnittener Hash-Suffix)" von "dies ist ein andersartiges,
    unabhängiges Einzelumsatz-ID-Format". NUR für Werte gedacht, für die
    `_sammel_id_analyse` bereits None geliefert hat."""

    return len(enthaltene_id) >= _SD_MINDESTLAENGE_FUER_PROFILVERSUCH


def _validiere_basis(zeile_nr: int, csv_zeile: int, rohfelder: dict[str, str]) -> _ZeilenKontext:
    # `werte` ist eine für die fachliche Auswertung normalisierte Kopie
    # (getrimmt) - `rohfelder` bleibt für Audit/Hash unverändert (siehe
    # `_lese_csv_zeilen`).
    werte = {spalte: rohfelder[spalte].strip() for spalte in ERWARTETE_SPALTEN}
    if not werte["Eigene IBAN"]:
        raise ValueError("Eigene IBAN fehlt.")
    if not werte["Betrag"]:
        raise ValueError("Betrag fehlt.")
    if not werte["Buchungsdatum"]:
        raise ValueError("Buchungsdatum fehlt.")
    if not werte["Währung"]:
        raise ValueError("Währung fehlt.")
    if werte["Währung"] != _UNTERSTUETZTE_WAEHRUNG:
        raise ValueError(f"Nicht unterstützte Währung '{werte['Währung']}'; nur {_UNTERSTUETZTE_WAEHRUNG} wird geprüft.")

    betrag_cent = parse_oesterreichischen_betrag(werte["Betrag"])
    buchungsdatum = _parse_datum(werte["Buchungsdatum"], "Buchungsdatum")
    valuta = _parse_datum(werte["Valutadatum"], "Valutadatum") if werte["Valutadatum"] else None

    return _ZeilenKontext(
        zeile_nr=zeile_nr,
        csv_zeile=csv_zeile,
        werte=werte,
        rohfelder=rohfelder,
        eigene_iban=werte["Eigene IBAN"],
        betrag_cent=betrag_cent,
        waehrung=werte["Währung"],
        buchungsdatum=buchungsdatum,
        valuta=valuta,
        feld_hash=_sha256_felder(rohfelder),
    )


def _optional(wert: str) -> str | None:
    return wert or None


def _baue_kandidat(kontext: _ZeilenKontext) -> GeorgeKandidat:
    w = kontext.werte
    return GeorgeKandidat(
        kandidaten_id=kontext.feld_hash,
        zeile_nr=kontext.zeile_nr,
        eigene_iban=w["Eigene IBAN"],
        eigener_kontoname=w["Eigener Kontoname"],
        buchungsdatum=kontext.buchungsdatum,
        valuta=kontext.valuta,
        betrag_cent=kontext.betrag_cent,
        waehrung=kontext.waehrung,
        partner_name=_optional(w["Partner Name"]),
        partner_iban=_optional(w["Partner IBAN"]),
        partner_bic=_optional(w["Partner BIC"]),
        buchungsreferenz=_optional(w["Buchungsreferenz"]),
        zahlungsreferenz=_optional(w["Zahlungsreferenz"]),
        auftraggeber_referenz=_optional(w["Auftraggeber-Referenz"]),
        sammel_id=_optional(w["(Sammel-) Überweisung ID"]),
        enthaltene_id=_optional(w["Enthaltene Überweisung ID"]),
    )


def _ergebnis_kandidat(kontext: _ZeilenKontext) -> GeorgeZeilenErgebnis:
    return GeorgeZeilenErgebnis(
        zeile_nr=kontext.zeile_nr,
        csv_zeile=kontext.csv_zeile,
        status="KANDIDAT",
        grund=None,
        sha256_zeile=kontext.feld_hash,
        felder=kontext.rohfelder,
        kandidat=_baue_kandidat(kontext),
    )


def _ergebnis_summenzeile(kontext: _ZeilenKontext, grund: str) -> GeorgeZeilenErgebnis:
    return GeorgeZeilenErgebnis(
        zeile_nr=kontext.zeile_nr,
        csv_zeile=kontext.csv_zeile,
        status="SAMMEL_SUMME",
        grund=grund,
        sha256_zeile=kontext.feld_hash,
        felder=kontext.rohfelder,
        kandidat=_baue_kandidat(kontext),
    )


def _ergebnis_pruefffall(kontext: _ZeilenKontext, grund: str) -> GeorgeZeilenErgebnis:
    return GeorgeZeilenErgebnis(
        zeile_nr=kontext.zeile_nr,
        csv_zeile=kontext.csv_zeile,
        status="PRUEFFALL",
        grund=grund,
        sha256_zeile=kontext.feld_hash,
        felder=kontext.rohfelder,
        kandidat=None,
    )


def _ergebnis_abgelehnt(zeile_nr: int, csv_zeile: int, rohfelder: dict[str, str], grund: str) -> GeorgeZeilenErgebnis:
    return GeorgeZeilenErgebnis(
        zeile_nr=zeile_nr,
        csv_zeile=csv_zeile,
        status="ABGELEHNT",
        grund=grund,
        sha256_zeile=_sha256_felder(rohfelder),
        felder=rohfelder,
        kandidat=None,
    )


_UNVOLLSTAENDIGE_SAMMELGRUPPE_GRUND = (
    "Sammelgruppe nicht eindeutig auflösbar (S/D-Markierung fehlt/uneindeutig, Gruppenpräfix "
    "inkonsistent, keine gemeinsame '(Sammel-) Überweisung ID' mit einer Detailzeile, oder die "
    "Detailbeträge summieren sich nicht centgenau auf die Summenzeile). Wird NICHT automatisch "
    "normalisiert oder bereinigt, sondern als Prüffall ausgewiesen."
)
_SAMMEL_SUMME_GRUND = (
    "Sammel-Summenzeile: S/D-Markierung, Gruppenpräfix, Sammel-ID-Bezug zu mindestens einer "
    "Detailzeile und centgenaue Detailsumme stimmen exakt überein; strukturell eindeutig als Summe "
    "erkannt und daher kein buchbarer Kandidat."
)
_FEHLENDE_REFERENZ_FUER_SD_GRUND = (
    "Zeile trägt eine gültige S/D-Sammelmarkierung, aber keine brauchbare Buchungsreferenz zur "
    "Gruppenbildung (leer oder NOTPROVIDED); die Sammelgruppe kann nicht validiert werden."
)
_INKONSISTENTES_SAMMELPROFIL_GRUND = (
    "'Enthaltene Überweisung ID' ist lang genug, um ein Versuch des 107-Zeichen-Sammel-S/D-Profils zu "
    "sein, erfüllt es aber nicht exakt (Konto/Währung/Padding/Jahr/Markerzeichen/Hex-Suffix stimmen "
    "nicht oder der Suffix ist abgeschnitten). Wird NIE stillschweigend wie ein gewöhnlicher "
    "Einzelumsatz durchgereicht, sondern als Prüffall ausgewiesen."
)
_KEINE_UNABHAENGIGE_IDENTITAET_GRUND = (
    "Weder 'Enthaltene Überweisung ID' noch '(Sammel-) Überweisung ID' sind brauchbar (leer oder "
    "NOTPROVIDED) - eine Buchungsreferenz allein ist kein eindeutiger Schlüssel. Ohne unabhängig "
    "eindeutige Identität wird die Zeile nicht automatisch zum Kandidaten, sondern als Prüffall "
    "ausgewiesen; das gilt unabhängig davon, ob die Datei eine oder mehrere Zeilen enthält."
)
_GRUPPE_DURCH_AUSSCHLUSS_UNVOLLSTAENDIG_GRUND = (
    "Sammelgruppe (gleiches Konto/Währung/Datum/Buchungsreferenz) enthält ein Mitglied, das separat "
    "als Dublette oder als inkonsistentes S/D-Profil ausgeschlossen wurde - die verbleibenden Zeilen "
    "werden NICHT auf Basis einer dadurch verkleinerten Gruppe validiert, auch wenn ihre Summe für "
    "sich genommen aufginge."
)


def _verarbeite_sd_gruppe(
    mitglieder: list[tuple[_ZeilenKontext, str, str | None]],
    dubletten_grund: dict[int, str],
) -> list[GeorgeZeilenErgebnis]:
    # Reihenfolge in der Datei ist irrelevant (Summen können vor oder
    # nach ihren Details stehen) - die Klassifizierung hängt nur vom
    # Mengeninhalt der Gruppe ab, nie von der Position.
    hat_kaputtes_mitglied = any(marker == "KAPUTT" for _, marker, _ in mitglieder)
    hat_dublette = any(kontext.zeile_nr in dubletten_grund for kontext, _, _ in mitglieder)

    summenzeilen = [(k, p) for k, m, p in mitglieder if m == "S"]
    detailzeilen = [(k, p) for k, m, p in mitglieder if m == "D"]
    praefixe = {p for _, m, p in mitglieder if m in ("S", "D")}

    gueltig = (
        not hat_kaputtes_mitglied
        and not hat_dublette
        and len(summenzeilen) == 1
        and len(detailzeilen) >= 1
        and len(praefixe) == 1
        and sum(k.betrag_cent for k, _ in detailzeilen) == summenzeilen[0][0].betrag_cent
        and _ist_brauchbarer_schluessel(summenzeilen[0][0].werte["(Sammel-) Überweisung ID"])
        and summenzeilen[0][0].werte["(Sammel-) Überweisung ID"]
        in {k.werte["(Sammel-) Überweisung ID"] for k, _ in detailzeilen}
    )
    if not gueltig:
        ergebnisse = []
        for kontext, marker, _ in mitglieder:
            if kontext.zeile_nr in dubletten_grund:
                ergebnisse.append(_ergebnis_pruefffall(kontext, dubletten_grund[kontext.zeile_nr]))
            elif marker == "KAPUTT":
                ergebnisse.append(_ergebnis_pruefffall(kontext, _INKONSISTENTES_SAMMELPROFIL_GRUND))
            elif hat_kaputtes_mitglied or hat_dublette:
                # Ein ANDERES Mitglied dieser Gruppe wurde als Dublette
                # oder kaputtes Profil ausgeschlossen - auch die für sich
                # genommen unauffälligen Mitglieder gelten deshalb als
                # unvollständige Gruppe, nicht als isoliert bewertbar.
                ergebnisse.append(_ergebnis_pruefffall(kontext, _GRUPPE_DURCH_AUSSCHLUSS_UNVOLLSTAENDIG_GRUND))
            else:
                ergebnisse.append(_ergebnis_pruefffall(kontext, _UNVOLLSTAENDIGE_SAMMELGRUPPE_GRUND))
        return ergebnisse

    ergebnisse = [_ergebnis_kandidat(k) for k, _ in detailzeilen]
    ergebnisse.append(_ergebnis_summenzeile(summenzeilen[0][0], _SAMMEL_SUMME_GRUND))
    return ergebnisse


def erstelle_preview(
    rohbytes: bytes,
    *,
    erwartetes_konto_iban: str,
    von: date,
    bis: date,
    anfangssaldo_cent: int | None = None,
    endsaldo_cent: int | None = None,
) -> GeorgeBusinessPreview:
    """Liest einen George-Business-CSV-Export rein lesend und liefert eine
    auditierbare Vorschau. Kein Datenbankzugriff, keine Bankverbindung,
    kein Versand, keine Buchung — reine In-Memory-Verarbeitung der
    übergebenen Bytes.

    `erwartetes_konto_iban`/`von`/`bis` sind explizite Prüfparameter:
    JEDE Zeile wird dagegen geprüft, aber KEINE Zeile verschwindet
    deshalb aus dem Ergebnis. Eine Zeile auf einem anderen Konto wird
    ABGELEHNT (für diese Vorschau nicht relevant); eine Zeile auf dem
    richtigen Konto, aber außerhalb des Zeitraums, wird ein PRUEFFALL
    (könnte ein falscher Zeitraum oder eine echte Überraschung sein) -
    beide werden NIE als KANDIDAT gezählt oder in die Summen
    (`eingaenge_cent`/`ausgaenge_cent`) bzw. die Saldenkontrolle
    einbezogen.
    """

    if bis < von:
        raise ValueError("'bis' darf nicht vor 'von' liegen.")
    if (anfangssaldo_cent is None) != (endsaldo_cent is None):
        raise ValueError(
            "Saldenkontrolle erfordert entweder Anfangs- UND Endsaldo, oder keines von beiden - "
            "ein einzelner Wert reicht nicht für eine geprüfte Kontrolle."
        )

    _pruefe_kein_xlsx(rohbytes)
    text = _dekodiere(rohbytes)
    rohzeilen = _lese_csv_zeilen(text)
    if not rohzeilen:
        # Eine Kopfzeile ohne jede Datenzeile ist keine bestätigte
        # Vollständigkeit ("nichts zu beanstanden") - es gibt schlicht
        # nichts, das geprüft werden konnte. Genau wie eine vollständig
        # leere Datei wird das explizit abgelehnt statt stillschweigend
        # als erfolgreiche (weil leere) Vorschau durchgereicht.
        raise GeorgeFormatFehlerError(
            "Datei enthält keine Datenzeilen (nur Kopfzeile); Vollständigkeit kann nicht bestätigt werden."
        )

    ergebnisse_je_zeile: dict[int, GeorgeZeilenErgebnis] = {}
    geprueft: list[_ZeilenKontext] = []

    for zeile_nr, csv_zeile, rohfelder, strukturfehler in rohzeilen:
        if strukturfehler is not None:
            ergebnisse_je_zeile[zeile_nr] = _ergebnis_abgelehnt(zeile_nr, csv_zeile, rohfelder, strukturfehler)
            continue
        try:
            kontext = _validiere_basis(zeile_nr, csv_zeile, rohfelder)
        except ValueError as exc:
            ergebnisse_je_zeile[zeile_nr] = _ergebnis_abgelehnt(zeile_nr, csv_zeile, rohfelder, str(exc))
            continue

        if kontext.eigene_iban != erwartetes_konto_iban:
            ergebnisse_je_zeile[zeile_nr] = _ergebnis_abgelehnt(
                zeile_nr, csv_zeile, rohfelder,
                f"Eigene IBAN '{kontext.eigene_iban}' weicht vom erwarteten Konto "
                f"'{erwartetes_konto_iban}' ab; für diese Vorschau nicht relevant.",
            )
            continue
        if not (von <= kontext.buchungsdatum <= bis):
            ergebnisse_je_zeile[zeile_nr] = _ergebnis_pruefffall(
                kontext,
                f"Buchungsdatum {kontext.buchungsdatum:%d.%m.%Y} liegt außerhalb des angefragten "
                f"Zeitraums {von:%d.%m.%Y}–{bis:%d.%m.%Y}.",
            )
            continue

        geprueft.append(kontext)

    # --- Dublettenerkennung: liefert nur eine Menge markierter
    # Zeilennummern -> Gründe, entfernt NICHTS aus der Betrachtung für
    # die anschließende Sammelgruppen-Analyse (siehe unten) - eine
    # separat als Dublette erkannte Zeile darf eine verbleibende
    # Sammelgruppe nicht "zufällig" valide erscheinen lassen. ---
    dubletten_grund: dict[int, str] = {}
    nach_enthaltener_id: dict[str, list[_ZeilenKontext]] = {}
    ohne_brauchbare_id: list[_ZeilenKontext] = []
    for kontext in geprueft:
        eid = kontext.werte["Enthaltene Überweisung ID"]
        if _ist_brauchbarer_schluessel(eid):
            nach_enthaltener_id.setdefault(eid, []).append(kontext)
        else:
            ohne_brauchbare_id.append(kontext)
    for eid, gruppe in nach_enthaltener_id.items():
        if len(gruppe) > 1:
            for kontext in gruppe:
                dubletten_grund[kontext.zeile_nr] = (
                    f"Identische 'Enthaltene Überweisung ID' ({eid}) mehrfach auf demselben Konto; "
                    "wird nicht automatisch als getrennte Buchungen normalisiert."
                )
    # Zwei echte, unterscheidbare Zahlungen mit zufällig gleichem Betrag
    # bleiben erhalten, solange sich IRGENDEIN Feld unterscheidet - nur
    # ein vollständig identischer Datensatz (alle Spalten) gilt als
    # ambige Wiederholung.
    nach_inhalt: dict[str, list[_ZeilenKontext]] = {}
    for kontext in ohne_brauchbare_id:
        nach_inhalt.setdefault(kontext.feld_hash, []).append(kontext)
    for feld_hash, gruppe in nach_inhalt.items():
        if len(gruppe) > 1:
            for kontext in gruppe:
                dubletten_grund[kontext.zeile_nr] = (
                    "Identischer Rohdatensatz mehrfach vorhanden, ohne brauchbare eindeutige ID; kann "
                    "nicht sicher als getrennte Buchungen oder als Wiederholung unterschieden werden."
                )

    # --- S/D-Sammelgruppen-Zugehörigkeit ÜBER ALLE geprüften Zeilen
    # bestimmen (unabhängig von obiger Dublettenerkennung). Eine Zeile
    # mit kaputtem Sammelprofil wird NIE zum gewöhnlichen Einzelumsatz -
    # sie zählt als Gruppenmitglied ("KAPUTT") und poisoned damit jede
    # sonst vielleicht zufällig valide erscheinende Restgruppe. ---
    einzel_kontexte: list[_ZeilenKontext] = []
    sd_gruppen: dict[_GruppenSchluessel, list[tuple[_ZeilenKontext, str, str | None]]] = {}
    for kontext in geprueft:
        eid = kontext.werte["Enthaltene Überweisung ID"]
        analyse = _sammel_id_analyse(eid, iban=kontext.eigene_iban, waehrung=kontext.waehrung, buchungsdatum=kontext.buchungsdatum)
        ist_kaputtes_profil = analyse is None and bool(eid) and _sieht_wie_sammel_id_versuch_aus(eid)
        if analyse is None and not ist_kaputtes_profil:
            einzel_kontexte.append(kontext)
            continue
        referenz = kontext.werte["Buchungsreferenz"]
        if not _ist_brauchbarer_schluessel(referenz):
            grund = _FEHLENDE_REFERENZ_FUER_SD_GRUND if analyse is not None else _INKONSISTENTES_SAMMELPROFIL_GRUND
            ergebnisse_je_zeile[kontext.zeile_nr] = _ergebnis_pruefffall(kontext, grund)
            continue
        schluessel = _GruppenSchluessel(kontext.eigene_iban, kontext.waehrung, kontext.buchungsdatum, referenz)
        if analyse is not None:
            marker, praefix = analyse
            sd_gruppen.setdefault(schluessel, []).append((kontext, marker, praefix))
        else:
            sd_gruppen.setdefault(schluessel, []).append((kontext, "KAPUTT", None))

    for kontext in einzel_kontexte:
        if kontext.zeile_nr in dubletten_grund:
            ergebnisse_je_zeile[kontext.zeile_nr] = _ergebnis_pruefffall(kontext, dubletten_grund[kontext.zeile_nr])
        elif not _hat_unabhaengige_identitaet(kontext):
            ergebnisse_je_zeile[kontext.zeile_nr] = _ergebnis_pruefffall(kontext, _KEINE_UNABHAENGIGE_IDENTITAET_GRUND)
        else:
            ergebnisse_je_zeile[kontext.zeile_nr] = _ergebnis_kandidat(kontext)

    for mitglieder in sd_gruppen.values():
        for ergebnis in _verarbeite_sd_gruppe(mitglieder, dubletten_grund):
            ergebnisse_je_zeile[ergebnis.zeile_nr] = ergebnis

    zeilen = tuple(ergebnisse_je_zeile[nr] for nr in sorted(ergebnisse_je_zeile))

    kandidaten_liste = [z.kandidat for z in zeilen if z.status == "KANDIDAT" and z.kandidat is not None]
    eingaenge_cent = sum(k.betrag_cent for k in kandidaten_liste if k.betrag_cent > 0)
    ausgaenge_cent = sum(k.betrag_cent for k in kandidaten_liste if k.betrag_cent < 0)

    saldo_kontrolle = None
    if anfangssaldo_cent is not None and endsaldo_cent is not None:
        berechnet = anfangssaldo_cent + eingaenge_cent + ausgaenge_cent
        saldo_kontrolle = SaldoKontrolle(
            anfangssaldo_cent=anfangssaldo_cent,
            endsaldo_cent=endsaldo_cent,
            berechneter_endsaldo_cent=berechnet,
            stimmt_ueberein=(berechnet == endsaldo_cent),
        )

    return GeorgeBusinessPreview(
        datei_sha256=_sha256_bytes(rohbytes),
        erwartetes_konto_iban=erwartetes_konto_iban,
        von=von,
        bis=bis,
        zeilen=zeilen,
        eingaenge_cent=eingaenge_cent,
        ausgaenge_cent=ausgaenge_cent,
        saldo_kontrolle=saldo_kontrolle,
    )
