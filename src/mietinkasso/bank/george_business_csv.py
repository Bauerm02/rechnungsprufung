"""Rein lesender Vorschau-Adapter für den George-Business-CSV-Export.

WICHTIG — Abgrenzung zu `bank/importer.py` und `bank/service.py`:

- Dieses Modul ist bewusst NICHT mit dem bestehenden Bankimport
  verdrahtet. Es liest keine Datenbank, schreibt keine Datenbank, ordnet
  keine Zahlungen einem Vertrag zu und löst keinen Mahnlauf/Versand aus.
  Es ist ein reiner Format-/Struktur-Prüfschritt für einen echten
  George-Business-CSV-Export, gedacht als Vorstufe VOR einem eventuen
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

Sammel-Summenzeilen (kritischer Punkt): Der Export kann sowohl
Sammelüberweisungs-Summenzeilen als auch deren Einzelposten enthalten,
beide unter derselben "(Sammel-) Überweisung ID". Weder die
"(Sammel-) Überweisung ID" noch die "Buchungsreferenz" sind daher ein
eindeutiger Transaktionsschlüssel und werden hier NIEMALS pauschal zur
Deduplizierung verwendet. Eine Zeile wird nur dann als Summenzeile
ausgewiesen (und aus den buchbaren Kandidaten entfernt), wenn innerhalb
derselben Sammelgruppe eine eindeutige Markierung ("S" für Summe, "D"
für Detail — abgeleitet aus dem letzten Buchstaben vor einer optionalen
Endziffernfolge in "Enthaltene Überweisung ID") UND eine centgenau
exakte Detail-Summenprüfung vorliegen. Dieses Markierungsmerkmal ist
eine BEOBACHTETE Struktur, keine verifizierte Anbieterzusage — jede
Sammelgruppe, die sich nicht eindeutig und vollständig auflösen lässt,
wird als Prüffall ausgewiesen statt geraten oder automatisch bereinigt.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

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

_OES_BETRAG_MUSTER = re.compile(r"^[+-]?(?:[0-9]{1,3}(?:\.[0-9]{3})*|[0-9]+),[0-9]{2}$")
_DATUM_MUSTER = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
_ENDMARKIERUNG_MUSTER = re.compile(r"([A-Za-z])[0-9]*$")

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
    abweichende Kopfzeile). Wird abgelehnt statt spekulativ als CSV mit
    Lücken weiterverarbeitet."""


@dataclass(frozen=True)
class GeorgeKandidat:
    """Ein normalisierter, geprüfter Buchungskandidat (Einzelumsatz oder
    validierter Sammel-Detailposten). Noch keine Buchung, keine
    Vertrags-/Mieterzuordnung — reine Vorschau."""

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
    konto_stimmt_ueberein: bool
    im_erwarteten_zeitraum: bool


@dataclass(frozen=True)
class GeorgeZeilenErgebnis:
    """Ergebnis genau einer Datenzeile — jede Zeile der Datei taucht hier
    genau einmal auf, unabhängig davon ob sie ein Kandidat, eine erkannte
    Summenzeile, ein Prüffall oder abgelehnt ist. Nichts wird still
    weggelassen.

    status:
      - "KANDIDAT": normalisierter, buchbarer Vorschau-Kandidat.
      - "SAMMEL_SUMME": strukturell eindeutig als Sammel-Summenzeile
        erkannt (Detailsumme stimmt exakt); bewusst kein Kandidat.
      - "PRUEFFALL": Sammelgruppe/Zeile nicht eindeutig auflösbar;
        erfordert manuelle Klärung, wird NICHT automatisch normalisiert.
      - "ABGELEHNT": Zeile technisch ungültig (Format-/Wertefehler).
    """

    zeile_nr: int
    status: str
    grund: str | None
    sha256_zeile: str
    roh_zeile: str
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


@dataclass(frozen=True)
class _ZeilenKontext:
    zeile_nr: int
    werte: dict[str, str]
    betrag_cent: int
    waehrung: str
    buchungsdatum: date
    valuta: date | None
    konto_stimmt_ueberein: bool
    im_erwarteten_zeitraum: bool


def _roh_zeile(werte: dict[str, str]) -> str:
    return ",".join(f"{spalte}={werte.get(spalte, '')}" for spalte in ERWARTETE_SPALTEN)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pruefe_kein_xlsx(rohbytes: bytes) -> None:
    # XLSX-Dateien (und jedes andere ZIP-Container-Format) beginnen mit
    # der ZIP-Signatur. Eine .xlsx-Datei "irrtümlich" als Text/CSV zu
    # dekodieren würde Binärmüll oder eine leere/kaputte Kopfzeile
    # liefern statt eines klaren Fehlers — deshalb explizit vorab prüfen.
    if rohbytes[:4] == b"PK\x03\x04" or rohbytes[:4] == b"PK\x05\x06" or rohbytes[:4] == b"PK\x07\x08":
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


