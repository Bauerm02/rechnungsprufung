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
#
# **Korrektur nach unabhängiger Abnahme (Codex, 20.10.2026, reproduzierter
# Fehler):** die vorherige Fassung begrenzte die FÜHRENDE Zifferngruppe
# auf `[0-9]{1,3}` OHNE eine nachfolgende Zifferngrenze zu verlangen -
# bei einer unformatierten Zahl ohne Tausenderpunkt (z. B. "1200",
# "1200,00", "12345,67") matchte die Regex deshalb nur die ERSTEN 1-3
# Ziffern und ließ den Rest als scheinbar erfolgreichen, aber
# TRUNKIERTEN Treffer stehen ("1200" -> "120", "12345,67" -> "123").
# `[0-9]+` (unbegrenzt) statt `[0-9]{1,3}` behebt das: die führende
# Zifferngruppe konsumiert IMMER den vollständigen zusammenhängenden
# Ziffernlauf, die nachfolgenden optionalen Gruppen fangen danach nur
# noch einen WIRKLICHEN Tausenderpunkt-/Dezimaltrenner-Rest ab (siehe
# Regressionstests in `test_vertragsanlage_pdf.py`). Andere, bereits
# korrekt erkannte Formate ("1.500,00", "1.500.000,00", "45.50") werden
# dadurch NICHT umgedeutet - nur der Umfang der führenden Zifferngruppe
# selbst ändert sich.
#
# **Zweite Korrektur nach unabhängiger Abnahme (Codex, 20.10.2026,
# zwei weitere reproduzierte Grenzfälle):** "EUR1234.5678" wurde zu
# "1234.567" und "EUR1200,0005" zu "1200,000" - eine Zahl mit MEHR
# Nachkommastellen als unterstützt (max. 2, oder exakt 3-stellige
# Tausendergruppen) wurde bisher nicht abgelehnt, sondern nach den
# zuerst passenden optionalen Gruppen einfach STEHEN GELASSEN, mit
# einer unverarbeiteten Restziffer direkt danach - ein stillschweigend
# TEILWEISE korrekter, aber inhaltlich FALSCHER Betrag (Auftrag Markus:
# "niemals teilweise als gültiger Betrag gelten"). Der nachgestellte
# negative Lookahead `(?![.,]?\d)` verlangt, dass UNMITTELBAR nach dem
# letzten erfolgreich konsumierten Zeichen KEINE weitere Ziffer (auch
# nicht durch einen weiteren Trenner eingeleitet) folgt - reicht die
# tatsächliche Präzision über das unterstützte Format hinaus, schlägt
# dadurch JEDE Backtracking-Variante fehl und die gesamte Zahl bleibt
# AN DIESER STELLE unerkannt (kein Treffer), statt einen verkürzten
# Suffix zu matchen. Siehe Regressionstests in
# `test_vertragsanlage_pdf.py` (inkl. der spiegelbildlichen
# Betrag-vor-Währung-Alternative).
_BETRAG_FRAGMENT = (
    r"(?:in H(?:ö|oe)he von\s*)?(?:"
    r"(?:€|EUR)\s*([0-9]+(?:[.,][0-9]{3})*(?:[.,][0-9]{2})?)(?![.,]?\d)"
    r"|"
    r"([0-9]+(?:[.,][0-9]{3})*(?:[.,][0-9]{2})?)(?![.,]?\d)\s*(?:€|EUR)"
    r")"
)

