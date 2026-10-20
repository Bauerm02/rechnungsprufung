"""Die vier Mahnstufen-Texte (Auftrag HV-20261020-VIER-MAHNSTUFEN,
Nutzerentscheidung 20.10.2026) - reine Textbausteine ohne DB-/
Netzwerkzugriff, GEMEINSAM von HTML (Backoffice-Vorschau), E-Mail-Text
(`indexautomatik/mailversand_service.py`) und PDF-Brief
(`mahnwesen/brief_pdf.py`) verwendet, damit der Kundentext NIE an zwei
Stellen unabhängig voneinander formuliert wird und auseinanderlaufen
kann.

**Ton je Stufe (wörtliche Nutzervorgabe 20.10.2026, präzisiert durch
Nutzerkorrektur 20.10.2026: "Auch Stufe1/2 müssen den Ernst der
Zahlungsaufforderung erkennen lassen ... bloß Vielleicht-übersehen reicht
allein nicht"):**

- Stufe 1 - freundlich, ABER mit konkreter fälliger Forderung und
  14-Tage-Frist ab Zugang ("Vielleicht haben Sie die Zahlung übersehen"
  als Einstieg, NICHT als einzige Aussage).
- Stufe 2 - bietet eine Klärungsmöglichkeit bei Zahlungsschwierigkeiten
  an, OHNE dabei automatisch einen Zahlungsaufschub zuzusagen (das bleibt
  eine gesonderte, von einem Menschen zu treffende Entscheidung) - UND
  ebenfalls mit konkreter Forderung/Frist.
- Stufe 3 - nachdrückliche Zahlungsaufforderung.
- Stufe 4 - letzte Zahlungsaufforderung MIT dem gesetzlichen Kern aus der
  Nutzervorlage (siehe `rechtlicher_kern_stufe4`): §30 Abs 2 Z1 MRG
  (NUR wo das MRG tatsächlich im Vollanwendungsbereich gilt) bzw. §1118
  zweiter Fall ABGB, Übergabe an einen Rechtsanwalt nach fruchtloser
  Frist - AUSDRÜCKLICH KEINE Kündigung/Auflösung selbst, siehe
  `_MAHNSCHREIBEN_IST_KEINE_KUENDIGUNG`.

**Grundsatz "Zinsen/Mahnkosten ≠ Mietkapital/Kündigungsvoraussetzung"
(wörtliche Nutzervorgabe):** `rechtlicher_kern_stufe4` benennt
AUSDRÜCKLICH, dass Grundlage einer allfälligen gerichtlichen Geltend-
machung/Kündigung ausschließlich der RÜCKSTÄNDIGE MIETZINS (das
Mietkapital) ist - NICHT die gesondert ausgewiesenen Verzugszinsen/
Mahnkosten/Bankentgelte. Diese Trennung ist bereits strukturell in
`erzeuge_mahnbrief_pdf`/`op/kontoblatt.py` angelegt (getrennte Zeilen/
Summen) - dieser Textbaustein macht sie zusätzlich EXPLIZIT im
Kundentext.

**"Unbekanntes nicht erfinden":** `rechtlicher_kern_stufe4` verwendet NUR
die tatsächlich übergebene `rechtsordnung` - für eine Rechtsordnung ohne
hinterlegten Baustein (insbesondere `DEUTSCHLAND`, ein ausländisches
Rechtssystem, in dem ABGB/MRG nicht gelten) wird NIE ein österreichischer
Gesetzestext behauptet, sondern ein neutraler Hinweis plus ein
`hinweis_intern`-Flag zurückgegeben, das ausschließlich backoffice-intern
sichtbar ist (siehe `_MahnKernErgebnis.hinweis_intern` - NIE Teil des an
den Mieter gehenden Textes, Auftrag Markus: "dokumentierter
Prüfhintergrund NICHT in Kundentext").

**Rechtsordnungs-Zuordnung des §30-Abs-2-Z1-MRG-Hinweises (fachliche
Annahme, VOR Produktivgang durch Codex/Markus zu bestätigen - siehe
`docs/hausverwaltung/OFFENE_PUNKTE.md`):** `§30 Abs 2 Z1 MRG` (gericht-
liche Aufkündigung wegen qualifizierten Zahlungsrückstands) wird NUR bei
`OESTERREICH_MRG_VOLL` genannt - der Kündigungsschutz des § 30 MRG gilt
im MRG-Teilanwendungsbereich nach § 1 Abs 4 MRG grundsätzlich NICHT. Für
alle anderen geklärten österreichischen Rechtsordnungen (MRG_TEIL,
MRG_FREI, GEWERBE, WGG) wird deshalb NUR § 1118 zweiter Fall ABGB
genannt (die allgemeine vorzeitige Auflösung wegen qualifizierten
Zahlungsverzugs, unabhängig vom MRG-Anwendungsbereich)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from mietinkasso.domain.enums import MahnStufe, Rechtsordnung

#: Wörtliche Nutzervorgabe 20.10.2026 - erscheint auf JEDER Stufe-4-
#: Mahnung, damit der Brief unter keinen Umständen als Kündigung/
#: Vertragsauflösung selbst missverstanden werden kann.
_MAHNSCHREIBEN_IST_KEINE_KUENDIGUNG = (
    "Dieses Schreiben ist KEINE Kündigung und KEINE Erklärung der vorzeitigen "
    "Vertragsauflösung - es handelt sich ausschließlich um eine letzte Zahlungsaufforderung."
)

#: §1118 ABGB, zweiter Fall - Nutzervorgabe 20.10.2026, wörtlich: "14
#: Tage sind NICHT Ersatz für den nächsten Zinstermin" (die 14-Tage-Frist
#: dieses Schreibens ersetzt NICHT die eigenständige Voraussetzung des
#: § 1118 ABGB, dass der Vermieter mit der Zahlung so lange im Rückstand
#: sein muss, dass er trotz Mahnung noch bis zum nächsten Zinstermin
#: nicht bezahlt hat).
_1118_FRIST_HINWEIS = (
    "Die 14-Tage-Frist dieses Schreibens ist KEIN Ersatz für die gesonderte, eigenständige "
    "Voraussetzung des § 1118 zweiter Fall ABGB (Fortdauer des qualifizierten Zahlungsverzugs "
    "bis zum nächsten Zinstermin)."
)

#: Nutzervorlage 20.10.2026, Quellen laut Nutzerangabe: RIS 4 Ob 75/21i,
#: 3 Ob 37/18i - Mitmieter-Zustellungsgrundsatz. Rein deskriptiv, erzeugt
#: KEINE automatische Mehrfachzustellung (das übernimmt die
#: Empfänger-/Zugangsnachweis-Verwaltung je Person, siehe
#: `mahnwesen/repository.py::MahnEmpfaengerRepository`).
_MITMIETER_HINWEIS = (
    "Bestehen mehrere Mieter desselben Mietverhältnisses (Mitmieter), ergeht dieses Schreiben "
    "gegen JEDEN von ihnen gesondert; der Zugang muss für jede Person einzeln nachgewiesen werden "
    "(ein gewöhnlicher E-Mail-/SMTP-Versand oder die bloße Brief-Einlieferung bei der Post ist dafür "
    "NICHT automatisch ein rechtssicherer Zugangsnachweis)."
)


@dataclass(frozen=True)
class MahnstufeText:
    """Der vollständige Kundentext-Baustein EINER Stufe - `einleitung`
    und `kerntext` sind IMMER an den Mieter gerichteter Text;
    `rechtlicher_kern`/`hinweis_intern` sind nur für Stufe 4 gesetzt
    (sonst `None`/leer)."""

    stufe: int
    betreff: str
    einleitung: str
    kerntext: str
    rechtlicher_kern: str | None = None
    #: NIE Teil des an den Mieter gehenden Textes (siehe Moduldoc,
    #: "dokumentierter Prüfhintergrund NICHT in Kundentext") - nur für
    #: das Backoffice/die interne Dokumentation.
    hinweis_intern: str | None = None


#: MRG-Vollanwendungsbereich ist die EINZIGE Rechtsordnung, in der der
#: § 30 Abs 2 Z1 MRG-Kündigungsschutz greift (siehe Moduldoc) - für ALLE
#: anderen geklärten österreichischen Rechtsordnungen bleibt NUR § 1118
#: ABGB zweiter Fall als allgemeine Grundlage.
_OESTERREICHISCHE_RECHTSORDNUNGEN_MIT_ABGB = frozenset(
    {
        Rechtsordnung.OESTERREICH_MRG_VOLL.value,
        Rechtsordnung.OESTERREICH_MRG_TEIL.value,
        Rechtsordnung.OESTERREICH_MRG_FREI.value,
        Rechtsordnung.OESTERREICH_WGG.value,
        Rechtsordnung.OESTERREICH_GEWERBE.value,
    }
)


def rechtlicher_kern_stufe4(*, rechtsordnung: str) -> tuple[str, str | None]:
    """Liefert `(rechtlicher_kern_text, hinweis_intern)` für die
    Stufe-4-Mahnung - NIE geraten, siehe Moduldoc "Unbekanntes nicht
    erfinden". Für eine Rechtsordnung OHNE hinterlegten österreichischen
    Gesetzesbaustein (z. B. `DEUTSCHLAND`) ist `rechtlicher_kern_text`
    bewusst ALLGEMEIN gehalten (keine erfundene ausländische Norm) und
    `hinweis_intern` macht das für das Backoffice sichtbar. Wird NIE für
    `UNGEKLAERT` aufgerufen - diese Rechtsordnung ist bereits strukturell
    von jeder Mahnung ausgeschlossen (`rechtsordnung_geklaert`)."""

    satz_folgen = (
        "Erfolgt innerhalb der oben genannten Frist keine vollständige Zahlung, werden wir diesen Fall "
        "einem Rechtsanwalt zur gerichtlichen Geltendmachung übergeben."
    )
    if rechtsordnung == Rechtsordnung.OESTERREICH_MRG_VOLL.value:
        kern = (
            f"{satz_folgen} Dabei kommt gegebenenfalls eine gerichtliche Aufkündigung wegen "
            "qualifizierten Zahlungsrückstands gemäß § 30 Abs 2 Z 1 MRG oder die vorzeitige "
            "Vertragsauflösung gemäß § 1118 zweiter Fall ABGB in Betracht, sofern die dafür "
            f"jeweils erforderlichen gesetzlichen Voraussetzungen vorliegen. {_1118_FRIST_HINWEIS} "
            f"Grundlage einer solchen Kündigung/Auflösung ist ausschließlich der oben gesondert "
            f"ausgewiesene RÜCKSTÄNDIGE MIETZINS - NICHT die gesondert ausgewiesenen Verzugszinsen "
            f"oder Mahnkosten. {_MITMIETER_HINWEIS} {_MAHNSCHREIBEN_IST_KEINE_KUENDIGUNG}"
        )
        return kern, None
    if rechtsordnung in _OESTERREICHISCHE_RECHTSORDNUNGEN_MIT_ABGB:
        kern = (
            f"{satz_folgen} Dabei kommt gegebenenfalls die vorzeitige Vertragsauflösung gemäß "
            f"§ 1118 zweiter Fall ABGB in Betracht, sofern die dafür erforderlichen gesetzlichen "
            f"Voraussetzungen vorliegen. {_1118_FRIST_HINWEIS} Grundlage einer solchen Auflösung "
            f"ist ausschließlich der oben gesondert ausgewiesene RÜCKSTÄNDIGE MIETZINS - NICHT die "
            f"gesondert ausgewiesenen Verzugszinsen oder Mahnkosten. {_MITMIETER_HINWEIS} "
            f"{_MAHNSCHREIBEN_IST_KEINE_KUENDIGUNG}"
        )
        return kern, None
    # Keine hinterlegte österreichische Rechtsgrundlage (z. B. eine
    # ausländische Rechtsordnung wie DEUTSCHLAND) - NIE eine Norm
    # erfinden, stattdessen allgemein formulieren und intern markieren.
    kern = (
        f"{satz_folgen} Die weiteren rechtlichen Schritte richten sich nach den für dieses "
        f"Mietverhältnis anwendbaren gesetzlichen Bestimmungen. {_MITMIETER_HINWEIS} "
        f"{_MAHNSCHREIBEN_IST_KEINE_KUENDIGUNG}"
    )
    hinweis_intern = (
        f"Rechtsordnung {rechtsordnung!r} hat KEINEN hinterlegten österreichischen MRG/ABGB-"
        "Gesetzestextbaustein (z. B. ausländische Rechtsordnung) - der Stufe-4-Text bleibt bewusst "
        "allgemein. Vor einer tatsächlichen RA-Übergabe/Kündigung ist die konkrete Rechtsgrundlage "
        "fachlich gesondert zu prüfen (NIE automatisch aus dieser Stufe abgeleitet)."
    )
    return kern, hinweis_intern


def text_fuer_stufe(*, stufe: int, rechtsordnung: str, zahlungsfrist_bis: date) -> MahnstufeText:
    """Der vollständige Textbaustein für GENAU eine Stufe (1-4) - reine
    Funktion, von HTML/E-Mail/PDF GEMEINSAM verwendet (siehe Moduldoc).
    `zahlungsfrist_bis` ist das bereits anderswo (siehe `mahnwesen/
    fristen.py::fristende_ab_zugang`) berechnete, auf den nächsten
    Werktag verschobene tatsächliche Fristende - diese Funktion
    berechnet selbst KEIN Datum."""

    frist_text = zahlungsfrist_bis.strftime("%d.%m.%Y")
    if stufe == MahnStufe.STUFE_1.value:
        return MahnstufeText(
            stufe=1,
            betreff="Zahlungserinnerung",
            einleitung="Vielleicht haben Sie die Zahlung übersehen.",
            kerntext=(
                f"Für das unten genannte Mietverhältnis ist die unten aufgeschlüsselte Forderung "
                f"fällig und noch nicht bei uns eingelangt. Wir ersuchen Sie, den Gesamtbetrag "
                f"binnen 14 Tagen ab Zugang dieses Schreibens, also bis spätestens {frist_text}, "
                "vollständig zu begleichen. Sollte die Zahlung bereits erfolgt sein, betrachten Sie "
                "dieses Schreiben bitte als gegenstandslos und senden uns den Zahlungsbeleg."
            ),
        )
    if stufe == MahnStufe.STUFE_2.value:
        return MahnstufeText(
            stufe=2,
            # Betreff bewusst UNVERÄNDERT "Zweite Mahnung" (bisherige
            # Formulierung in `brief_pdf.py`/`mailversand_service.py`
            # vor diesem Auftrag) - keine Rückschritte/kein Bruch
            # bestehender Tests/Vorlagen allein wegen des Betreffs; die
            # inhaltliche Verschärfung (konkrete Frist, kein bloßes
            # "vielleicht übersehen") steckt im Fließtext.
            betreff="Zweite Mahnung",
            einleitung="Die mit unserem vorangegangenen Schreiben angeführte Forderung ist weiterhin offen.",
            kerntext=(
                f"Wir ersuchen Sie neuerlich, den unten aufgeschlüsselten Gesamtbetrag binnen 14 "
                f"Tagen ab Zugang dieses Schreibens, also bis spätestens {frist_text}, vollständig "
                "zu begleichen; eine weitere Verzögerung über diese Nachfrist hinaus können wir "
                "nicht in Aussicht stellen. Falls es Zahlungsschwierigkeiten gibt, melden Sie sich "
                "bitte umgehend zur Klärung bei uns - damit ist noch KEIN Zahlungsaufschub "
                "zugesagt; eine etwaige Stundung/Ratenvereinbarung bedarf einer gesonderten, "
                "ausdrücklichen Vereinbarung mit uns."
            ),
        )
    if stufe == MahnStufe.STUFE_3.value:
        return MahnstufeText(
            stufe=3,
            betreff="Nachdrückliche Zahlungsaufforderung",
            einleitung="Die unten aufgeschlüsselte Forderung ist trotz zweier vorangegangener Schreiben weiterhin unbeglichen.",
            kerntext=(
                f"Wir fordern Sie hiermit nachdrücklich auf, den Gesamtbetrag binnen 14 Tagen ab "
                f"Zugang dieses Schreibens, also bis spätestens {frist_text}, vollständig zu "
                "begleichen. Eine weitere Nichtzahlung wird weitere, auch rechtliche Schritte zur "
                "Folge haben."
            ),
        )
    if stufe == MahnStufe.STUFE_4.value:
        rechtlicher_kern, hinweis_intern = rechtlicher_kern_stufe4(rechtsordnung=rechtsordnung)
        return MahnstufeText(
            stufe=4,
            betreff="Letzte Zahlungsaufforderung",
            einleitung="Die unten aufgeschlüsselte Forderung ist trotz mehrerer vorangegangener Schreiben weiterhin unbeglichen.",
            kerntext=(
                f"Wir fordern Sie hiermit LETZTMALIG auf, den Gesamtbetrag binnen 14 Tagen ab "
                f"Zugang dieses Schreibens, also bis spätestens {frist_text}, vollständig zu "
                "begleichen."
            ),
            rechtlicher_kern=rechtlicher_kern,
            hinweis_intern=hinweis_intern,
        )
    raise ValueError(f"Unbekannte Mahnstufe {stufe!r} - dieses Modul kennt nur Stufe 1-4.")
