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
    """Auftrag HV-20261020-VIER-MAHNSTUFEN: von genau zwei auf vier
    Stufen erweitert (Nutzerentscheidung 20.10.2026, löst die bisherige
    Fachregel 7 "genau zwei Stufen" ab) - additiv, STUFE_1/STUFE_2
    behalten ihre bisherigen Werte, bestehende Daten/Vergleiche bleiben
    gültig. Ob eine konkrete Forderung/ein Vertrag tatsächlich über
    STUFE_2 hinaus eskaliert wird, entscheidet weiterhin die jeweils
    FREIGEGEBENE `MahnPolicyTable`-Version (siehe `mahnwesen/service.py::
    MahnwesenService._naechste_stufe_fuer_forderung`) - eine alte, vor
    diesem Auftrag freigegebene Policy ohne Vier-Stufen-Profil bleibt bei
    genau zwei Stufen (kein stillschweigender Verhaltenswechsel für
    bereits laufende Mahnzyklen)."""

    STUFE_1 = 1
    STUFE_2 = 2
    STUFE_3 = 3
    STUFE_4 = 4


class MahnStatus(str, enum.Enum):
    GEPLANT = "GEPLANT"
    # Dauerhaft exklusiv einer eingefrorenen MahnLauf-Gruppe zugeordnet
    # (Rückprüfung Codex 14.09.2026: verhindert, dass derselbe MahnFall
    # gleichzeitig Teil zweier überlappender Gruppen wird - siehe
    # `mahnwesen/repository.py::MahnFallRepository.claim_fuer_buendelung`
    # und `mahnwesen/service.py::MahnwesenService.plane_mahnlauf`). Ein
    # GEBUENDELTER Fall ist NIE Kandidat einer neuen Gruppenbildung.
    GEBUENDELT = "GEBUENDELT"
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


class Nutzungsart(str, enum.Enum):
    """Anzeige-/Verwaltungsklassifikation der tatsächlichen Nutzung einer
    Mieteinheit (Auftrag HV-20260913-VERTRAGSANLAGE) - AUSDRÜCKLICH
    UNABHÄNGIG von `Rechtsordnung`/`RechtsprofilTable.ist_wohnungsnutzung`:
    ein Büro kann MRG-Vollanwendung sein, eine Wohnung kann Geschäftsraum-
    artig vermietet sein. Niemals aus der Rechtsordnung ableiten (kein
    "Büro = MRG-frei"-Rateversuch, siehe `mieweg_vorschau/service.py`)."""

    WOHNUNG = "WOHNUNG"
    BUERO = "BUERO"
    GESCHAEFTSLOKAL = "GESCHAEFTSLOKAL"
    SONSTIGE = "SONSTIGE"
    UNGEKLAERT = "UNGEKLAERT"


class MietvertragsprofilQuelle(str, enum.Enum):
    """Herkunft einer `MietvertragsprofilTable`-Version - nie
    stillschweigend geraten, siehe `intake/pdf_extraktion.py`/
    `intake/schema.py::MietvertragsprofilZeile`."""

    PDF_EXTRAKTION = "PDF_EXTRAKTION"
    MANUELL = "MANUELL"
    IMPORT_SCHEMA = "IMPORT_SCHEMA"


class Vertragsparteirolle(str, enum.Enum):
    """Auftrag HV-20261020-VERTRAGSDATENBASIS (Nutzerdesignvorgabe
    20.10.2026: 'EINE führende Quelle, Hauptmieter nicht mit abweichend
    editierbaren Kontakten neben Debitor duplizieren'): die Rolle einer
    `VertragMitmieterTable`-Zeile. `HAUPTMIETER`-Zeilen verknüpfen
    AUSSCHLIESSLICH über `debitor_id` auf den bestehenden Debitor (KEINE
    eigenen Kontaktfelder - der Debitor bleibt die einzige Quelle für
    Name/E-Mail/Adresse/Telefon). `MITMIETER`/`VERTRETER`-Zeilen tragen
    ihre Kontaktdaten selbst, weil sie KEIN Debitor sind. Mahnwesen
    adressiert automatisch nur bestätigte `MITMIETER` (siehe
    `mahnwesen/repository.py::VertragMitmieterRepository.
    list_bestaetigte_mitmieter`) - ein `VERTRETER` wird NIE automatisch
    mitadressiert, das bleibt eine bewusste menschliche Entscheidung."""

    HAUPTMIETER = "HAUPTMIETER"
    MITMIETER = "MITMIETER"
    VERTRETER = "VERTRETER"


class Mietparteizustand(str, enum.Enum):
    """Auftrag HV-20261020-VERTRAGSDATENBASIS: Erfassungszustand EINER
    `VertragMitmieterTable`-Zeile - `AUSGELESEN` (aus einem PDF
    extrahiert, noch nicht menschlich bestätigt), `BESTAETIGT`
    (menschlich geprüft/freigegeben - NUR in diesem Zustand wird eine
    Zeile automatisch Mahnempfänger, siehe `Vertragsparteirolle`-
    Docstring), `FEHLT` (bekannte Lücke, z. B. Telefonnummer fehlt -
    blockiert NICHT automatisch andere Verwendungen), `MEHRDEUTIG`
    (mehrere widersprüchliche Fundstellen, siehe `vertragsanlage/
    pdf_extraktion.py`)."""

    AUSGELESEN = "AUSGELESEN"
    BESTAETIGT = "BESTAETIGT"
    FEHLT = "FEHLT"
    MEHRDEUTIG = "MEHRDEUTIG"


