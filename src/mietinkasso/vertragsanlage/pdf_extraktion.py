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
übernommenen Wert."""

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
    warnungen: tuple[str, ...]


_MAX_AUSZUG_LAENGE = 220
_MINDEST_TEXTLAENGE_FUER_TEXTLAGE = 20

# Bewusst EINFACHE, dokumentierte Regex-Heuristiken für gängige
# deutschsprachige Vertragsformulierungen - KEIN Anspruch auf
# vollständige Vertragssemantik. Jedes Muster hat genau eine
# Klammergruppe (der eigentliche Wert); ein Muster ohne Klammergruppe
# dient nur als Ja/Nein-Indikator (z. B. Nutzungsart-Hinweise).
_MUSTER: dict[str, re.Pattern[str]] = {
    "kaution_cent": re.compile(
        r"Kaution[^\d€]{0,60}([0-9]{1,3}(?:[.,][0-9]{3})*(?:,[0-9]{2})?)\s*(?:€|EUR)",
        re.IGNORECASE,
    ),
    "mietbeginn": re.compile(
        r"(?:Mietbeginn|Beginn des Mietverh(?:ä|ae)ltnisses|Mietverh(?:ä|ae)ltnis beginnt am)"
        r"[^\d]{0,20}(\d{1,2}\.\d{1,2}\.\d{4})",
        re.IGNORECASE,
    ),
    "index_reihe": re.compile(
        r"(Verbraucherpreisindex\s*\d{4}|VPI\s*\d{4})",
        re.IGNORECASE,
    ),
    "index_schwelle_prozent": re.compile(
        r"(?:um mehr als|übersteigt um|Schwankung von)\s*([0-9]{1,2}(?:,[0-9]{1,2})?)\s*(?:%|Prozent)",
        re.IGNORECASE,
    ),
    "mahngebuehr_cent": re.compile(
        r"Mahnspesen?[^\d€]{0,40}([0-9]{1,3}(?:[.,][0-9]{3})*(?:,[0-9]{2})?)\s*(?:€|EUR)",
        re.IGNORECASE,
    ),
    "mahngebuehr_keine": re.compile(r"(keine Mahnspesen|keine Mahngeb(?:ü|ue)hr)", re.IGNORECASE),
    "nutzungsart_wohnung": re.compile(r"(Wohnungsmietvertrag|zu Wohnzwecken)", re.IGNORECASE),
    "nutzungsart_geschaeft": re.compile(r"(Gesch(?:ä|ae)ftsraummietvertrag|Gesch(?:ä|ae)ftslokal)", re.IGNORECASE),
    "nutzungsart_buero": re.compile(r"(B(?:ü|ue)romietvertrag|als B(?:ü|ue)ro vermietet)", re.IGNORECASE),
}


def _auszug(text: str, start: int, ende: int) -> str:
    von = max(0, start - 80)
    bis = min(len(text), ende + 80)
    return text[von:bis].replace("\n", " ").strip()[:_MAX_AUSZUG_LAENGE]


def extrahiere(rohbytes: bytes, *, max_seiten: int) -> ExtraktionsErgebnis:
    """Liest Text seitenweise aus (kein OCR - ein gescanntes Bild ohne
    Textlayer liefert leeren Text) und sucht die `_MUSTER` je Feld über
    ALLE ausgewerteten Seiten hinweg; der ERSTE Treffer gewinnt (kein
    "letzter überschreibt"-Zufall bei mehrfacher Erwähnung, z. B. Kaution
    in der Präambel UND in einer späteren Zusatzvereinbarung)."""

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
    for seite in alle_seiten[:ausgewertete_seiten]:
        try:
            seiten_texte.append(seite.extract_text() or "")
        except Exception:
            seiten_texte.append("")

    gesamtlaenge = sum(len(t.strip()) for t in seiten_texte)
    hat_textlage = gesamtlaenge >= _MINDEST_TEXTLAENGE_FUER_TEXTLAGE
    if not hat_textlage:
        warnungen.append(
            "Kein auswertbarer Textlayer gefunden (vermutlich gescanntes Dokument ohne OCR) - "
            "es konnten KEINE Felder automatisch vorgeschlagen werden, bitte vollständig manuell erfassen."
        )
        return ExtraktionsErgebnis(
            seiten_anzahl=seiten_anzahl, ausgewertete_seiten=ausgewertete_seiten,
            hat_textlage=False, treffer={}, warnungen=tuple(warnungen),
        )

    treffer: dict[str, FeldTreffer] = {}
    for feld, muster in _MUSTER.items():
        for seiten_index, text in enumerate(seiten_texte):
            match = muster.search(text)
            if match is None:
                continue
            gruppe = match.group(1) if match.groups() else match.group(0)
            treffer[feld] = FeldTreffer(wert=gruppe, seite=seiten_index + 1, auszug=_auszug(text, match.start(), match.end()))
            break
    return ExtraktionsErgebnis(
        seiten_anzahl=seiten_anzahl, ausgewertete_seiten=ausgewertete_seiten,
        hat_textlage=True, treffer=treffer, warnungen=tuple(warnungen),
    )