def _lese_csv_zeilen(text: str) -> list[tuple[int, dict[str, str]]]:
    reader = csv.DictReader(io.StringIO(text))
    spalten = tuple(reader.fieldnames or ())
    if set(spalten) != set(ERWARTETE_SPALTEN):
        fehlend = sorted(set(ERWARTETE_SPALTEN) - set(spalten))
        zusaetzlich = sorted(set(spalten) - set(ERWARTETE_SPALTEN))
        raise GeorgeFormatFehlerError(
            "Kopfzeile entspricht nicht dem erwarteten George-Business-CSV-Format. "
            f"Fehlende Spalten: {fehlend or '-'}; unerwartete Spalten: {zusaetzlich or '-'}."
        )
    ergebnis: list[tuple[int, dict[str, str]]] = []
    zeile_nr = 0
    for rohzeile in reader:
        # Vollständig leere Zeilen (z. B. eine schließende Leerzeile durch
        # \r\n\r\n am Dateiende) sind kein Datensatz und keine
        # Ausschlusskandidatin - werden übersprungen statt als kaputte
        # Zeile ausgewiesen zu werden.
        if all((wert or "").strip() == "" for wert in rohzeile.values()):
            continue
        zeile_nr += 1
        werte = {spalte: (rohzeile.get(spalte) or "").strip() for spalte in ERWARTETE_SPALTEN}
        ergebnis.append((zeile_nr, werte))
    return ergebnis


def parse_oesterreichischen_betrag(text: str) -> int:
    if not _OES_BETRAG_MUSTER.fullmatch(text):
        raise ValueError(
            f"Betrag '{text}' ist nicht im erwarteten österreichischen Format (z. B. 1.234,56 oder "
            "-123,45 mit genau zwei Nachkommastellen)."
        )
    normalisiert = text.replace(".", "").replace(",", ".")
    cent = to_cents(Decimal(normalisiert))
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


def _endmarkierung(enthaltene_id: str) -> str | None:
    if not enthaltene_id:
        return None
    treffer = _ENDMARKIERUNG_MUSTER.search(enthaltene_id)
    return treffer.group(1).upper() if treffer else None


def _validiere_basis(
    zeile_nr: int,
    werte: dict[str, str],
    erwartetes_konto_iban: str,
    von: date,
    bis: date,
) -> _ZeilenKontext:
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
        werte=werte,
        betrag_cent=betrag_cent,
        waehrung=werte["Währung"],
        buchungsdatum=buchungsdatum,
        valuta=valuta,
        konto_stimmt_ueberein=(werte["Eigene IBAN"] == erwartetes_konto_iban),
        im_erwarteten_zeitraum=(von <= buchungsdatum <= bis),
    )


def _optional(wert: str) -> str | None:
    return wert or None


def _baue_kandidat(kontext: _ZeilenKontext) -> GeorgeKandidat:
    w = kontext.werte
    return GeorgeKandidat(
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
        konto_stimmt_ueberein=kontext.konto_stimmt_ueberein,
        im_erwarteten_zeitraum=kontext.im_erwarteten_zeitraum,
    )


def _ergebnis_kandidat(kontext: _ZeilenKontext) -> GeorgeZeilenErgebnis:
    return GeorgeZeilenErgebnis(
        zeile_nr=kontext.zeile_nr,
        status="KANDIDAT",
        grund=None,
        sha256_zeile=_sha256(_roh_zeile(kontext.werte)),
        roh_zeile=_roh_zeile(kontext.werte),
        kandidat=_baue_kandidat(kontext),
    )


def _ergebnis_summenzeile(kontext: _ZeilenKontext, grund: str) -> GeorgeZeilenErgebnis:
    return GeorgeZeilenErgebnis(
        zeile_nr=kontext.zeile_nr,
        status="SAMMEL_SUMME",
        grund=grund,
        sha256_zeile=_sha256(_roh_zeile(kontext.werte)),
        roh_zeile=_roh_zeile(kontext.werte),
        kandidat=_baue_kandidat(kontext),
    )


def _ergebnis_pruefffall(kontext: _ZeilenKontext, grund: str) -> GeorgeZeilenErgebnis:
    return GeorgeZeilenErgebnis(
        zeile_nr=kontext.zeile_nr,
        status="PRUEFFALL",
        grund=grund,
        sha256_zeile=_sha256(_roh_zeile(kontext.werte)),
        roh_zeile=_roh_zeile(kontext.werte),
        kandidat=None,
    )


def _ergebnis_abgelehnt(zeile_nr: int, werte: dict[str, str], grund: str) -> GeorgeZeilenErgebnis:
    return GeorgeZeilenErgebnis(
        zeile_nr=zeile_nr,
        status="ABGELEHNT",
        grund=grund,
        sha256_zeile=_sha256(_roh_zeile(werte)),
        roh_zeile=_roh_zeile(werte),
        kandidat=None,
    )


