"""Deterministic daily MailOps wiring for existing business outboxes."""
from datetime import timedelta

from sqlalchemy import select

from mietinkasso.auth.service import require_gesellschaft_access, require_schreibrecht
from mietinkasso.bank.repository import BankRepository
from mietinkasso.bank.service import BankImportService
from mietinkasso.domain.exceptions import ObjektAusgeschlossenError, TransportFehlerUngewissError
from mietinkasso.indexautomatik.mailnachweis import nachweis_daten, versand_belegen
from mietinkasso.indexautomatik.mailops_client import MailOpsAuftrag, MailOpsClient
from mietinkasso.indexautomatik.mailops_transport import MailOpsTransportadapter
from mietinkasso.infrastructure.db.tables import (
    AuditEventTable, ErhoehungsschreibenTable, MahnFallTable, VertragsendeErinnerungTable,
)
from mietinkasso.mahnwesen.kosten_repository import MahnkostenRepository
from mietinkasso.mahnwesen.kosten_service import MahnkostenService
from mietinkasso.mahnwesen.repository import MahnFallRepository, MahnLaufRepository, MahnPolicyRepository
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
    if vorschau.bereits_gebuchte_zinsen_cent > 0:
        zeilen.append(f"Bereits verrechnete Verzugszinsen aus früheren Mahnläufen: {_eur_text(vorschau.bereits_gebuchte_zinsen_cent)} EUR.")
    neu_zinsen = vorschau.neue_zinsen_delta_cent
    if neu_zinsen > 0:
        satz_text = f"{vorschau.zinssatz_prozent} % p.a." if vorschau.zinssatz_prozent is not None else "mehreren Sätzen (siehe Zeiträume unten)"
        zeitraum_text = f"{vorschau.zins_von.strftime('%d.%m.%Y')} bis {vorschau.zins_bis.strftime('%d.%m.%Y')}" if vorschau.zins_von and vorschau.zins_bis else ""
        zeilen.append(f"Neu anzusetzende Verzugszinsen: {_eur_text(neu_zinsen)} EUR ({satz_text}, Zeitraum {zeitraum_text}).")
        if len(vorschau.zins_segmente) > 1:
            for segment in vorschau.zins_segmente:
                if segment.zinsen_cent <= 0:
                    continue
                zeilen.append(
                    f"  - {segment.von.strftime('%d.%m.%Y')} bis {segment.bis.strftime('%d.%m.%Y')}: "
                    f"{_eur_text(segment.zinsen_cent)} EUR ({segment.satz_prozent} % p.a.)."
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
    if not zeilen:
        return ""
    return "\n".join(zeilen) + "\n\n"


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
        self.mahnkosten_service = MahnkostenService(self.mahnkosten_repo, self.op_service, bundle.stammdaten_repository)
        self.mahn_service = MahnwesenService(self.mahn_repo, bundle.stammdaten_repository,
            self.op_service, self.policy_repo, bank_stand_max_age_days=settings.bank_stand_max_age_days,
            mahnkosten_service=self.mahnkosten_service, mahnlauf_repository=self.mahnlauf_repo)

    def owner_senden(self, auftrag):
        if self.client is None or not self.settings.hv_mail_allowlist_bestaetigt:
            raise ValueError("Privater Mailweg ist nicht freigegeben.")
        return self.client.senden(MailOpsAuftrag(
            auftrag["idempotenzschluessel"], "VERTRAGSENDE", auftrag["empfaenger"], "Markus Bauer",
            "Mietvertrag endet: Entscheidung zur Verlängerung", auftrag["text"],
            "OWNER-ONLY-DREI-KALENDERMONATE:" + auftrag["vertrag_id"]))

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
                )
                vorschau_slot["vorschau"] = vorschau
            else:
                vorschau = None
            kosten_text = _mahnkosten_text_baustein(vorschau)

            deadline = heute + timedelta(days=contract.zahlungsfrist_tage)
            subject = "Zahlungserinnerung" if mahnlauf.stufe == 1 else "Zweite Mahnung"
            anker = mitglieder[0]
            empfaenger_name = anker.snapshot["empfaenger_name"]
            text = (
                f"Guten Tag {empfaenger_name},\n\n"
                f"für {obj.bezeichnung}, {unit.bezeichnung}, ist folgende Forderung offen:\n\n"
                + "\n".join(posten_zeilen) +
                f"\n\nHauptforderung gesamt: {_eur_text(gesamt_cent)} EUR.\n\n"
                + kosten_text +
                f"Bitte begleichen Sie den offenen Gesamtbetrag bis {deadline.strftime('%d.%m.%Y')} "
                "auf das Ihnen für dieses Mietverhältnis bekannt gegebene Konto. "
                "Bei einer inzwischen erfolgten Zahlung senden Sie uns bitte den Zahlungsbeleg.\n\n"
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
                for stufe in geplante_stufen:
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
