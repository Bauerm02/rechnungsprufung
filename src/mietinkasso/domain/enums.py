"""Domain enums for the Hausverwaltung & Mietinkasso module.

Kept deliberately explicit (no free-text status strings) so that every
state transition is enforceable in the service layer and reviewable by
a non-developer against the Fachregeln in docs/hausverwaltung/RAHMENPROGRAMM.md.
"""

from __future__ import annotations

import enum


class Nutzungsstatus(str, enum.Enum):
    DAUERVERMIETUNG = "DAUERVERMIETUNG"
    KURZZEITVERMIETUNG = "KURZZEITVERMIETUNG"
    EIGENNUTZUNG = "EIGENNUTZUNG"
    LEERSTAND = "LEERSTAND"
    SELFSTORAGE = "SELFSTORAGE"


class OPTyp(str, enum.Enum):
    EROEFFNUNG = "EROEFFNUNG"
    SOLL = "SOLL"
    GUTSCHRIFT = "GUTSCHRIFT"
    ZAHLUNG = "ZAHLUNG"
    RUECKLASTSCHRIFT = "RUECKLASTSCHRIFT"
    KORREKTUR = "KORREKTUR"


class OPPositionStatus(str, enum.Enum):
    AKTIV = "AKTIV"
    STORNIERT = "STORNIERT"


class VorschreibungStatus(str, enum.Enum):
    ENTWURF = "ENTWURF"
    SOLLGESTELLT = "SOLLGESTELLT"
    ZUGESTELLT = "ZUGESTELLT"
    EXPORTIERT = "EXPORTIERT"


class MahnStufe(int, enum.Enum):
    STUFE_1 = 1
    STUFE_2 = 2


class MahnStatus(str, enum.Enum):
    GEPLANT = "GEPLANT"
    IN_VERSAND = "IN_VERSAND"  # atomar beansprucht, Provider-Aufruf läuft/lief möglicherweise
    GESENDET = "GESENDET"
    UNSICHER = "UNSICHER"
    BLOCKIERT = "BLOCKIERT"
    UEBERSPRUNGEN = "UEBERSPRUNGEN"


class Sperrgrund(str, enum.Enum):
    STREIT = "STREIT"
    MIETMINDERUNG = "MIETMINDERUNG"
    RATENPLAN = "RATENPLAN"
    INSOLVENZ = "INSOLVENZ"
    RECHTSANWALT = "RECHTSANWALT"
    UNGEKLAERTER_EINGANG = "UNGEKLAERTER_EINGANG"
    UNKLARER_EROEFFNUNGSSALDO = "UNKLARER_EROEFFNUNGSSALDO"
    BOUNCE = "BOUNCE"
    MANUELL = "MANUELL"


class BKPositionsart(str, enum.Enum):
    UMLAGEFAEHIG = "UMLAGEFAEHIG"
    EIGENTUEMER = "EIGENTUEMER"
    UNGEKLAERT = "UNGEKLAERT"


class BKAbrechnungStatus(str, enum.Enum):
    ENTWURF = "ENTWURF"
    GEPRUEFT = "GEPRUEFT"
    FREIGEGEBEN = "FREIGEGEBEN"


class IndexKlauselStatus(str, enum.Enum):
    ENTWURF = "ENTWURF"
    FREIGEGEBEN = "FREIGEGEBEN"
    GESPERRT = "GESPERRT"


class IndexAnpassungStatus(str, enum.Enum):
    VORSCHLAG = "VORSCHLAG"
    FREIGEGEBEN = "FREIGEGEBEN"
    VERWORFEN = "VERWORFEN"
    INVALIDIERT = "INVALIDIERT"


class Rechtsordnung(str, enum.Enum):
    OESTERREICH_MRG_VOLL = "OESTERREICH_MRG_VOLL"
    OESTERREICH_MRG_TEIL = "OESTERREICH_MRG_TEIL"
    OESTERREICH_MRG_FREI = "OESTERREICH_MRG_FREI"
    OESTERREICH_WGG = "OESTERREICH_WGG"
    OESTERREICH_GEWERBE = "OESTERREICH_GEWERBE"
    DEUTSCHLAND = "DEUTSCHLAND"
    # Bewusst kein Rateversuch: eine noch nicht geklärte rechtliche
    # Einordnung wird explizit als UNGEKLAERT erfasst statt eine der
    # anderen Kategorien zu raten. Ein Vertrag mit dieser Rechtsordnung
    # bleibt stammdatenseitig anlegbar, ist aber technisch von
    # Mahnung, Index-Anpassung und Sollstellung gesperrt (siehe
    # `stammdaten/repository.py`, `mahnwesen/service.py`,
    # `index/service.py`, `vorschreibung/service.py`).
    UNGEKLAERT = "UNGEKLAERT"


def rechtsordnung_geklaert(rechtsordnung: str) -> bool:
    """Zentrale Prüfung, die von `vorschreibung/service.py::sollstellen`,
    `index/service.py::klausel_anlegen`/`berechne_vorschlag` und
    `mahnwesen/service.py::plane_forderung` gleichermaßen benutzt wird,
    damit die drei Sperren nicht unabhängig voneinander (und potenziell
    inkonsistent) je Aufrufer neu formuliert werden."""

    return rechtsordnung != Rechtsordnung.UNGEKLAERT.value


