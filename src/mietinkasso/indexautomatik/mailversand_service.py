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
from mietinkasso.mahnwesen.kosten_repository import MahnkostenRepository
from mietinkasso.mahnwesen.kosten_service import MahnkostenService
from mietinkasso.mahnwesen.repository import MahnFallRepository, MahnPolicyRepository
from mietinkasso.mahnwesen.service import MahnwesenService
from mietinkasso.op.repository import OPRepository
from mietinkasso.op.service import OPService, compute_content_hash


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
        self.policy_repo = MahnPolicyRepository(session_factory)
        self.mahnkosten_repo = MahnkostenRepository(session_factory)
        self.mahnkosten_service = MahnkostenService(self.mahnkosten_repo, self.op_service, bundle.stammdaten_repository)
        self.mahn_service = MahnwesenService(self.mahn_repo, bundle.stammdaten_repository,
            self.op_service, self.policy_repo, bank_stand_max_age_days=settings.bank_stand_max_age_days,
            mahnkosten_service=self.mahnkosten_service)

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
        """Sendet GENAU EIN Schreiben je Vertrag+Mahnstufe (Rückprüfung
        14.09.2026), unabhängig davon, für welchen einzelnen `row_id`
        (eine von möglicherweise mehreren GEPLANTEN Forderungen desselben
        Vertrags/derselben Stufe - z. B. HMZ+BK derselben Vorschreibung)
        diese Methode aufgerufen wird: alle GEPLANTEN MahnFälle desselben
        (`vertrag_id`, `stufe`) werden zu EINER "Gruppe" gebündelt, die
        Forderung mit der KLEINSTEN Id wird deterministisch zum
        "führenden" Fall (in aller Regel die am längsten überfällige,
        siehe Modul-Hinweis unten). Nur für den führenden Fall wird
        tatsächlich einmal der externe Mailversand aufgerufen; alle
        anderen Gruppenmitglieder übernehmen DENSELBEN bereits erhaltenen
        Versandnachweis (kein zweiter externer Aufruf) und werden über
        den bestehenden, unverändert genutzten
        `MahnwesenService.versenden()`-Zustandsautomaten (Claim/Idempotenz/
        Bank-/Sperr-/Empfänger-Frischprüfung je Fall) individuell auf
        GESENDET gesetzt - dieselbe Prüfschärfe wie bisher, nur EIN
        tatsächlich verschickter Brief.

        Zwei GLEICHZEITIGE Aufrufe für zwei VERSCHIEDENE Mitglieder
        derselben Gruppe lösen NIE zwei E-Mails aus: beide berechnen
        dieselbe Gruppe/denselben führenden Fall und konkurrieren um
        DESSEN atomaren `claim_fuer_versand`-Compare-and-Swap (siehe
        `mahnwesen/repository.py`) - nur einer gewinnt und ruft den
        externen Versand auf, der andere sieht `BEREITS_VERARBEITET`.

        Der Mahnkosten-/Zinsnachweis im Brieftext stammt aus GENAU EINER
        `MahnkostenService.vorschau()`-Berechnung (unmittelbar vor dem
        tatsächlichen Versand, berücksichtigt also auch kurz zuvor
        eingegangene Zahlungen) und wird über `mahnkosten_vorschau_slot`
        1:1 an die anschließende Buchung weitergereicht - Text und
        Buchung können dadurch nie auseinanderlaufen (siehe
        `kosten_service.py`-Moduldoc)."""

        row = self.mahn_repo.get(row_id)
        if row is None:
            raise ValueError("Mahnfall fehlt.")
        require_gesellschaft_access(ctx, row.gesellschaft_id)
        require_schreibrecht(ctx)
        confirmed, unclear = bank_freigabe_ableiten(
            self.bank_repo, self.bank_service, row.gesellschaft_id, row.vertrag_id)
        send_enabled = bool(self.client and self.settings.send_enabled and self.settings.hv_mail_allowlist_bestaetigt)

        gruppe = sorted(
            (f for f in self.mahn_repo.list_fuer_vertrag(row.vertrag_id) if f.stufe == row.stufe and f.status == "GEPLANT"),
            key=lambda f: f.id,
        )
        if not any(f.id == row_id for f in gruppe):
            # `row` selbst ist nicht (mehr) GEPLANT (z. B. bereits verarbeitet
            # oder blockiert) - unverändertes Einzelverhalten für eine
            # konsistente Fehlermeldung/Statusabfrage, keine Gruppierung.
            gruppe = [row]
        leader = gruppe[0]
        mitglieder = gruppe

        beleg_holder: dict = {}
        vorschau_slot: dict = {}

        def leader_versand_fn(_leader_snapshot):
            if self.client is None:
                raise ValueError("Privater Mailweg ist nicht eingerichtet.")
            # Existing policies have zero charges/interest. Other policies need
            # an explicitly implemented calculation before any tenant mail.
            for mitglied in mitglieder:
                if mitglied.snapshot["gebuehr_cent"] or float(mitglied.snapshot["zinsen_prozent"]) != 0:
                    raise ValueError("Mahngebühren/Zinsen benötigen eine eigene belegte Berechnung.")

            st = self.bundle.stammdaten_repository
            contract = st.get_vertrag(row.vertrag_id)
            obj = st.objekt_fuer_vertrag(row.vertrag_id)
            unit = st.get_einheit(contract.einheit_id)
            account = st.get_konto_by_vertrag(row.vertrag_id)
            offene = {f.op_position_id: f for f in self.op_service.offene_forderungen(account.id, heute=heute)}

            posten_zeilen = []
            gesamt_cent = 0
            for mitglied in mitglieder:
                debt = offene.get(mitglied.forderung_op_position_id)
                if debt is None or debt.rest_cent != mitglied.snapshot["betrag_cent"] or debt.faelligkeit is None:
                    raise ValueError("Offene Forderung hat sich verändert.")
                gesamt_cent += debt.rest_cent
                posten_zeilen.append(f"- {_eur_text(debt.rest_cent)} EUR, fällig seit {debt.faelligkeit.strftime('%d.%m.%Y')}")

            vorschau = None
            if self.mahnkosten_service is not None:
                vorschau = self.mahnkosten_service.vorschau(vertrag_id=row.vertrag_id, stufe=leader.stufe, heute=heute)
            vorschau_slot["vorschau"] = vorschau
            kosten_text = _mahnkosten_text_baustein(vorschau)

            deadline = heute + timedelta(days=contract.zahlungsfrist_tage)
            subject = "Zahlungserinnerung" if leader.stufe == 1 else "Zweite Mahnung"
            empfaenger_name = leader.snapshot["empfaenger_name"]
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
            gruppen_schluessel = compute_content_hash({"op_ids": op_ids, "stufe": leader.stufe})
            beleg = self.client.senden(MailOpsAuftrag(
                "mahnungslauf:" + gruppen_schluessel, "MAHNUNG",
                leader.snapshot["empfaenger_email"], empfaenger_name, subject, text,
                f"MAHNPOLICY:{leader.policy_version}:VERTRAG:{row.vertrag_id}:STUFE:{leader.stufe}:FORDERUNGEN:{','.join(str(i) for i in op_ids)}",
            ))
            beleg_holder["beleg"] = beleg
            return beleg

        leader_ergebnis = self.mahn_service.versenden(
            ctx=ctx, mahnfall_id=leader.id, heute=heute, bank_bestaetigt_bis=confirmed,
            ungeklaerte_eingaenge_vorhanden=unclear, send_enabled=send_enabled,
            versand_fn=leader_versand_fn, mahnkosten_vorschau_slot=vorschau_slot,
        )

        if leader.id == row_id:
            ergebnis_fuer_aufrufer = leader_ergebnis
        else:
            ergebnis_fuer_aufrufer = None

        if leader_ergebnis.status == "GESENDET":
            beleg = beleg_holder["beleg"]
            for mitglied in mitglieder:
                if mitglied.id == leader.id:
                    continue
                einzel = self.mahn_service.versenden(
                    ctx=ctx, mahnfall_id=mitglied.id, heute=heute, bank_bestaetigt_bis=confirmed,
                    ungeklaerte_eingaenge_vorhanden=unclear, send_enabled=send_enabled,
                    versand_fn=lambda _snapshot, _beleg=beleg: _beleg, mahnkosten_vorschau_slot=vorschau_slot,
                )
                if mitglied.id == row_id:
                    ergebnis_fuer_aufrufer = einzel

        return ergebnis_fuer_aufrufer if ergebnis_fuer_aufrufer is not None else leader_ergebnis

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
                        # `mahnung_senden` bündelt automatisch ALLE zu diesem
                        # Zeitpunkt GEPLANTEN Fälle desselben Vertrags/derselben
                        # Stufe zu EINEM Schreiben (siehe dort) - ein bereits
                        # über eine FRÜHERE Gruppen-Zustellung in DIESEM Lauf
                        # miterledigtes Mitglied ist hier nicht mehr GEPLANT
                        # und wird nicht nochmal einzeln gezählt/angestoßen.
                        aktueller_stand = self.mahn_repo.get(p.mahnfall_id)
                        if aktueller_stand is None or aktueller_stand.status != "GEPLANT":
                            continue
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
