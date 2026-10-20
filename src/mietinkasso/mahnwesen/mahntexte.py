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

**Rechtsordnungs-Zuordnung des §30-Abs-2-Z1-MRG-Hinweises - KORRIGIERT
nach unabhängiger Abnahme (Codex, 20.10.2026, Primärquelle RIS §1 MRG):**
die vorherige Fassung behauptete, der Kündigungsschutz des § 30 MRG gelte
im MRG-Teilanwendungsbereich NICHT - DAS WAR FALSCH. § 1 Abs 4 MRG
erklärt für den Teilanwendungsbereich ausdrücklich (unter anderem) die
§§ 29 bis 36 MRG für anwendbar, damit grundsätzlich AUCH § 30 MRG. Sowohl
`OESTERREICH_MRG_VOLL` als auch `OESTERREICH_MRG_TEIL` erhalten deshalb
jetzt BEIDE einen § 30 Abs 2 Z1 MRG-Hinweis - für TEIL zusätzlich
AUSDRÜCKLICH gehedgt ("soweit... anwendbar ist"), weil § 1 Abs 4 MRG
mehrere, unterschiedliche Teilanwendungsfälle kennt und ob § 30 MRG
konkret einschlägig ist, vom jeweiligen Einzelfall abhängt - diese
Funktion behauptet das deshalb NIE unbedingt. Für `OESTERREICH_GEWERBE`
(Legacy-Enum) gilt weiterhin NUR § 1118 ABGB, OHNE damit zu behaupten,
Gewerbenutzung sei automatisch eine MRG-Ausnahme (das ist sie nicht - ob
das MRG auf ein konkretes Gewerbemietverhältnis voll/teilweise anwendbar
ist, ist eine vom Legacy-Enum NICHT abgedeckte, separate Frage, siehe
`hinweis_intern` unten und das künftige Rechtsachsen-Modell, Auftrag
HV-20261020-VERTRAGSDATENBASIS). KEINE Garantie für Rechtssicherheit im
Einzelfall - vor Produktivgang durch Codex/Markus fachlich zu bestätigen,
siehe `docs/hausverwaltung/OFFENE_PUNKTE.md`."""

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
#: 3 Ob 37/18i - Mitmieter-Zustellungsgrundsatz.
#:
#: **Korrektur nach unabhängiger Abnahme (Codex, 20.10.2026):** dieser
#: Hinweis ist ein interner Zustellprüf-/Prozesshinweis für das
#: Backoffice (Auftrag Markus: "allgemeine Zustellprüfhinweise/
#: Mitmieter-rechtssicherer-Zugang gehören intern, nicht als technischer
#: Prozesshinweis in Kundentext") - er war bisher FÄLSCHLICH Teil des an
#: den Mieter gehenden `rechtlicher_kern`-Textes. Er wird jetzt
#: AUSSCHLIESSLICH über `hinweis_intern` zurückgegeben, NIE mehr in den
#: Kundentext eingebaut.
_MITMIETER_HINWEIS_INTERN = (
    "Bestehen mehrere Mieter desselben Mietverhältnisses (Mitmieter), muss dieses Schreiben gegen "
    "JEDEN von ihnen gesondert ergehen und der Zugang für jede Person einzeln nachgewiesen werden "
    "(ein gewöhnlicher E-Mail-/SMTP-Versand oder die bloße Brief-Einlieferung bei der Post ist dafür "
    "NICHT automatisch ein rechtssicherer Zugangsnachweis) - NIE Teil des Kundentexts."
)

#: Legacy-Enum-Hinweis (Korrektur nach unabhängiger Abnahme, Codex
#: 20.10.2026): `OESTERREICH_GEWERBE` sagt NICHTS darüber aus, ob das MRG
#: (und damit der Kündigungsschutz der §§ 29-36 MRG) auf dieses
#: Gewerbemietverhältnis im Voll- oder Teilanwendungsbereich zutrifft -
#: Gewerbenutzung ist KEINE automatische MRG-Ausnahme (Auftrag Markus,
#: HV-20261020-VERTRAGSDATENBASIS). Der Stufe-4-Text nennt deshalb bewusst
#: nur § 1118 ABGB; die tatsächliche MRG-Anwendbarkeit ist erst mit dem
#: separaten Rechtsachsen-Modell (MRG-Anwendungsbereich getrennt von
#: Nutzungsart) fachlich zu klären, NIE automatisch aus dem Legacy-Enum
#: abgeleitet.
_GEWERBE_LEGACY_HINWEIS_INTERN = (
    "Legacy-Rechtsordnung GEWERBE sagt NICHTS darüber aus, ob das MRG (und damit der Kündigungsschutz "
    "der §§ 29-36 MRG) auf dieses Gewerbemietverhältnis im Voll- oder Teilanwendungsbereich zutrifft - "
    "Gewerbenutzung ist KEINE automatische MRG-Ausnahme. Der Stufe-4-Text nennt deshalb bewusst nur "
    "§ 1118 ABGB; die tatsächliche MRG-Anwendbarkeit ist fachlich gesondert zu klären (Auftrag "
    "HV-20261020-VERTRAGSDATENBASIS, Rechtsachsen-Modell), NIE automatisch aus diesem Legacy-Enum "
    "abgeleitet."
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


#: Rechtsordnungen, bei denen § 30 Abs 2 Z1 MRG grundsätzlich (bedingt,
#: siehe Moduldoc) in Betracht kommt - MRG-Vollanwendung UNBEDINGT (das
#: ganze Hauptstück gilt), MRG-Teilanwendung GEHEDGT (§ 1 Abs 4 MRG
#: erklärt §§ 29-36 MRG nur "soweit" der jeweilige Teilanwendungsfall das
#: vorsieht).
_RECHTSORDNUNGEN_MIT_PARAGRAPH_30 = frozenset(
    {Rechtsordnung.OESTERREICH_MRG_VOLL.value, Rechtsordnung.OESTERREICH_MRG_TEIL.value}
)

#: Weitere österreichische Rechtsordnungen MIT ABGB, aber OHNE (hier
#: behauptetem) § 30 MRG-Bezug - NUR § 1118 ABGB zweiter Fall.
_OESTERREICHISCHE_RECHTSORDNUNGEN_NUR_ABGB = frozenset(
    {
        Rechtsordnung.OESTERREICH_MRG_FREI.value,
        Rechtsordnung.OESTERREICH_WGG.value,
        Rechtsordnung.OESTERREICH_GEWERBE.value,
    }
)


def rechtlicher_kern_stufe4(*, rechtsordnung: str) -> tuple[str, str | None]:
    """Liefert `(rechtlicher_kern_text, hinweis_intern)` für die
    Stufe-4-Mahnung - NIE geraten, siehe Moduldoc "Unbekanntes nicht
    erfinden". `hinweis_intern` ist IMMER gesetzt (mindestens der
    Mitmieter-Zustellprüfhinweis, siehe `_MITMIETER_HINWEIS_INTERN`) -
    NIE Teil des an den Mieter gehenden Textes. Für eine Rechtsordnung
    OHNE hinterlegten österreichischen Gesetzesbaustein (z. B.
    `DEUTSCHLAND`) ist `rechtlicher_kern_text` bewusst ALLGEMEIN gehalten
    (keine erfundene ausländische Norm). Wird NIE für `UNGEKLAERT`
    aufgerufen - diese Rechtsordnung ist bereits strukturell von jeder
    Mahnung ausgeschlossen (`rechtsordnung_geklaert`)."""

    satz_folgen = (
        "Erfolgt innerhalb der oben genannten Frist keine vollständige Zahlung, werden wir diesen Fall "
        "einem Rechtsanwalt zur gerichtlichen Geltendmachung übergeben."
    )
    hinweise_intern = [_MITMIETER_HINWEIS_INTERN]

    if rechtsordnung in _RECHTSORDNUNGEN_MIT_PARAGRAPH_30:
        if rechtsordnung == Rechtsordnung.OESTERREICH_MRG_VOLL.value:
            paragraph_30_einleitung = "eine gerichtliche Aufkündigung wegen qualifizierten Zahlungsrückstands gemäß § 30 Abs 2 Z 1 MRG oder"
        else:
            paragraph_30_einleitung = (
                "- soweit der Kündigungsschutz der §§ 29 bis 36 MRG nach § 1 Abs 4 MRG auf dieses "
                "Mietverhältnis im Teilanwendungsbereich anwendbar ist - eine gerichtliche Aufkündigung "
                "wegen qualifizierten Zahlungsrückstands gemäß § 30 Abs 2 Z 1 MRG oder"
            )
        kern = (
            f"{satz_folgen} Dabei kommt gegebenenfalls {paragraph_30_einleitung} die vorzeitige "
            "Vertragsauflösung gemäß § 1118 zweiter Fall ABGB in Betracht, sofern die dafür "
            f"jeweils erforderlichen gesetzlichen Voraussetzungen vorliegen. {_1118_FRIST_HINWEIS} "
            f"Grundlage einer solchen Kündigung/Auflösung ist ausschließlich der oben gesondert "
            f"ausgewiesene RÜCKSTÄNDIGE MIETZINS - NICHT die gesondert ausgewiesenen Verzugszinsen "
            f"oder Mahnkosten. {_MAHNSCHREIBEN_IST_KEINE_KUENDIGUNG}"
        )
        return kern, "\n".join(hinweise_intern)

    if rechtsordnung in _OESTERREICHISCHE_RECHTSORDNUNGEN_NUR_ABGB:
        kern = (
            f"{satz_folgen} Dabei kommt gegebenenfalls die vorzeitige Vertragsauflösung gemäß "
            f"§ 1118 zweiter Fall ABGB in Betracht, sofern die dafür erforderlichen gesetzlichen "
            f"Voraussetzungen vorliegen. {_1118_FRIST_HINWEIS} Grundlage einer solchen Auflösung "
            f"ist ausschließlich der oben gesondert ausgewiesene RÜCKSTÄNDIGE MIETZINS - NICHT die "
            f"gesondert ausgewiesenen Verzugszinsen oder Mahnkosten. {_MAHNSCHREIBEN_IST_KEINE_KUENDIGUNG}"
        )
        if rechtsordnung == Rechtsordnung.OESTERREICH_GEWERBE.value:
            hinweise_intern.append(_GEWERBE_LEGACY_HINWEIS_INTERN)
        return kern, "\n".join(hinweise_intern)

    # Keine hinterlegte österreichische Rechtsgrundlage (z. B. eine
    # ausländische Rechtsordnung wie DEUTSCHLAND) - NIE eine Norm
    # erfinden, stattdessen allgemein formulieren und intern markieren.
    kern = (
        f"{satz_folgen} Die weiteren rechtlichen Schritte richten sich nach den für dieses "
        f"Mietverhältnis anwendbaren gesetzlichen Bestimmungen. {_MAHNSCHREIBEN_IST_KEINE_KUENDIGUNG}"
    )
    hinweise_intern.append(
        f"Rechtsordnung {rechtsordnung!r} hat KEINEN hinterlegten österreichischen MRG/ABGB-"
        "Gesetzestextbaustein (z. B. ausländische Rechtsordnung) - der Stufe-4-Text bleibt bewusst "
        "allgemein. Vor einer tatsächlichen RA-Übergabe/Kündigung ist die konkrete Rechtsgrundlage "
        "fachlich gesondert zu prüfen (NIE automatisch aus dieser Stufe abgeleitet)."
    )
    return kern, "\n".join(hinweise_intern)


def text_fuer_stufe(*, stufe: int, rechtsordnung: str, zahlungsfrist_bis: date) -> MahnstufeText:
    """Der vollständige Textbaustein für GENAU eine Stufe (1-4) - reine
    Funktion, von HTML/E-Mail/PDF GEMEINSAM verwendet (siehe Moduldoc).

    **Korrektur nach unabhängiger Abnahme (Codex, 20.10.2026):** der
    Kundentext nannte bisher ein konkretes Kalenderdatum ("...binnen 14
    Tagen ab Zugang dieses Schreibens, also bis spätestens {datum}") -
    das ist in sich widersprüchlich, weil der tatsächliche Zugang zum
    Versandzeitpunkt noch NICHT bekannt ist (er liegt per Definition in
    der Zukunft) und deshalb KEIN konkretes Datum als Tatsache behauptet
    werden darf (Auftrag Markus: "keine vorgetäuschten Zugangsdaten").
    Der Kundentext bleibt deshalb jetzt durchgehend REIN RELATIV ("binnen
    14 Tagen ab Zugang dieses Schreibens"), OHNE ein daraus abgeleitetes
    Kalenderdatum zu nennen. `zahlungsfrist_bis` wird deshalb hier NICHT
    mehr zur Textbildung verwendet - der Parameter bleibt (noch) in der
    Signatur, um die Aufrufer (`brief_pdf.py`, `mailversand_service.py`)
    nicht in diesem ohnehin schon umfangreichen Korrekturcommit zusätzlich
    umbauen zu müssen; eine Bereinigung der Signatur ist als kleiner
    Folgepunkt in `docs/hausverwaltung/OFFENE_PUNKTE.md` vermerkt."""

    if stufe == MahnStufe.STUFE_1.value:
        return MahnstufeText(
            stufe=1,
            betreff="Zahlungserinnerung",
            einleitung="Vielleicht haben Sie die Zahlung übersehen.",
            kerntext=(
                f"Für das unten genannte Mietverhältnis ist die unten aufgeschlüsselte Forderung "
                f"fällig und noch nicht bei uns eingelangt. Wir ersuchen Sie, den Gesamtbetrag "
                "binnen 14 Tagen ab Zugang dieses Schreibens vollständig zu begleichen. Sollte die "
                "Zahlung bereits erfolgt sein, betrachten Sie dieses Schreiben bitte als "
                "gegenstandslos und senden uns den Zahlungsbeleg."
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
                "Wir ersuchen Sie neuerlich, den unten aufgeschlüsselten Gesamtbetrag binnen 14 "
                "Tagen ab Zugang dieses Schreibens vollständig zu begleichen; eine weitere "
                "Verzögerung über diese Nachfrist hinaus können wir nicht in Aussicht stellen. "
                "Falls es Zahlungsschwierigkeiten gibt, melden Sie sich bitte umgehend zur Klärung "
                "bei uns - damit ist noch KEIN Zahlungsaufschub zugesagt; eine etwaige Stundung/"
                "Ratenvereinbarung bedarf einer gesonderten, ausdrücklichen Vereinbarung mit uns."
            ),
        )
    if stufe == MahnStufe.STUFE_3.value:
        return MahnstufeText(
            stufe=3,
            betreff="Nachdrückliche Zahlungsaufforderung",
            einleitung="Die unten aufgeschlüsselte Forderung ist trotz zweier vorangegangener Schreiben weiterhin unbeglichen.",
            kerntext=(
                "Wir fordern Sie hiermit nachdrücklich auf, den Gesamtbetrag binnen 14 Tagen ab "
                "Zugang dieses Schreibens vollständig zu begleichen. Eine weitere Nichtzahlung wird "
                "weitere, auch rechtliche Schritte zur Folge haben."
            ),
        )
    if stufe == MahnStufe.STUFE_4.value:
        rechtlicher_kern, hinweis_intern = rechtlicher_kern_stufe4(rechtsordnung=rechtsordnung)
        return MahnstufeText(
            stufe=4,
            betreff="Letzte Zahlungsaufforderung",
            einleitung="Die unten aufgeschlüsselte Forderung ist trotz mehrerer vorangegangener Schreiben weiterhin unbeglichen.",
            kerntext=(
                "Wir fordern Sie hiermit LETZTMALIG auf, den Gesamtbetrag binnen 14 Tagen ab Zugang "
                "dieses Schreibens vollständig zu begleichen."
            ),
            rechtlicher_kern=rechtlicher_kern,
            hinweis_intern=hinweis_intern,
        )
    raise ValueError(f"Unbekannte Mahnstufe {stufe!r} - dieses Modul kennt nur Stufe 1-4.")