class VertragPruefungStatus(str, enum.Enum):
    """Fachstatus einer versionierten Vertragsprüfung (siehe
    `vertragspruefung/service.py`). Nur `GEPRUEFT` schreibt die gewählte
    Rechtsordnung tatsächlich auf den Vertrag zurück (Freigabe);
    `ENTWURF` bleibt ein reiner, sichtbarer Vorschlag ohne Wirkung."""

    ENTWURF = "ENTWURF"
    GEPRUEFT = "GEPRUEFT"


class Rolle(str, enum.Enum):
    ADMIN = "ADMIN"
    BUCHHALTUNG = "BUCHHALTUNG"
    HAUSVERWALTUNG = "HAUSVERWALTUNG"
    LESEZUGRIFF = "LESEZUGRIFF"


class ZahlungsMatchTyp(str, enum.Enum):
    AUTOMATISCH_EINDEUTIG = "AUTOMATISCH_EINDEUTIG"
    MANUELL = "MANUELL"


class JobLaufStatus(str, enum.Enum):
    LAEUFT = "LAEUFT"
    ERFOLGREICH = "ERFOLGREICH"
    FEHLGESCHLAGEN = "FEHLGESCHLAGEN"


class RechtsprofilStatus(str, enum.Enum):
    ENTWURF = "ENTWURF"
    FREIGEGEBEN = "FREIGEGEBEN"
    INVALIDIERT = "INVALIDIERT"


class IndexautomatikLaufStatus(str, enum.Enum):
    BLOCKIERT = "BLOCKIERT"
    TERMIN_NICHT_ERREICHT = "TERMIN_NICHT_ERREICHT"
    KEIN_ERHOEHUNGSBEDARF = "KEIN_ERHOEHUNGSBEDARF"
    # Codex-Rückprüfung zu 5535ae2: eine NEGATIVE Anpassung (Index
    # tatsächlich gesunken) ist fachlich etwas anderes als "keine
    # Erhöhung nötig" (Anpassung == 0, unterhalb der Schwelle) - eine
    # Senkung darf nicht stillschweigend unter demselben Status
    # verschwinden. Automatische Senkungen sind NICHT implementiert
    # (kein automatisches Schreiben); dieser Status macht den internen
    # Prüfbedarf sichtbar, statt ihn zu verstecken.
    SENKUNG_PRUEFBEDARF = "SENKUNG_PRUEFBEDARF"
    ERHOEHUNG_ERZEUGT = "ERHOEHUNG_ERZEUGT"
    BEREITS_ERFASST = "BEREITS_ERFASST"


class ErhoehungsschreibenStatus(str, enum.Enum):
    """Der Auftrag nennt die Zustände wörtlich "Entwurf/blockiert/
    bereit/gesendet/Zugang bestätigt/ausgeführt/unklar" - `ausgeführt`
    wird hier bewusst als `SOLL_UMSETZUNG_OFFEN` geführt (unabhängiger
    Review b31: "outbox_service.taegliche_pflege setzt weiterhin
    AUSGEFUEHRT, obwohl keine Solländerung stattfindet"): dieses Modul
    löst NIE automatisch eine Sollstellung/Vorschreibungsänderung aus
    (siehe OFFENE_PUNKTE.md) - der Name muss das auch dann noch ehrlich
    ausdrücken, wenn er vom wörtlichen Auftragstext abweicht.
    `IN_VERSAND` ist ein zusätzlicher, rein interner Zwischenzustand
    (wie `MahnStatus.IN_VERSAND` in `mahnwesen/`) für den atomaren Claim
    zwischen "bereit" und "gesendet" - ohne ihn bliebe die Zeile beim
    Claim auf BEREIT stehen und ein zweiter Aufrufer könnte denselben
    Fall parallel claimen (Doppelversand)."""

    ENTWURF = "ENTWURF"
    BLOCKIERT = "BLOCKIERT"
    BEREIT = "BEREIT"
    IN_VERSAND = "IN_VERSAND"
    GESENDET = "GESENDET"
    ZUGANG_BESTAETIGT = "ZUGANG_BESTAETIGT"
    SOLL_UMSETZUNG_OFFEN = "SOLL_UMSETZUNG_OFFEN"
    # Auftrag HV-20260913-VERSAND-SOLL: der bisher fehlende letzte
    # Schritt (SOLL_UMSETZUNG_OFFEN -> tatsächliche Änderung von
    # VertragsKomponenteTable/Indexbasis). SOLL_UMSETZUNG_IN_PRUEFUNG ist
    # ein rein interner, transienter Claim-Zustand (analog IN_VERSAND) -
    # er wird NIE dauerhaft sichtbar, weil `umsetzung_service.umsetzen`
    # den Claim UND alle Änderungen in EINER Transaktion committet: ein
    # Absturz dazwischen rollt die gesamte Transaktion (inkl. Claim)
    # zurück, die Zeile bleibt bei SOLL_UMSETZUNG_OFFEN stehen (kein
    # separater Recovery-Job wie bei IN_VERSAND nötig, weil hier - anders
    # als beim Mailversand - kein externer Aufruf die Transaktion
    # überdauern kann). SOLL_UMSETZUNG_BLOCKIERT ist wie BLOCKIERT
    # retryable (z. B. nach einer nachträglich belegten differenziellen
    # Korrektur), SOLL_UMGESETZT ist terminal.
    SOLL_UMSETZUNG_IN_PRUEFUNG = "SOLL_UMSETZUNG_IN_PRUEFUNG"
    SOLL_UMSETZUNG_BLOCKIERT = "SOLL_UMSETZUNG_BLOCKIERT"
    SOLL_UMGESETZT = "SOLL_UMGESETZT"
    UNKLAR = "UNKLAR"