class Befristungsstatus(str, enum.Enum):
    """Auftrag HV-20261020-VERTRAGSDATENBASIS: explizite Tri-State-
    Angabe, ob ein Vertrag befristet/unbefristet ist - `UNGEKLAERT` ist
    der Default und wird NIEMALS automatisch aus einem fehlenden
    `VertragTable.gueltig_bis`/`MietvertragsprofilZeile`-Wert abgeleitet
    ("Leeres Ende darf NICHT automatisch unbefristet sein", Auftrag
    Markus) - nur eine tatsächlich belegte Vertragsklausel setzt
    `BEFRISTET`/`UNBEFRISTET`."""

    BEFRISTET = "BEFRISTET"
    UNBEFRISTET = "UNBEFRISTET"
    UNGEKLAERT = "UNGEKLAERT"


class BkHkAbrechnungsart(str, enum.Enum):
    """Auftrag HV-20261020-VERTRAGSDATENBASIS (Nutzerergänzung
    20.10.2026): entscheidet, ob eine BK/HK-Nachverrechnung später
    überhaupt zulässig ist - NIE aus einem bloßen BK-/HK-Betrag
    geraten. `UNGEKLAERT` ist der Default, solange keine Vertragsklausel
    das ausdrücklich belegt."""

    AKONTO_VORAUSZAHLUNG = "AKONTO_VORAUSZAHLUNG"
    PAUSCHALE_INKLUSIVMIETE = "PAUSCHALE_INKLUSIVMIETE"
    UNGEKLAERT = "UNGEKLAERT"


class VerbraucherUnternehmerStatus(str, enum.Enum):
    """Auftrag HV-20261020-VERTRAGSDATENBASIS (Nutzerergänzung
    20.10.2026): Verbraucher-/Unternehmerstatus EINER Vertragspartei,
    soweit für den gesetzlichen Verzugszins-Fallback (ABGB/UGB)
    erforderlich - NIEMALS allein aus der Nutzungsart (z. B. "Büro"/
    "Gewerbe") abgeleitet. `UNGEKLAERT` bleibt der Default."""

    VERBRAUCHER = "VERBRAUCHER"
    UNTERNEHMER = "UNTERNEHMER"
    UNGEKLAERT = "UNGEKLAERT"


class RechtsachseMrgAnwendungsbereich(str, enum.Enum):
    """Auftrag HV-20261020-VERTRAGSDATENBASIS: EINE von drei getrennten
    Rechtsachsen (die anderen sind `RechtsachseMietzinsbildung` und
    `Nutzungsart`) - AUSDRÜCKLICH unverbindliches Staging-Feld auf
    `MietvertragsprofilTable` (wie die Index-Quellfelder), ERSETZT NICHT
    `VertragTable.rechtsordnung`/`RechtsprofilTable` und löst KEINE
    Freigabe/Sollstellung/Mahnsperre aus. Gewerbenutzung ist KEINE
    automatische MRG-Ausnahme, freier Mietzins ist KEINE automatische
    MRG-Ausnahme - diese Achse bleibt deshalb bewusst unabhängig von
    `RechtsachseMietzinsbildung`/`Nutzungsart`."""

    VOLL = "VOLL"
    TEIL = "TEIL"
    AUSNAHME = "AUSNAHME"
    UNGEKLAERT = "UNGEKLAERT"


class VerzugszinsArt(str, enum.Enum):
    """Auftrag HV-20261020-VERTRAGSDATENBASIS: Herkunft des im Profil
    erfassten Verzugszinssatzes - `VERTRAGLICH_VEREINBART` (eine
    konkrete Klausel nennt einen Satz), `GESETZLICHER_FALLBACK` (keine
    wirksame vertragliche Vereinbarung, der gesetzliche Zinssatz -
    ABGB/UGB, je nach `VerbraucherUnternehmerStatus` - gilt), oder
    `UNGEKLAERT`. Rein unverbindliches Staging wie die Index-Quellfelder
    - die TATSÄCHLICH wirksame Berechnungsgrundlage bleibt ausschließlich
    die freigegebene `MahnPolicyTable.zinsen_prozent`, siehe
    `mahnwesen/kosten_service.py`; dieses Feld erzeugt KEINE Freigabe."""

    VERTRAGLICH_VEREINBART = "VERTRAGLICH_VEREINBART"
    GESETZLICHER_FALLBACK = "GESETZLICHER_FALLBACK"
    UNGEKLAERT = "UNGEKLAERT"


class RechtsachseMietzinsbildung(str, enum.Enum):
    """Auftrag HV-20261020-VERTRAGSDATENBASIS: zweite der drei getrennten
    Rechtsachsen (siehe `RechtsachseMrgAnwendungsbereich`-Docstring) -
    gleiches Staging-Prinzip, keine Wirkung auf Freigabe/Sollstellung."""

    FREI = "FREI"
    RICHTWERT = "RICHTWERT"
    KATEGORIE = "KATEGORIE"
    ANDERE = "ANDERE"
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
