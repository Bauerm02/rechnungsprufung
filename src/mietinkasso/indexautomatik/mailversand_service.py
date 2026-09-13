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
from mietinkasso.infrastructure.db.tables import AuditEventTable, ErhoehungsschreibenTable, MahnFallTable, VertragsendeErinnerungTable
from mietinkasso.mahnwesen.repository import MahnFallRepository, MahnPolicyRepository
from mietinkasso.mahnwesen.service import MahnwesenService
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


class HVMailversandService:
    def __init__(self, session_factory, bundle, settings, *, client=None):
        self.sf, self.bundle, self.settings = session_factory, bundle, settings
        self.client = client if client is not None else mail_client_fuer(settings)
        self.bank_repo = BankRepository(session_factory)
        self.op_service = OPService(OPRepository(session_factory), bundle.stammdaten_repository)
        self.bank_service = BankImportService(self.bank_repo, bundle.stammdaten_repository, self.op_service)
        self.mahn_repo = MahnFallRepository(session_factory)
        self.policy_repo = MahnPolicyRepository(session_factory)
        self.mahn_service = MahnwesenService(self.mahn_repo, bundle.stammdaten_repository,
            self.op_service, self.policy_repo, bank_stand_max_age_days=settings.bank_stand_max_age_days)

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

    def mahnung_senden(self, *, ctx, row_id, heute):
        row = self.mahn_repo.get(row_id)
        if row is None:
            raise ValueError("Mahnfall fehlt.")
        require_gesellschaft_access(ctx, row.gesellschaft_id)
        require_schreibrecht(ctx)
        confirmed, unclear = bank_freigabe_ableiten(
            self.bank_repo, self.bank_service, row.gesellschaft_id, row.vertrag_id)

        def provider(snapshot):
            if self.client is None:
                raise ValueError("Privater Mailweg ist nicht eingerichtet.")
            # Existing policies have zero charges/interest. Other policies need
            # an explicitly implemented calculation before any tenant mail.
            if snapshot["gebuehr_cent"] or float(snapshot["zinsen_prozent"]) != 0:
                raise ValueError("Mahngebühren/Zinsen benötigen eine eigene belegte Berechnung.")
            st = self.bundle.stammdaten_repository
            contract = st.get_vertrag(row.vertrag_id)
            obj = st.objekt_fuer_vertrag(row.vertrag_id)
            unit = st.get_einheit(contract.einheit_id)
            account = st.get_konto_by_vertrag(row.vertrag_id)
            debt = next((d for d in self.op_service.offene_forderungen(account.id, heute=heute)
                         if d.op_position_id == row.forderung_op_position_id), None)
            if debt is None or debt.rest_cent != snapshot["betrag_cent"] or debt.faelligkeit is None:
                raise ValueError("Offene Forderung hat sich verändert.")
            deadline = heute + timedelta(days=contract.zahlungsfrist_tage)
            amount = f"{snapshot['betrag_cent']/100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
            subject = "Zahlungserinnerung" if row.stufe == 1 else "Zweite Mahnung"
            text = (f"Sehr geehrte(r) {snapshot['empfaenger_name']},\n\n"
                f"für {obj.bezeichnung}, {unit.bezeichnung}, ist die Forderung vom "
                f"{debt.faelligkeit.strftime('%d.%m.%Y')} über {amount} EUR noch offen.\n\n"
                f"Bitte begleichen Sie den offenen Betrag bis {deadline.strftime('%d.%m.%Y')} "
                "auf das Ihnen für dieses Mietverhältnis bekannt gegebene Konto. "
                "Bei einer inzwischen erfolgten Zahlung senden Sie uns bitte den Zahlungsbeleg.\n\n"
                "Bei Fragen zur Forderung antworten Sie bitte auf diese Nachricht.")
            return self.client.senden(MailOpsAuftrag("mahnung:" + row.outbox_key, "MAHNUNG",
                snapshot["empfaenger_email"], snapshot["empfaenger_name"], subject, text,
                f"MAHNPOLICY:{row.policy_version}:FORDERUNG:{row.forderung_op_position_id}:STUFE:{row.stufe}"))

        return self.mahn_service.versenden(ctx=ctx, mahnfall_id=row_id, heute=heute,
            bank_bestaetigt_bis=confirmed, ungeklaerte_eingaenge_vorhanden=unclear,
            send_enabled=bool(self.client and self.settings.send_enabled and self.settings.hv_mail_allowlist_bestaetigt),
            versand_fn=provider)

    def mahnlauf(self, *, ctx, heute):
        if not (self.client and self.settings.send_enabled and self.settings.hv_mail_allowlist_bestaetigt):
            return {"geplant": 0, "gesendet": 0, "blockiert": 0}
        self.mahn_service.markiere_verwaiste_als_unsicher()
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
                for p in planned:
                    if p.status == "GEPLANT":
                        counts["geplant"] += 1
                        result = self.mahnung_senden(ctx=ctx, row_id=p.mahnfall_id, heute=heute)
                        counts["gesendet" if result.status == "GESENDET" else "blockiert"] += 1
                    elif p.status == "BLOCKIERT":
                        counts["blockiert"] += 1
            except ObjektAusgeschlossenError:
                continue
        return counts

    def status_abgleichen(self, *, ctx):
        """GET only, including after sending flags are disabled. Never re-send."""
        require_schreibrecht(ctx)
        if self.client is None:
            return 0
        count = 0
        specs = [
            (ErhoehungsschreibenTable, {"UNKLAR"}, "GESENDET", "versendet_am", "externe_versandreferenz", lambda r: r.idempotenzschluessel),
            (MahnFallTable, {"UNSICHER"}, "GESENDET", "gesendet_am", None, lambda r: "mahnung:" + r.outbox_key),
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