#: Formal für eine MRG-Zugangsfrist geeignete Zustellwege - eine bloß
#: technisch angenommene E-Mail (SMTP/HTTP-accepted) gilt NICHT
#: automatisch als Zugang (Fachlicher Nachtrag 13.09.: "nicht jedes
#: E-Mail verbieten", aber "keine automatische Gleichsetzung von SMTP/
#: HTTP-accepted mit Zugang"). `EMAIL_UNBESTAETIGT` bleibt daher
#: bewusst AUSSERHALB dieser Menge; eine formal ausreichende Bestätigung
#: (z. B. Lesebestätigung, Übergabeprotokoll, Rückschein) gehört hier
#: hinein.
ZUGANGSFORMEN_AUSREICHEND = frozenset({
    "EINSCHREIBEN_RUECKSCHEIN",
    "UEBERGABE_BESTAETIGT",
    "EMAIL_MIT_LESEBESTAETIGUNG",
    "SONSTIGER_FORMELLER_NACHWEIS",
})
ZUGANGSFORMEN_ALLE = ZUGANGSFORMEN_AUSREICHEND | {"EMAIL_UNBESTAETIGT"}


class VertragsendeErinnerungStatus(str, enum.Enum):
    OFFEN = "OFFEN"
    IN_VERSAND = "IN_VERSAND"
    BENACHRICHTIGT = "BENACHRICHTIGT"
    UNKLAR = "UNKLAR"
    ENTSCHIEDEN = "ENTSCHIEDEN"
    UNGUELTIG = "UNGUELTIG"


class VertragsendeEntscheidung(str, enum.Enum):
    VERLAENGERN_PRUEFEN = "VERLAENGERN_PRUEFEN"
    NICHT_VERLAENGERN_PRUEFEN = "NICHT_VERLAENGERN_PRUEFEN"
    RUECKFRAGE = "RUECKFRAGE"


class VariableAbrechnungArt(str, enum.Enum):
    """Auftrag 13.09., HV-20260913-DASHBOARD - nur diese zwei
    Nutzungsarten haben eine variable, berichtsbasierte
    Monatsabrechnung statt einer festen Dauervermietungs-Vorschreibung."""

    KURZZEITVERMIETUNG = "KURZZEITVERMIETUNG"
    SELFSTORAGE = "SELFSTORAGE"


class VariableAbrechnungStatus(str, enum.Enum):
    """Quellenbedingte Präzisierung (13.09.): Monatsberichte enthalten
    häufig zunächst nur einen Buchungsumsatz OHNE bestätigten
    Eigentümer-Nettoanteil. `ENTWURF` bleibt sichtbar, fließt aber NIE
    in eine Erlössumme ein - nur `BESTAETIGT` (mit belegtem, geprüftem
    `unser_netto_anteil_cent`) zählt."""

    ENTWURF = "ENTWURF"
    BESTAETIGT = "BESTAETIGT"


class KomponentenNettoMietFreigabeStatus(str, enum.Enum):
    """Auftrag 13.09., unabhängiger Review: `VertragsKomponenteTable.
    betrag_cent` ist im Bestand teils BRUTTO gespeichert (auch bei
    HMZ/KUECHE/PARKPLATZ) - `ust_satz_promille` allein beweist keine
    Betragsbasis. Diese separate, additive Freigabe bestätigt EXPLIZIT
    einen geprüften Netto-Mietanteil für Reporting-Zwecke, OHNE
    `VertragsKomponenteTable`/`OPPositionTable` zu verändern. Wie bei
    `RechtsprofilStatus` entwertet eine spätere Änderung der
    referenzierten Komponente die Freigabe automatisch (`quelle_hash`)."""

    FREIGEGEBEN = "FREIGEGEBEN"
    INVALIDIERT = "INVALIDIERT"


class VariableAbrechnungBetragsart(str, enum.Enum):
    """Betragsart des ROH gemeldeten Ursprungsbetrags
    (`berichteter_betrag_cent`) - getrennt von `unser_netto_anteil_cent`.
    Es gibt HIER keine automatische Netto-Umrechnung über einen
    angenommenen USt-Satz ("keine pauschale USt-Umrechnung, keine
    Division durch erfundene USt")."""

    BRUTTO = "BRUTTO"
    NETTO = "NETTO"
    UNGEKLAERT = "UNGEKLAERT"
