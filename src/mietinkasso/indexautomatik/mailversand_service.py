"""Deterministic daily MailOps wiring for existing business outboxes."""
from datetime import date, datetime, time, timedelta, timezone
from typing import NamedTuple

from sqlalchemy import select

from mietinkasso.auth.service import require_gesellschaft_access, require_schreibrecht
from mietinkasso.bank.repository import BankRepository, VertragZahlungszielBindungRepository
from mietinkasso.bank.service import BankImportService
from mietinkasso.domain.exceptions import (
    BindungInkonsistentError, ObjektAusgeschlossenError, TransportFehlerUngewissError, VorgangIdKonfliktError,
    ZahlungszielUngeklaertError,
)
from mietinkasso.indexautomatik.mailnachweis import RekonstruierterBeleg, nachweis_daten, versand_belegen
from mietinkasso.indexautomatik.mailops_client import MailOpsAuftrag, MailOpsClient
from mietinkasso.indexautomatik.mailops_transport import MailOpsTransportadapter
from mietinkasso.infrastructure.db.tables import (
    AuditEventTable, ErhoehungsschreibenTable, MahnFallTable, MahnLaufTable, VertragsendeErinnerungTable,
)
from mietinkasso.mahnwesen import brief_sftp
from mietinkasso.mahnwesen.brief_pdf import AdressfehlerError
from mietinkasso.mahnwesen.brief_pdf import Absender as BriefAbsender
from mietinkasso.mahnwesen.brief_pdf import Forderungszeile as BriefForderungszeile
from mietinkasso.mahnwesen.brief_pdf import Zahlungsziel as BriefZahlungsziel
from mietinkasso.mahnwesen.brief_pdf import Zinssegment as BriefZinssegment
from mietinkasso.mahnwesen.brief_pdf import erzeuge_mahnbrief_pdf
from mietinkasso.mahnwesen.kosten import snapshot_aus_json, zins_bis_einschliesslich
from mietinkasso.mahnwesen.mahntexte import text_fuer_stufe
from mietinkasso.mahnwesen.kosten_repository import MahnkostenRepository
from mietinkasso.mahnwesen.kosten_service import MahnkostenService
from mietinkasso.mahnwesen.kontoauszug_anhang import haenge_kontoauszug_pdf_an, journal_als_text
from mietinkasso.mahnwesen.mahnbrief_archiv import MahnbriefArchivRepository
from mietinkasso.mahnwesen.manueller_mahnversand_repository import ManuellerMahnversandRepository
from mietinkasso.mahnwesen.manueller_mahnversand_service import ManuellerMahnversandService
from mietinkasso.op.kontoauszug_pdf import erzeuge_kontoauszug_pdf
from mietinkasso.op.kontoblatt import lade_kontoblatt
from mietinkasso.bank.ruecklastschriftkosten_service import RuecklastschriftkostenRepository
from mietinkasso.mahnwesen.repository import (
    BriefAnbieterProfilRepository, MahnEmpfaengerZugangRepository, MahnFallRepository, MahnKanalregelRepository,
    MahnLaufRepository, MahnPolicyRepository, MahnTageslaufRepository, VertragMitmieterRepository,
)
from mietinkasso.mahnwesen.service import MahnwesenService, VersandErgebnis
from mietinkasso.op.repository import OPRepository
from mietinkasso.op.service import OPService


def mail_client_fuer(settings):
    if not settings.hv_mail_socket_path or not settings.hv_mail_token_file:
        return None
    return MailOpsClient(socket_path=settings.hv_mail_socket_path, token_file=settings.hv_mail_token_file)


def bank_freigabe_ableiten(bank_repo, bank_service, gesellschaft_id, vertrag_id):
    accounts = bank_repo.list_bank_konten(gesellschaft_id=gesellschaft_id)
    if not accounts:
        return None, False
    dates = [bank_service.bankvollstaendigkeit_bestaetigt_bis(a.id) for a in accounts]
    confirmed = min(dates) if all(d is not None for d in dates) else None
    unclear = any(bank_service.hat_ungeklaerte_relevante_eingaenge(
        bank_konto_id=a.id, vertrag_id=vertrag_id) for a in accounts)
    return confirmed, unclear


def _eur_text(cent: int) -> str:
    return f"{cent/100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def bank_konto_fuer_gesellschaft_eindeutig(bank_repo, gesellschaft_id: str):
    """Liefert das EINE Bankkonto dieser Gesellschaft, wenn (und nur wenn)
    es eindeutig ist - `None` sowohl bei FEHLENDER (kein Konto hinterlegt)
    als auch bei MEHRDEUTIGER (mehr als ein Konto, keine ausgezeichnete
    "Hauptkonto"-Markierung existiert) Bindung. Es gibt bewusst KEINE
    Rangfolge/Heuristik (z. B. "das zuerst angelegte"/"das zuletzt
    verwendete") - eine echte Mehrdeutigkeit bleibt ein Prüffall, nie eine
    geratene Wahl (Nutzervorgabe 20.10.2026: "keine falsche globale
    Kontoverbindung"). Rein gesellschaftsweit - für die tatsächliche
    Versandauflösung siehe `bank_konto_fuer_vertrag_eindeutig`, das bei
    MEHREREN Konten zusätzlich eine bestätigte Vertragsbindung
    berücksichtigt."""

    konten = bank_repo.list_bank_konten(gesellschaft_id=gesellschaft_id)
    if len(konten) != 1:
        return None
    return konten[0]


def bank_konto_fuer_vertrag_eindeutig(bank_repo, zuordnung_repo, *, gesellschaft_id: str, vertrag_id: str):
    """Liefert das für DIESEN Vertrag tatsächlich maßgebliche Bankkonto -
    bei genau einem Bankkonto der Gesellschaft ist das bereits eindeutig
    (wie bisher); bei MEHREREN Bankkonten derselben Gesellschaft löst NUR
    eine ausdrücklich bestätigte `VertragZahlungszielBindungTable`-Zeile
    die Mehrdeutigkeit auf (Nutzervorgabe 21.10.2026: "keine Auswahl nach
    erster Zeile oder Bezeichnung"). Fehlt diese Bindung, oder verweist
    sie auf ein Konto, das für diese Gesellschaft nicht mehr gelistet ist
    (z. B. zwischenzeitlich entfernt/umgehängt), bleibt die Rückgabe
    `None` - das ist ein gezielter Prüffall, nie eine geratene Wahl."""

    konten = bank_repo.list_bank_konten(gesellschaft_id=gesellschaft_id)
    if len(konten) == 1:
        return konten[0]
    if len(konten) == 0 or zuordnung_repo is None:
        return None
    bindung = zuordnung_repo.aktuelle_bindung(vertrag_id)
    if bindung is None:
        return None
    return next((k for k in konten if k.id == bindung.bank_konto_id), None)


def zahlungsziel_fuer_mahnung(
    bank_repo, stammdaten_repo, *, gesellschaft_id: str, vertrag_id: str, stufe: int, zuordnung_repo=None,
) -> BriefZahlungsziel:
    """Baut das Zahlungsziel für Brief UND E-Mail aus genau EINER Quelle
    (Abnahme-Folgefix Codex 20.10.2026) - `verwendungszweck` wird
    deterministisch aus Vertrag/Stufe gebildet, NIE frei erfunden.

    Auftrag HV-20261020-MAHN-FORTSETZUNG (Nutzervorgabe 20.10.2026:
    "Zahlungsziel aus geprüfter Vertrag/Objekt/Gesellschaft-Bindung statt
    globaler IBAN"): die vorherige Fassung las AUSSCHLIESSLICH eine
    einzige globale Einstellung (`settings.brief_zahlungsziel_iban`/
    `_kontoinhaber`) - das kann bei mehreren Vermietergesellschaften mit
    jeweils EIGENEM Bankkonto strukturell nur für höchstens eine von ihnen
    richtig sein (für jede andere wäre es eine FALSCH adressierte Zahlung).

    Die IBAN wird deshalb jetzt IMMER aus der TATSÄCHLICHEN, bereits
    bestehenden `BankKontoTable`-Bindung der Vertrags-Gesellschaft
    abgeleitet (`vertrag.gesellschaft_id`, dieselbe Bindung wie
    Bankabgleich/-vollständigkeit) - KEIN Rateschritt. Der Kontoinhaber
    ist dabei schlicht der registrierte Name DIESER Gesellschaft
    (`GesellschaftTable.name`) - ein Geschäftskonto lautet auf seine
    Inhabergesellschaft, keine separate Eingabe nötig/erfunden.

    **Korrektur (HV-20261021-ABSCHLUSS, Nutzervorgabe 21.10.2026):** bei
    MEHREREN Bankkonten derselben Gesellschaft wird die Mehrdeutigkeit
    jetzt über `bank_konto_fuer_vertrag_eindeutig` aufgelöst - NUR eine
    ausdrücklich bestätigte Vertragsbindung (`zuordnung_repo`, siehe dort)
    zählt, nie "das erste Konto"/"das nach Bezeichnung passende". Fehlt
    für die Gesellschaft ein Bankkonto ODER bleibt es bei MEHREREN ohne
    bestätigte Bindung, bleibt die Rückgabe weiterhin `iban=None`/
    `kontoinhaber=None` - DIESER ungeklärte Fall ist aber NICHT mehr bloß
    ein unspezifischer Hinweistext im sonst unverändert versandten
    Schreiben: der tatsächliche Versand selbst wird dafür jetzt vom
    Aufrufer als gezielter Prüffall gestoppt (siehe `MahnwesenService`s
    `zahlungsziel_geklaert`-Gate), damit weder ein falsches noch ein
    unbrauchbares Zahlungsziel gemahnt wird."""

    verwendungszweck = f"Mahnung Stufe {stufe}, Vertrag {vertrag_id}"
    konto = bank_konto_fuer_vertrag_eindeutig(bank_repo, zuordnung_repo, gesellschaft_id=gesellschaft_id, vertrag_id=vertrag_id)
    if konto is None:
        return BriefZahlungsziel(iban=None, kontoinhaber=None, verwendungszweck=verwendungszweck)
    gesellschaft = stammdaten_repo.get_gesellschaft(gesellschaft_id)
    kontoinhaber = gesellschaft.name if gesellschaft is not None else None
    if not konto.iban or not kontoinhaber:
        return BriefZahlungsziel(iban=None, kontoinhaber=None, verwendungszweck=verwendungszweck)
    return BriefZahlungsziel(iban=konto.iban, kontoinhaber=kontoinhaber, verwendungszweck=verwendungszweck)


def zahlungsziel_geklaert(bank_repo, zuordnung_repo, stammdaten_repo, *, gesellschaft_id: str, vertrag_id: str) -> bool:
    """Der Versand-Gate-Wert für `MahnwesenService`s `zahlungsziel_
    geklaert`-Parameter (Nutzervorgabe 21.10.2026, HV-20261021-ABSCHLUSS:
    "Vor tatsächlichem Versand muss das als gezielter Prüffall stoppen,
    damit weder ein falsches noch ein unbrauchbares Zahlungsziel gemahnt
    wird") - `True` nur, wenn `zahlungsziel_fuer_mahnung` tatsächlich eine
    konkrete IBAN liefern würde (dieselbe Auflösung, EINE Quelle, nie
    eine zweite, potenziell abweichende Prüfung). Die Stufe selbst
    beeinflusst die IBAN-Auflösung nicht (nur den Verwendungszweck-Text),
    daher hier irrelevant."""

    return zahlungsziel_fuer_mahnung(
        bank_repo, stammdaten_repo, gesellschaft_id=gesellschaft_id, vertrag_id=vertrag_id, stufe=0,
        zuordnung_repo=zuordnung_repo,
    ).iban is not None


def _mahnkosten_text_baustein(vorschau) -> str:
    """Baut den Kosten-/Zinsnachweis-Absatz für den TATSÄCHLICH gesendeten
    Mahntext - GENAU aus der übergebenen `MahnkostenVorschau`, die
    unmittelbar danach 1:1 an `MahnkostenService.buche_vorschau` übergeben
    wird (siehe `kosten_service.py`-Moduldoc). Es wird hier NICHTS neu
    berechnet, nur formatiert - Text und Buchung können dadurch nie
    auseinanderlaufen. Liefert einen leeren String, wenn es nichts
    Neues zu vermerken gibt (kein Delta, keine neue Gebühr)."""

    if vorschau is None:
        return ""
    zeilen: list[str] = []
    if vorschau.bereits_offene_mahnkosten_cent > 0:
        zeilen.append(f"Noch offene Kosten aus früheren Mahnläufen: {_eur_text(vorschau.bereits_offene_mahnkosten_cent)} EUR.")
    if vorschau.bereits_gebuchte_zinsen_cent > 0:
        zeilen.append(f"Bereits verrechnete Verzugszinsen aus früheren Mahnläufen: {_eur_text(vorschau.bereits_gebuchte_zinsen_cent)} EUR.")
    neu_zinsen = vorschau.neue_zinsen_delta_cent
    if neu_zinsen > 0:
        satz_text = f"{vorschau.zinssatz_prozent} % p.a." if vorschau.zinssatz_prozent is not None else "mehreren Sätzen (siehe Zeiträume unten)"
        # `zins_bis`/`segment.bis` sind EXKLUSIV (siehe `kosten.py::
        # ZinsSegment`-Docstring) - für den Mieter wird der tatsächlich
        # letzte verzinste Tag ausgewiesen (`zins_bis_einschliesslich`),
        # nie das exklusive Enddatum selbst.
        zeitraum_text = (
            f"{vorschau.zins_von.strftime('%d.%m.%Y')} bis {zins_bis_einschliesslich(vorschau.zins_bis).strftime('%d.%m.%Y')} (einschließlich)"
            if vorschau.zins_von and vorschau.zins_bis else ""
        )
        zeilen.append(f"Neu anzusetzende Verzugszinsen: {_eur_text(neu_zinsen)} EUR ({satz_text}, Zeitraum {zeitraum_text}).")
        if len(vorschau.zins_segmente) > 1:
            for segment in vorschau.zins_segmente:
                if segment.zinsen_cent <= 0:
                    continue
                zeilen.append(
                    f"  - {segment.von.strftime('%d.%m.%Y')} bis {zins_bis_einschliesslich(segment.bis).strftime('%d.%m.%Y')} "
                    f"(einschließlich): {_eur_text(segment.zinsen_cent)} EUR ({segment.satz_prozent} % p.a.)."
                )
    if vorschau.zins_teilweise_ungeklaert:
        zeilen.append(
            "Für einen Teil des Verzugszeitraums ist die Zinsberechnungsgrundlage noch nicht abschließend "
            "geklärt; dieser Anteil wird gesondert nachgetragen, sobald sie vorliegt, und berührt die "
            "Hauptforderung nicht."
        )
    gebuehr_gesamt = sum(s.betrag_cent for s in vorschau.gebuehr_segmente)
    if gebuehr_gesamt > 0:
        zeilen.append(
            f"Neue Mahnspesen ({vorschau.gebuehr_rechtsgrundlage}): {_eur_text(gebuehr_gesamt)} EUR "
            f"für {len(vorschau.gebuehr_segmente)} bislang noch nicht bepauschalte Forderung(en)."
        )
    if vorschau.mahnpauschale_cent:
        zeilen.append(
            f"Mahnkostenpauschale ({vorschau.mahnpauschale_rechtsgrundlage}): "
            f"{_eur_text(vorschau.mahnpauschale_cent)} EUR für dieses Mahnschreiben."
        )
    if not zeilen:
        return ""
    return "\n".join(zeilen) + "\n\n"


