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
KEIN Formularwert vorgeschlagen (siehe `vorschlaege.py`).

Auftrag Markus 01.11.2026 (Vertragsquellen): PDF-Formularwerte fließen über
dieselbe Fundstellen-Zusammenführung ein wie der Textlayer. Ausgewertet wird
ein Widget nur, wenn es (a) über `/Annots` einer ausgewerteten Seite erreicht
wurde (verifizierte Seite) und (b) sein normalisierter Name in der festen
Whitelist `_FORMULARFELDER` steht. Formularinhalte sind reine Daten: unbekannte
Felder werden NICHT geraten, ihre Werte nicht übernommen (nur Name/Seite als
Prüfhinweis). `hat_textlage` beschreibt weiterhin AUSSCHLIESSLICH den
Textlayer. Ein Signaturfeld ist nur ein Prüfhinweis: es wird nicht validiert
und setzt keinerlei "unterschrieben"-Aussage."""

from __future__ import annotations

import io
import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from hashlib import sha256

from pypdf import PasswordType, PdfReader
from pypdf.errors import PdfReadError
from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, NameObject, NumberObject, TextStringObject


class PdfNichtLesbarError(Exception):
    """Datei konnte nicht als PDF geparst werden (beschädigt, entgegen der
    Magic-Byte-Prüfung in `ablage.py` doch kein valides PDF o. Ä.). Ein
    verschlüsseltes PDF ist KEIN Lesefehler, sondern eine offene Stelle
    (`OffeneStelle.grund == "VERSCHLUESSELT"`): die manuelle Erfassung bleibt
    möglich, der Quellhash bleibt erhalten."""


@dataclass(frozen=True)
class FeldTreffer:
    wert: str
    seite: int  # 1-basiert
    auszug: str  # Textumgebung des Treffers als Beleg, gekürzt
    quellart: str = "TEXT"  # TEXT | FORMULAR | TEXT_UND_FORMULAR
    feldname: str | None = None  # normalisierter Whitelist-Name bei FORMULAR
    im_nachtrag: bool = False  # steht hinter einem "Nachtrag"-Hinweis; entscheidet nichts
    #: Alle weiteren Fundstellen desselben (inhaltsgleichen) Werts.
    weitere_belege: tuple["FeldTreffer", ...] = ()


@dataclass(frozen=True)
class OffeneStelle:
    """Etwas im Dokument, das NICHT ausgewertet wurde - nie ein stiller
    Erfolg. `seite=None` betrifft das gesamte Dokument."""

    seite: int | None
    #: VERSCHLUESSELT | NICHT_AUSGEWERTET | KEIN_TEXT | NUR_FORMULAR | LESEFEHLER |
    #: FORMULARWERT_UNLESBAR | FORMULARFELD_OHNE_SEITE | XFA_NICHT_AUSGEWERTET
    grund: str
    text: str


@dataclass(frozen=True)
class FormularfeldHinweis:
    """Nicht auf der Whitelist: nur Name/Seite, der Wert bleibt draußen."""

    name: str
    seite: int


@dataclass(frozen=True)
class SignaturfeldHinweis:
    """Reiner Prüfhinweis. Ein vorhandener Wert ist nicht validiert und
    beweist keine wirksame Unterschrift."""

    name: str
    seite: int
    wert_vorhanden: bool


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
    offene_stellen: tuple[OffeneStelle, ...] = ()
    unbekannte_formularfelder: tuple[FormularfeldHinweis, ...] = ()
    signaturfelder: tuple[SignaturfeldHinweis, ...] = ()
    quelle_sha256: str | None = None


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


# Abnahme-Folgefix (Codex, "Netto/USt/Brutto je Mietkomponente inkl.
# gemischter Steuersätze"): Netto-Betrag und USt-Satz je Mietkomponente
# werden als ZWEI GETRENNTE Muster erfasst (Betrag/Prozentsatz), da ein
# einzelner Treffer immer nur EINEN Wert liefert (siehe `_erste_gruppe`).
# Ein bloßer Brutto-Betrag OHNE das Wort "netto" bleibt unverändert nur
# vom bereits bestehenden Brutto-Muster oben erfasst - NIE doppelt
# gezählt. Beide Muster sind bewusst einfache, dokumentierte Heuristiken
# (siehe Moduldoc) - jeder Treffer bleibt ein bloßer Vorschlag mit
# Seitenbeleg, nie eine ungeprüfte Übernahme.
#
# **Korrektur nach unabhängiger Abnahme (Codex, zweiter Praxistest):**
# "netto" steht in der Praxis GENAUSO oft NACH dem Betrag ("Hauptmietzins
# EUR 1200,00 netto, zuzüglich ...") wie davor ("Hauptmietzins (netto):
# ...") - die vorherige Fassung erkannte NUR die Voranstellung, ein
# nachgestelltes "netto" blieb unentdeckt UND wurde zusätzlich vom
# Brutto-Muster (das versuchte, genau diesen Fall per negativem Lookahead
# auszuschließen, aber nur die Voranstellung kannte) fälschlich als
# Brutto übernommen. Die Brutto-Ausschlussprüfung läuft deshalb jetzt NICHT
# mehr per Lookahead in der Regex selbst, sondern in `vorschlaege.py`
# (Brutto wird NUR übernommen, wenn für dieselbe Komponente KEIN
# Netto-Treffer vorliegt - siehe dort).
def _netto_muster(label: str) -> re.Pattern[str]:
    netto_vor = label + r"[^\d€]{0,10}\(?netto\)?:?[^\d€.]{0,20}" + _BETRAG_FRAGMENT
    netto_nach = label + r"[^\d€.]{0,40}" + _BETRAG_FRAGMENT + r"\s*,?\s*netto\b"
    return re.compile(f"(?:{netto_vor})|(?:{netto_nach})", re.IGNORECASE)


def _ust_muster(label: str) -> re.Pattern[str]:
    return re.compile(
        label + r"[^%]{0,100}?(?:zzgl\.?|zuz(?:ü|ue)glich)[^%\d]{0,10}([0-9]{1,2}(?:,[0-9]{1,2})?)\s*%\s*"
        r"(?:USt\.?|Ust\.?|Umsatzsteuer)",
        re.IGNORECASE,
    )

# Bewusst EINFACHE, dokumentierte Regex-Heuristiken für gängige
# deutschsprachige Vertragsformulierungen - KEIN Anspruch auf
# vollständige Vertragssemantik. Jedes Muster liefert GENAU EINEN
# relevanten Wert über `_erste_gruppe`; ein Muster ohne Klammergruppe
# dient nur als Ja/Nein-Indikator (z. B. Nutzungsart-Hinweise).
_MUSTER: dict[str, re.Pattern[str]] = {
    # Korrektur nach unabhängiger Abnahme (Codex, zweiter Praxistest):
    # "Kaution[^\d€]{0,60}" konnte über einen Satzpunkt HINWEG bis in
    # einen völlig unrelated Folgesatz greifen (z. B. "... die Kaution
    # liegt NICHT bei. Mahngebühr: EUR 20,00 ..." - das "20,00" wurde
    # fälschlich als ZWEITER, widersprüchlicher Kautionsbetrag gewertet
    # und die gesamte Kaution landete dadurch als Mehrdeutigkeit OHNE
    # Vorschlag). Der Satzpunkt `.` ist jetzt explizit AUSGESCHLOSSEN -
    # der Betrag muss im SELBEN Satz wie das Wort "Kaution" stehen.
    "kaution_cent": re.compile(r"Kaution[^\d€.]{0,60}" + _BETRAG_FRAGMENT, re.IGNORECASE),
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
    #
    # **Weitere Korrektur (Abnahme-Folgefix, zweiter Praxistest):** die
    # Brutto-Ausschlussprüfung über einen negativen Lookahead erkannte NUR
    # ein VORANGESTELLTES "netto" - ein NACHGESTELLTES "netto" (siehe
    # `_netto_muster`-Korrektur oben) griff weiterhin zusätzlich hier als
    # vermeintliches Brutto. Diese Muster bleiben deshalb jetzt bewusst
    # EINFACHE Betragserkennung OHNE Lookahead; die Brutto/Netto-
    # Entscheidung fällt ausschließlich in `vorschlaege.py` (Netto-Treffer
    # vorhanden -> Brutto-Treffer für dieselbe Komponente wird NICHT
    # übernommen, siehe dort).
    "hauptmietzins_cent": re.compile(
        r"(?:Hauptmietzins|Miete monatlich)[^\d€.]{0,40}" + _BETRAG_FRAGMENT, re.IGNORECASE,
    ),
    "nettomiete_cent": re.compile(r"Nettomiete[^\d€.]{0,40}" + _BETRAG_FRAGMENT, re.IGNORECASE),
    "betriebskosten_cent": re.compile(r"Betriebskosten[^\d€.]{0,40}" + _BETRAG_FRAGMENT, re.IGNORECASE),
    "heizkosten_cent": re.compile(r"Heizkosten[^\d€.]{0,40}" + _BETRAG_FRAGMENT, re.IGNORECASE),
    "kueche_cent": re.compile(r"K(?:ü|ue)che[^\d€.]{0,40}" + _BETRAG_FRAGMENT, re.IGNORECASE),
    "parkplatz_cent": re.compile(r"Parkplatz[^\d€.]{0,40}" + _BETRAG_FRAGMENT, re.IGNORECASE),
    # Netto + USt-Satz je Mietkomponente (siehe `_netto_muster`/`_ust_muster`
    # oben) - gemischte Steuersätze je Komponente möglich.
    "hauptmietzins_netto_cent": _netto_muster(r"Hauptmietzins"),
    "hauptmietzins_ust_prozent": _ust_muster(r"Hauptmietzins"),
    "betriebskosten_netto_cent": _netto_muster(r"Betriebskosten"),
    "betriebskosten_ust_prozent": _ust_muster(r"Betriebskosten"),
    "heizkosten_netto_cent": _netto_muster(r"Heizkosten"),
    "heizkosten_ust_prozent": _ust_muster(r"Heizkosten"),
    "kueche_netto_cent": _netto_muster(r"K(?:ü|ue)che"),
    "kueche_ust_prozent": _ust_muster(r"K(?:ü|ue)che"),
    "parkplatz_netto_cent": _netto_muster(r"Parkplatz"),
    "parkplatz_ust_prozent": _ust_muster(r"Parkplatz"),
    # Korrektur nach unabhängiger Abnahme (Codex, 20.10.2026): "Mahngebühr"
    # (mit und ohne Plural-"en") war bisher kein erkanntes Synonym -
    # Verträge verwenden beide Begriffe für dieselbe Fachregel.
    "mahngebuehr_cent": re.compile(
        r"(?:Mahnspesen?|Mahngeb(?:ü|ue)hr(?:en)?)[^\d€.]{0,40}" + _BETRAG_FRAGMENT, re.IGNORECASE,
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
    # Abnahme-Folgefix (Codex, "vertraglicher Fälligkeitstag"): bisher
    # fiel `VertragZeile.faelligkeit_tag` beim Intake IMMER auf den
    # Default 5 zurück, unabhängig von einer tatsächlich belegten
    # abweichenden Fälligkeit ("Zahlung ... bis zum 1./15. des
    # Monats") - für Zinsen-/Mahnlauf-Berechnung ist das bei einem
    # Gewerbevertrag mit abweichendem Zahlungstermin FALSCH. Erkennt
    # gängige Formulierungen ("Zahlung monatlich im Voraus bis zum
    # 5. des Monats"/"bis zum 1. eines jeden Monats") - bewusst
    # EINFACHE Heuristik wie alle übrigen Muster hier; bleibt ein
    # Vorschlag, der im Review geprüft/korrigiert wird.
    "faelligkeit_tag": re.compile(
        r"(?:Zahlung|Miete|Mietzins)[^\d.]{0,60}bis zum\s*([0-9]{1,2})\.?[^\d\n]{0,25}?Monats",
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
    # Abnahme-Folgefix (Codex): "Anpassung im Jänner"/"Mindestintervall 12
    # Monate" bisher nicht extrahiert - Monatsname wird in `vorschlaege.py`
    # über die bereits bestehende `_MONATSNAMEN`-Tabelle in 1-12 übersetzt.
    #
    # **Korrektur nach unabhängiger Abnahme (Codex, zweiter Praxistest):**
    # "Anpassung NUR IM Jaenner" wurde nicht erkannt - die vorherige
    # Lücke `[^\wÄÖÜäöüß]{0,20}` erlaubte NUR Nicht-Wortzeichen
    # (Leerzeichen/Doppelpunkt) zwischen "Anpassung" und dem Monatsnamen,
    # aber KEIN dazwischenliegendes Füllwort wie "nur". `[^\n]{0,30}?`
    # (lazy, zeilenbegrenzt) erlaubt jetzt beliebige kurze Zwischentexte.
    "index_anpassungsmonat_name": re.compile(
        r"Anpassung(?:smonat)?[^\n]{0,30}?\b"
        r"(J(?:ä|ae)nner|Januar|Februar|M(?:ä|ae)rz|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember)\b",
        re.IGNORECASE,
    ),
    "index_mindestintervall_monate": re.compile(
        r"Mindest(?:abstand|intervall)[^\d]{0,30}([0-9]{1,2})\s*Monate", re.IGNORECASE,
    ),
    # Tri-State wie `befristet_hinweis`/`unbefristet_hinweis`: "mehr als
    # X %" ist eindeutig EXKLUSIV (die Schwelle muss überschritten, nicht
    # nur erreicht werden), siehe auch `index_schwelle_prozent` oben. Die
    # enge %-Nähe (max. 15 Zeichen) verhindert einen Fehltreffer auf ein
    # unrelated "mehr als" an anderer Stelle im Vertrag.
    "index_schwelle_exklusiv_hinweis": re.compile(r"(?:mehr als|übersteigt(?:\s+um)?)[^%]{0,15}%", re.IGNORECASE),
    "index_schwelle_inklusiv_hinweis": re.compile(
        r"(?:ab (?:einer |der )?(?:Schwankung|Ver(?:ä|ae)nderung)[^%]{0,15}%|ab Erreichen (?:der|einer) Schwelle)",
        re.IGNORECASE,
    ),
    # Abnahme-Folgefix: Verzugszins-Satz (löst GLEICHZEITIG
    # `verzugszins_art=VERTRAGLICH_VEREINBART` in `vorschlaege.py` aus -
    # ein belegter konkreter Satz IST die vertragliche Vereinbarung).
    "verzugszins_satz_prozent": re.compile(
        r"Verzugszins(?:en)?[^\d%]{0,40}([0-9]{1,2}(?:,[0-9]{1,2})?)\s*%", re.IGNORECASE,
    ),
    # Rechtsachse MRG-Anwendungsbereich (Voll/Teil/Ausnahme) - DREI
    # GETRENNTE Hinweismuster wie bei `nutzungsart_*`/`befristet_hinweis`
    # oben; `vorschlaege.py` setzt das Feld NUR bei GENAU EINEM Treffer.
    #
    # **Korrektur nach unabhängiger Abnahme (Codex, zweiter Praxistest):**
    # "MRG-Anwendung: Teilanwendung" (Label-Doppelpunkt-Wert-Schreibweise)
    # wurde bisher NICHT erkannt - nur zusammenhängende Prosa-Varianten
    # wie "MRG-Teilanwendung"/"Teilanwendung des MRG". Der optionale
    # Zwischenteil `(?:Anwendung\s*:?\s*)?` deckt jetzt BEIDE Varianten ab.
    "mrg_vollanwendung_hinweis": re.compile(
        r"(MRG[- ]?(?:Anwendung\s*:?\s*)?Vollanwendung|Vollanwendung des MRG|volle[rn]? Anwendungsbereich des MRG)",
        re.IGNORECASE,
    ),
    "mrg_teilanwendung_hinweis": re.compile(
        r"(MRG[- ]?(?:Anwendung\s*:?\s*)?Teilanwendung|Teilanwendung des MRG|teilweise[rn]? Anwendungsbereich des MRG)",
        re.IGNORECASE,
    ),
    "mrg_ausnahme_hinweis": re.compile(
        r"(MRG[- ]?(?:Anwendung\s*:?\s*)?(?:Voll-?)?Ausnahme|Ausnahme vom MRG|MRG findet keine Anwendung)",
        re.IGNORECASE,
    ),
    # Rechtsachse Mietzinsbildung (frei/Richtwert/Kategorie) - ebenfalls
    # DREI GETRENNTE Hinweismuster, unabhängig von der MRG-Achse oben.
    # Gleiche Korrektur: "Mietzinsbildung: frei vereinbart" (Label-Wert-
    # Schreibweise) jetzt zusätzlich zu den Prosa-Varianten erkannt.
    "mietzinsbildung_frei_hinweis": re.compile(
        r"(Mietzinsbildung\s*:?\s*frei(?:\s+vereinbart)?|freie[r]? Mietzinsbildung|"
        r"frei vereinbarte[rn]? Mietzins|Mietzins (?:wird |ist )?frei vereinbart)",
        re.IGNORECASE,
    ),
    "mietzinsbildung_richtwert_hinweis": re.compile(
        r"(Mietzinsbildung\s*:?\s*Richtwert|Richtwertmietzins|Richtwert-?system|nach dem Richtwert(?:system)?)",
        re.IGNORECASE,
    ),
    "mietzinsbildung_kategorie_hinweis": re.compile(
        r"(Mietzinsbildung\s*:?\s*Kategorie|Kategoriemietzins|Kategorie-?system|nach der Kategorie(?:system)?)",
        re.IGNORECASE,
    ),
    # Abnahme-Folgefix (Codex, zweiter Praxistest): "Zustellanschrift:
    # Musterweg 7, Top 8, 1000 Musterort" wurde bisher GAR NICHT
    # extrahiert - `neuer_debitor_adresse` hatte keine PDF-Quelle. Der
    # Rest der Zeile (bis zum Zeilenende) wird als Rohvorschlag
    # übernommen - bleibt wie jeder andere Fund vollständig editierbar.
    "mieter_adresse": re.compile(
        r"(?:Zustellanschrift|Anschrift (?:des )?Mieters?|Adresse (?:des )?Mieters?)\s*:?\s*([^\n]{3,120})",
        re.IGNORECASE,
    ),
    # Reiner Hinweis (Auftrag 01.11.2026): mehrere Einheiten bleiben alle
    # sichtbar, die Zuordnung zur gewählten Einheit bleibt menschlich.
    "einheit_bezeichnung": re.compile(r"\b(Top\s*(?:Nr\.?\s*)?[0-9]+[A-Za-z]?(?:/[0-9]+)?)\b"),
    "nutzungsart_wohnung": re.compile(r"(Wohnungsmietvertrag|zu Wohnzwecken)", re.IGNORECASE),
    "nutzungsart_geschaeft": re.compile(r"(Gesch(?:ä|ae)ftsraummietvertrag|Gesch(?:ä|ae)ftslokal)", re.IGNORECASE),
    "nutzungsart_buero": re.compile(r"(B(?:ü|ue)romietvertrag|als B(?:ü|ue)ro vermietet)", re.IGNORECASE),
    # Auftrag HV-20261020-VERTRAGSDATENBASIS - Termine GETRENNT von
    # Mietbeginn/-ende (siehe `vorschlaege.py`):
    # Abnahme-Folgefix (Codex, zweiter Praxistest): "Unterzeichnung am
    # 01.06.2026" (Substantiv statt Verbform) wurde bisher NICHT erkannt -
    # nur "unterzeichnet am"/"unterschrieben am"/"Datum der
    # Unterzeichnung" waren als Auslöser hinterlegt.
    "unterzeichnungsdatum": re.compile(
        r"(?:unterzeichnet am|unterschrieben am|Datum der Unterzeichnung|Unterzeichnung am)[^\d]{0,20}(\d{1,2}\.\d{1,2}\.\d{4})",
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


def _betrag_in_cent(wert: str) -> int | None:
    """Nur für den Vergleich und ausschließlich ganzzahlig (nie float):
    "1.500,00", "1500,00" und "1500" sind derselbe Betrag."""

    treffer = re.fullmatch(r"([0-9]+(?:[.,][0-9]{3})*)(?:[.,]([0-9]{2}))?", wert.strip())
    if treffer is None:
        return None
    euro = re.sub(r"[.,]", "", treffer.group(1))
    return int(euro) * 100 + int(treffer.group(2) or 0)


def _datum_oder_none(wert: str) -> date | None:
    treffer = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", wert.strip())
    if treffer is None:
        return None
    try:
        return date(int(treffer.group(3)), int(treffer.group(2)), int(treffer.group(1)))
    except ValueError:
        return None


def _dezimal_oder_none(wert: str) -> Decimal | None:
    if not re.fullmatch(r"[0-9]+(?:[.,][0-9]+)?", wert.strip()):
        return None
    try:
        return Decimal(wert.strip().replace(",", "."))
    except InvalidOperation:
        return None


_DATUMSFELDER = frozenset({"mietbeginn", "mietende", "unterzeichnungsdatum", "uebergabe_am"})
_ZAHLENFELDER = frozenset({
    "index_schwelle_prozent", "index_urspruenglicher_basiswert", "verzugszins_satz_prozent",
    "hauptmietzins_ust_prozent", "betriebskosten_ust_prozent", "heizkosten_ust_prozent",
    "kueche_ust_prozent", "parkplatz_ust_prozent", "faelligkeit_tag", "index_mindestintervall_monate",
})


def _ist_ja_hinweis(feld: str) -> bool:
    """Felder, die nur "kommt vor" belegen: ein Häkchen bestätigt den Hinweis,
    ohne selbst einen abweichenden Wortlaut einzubringen."""

    return feld.endswith("_hinweis") or feld.startswith("nutzungsart_") or feld == "mahngebuehr_keine"


def _vergleichswert(feld: str, wert: str) -> str:
    """Inhaltlicher Vergleich quer über Text und Formular. Nicht erkannte
    Formate fallen auf den Wortlaut zurück und bleiben dadurch ein sichtbarer
    Unterschied statt einer stillen Gleichsetzung."""

    if feld.endswith("_cent"):
        cent = _betrag_in_cent(wert)
        if cent is not None:
            return f"cent:{cent}"
    elif feld in _DATUMSFELDER:
        datum = _datum_oder_none(wert)
        if datum is not None:
            return datum.isoformat()
    elif feld in _ZAHLENFELDER:
        zahl = _dezimal_oder_none(wert)
        if zahl is not None:
            return f"zahl:{zahl.normalize():f}"
    elif feld in ("mieter_telefon", "mitmieter_telefon"):
        return re.sub(r"[\s/().-]", "", wert)
    return _normalisiere(wert)


def _auszug(text: str, start: int, ende: int) -> str:
    von = max(0, start - 80)
    bis = min(len(text), ende + 80)
    return text[von:bis].replace("\n", " ").strip()[:_MAX_AUSZUG_LAENGE]


# -- PDF-Formularfelder --------------------------------------------------------

_TYP_BETRAG = "BETRAG"
_TYP_DATUM = "DATUM"
_TYP_PROZENT = "PROZENT"
_TYP_NAME = "NAME"
_TYP_EMAIL = "EMAIL"
_TYP_TELEFON = "TELEFON"
_TYP_ADRESSE = "ADRESSE"
_TYP_TAG = "TAG"
_TYP_GANZZAHL = "GANZZAHL"
_TYP_DEZIMAL = "DEZIMAL"
_TYP_INDEXREIHE = "INDEXREIHE"
_TYP_BASISMONAT = "BASISMONAT"
_TYP_MONAT = "MONAT"
_TYP_EINHEIT = "EINHEIT"
_TYP_HAKEN = "HAKEN"

_MONATE = (
    r"J(?:ä|ae)nner|Januar|Februar|M(?:ä|ae)rz|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember"
)


def _komponenten_formularfelder() -> dict[str, tuple[str, str]]:
    felder: dict[str, tuple[str, str]] = {}
    for name in ("hauptmietzins", "betriebskosten", "heizkosten", "kueche", "parkplatz"):
        felder[name] = (f"{name}_cent", _TYP_BETRAG)
        felder[f"{name}_brutto"] = (f"{name}_cent", _TYP_BETRAG)
        felder[f"{name}_netto"] = (f"{name}_netto_cent", _TYP_BETRAG)
        felder[f"{name}_ust"] = (f"{name}_ust_prozent", _TYP_PROZENT)
    return felder


#: Bewusst explizit und abschließend: normalisierter Feldname -> (Extraktions-
#: schlüssel, Werttyp). Was hier fehlt, wird NICHT aus dem Namen erraten.
_FORMULARFELDER: dict[str, tuple[str, str]] = {
    **_komponenten_formularfelder(),
    "kaution": ("kaution_cent", _TYP_BETRAG),
    "kautionsbetrag": ("kaution_cent", _TYP_BETRAG),
    "nettomiete": ("nettomiete_cent", _TYP_BETRAG),
    "mahngebuehr": ("mahngebuehr_cent", _TYP_BETRAG),
    "mahnspesen": ("mahngebuehr_cent", _TYP_BETRAG),
    "mieter": ("mieter_name", _TYP_NAME),
    "mieter_name": ("mieter_name", _TYP_NAME),
    "name_mieter": ("mieter_name", _TYP_NAME),
    "vermieter": ("vermieter_name", _TYP_NAME),
    "vermieter_name": ("vermieter_name", _TYP_NAME),
    "name_vermieter": ("vermieter_name", _TYP_NAME),
    "mitmieter": ("mitmieter_name", _TYP_NAME),
    "mitmieter_name": ("mitmieter_name", _TYP_NAME),
    "mieter_email": ("mieter_email", _TYP_EMAIL),
    "email_mieter": ("mieter_email", _TYP_EMAIL),
    "e_mail_mieter": ("mieter_email", _TYP_EMAIL),
    "mieter_telefon": ("mieter_telefon", _TYP_TELEFON),
    "telefon_mieter": ("mieter_telefon", _TYP_TELEFON),
    "mitmieter_email": ("mitmieter_email", _TYP_EMAIL),
    "email_mitmieter": ("mitmieter_email", _TYP_EMAIL),
    "mitmieter_telefon": ("mitmieter_telefon", _TYP_TELEFON),
    "telefon_mitmieter": ("mitmieter_telefon", _TYP_TELEFON),
    "mieter_adresse": ("mieter_adresse", _TYP_ADRESSE),
    "anschrift_mieter": ("mieter_adresse", _TYP_ADRESSE),
    "zustellanschrift": ("mieter_adresse", _TYP_ADRESSE),
    "einheit": ("einheit_bezeichnung", _TYP_EINHEIT),
    "top": ("einheit_bezeichnung", _TYP_EINHEIT),
    "top_nr": ("einheit_bezeichnung", _TYP_EINHEIT),
    "mietbeginn": ("mietbeginn", _TYP_DATUM),
    "mietende": ("mietende", _TYP_DATUM),
    "unterzeichnungsdatum": ("unterzeichnungsdatum", _TYP_DATUM),
    "uebergabe_am": ("uebergabe_am", _TYP_DATUM),
    "uebergabedatum": ("uebergabe_am", _TYP_DATUM),
    "faelligkeit_tag": ("faelligkeit_tag", _TYP_TAG),
    "index_reihe": ("index_reihe", _TYP_INDEXREIHE),
    "indexreihe": ("index_reihe", _TYP_INDEXREIHE),
    "index_schwelle": ("index_schwelle_prozent", _TYP_PROZENT),
    "index_schwelle_prozent": ("index_schwelle_prozent", _TYP_PROZENT),
    "index_basismonat": ("index_urspruenglicher_basismonat", _TYP_BASISMONAT),
    "index_basiswert": ("index_urspruenglicher_basiswert", _TYP_DEZIMAL),
    "index_anpassungsmonat": ("index_anpassungsmonat_name", _TYP_MONAT),
    "index_mindestintervall_monate": ("index_mindestintervall_monate", _TYP_GANZZAHL),
    "verzugszins_satz": ("verzugszins_satz_prozent", _TYP_PROZENT),
    "verzugszins_prozent": ("verzugszins_satz_prozent", _TYP_PROZENT),
    # Häkchen: nur ein gesetzter Zustand belegt etwas, "Off" belegt nichts.
    "befristet": ("befristet_hinweis", _TYP_HAKEN),
    "unbefristet": ("unbefristet_hinweis", _TYP_HAKEN),
    "wohnung": ("nutzungsart_wohnung", _TYP_HAKEN),
    "buero": ("nutzungsart_buero", _TYP_HAKEN),
    "geschaeftslokal": ("nutzungsart_geschaeft", _TYP_HAKEN),
    "keine_mahngebuehr": ("mahngebuehr_keine", _TYP_HAKEN),
}

#: Auswahlfelder (Radio/Choice): Feldname -> {normalisierter Wert -> Schlüssel}.
_AUSWAHLFELDER: dict[str, dict[str, str]] = {
    "nutzungsart": {
        "wohnung": "nutzungsart_wohnung", "buero": "nutzungsart_buero", "geschaeftslokal": "nutzungsart_geschaeft",
    },
    "befristung": {"befristet": "befristet_hinweis", "unbefristet": "unbefristet_hinweis"},
}

#: Technische Container in Feldhierarchien; nur diese dürfen wegfallen.
_GENERISCHE_FELDCONTAINER = re.compile(r"(?:topmostsubform|form(?:ular)?\d*|page\d*|seite\d*)")
_MAX_FORMULARWERT = 500
_MAX_WIDGETS = 5000
_MAX_FELDTIEFE = 32


def _name_normalisiert(teil: str) -> str:
    teil = unicodedata.normalize("NFKC", teil).casefold()
    for von, nach in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        teil = teil.replace(von, nach)
    return re.sub(r"[^a-z0-9]+", "_", teil).strip("_")


def _feld_aufloesen(voller_name: str) -> tuple[str, str, bool] | None:
    """Liefert (Whitelist-Name, Anzeigename, im_nachtrag) oder `None`. Löst
    nur explizit erlaubte Abwandlungen auf: technische Container, ein Präfix
    `nachtrag_` und eine laufende Nummer (`mieter_2`) - mehrere nummerierte
    Felder bleiben dadurch mehrere Fundstellen desselben Schlüssels."""

    teile = [_name_normalisiert(re.sub(r"\[\d+\]", "", t)) for t in voller_name.split(".")]
    teile = [t for t in teile if t]
    if not teile:
        return None
    while len(teile) > 1 and _GENERISCHE_FELDCONTAINER.fullmatch(teile[0]):
        teile = teile[1:]
    kandidat = "_".join(teile)
    anzeige = kandidat[:80]
    im_nachtrag = False
    if kandidat.startswith("nachtrag_"):
        im_nachtrag = True
        kandidat = kandidat[len("nachtrag_"):]
    if kandidat in _FORMULARFELDER or kandidat in _AUSWAHLFELDER:
        return kandidat, anzeige, im_nachtrag
    nummeriert = re.fullmatch(r"(.+?)_?\d{1,2}", kandidat)
    if nummeriert and nummeriert.group(1) in _FORMULARFELDER:
        return nummeriert.group(1), anzeige, im_nachtrag
    return None


def _wert_normalisiert(typ: str, roh: str) -> str | None:
    """Prüft den GESAMTEN Wert gegen das Format des Typs. Passt er nicht,
    gibt es keinen Teiltreffer und keine Korrektur (`None` -> offene Stelle)."""

    wert = roh.strip()
    if typ == _TYP_ADRESSE:
        wert = re.sub(r"\s*[\r\n]+\s*", ", ", wert)
        return wert if re.fullmatch(r"[^\x00-\x1f<>]{3,160}", wert) else None
    if re.search(r"[\x00-\x1f]", wert):
        return None
    if typ == _TYP_BETRAG:
        treffer = re.fullmatch(
            r"(?:€|EUR)?\s*((?:[0-9]+|[0-9]{1,3}(?:\.[0-9]{3})+)(?:,[0-9]{1,2})?|[0-9]+\.[0-9]{2})\s*(?:€|EUR)?", wert,
        )
        if treffer is None:
            return None
        zahl = treffer.group(1)
        if "," in zahl:
            euro, cent = zahl.split(",")
            cent = cent.ljust(2, "0")
        elif re.fullmatch(r"[0-9]+\.[0-9]{2}", zahl):
            euro, cent = zahl.split(".")
        else:
            euro, cent = zahl, "00"
        return f"{euro.replace('.', '')},{cent}"
    if typ == _TYP_DATUM:
        datum = _datum_oder_none(wert)
        if datum is None:
            try:
                datum = date.fromisoformat(wert)
            except ValueError:
                return None
        return datum.strftime("%d.%m.%Y")
    if typ == _TYP_PROZENT:
        wert = re.sub(r"\s*%$", "", wert)
        return wert if re.fullmatch(r"[0-9]{1,2}(?:[.,][0-9]{1,3})?", wert) else None
    if typ == _TYP_NAME:
        return wert if re.fullmatch(r"[^\W\d_][\w .,&'-]{1,118}", wert) else None
    if typ == _TYP_EMAIL:
        return wert if re.fullmatch(r"[\w.+-]+@[\w.-]+\.\w+", wert) else None
    if typ == _TYP_TELEFON:
        return wert if re.fullmatch(r"[0-9+()/ .-]{6,25}", wert) else None
    if typ == _TYP_TAG:
        return wert if re.fullmatch(r"[0-9]{1,2}", wert) and 1 <= int(wert) <= 31 else None
    if typ == _TYP_GANZZAHL:
        return wert if re.fullmatch(r"[0-9]{1,3}", wert) else None
    if typ == _TYP_DEZIMAL:
        return wert if re.fullmatch(r"[0-9]{1,8}(?:[.,][0-9]{1,4})?", wert) else None
    if typ == _TYP_INDEXREIHE:
        return wert if re.fullmatch(r"(?:Verbraucherpreisindex|VPI)\s*\d{4}", wert, re.IGNORECASE) else None
    if typ == _TYP_BASISMONAT:
        return wert if re.fullmatch(rf"(?:{_MONATE})\s*\d{{4}}|\d{{1,2}}[./]\d{{4}}", wert, re.IGNORECASE) else None
    if typ == _TYP_MONAT:
        return wert if re.fullmatch(_MONATE, wert, re.IGNORECASE) else None
    if typ == _TYP_EINHEIT:
        return wert if re.fullmatch(r"[\w ./-]{1,40}", wert) else None
    return None


def _objekt(wert):
    return wert.get_object() if wert is not None else None


def _roh(dictionary: DictionaryObject, schluessel: str):
    return dictionary.raw_get(schluessel) if schluessel in dictionary else None


def _vererbt(feld: DictionaryObject, schluessel: str):
    aktuell, tiefe = feld, 0
    while isinstance(aktuell, DictionaryObject) and tiefe < _MAX_FELDTIEFE:
        if schluessel in aktuell:
            return _objekt(_roh(aktuell, schluessel))
        aktuell, tiefe = _objekt(_roh(aktuell, "/Parent")), tiefe + 1
    return None


def _voller_feldname(feld: DictionaryObject) -> str:
    teile: list[str] = []
    aktuell, tiefe = feld, 0
    while isinstance(aktuell, DictionaryObject) and tiefe < _MAX_FELDTIEFE:
        teil = _objekt(_roh(aktuell, "/T"))
        if isinstance(teil, str) and teil:
            teile.append(str(teil))
        aktuell, tiefe = _objekt(_roh(aktuell, "/Parent")), tiefe + 1
    return ".".join(reversed(teile))


def _skalarer_wert(wert) -> str | None:
    if isinstance(wert, NameObject):
        return str(wert)[1:]
    if isinstance(wert, (TextStringObject, NumberObject)):
        return str(wert)
    return None


def _widgets(seite) -> list[tuple[DictionaryObject, int | None]]:
    anmerkungen = _objekt(_roh(seite, "/Annots"))
    if not isinstance(anmerkungen, ArrayObject):
        return []
    ergebnis = []
    for roh in list.__iter__(anmerkungen):
        widget = _objekt(roh)
        if isinstance(widget, DictionaryObject) and _objekt(_roh(widget, "/Subtype")) == "/Widget":
            ergebnis.append((widget, roh.idnum if isinstance(roh, IndirectObject) else None))
    return ergebnis


def _felder_ohne_seite(reader: PdfReader, gesehen: set[int]) -> tuple[list[str], bool]:
    """Befüllte AcroForm-Felder ohne Widget auf einer ausgewerteten Seite:
    ihr Wert hat keinen verifizierten Ort und wird nicht übernommen."""

    wurzel = reader.trailer["/Root"]
    acroform = _objekt(_roh(wurzel, "/AcroForm"))
    if not isinstance(acroform, DictionaryObject):
        return [], False
    hat_xfa = "/XFA" in acroform
    felder = _objekt(_roh(acroform, "/Fields"))
    namen: list[str] = []
    if not isinstance(felder, ArrayObject):
        return namen, hat_xfa
    besucht: set[int] = set()
    stapel = [(roh, 0) for roh in list.__iter__(felder)]
    while stapel and len(besucht) < _MAX_WIDGETS:
        roh, tiefe = stapel.pop()
        kennung = roh.idnum if isinstance(roh, IndirectObject) else None
        if kennung is not None:
            if kennung in besucht:
                continue
            besucht.add(kennung)
        feld = _objekt(roh)
        if not isinstance(feld, DictionaryObject) or tiefe > _MAX_FELDTIEFE:
            continue
        kinder = _objekt(_roh(feld, "/Kids"))
        kinder_roh = list(list.__iter__(kinder)) if isinstance(kinder, ArrayObject) else []
        unterfelder = [k for k in kinder_roh if isinstance(_objekt(k), DictionaryObject) and "/T" in _objekt(k)]
        if unterfelder:
            stapel.extend((k, tiefe + 1) for k in unterfelder)
            continue
        kennungen = {k.idnum for k in kinder_roh if isinstance(k, IndirectObject)}
        if kennung is not None:
            kennungen.add(kennung)
        if not kennungen & gesehen and _vererbt(feld, "/V") not in (None, ""):
            namen.append(_voller_feldname(feld)[:80])
    return sorted(namen), hat_xfa


@dataclass
class _Formularstand:
    funde: dict[str, list[FeldTreffer]]
    offen: list[OffeneStelle]
    unbekannt: list[FormularfeldHinweis]
    signaturen: list[SignaturfeldHinweis]
    gesehen: set[int]


def _lese_widget(
    widget: DictionaryObject, seite_nr: int, nachtrag_ab_seite: int | None, stand: _Formularstand,
) -> None:
    voller_name = _voller_feldname(widget)
    feldtyp = _vererbt(widget, "/FT")
    wert = _vererbt(widget, "/V")
    if feldtyp == "/Sig":
        stand.signaturen.append(SignaturfeldHinweis(voller_name[:80], seite_nr, wert is not None))
        return
    if feldtyp == "/Btn":
        flags = _vererbt(widget, "/Ff")
        if isinstance(flags, int) and flags & 65536:  # reine Schaltfläche, kein Wert
            return
        zustand = _objekt(_roh(widget, "/AS"))
        if isinstance(zustand, NameObject) and zustand == "/Off":
            return
        if not isinstance(wert, NameObject) or wert == "/Off":
            return
    roh = _skalarer_wert(wert)
    if wert is None or roh == "":
        return

    aufgeloest = _feld_aufloesen(voller_name)
    if aufgeloest is None:
        stand.unbekannt.append(FormularfeldHinweis(voller_name[:80], seite_nr))
        return
    name, anzeige, nachtrag_im_namen = aufgeloest
    unlesbar = OffeneStelle(
        seite_nr, "FORMULARWERT_UNLESBAR", f"Formularfeld „{anzeige}“: Wert hat nicht das erwartete Format, nicht übernommen.",
    )
    if roh is None or len(roh) > _MAX_FORMULARWERT:
        stand.offen.append(unlesbar)
        return

    if name in _AUSWAHLFELDER:
        feld = _AUSWAHLFELDER[name].get(_name_normalisiert(roh))
        normalisiert = _name_normalisiert(roh) if feld else None
    else:
        feld, typ = _FORMULARFELDER[name]
        if typ == _TYP_HAKEN:
            normalisiert = name if feldtyp == "/Btn" else None
        else:
            normalisiert = _wert_normalisiert(typ, roh) if feldtyp in ("/Tx", "/Ch") else None
    if feld is None or normalisiert is None:
        stand.offen.append(unlesbar)
        return
    stand.funde.setdefault(feld, []).append(FeldTreffer(
        wert=normalisiert, seite=seite_nr, auszug=f"Formularfeld „{anzeige}“: {normalisiert}"[:_MAX_AUSZUG_LAENGE],
        quellart="FORMULAR", feldname=name,
        im_nachtrag=nachtrag_im_namen or (nachtrag_ab_seite is not None and seite_nr >= nachtrag_ab_seite),
    ))


def _lese_formular(reader: PdfReader, seiten: list, nachtrag_ab_seite: int | None) -> _Formularstand:
    stand = _Formularstand(funde={}, offen=[], unbekannt=[], signaturen=[], gesehen=set())
    gelesen = 0
    for seiten_index, seite in enumerate(seiten):
        seite_nr = seiten_index + 1
        try:
            for widget, kennung in _widgets(seite):
                gelesen += 1
                if gelesen > _MAX_WIDGETS:
                    stand.offen.append(OffeneStelle(
                        seite_nr, "LESEFEHLER", f"Mehr als {_MAX_WIDGETS} Formularfelder: Rest nicht ausgewertet.",
                    ))
                    return stand
                if kennung is not None:
                    stand.gesehen.add(kennung)
                _lese_widget(widget, seite_nr, nachtrag_ab_seite, stand)
        except Exception:
            stand.offen.append(OffeneStelle(seite_nr, "LESEFEHLER", "Formularfelder dieser Seite konnten nicht gelesen werden."))
    try:
        ohne_seite, hat_xfa = _felder_ohne_seite(reader, stand.gesehen)
    except Exception:
        stand.offen.append(OffeneStelle(None, "LESEFEHLER", "Formularstruktur konnte nicht vollständig gelesen werden."))
        return stand
    if hat_xfa:
        stand.offen.append(OffeneStelle(None, "XFA_NICHT_AUSGEWERTET", "Dokument enthält XFA-Formulardaten; diese wurden nicht ausgewertet."))
    for name in ohne_seite:
        stand.offen.append(OffeneStelle(
            None, "FORMULARFELD_OHNE_SEITE", f"Formularfeld „{name}“ hat auf keiner ausgewerteten Seite ein Widget: Wert nicht übernommen.",
        ))
    return stand


def _zusammenfuehren(feld: str, funde: list[FeldTreffer]) -> tuple[FeldTreffer | None, list[FeldTreffer] | None]:
    """Gleicher Inhalt: EIN Treffer mit allen Belegen. Unterschiedlicher
    Inhalt (auch Text gegen Formular oder Nachtrag): kein Treffer, alle
    Fundstellen bleiben erhalten - nie "erster gewinnt", nie "Nachtrag gewinnt"."""

    if _ist_ja_hinweis(feld):
        # Ein Häkchen bestätigt nur; Unterschiede im Textwortlaut bleiben wie bisher.
        distinkt = {_vergleichswert(feld, f.wert) for f in funde if f.quellart == "TEXT"} or {"JA"}
    else:
        distinkt = {_vergleichswert(feld, f.wert) for f in funde}
    if len(distinkt) > 1:
        return None, funde
    arten = {f.quellart for f in funde}
    quellart = "TEXT_UND_FORMULAR" if len(arten) > 1 else funde[0].quellart
    return replace(funde[0], quellart=quellart, weitere_belege=tuple(funde[1:])), None


_NACHTRAG = re.compile(r"\bNachtrag\b", re.IGNORECASE)


def extrahiere(rohbytes: bytes, *, max_seiten: int) -> ExtraktionsErgebnis:
    """Liest Text seitenweise aus (kein OCR - ein gescanntes Bild ohne
    Textlayer liefert leeren Text), sucht die `_MUSTER` je Feld über ALLE
    ausgewerteten Seiten hinweg und ergänzt die Whitelist-Formularfelder.
    Anders als ein "erster Treffer gewinnt"-Ansatz werden ALLE Fundstellen
    gesammelt: liefern sie denselben Wert, wird GENAU EIN `FeldTreffer`
    vorgeschlagen; widersprechen sie sich, landet das Feld in
    `mehrdeutige_treffer` mit allen Fundstellen und OHNE Formularvorschlag
    (siehe Moduldoc)."""

    quelle_sha256 = sha256(rohbytes).hexdigest()
    try:
        reader = PdfReader(io.BytesIO(rohbytes))
    except (PdfReadError, ValueError, KeyError) as exc:
        raise PdfNichtLesbarError(f"PDF konnte nicht gelesen werden: {exc}") from exc

    if reader.is_encrypted:
        try:
            entschluesselt = reader.decrypt("") != PasswordType.NOT_DECRYPTED
        except Exception:
            entschluesselt = False
        if not entschluesselt:
            meldung = "PDF ist passwortgeschützt - kein automatischer Zugriff auf Text oder Formularfelder, bitte vollständig manuell erfassen."
            return ExtraktionsErgebnis(
                seiten_anzahl=0, ausgewertete_seiten=0, hat_textlage=False, treffer={}, mehrdeutige_treffer={},
                warnungen=(meldung,), offene_stellen=(OffeneStelle(None, "VERSCHLUESSELT", meldung),),
                quelle_sha256=quelle_sha256,
            )

    try:
        alle_seiten = reader.pages
        seiten_anzahl = len(alle_seiten)
    except (PdfReadError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise PdfNichtLesbarError(f"PDF konnte nicht gelesen werden: {exc}") from exc
    warnungen: list[str] = []
    offen: list[OffeneStelle] = []
    ausgewertete_seiten = min(seiten_anzahl, max_seiten)
    if seiten_anzahl > max_seiten:
        warnungen.append(
            f"Dokument hat {seiten_anzahl} Seiten, es wurden nur die ersten {max_seiten} ausgewertet."
        )
        offen.extend(
            OffeneStelle(nr, "NICHT_AUSGEWERTET", "Seitenlimit überschritten: Seite wurde nicht ausgewertet.")
            for nr in range(ausgewertete_seiten + 1, seiten_anzahl + 1)
        )

    seiten = list(alle_seiten[:ausgewertete_seiten])
    seiten_texte: list[str] = []
    fehlgeschlagene_seiten = 0
    for nr, seite in enumerate(seiten, start=1):
        try:
            seiten_texte.append(seite.extract_text() or "")
        except Exception:
            seiten_texte.append("")
            fehlgeschlagene_seiten += 1
            offen.append(OffeneStelle(nr, "LESEFEHLER", "Text dieser Seite konnte nicht gelesen werden."))
    if fehlgeschlagene_seiten:
        warnungen.append(
            f"Auf {fehlgeschlagene_seiten} von {ausgewertete_seiten} ausgewerteten Seite(n) konnte kein Text "
            "gelesen werden (übersprungen, NICHT als vollständig erfolgreich ausgegeben)."
        )

    gesamtlaenge = sum(len(t.strip()) for t in seiten_texte)
    hat_textlage = gesamtlaenge >= _MINDEST_TEXTLAENGE_FUER_TEXTLAGE

    nachtrag_stelle = next(
        ((i, m.start()) for i, t in enumerate(seiten_texte) for m in [_NACHTRAG.search(t)] if m), None,
    ) if hat_textlage else None
    formular = _lese_formular(reader, seiten, nachtrag_stelle[0] + 1 if nachtrag_stelle else None)
    offen.extend(formular.offen)

    seiten_mit_formularwert = {f.seite for funde in formular.funde.values() for f in funde}
    lesefehler_seiten = {o.seite for o in offen if o.grund == "LESEFEHLER"}
    leere_seiten = []
    for nr, text in enumerate(seiten_texte, start=1):
        if len(text.strip()) >= _MINDEST_TEXTLAENGE_FUER_TEXTLAGE or nr in lesefehler_seiten:
            continue
        leere_seiten.append(nr)
        offen.append(
            OffeneStelle(nr, "NUR_FORMULAR", "Seite ohne Textlayer: nur Formularfelder ausgewertet, übriger Inhalt ungeprüft.")
            if nr in seiten_mit_formularwert else
            OffeneStelle(nr, "KEIN_TEXT", "Seite ohne auswertbaren Text (vermutlich Scan): nicht ausgewertet.")
        )
    if not hat_textlage:
        warnungen.append(
            "Kein auswertbarer Textlayer gefunden - Belegwerte stammen ausschließlich aus Formularfeldern, "
            "bitte den übrigen Inhalt manuell prüfen."
            if formular.funde else
            "Kein auswertbarer Textlayer gefunden (vermutlich gescanntes Dokument ohne OCR) - "
            "es konnten KEINE Felder automatisch vorgeschlagen werden, bitte vollständig manuell erfassen."
        )
    elif leere_seiten:
        warnungen.append(
            f"Seite(n) {', '.join(map(str, leere_seiten))} ohne auswertbaren Text - Inhalt ungeprüft, bitte manuell prüfen."
        )
    if nachtrag_stelle is not None:
        warnungen.append(
            f"Dokument enthält ab Seite {nachtrag_stelle[0] + 1} einen Nachtrag-Hinweis - Rangfolge der Regelungen bitte manuell prüfen."
        )

    funde_je_feld: dict[str, list[FeldTreffer]] = {}
    if hat_textlage:
        for feld, muster in _MUSTER.items():
            for seiten_index, text in enumerate(seiten_texte):
                for match in muster.finditer(text):
                    im_nachtrag = nachtrag_stelle is not None and (seiten_index, match.start()) >= nachtrag_stelle
                    funde_je_feld.setdefault(feld, []).append(FeldTreffer(
                        wert=_erste_gruppe(match), seite=seiten_index + 1,
                        auszug=_auszug(text, match.start(), match.end()), im_nachtrag=im_nachtrag,
                    ))
    for feld, funde in formular.funde.items():
        funde_je_feld.setdefault(feld, []).extend(funde)

    treffer: dict[str, FeldTreffer] = {}
    mehrdeutige_treffer: dict[str, list[FeldTreffer]] = {}
    for feld, funde in funde_je_feld.items():
        einzeln, mehrere = _zusammenfuehren(feld, funde)
        if einzeln is not None:
            treffer[feld] = einzeln
        else:
            mehrdeutige_treffer[feld] = mehrere

    return ExtraktionsErgebnis(
        seiten_anzahl=seiten_anzahl, ausgewertete_seiten=ausgewertete_seiten,
        hat_textlage=hat_textlage, treffer=treffer, mehrdeutige_treffer=mehrdeutige_treffer,
        warnungen=tuple(warnungen), offene_stellen=tuple(offen),
        unbekannte_formularfelder=tuple(formular.unbekannt), signaturfelder=tuple(formular.signaturen),
        quelle_sha256=quelle_sha256,
    )
