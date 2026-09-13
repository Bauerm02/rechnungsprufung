"""Lokale, kostenlose PDF-Textextraktion + einfache Regex-Feldvorschläge
(Auftrag HV-20260913-VERTRAGSANLAGE) - AUSDRÜCKLICH KEIN LLM-Aufruf zur
Laufzeit (siehe `infrastructure/config.py`-Docstring: "MVP1 läuft als
deterministische Regelmaschine ohne LLM-Abhängigkeit zur Laufzeit").

Jeder Treffer ist ein bloßer REGEX-Fund mit Seitenreferenz und
Textauszug (Beleg) - das hier täuscht NIE eine sichere, umfassende
Vertragssemantik vor. Ein gescanntes Dokument ohne Textlayer liefert
`hat_textlage=False` und KEINE Treffer statt stillschweigend falscher
oder leerer Werte - die editierbare menschliche Prüfung im Backoffice
bleibt in JEDEM Fall Pflicht, das hier erzeugt nie einen final
übernommenen Wert.

Findet ein Muster MEHRERE, inhaltlich UNTERSCHIEDLICHE Werte für
dasselbe Feld (z. B. eine Kaution in der Präambel und ein abweichender
Betrag in einem späteren Nachtrag), wird das NICHT stillschweigend zu
einem einzelnen "ersten Treffer gewinnt"-Vorschlag - stattdessen landet
das Feld in `mehrdeutige_treffer` mit ALLEN Fundstellen, und es wird
KEIN Formularwert vorgeschlagen (siehe `vorschlaege.py`)."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass

from pypdf import PdfReader
from pypdf.errors import PdfReadError


class PdfNichtLesbarError(Exception):
    """Datei konnte nicht als PDF geparst werden (beschädigt, entgegen der
    Magic-Byte-Prüfung in `ablage.py` doch kein valides PDF, verschlüsselt
    ohne bekanntes Passwort o. Ä.)."""


@dataclass(frozen=True)
class FeldTreffer:
    wert: str
    seite: int  # 1-basiert
    auszug: str  # Textumgebung des Treffers als Beleg, gekürzt


@dataclass(frozen=True)
class ExtraktionsErgebnis:
    seiten_anzahl: int
    ausgewertete_seiten: int
    hat_textlage: bool
    treffer: dict[str, FeldTreffer]
    #: Felder mit MEHREREN unterschiedlichen Werten im Dokument - siehe
    #: Moduldoc. Nie gleichzeitig mit einem Eintrag in `treffer` für
    #: dasselbe Feld.
    mehrdeutige_treffer: dict[str, list[FeldTreffer]]
    warnungen: tuple[str, ...]


_MAX_AUSZUG_LAENGE = 220
_MINDEST_TEXTLAENGE_FUER_TEXTLAGE = 20

# Geldbetrag VOR ODER NACH der Währung ("1.500,00 EUR", "EUR 1.500,00",
# "€ 1.500,00", "in Höhe von EUR 1.500,00") - GENAU eine der beiden
# Gruppen ist nach einem Treffer belegt, siehe `_erste_gruppe`.
_BETRAG_FRAGMENT = (
    r"(?:in H(?:ö|oe)he von\s*)?(?:"
    r"(?:€|EUR)\s*([0-9]{1,3}(?:[.,][0-9]{3})*(?:[.,][0-9]{2})?)"
    r"|"
    r"([0-9]{1,3}(?:[.,][0-9]{3})*(?:[.,][0-9]{2})?)\s*(?:€|EUR)"
    r")"
)

# Bewusst EINFACHE, dokumentierte Regex-Heuristiken für gängige
# deutschsprachige Vertragsformulierungen - KEIN Anspruch auf
# vollständige Vertragssemantik. Jedes Muster liefert GENAU EINEN
# relevanten Wert über `_erste_gruppe`; ein Muster ohne Klammergruppe
# dient nur als Ja/Nein-Indikator (z. B. Nutzungsart-Hinweise).
_MUSTER: dict[str, re.Pattern[str]] = {
    "kaution_cent": re.compile(r"Kaution[^\d€]{0,60}" + _BETRAG_FRAGMENT, re.IGNORECASE),
    "hauptmietzins_cent": re.compile(
        r"(?:Hauptmietzins|Nettomiete|Miete monatlich)[^\d€]{0,40}" + _BETRAG_FRAGMENT, re.IGNORECASE,
    ),
    "betriebskosten_cent": re.compile(r"Betriebskosten[^\d€]{0,40}" + _BETRAG_FRAGMENT, re.IGNORECASE),
    "heizkosten_cent": re.compile(r"Heizkosten[^\d€]{0,40}" + _BETRAG_FRAGMENT, re.IGNORECASE),
    "kueche_cent": re.compile(r"K(?:ü|ue)che[^\d€]{0,40}" + _BETRAG_FRAGMENT, re.IGNORECASE),
    "parkplatz_cent": re.compile(r"Parkplatz[^\d€]{0,40}" + _BETRAG_FRAGMENT, re.IGNORECASE),
    "mahngebuehr_cent": re.compile(r"Mahnspesen?[^\d€]{0,40}" + _BETRAG_FRAGMENT, re.IGNORECASE),
    "mahngebuehr_keine": re.compile(r"(keine Mahnspesen|keine Mahngeb(?:ü|ue)hr)", re.IGNORECASE),
    "mieter_name": re.compile(r"Mieter\s*:?\s*\n?\s*([A-ZÄÖÜ][\wÄÖÜäöüß .,-]{2,70})", re.MULTILINE),
    "vermieter_name": re.compile(r"Vermieter\s*:?\s*\n?\s*([A-ZÄÖÜ][\wÄÖÜäöüß .,-]{2,70})", re.MULTILINE),
    "mietbeginn": re.compile(
        r"(?:Mietbeginn|Beginn des Mietverh(?:ä|ae)ltnisses|Mietverh(?:ä|ae)ltnis beginnt am)"
        r"[^\d]{0,20}(\d{1,2}\.\d{1,2}\.\d{4})",
        re.IGNORECASE,
    ),
    "mietende": re.compile(
        r"(?:Mietende|befristet bis|Ende des Mietverh(?:ä|ae)ltnisses)[^\d]{0,20}(\d{1,2}\.\d{1,2}\.\d{4})",
        re.IGNORECASE,
    ),
    "index_reihe": re.compile(r"(Verbraucherpreisindex\s*\d{4}|VPI\s*\d{4})", re.IGNORECASE),
    "index_schwelle_prozent": re.compile(
        r"(?:um mehr als|übersteigt um|Schwankung von)\s*([0-9]{1,2}(?:,[0-9]{1,2})?)\s*(?:%|Prozent)",
        re.IGNORECASE,
    ),
    "nutzungsart_wohnung": re.compile(r"(Wohnungsmietvertrag|zu Wohnzwecken)", re.IGNORECASE),
    "nutzungsart_geschaeft": re.compile(r"(Gesch(?:ä|ae)ftsraummietvertrag|Gesch(?:ä|ae)ftslokal)", re.IGNORECASE),
    "nutzungsart_buero": re.compile(r"(B(?:ü|ue)romietvertrag|als B(?:ü|ue)ro vermietet)", re.IGNORECASE),
}


def _erste_gruppe(match: re.Match) -> str:
    if not match.groups():
        return match.group(0)
    for gruppe in match.groups():
        if gruppe is not None:
            return gruppe
    return match.group(0)


def _normalisiere(wert: str) -> str:
    """Für den Mehrdeutigkeits-Vergleich: reine Formatierungsunterschiede
    (Leerraum, Groß-/Kleinschreibung) sind KEIN inhaltlicher Unterschied,
    ein anderer Betrag/Wortlaut hingegen schon."""

    return " ".join(wert.split()).casefold()


def _auszug(text: str, start: int, ende: int) -> str:
    von = max(0, start - 80)
    bis = min(len(text), ende + 80)
    return text[von:bis].replace("\n", " ").strip()[:_MAX_AUSZUG_LAENGE]


def extrahiere(rohbytes: bytes, *, max_seiten: int) -> ExtraktionsErgebnis:
    """Liest Text seitenweise aus (kein OCR - ein gescanntes Bild ohne
    Textlayer liefert leeren Text) und sucht die `_MUSTER` je Feld über
    ALLE ausgewerteten Seiten hinweg. Anders als ein "erster Treffer
    gewinnt"-Ansatz werden ALLE Fundstellen gesammelt: liefern sie
    denselben (normalisierten) Wert, wird GENAU EIN `FeldTreffer`
    vorgeschlagen; widersprechen sie sich, landet das Feld in
    `mehrdeutige_treffer` mit allen Fundstellen und OHNE Formularvorschlag
    (siehe Moduldoc)."""

    try:
        reader = PdfReader(io.BytesIO(rohbytes))
    except (PdfReadError, ValueError, KeyError) as exc:
        raise PdfNichtLesbarError(f"PDF konnte nicht gelesen werden: {exc}") from exc

    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:
            raise PdfNichtLesbarError("PDF ist passwortgeschützt - kein automatischer Textzugriff möglich.") from exc

    alle_seiten = reader.pages
    seiten_anzahl = len(alle_seiten)
    warnungen: list[str] = []
    ausgewertete_seiten = min(seiten_anzahl, max_seiten)
    if seiten_anzahl > max_seiten:
        warnungen.append(
            f"Dokument hat {seiten_anzahl} Seiten, es wurden nur die ersten {max_seiten} ausgewertet."
        )

    seiten_texte: list[str] = []
    fehlgeschlagene_seiten = 0
    for seite in alle_seiten[:ausgewertete_seiten]:
        try:
            seiten_texte.append(seite.extract_text() or "")
        except Exception:
            seiten_texte.append("")
            fehlgeschlagene_seiten += 1
    if fehlgeschlagene_seiten:
        warnungen.append(
            f"Auf {fehlgeschlagene_seiten} von {ausgewertete_seiten} ausgewerteten Seite(n) konnte kein Text "
            "gelesen werden (übersprungen, NICHT als vollständig erfolgreich ausgegeben)."
        )

    gesamtlaenge = sum(len(t.strip()) for t in seiten_texte)
    hat_textlage = gesamtlaenge >= _MINDEST_TEXTLAENGE_FUER_TEXTLAGE
    if not hat_textlage:
        warnungen.append(
            "Kein auswertbarer Textlayer gefunden (vermutlich gescanntes Dokument ohne OCR) - "
            "es konnten KEINE Felder automatisch vorgeschlagen werden, bitte vollständig manuell erfassen."
        )
        return ExtraktionsErgebnis(
            seiten_anzahl=seiten_anzahl, ausgewertete_seiten=ausgewertete_seiten,
            hat_textlage=False, treffer={}, mehrdeutige_treffer={}, warnungen=tuple(warnungen),
        )

    treffer: dict[str, FeldTreffer] = {}
    mehrdeutige_treffer: dict[str, list[FeldTreffer]] = {}
    for feld, muster in _MUSTER.items():
        funde: list[FeldTreffer] = []
        for seiten_index, text in enumerate(seiten_texte):
            for match in muster.finditer(text):
                wert = _erste_gruppe(match)
                funde.append(FeldTreffer(wert=wert, seite=seiten_index + 1, auszug=_auszug(text, match.start(), match.end())))
        if not funde:
            continue
        distinkte_werte = {_normalisiere(f.wert) for f in funde}
        if len(distinkte_werte) == 1:
            treffer[feld] = funde[0]
        else:
            mehrdeutige_treffer[feld] = funde

    return ExtraktionsErgebnis(
        seiten_anzahl=seiten_anzahl, ausgewertete_seiten=ausgewertete_seiten,
        hat_textlage=True, treffer=treffer, mehrdeutige_treffer=mehrdeutige_treffer, warnungen=tuple(warnungen),
    )