_UNVOLLSTAENDIGE_SAMMELGRUPPE_GRUND = (
    "Sammelgruppe nicht eindeutig auflösbar (Markierung 'S'/'D' in 'Enthaltene Überweisung ID' fehlt, "
    "ist nicht eindeutig, oder die Detailbeträge summieren sich nicht centgenau auf die Summenzeile). "
    "Wird NICHT automatisch normalisiert oder bereinigt, sondern als Prüffall ausgewiesen."
)


def _verarbeite_sammelgruppe(gruppe: list[_ZeilenKontext]) -> list[GeorgeZeilenErgebnis]:
    if len(gruppe) == 1:
        return [_ergebnis_kandidat(gruppe[0])]

    markierungen = {k.zeile_nr: _endmarkierung(k.werte["Enthaltene Überweisung ID"]) for k in gruppe}
    summenzeilen = [k for k in gruppe if markierungen[k.zeile_nr] == "S"]
    detailzeilen = [k for k in gruppe if markierungen[k.zeile_nr] == "D"]
    unklare = [k for k in gruppe if markierungen[k.zeile_nr] not in ("S", "D")]

    eindeutig = (
        len(summenzeilen) == 1
        and len(detailzeilen) >= 1
        and not unklare
        and len({k.werte["Eigene IBAN"] for k in gruppe}) == 1
        and len({k.waehrung for k in gruppe}) == 1
        and len({k.buchungsdatum for k in gruppe}) == 1
        and sum(k.betrag_cent for k in detailzeilen) == summenzeilen[0].betrag_cent
    )
    if not eindeutig:
        return [_ergebnis_pruefffall(k, _UNVOLLSTAENDIGE_SAMMELGRUPPE_GRUND) for k in gruppe]

    ergebnisse = [_ergebnis_kandidat(k) for k in detailzeilen]
    ergebnisse.append(
        _ergebnis_summenzeile(
            summenzeilen[0],
            "Sammel-Summenzeile: Detailbeträge derselben Sammelgruppe summieren sich centgenau exakt auf "
            "diesen Betrag; strukturell eindeutig als Summe erkannt und daher kein buchbarer Kandidat.",
        )
    )
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

    `erwartetes_konto_iban`/`von`/`bis` sind explizite Prüfparameter: JEDE
    Zeile wird dagegen geprüft (siehe `GeorgeKandidat.konto_stimmt_ueberein`/
    `im_erwarteten_zeitraum`), aber keine Zeile wird deshalb aus dem
    Ergebnis entfernt - nur die Summenbildung (`eingaenge_cent`/
    `ausgaenge_cent`) und die optionale Saldenkontrolle berücksichtigen
    ausschließlich Zeilen, die auf beide Parameter passen.
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

    ergebnisse_je_zeile: dict[int, GeorgeZeilenErgebnis] = {}
    kontexte_je_sammel_id: dict[str, list[_ZeilenKontext]] = {}
    einzel_kontexte: list[_ZeilenKontext] = []

    for zeile_nr, werte in rohzeilen:
        try:
            kontext = _validiere_basis(zeile_nr, werte, erwartetes_konto_iban, von, bis)
        except ValueError as exc:
            ergebnisse_je_zeile[zeile_nr] = _ergebnis_abgelehnt(zeile_nr, werte, str(exc))
            continue
        sammel_id = werte["(Sammel-) Überweisung ID"]
        if sammel_id:
            kontexte_je_sammel_id.setdefault(sammel_id, []).append(kontext)
        else:
            einzel_kontexte.append(kontext)

    for kontext in einzel_kontexte:
        ergebnisse_je_zeile[kontext.zeile_nr] = _ergebnis_kandidat(kontext)

    for gruppe in kontexte_je_sammel_id.values():
        for ergebnis in _verarbeite_sammelgruppe(gruppe):
            ergebnisse_je_zeile[ergebnis.zeile_nr] = ergebnis

    zeilen = tuple(ergebnisse_je_zeile[nr] for nr in sorted(ergebnisse_je_zeile))

    relevante_kandidaten = [
        z.kandidat
        for z in zeilen
        if z.status == "KANDIDAT" and z.kandidat is not None
        and z.kandidat.konto_stimmt_ueberein and z.kandidat.im_erwarteten_zeitraum
    ]
    eingaenge_cent = sum(k.betrag_cent for k in relevante_kandidaten if k.betrag_cent > 0)
    ausgaenge_cent = sum(k.betrag_cent for k in relevante_kandidaten if k.betrag_cent < 0)

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
        datei_sha256=_sha256(text),
        erwartetes_konto_iban=erwartetes_konto_iban,
        von=von,
        bis=bis,
        zeilen=zeilen,
        eingaenge_cent=eingaenge_cent,
        ausgaenge_cent=ausgaenge_cent,
        saldo_kontrolle=saldo_kontrolle,
    )
