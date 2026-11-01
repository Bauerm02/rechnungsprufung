"""Objektgebundene Mieterantwort-ENTWÜRFE - deterministisch, ohne LLM, ohne
Speicherung und ohne Versand (Auftrag Markus 01.11.2026).

Eine Frage ist reine Eingabe: sie wird nie als Anweisung befolgt, und feste
Muster entscheiden nur, welcher belegte Fakt in Frage kommt. Das ist KEIN
Textverständnis; eine Musterzuordnung behauptet keine umfassende Antwort und
jeder Entwurf muss vor Verwendung von einem Menschen geprüft werden.

Regeln:

- Ohne bestätigten Bezug auf GENAU diesen Vertrag gehen alle Fragen in die
  manuelle Prüfung. Namen werden nie ausgewertet. Nennt eine Frage eine andere
  Vertragskennung, Einheit oder ein anderes Objekt, geht sie in die manuelle
  Prüfung, ohne dass geprüft wird, ob es das Genannte gibt.
- Jede Teilfrage bleibt erhalten und endet entweder mit Beleg oder als offen.
- Anweisungsversuche, Rechtsfragen, Fristen/Zusagen, Bankdaten und fremde
  Parteien werden nie beantwortet, auch wenn die Frage daneben ein belegbares
  Thema nennt.
- Unbekanntes bleibt unbekannt, niemals 0. Der technische `gueltig_von` und der
  Default-`faelligkeit_tag` des Vertrags sind keine Vertragsbelege und werden
  nicht als Antwort verwendet.
- Finanzfragen zeigen nur den gebuchten Stand aus der bestehenden Saldorechnung
  samt ausdrücklicher Abdeckungsgrenze, nie eine pauschale Zahlungsaussage."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from mietinkasso.abgleichstatus.kontoabdeckung import STATUS_VOLLSTAENDIG_BELEGT, VertragsCheck
from mietinkasso.bank.referenzen import parse_vertragskennungen
from mietinkasso.domain.money import cents_to_decimal

STATUS_BELEGT = "BELEGT"
STATUS_BELEGT_MIT_GRENZE = "BELEGT_MIT_GRENZE"
STATUS_OFFEN = "OFFEN"
STATUS_MANUELL = "MANUELLE_PRUEFUNG"

MAX_EINGABE_ZEICHEN = 20_000
MAX_TEILFRAGEN = 50

_AUFZAEHLUNG = re.compile(r"^\s*(?:[-*•–]|\d{1,2}[.)])\s+")
_STEUERZEICHEN = re.compile(r"[\x00-\x1f\x7f  ]+")

_GESPERRT = (
    ("ANWEISUNG", "Enthält eine Anweisung an das System; wird nicht befolgt.", re.compile(
        r"ignorier|vergiss|disregard|ignore\b|system\s*-?prompt|\bprompt\b|anweisung|instruction|du bist jetzt|ab sofort"
        r"|best(?:ä|ae)tige\b|schreib(?:e)?\s+(?:mir\s+|bitte\s+)*(?:dass|,)|antworte\s+(?:nur|mit)|<\s*/?\s*[a-z!]",
        re.IGNORECASE,
    )),
    ("RECHT", "Rechtsfrage: wird nicht aus Stammdaten beantwortet.", re.compile(
        r"k(?:ü|ue)ndig|recht(?:s|lich|m(?:ä|ae)(?:ß|ss)ig|ens)|anwalt|klage|gericht|r(?:ä|ae)um|minderung|mindern"
        r"|\bmrg\b|\babgb\b|\bbgb\b|gesetz|zul(?:ä|ae)ssig|wirksam|haftung|schaden|verj(?:ä|ae)hr",
        re.IGNORECASE,
    )),
    ("FRIST", "Frist oder Zusage: im Entwurf wird nichts zugesagt.", re.compile(
        r"stund|raten|aufschub|verl(?:ä|ae)nger|\bfrist|sp(?:ä|ae)ter\s+zahl|bis wann|zusag|garantier|versprech|erlass"
        r"|verzicht|zur(?:ü|ue)ck|r(?:ü|ue)ckzahl|verrechn|abrechn",
        re.IGNORECASE,
    )),
    ("BANKDATEN", "Bankdaten werden im Entwurf nicht genannt.", re.compile(
        r"\biban\b|\bbic\b|kontonummer|bankverbindung|bankdaten|auf welches konto|wohin .*(?:zahlen|(?:ü|ue)berweis)",
        re.IGNORECASE,
    )),
    ("PARTEIEN", "Fragt nach weiteren Personen: wird nicht aus dem Entwurf beantwortet.", re.compile(
        r"mitmieter|andere[rnms]?\s+mieter|nachbar|vermieter|eigent(?:ü|ue)mer|vertreter", re.IGNORECASE,
    )),
)

_THEMEN = (
    ("KAUTION", re.compile(
        r"kaution.*(?:wie hoch|h(?:ö|oe)he|wie ?viel|betrag|betr(?:ä|ae)gt|hinterlegt|eingegangen|erhalten)"
        r"|(?:wie hoch|h(?:ö|oe)he|wie ?viel|betrag|betr(?:ä|ae)gt|hinterlegt|eingegangen|erhalten).*kaution",
        re.IGNORECASE,
    )),
    ("MIETBEGINN", re.compile(
        r"mietbeginn|beginn des mietverh|wann\s+(?:hat|begann|beginnt|startet?e?).*(?:miet|vertrag)", re.IGNORECASE,
    )),
    ("VERTRAGSENDE", re.compile(
        r"mietende|vertragsende|befrist|wann\s+(?:endet|l(?:ä|ae)uft).*(?:miet|vertrag)", re.IGNORECASE,
    )),
    ("MIETHOEHE", re.compile(
        r"(?:wie hoch|h(?:ö|oe)he|wie ?viel|betr(?:ä|ae)gt).*(?:miete\b|mietzins|betriebskosten|heizkosten|vorschreibung)"
        r"|(?:miete\b|mietzins|betriebskosten|heizkosten|vorschreibung).*(?:wie hoch|h(?:ö|oe)he|wie ?viel|betr(?:ä|ae)gt)",
        re.IGNORECASE,
    )),
    ("SALDO", re.compile(
        r"saldo|r(?:ü|ue)ckstand|\boffen(?:e[rnms]?)?\b|schulde|kontostand|guthaben|bezahlt|gezahlt"
        r"|(?:zahlung|miete|(?:ü|ue)berweisung).*(?:eingegangen|erhalten|angekommen)",
        re.IGNORECASE,
    )),
    ("FAELLIGKEIT", re.compile(r"f(?:ä|ae)llig|zahlungstermin|wann\s+muss\s+ich\s+(?:zahlen|(?:ü|ue)berweisen)", re.IGNORECASE)),
    ("NUTZUNGSART", re.compile(r"nutzungsart|wohnung oder|als b(?:ü|ue)ro", re.IGNORECASE)),
    ("KONTAKT", re.compile(
        r"(?:welche|hinterlegt|gespeichert).*(?:e-?mail|telefon|adresse|anschrift|kontakt)"
        r"|(?:e-?mail|telefon|adresse|anschrift|kontakt).*(?:hinterlegt|gespeichert)",
        re.IGNORECASE,
    )),
)


def _eur(cent: int) -> str:
    return f"{cents_to_decimal(cent).quantize(Decimal('0.01')):,.2f}".replace(",", "§").replace(".", ",").replace("§", ".") + " €"


def _einzeilig(text: str) -> str:
    """Eingaben bleiben im Entwurf einzeilig: keine Zeilen-/Headerinjektion."""

    return " ".join(_STEUERZEICHEN.sub(" ", text).split())


def zerlege_fragen(frage: str, teilfragen: Iterable[str] = ()) -> list[str]:
    """Ein Punkt je Zeile, strukturierte Teilfragen und mehrere Fragesätze in
    einer Zeile. Nichts wird verworfen, auch keine Anrede: sie endet als offen."""

    ergebnis: list[str] = []
    for roh in [*frage.splitlines(), *teilfragen]:
        zeile = _AUFZAEHLUNG.sub("", _STEUERZEICHEN.sub(" ", roh))
        for teil in re.split(r"(?<=\?)\s+", zeile):
            bereinigt = _einzeilig(teil)
            if bereinigt:
                ergebnis.append(bereinigt)
    return ergebnis


@dataclass(frozen=True)
class AntwortFakten:
    """Nur zu GENAU EINEM autorisierten Vertrag geladene Daten. Weitere Parteien
    sind absichtlich kein Bestandteil."""

    vertrag_id: str
    objekt_id: str
    objekt_bezeichnung: str
    einheit_bezeichnung: str
    gueltig_bis: date | None
    profil: object | None
    komponenten: tuple
    debitor_email: str | None
    debitor_telefon: str | None
    debitor_adresse: str | None
    check: VertragsCheck


@dataclass(frozen=True)
class Antwortpunkt:
    nummer: int
    frage: str
    status: str
    thema: str | None
    antwort: str | None
    belege: tuple[str, ...]
    grund: str | None


@dataclass(frozen=True)
class Antwortentwurf:
    vertrag_id: str
    stichtag: date
    bezug_bestaetigt: bool
    punkte: tuple[Antwortpunkt, ...]
    text: str

    @property
    def offene_punkte(self) -> int:
        return sum(1 for p in self.punkte if p.status in (STATUS_OFFEN, STATUS_MANUELL))


def _nennt_anderen_bezug(frage: str, fakten: AntwortFakten) -> bool:
    befund = parse_vertragskennungen(frage)
    if befund.unklar or befund.ids - {fakten.vertrag_id}:
        return True
    for kennung in re.findall(r"\bVertrag(?:snummer|s-?nr\.?)?\s*[:#]?\s*([A-Za-z0-9][\w-]*\d[\w-]*)", frage, re.IGNORECASE):
        if kennung != fakten.vertrag_id:
            return True
    einheit = _einzeilig(fakten.einheit_bezeichnung).casefold()
    for top in re.findall(r"\bTop\s*(?:Nr\.?\s*)?(\w+)", frage, re.IGNORECASE):
        if not re.search(rf"\btop\s*(?:nr\.?\s*)?{re.escape(top.casefold())}(?!\w)", einheit):
            return True
    objekt_text = f"{fakten.objekt_id} {fakten.objekt_bezeichnung}".casefold()
    for objekt in re.findall(r"\bObjekt\s+([\w-]+)", frage, re.IGNORECASE):
        if not re.search(rf"(?<!\w){re.escape(objekt.casefold())}(?!\w)", objekt_text):
            return True
    return False


def _profilbeleg(profil) -> str:
    beleg = f"Mietvertragsprofil Version {profil.version} (Quelle {profil.quelle_typ}"
    if profil.quelle_referenz and profil.quelle_referenz.startswith("pdf-sha256:"):
        beleg += f", Prüfsumme {profil.quelle_referenz.removeprefix('pdf-sha256:')[:12]}…"
    return beleg + ")"


_Ergebnis = tuple[str, str | None, tuple[str, ...], str | None]


def _offen(grund: str) -> _Ergebnis:
    return STATUS_OFFEN, None, (), grund


def _antwort_kaution(fakten: AntwortFakten) -> _Ergebnis:
    kaution = fakten.check.kaution
    if kaution.abgeschlossen_am is not None:
        return _offen("Kaution ist als abgeschlossen vermerkt: bitte manuell beantworten.")
    saetze: list[str] = []
    belege: list[str] = []
    if kaution.eingang_cent is not None:
        saetze.append(f"Als eingegangen erfasst: {_eur(kaution.eingang_cent)} (Stichtag {kaution.eingang_stichtag:%d.%m.%Y}).")
        belege.append(f"Kautionseintrag{f', Referenz {kaution.eingang_referenz}' if kaution.eingang_referenz else ''}")
    if kaution.vereinbart_cent is not None and kaution.vereinbart_beleg and fakten.profil is not None:
        saetze.append(f"Laut Vertragsdaten vereinbart: {_eur(kaution.vereinbart_cent)}.")
        belege.append(f"{_profilbeleg(fakten.profil)}: {kaution.vereinbart_beleg}")
    if not saetze:
        return _offen("Kein belegter Kautionsbetrag erfasst (unbekannt).")
    return STATUS_BELEGT, " ".join(saetze), tuple(belege), None


def _antwort_mietbeginn(fakten: AntwortFakten) -> _Ergebnis:
    profil = fakten.profil
    if profil is None or profil.urspruenglicher_mietbeginn is None:
        return _offen("Kein belegter Mietbeginn erfasst (technischer Vertragsbeginn ist kein Beleg).")
    return STATUS_BELEGT, f"Mietbeginn laut Vertragsdaten: {profil.urspruenglicher_mietbeginn:%d.%m.%Y}.", (_profilbeleg(profil),), None


def _antwort_vertragsende(fakten: AntwortFakten) -> _Ergebnis:
    profil = fakten.profil
    if profil is None or not profil.befristungsstatus_quellenbeleg:
        return _offen("Keine belegte Befristungsangabe erfasst.")
    beleg = (f"{_profilbeleg(profil)}: {profil.befristungsstatus_quellenbeleg}",)
    if profil.befristungsstatus == "UNBEFRISTET":
        return STATUS_BELEGT, "Laut Vertragsdaten ist der Vertrag unbefristet.", beleg, None
    if profil.befristungsstatus == "BEFRISTET" and fakten.gueltig_bis is not None:
        return STATUS_BELEGT, f"Laut Vertragsdaten ist der Vertrag befristet bis {fakten.gueltig_bis:%d.%m.%Y}.", beleg, None
    return _offen("Befristung ungeklärt oder ohne Enddatum.")


def _antwort_miethoehe(fakten: AntwortFakten) -> _Ergebnis:
    if not fakten.komponenten:
        return _offen("Keine aktiven Mietbestandteile erfasst.")
    teile = [f"{k.bezeichnung}: {_eur(k.betrag_cent)} brutto" for k in fakten.komponenten]
    belege = tuple(f"Vertragskomponente {k.bezeichnung}, gültig ab {k.gueltig_von:%d.%m.%Y}" for k in fakten.komponenten)
    return STATUS_BELEGT, f"Aktuell erfasste Mietbestandteile: {'; '.join(teile)}.", belege, None


def _antwort_saldo(fakten: AntwortFakten) -> _Ergebnis:
    check = fakten.check
    stand = check.zahlungsstand
    if stand.konto_id is None or stand.saldo_cent is None:
        return _offen("Für diesen Vertrag gibt es kein Mietkonto (kein Saldo vorhanden).")
    saetze = [
        f"Laut gebuchtem Mietkonto, Stand {check.stichtag:%d.%m.%Y}: offener Betrag {_eur(stand.offen_cent)}, "
        f"Guthaben {_eur(stand.guthaben_cent)}."
    ]
    if stand.offene_zeitraeume:
        zeitraeume = "; ".join(f"{z.bezeichnung}: {_eur(z.rest_cent)}" for z in stand.offene_zeitraeume)
        saetze.append(f"Offene Positionen: {zeitraeume}.")
    if check.ruecklastschriften:
        saetze.append(f"Enthält {len(check.ruecklastschriften)} gebuchte Rücklastschrift(en).")
    if stand.letzte_erfasste_zahlung:
        saetze.append(f"Zuletzt erfasste Zahlung: {stand.letzte_erfasste_zahlung:%d.%m.%Y}.")
    if check.abdeckung is None:
        grenze = "Bankabdeckung unbekannt: kein eindeutig zugeordnetes Bankkonto."
    elif check.abdeckung.status == STATUS_VOLLSTAENDIG_BELEGT:
        grenze = f"Bankabdeckung für {check.von:%d.%m.%Y} bis {check.bis:%d.%m.%Y} belegt."
    elif not check.abdeckung.abgedeckt:
        grenze = f"Bankabdeckung für {check.von:%d.%m.%Y} bis {check.bis:%d.%m.%Y} unbekannt: belegte Perioden fehlen."
    else:
        luecken = ", ".join(f"{l.von:%d.%m.%Y} bis {l.bis:%d.%m.%Y}" for l in check.abdeckung.luecken)
        grenze = f"Bankabdeckung unbekannt für: {luecken}."
    saetze.append(grenze)
    if check.kandidaten:
        saetze.append(f"{len(check.kandidaten)} Bankbewegung(en) sind noch nicht zugeordnet und können den Stand ändern.")
    if stand.einzuege_offen_cent:
        saetze.append(f"Eingereichte Einzüge von {_eur(stand.einzuege_offen_cent)} gelten noch nicht als Zahlungseingang.")
    saetze.append("Das ist nur der gebuchte Stand und keine Bestätigung, dass alle Zahlungen erfasst oder bezahlt sind.")
    return STATUS_BELEGT_MIT_GRENZE, " ".join(saetze), (f"Mietkonto {stand.konto_id}, Berechnung OPService",), None


def _antwort_nutzungsart(fakten: AntwortFakten) -> _Ergebnis:
    profil = fakten.profil
    if profil is None or profil.nutzungsart in (None, "UNGEKLAERT"):
        return _offen("Nutzungsart nicht geklärt.")
    return STATUS_BELEGT, f"Nutzungsart laut Vertragsdaten: {profil.nutzungsart}.", (_profilbeleg(profil),), None


def _antwort_kontakt(fakten: AntwortFakten) -> _Ergebnis:
    teile = [
        f"{label}: {_einzeilig(wert)}"
        for label, wert in (("E-Mail", fakten.debitor_email), ("Telefon", fakten.debitor_telefon), ("Anschrift", fakten.debitor_adresse))
        if wert
    ]
    if not teile:
        return _offen("Keine Kontaktdaten des Mieters hinterlegt.")
    return STATUS_BELEGT, f"Hinterlegte Kontaktdaten: {'; '.join(teile)}.", ("Stammdaten des Hauptmieters dieses Vertrags",), None


def _antwort_faelligkeit(_fakten: AntwortFakten) -> _Ergebnis:
    return _offen("Kein belegter Fälligkeitstag gespeichert (der Vertragswert kann ein technischer Standard sein).")


_ANTWORTEN = {
    "KAUTION": _antwort_kaution, "MIETBEGINN": _antwort_mietbeginn, "VERTRAGSENDE": _antwort_vertragsende,
    "MIETHOEHE": _antwort_miethoehe, "SALDO": _antwort_saldo, "FAELLIGKEIT": _antwort_faelligkeit,
    "NUTZUNGSART": _antwort_nutzungsart, "KONTAKT": _antwort_kontakt,
}


# Full question forms bound the deterministic answer surface. A matching word
# alone must never hide another request or an unrecognised address.
_KURZFRAGEN = {
    "KAUTION": r"(?:wie hoch ist|wie ?viel betraegt|wie ?viel beträgt) (?:meine?|die|der|unsere?)?\s*kaution|ist (?:meine|die) kaution (?:hinterlegt|eingegangen|bezahlt)",
    "MIETBEGINN": r"wann (?:begann|beginnt|startet) (?:mein|der|unser) (?:mietvertrag|vertrag|mietverhältnis)|(?:was|wann) ist (?:mein|der) mietbeginn",
    "VERTRAGSENDE": r"ist (?:mein|der|unser) (?:mietvertrag|vertrag) (?:befristet|unbefristet)|wann endet (?:mein|der|unser) (?:mietvertrag|vertrag|mietverhältnis)",
    "MIETHOEHE": r"(?:wie hoch ist|wie ?viel beträgt|wie ?viel betraegt) (?:meine?|die|der|unsere?)?\s*(?:miete|mietzins|betriebskosten|heizkosten|vorschreibung)",
    "SALDO": r"habe ich (?:alles|die miete) (?:bezahlt|gezahlt)|wie hoch ist (?:mein|der|unser) (?:saldo|rückstand|rueckstand|kontostand|guthaben)|ist (?:meine|die) (?:zahlung|miete|überweisung) (?:eingegangen|angekommen)",
    "FAELLIGKEIT": r"wann ist (?:die|meine) miete fällig|wann muss ich (?:zahlen|überweisen)",
    "NUTZUNGSART": r"welche nutzungsart ist (?:hinterlegt|gespeichert)|was ist (?:die|meine) nutzungsart",
    "KONTAKT": r"welche (?:e-?mail(?:-adresse)?|telefonnummer|adresse|anschrift|kontaktdaten) (?:ist|sind) (?:hinterlegt|gespeichert)",
}


def _ist_vollstaendige_kurzfrage(frage: str, thema: str, fakten: AntwortFakten) -> bool:
    text = _einzeilig(frage).rstrip("?.! ").casefold()
    # Remove only exact, already verified suffixes, never arbitrary locations.
    suffixe = (
        f"für {fakten.einheit_bezeichnung}", f"zu vertrag:{fakten.vertrag_id}",
        f"im vertrag {fakten.vertrag_id}", f"im objekt {fakten.objekt_id}",
        f"im objekt {fakten.objekt_bezeichnung}", f"in {fakten.objekt_bezeichnung}",
    )
    for suffix in suffixe:
        ende = " " + _einzeilig(suffix).casefold()
        if text.endswith(ende):
            text = text[:-len(ende)]
            break
    return re.fullmatch(_KURZFRAGEN.get(thema, r"(?!)"), text) is not None


def _beantworte(nummer: int, frage: str, fakten: AntwortFakten, bezug_bestaetigt: bool) -> Antwortpunkt:
    def manuell(grund: str, thema: str | None = None) -> Antwortpunkt:
        return Antwortpunkt(nummer, frage, STATUS_MANUELL, thema, None, (), grund)

    if not bezug_bestaetigt:
        return manuell("Bezug zu diesem Vertrag nicht bestätigt: manuelle Prüfung.")
    for art, grund, muster in _GESPERRT:
        if muster.search(frage):
            return manuell(grund, art)
    if _nennt_anderen_bezug(frage, fakten):
        return manuell("Frage nennt eine andere Vertragskennung, Einheit oder ein anderes Objekt: manuelle Prüfung.")
    themen = [thema for thema, muster in _THEMEN if muster.search(frage)]
    if not themen:
        return Antwortpunkt(nummer, frage, STATUS_OFFEN, None, None, (), "Keine feste Regel passt: offen, bitte manuell beantworten.")
    if len(themen) > 1:
        return Antwortpunkt(nummer, frage, STATUS_OFFEN, None, None, (), f"Berührt mehrere Themen ({', '.join(themen)}): offen.")
    if not _ist_vollstaendige_kurzfrage(frage, themen[0], fakten):
        return manuell("Frage enthält ungeklärte Zusatzangaben oder liegt außerhalb der belegten Frageformen; vollständig manuell prüfen.", themen[0])
    status, antwort, belege, grund = _ANTWORTEN[themen[0]](fakten)
    return Antwortpunkt(nummer, frage, status, themen[0], antwort, belege, grund)


def _als_text(fakten: AntwortFakten, stichtag: date, punkte: list[Antwortpunkt]) -> str:
    zeilen = [
        "ENTWURF - NICHT VERSENDET - vor Verwendung prüfen",
        f"Vertrag {fakten.vertrag_id}, {_einzeilig(fakten.objekt_bezeichnung)} / {_einzeilig(fakten.einheit_bezeichnung)}, Stand {stichtag:%d.%m.%Y}",
        "",
    ]
    for punkt in punkte:
        zeilen.append(f"{punkt.nummer}. Frage: {punkt.frage}")
        if punkt.antwort is not None:
            zeilen.append(f"   Antwort: {punkt.antwort}")
            zeilen.extend(f"   Beleg: {_einzeilig(beleg)}" for beleg in punkt.belege)
        else:
            marke = "MANUELLE PRÜFUNG" if punkt.status == STATUS_MANUELL else "OFFEN"
            zeilen.append(f"   [{marke}] {punkt.grund}")
    zeilen.extend(("", "Versand ausschließlich über den Mail-Verantwortlichen; dieser Entwurf löst nichts aus."))
    return "\n".join(zeilen)


def erstelle_antwortentwurf(
    *, frage: str, teilfragen: Iterable[str] = (), fakten: AntwortFakten, bezug_vertrag_id: str,
) -> Antwortentwurf:
    """Rein funktional, keine Schreib- oder Sendewirkung. Mehr als
    `MAX_TEILFRAGEN` Punkte bleiben mit ihrem ganzen Wortlaut einzeln offen.
    Zu lange Eingaben werden insgesamt abgelehnt, niemals still gekürzt."""

    teilfragen = tuple(teilfragen)
    if len(frage) + sum(len(t) for t in teilfragen) > MAX_EINGABE_ZEICHEN:
        raise ValueError(f"Eingabe länger als {MAX_EINGABE_ZEICHEN} Zeichen; kein gekürzter Entwurf erzeugt.")
    bezug_bestaetigt = bezug_vertrag_id == fakten.vertrag_id
    fragen = zerlege_fragen(frage, teilfragen)
    punkte = [_beantworte(i, f, fakten, bezug_bestaetigt) for i, f in enumerate(fragen[:MAX_TEILFRAGEN], start=1)]
    for nummer, restfrage in enumerate(fragen[MAX_TEILFRAGEN:], start=MAX_TEILFRAGEN + 1):
        punkte.append(Antwortpunkt(
            nummer, restfrage, STATUS_OFFEN, None, None, (),
            f"Mehr als {MAX_TEILFRAGEN} Punkte: dieser Originalpunkt wurde nicht automatisch ausgewertet, bitte manuell prüfen.",
        ))
    stichtag = fakten.check.stichtag
    return Antwortentwurf(
        vertrag_id=fakten.vertrag_id, stichtag=stichtag, bezug_bestaetigt=bezug_bestaetigt, punkte=tuple(punkte),
        text=_als_text(fakten, stichtag, punkte),
    )