# Bewusst EINFACHE, dokumentierte Regex-Heuristiken für gängige
# deutschsprachige Vertragsformulierungen - KEIN Anspruch auf
# vollständige Vertragssemantik. Jedes Muster liefert GENAU EINEN
# relevanten Wert über `_erste_gruppe`; ein Muster ohne Klammergruppe
# dient nur als Ja/Nein-Indikator (z. B. Nutzungsart-Hinweise).
_MUSTER: dict[str, re.Pattern[str]] = {
    "kaution_cent": re.compile(r"Kaution[^\d€]{0,60}" + _BETRAG_FRAGMENT, re.IGNORECASE),
    # Korrektur nach unabhängiger Abnahme (Codex, 20.10.2026): "Nettomiete"
    # wurde bisher in DENSELBEN Extraktionsschlüssel wie "Hauptmietzins"/
    # "Miete monatlich" gemischt - `hauptmietzins_cent` ist im System aber
    # per Definition BRUTTO (siehe `backoffice/vertragsanlage_form.py`,
    # Label "Hauptmietzins (brutto, EUR)" und `VertragsKomponenteTable.
    # betrag_cent`-Docstring in `domain/money.py`). Eine explizit als NETTO
    # bezeichnete Vertragsangabe darf NIE stillschweigend in dieses
    # Brutto-Feld übernommen werden (Auftrag Markus: "Nettomiete niemals
    # still als Brutto übernehmen") - "Nettomiete" hat deshalb jetzt einen
    # EIGENEN Extraktionsschlüssel `nettomiete_cent`, der NICHT auf
    # `hauptmietzins_cent` gemappt wird (siehe `vorschlaege.py`).
    "hauptmietzins_cent": re.compile(
        r"(?:Hauptmietzins|Miete monatlich)[^\d€]{0,40}" + _BETRAG_FRAGMENT, re.IGNORECASE,
    ),
    "nettomiete_cent": re.compile(r"Nettomiete[^\d€]{0,40}" + _BETRAG_FRAGMENT, re.IGNORECASE),
    "betriebskosten_cent": re.compile(r"Betriebskosten[^\d€]{0,40}" + _BETRAG_FRAGMENT, re.IGNORECASE),
    "heizkosten_cent": re.compile(r"Heizkosten[^\d€]{0,40}" + _BETRAG_FRAGMENT, re.IGNORECASE),
    "kueche_cent": re.compile(r"K(?:ü|ue)che[^\d€]{0,40}" + _BETRAG_FRAGMENT, re.IGNORECASE),
    "parkplatz_cent": re.compile(r"Parkplatz[^\d€]{0,40}" + _BETRAG_FRAGMENT, re.IGNORECASE),
    # Korrektur nach unabhängiger Abnahme (Codex, 20.10.2026): "Mahngebühr"
    # (mit und ohne Plural-"en") war bisher kein erkanntes Synonym -
    # Verträge verwenden beide Begriffe für dieselbe Fachregel.
    "mahngebuehr_cent": re.compile(
        r"(?:Mahnspesen?|Mahngeb(?:ü|ue)hr(?:en)?)[^\d€]{0,40}" + _BETRAG_FRAGMENT, re.IGNORECASE,
    ),
    "mahngebuehr_keine": re.compile(r"(keine Mahnspesen|keine Mahngeb(?:ü|ue)hr(?:en)?)", re.IGNORECASE),
    "mieter_name": re.compile(r"(?<!Mit)Mieter\s*:?\s*\n?\s*([A-ZÄÖÜ][\wÄÖÜäöüß .,-]{2,70})", re.MULTILINE),
    "vermieter_name": re.compile(r"Vermieter\s*:?\s*\n?\s*([A-ZÄÖÜ][\wÄÖÜäöüß .,-]{2,70})", re.MULTILINE),
    # Abnahme-Folgefix (Codex 20.10.2026): "explizite Mieter-E-Mail/
    # -Telefon werden gar nicht extrahiert" - Mieterkontakte werden,
    # ANDERS als Vermieterkontakte, tatsächlich als editierbarer
    # Formularvorschlag gebraucht (für eine neu anzulegende Debitor-
    # Kontaktangabe) - Vermieterkontakte werden bewusst NIE extrahiert
    # (Auftrag: "Vermieterkontakte niemals übernehmen" - es gibt deshalb
    # absichtlich KEIN `vermieter_email`/`vermieter_telefon`-Muster).
    # `(?<!Mit)` verhindert ein Fehltreffer auf "...Mitmieter" (das Wort
    # "Mieter" ist eine Teilzeichenkette von "Mitmieter").
    "mieter_email": re.compile(r"E-?Mail\s+(?<!Mit)Mieter\s*:?\s*([\w.+-]+@[\w.-]+\.\w+)", re.IGNORECASE),
    "mieter_telefon": re.compile(r"Telefon\s+(?<!Mit)Mieter\s*:?\s*([0-9+()/ .-]{6,25})", re.IGNORECASE),
    # Auftrag HV-20261020-VERTRAGSDATENBASIS: Mitmieter/Vertreter sind
    # KEIN Debitor und werden NIE automatisch angelegt (siehe
    # `vorschlaege.py`/`backoffice/vertragsanlage_form.py::_parteien_html`)
    # - nur ein geprüfter Hinweis mit Seitenbeleg für die MANUELLE
    # Erfassung über "Partei hinzufügen" in der Mieterakte.
    "mitmieter_name": re.compile(
        r"(?:Mitmieter(?:in)?|weitere[rn]?\s+Mieter(?:in)?|zus(?:ä|ae)tzliche[rn]?\s+Mieter(?:in)?)\s*:?\s*\n?\s*"
        r"([A-ZÄÖÜ][\wÄÖÜäöüß .,-]{2,70}?)(?=\s*(?:,|Tel(?:efon)?\.?\s*:|\n|$))",
        re.MULTILINE | re.IGNORECASE,
    ),
    # Abnahme-Folgefix (Codex 20.10.2026): das Telefon/E-Mail steht in der
    # Praxis oft NICHT in derselben Zeile wie der Name, sondern in einer
    # eigenen Folgezeile ("Telefon Mitmieter: ..."/"E-Mail Mitmieter: ...")
    # - zusätzlich zum bisherigen (Name+Tel in EINER Zeile)-Muster.
    "mitmieter_telefon": re.compile(
        r"(?:Mitmieter(?:in)?|weitere[rn]?\s+Mieter(?:in)?)[^\n]{0,80}?(?:Tel(?:efon)?\.?\s*:?)\s*([0-9+()/ .-]{6,25})"
        r"|Telefon\s+Mitmieter\s*:?\s*([0-9+()/ .-]{6,25})",
        re.IGNORECASE,
    ),
    "mitmieter_email": re.compile(r"E-?Mail\s+Mitmieter\s*:?\s*([\w.+-]+@[\w.-]+\.\w+)", re.IGNORECASE),
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
        r"(?:um mehr als|übersteigt um|Schwankung von|Schwankung mehr als)\s*([0-9]{1,2}(?:,[0-9]{1,2})?)\s*(?:%|Prozent)",
        re.IGNORECASE,
    ),
    # Abnahme-Folgefix (Codex 20.10.2026): "ausdrücklich genannter
    # Indexbasiswert wird nicht extrahiert" - Basismonat als Rohstring
    # (Monatsname+Jahr ODER MM/YYYY-Schreibweise), wird in `vorschlaege.py`
    # in das kanonische `YYYY-MM`-Formularformat übersetzt; Basiswert als
    # einfache Dezimalzahl wie `index_schwelle_prozent` oben.
    "index_urspruenglicher_basismonat": re.compile(
        r"Basismonat[^\d\wÄÖÜäöüß]{0,10}"
        r"((?:J(?:ä|ae)nner|Januar|Februar|M(?:ä|ae)rz|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember)"
        r"\s*\d{4}|\d{1,2}[./]\d{4})",
        re.IGNORECASE,
    ),
    "index_urspruenglicher_basiswert": re.compile(r"Basiswert[^\d]{0,10}([0-9]+(?:[.,][0-9]+)?)", re.IGNORECASE),
    "nutzungsart_wohnung": re.compile(r"(Wohnungsmietvertrag|zu Wohnzwecken)", re.IGNORECASE),
    "nutzungsart_geschaeft": re.compile(r"(Gesch(?:ä|ae)ftsraummietvertrag|Gesch(?:ä|ae)ftslokal)", re.IGNORECASE),
    "nutzungsart_buero": re.compile(r"(B(?:ü|ue)romietvertrag|als B(?:ü|ue)ro vermietet)", re.IGNORECASE),
    # Auftrag HV-20261020-VERTRAGSDATENBASIS - Termine GETRENNT von
    # Mietbeginn/-ende (siehe `vorschlaege.py`):
    "unterzeichnungsdatum": re.compile(
        r"(?:unterzeichnet am|unterschrieben am|Datum der Unterzeichnung)[^\d]{0,20}(\d{1,2}\.\d{1,2}\.\d{4})",
        re.IGNORECASE,
    ),
    "uebergabe_am": re.compile(
        r"(?:[Üü]bergeben am|[Üü]bergabe(?:datum)?[^\d€]{0,10}am)[^\d]{0,20}(\d{1,2}\.\d{1,2}\.\d{4})",
        re.IGNORECASE,
    ),
    # Tri-State (siehe `domain/enums.py::Befristungsstatus`) - wie bei
    # `nutzungsart_*` oben ZWEI GETRENNTE Hinweismuster statt einem
    # gemeinsamen: `vorschlaege.py` setzt das Feld NUR, wenn GENAU EINES
    # der beiden trifft - trifft ein Dokument BEIDE (Mischtext/falsch
    # erkannte Passage), bleibt das Feld bewusst unbesetzt statt zu raten.
    # Negative Lookbehind "(?<!un)" verhindert, dass "befristet_hinweis"
    # fälschlich INNERHALB von "unbefristet..." mitgematcht wird
    # ("befristet" ist eine Teilzeichenkette von "unbefristet").
    "befristet_hinweis": re.compile(r"(?<!un)(befristet(?:er Mietvertrag| auf| bis)?)", re.IGNORECASE),
    "unbefristet_hinweis": re.compile(r"(unbefristet(?:er Mietvertrag)?)", re.IGNORECASE),
    "bk_hk_akonto_hinweis": re.compile(
        r"(Akontozahlung|Betriebskostenakonto|Vorauszahlung(?:en)? (?:für|auf) (?:die )?Betriebskosten)",
        re.IGNORECASE,
    ),
    "bk_hk_pauschale_hinweis": re.compile(
        r"(Pauschalmiete|Inklusivmiete|All-in-Miete|Betriebskosten(?:pauschale)? (?:sind|ist) (?:in der Miete )?inkludiert)",
        re.IGNORECASE,
    ),
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