#: Auftrag HV-20261020-VIER-MAHNSTUFEN, Nutzervorgabe wörtlich: "Zwei
#: deterministische Prüfläufe je Monat, Standard 10. und 25. um 09:00
#: Europe/Vienna (Implementierungsentscheidung; veränderbar)" - technisch
#: präzisiert (Server-Readback 20.10.2026): läuft NICHT als eigener
#: 09:00-Termin, sondern als Tagesbedingung INNERHALB des bestehenden,
#: bereits täglich um 08:15 Europe/Vienna laufenden `mietinkasso-fristen-
#: tag.timer` (`scripts/indexautomatik_taegliche_pflege.py`) - KEIN
#: zweiter, konkurrierender Scheduler. `heute` kommt hier bereits als
#: Europe/Vienna-Kalendertag (`indexautomatik/zeit.py::heute_wien`) an.
_VIER_STUFEN_PRUEFTAGE = frozenset({10, 25})


class _BriefKontext(NamedTuple):
    """Der empfängerUNABHÄNGIGE Brief-Inhalt EINES Mahnlaufs (Auftrag
    HV-20261021-ABSCHLUSS) - Forderungszeilen/Zinssegmente/Gesamtbetrag/
    Zahlungsziel/Absender sind für JEDEN Empfänger (Hauptdebitor, jeder
    Mitmieter, eine spätere Einzel-Nachsendung) IDENTISCH; nur Name/
    Adresse unterscheiden sich je Person (siehe `HVMailversandService.
    _rendere_brief_pdf`). Named statt eines bloßen Tupels, damit eine
    Positionsverwechslung beim Aufrufen nicht unbemerkt bleibt."""

    contract: object
    obj: object
    unit: object
    absender: object
    forderungszeilen: list
    zins_segmente: list
    gesamtbetrag_cent: int
    zinssatz_einheitlich_text: str | None
    deadline: date
    zahlungsziel: object


