"""Deterministic daily MailOps wiring for existing business outboxes."""
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select

from mietinkasso.auth.service import require_gesellschaft_access, require_schreibrecht
from mietinkasso.bank.repository import BankRepository
from mietinkasso.bank.service import BankImportService
from mietinkasso.domain.exceptions import (
    BindungInkonsistentError, ObjektAusgeschlossenError, TransportFehlerUngewissError, VorgangIdKonfliktError,
)
from mietinkasso.indexautomatik.mailnachweis import RekonstruierterBeleg, nachweis_daten, versand_belegen
from mietinkasso.indexautomatik.mailops_client import MailOpsAuftrag, MailOpsClient
from mietinkasso.indexautomatik.mailops_transport import MailOpsTransportadapter
from mietinkasso.infrastructure.db.tables import (
    AuditEventTable, ErhoehungsschreibenTable, MahnFallTable, MahnLaufTable, VertragsendeErinnerungTable,
)
from mietinkasso.mahnwesen import brief_sftp
from mietinkasso.mahnwesen.brief_pdf import Absender as BriefAbsender
from mietinkasso.mahnwesen.brief_pdf import Forderungszeile as BriefForderungszeile
from mietinkasso.mahnwesen.brief_pdf import Zinssegment as BriefZinssegment
from mietinkasso.mahnwesen.brief_pdf import erzeuge_mahnbrief_pdf
from mietinkasso.mahnwesen.kosten import zins_bis_einschliesslich
from mietinkasso.mahnwesen.mahntexte import text_fuer_stufe
from mietinkasso.mahnwesen.kosten_repository import MahnkostenRepository
from mietinkasso.mahnwesen.kosten_service import MahnkostenService
from mietinkasso.mahnwesen.mahnbrief_archiv import MahnbriefArchivRepository
from mietinkasso.mahnwesen.repository import (
    BriefAnbieterProfilRepository, MahnEmpfaengerZugangRepository, MahnFallRepository, MahnKanalregelRepository,
    MahnLaufRepository, MahnPolicyRepository, VertragMitmieterRepository,
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


class HVMailversandService:
    def __init__(self, session_factory, bundle, settings, *, client=None):
        self.sf, self.bundle, self.settings = session_factory, bundle, settings
        self.client = client if client is not None else mail_client_fuer(settings)
        self.bank_repo = BankRepository(session_factory)
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
            mitmieter_repository=self.mitmieter_repo, empfaenger_zugang_repository=self.empfaenger_zugang_repo)

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

    def _dispatch_mahnlauf(self, *, ctx, mahnlauf, heute, confirmed, unclear) -> VersandErgebnis:
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
            if mahnlauf.kanal == "BRIEF":
                return self._sende_brief(mahnlauf=mahnlauf, mitglieder=mitglieder, heute=heute, vorschau_slot=vorschau_slot)

            if self.client is None:
                raise ValueError("Privater Mailweg ist nicht eingerichtet.")

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
                posten_zeilen.append(f"- {_eur_text(debt.rest_cent)} EUR, fällig seit {debt.faelligkeit.strftime('%d.%m.%Y')}")

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
            kosten_text = _mahnkosten_text_baustein(vorschau)
            # Rückprüfung 14.09.2026: der Brieftext nannte bisher nirgends
            # den tatsächlichen Gesamtbetrag als Zahl - nur einzelne
            # Bausteine (Hauptforderung, ggf. Zinsen/Gebühr getrennt).
            # `zusaetzlicher_betrag_cent` deckt bereits neue Zinsen+Gebühr
            # ab (siehe `MahnkostenVorschau`-Docstring).
            gesamtbetrag_cent = gesamt_cent + (
                (vorschau.bereits_offene_mahnkosten_cent + vorschau.zusaetzlicher_betrag_cent) if vorschau is not None else 0
            )

            # `deadline` bleibt die bisherige, rein als ANZEIGE-Komfort
            # gedachte Schätzung (`heute + zahlungsfrist_tage`) - die
            # rechtlich maßgebliche Frist ist "binnen 14 Tagen AB ZUGANG"
            # (siehe `mahntexte.text_fuer_stufe`/`mahnwesen/fristen.py`),
            # der tatsächliche Zugang ist zum Versandzeitpunkt aber noch
            # nicht bekannt - dieses Datum ist deshalb ausdrücklich eine
            # Orientierungshilfe für den Mieter, nie die rechtliche
            # Fristgrundlage selbst.
            deadline = heute + timedelta(days=contract.zahlungsfrist_tage)
            mahntext = text_fuer_stufe(stufe=mahnlauf.stufe, rechtsordnung=contract.rechtsordnung, zahlungsfrist_bis=deadline)
            subject = mahntext.betreff
            anker = mitglieder[0]
            empfaenger_name = anker.snapshot["empfaenger_name"]
            rechtlicher_absatz = f"\n\n{mahntext.rechtlicher_kern}" if mahntext.rechtlicher_kern else ""
            text = (
                f"Guten Tag {empfaenger_name},\n\n"
                f"{mahntext.einleitung} {mahntext.kerntext}\n\n"
                f"Für {obj.bezeichnung}, {unit.bezeichnung}, ist folgende Forderung offen:\n\n"
                + "\n".join(posten_zeilen) +
                f"\n\nHauptforderung gesamt: {_eur_text(gesamt_cent)} EUR.\n\n"
                + kosten_text +
                f"Gesamtbetrag (Hauptforderung inkl. noch offener früherer Kosten und neuer zulässiger "
                f"Zinsen/Gebühr): {_eur_text(gesamtbetrag_cent)} EUR."
                + rechtlicher_absatz +
                "\n\nBei einer inzwischen erfolgten Zahlung senden Sie uns bitte den Zahlungsbeleg. "
                "Bei Fragen zur Forderung antworten Sie bitte auf diese Nachricht."
            )
            op_ids = sorted(m.forderung_op_position_id for m in mitglieder)
            return self.client.senden(MailOpsAuftrag(
                "mahnungslauf:" + mahnlauf.outbox_key, "MAHNUNG",
                anker.snapshot["empfaenger_email"], empfaenger_name, subject, text,
                f"MAHNPOLICY:{anker.policy_version}:VERTRAG:{mahnlauf.vertrag_id}:STUFE:{mahnlauf.stufe}:"
                f"FORDERUNGEN:{','.join(str(i) for i in op_ids)}",
            ))

        return self.mahn_service.versende_mahnlauf(
            ctx=ctx, mahnlauf_id=mahnlauf.id, heute=heute, bank_bestaetigt_bis=confirmed,
            ungeklaerte_eingaenge_vorhanden=unclear, send_enabled=send_enabled,
            versand_fn=versand_fn, mahnkosten_vorschau_slot=vorschau_slot,
        )

    def _sende_brief(self, *, mahnlauf, mitglieder, heute, vorschau_slot) -> None:
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
        contract = st.get_vertrag(mahnlauf.vertrag_id)
        obj = st.objekt_fuer_vertrag(mahnlauf.vertrag_id)
        unit = st.get_einheit(contract.einheit_id)
        account = st.get_konto_by_vertrag(mahnlauf.vertrag_id)
        debitor = st.get_debitor(account.debitor_id)
        if debitor is None or not debitor.postadresse_geprueft or not (debitor.adresse or "").strip():
            raise ValueError("Keine geprüfte Postadresse für den Briefkanal hinterlegt.")

        offene = {f.op_position_id: f for f in self.op_service.offene_forderungen(account.id, heute=heute)}
        gesamt_cent = 0
        for mitglied in mitglieder:
            debt = offene.get(mitglied.forderung_op_position_id)
            if debt is None or debt.rest_cent != mitglied.snapshot["betrag_cent"] or debt.faelligkeit is None:
                raise ValueError("Offene Forderung hat sich verändert.")
            gesamt_cent += debt.rest_cent

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

        offene_ids = frozenset(vorschau.forderung_op_position_ids) if vorschau is not None else frozenset(
            m.forderung_op_position_id for m in mitglieder
        )
        forderungszeilen = [
            BriefForderungszeile(
                bezeichnung=f.leistungsperiode or f.art,
                faelligkeit=f.faelligkeit if f.faelligkeit_bekannt else None,
                betrag_cent=f.rest_cent,
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
        anker = mitglieder[0]
        empfaenger_name = anker.snapshot["empfaenger_name"]

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
        pdf_bytes = erzeuge_mahnbrief_pdf(
            absender=absender, empfaenger_name=empfaenger_name,
            empfaenger_adresse=(debitor.adresse or "").replace(", ", "\n"),
            objekt_bezeichnung=obj.bezeichnung, einheit_bezeichnung=unit.bezeichnung, stufe=mahnlauf.stufe,
            rechtsordnung=contract.rechtsordnung,
            heute=heute, zahlungsfrist_bis=deadline, forderungszeilen=forderungszeilen,
            bereits_offene_mahnkosten_cent=(vorschau.bereits_offene_mahnkosten_cent if vorschau is not None else 0),
            neue_zinsen_delta_cent=(vorschau.neue_zinsen_delta_cent if vorschau is not None else 0),
            zins_segmente=zins_segmente, zinssatz_einheitlich_text=zinssatz_einheitlich_text,
            neue_gebuehr_cent=((vorschau.gebuehr_cent or 0) if vorschau is not None else 0),
            gebuehr_rechtsgrundlage=(vorschau.gebuehr_rechtsgrundlage if vorschau is not None else None),
            gesamtbetrag_cent=gesamtbetrag_cent,
        )

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
            return None

        # Ab hier gilt der Upload als erfolgreich gemeldet (Bytes wurden
        # übertragen) - JEDE weitere Störung bleibt ungewiss, NIE BLOCKIERT.
        try:
            self.mahnbrief_archiv_repo.aktualisiere_status(
                archiv_id=archiv_zeile.id, neuer_status="UEBERMITTELT",
                beleg_referenz=ergebnis.externe_referenz, erfasst_von="hv-mailversand",
            )
        except Exception:
            pass  # Archivstatus bleibt ggf. bei VORBEREITET stehen, über sende_schluessel manuell nachvollziehbar
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

        mahnlauf = self.mahn_service.plane_mahnlauf(
            ctx=ctx, vertrag=contract, konto=account, stufe=row.stufe, heute=heute,
            bank_bestaetigt_bis=confirmed, ungeklaerte_eingaenge_vorhanden=unclear,
        )
        if mahnlauf is not None and row_id in MahnLaufRepository.mitglieder_ids(mahnlauf):
            return self._dispatch_mahnlauf(ctx=ctx, mahnlauf=mahnlauf, heute=heute, confirmed=confirmed, unclear=unclear)

        # `row_id` selbst ist NICHT (mehr) Teil der aktuell gebildeten
        # Gruppe (z. B. Frist noch nicht abgelaufen, oder bei der
        # Planung final aus der Bündelung ausgeschieden) - ANDERE
        # Mitglieder desselben Vertrags/derselben Stufe können trotzdem
        # bereits versandbereit sein und wurden oben bereits verarbeitet;
        # das Ergebnis für DIESEN Aufrufer richtet sich nach SEINEM
        # eigenen, tatsächlich persistierten Stand.
        if mahnlauf is not None:
            self._dispatch_mahnlauf(ctx=ctx, mahnlauf=mahnlauf, heute=heute, confirmed=confirmed, unclear=unclear)
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
                planned = self.mahn_service.plane_alle_offenen_forderungen(ctx=ctx, vertrag=contract,
                    konto=account, policy=policy, heute=heute, bank_bestaetigt_bis=confirmed,
                    ungeklaerte_eingaenge_vorhanden=unclear)
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
                for stufe in stufen_fuer_diesen_lauf:
                    mahnlauf = self.mahn_service.plane_mahnlauf(
                        ctx=ctx, vertrag=contract, konto=account, stufe=stufe, heute=heute,
                        bank_bestaetigt_bis=confirmed, ungeklaerte_eingaenge_vorhanden=unclear,
                    )
                    if mahnlauf is None:
                        continue
                    ergebnis = self._dispatch_mahnlauf(
                        ctx=ctx, mahnlauf=mahnlauf, heute=heute, confirmed=confirmed, unclear=unclear,
                    )
                    counts["gesendet" if ergebnis.status == "GESENDET" else "blockiert"] += 1
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