class HVMailversandService:
    def __init__(self, session_factory, bundle, settings, *, client=None):
        self.sf, self.bundle, self.settings = session_factory, bundle, settings
        self.client = client if client is not None else mail_client_fuer(settings)
        self.bank_repo = BankRepository(session_factory)
        self.zahlungsziel_bindung_repo = VertragZahlungszielBindungRepository(session_factory)
        self.op_service = OPService(OPRepository(session_factory), bundle.stammdaten_repository)
        self.bank_service = BankImportService(self.bank_repo, bundle.stammdaten_repository, self.op_service)
        self.mahn_repo = MahnFallRepository(session_factory)
        self.mahnlauf_repo = MahnLaufRepository(session_factory)
        self.policy_repo = MahnPolicyRepository(session_factory)
        self.mahnkosten_repo = MahnkostenRepository(session_factory)
        # Kanalregel/Brief-Anbieterprofil (Auftrag HV-20260914-MAHNUNG-
        # BRIEF): Repositories bewusst IMMER verdrahtet - der Default-
        # Kanal bleibt trotzdem EMAIL für beide Stufen, solange Codex
        # keine Kanalregel freigibt (siehe `MahnwesenService._resolve_
        # kanal`-Docstring). `brief_transport_verfuegbar` ist jetzt (Teil 3,
        # HV-20261019-VERSAND-KOSTEN-ABSCHLUSS) real an `brief_sftp.
        # konfiguration_aus_settings` gekoppelt statt hartkodiert `False` -
        # in dieser Entwicklungsumgebung bleibt sie trotzdem IMMER
        # geschlossen, weil nie echte Host/Credentials/Hostkey-Werte
        # gesetzt sind (kein erfundener Live-Provider); echt wirksam wird
        # sie erst, sobald Codex diese vier Werte auf dem Server setzt.
        self.kanalregel_repo = MahnKanalregelRepository(session_factory)
        self.brief_anbieterprofil_repo = BriefAnbieterProfilRepository(session_factory)
        self.mahnbrief_archiv_repo = MahnbriefArchivRepository(session_factory)
        # Auftrag HV-20261020-VIER-MAHNSTUFEN: Mitmieter-/Zugangsnachweis-
        # Repositories IMMER verdrahtet (wie Kanalregel/Brief-
        # Anbieterprofil oben) - wirksam wird das Vier-Stufen-Verhalten
        # trotzdem erst, sobald eine Policy mit `vier_stufen_profil_
        # aktiv=True` tatsächlich freigegeben ist (siehe `MahnwesenService.
        # _naechste_stufe_fuer_forderung`/`_pruefe_vorstufe_abstand`).
        self.mitmieter_repo = VertragMitmieterRepository(session_factory)
        self.empfaenger_zugang_repo = MahnEmpfaengerZugangRepository(session_factory)
        # Auftrag HV-20261022-EIN-KONTOBLATT: kontrollierter Backoffice-
        # Import tatsächlich manuell versendeter Mahnungen - IMMER
        # verdrahtet (wie Kanalregel/Mitmieter oben), siehe
        # `manueller_mahnversand_repository.py`/`manueller_mahnversand_
        # service.py`-Moduldoc.
        self.manueller_mahnversand_repo = ManuellerMahnversandRepository(session_factory)
        # Auftrag HV-20261022-EIN-KONTOBLATT, Nachbesserung Markus
        # 22.10.2026 ("bisherige Mail-/Briefpipeline nutzt noch keinen
        # kanonischen Kontoauszug"): der EINE kanonische Kontoauszug wird
        # direkt in diesen Service eingebunden (`_sende_brief`/`_baue_
        # email_mahntext`), nicht nur als separater Backoffice-Download
        # angeboten - `ruecklastschriftkosten_repo` ist dafür eine der
        # drei Zutaten von `lade_kontoblatt` (neben `mahnkosten_repo`/
        # `mahnlauf_repo`, die bereits oben verdrahtet sind).
        self.ruecklastschriftkosten_repo = RuecklastschriftkostenRepository(session_factory)
        # Korrektur nach unabhängiger Abnahme (Codex, 20.10.2026,
        # reproduzierter Fehler): DB-seitiger, über den UNIQUE-Constraint
        # abgesicherter Claim "höchstens ein tatsächlicher Versandversuch
        # je (Vertrag, Prüfdatum)" - siehe `MahnTageslaufTable`-Docstring
        # und `mahnlauf()` unten. IMMER verdrahtet (nicht optional) - die
        # Exklusivität ist eine allgemeine Korrektheitseigenschaft, nicht
        # auf das Vier-Stufen-Profil beschränkt.
        self.tageslauf_repo = MahnTageslaufRepository(session_factory)
        self._brief_sftp_konfiguration = brief_sftp.konfiguration_aus_settings(settings)
        self.brief_sftp_adapter = (
            brief_sftp.PostSftpTransportadapter(self._brief_sftp_konfiguration)
            if self._brief_sftp_konfiguration is not None else None
        )
        self.mahnkosten_service = MahnkostenService(
            self.mahnkosten_repo, self.op_service, bundle.stammdaten_repository,
            brief_anbieterprofil_repository=self.brief_anbieterprofil_repo,
        )
        self.mahn_service = MahnwesenService(self.mahn_repo, bundle.stammdaten_repository,
            self.op_service, self.policy_repo, bank_stand_max_age_days=settings.bank_stand_max_age_days,
            mahnkosten_service=self.mahnkosten_service, mahnlauf_repository=self.mahnlauf_repo,
            kanalregel_repository=self.kanalregel_repo, brief_anbieterprofil_repository=self.brief_anbieterprofil_repo,
            brief_transport_verfuegbar=self.brief_sftp_adapter is not None,
            mitmieter_repository=self.mitmieter_repo, empfaenger_zugang_repository=self.empfaenger_zugang_repo,
            manueller_mahnversand_repository=self.manueller_mahnversand_repo)
        self.manueller_mahnversand_service = ManuellerMahnversandService(
            repository=self.manueller_mahnversand_repo, stammdaten_repository=bundle.stammdaten_repository,
            op_service=self.op_service, mahnkosten_service=self.mahnkosten_service,
        )

    def owner_senden(self, auftrag):
        if self.client is None or not self.settings.hv_mail_allowlist_bestaetigt:
            raise ValueError("Privater Mailweg ist nicht freigegeben.")
        return self.client.senden(MailOpsAuftrag(
            auftrag["idempotenzschluessel"], "VERTRAGSENDE", auftrag["empfaenger"], "Markus Bauer",
            "Mietvertrag endet: Entscheidung zur Verlängerung", auftrag["text"],
            "OWNER-ONLY-DREI-KALENDERMONATE:" + auftrag["vertrag_id"]))

    def monatsbericht_senden(self, auftrag):
        """Umfang B (HV-20260919-INDEX-MONATSBERICHT): eigener Kind
        `INDEX_MONATSBERICHT`, unabhängig von `owner_senden`/
        `index_senden` - siehe `mailops_client.py` für die harte
        Empfängergrenze und `infrastructure/config.py` für den eigenen
        Aktivierungsschalter."""

        if self.client is None or not self.settings.hv_mail_allowlist_bestaetigt:
            raise ValueError("Privater Mailweg ist nicht freigegeben.")
        return self.client.senden(MailOpsAuftrag(
            auftrag["idempotenzschluessel"], "INDEX_MONATSBERICHT", auftrag["empfaenger"], "Markus Bauer",
            f"Index-Monatsbericht {auftrag['periode']}", auftrag["text"],
            "OWNER-ONLY-MONATSBERICHT:" + auftrag["periode"]))

    def index_senden(self, *, ctx, row_id, heute):
        if self.client is None:
            raise ValueError("Privater Mailweg ist nicht eingerichtet.")
        return self.bundle.outbox_service.versenden(ctx=ctx, erhoehungsschreiben_id=row_id, heute=heute,
            send_enabled=self.settings.indexautomatik_send_enabled,
            mailops_allowlist_bestaetigt=self.settings.hv_mail_allowlist_bestaetigt,
            transport=MailOpsTransportadapter(self.client))

    def _friere_erwartete_empfaenger_ein(self, mahnlauf):
        """Auftrag HV-20261021-ABSCHLUSS: friert die zu DIESEM Zeitpunkt
        (also VOR jedem Providerkontakt, kanalunabhängig - EMAIL UND
        BRIEF) tatsächlich erwarteten Empfänger ein (Hauptdebitor + jeder
        aktive, BESTÄTIGTE Mitmieter, `VertragMitmieterRepository.
        list_bestaetigte_mitmieter` - NIE ein Vertreter, NIE eine noch
        ungeklärte Angabe). Dasselbe Prinzip wie der Kosten-/
        Inhaltssnapshot: durabel VOR dem eigentlichen Providerkontakt,
        damit ein späterer Absturz/eine erst über die Status-Abgleich-/
        Recovery-Strecke bestätigte Gruppe keinen bereits tatsächlich
        erfolgten Kopieversand mehr "vergessen" kann.

        Gibt `(bestaetigte_mitmieter, empfaenger_zugang_je_mitmieter)`
        zurück - Letzteres eine `{mitmieter_id: MahnEmpfaengerZugangTable}`-
        Abbildung (inkl. `None` für den Hauptdebitor), leer, wenn kein
        `empfaenger_zugang_repo` konfiguriert ist (unverändertes
        Alt-Verhalten für einen Aufrufer ohne dieses Repository)."""

        bestaetigte_mitmieter = (
            self.mitmieter_repo.list_bestaetigte_mitmieter(mahnlauf.vertrag_id)
            if self.mitmieter_repo is not None else []
        )
        empfaenger_zugang_je_mitmieter: dict[int | None, object] = {}
        if self.empfaenger_zugang_repo is not None:
            st = self.bundle.stammdaten_repository
            vertrag = st.get_vertrag(mahnlauf.vertrag_id)
            konto = st.get_konto_by_vertrag(mahnlauf.vertrag_id) if vertrag is not None else None
            debitor = st.get_debitor(konto.debitor_id) if konto is not None else None
            haupt_name = debitor.name if debitor is not None else "Hauptmieter"
            erwartete_empfaenger = [(None, haupt_name)] + [(m.id, m.name) for m in bestaetigte_mitmieter]
            self.empfaenger_zugang_repo.erstelle_erwartete_eintraege(
                mahnlauf_id=mahnlauf.id, empfaenger=erwartete_empfaenger, erstellt_von="hv-mailversand",
            )
            empfaenger_zugang_je_mitmieter = {
                eintrag.mitmieter_id: eintrag for eintrag in self.empfaenger_zugang_repo.fuer_mahnlauf(mahnlauf.id)
            }
        return bestaetigte_mitmieter, empfaenger_zugang_je_mitmieter

    def _baue_email_mahntext(self, *, mahnlauf, mitglieder, heute, vorschau):
        """Komponiert GENAU denselben Mahn-E-Mail-Inhalt für eine
        gegebene, eingefrorene Mitgliedergruppe UND eine gegebene
        (frisch berechnete ODER aus `MahnLaufTable.
        mahnkosten_snapshot_json` rekonstruierte) Kostenvorschau - EINE
        gemeinsame Quelle für den ursprünglichen Hauptversand UND eine
        spätere sichere Einzel-Nachsendung (`nachsenden_einzelempfaenger`,
        Auftrag HV-20261021-ABSCHLUSS), damit beide NIE auseinanderlaufen
        können und eine Nachsendung NIE eine eigene, zweite
        Kostenberechnung anstößt ("keine doppelte Gebührenbuchung").
        Prüft weiterhin `debt.rest_cent != mitglied.snapshot
        ["betrag_cent"]` - ein zwischenzeitlich veränderter
        Forderungsstand lässt BEIDE Aufrufer (Erstversand UND
        Nachsendung) gleichermaßen mit `ValueError` scheitern, nie eine
        veraltete Zahl verschicken.

        Gibt `(subject, text, empfaenger_name, empfaenger_email,
        policy_version)` zurück - `text` ist IMMER an den Hauptdebitor
        adressiert ("Guten Tag {empfaenger_name},..."); eine Mitmieter-
        Kopie ersetzt diese eine Zeile (siehe Aufrufer)."""

        st = self.bundle.stammdaten_repository
        contract = st.get_vertrag(mahnlauf.vertrag_id)
        obj = st.objekt_fuer_vertrag(mahnlauf.vertrag_id)
        unit = st.get_einheit(contract.einheit_id)
        account = st.get_konto_by_vertrag(mahnlauf.vertrag_id)
        offene = {f.op_position_id: f for f in self.op_service.offene_forderungen(account.id, heute=heute)}

        posten_zeilen = []
        gesamt_cent = 0
        for mitglied in mitglieder:
            debt = offene.get(mitglied.forderung_op_position_id)
            if debt is None or debt.rest_cent != mitglied.snapshot["betrag_cent"] or debt.faelligkeit is None:
                raise ValueError("Offene Forderung hat sich verändert.")
            gesamt_cent += debt.rest_cent
            periode_text = f"{debt.leistungsperiode}: " if debt.leistungsperiode else ""
            zeile = f"- {periode_text}{_eur_text(debt.rest_cent)} EUR, fällig seit {debt.faelligkeit.strftime('%d.%m.%Y')}"
            # Abnahme-Folgefix (Codex 20.10.2026): "keine vollständige
            # Periode/Position/Ursprungsbetrag/angerechnete Zahlungen/
            # Rest-Tabelle" - bei einer bereits teilweise angerechneten
            # Zahlung wird das HIER explizit genannt, statt nur den
            # (für den Mieter sonst unerklärten) Restbetrag zu zeigen.
            if debt.betrag_cent != debt.rest_cent:
                angerechnet = debt.betrag_cent - debt.rest_cent
                zeile += (
                    f" (ursprünglich {_eur_text(debt.betrag_cent)} EUR, davon bereits angerechnet "
                    f"{_eur_text(angerechnet)} EUR, Rest {_eur_text(debt.rest_cent)} EUR)"
                )
            posten_zeilen.append(zeile)

        kosten_text = _mahnkosten_text_baustein(vorschau)
        gesamtbetrag_cent = gesamt_cent + (
            (vorschau.bereits_offene_mahnkosten_cent + vorschau.zusaetzlicher_betrag_cent) if vorschau is not None else 0
        )
        deadline = heute + timedelta(days=contract.zahlungsfrist_tage)
        mahntext = text_fuer_stufe(stufe=mahnlauf.stufe, rechtsordnung=contract.rechtsordnung, zahlungsfrist_bis=deadline)
        subject = mahntext.betreff
        anker = mitglieder[0]
        empfaenger_name = anker.snapshot["empfaenger_name"]
        rechtlicher_absatz = f"\n\n{mahntext.rechtlicher_kern}" if mahntext.rechtlicher_kern else ""
        zahlungsziel = zahlungsziel_fuer_mahnung(
            self.bank_repo, st, gesellschaft_id=contract.gesellschaft_id, vertrag_id=mahnlauf.vertrag_id, stufe=mahnlauf.stufe,
            zuordnung_repo=self.zahlungsziel_bindung_repo,
        )
        if zahlungsziel.iban and zahlungsziel.kontoinhaber:
            zahlungsziel_text = (
                "\n\nBitte überweisen Sie den Gesamtbetrag binnen 14 Tagen ab Zugang dieser Nachricht auf "
                f"folgendes Konto: IBAN {zahlungsziel.iban}, Kontoinhaber {zahlungsziel.kontoinhaber}, "
                f"Verwendungszweck: {zahlungsziel.verwendungszweck}."
            )
        else:
            zahlungsziel_text = (
                "\n\nBitte begleichen Sie den Gesamtbetrag binnen 14 Tagen ab Zugang dieser Nachricht auf "
                "das Ihnen für dieses Mietverhältnis bekannt gegebene Konto."
            )
        # Auftrag HV-20261022-EIN-KONTOBLATT, Nachbesserung Markus
        # 22.10.2026: der E-Mail-Kanal kennt KEINE Dateianhänge
        # (`mailops_client.py::MailOpsAuftrag` ist reiner Text) - statt
        # eine nicht existierende Anhangsfähigkeit vorzutäuschen, wird
        # DIESELBE kanonische Journal-Quelle (`lade_kontoblatt`, identisch
        # zu Kontoansicht/Mieterakte/Briefanhang) als Text-Kurzfassung
        # eingebettet. Zeigt NUR den zum Versandzeitpunkt BEREITS
        # gebuchten Stand (die neuen Zinsen/Gebühren DIESES Mahnlaufs
        # werden erst NACH dem Versand gebucht, siehe `MahnwesenService.
        # versende_mahnlauf` - keine erfundene Vorwegnahme).
        kontoauszug_text = journal_als_text(lade_kontoblatt(
            konto_id=account.id, vertrag_id=mahnlauf.vertrag_id, op_service=self.op_service,
            mahnkosten_repo=self.mahnkosten_repo, ruecklastschriftkosten_repo=self.ruecklastschriftkosten_repo,
            mahnlauf_repo=self.mahnlauf_repo,
        ))
        text = (
            f"Guten Tag {empfaenger_name},\n\n"
            f"{mahntext.einleitung} {mahntext.kerntext}\n\n"
            f"Für {obj.bezeichnung}, {unit.bezeichnung}, ist folgende Forderung offen:\n\n"
            + "\n".join(posten_zeilen) +
            f"\n\nHauptforderung gesamt: {_eur_text(gesamt_cent)} EUR.\n\n"
            + kosten_text +
            f"Gesamtbetrag (Hauptforderung inkl. noch offener früherer Kosten und neuer zulässiger "
            f"Zinsen/Gebühr): {_eur_text(gesamtbetrag_cent)} EUR."
            + rechtlicher_absatz
            + zahlungsziel_text +
            "\n\nBei einer inzwischen erfolgten Zahlung senden Sie uns bitte den Zahlungsbeleg. "
            "Bei Fragen zur Forderung antworten Sie bitte auf diese Nachricht."
            + f"\n\n---\nKontoauszug (Stand vor dieser Mahnung):\n\n{kontoauszug_text}"
        )
        return subject, text, empfaenger_name, anker.snapshot["empfaenger_email"], anker.policy_version

    def _dispatch_mahnlauf(self, *, ctx, mahnlauf, heute, confirmed, unclear, zahlungsziel_geklaert_wert=True) -> VersandErgebnis:
        """Versendet EINEN bereits über `MahnwesenService.plane_mahnlauf`
        gebildeten, eingefrorenen Mahnlauf - GENAU EIN tatsächlicher
        externer Mailversand für die vollständige Mitgliedergruppe
        (Rückprüfung 14.09.2026, Risiko 1+2: keine kleinste-Id-Leader-
        Heuristik mehr, die atomare Exklusivität hängt an der
        `MahnLaufTable`-Zeile selbst, siehe `MahnwesenService.
        versende_mahnlauf`). Die alte Prüfung auf feste
        MahnPolicy-Pauschalen (`snapshot["gebuehr_cent"]`/
        `snapshot["zinsen_prozent"]`) entfällt bewusst: `MahnkostenService`
        ist die EINZIGE Quelle für Zinsen/Gebühren, eine daneben laufende
        Pauschale aus der Policy würde diese Vereinheitlichung wieder
        aufheben."""

        send_enabled = bool(self.client and self.settings.send_enabled and self.settings.hv_mail_allowlist_bestaetigt)
        vorschau_slot: dict = {}

        def versand_fn(mitglieder):
            # Auftrag HV-20261021-ABSCHLUSS: die erwarteten Empfänger
            # (Hauptdebitor + die zu DIESEM Zeitpunkt bestätigten
            # Mitmieter) werden JETZT, VOR jedem Providerkontakt UND
            # kanalunabhängig, eingefroren - dasselbe Prinzip wie der
            # Kosten-/Inhaltssnapshot, gilt jetzt für BEIDE Kanäle (vorher
            # nur für EMAIL verdrahtet).
            bestaetigte_mitmieter, empfaenger_zugang_je_mitmieter = self._friere_erwartete_empfaenger_ein(mahnlauf)

            if mahnlauf.kanal == "BRIEF":
                return self._sende_brief(
                    mahnlauf=mahnlauf, mitglieder=mitglieder, heute=heute, vorschau_slot=vorschau_slot,
                    bestaetigte_mitmieter=bestaetigte_mitmieter,
                    empfaenger_zugang_je_mitmieter=empfaenger_zugang_je_mitmieter,
                )

            if self.client is None:
                raise ValueError("Privater Mailweg ist nicht eingerichtet.")

            # `versende_mahnlauf` berechnet (und reserviert/kürzt, siehe
            # Rückprüfung Codex 14.09.2026) die Vorschau bereits VOR dem
            # Aufruf dieser Funktion und befüllt `vorschau_slot` damit -
            # GENAU DIESES Objekt wird hier verwendet, NIE eine eigene
            # Neuberechnung (die die zwischenzeitliche Kürzung um bereits
            # anderweitig reservierte Gebührensegmente ignorieren würde).
            if "vorschau" in vorschau_slot:
                vorschau = vorschau_slot["vorschau"]
            elif self.mahnkosten_service is not None:
                # Rückfall für einen hypothetischen Aufrufer ohne
                # vorbefüllten Slot - an die eingefrorene Gruppe gebunden
                # (siehe `kosten_service.py::vorschau`-Docstring).
                vorschau = self.mahnkosten_service.vorschau(
                    vertrag_id=mahnlauf.vertrag_id, stufe=mahnlauf.stufe, heute=heute,
                    nur_op_position_ids=frozenset(m.forderung_op_position_id for m in mitglieder),
                    kanal=mahnlauf.kanal,
                )
                vorschau_slot["vorschau"] = vorschau
            else:
                vorschau = None

            subject, text, empfaenger_name, empfaenger_email, policy_version = self._baue_email_mahntext(
                mahnlauf=mahnlauf, mitglieder=mitglieder, heute=heute, vorschau=vorschau,
            )
            op_ids = sorted(m.forderung_op_position_id for m in mitglieder)

            haupt_beleg = self.client.senden(MailOpsAuftrag(
                "mahnungslauf:" + mahnlauf.outbox_key, "MAHNUNG",
                empfaenger_email, empfaenger_name, subject, text,
                f"MAHNPOLICY:{policy_version}:VERTRAG:{mahnlauf.vertrag_id}:STUFE:{mahnlauf.stufe}:"
                f"FORDERUNGEN:{','.join(str(i) for i in op_ids)}",
            ))

            # JEDER aktive, BESTÄTIGTE Mitmieter mit hinterlegter E-Mail-
            # Adresse bekommt eine EIGENE Kopie DESSELBEN, bereits oben
            # komponierten Mahntexts - derselbe `self.client`/dieselbe
            # `MailOpsAuftrag`-Infrastruktur wie der Hauptversand, primär
            # vorhandene Services/Empfänger-Snapshots genutzt, KEINE zweite
            # Finanzstrecke. Jede Person bekommt einen EIGENEN,
            # deterministisch von `outbox_key` abgeleiteten
            # Idempotenzschlüssel (ein Retry derselben Gruppe sendet
            # dadurch niemals ein zweites Mal an dieselbe Person - dieselbe
            # Idempotenzgarantie wie beim bestehenden Hauptversand); der
            # tatsächliche Erfolg wird UNMITTELBAR NACH DIESEM EINEN
            # Versuch durabel in `MahnEmpfaengerZugangTable` nachgetragen -
            # NICHT erst nachträglich nach Bestätigung des Hauptversands,
            # und NIE in einer nur lokal gehaltenen, bei einem Absturz
            # verlorenen Zwischenvariable.
            #
            # BEWUSST in einem eigenen try/except JE PERSON: ein
            # fehlgeschlagener/unsicherer Mitmieter-Kopieversand darf NIE
            # den bereits oben erhaltenen `haupt_beleg` verwerfen oder
            # einen weiteren Mitmieter überspringen - jede Person wird
            # GENAU EINMAL versucht, unabhängig vom Ergebnis der anderen.
            # Der `MahnLauf`-/Mahnfall-Status (GESENDET/UNSICHER/BLOCKIERT)
            # richtet sich WEITERHIN ausschließlich nach `haupt_beleg`.
            if self.empfaenger_zugang_repo is not None:
                for mitmieter in bestaetigte_mitmieter:
                    if not mitmieter.email:
                        continue  # keine angenommene/erfundene Adresse - kein Versand ohne echte E-Mail
                    eintrag = empfaenger_zugang_je_mitmieter.get(mitmieter.id)
                    if eintrag is None:
                        continue  # konnte nicht eingefroren werden - kein Versand ohne zugehörigen Nachweisplatz
                    if eintrag.versand_belegt_am is not None:
                        continue  # bereits belegt (Teilfehleridempotenz) - nie ein zweites Mal versuchen
                    mitmieter_text = text.replace(f"Guten Tag {empfaenger_name},", f"Guten Tag {mitmieter.name},", 1)
                    mitmieter_schluessel = f"mahnungslauf:{mahnlauf.outbox_key}:mitmieter:{mitmieter.id}"
                    # Breit gefangen (nicht nur der Sendeaufruf selbst):
                    # WEDER ein Providerfehler NOCH ein nachgelagerter
                    # Bookkeeping-Fehler (z. B. DB-Störung bei
                    # `markiere_versand`) darf diese EINE Person für immer
                    # unbearbeitet aus der Schleife werfen oder gar den
                    # bereits erhaltenen `haupt_beleg`/die Verarbeitung der
                    # ÜBRIGEN Mitmieter gefährden - "Teilfehler dürfen das
                    # Ganze nicht als vollständig qualifiziert gemahnt
                    # ausgeben" gilt in BEIDE Richtungen: ein Fehlversuch
                    # bleibt sichtbar unbelegt, verhindert aber nie die
                    # übrigen, tatsächlich erfolgreichen Zustellungen.
                    try:
                        kopie_beleg = self.client.senden(MailOpsAuftrag(
                            mitmieter_schluessel, "MAHNUNG", mitmieter.email, mitmieter.name, subject, mitmieter_text,
                            f"MAHNPOLICY:{policy_version}:VERTRAG:{mahnlauf.vertrag_id}:STUFE:{mahnlauf.stufe}:"
                            f"MITMIETER:{mitmieter.id}",
                        ))
                        proof = nachweis_daten(kopie_beleg)
                        if proof is None:
                            continue  # kein tatsächlicher Versandnachweis - kein erfundener Beleg
                        # Eingefrorener EIGENER Nachweis dieser Person: der
                        # TATSÄCHLICHE, vom Provider bestätigte Versand-
                        # zeitpunkt (nicht die lokale Serverzeit "jetzt")
                        # plus dessen externe Referenz - dasselbe
                        # Belegprinzip wie beim Hauptversand
                        # (`nachweis_daten`/`versand_belegen`).
                        self.empfaenger_zugang_repo.markiere_versand(
                            eintrag.id, versand_belegt_am=datetime.fromisoformat(proof["versendet_am"]),
                            versand_referenz=proof["externe_referenz"],
                        )
                    except Exception:
                        continue  # bleibt unbelegt - sichtbarer, nicht erfundener Fehlversuch

            return haupt_beleg

        return self.mahn_service.versende_mahnlauf(
            ctx=ctx, mahnlauf_id=mahnlauf.id, heute=heute, bank_bestaetigt_bis=confirmed,
            ungeklaerte_eingaenge_vorhanden=unclear, send_enabled=send_enabled,
            versand_fn=versand_fn, mahnkosten_vorschau_slot=vorschau_slot,
            zahlungsziel_geklaert=zahlungsziel_geklaert_wert,
        )

    def _baue_brief_kontext(self, *, mahnlauf, mitglieder, heute, vorschau) -> _BriefKontext:
        """Komponiert den empfängerUNABHÄNGIGEN Brief-Inhalt - EINE
        gemeinsame Quelle für den Hauptbrief, JEDEN Mitmieter-Brief UND
        eine spätere sichere Einzel-Nachsendung (`nachsenden_
        einzelempfaenger`, Auftrag HV-20261021-ABSCHLUSS), damit keine
        zweite, abweichende Kostenberechnung entsteht. `vorschau` kommt
        IMMER bereits vom Aufrufer aufgelöst (frisch berechnet beim
        Erstversand, aus `MahnLaufTable.mahnkosten_snapshot_json`
        rekonstruiert bei einer Nachsendung - "keine doppelte
        Gebührenbuchung"). Prüft dieselbe `debt.rest_cent != mitglied.
        snapshot["betrag_cent"]`-Konsistenz wie der E-Mail-Kanal."""

        st = self.bundle.stammdaten_repository
        contract = st.get_vertrag(mahnlauf.vertrag_id)
        obj = st.objekt_fuer_vertrag(mahnlauf.vertrag_id)
        unit = st.get_einheit(contract.einheit_id)
        account = st.get_konto_by_vertrag(mahnlauf.vertrag_id)
        offene = {f.op_position_id: f for f in self.op_service.offene_forderungen(account.id, heute=heute)}
        gesamt_cent = 0
        for mitglied in mitglieder:
            debt = offene.get(mitglied.forderung_op_position_id)
            if debt is None or debt.rest_cent != mitglied.snapshot["betrag_cent"] or debt.faelligkeit is None:
                raise ValueError("Offene Forderung hat sich verändert.")
            gesamt_cent += debt.rest_cent

        offene_ids = frozenset(vorschau.forderung_op_position_ids) if vorschau is not None else frozenset(
            m.forderung_op_position_id for m in mitglieder
        )
        forderungszeilen = [
            BriefForderungszeile(
                bezeichnung=f.leistungsperiode or f.art,
                faelligkeit=f.faelligkeit if f.faelligkeit_bekannt else None,
                betrag_cent=f.rest_cent,
                urspruenglicher_betrag_cent=f.betrag_cent,
            )
            for f in offene.values() if f.op_position_id in offene_ids
        ]
        zins_segmente = [
            BriefZinssegment(von=s.von, bis=s.bis, satz_prozent=s.satz_prozent, zinsen_cent=s.zinsen_cent)
            for s in (vorschau.zins_segmente if vorschau is not None else ())
        ]
        gesamtbetrag_cent = gesamt_cent + (
            (vorschau.bereits_offene_mahnkosten_cent + vorschau.zusaetzlicher_betrag_cent) if vorschau is not None else 0
        )
        zinssatz_einheitlich_text = (
            f"{vorschau.zinssatz_prozent} % p.a." if vorschau is not None and vorschau.zinssatz_prozent is not None else None
        )
        deadline = heute + timedelta(days=contract.zahlungsfrist_tage)
        zahlungsziel = zahlungsziel_fuer_mahnung(
            self.bank_repo, st, gesellschaft_id=contract.gesellschaft_id, vertrag_id=mahnlauf.vertrag_id, stufe=mahnlauf.stufe,
            zuordnung_repo=self.zahlungsziel_bindung_repo,
        )
        absender = BriefAbsender(
            name=self.settings.brief_absender_name, adresse=self.settings.brief_absender_adresse,
            fn=self.settings.brief_absender_fn, uid=self.settings.brief_absender_uid,
            telefon=self.settings.brief_absender_telefon, website=self.settings.brief_absender_website,
            email=self.settings.brief_absender_email, farbe_anthrazit=self.settings.brief_farbe_anthrazit,
            farbe_gold=self.settings.brief_farbe_gold, logo_pfad=self.settings.brief_logo_pfad,
            font_regular_pfad=self.settings.brief_font_regular_pfad, font_bold_pfad=self.settings.brief_font_bold_pfad,
            font_headline_pfad=self.settings.brief_font_headline_pfad,
            fenster_links_mm=self.settings.brief_fenster_links_mm, fenster_oben_mm=self.settings.brief_fenster_oben_mm,
            fenster_breite_mm=self.settings.brief_fenster_breite_mm, fenster_hoehe_mm=self.settings.brief_fenster_hoehe_mm,
        )
        return _BriefKontext(
            contract=contract, obj=obj, unit=unit, absender=absender, forderungszeilen=forderungszeilen,
            zins_segmente=zins_segmente, gesamtbetrag_cent=gesamtbetrag_cent,
            zinssatz_einheitlich_text=zinssatz_einheitlich_text, deadline=deadline, zahlungsziel=zahlungsziel,
        )

    def _rendere_brief_pdf(self, *, kontext: _BriefKontext, mahnlauf, heute, vorschau, empfaenger_name: str, empfaenger_adresse: str) -> bytes:
        """Rendert EIN Mahnbrief-PDF für EINEN konkreten Empfänger aus dem
        gemeinsamen, empfängerunabhängigen `_BriefKontext` - verwendet von
        Hauptbrief, Mitmieter-Briefen UND einer Einzel-Nachsendung, damit
        der finanzielle Inhalt nie auseinanderlaufen kann."""

        return erzeuge_mahnbrief_pdf(
            absender=kontext.absender, empfaenger_name=empfaenger_name, empfaenger_adresse=empfaenger_adresse,
            objekt_bezeichnung=kontext.obj.bezeichnung, einheit_bezeichnung=kontext.unit.bezeichnung,
            stufe=mahnlauf.stufe, rechtsordnung=kontext.contract.rechtsordnung,
            heute=heute, zahlungsfrist_bis=kontext.deadline, forderungszeilen=kontext.forderungszeilen,
            bereits_offene_mahnkosten_cent=(vorschau.bereits_offene_mahnkosten_cent if vorschau is not None else 0),
            neue_zinsen_delta_cent=(vorschau.neue_zinsen_delta_cent if vorschau is not None else 0),
            zins_segmente=kontext.zins_segmente, zinssatz_einheitlich_text=kontext.zinssatz_einheitlich_text,
            neue_gebuehr_cent=((vorschau.gebuehr_cent or 0) if vorschau is not None else 0),
            gebuehr_rechtsgrundlage=(vorschau.gebuehr_rechtsgrundlage if vorschau is not None else None),
            gesamtbetrag_cent=kontext.gesamtbetrag_cent, zahlungsziel=kontext.zahlungsziel,
        )

    def _sende_brief(self, *, mahnlauf, mitglieder, heute, vorschau_slot, bestaetigte_mitmieter, empfaenger_zugang_je_mitmieter) -> None:
        """Kanal-BRIEF-Gegenstück zu `_dispatch_mahnlauf`s E-Mail-
        `versand_fn` (Auftrag HV-20261019-VERSAND-KOSTEN-ABSCHLUSS, Teil 3)
        - erzeugt GENAU dasselbe Mahnbrief-PDF wie die bestehende
        On-Demand-Vorschau (`backoffice/routes/mahnwesen.py::
        mahnbrief_pdf`, dieselbe `vorschau()`/Forderungszeilen-Komposition),
        archiviert es dauerhaft/konfliktgeprüft (`mahnbrief_archiv_repo.
        archivieren`, reserviert damit den EINEN permanenten Sende-
        Schlüssel für die SFTP-Dateinamensbildung) und überträgt es erst
        DANACH über `brief_sftp_adapter`.

        **Zentral (Codex-Abnahme 19.10.2026):** gibt IMMER `None` zurück,
        NIE das `BriefSftpErgebnis` des erfolgreichen Uploads selbst - ein
        erfolgreicher SFTP-Upload ist AUSSCHLIESSLICH ein ÜBERMITTELT-
        Nachweis gegenüber dem Server (`aktualisiere_status`), NIEMALS ein
        abgeschlossener Versand im Sinne von `MahnwesenService.
        versende_mahnlauf`s `nachweis_daten`/`versand_belegen` - dessen
        Mechanismus würde aus einem direkt durchgereichten `status==
        "GESENDET"` sofort Kostenbuchung (Gebühren/Zinsen) UND den
        GESENDET-Übergang ableiten, lange bevor ein echter
        Postaufgabebeleg vorliegt. `None` lässt `versende_mahnlauf`
        stattdessen den etablierten "noch kein tatsächlicher
        Versandnachweis"-Pfad nehmen: der Mahnlauf bleibt UNSICHER
        (Reservierungen bleiben erhalten, keine Buchung, kein
        automatischer Neuversand) - exakt das gewünschte Verhalten, bis
        `HVMailversandService.vervollstaendige_mahnlauf_aus_anbieterbeleg`
        mit einem authentischen Anbieterbeleg vervollständigt.

        Fehlerbehandlung NACH einem möglichen Netzkontakt ist bewusst
        NIE `ValueError`/BLOCKIERT (das würde Mitglieder für eine neue
        Planung freigeben und einen zweiten, unabhängigen Versandversuch
        ermöglichen, obwohl bereits Bytes unterwegs gewesen sein könnten):
        - `brief_sftp.PostSftpDateiKonfliktError` (die reservierte Datei
          existiert am Server bereits - könnte ein früherer, unbestätigter
          Versuch gewesen sein) -> bleibt ungewiss, `None`.
        - Jeder Fehler beim nachgelagerten `aktualisiere_status` (DB-
          Fehler, Konflikt) NACH erfolgreich gemeldetem Upload -> wird
          verschluckt, bleibt ungewiss, `None` (die Archivzeile bleibt
          dabei ggf. bei `VORBEREITET` stehen und ist über den
          permanenten `sende_schluessel` jederzeit manuell nachvollziehbar).
        Nur Fehler VOR jedem Netzkontakt (fehlende Postadresse, fehlende
        Konfiguration, Archivkonflikt, ungültige Sendevoraussetzungen)
        bleiben `ValueError`/BLOCKIERT - dort ist sicher, dass nichts
        gesendet wurde."""

        if self.brief_sftp_adapter is None:
            raise ValueError("Brief-SFTP-Transport ist nicht eingerichtet.")

        st = self.bundle.stammdaten_repository
        account = st.get_konto_by_vertrag(mahnlauf.vertrag_id)
        debitor = st.get_debitor(account.debitor_id)
        if debitor is None or not debitor.postadresse_geprueft or not (debitor.adresse or "").strip():
            raise ValueError("Keine geprüfte Postadresse für den Briefkanal hinterlegt.")

        if "vorschau" in vorschau_slot:
            vorschau = vorschau_slot["vorschau"]
        elif self.mahnkosten_service is not None:
            vorschau = self.mahnkosten_service.vorschau(
                vertrag_id=mahnlauf.vertrag_id, stufe=mahnlauf.stufe, heute=heute,
                nur_op_position_ids=frozenset(m.forderung_op_position_id for m in mitglieder),
                kanal=mahnlauf.kanal,
            )
            vorschau_slot["vorschau"] = vorschau
        else:
            vorschau = None

        kontext = self._baue_brief_kontext(mahnlauf=mahnlauf, mitglieder=mitglieder, heute=heute, vorschau=vorschau)
        anker = mitglieder[0]
        empfaenger_name = anker.snapshot["empfaenger_name"]
        pdf_bytes = self._rendere_brief_pdf(
            kontext=kontext, mahnlauf=mahnlauf, heute=heute, vorschau=vorschau,
            empfaenger_name=empfaenger_name, empfaenger_adresse=(debitor.adresse or "").replace(", ", "\n"),
        )
        # Auftrag HV-20261022-EIN-KONTOBLATT, Nachbesserung Markus
        # 22.10.2026: der EINE kanonische Kontoauszug wird den Seiten DES
        # TATSÄCHLICHEN Mahnbriefs angehängt, BEVOR er archiviert/
        # übertragen wird - kein bloßer separater Download neben dem
        # echten Schreiben, sondern GENAU DASSELBE physische Dokument,
        # das danach unveränderlich archiviert wird (siehe
        # `kontoauszug_anhang.py`-Moduldoc). Dieselbe GENERISCHE
        # `lade_kontoblatt`-Quelle wie Kontoansicht/Mieterakte - KEINE
        # zweite, abweichende Berechnung.
        kontoauszug_pdf_bytes = erzeuge_kontoauszug_pdf(
            absender=kontext.absender, objekt_bezeichnung=kontext.obj.bezeichnung,
            einheit_bezeichnung=kontext.unit.bezeichnung, debitor_name=debitor.name,
            vertrag_id=mahnlauf.vertrag_id, konto_id=account.id, heute=heute,
            kontoblatt=lade_kontoblatt(
                konto_id=account.id, vertrag_id=mahnlauf.vertrag_id, op_service=self.op_service,
                mahnkosten_repo=self.mahnkosten_repo, ruecklastschriftkosten_repo=self.ruecklastschriftkosten_repo,
                mahnlauf_repo=self.mahnlauf_repo,
            ),
        )
        pdf_bytes = haenge_kontoauszug_pdf_an(pdf_bytes, kontoauszug_pdf_bytes)

        try:
            archiv_zeile = self.mahnbrief_archiv_repo.archivieren(
                vertrag_id=mahnlauf.vertrag_id, stufe=mahnlauf.stufe, mahnlauf_schluessel=mahnlauf.outbox_key,
                pdf_bytes=pdf_bytes, versandnachweis_referenz="mahnungslauf:" + mahnlauf.outbox_key,
                empfaenger_name=empfaenger_name, empfaenger_adresse=debitor.adresse, erstellt_von="hv-mailversand",
            )
        except (VorgangIdKonfliktError, BindungInkonsistentError) as exc:
            raise ValueError(f"Mahnbrief-Archivierung inkonsistent: {exc}") from exc

        dateiname = brief_sftp.baue_dateiname(sende_schluessel=archiv_zeile.sende_schluessel)
        try:
            ergebnis = self.brief_sftp_adapter.sende(pdf_bytes=archiv_zeile.pdf_bytes, dateiname=dateiname)
        except brief_sftp.PostSftpDateiKonfliktError:
            # Siehe Docstring: ein Dateikonflikt unter dem permanent
            # reservierten Schlüssel ist NICHT "sauber nichts gesendet" -
            # bleibt ungewiss statt BLOCKIERT/freigegeben für Neuversand.
            # Die Mitmieter-Briefe unten werden TROTZDEM versucht - ihr
            # je EIGENER, permanenter `brief_sende_schluessel` ist völlig
            # unabhängig vom Hauptbrief-Schlüssel, ein Konflikt bei EINEM
            # Schreiben darf die anderen nie blockieren.
            ergebnis = None

        if ergebnis is not None:
            # Ab hier gilt der Upload als erfolgreich gemeldet (Bytes
            # wurden übertragen) - JEDE weitere Störung bleibt ungewiss,
            # NIE BLOCKIERT.
            try:
                self.mahnbrief_archiv_repo.aktualisiere_status(
                    archiv_id=archiv_zeile.id, neuer_status="UEBERMITTELT",
                    beleg_referenz=ergebnis.externe_referenz, erfasst_von="hv-mailversand",
                )
            except Exception:
                pass  # Archivstatus bleibt ggf. bei VORBEREITET stehen, über sende_schluessel manuell nachvollziehbar

        # Auftrag HV-20261021-ABSCHLUSS ("Briefsendung samt eigenem PDF,
        # postalischer Anschrift, permanentem Sendeschlüssel, Anbieter-/
        # Zugangsnachweis für ALLE bestätigten echten Vertragsmieter"):
        # JEDER aktive, BESTÄTIGTE Mitmieter mit einer EIGENEN, GEPRÜFTEN
        # Postadresse bekommt eine EIGENE Kopie DESSELBEN, bereits oben
        # komponierten Forderungs-/Kosteninhalts (dieselbe `vorschau`/
        # `forderungszeilen`/`zahlungsziel` - keine zweite Finanzstrecke,
        # keine zweite Kostenberechnung) - ein EIGENES PDF, ein EIGENER,
        # permanenter `brief_sende_schluessel` (SFTP-Dateiname), ein
        # EIGENER Anbieter-/Zugangsnachweis. Niemals ein Vertreter, niemals
        # eine noch nicht bestätigte Angabe (`bestaetigte_mitmieter` kommt
        # bereits strikt gefiltert aus `_friere_erwartete_empfaenger_ein`).
        #
        # BEWUSST in einem eigenen try/except JE PERSON, genau wie beim
        # EMAIL-Kanal: ein Fehler bei EINEM Mitmieter-Brief darf weder den
        # Hauptbrief noch einen ANDEREN Mitmieter-Brief verhindern - jede
        # Person wird GENAU EINMAL versucht (Teilfehleridempotenz über den
        # `eintrag.brief_status`-Vorab-Check unten).
        for mitmieter in bestaetigte_mitmieter:
            if not mitmieter.postadresse_geprueft or not (mitmieter.adresse or "").strip():
                continue  # keine angenommene/erfundene Adresse - kein Versand ohne geprüfte Postadresse
            eintrag = empfaenger_zugang_je_mitmieter.get(mitmieter.id)
            if eintrag is None or eintrag.brief_status == "UEBERMITTELT":
                continue  # kein Nachweisplatz ODER bereits übermittelt - nie ein zweiter Versuch

            try:
                mitmieter_pdf_bytes = self._rendere_brief_pdf(
                    kontext=kontext, mahnlauf=mahnlauf, heute=heute, vorschau=vorschau,
                    empfaenger_name=mitmieter.name, empfaenger_adresse=(mitmieter.adresse or "").replace(", ", "\n"),
                )
                # Dieselbe kanonische Kontoauszug-Kopie wie der Hauptbrief
                # (siehe oben) - EIN Konto hat EINEN Kontoauszug,
                # unabhängig davon, an wie viele bestätigte Mitmieter eine
                # eigene Briefkopie geht.
                mitmieter_pdf_bytes = haenge_kontoauszug_pdf_an(mitmieter_pdf_bytes, kontoauszug_pdf_bytes)
            except AdressfehlerError:
                continue  # ungültige/zu lange Adresse - kein Versand, kein Absturz der Gruppe
            except Exception:
                continue  # unerwarteter PDF-Fehler - bleibt unbelegt, kein Absturz der Gruppe

            mitmieter_referenz = f"mahnungslauf:{mahnlauf.outbox_key}:mitmieter:{mitmieter.id}"
            try:
                mitmieter_archiv = self.empfaenger_zugang_repo.archiviere_brief(
                    eintrag.id, pdf_bytes=mitmieter_pdf_bytes, versandnachweis_referenz=mitmieter_referenz,
                    empfaenger_name=mitmieter.name, empfaenger_adresse=mitmieter.adresse, erstellt_von="hv-mailversand",
                )
            except VorgangIdKonfliktError:
                continue  # inkonsistent - bleibt unversandt, über die Zugangsübersicht manuell nachvollziehbar
            except Exception:
                continue  # unerwarteter Archivierungsfehler - bleibt unversandt, kein Absturz der Gruppe

            mitmieter_dateiname = brief_sftp.baue_dateiname(sende_schluessel=mitmieter_archiv.brief_sende_schluessel)
            try:
                mitmieter_ergebnis = self.brief_sftp_adapter.sende(
                    pdf_bytes=mitmieter_archiv.brief_pdf_bytes, dateiname=mitmieter_dateiname,
                )
            except brief_sftp.PostSftpDateiKonfliktError:
                continue  # ungewiss (siehe Docstring oben) - nie ein zweiter Versuch unter demselben Schlüssel
            except Exception:
                # Jeder andere, unerwartete Transportfehler bleibt
                # ebenso ungewiss statt die Verarbeitung der ÜBRIGEN
                # Mitmieter/des bereits archivierten Hauptbriefs zu
                # gefährden - die Archivzeile bleibt bei VORBEREITET,
                # über `brief_sende_schluessel` jederzeit nachvollziehbar.
                continue

            try:
                self.empfaenger_zugang_repo.aktualisiere_brief_status(
                    eintrag.id, neuer_status="UEBERMITTELT", beleg_referenz=mitmieter_ergebnis.externe_referenz,
                    erfasst_von="hv-mailversand",
                )
            except Exception:
                pass  # Status bleibt ggf. bei VORBEREITET, über brief_sende_schluessel manuell nachvollziehbar

        return None

    def vervollstaendige_mahnlauf_aus_anbieterbeleg(
        self, *, ctx, archiv_id: int, anbieterbeleg_pdf_bytes: bytes, anbieterbeleg_referenz: str,
        postaufgabe_datum: date, importiert_von: str, heute: date,
    ) -> None:
        """Backoffice-Einstiegspunkt (Auftrag HV-20261019-VERSAND-KOSTEN-
        ABSCHLUSS, Teil 3, Codex-Abnahme 19.10.2026, zweite Korrektur):
        prüft ERST Berechtigung (Gesellschaft+Schreibrecht+107-Ausschluss)
        UND Plausibilität, BEVOR überhaupt irgendein Belegdatum
        geschrieben wird - die vorherige Fassung importierte den Beleg
        bereits VOR jeder Berechtigungsprüfung. Lehnt einen Abschluss für
        einen noch nie übertragenen/neu geplanten Lauf ab (nur `UNSICHER`
        - tatsächlich ein Übermittlungsversuch war - oder `GESENDET` -
        bereits bestätigt, idempotenter Retry - sind zulässig) sowie ein
        Postaufgabedatum in der Zukunft oder vor der eigenen Archivierung.
        Importiert ERST DANACH idempotent/konfliktgeprüft
        (`mahnbrief_archiv_repo.importiere_anbieterbeleg` - ein bloßer
        Referenztext allein wird dort bereits abgelehnt) und
        vervollständigt den eingefrorenen Mahnlauf samt Kostensnapshot
        anhand des TATSÄCHLICHEN Postaufgabedatums, niemals erneut
        sendend (`MahnwesenService.vervollstaendige_mahnlauf_aus_
        echtem_beleg`)."""

        archiv = self.mahnbrief_archiv_repo.get(archiv_id)
        if archiv is None:
            raise ValueError(f"Unbekannte Mahnbrief-Archivzeile {archiv_id}.")
        vertrag = self.bundle.stammdaten_repository.get_vertrag(archiv.vertrag_id)
        if vertrag is None:
            raise ValueError(f"Unbekannter Vertrag {archiv.vertrag_id} zu Archivzeile {archiv_id}.")
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)
        self.bundle.stammdaten_repository.pruefe_vertrag_nicht_ausgeschlossen(vertrag.id)

        with self.sf() as session:
            mahnlauf = session.execute(select(MahnLaufTable).where(
                MahnLaufTable.vertrag_id == archiv.vertrag_id, MahnLaufTable.stufe == archiv.stufe,
                MahnLaufTable.outbox_key == archiv.mahnlauf_schluessel,
            )).scalar_one_or_none()
        if mahnlauf is None:
            raise ValueError(f"Kein Mahnlauf zu Archivzeile {archiv_id} gefunden.")
        if mahnlauf.status not in ("UNSICHER", "GESENDET"):
            raise ValueError(
                f"Mahnlauf {mahnlauf.id} hat Status {mahnlauf.status!r} - eine Vervollständigung aus einem "
                "Anbieterbeleg ist nur für einen tatsächlich übermittelten (UNSICHER) oder bereits "
                "bestätigten (GESENDET, idempotenter Retry) Versandversuch vorgesehen, nie für einen noch "
                "nie übertragenen/neu geplanten Lauf."
            )
        if postaufgabe_datum > heute:
            raise ValueError(f"Postaufgabedatum {postaufgabe_datum} liegt in der Zukunft.")
        archiviert_am_datum = archiv.archiviert_am.date() if archiv.archiviert_am is not None else None
        if archiviert_am_datum is not None and postaufgabe_datum < archiviert_am_datum:
            raise ValueError(
                f"Postaufgabedatum {postaufgabe_datum} liegt vor der Archivierung/Übermittlung "
                f"({archiviert_am_datum}) - ein Postaufgabebeleg kann nicht vor dem eigenen Versandversuch liegen."
            )

        archiv = self.mahnbrief_archiv_repo.importiere_anbieterbeleg(
            archiv_id=archiv_id, anbieterbeleg_pdf_bytes=anbieterbeleg_pdf_bytes,
            anbieterbeleg_referenz=anbieterbeleg_referenz, postaufgabe_datum=postaufgabe_datum,
            importiert_von=importiert_von,
        )

        # `versendet_am` trägt NUR das TATSÄCHLICHE Postaufgabedatum -
        # Mitternacht UTC ist dabei ein reiner Speicher-Platzhalter
        # (DB-Spalte verlangt einen Zeitstempel), KEINE behauptete
        # tatsächliche Uhrzeit (Codex-Abnahme 19.10.2026: "eine
        # datumsgenaue Angabe nicht als erfundene tatsächliche Uhrzeit
        # ausgeben") - nur das Datum selbst ist authentisch.
        beleg = RekonstruierterBeleg(
            status="GESENDET", versendet_am=datetime.combine(postaufgabe_datum, time.min, tzinfo=timezone.utc),
            externe_referenz=anbieterbeleg_referenz, provider_referenz=f"post-anbieterbeleg:{anbieterbeleg_referenz}",
        )
        self.mahn_service.vervollstaendige_mahnlauf_aus_echtem_beleg(
            ctx=ctx, mahnlauf_id=mahnlauf.id, beleg=beleg, akteur=importiert_von,
        )

    def nachsenden_einzelempfaenger(self, *, ctx, mahnlauf_id: int, mitmieter_id: int, heute: date) -> str:
        """Auftrag HV-20261021-ABSCHLUSS, Task 2 ("sichere Nachsende-
        Aktion ausschließlich für nachweislich fehlgeschlagene einzelne
        Empfänger"): Backoffice-Einstiegspunkt für EINEN einzelnen,
        bereits erwarteten Mitmieter-Empfänger EINES bereits tatsächlich
        versandversuchten Mahnlaufs.

        Verbindliche Garantien:
        - Niemals für eine Gruppe, die noch nie tatsächlich versandt
          wurde (`mahnlauf.status` muss `GESENDET` oder `UNSICHER` sein -
          das normale Planen/Dispatchen bleibt der einzige Weg, eine neue
          Gruppe zu bilden).
        - Ein bereits belegter (`versand_belegt_am`/`brief_status==
          UEBERMITTELT`) Empfänger wird NIE erneut angeschrieben - reiner
          No-Op, Rückgabe `BEREITS_GESENDET`.
        - Beim EMAIL-Kanal wird ein ungewisser Providerstatus ERST über
          `MailOpsClient.status_abfragen` geklärt, BEVOR überhaupt neu
          gesendet wird - nur eine als `NICHT_GEFUNDEN` bestätigte
          Referenz (nachweislich nie angenommen) gilt als sicher
          nachsendbar; jeder andere Status (inkl. eines Transportfehlers
          bei der Abfrage selbst) bleibt `UNKLAR`, NIE automatisch erneut
          gesendet. Beim BRIEF-Kanal übernimmt die exklusive Anlage des
          bereits reservierten `brief_sende_schluessel`-Dateinamens genau
          diese Rolle (ein Konflikt dort bedeutet "war schon da").
        - Reine Wiederverwendung des bereits eingefrorenen
          `MahnLaufTable.mahnkosten_snapshot_json` - NIE eine eigene,
          neue Kostenberechnung/-buchung ("keine doppelte
          Gebührenbuchung"); `_baue_email_mahntext`/`_baue_brief_kontext`
          prüfen weiterhin den aktuellen Forderungsstand gegen den
          eingefrorenen Mitgliedersnapshot und lehnen bei einer
          zwischenzeitlichen Änderung ab, statt eine veraltete Zahl zu
          verschicken.
        - Ändert NIEMALS `MahnLaufTable`/`MahnFallTable`-Status - ein
          Teilfehler bei einem Mitmieter gibt die Gruppe selbst NIE als
          "vollständig qualifiziert gemahnt" aus (das bleibt
          ausschließlich an den Hauptdebitor-Nachweis gebunden).

        Rückgabe (reiner Statuscode, keine Exception für einen
        regulären, erwarteten Ausgang):
        - `"GESENDET"`: jetzt tatsächlich erfolgreich nachgesendet.
        - `"BEREITS_GESENDET"`: war bereits erfolgreich zugestellt (lokal
          oder laut Providerabfrage) - nichts Neues getan.
        - `"UNKLAR"`: Status (noch) nicht eindeutig klärbar ODER der
          erneute Sendeversuch selbst blieb ungewiss - keine weitere
          automatische Aktion, manuell/später erneut prüfen.
        - `"BLOCKIERT"`: ein tatsächlich NEUER Sendeversuch wäre jetzt
          nötig gewesen (Status nachweislich `NICHT_GEFUNDEN`/BRIEF ohne
          vorherigen Upload), aber `SEND_ENABLED`/Allowlist ist aus ODER
          dieselben "frisch versandbereit"-Bedingungen wie beim
          Erstversand (Bank/ungeklärte Eingänge/Zahlungsziel/Policy/
          Empfänger/OP-Stand/Frist/Briefkanal-Transport) sind jetzt
          NICHT erfüllt - KEIN Providerkontakt, reine Statusabklärung
          (die bereits VOR dieser Prüfung lief) bleibt davon unberührt."""

        mahnlauf = self.mahnlauf_repo.get(mahnlauf_id)
        if mahnlauf is None:
            raise ValueError(f"Unbekannter Mahnlauf {mahnlauf_id}")
        vertrag = self.bundle.stammdaten_repository.get_vertrag(mahnlauf.vertrag_id)
        if vertrag is None:
            raise ValueError(f"Unbekannter Vertrag {mahnlauf.vertrag_id}")
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)
        self.bundle.stammdaten_repository.pruefe_vertrag_nicht_ausgeschlossen(vertrag.id)

        if mahnlauf.status not in ("GESENDET", "UNSICHER"):
            raise ValueError(
                f"Mahnlauf {mahnlauf.id} hat Status {mahnlauf.status!r} - eine Einzel-Nachsendung ist nur für "
                "eine Gruppe mit TATSÄCHLICH versuchtem Versand vorgesehen (GESENDET oder UNSICHER), nie für "
                "eine noch nie übertragene/neu geplante Gruppe."
            )

        # Nutzervorgabe 21.10.2026: dasselbe Versand-Gate wie beim
        # Erstversand gilt UNEINGESCHRÄNKT auch für die Nachsendung - eine
        # zwischenzeitlich entstandene/wieder aufgetretene Mehrdeutigkeit
        # (z. B. ein zweites Bankkonto wurde nach dem Erstversand
        # angelegt) darf eine Nachsendung NIE mit einem unbrauchbaren
        # Zahlungsziel ausliefern.
        if not zahlungsziel_geklaert(
            self.bank_repo, self.zahlungsziel_bindung_repo, self.bundle.stammdaten_repository,
            gesellschaft_id=vertrag.gesellschaft_id, vertrag_id=vertrag.id,
        ):
            raise ZahlungszielUngeklaertError(
                f"Zahlungsziel für Gesellschaft {vertrag.gesellschaft_id} ist FEHLEND oder MEHRDEUTIG - "
                "keine Nachsendung mit einem falschen oder unbrauchbaren Zahlungsziel."
            )
        if self.empfaenger_zugang_repo is None:
            raise ValueError("Kein Empfänger-Zugangsnachweis-Repository konfiguriert.")
        eintrag = next(
            (e for e in self.empfaenger_zugang_repo.fuer_mahnlauf(mahnlauf.id) if e.mitmieter_id == mitmieter_id), None,
        )
        if eintrag is None:
            raise ValueError(f"Kein erwarteter Empfänger (Mitmieter {mitmieter_id}) für Mahnlauf {mahnlauf.id}.")

        mitmieter = self.mitmieter_repo.get(mitmieter_id) if self.mitmieter_repo is not None else None
        if (
            mitmieter is None or not mitmieter.aktiv or mitmieter.rolle != "MITMIETER"
            or mitmieter.zustand != "BESTAETIGT"
        ):
            raise ValueError(
                f"Mitmieter {mitmieter_id} ist nicht (mehr) ein aktiver, bestätigter Mahn-Empfänger - "
                "keine Nachsendung an eine zwischenzeitlich geänderte/zurückgezogene Angabe."
            )

        # Korrektur nach unabhängiger Abnahme (Codex, 21.10.2026,
        # reproduzierter Fehler): bis hierher war NUR das Zahlungsziel-
        # Gate geprüft - `send_enabled`/Allowlist, die aktuelle
        # Bankbestätigung UND alle übrigen "frisch versandbereit"-
        # Bedingungen (Policy/Empfänger/OP-Stand/Frist/Briefkanal-
        # Transport) waren für die Nachsendung vollständig ungegatet,
        # obwohl sie der Erstversand unmittelbar vor JEDEM tatsächlichen
        # Providerkontakt prüft. EIN erneuter Sendeversuch (NICHT die
        # reine Statusabfrage, die bleibt immer erlaubt) wird deshalb
        # jetzt GENAU WIE beim Erstversand gegatet - für EMAIL und
        # BRIEF gleich, kein Umgehen über einen Unterpfad.
        send_enabled = bool(self.client and self.settings.send_enabled and self.settings.hv_mail_allowlist_bestaetigt)
        confirmed, unclear = bank_freigabe_ableiten(self.bank_repo, self.bank_service, vertrag.gesellschaft_id, vertrag.id)
        versandbereit, versandbereit_grund = self.mahn_service.pruefe_mahnlauf_frisch_versandbereit(
            mahnlauf_id=mahnlauf.id, heute=heute, bank_bestaetigt_bis=confirmed,
            ungeklaerte_eingaenge_vorhanden=unclear, zahlungsziel_geklaert=True,
        )
        if not send_enabled:
            versandbereit, versandbereit_grund = False, "Versand ist derzeit deaktiviert (SEND_ENABLED/Allowlist)."

        if mahnlauf.kanal == "EMAIL":
            return self._nachsenden_email(
                mahnlauf=mahnlauf, eintrag=eintrag, mitmieter=mitmieter, heute=heute,
                versandbereit=versandbereit, versandbereit_grund=versandbereit_grund,
            )
        if mahnlauf.kanal == "BRIEF":
            return self._nachsenden_brief(
                mahnlauf=mahnlauf, eintrag=eintrag, mitmieter=mitmieter, heute=heute,
                versandbereit=versandbereit, versandbereit_grund=versandbereit_grund,
            )
        raise ValueError(f"Unbekannter Mahnlauf-Kanal {mahnlauf.kanal!r}.")

    def _nachsenden_email(self, *, mahnlauf, eintrag, mitmieter, heute, versandbereit=True, versandbereit_grund="") -> str:
        if self.client is None:
            raise ValueError("Privater Mailweg ist nicht eingerichtet.")
        if eintrag.versand_belegt_am is not None:
            return "BEREITS_GESENDET"
        if not mitmieter.email:
            raise ValueError(f"Mitmieter {mitmieter.id} hat keine hinterlegte E-Mail-Adresse.")

        referenz = f"mahnungslauf:{mahnlauf.outbox_key}:mitmieter:{mitmieter.id}"
        try:
            status = self.client.status_abfragen(referenz)
        except TransportFehlerUngewissError:
            return "UNKLAR"

        if status.status == "GESENDET":
            # Tatsächlich bereits angenommen - lokal nur nicht belegt
            # (z. B. ein Absturz zwischen Versand und `markiere_versand`
            # beim Erstversand). Jetzt NACHTRÄGLICH belegen, NIE erneut
            # senden.
            proof = nachweis_daten(status)
            if proof is not None:
                self.empfaenger_zugang_repo.markiere_versand(
                    eintrag.id, versand_belegt_am=datetime.fromisoformat(proof["versendet_am"]),
                    versand_referenz=proof["externe_referenz"],
                )
            return "BEREITS_GESENDET"
        if status.status != "NICHT_GEFUNDEN":
            # ANGENOMMEN/IN_BEARBEITUNG/UNKLAR/FEHLER/DEAKTIVIERT: weder
            # sicher zugestellt NOCH nachweislich nie angenommen - bleibt
            # ungewiss, NIE automatisch erneut gesendet.
            return "UNKLAR"

        # NICHT_GEFUNDEN: nachweislich nie unter dieser Referenz
        # angenommen - grundsätzlich (erstmalig) nachsendbar. Ab HIER
        # würde ein TATSÄCHLICH NEUER Providerkontakt erfolgen - dafür
        # gelten GENAU DIESELBEN aktuellen Versand-/Allowlist-/Bank-/
        # OP-/Sonderfallbedingungen wie beim Erstversand (Korrektur nach
        # unabhängiger Abnahme, Codex 21.10.2026).
        if not versandbereit:
            return "BLOCKIERT"

        mahnfall_ids = MahnLaufRepository.mitglieder_ids(mahnlauf)
        mitglieder = [self.mahn_repo.get(i) for i in mahnfall_ids]
        if not mahnfall_ids or any(m is None for m in mitglieder):
            raise ValueError(f"Mahnlauf {mahnlauf.id}: Gruppe inkonsistent (Mitglied fehlt) - keine Nachsendung möglich.")

        # Reine Rekonstruktion des bereits EINMAL eingefrorenen
        # Kostensnapshots - NIE eine neue Berechnung/Reservierung/Buchung.
        vorschau = snapshot_aus_json(mahnlauf.mahnkosten_snapshot_json)
        subject, text, empfaenger_name, _email, policy_version = self._baue_email_mahntext(
            mahnlauf=mahnlauf, mitglieder=mitglieder, heute=heute, vorschau=vorschau,
        )
        mitmieter_text = text.replace(f"Guten Tag {empfaenger_name},", f"Guten Tag {mitmieter.name},", 1)
        try:
            kopie_beleg = self.client.senden(MailOpsAuftrag(
                referenz, "MAHNUNG", mitmieter.email, mitmieter.name, subject, mitmieter_text,
                f"MAHNPOLICY-NACHSENDUNG:{policy_version}:VERTRAG:{mahnlauf.vertrag_id}:STUFE:{mahnlauf.stufe}:"
                f"MITMIETER:{mitmieter.id}",
            ))
        except Exception:
            return "UNKLAR"
        proof = nachweis_daten(kopie_beleg)
        if proof is None:
            return "UNKLAR"
        self.empfaenger_zugang_repo.markiere_versand(
            eintrag.id, versand_belegt_am=datetime.fromisoformat(proof["versendet_am"]),
            versand_referenz=proof["externe_referenz"],
        )
        return "GESENDET"

    def _nachsenden_brief(self, *, mahnlauf, eintrag, mitmieter, heute, versandbereit=True, versandbereit_grund="") -> str:
        if self.brief_sftp_adapter is None:
            raise ValueError("Brief-SFTP-Transport ist nicht eingerichtet.")
        if eintrag.brief_status == "UEBERMITTELT":
            return "BEREITS_GESENDET"
        if not mitmieter.postadresse_geprueft or not (mitmieter.adresse or "").strip():
            raise ValueError(f"Mitmieter {mitmieter.id} hat keine geprüfte Postadresse hinterlegt.")

        if eintrag.brief_sende_schluessel is None:
            # Noch nie archiviert (z. B. ein Fehlschlag VOR dem ersten
            # Archivierungsversuch beim Erstversand) - PDF jetzt erzeugen
            # UND archivieren, aus demselben eingefrorenen Kostensnapshot
            # wie der Erstversand (NIE eine neue Berechnung/Buchung).
            mahnfall_ids = MahnLaufRepository.mitglieder_ids(mahnlauf)
            mitglieder = [self.mahn_repo.get(i) for i in mahnfall_ids]
            if not mahnfall_ids or any(m is None for m in mitglieder):
                raise ValueError(f"Mahnlauf {mahnlauf.id}: Gruppe inkonsistent (Mitglied fehlt) - keine Nachsendung möglich.")
            vorschau = snapshot_aus_json(mahnlauf.mahnkosten_snapshot_json)
            kontext = self._baue_brief_kontext(mahnlauf=mahnlauf, mitglieder=mitglieder, heute=heute, vorschau=vorschau)
            try:
                pdf_bytes = self._rendere_brief_pdf(
                    kontext=kontext, mahnlauf=mahnlauf, heute=heute, vorschau=vorschau,
                    empfaenger_name=mitmieter.name, empfaenger_adresse=(mitmieter.adresse or "").replace(", ", "\n"),
                )
            except AdressfehlerError as exc:
                raise ValueError(f"Ungültige Postadresse: {exc}") from exc

            mitmieter_referenz = f"mahnungslauf:{mahnlauf.outbox_key}:mitmieter:{mitmieter.id}"
            try:
                eintrag = self.empfaenger_zugang_repo.archiviere_brief(
                    eintrag.id, pdf_bytes=pdf_bytes, versandnachweis_referenz=mitmieter_referenz,
                    empfaenger_name=mitmieter.name, empfaenger_adresse=mitmieter.adresse,
                    erstellt_von="hv-mailversand-nachsendung",
                )
            except VorgangIdKonfliktError as exc:
                raise ValueError(f"Mahnbrief-Archivierung inkonsistent: {exc}") from exc

        # Der eigentliche SFTP-Upload ist beim Briefkanal der ERSTE und
        # EINZIGE tatsächliche Transportkontakt (keine separate Status-
        # Abfrage wie bei EMAIL) - dieselben aktuellen Versand-/Bank-/
        # OP-/Sonderfallbedingungen wie beim Erstversand gelten deshalb
        # UNMITTELBAR hier, vor diesem einen Kontakt (Korrektur nach
        # unabhängiger Abnahme, Codex 21.10.2026).
        if not versandbereit:
            return "BLOCKIERT"

        dateiname = brief_sftp.baue_dateiname(sende_schluessel=eintrag.brief_sende_schluessel)
        try:
            ergebnis = self.brief_sftp_adapter.sende(pdf_bytes=eintrag.brief_pdf_bytes, dateiname=dateiname)
        except brief_sftp.PostSftpDateiKonfliktError:
            # Datei existiert remote bereits unter dem permanent
            # reservierten Schlüssel - war wahrscheinlich bereits
            # erfolgreich übertragen, aber OHNE maschinellen Nachweis -
            # bleibt ungewiss, NIE als "GESENDET" ausgegeben.
            return "UNKLAR"
        except Exception:
            return "UNKLAR"

        try:
            self.empfaenger_zugang_repo.aktualisiere_brief_status(
                eintrag.id, neuer_status="UEBERMITTELT", beleg_referenz=ergebnis.externe_referenz,
                erfasst_von="hv-mailversand-nachsendung",
            )
        except Exception:
            pass  # Status bleibt ggf. bei VORBEREITET, über brief_sende_schluessel manuell nachvollziehbar
        return "GESENDET"

    def mahnung_senden(self, *, ctx, row_id, heute):
        """Sendet GENAU EIN Schreiben je Vertrag+Mahnstufe (Rückprüfung
        14.09.2026), unabhängig davon, für welchen einzelnen `row_id`
        (eine von möglicherweise mehreren GEPLANTEN Forderungen desselben
        Vertrags/derselben Stufe - z. B. HMZ+BK derselben Vorschreibung)
        diese Methode aufgerufen wird: `MahnwesenService.plane_mahnlauf`
        bildet die GENAU EINE, deterministische Gruppe ALLER aktuell
        tatsächlich versandbereiten Mitglieder (KEINE kleinste-Id-Leader-
        Heuristik - Rückprüfung 14.09.2026, Risiko 2) und friert sie in
        einer persistenten `MahnLaufTable`-Zeile ein; `versende_mahnlauf`
        claimt/versendet GENAU DIESE Gruppe atomar (Risiko 1).

        Zwei GLEICHZEITIGE Aufrufe für zwei VERSCHIEDENE Mitglieder
        derselben Gruppe lösen NIE zwei E-Mails aus: beide bilden
        dieselbe Gruppe und konkurrieren um DEREN atomaren
        `claim_fuer_versand`-Compare-and-Swap - nur einer gewinnt und
        ruft den externen Versand auf, der andere sieht bereits
        `mahnlauf.status != GEPLANT` (`BEREITS_VERARBEITET`).

        Der Mahnkosten-/Zinsnachweis im Brieftext stammt aus GENAU EINER
        `MahnkostenService.vorschau()`-Berechnung (unmittelbar vor dem
        tatsächlichen Versand, berücksichtigt also auch kurz zuvor
        eingegangene Zahlungen, UND an exakt die Gruppenmitglieder
        gebunden) und wird über `mahnkosten_vorschau_slot` 1:1 an die
        anschließende Buchung weitergereicht - Text und Buchung können
        dadurch nie auseinanderlaufen (siehe `kosten_service.py`-
        Moduldoc)."""

        row = self.mahn_repo.get(row_id)
        if row is None:
            raise ValueError("Mahnfall fehlt.")
        require_gesellschaft_access(ctx, row.gesellschaft_id)
        require_schreibrecht(ctx)
        if row.status != "GEPLANT":
            return VersandErgebnis("BEREITS_VERARBEITET", f"Status ist bereits {row.status}; kein Doppelversand.")

        st = self.bundle.stammdaten_repository
        contract = st.get_vertrag(row.vertrag_id)
        account = st.get_konto_by_vertrag(row.vertrag_id)
        if contract is None or account is None:
            raise ValueError("Vertrag/Konto zum Mahnfall fehlt.")
        confirmed, unclear = bank_freigabe_ableiten(
            self.bank_repo, self.bank_service, row.gesellschaft_id, row.vertrag_id)
        zahlungsziel_ok = zahlungsziel_geklaert(
            self.bank_repo, self.zahlungsziel_bindung_repo, st,
            gesellschaft_id=row.gesellschaft_id, vertrag_id=row.vertrag_id,
        )

        mahnlauf = self.mahn_service.plane_mahnlauf(
            ctx=ctx, vertrag=contract, konto=account, stufe=row.stufe, heute=heute,
            bank_bestaetigt_bis=confirmed, ungeklaerte_eingaenge_vorhanden=unclear,
            zahlungsziel_geklaert=zahlungsziel_ok,
        )
        if mahnlauf is not None and row_id in MahnLaufRepository.mitglieder_ids(mahnlauf):
            return self._dispatch_mahnlauf(
                ctx=ctx, mahnlauf=mahnlauf, heute=heute, confirmed=confirmed, unclear=unclear,
                zahlungsziel_geklaert_wert=zahlungsziel_ok,
            )

        # `row_id` selbst ist NICHT (mehr) Teil der aktuell gebildeten
        # Gruppe (z. B. Frist noch nicht abgelaufen, oder bei der
        # Planung final aus der Bündelung ausgeschieden) - ANDERE
        # Mitglieder desselben Vertrags/derselben Stufe können trotzdem
        # bereits versandbereit sein und wurden oben bereits verarbeitet;
        # das Ergebnis für DIESEN Aufrufer richtet sich nach SEINEM
        # eigenen, tatsächlich persistierten Stand.
        if mahnlauf is not None:
            self._dispatch_mahnlauf(
                ctx=ctx, mahnlauf=mahnlauf, heute=heute, confirmed=confirmed, unclear=unclear,
                zahlungsziel_geklaert_wert=zahlungsziel_ok,
            )
        aktuell = self.mahn_repo.get(row_id)
        if aktuell is None:
            raise ValueError("Mahnfall fehlt.")
        if aktuell.status == "GEPLANT":
            return VersandErgebnis("BLOCKIERT", "Mahnfrist ist noch nicht abgelaufen oder derzeit kein Mitglied der Gruppe versandbereit.")
        if aktuell.status in ("BLOCKIERT", "UEBERSPRUNGEN"):
            return VersandErgebnis(aktuell.status, "Bei der Gruppenplanung dauerhaft aus der Bündelung ausgeschieden.")
        return VersandErgebnis("BEREITS_VERARBEITET", f"Status ist bereits {aktuell.status}.")

    def mahnlauf(self, *, ctx, heute):
        if not (self.client and self.settings.send_enabled and self.settings.hv_mail_allowlist_bestaetigt):
            return {"geplant": 0, "gesendet": 0, "blockiert": 0}
        self.mahn_service.markiere_verwaiste_als_unsicher()
        self.mahn_service.markiere_verwaiste_mahnlaeufe_als_unsicher()
        # Recovery-Paket 14.09.2026: ein Absturz zwischen bestätigtem
        # Versand und der eigentlichen Kostenbuchung ODER zwischen dem
        # Gruppen- und dem Mitgliederübergang wird HIER anhand des
        # eingefrorenen Kosten-/Inhaltssnapshots bzw. der persistierten
        # Gruppenquittung nachgeholt, NIE anhand einer neu berechneten,
        # potenziell abweichenden Vorschau oder eines erneuten Versands
        # (siehe `MahnwesenService.vervollstaendige_gesendete_
        # mahnlaeufe`-Docstring).
        self.mahn_service.vervollstaendige_gesendete_mahnlaeufe(ctx=ctx, akteur="hv-mailversand-recovery")
        # Korrektur nach unabhängiger Abnahme (Codex, 20.10.2026): löst
        # Tageslauf-Claims auf, die seit mind. 15 Minuten in
        # IN_BEARBEITUNG feststecken (z. B. Absturz zwischen Claim und
        # Ergebnis) - siehe `MahnTageslaufRepository.freigeben_
        # verwaiste`-Docstring. Dieselbe Mindestwartezeit-Logik wie die
        # bestehende Mahnfall-/Mahnlauf-Recovery oben, kein Sofort-Retry
        # innerhalb eines noch laufenden Prozesses.
        self.tageslauf_repo.freigeben_verwaiste(aelter_als=datetime.now(timezone.utc) - timedelta(minutes=15))
        policy = self.policy_repo.aktuelle_freigegebene()
        counts = {"geplant": 0, "gesendet": 0, "blockiert": 0}
        if policy is None:
            return counts
        # Auftrag HV-20261020-VIER-MAHNSTUFEN, Nutzervorgabe wörtlich:
        # "Zwei deterministische Prüfläufe je Monat" (Standard 10./25.,
        # veränderbar) - im BESTEHENDEN täglichen 08:15-Dienst
        # (`scripts/indexautomatik_taegliche_pflege.py`), KEIN zweiter
        # konkurrierender Scheduler. Gilt NUR für eine Policy mit
        # `vier_stufen_profil_aktiv` - eine alte Zwei-Stufen-Policy läuft
        # UNVERÄNDERT täglich weiter (kein stillschweigender
        # Verhaltenswechsel für bereits laufende Mahnzyklen). Die bereits
        # oben ausgeführten Recovery-Schritte (verwaiste Fälle/Läufe,
        # Kostenabschluss) sind KEINE Eskalation und laufen deshalb
        # bewusst JEDEN Tag unverändert weiter.
        if policy.vier_stufen_profil_aktiv and heute.day not in _VIER_STUFEN_PRUEFTAGE:
            return counts
        st = self.bundle.stammdaten_repository
        for contract in st.list_alle_vertraege():
            if not ctx.has_zugriff(contract.gesellschaft_id):
                continue
            try:
                st.pruefe_vertrag_nicht_ausgeschlossen(contract.id)
                account = st.get_konto_by_vertrag(contract.id)
                if account is None:
                    continue
                confirmed, unclear = bank_freigabe_ableiten(self.bank_repo, self.bank_service, contract.gesellschaft_id, contract.id)
                zahlungsziel_ok = zahlungsziel_geklaert(
                    self.bank_repo, self.zahlungsziel_bindung_repo, st,
                    gesellschaft_id=contract.gesellschaft_id, vertrag_id=contract.id,
                )
                # Planung läuft IMMER (harmlos/idempotent, hält die
                # MahnFall-Zeilen aktuell) - NUR der tatsächliche Versand
                # weiter unten wird pro Prüftag begrenzt.
                planned = self.mahn_service.plane_alle_offenen_forderungen(ctx=ctx, vertrag=contract,
                    konto=account, policy=policy, heute=heute, bank_bestaetigt_bis=confirmed,
                    ungeklaerte_eingaenge_vorhanden=unclear, zahlungsziel_geklaert=zahlungsziel_ok)
                counts["geplant"] += sum(1 for p in planned if p.status == "GEPLANT")
                counts["blockiert"] += sum(1 for p in planned if p.status == "BLOCKIERT")
                # EIN `plane_mahnlauf`/Dispatch je BETROFFENER Stufe - bündelt
                # automatisch ALLE zu diesem Zeitpunkt GEPLANTEN Fälle
                # desselben Vertrags/derselben Stufe zu EINEM Schreiben
                # (siehe `_dispatch_mahnlauf`), kein Aufruf mehr je
                # einzelner Forderung.
                geplante_stufen = sorted({p.stufe for p in planned if p.status == "GEPLANT"})
                # Auftrag HV-20261020-VIER-MAHNSTUFEN, Nutzervorgabe
                # wörtlich: "Maximal EIN gebündeltes Schreiben pro
                # Vertrag/Monatslauftermin trotz neuer OPs/verschiedener
                # alter Stufen, keine neue Gebühr je OP/Mitmieterkopie."
                # Haben mehrere Forderungen DESSELBEN Vertrags in DIESEM
                # Lauf unterschiedliche, jeweils für sich genommen
                # versandreife Stufen erreicht, wird NUR die höchste
                # davon in DIESEM Lauf tatsächlich dispatcht - eine
                # niedrigere Stufe bleibt GEPLANT und wird erst in einem
                # SPÄTEREN Prüflauf berücksichtigt (jede Forderung
                # behält dabei ihre EIGENE, individuell erreichte Stufe -
                # keine Vorverlegung/kein "Hochziehen" einer neuen
                # Forderung auf die Stufe älterer Forderungen desselben
                # Vertrags). Unter dem ALTEN Zwei-Stufen-Profil ändert
                # das nichts (dort können ohnehin nicht gleichzeitig zwei
                # unterschiedliche Stufen für denselben Vertrag
                # versandreif sein, da Stufe 2 zwingend die gesendete
                # Stufe 1 derselben Forderung voraussetzt - diese
                # Einschränkung ist aus Vorsicht/Klarheit aber generell
                # formuliert, nicht nur für das Vier-Stufen-Profil).
                stufen_fuer_diesen_lauf = geplante_stufen[-1:] if geplante_stufen else []
                if not stufen_fuer_diesen_lauf:
                    continue

                # Korrektur nach unabhängiger Abnahme (Codex, 20.10.2026,
                # reproduzierter Fehler): die Auswahl "höchstens eine
                # Stufe je Aufruf" OBEN schützt NUR innerhalb DIESES
                # Funktionsaufrufs - ein zweiter Aufruf für `heute`
                # (Crash-Retry, Parallelität, oder eine zwischenzeitlich
                # neu gebuchte, bereits fällige Forderung) konnte sonst
                # einen ZWEITEN tatsächlichen Versand für denselben
                # Vertrag am selben Prüftag auslösen. Der DB-seitige,
                # über den UNIQUE-Constraint abgesicherte Claim
                # (`MahnTageslaufTable`) ist die EINZIGE Quelle der
                # Exklusivität - siehe dortigen Docstring. Erst HIER
                # geclaimt (nicht schon vor der Planung), damit ein
                # bereits für `heute` geschlossener Slot die harmlose
                # Planung/Buchführung nicht mitblockiert.
                tageslauf = self.tageslauf_repo.claim(vertrag_id=contract.id, pruefdatum=heute)
                if tageslauf is None:
                    continue
                tatsaechlich_versucht = False
                letzter_mahnlauf = None
                try:
                    for stufe in stufen_fuer_diesen_lauf:
                        letzter_mahnlauf = self.mahn_service.plane_mahnlauf(
                            ctx=ctx, vertrag=contract, konto=account, stufe=stufe, heute=heute,
                            bank_bestaetigt_bis=confirmed, ungeklaerte_eingaenge_vorhanden=unclear,
                            zahlungsziel_geklaert=zahlungsziel_ok,
                        )
                        if letzter_mahnlauf is None:
                            continue
                        # Korrektur nach unabhängiger Abnahme (Codex,
                        # 20.10.2026, reproduzierter Fehler): VOR jedem
                        # tatsächlichen Versandversuch verknüpfen, damit
                        # eine spätere Recovery (`freigeben_verwaiste`)
                        # bei einem Absturz NACH bestätigtem
                        # Providerkontakt den tatsächlichen Mahnlauf-
                        # Status nachschlagen kann, statt den Slot blind
                        # als sicher unversandt freizugeben.
                        self.tageslauf_repo.verknuepfe_mahnlauf(tageslauf.id, mahnlauf_id=letzter_mahnlauf.id)
                        ergebnis = self._dispatch_mahnlauf(
                            ctx=ctx, mahnlauf=letzter_mahnlauf, heute=heute, confirmed=confirmed, unclear=unclear,
                            zahlungsziel_geklaert_wert=zahlungsziel_ok,
                        )
                        counts["gesendet" if ergebnis.status == "GESENDET" else "blockiert"] += 1
                        if ergebnis.status in ("GESENDET", "UNSICHER"):
                            # Bytes waren tatsächlich unterwegs - der Slot
                            # bleibt DAUERHAFT geschlossen, egal ob der
                            # Provider die Annahme schon bestätigt hat.
                            tatsaechlich_versucht = True
                finally:
                    # Korrektur nach unabhängiger Abnahme (Codex,
                    # 20.10.2026, reproduzierter Fehler): `_dispatch_
                    # mahnlauf` kann NACHWEISLICH gesendet und ERST DANACH
                    # (vor Rückgabe des Ergebnisses) eine Exception werfen
                    # - in diesem Fall bleibt `tatsaechlich_versucht` oben
                    # fälschlich `False`, weil die Zeile, die es auf
                    # `True` setzt, nie erreicht wird. Dasselbe konservative
                    # Prinzip wie `freigeben_verwaiste`: VOR dem Freigeben
                    # den tatsächlichen `MahnLaufTable`-Status nachschlagen
                    # (bereits VOR jedem Versandversuch über `verknuepfe_
                    # mahnlauf` oben mit dem Slot verknüpft) - `GESENDET`/
                    # `UNSICHER` bedeutet ein Providerkontakt hat
                    # tatsächlich stattgefunden oder ist ungewiss, der
                    # Slot darf dann NIE als sicher unversandt freigegeben
                    # werden. Nur wenn der Mahnlauf nachweislich nie über
                    # die Planung hinauskam, bleibt es beim Freigeben.
                    if not tatsaechlich_versucht and letzter_mahnlauf is not None:
                        aktueller_mahnlauf = self.mahnlauf_repo.get(letzter_mahnlauf.id)
                        if aktueller_mahnlauf is not None and aktueller_mahnlauf.status in ("GESENDET", "UNSICHER"):
                            tatsaechlich_versucht = True
                    if tatsaechlich_versucht:
                        self.tageslauf_repo.markiere_versendet(
                            tageslauf.id, mahnlauf_id=(letzter_mahnlauf.id if letzter_mahnlauf is not None else None),
                        )
                    else:
                        self.tageslauf_repo.freigeben(tageslauf.id)
            except ObjektAusgeschlossenError:
                continue
        return counts

    def status_abgleichen(self, *, ctx):
        """GET only, including after sending flags are disabled. Never re-send."""
        require_schreibrecht(ctx)
        if self.client is None:
            return 0
        count = 0
        # `MahnFallTable` ist HIER BEWUSST NICHT (mehr) enthalten: jeder
        # produktive Versand läuft über den gebündelten Mahnlauf-Pfad
        # (`plane_mahnlauf`/`versende_mahnlauf`), dessen Mitglieder unter
        # der GRUPPEN-Referenz ("mahnungslauf:" + Gruppen-outbox_key)
        # gesendet werden, NICHT unter ihrem eigenen `outbox_key`
        # ("mahnung:" + eigener outbox_key) - eine generische Abfrage
        # unter der EIGENEN Referenz würde beim tatsächlichen Provider
        # ins Leere laufen. Gebündelte Mahnläufe werden stattdessen über
        # `MahnwesenService.vervollstaendige_unsichere_mahnlaeufe` unten
        # korrekt (Gruppen-Referenz, samt Mitgliedsnachweisen/
        # Kostenbuchung) abgeglichen. Der einzelfallbasierte, in
        # Produktion nicht mehr verdrahtete `versenden()`-Pfad hat noch
        # keine eigene Status-Abfrage-Rekonziliation - siehe
        # `docs/hausverwaltung/OFFENE_PUNKTE.md`.
        specs = [
            (ErhoehungsschreibenTable, {"UNKLAR"}, "GESENDET", "versendet_am", "externe_versandreferenz", lambda r: r.idempotenzschluessel),
            (VertragsendeErinnerungTable, {"UNKLAR"}, "BENACHRICHTIGT", "benachrichtigt_am", None,
             lambda r: f"vertragsende:{r.vertrag_id}:{r.end_datum.isoformat()}"),
        ]
        for table, pending, done, field, ref_field, reference in specs:
            with self.sf() as session:
                rows = list(session.execute(select(table).where(table.status.in_(pending))).scalars())
            for row in rows:
                contract = self.bundle.stammdaten_repository.get_vertrag(row.vertrag_id)
                if contract is None or not ctx.has_zugriff(contract.gesellschaft_id):
                    continue
                ref = reference(row)
                try:
                    result = self.client.status_abfragen(ref)
                except (TransportFehlerUngewissError, ValueError):
                    continue
                if nachweis_daten(result):
                    count += int(versand_belegen(self.sf, table, row.id, ergebnis=result, erlaubt=pending,
                        neuer_status=done, zeitfeld=field, referenz=ref, referenzfeld=ref_field))

        # Gebündelte Mahnläufe (MahnLaufTable) brauchen mehr als den
        # generischen Ein-Feld-Übergang oben: eine bestätigte Quittung
        # muss zusätzlich JEDES noch offene Mitglied und die Kostenbuchung
        # nachziehen (siehe `MahnwesenService.vervollstaendige_unsichere_
        # mahnlaeufe`-Docstring, Auftrag Markus 14.09.2026) - deshalb ein
        # eigener Aufruf statt eines weiteren generischen `specs`-Eintrags.
        count += len(self.mahn_service.vervollstaendige_unsichere_mahnlaeufe(
            ctx=ctx, akteur="hv-mailversand-status-abgleich", status_abfragen_fn=self.client.status_abfragen,
        ))
        return count

    def versanduebersicht(self, *, ctx):
        result = []
        specs = [(ErhoehungsschreibenTable, "Indexanpassung", "versendet_am"),
                 (MahnFallTable, "Mahnung", "gesendet_am"),
                 (VertragsendeErinnerungTable, "Vertragsende an Markus", "benachrichtigt_am")]
        with self.sf() as session:
            for table, kind, field in specs:
                for row in session.execute(select(table)).scalars():
                    contract = self.bundle.stammdaten_repository.get_vertrag(row.vertrag_id)
                    if contract is None or not ctx.has_zugriff(contract.gesellschaft_id):
                        continue
                    unit = self.bundle.stammdaten_repository.get_einheit(contract.einheit_id)
                    obj = self.bundle.stammdaten_repository.objekt_fuer_vertrag(contract.id)
                    event = session.execute(select(AuditEventTable).where(
                        AuditEventTable.entity_typ == table.__tablename__,
                        AuditEventTable.entity_id == str(row.id),
                        AuditEventTable.aktion == "MAILVERSAND_BESTAETIGT"
                    ).order_by(AuditEventTable.id.desc())).scalars().first()
                    proof = event.payload if event else {}
                    result.append({"art": kind, "objekt": obj.bezeichnung,
                        "einheit": unit.bezeichnung, "vertrag_id": contract.id,
                        "status": row.status, "versendet_am": getattr(row, field),
                        "nachweis": proof.get("externe_referenz", ""),
                        "provider_referenz": proof.get("provider_referenz", "")})
        return result
