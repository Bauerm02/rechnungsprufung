"""Persistente Outbox für Erhöhungsschreiben (Auftrag 13.09.) - Zustände
Entwurf/blockiert/bereit/gesendet/Zugang bestätigt/ausgeführt/unklar
(`ErhoehungsschreibenStatus`). Vor jedem tatsächlichen Versand werden
Quelle/Empfänger/Vertragsstatus GEGEN DEN BEI DER ENTWURFSERSTELLUNG
FESTGEHALTENEN SNAPSHOT erneut geprüft (analog
`mahnwesen/service.py::versenden`) - eine seither geänderte Adresse
oder ein inzwischen ausgeschlossener/abgelaufener Vertrag stoppt den
Versand, statt mit veralteten Daten weiterzusenden.

"Bei mehr als einer referenzierten Komponente wird BEWUSST KEINE
Verteilungsregel für den neuen Gesamtbetrag erfunden" - ein solcher
Fall wird HIER, vor jeder Texterzeugung, blockiert (Modellreview
13.09.: "bei nicht unterstützter Mehrkomponentenverteilung vor Versand
blockieren")."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from mietinkasso.auth.service import AuthContext, require_gesellschaft_access, require_schreibrecht
from mietinkasso.domain.enums import ZUGANGSFORMEN_ALLE, ZUGANGSFORMEN_AUSREICHEND, Rechtsordnung
from mietinkasso.domain.exceptions import ObjektAusgeschlossenError, QuellenbelegFehltError, TransportFehlerUngewissError
from mietinkasso.indexautomatik.rechtsprofil import RechtsprofilService
from mietinkasso.indexautomatik.repository import ErhoehungsschreibenRepository, RechtsprofilRepository
from mietinkasso.indexautomatik.schreiben import (
    SchreibenJahresschritt,
    SchreibenKomponente,
    SchreibenKontext,
    erhoehungsschreiben_text_klausel,
    erhoehungsschreiben_text_mieweg,
)
from mietinkasso.indexautomatik.transport import Transportadapter, VersandAuftrag
from mietinkasso.indexautomatik.zeit import naechster_zinstermin_ab
from mietinkasso.infrastructure.db.tables import ErhoehungsschreibenTable, MieWegVorschauTable, RechtsprofilTable, VertragTable
from mietinkasso.stammdaten.repository import StammdatenRepository


class VersandErgebnis:
    def __init__(self, status: str, grund: str):
        self.status = status
        self.grund = grund

    def __eq__(self, other):
        return isinstance(other, VersandErgebnis) and (self.status, self.grund) == (other.status, other.grund)

    def __repr__(self):
        return f"VersandErgebnis({self.status!r}, {self.grund!r})"


_ZUGANGSFRIST_UNTERSTUETZTE_RECHTSORDNUNGEN = {
    Rechtsordnung.OESTERREICH_MRG_VOLL.value,
    Rechtsordnung.OESTERREICH_MRG_TEIL.value,
}


class ErhoehungsschreibenOutboxService:
    def __init__(
        self,
        repository: ErhoehungsschreibenRepository,
        stammdaten_repository: StammdatenRepository,
        rechtsprofil_repository: RechtsprofilRepository,
        rechtsprofil_service: RechtsprofilService,
        *,
        jlb_signatur: str,
    ):
        self._repository = repository
        self._stammdaten_repository = stammdaten_repository
        self._jlb_signatur = jlb_signatur
        self._rechtsprofil_repository = rechtsprofil_repository
        self._rechtsprofil_service = rechtsprofil_service

    def _empfaenger_snapshot(self, vertrag: VertragTable) -> dict:
        debitor = self._stammdaten_repository.get_debitor(vertrag.debitor_id)
        return {
            "debitor_id": vertrag.debitor_id,
            "name": debitor.name if debitor else None,
            "adresse": debitor.adresse if debitor else None,
            "email": debitor.email if debitor else None,
            "vertrag_gueltig_bis": vertrag.gueltig_bis.isoformat() if vertrag.gueltig_bis else None,
            "vertrag_rechtsordnung": vertrag.rechtsordnung,
        }

    def _entwurf_speichern(
        self, row: ErhoehungsschreibenTable, *, mehrkomponenten_blockiert: bool, bestehende_id: int | None = None
    ) -> ErhoehungsschreibenTable:
        """`bestehende_id`: Retry eines bereits vorhandenen, aber noch
        NICHT versendeten (ENTWURF/BLOCKIERT) Erhöhungsschreibens nach
        einer behobenen Quelle - überschreibt dessen Inhalt IN PLACE
        statt einen zweiten, gegen den partiellen Unique-Index
        verstoßenden Versuch zu unternehmen (unabhängiger Review,
        fd8c2b2-Folgereview: "Ein unsent BLOCKIERTer MieWeG-Fall muss
        nach behobener Quelle dagegen erneuerbar sein; dauerhaftes
        BEREITS_ERFASST darf den ganzen Aprilzyklus nicht verschlucken").
        Der Aufrufer (`service.py`) garantiert, dass `bestehende_id` nur
        für eine Zeile im Status ENTWURF/BLOCKIERT übergeben wird -
        niemals für eine bereits GESENDETe."""

        gruende: list[str] = []
        empfaenger = row.empfaenger_snapshot
        if not (empfaenger.get("adresse") or "").strip():
            gruende.append("Kein gültige Postadresse für den Empfänger hinterlegt - kein Versand ohne Zustelladresse.")
        if mehrkomponenten_blockiert:
            gruende.append(
                "Mehr als eine referenzierte Basis-Komponente - eine Verteilungsregel für den neuen "
                "Gesamtbetrag auf einzelne Positionen wird bewusst nicht automatisch erfunden. Manuelle "
                "Prüfung/Aufteilung erforderlich, kein automatischer Versand."
            )
        row.status = "BLOCKIERT" if gruende else "BEREIT"
        row.blockiert_gruende = gruende
        if bestehende_id is not None:
            return self._repository.aktualisieren(
                bestehende_id,
                status=row.status,
                blockiert_gruende=row.blockiert_gruende,
                erhoehung_cent=row.erhoehung_cent,
                massgeblicher_termin=row.massgeblicher_termin,
                schreiben_text=row.schreiben_text,
                rechtsprofil_id=row.rechtsprofil_id,
                rechtsprofil_version=row.rechtsprofil_version,
                mieweg_vorschau_id=row.mieweg_vorschau_id,
                empfaenger_snapshot=row.empfaenger_snapshot,
            )
        return self._repository.anlegen(row)

    def erstellen_aus_mieweg(
        self,
        *,
        ctx: AuthContext,
        vertrag: VertragTable,
        profil: RechtsprofilTable,
        vorschau: MieWegVorschauTable,
        ziel_bewertungsjahr: int,
        massgeblicher_termin,
        erhoehung_cent: int,
        aktuell_verrechnet_cent: int,
        referenzierte_komponenten: list,
        unveraenderte_komponenten: list,
        akteur: str,
        bestehende_id: int | None = None,
    ) -> ErhoehungsschreibenTable:
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)
        ergebnis = json.loads(vorschau.ergebnis_json)
        eingaben = json.loads(vorschau.eingaben_json)
        gesellschaft = self._stammdaten_repository.get_gesellschaft(vertrag.gesellschaft_id)
        objekt = self._stammdaten_repository.objekt_fuer_vertrag(vertrag.id)
        einheit = self._stammdaten_repository.get_einheit(vertrag.einheit_id)
        debitor = self._stammdaten_repository.get_debitor(vertrag.debitor_id)

        mehrkomponenten_blockiert = len(referenzierte_komponenten) != 1
        neuer_gesamt_je_komponente = erhoehung_cent + sum(k.betrag_cent for k in referenzierte_komponenten)
        geaenderte = [
            SchreibenKomponente(
                art=k.art,
                bezeichnung=k.bezeichnung,
                alter_betrag_cent=k.betrag_cent,
                neuer_betrag_cent=(k.betrag_cent + erhoehung_cent) if not mehrkomponenten_blockiert else k.betrag_cent,
                ust_satz_promille=k.ust_satz_promille,
            )
            for k in referenzierte_komponenten
        ]
        unveraendert = [
            SchreibenKomponente(
                art=k.art, bezeichnung=k.bezeichnung, alter_betrag_cent=k.betrag_cent,
                neuer_betrag_cent=k.betrag_cent, ust_satz_promille=k.ust_satz_promille,
            )
            for k in unveraenderte_komponenten
        ]
        jahresschritte = [
            SchreibenJahresschritt(
                jahr=s["jahr"], vpi_vorjahr=s["vpi_vorjahr"], vpi_jahr=s["vpi_jahr"],
                rohe_veraenderung=s["rohe_veraenderung"], gedaempfte_veraenderung=s["gedaempfte_veraenderung"],
                angewandte_veraenderung=s["angewandte_veraenderung"],
            )
            for s in ergebnis.get("jahresschritte", [])
        ]
        kontext = SchreibenKontext(
            gesellschaft_name=gesellschaft.name if gesellschaft else vertrag.gesellschaft_id,
            objekt_bezeichnung=objekt.bezeichnung,
            objekt_adresse=objekt.adresse,
            einheit_bezeichnung=einheit.bezeichnung if einheit else vertrag.einheit_id,
            mieter_name=debitor.name if debitor else vertrag.debitor_id,
            mieter_adresse=debitor.adresse if debitor else None,
            rechtsordnung=profil.rechtsordnung,
            klausel_referenz=profil.klausel_referenz,
            geaenderte_komponenten=geaenderte,
            unveraenderte_komponenten=unveraendert,
            erhoehung_cent=erhoehung_cent,
            massgeblicher_termin=massgeblicher_termin,
            vertrag_beleg_referenz=profil.vertrag_beleg_referenz,
            rechtsprofil_version=profil.version,
            jlb_signatur=self._jlb_signatur,
            bezugsjahr=profil.bezugsjahr,
            bezugsmonat=profil.bezugsmonat,
            ziel_bewertungsjahr=ziel_bewertungsjahr,
            jahresschritte=jahresschritte,
            vertraglich_zulaessiger_betrag_cent=ergebnis.get("vertraglich_zulaessiger_betrag_cent"),
            vertraglicher_quellenbeleg=eingaben.get("vertraglicher_quellenbeleg"),
        )
        text = "" if mehrkomponenten_blockiert else erhoehungsschreiben_text_mieweg(kontext)
        row = ErhoehungsschreibenTable(
            vertrag_id=vertrag.id,
            ziel_bewertungsjahr=ziel_bewertungsjahr,
            rechtsprofil_id=profil.id,
            rechtsprofil_version=profil.version,
            mieweg_vorschau_id=vorschau.id,
            status="ENTWURF",
            massgeblicher_termin=massgeblicher_termin,
            erhoehung_cent=erhoehung_cent,
            schreiben_text=text or "(blockiert - Mehrkomponenten-Verteilung nicht unterstützt)",
            idempotenzschluessel=f"{vertrag.id}:mieweg:{ziel_bewertungsjahr}",
            empfaenger_snapshot=self._empfaenger_snapshot(vertrag),
        )
        return self._entwurf_speichern(row, mehrkomponenten_blockiert=mehrkomponenten_blockiert, bestehende_id=bestehende_id)

    def erstellen_aus_index_anpassung(
        self,
        *,
        ctx: AuthContext,
        vertrag: VertragTable,
        profil: RechtsprofilTable,
        index_anpassung_id: int,
        massgeblicher_termin,
        erhoehung_cent: int,
        referenzierte_komponenten: list,
        unveraenderte_komponenten: list,
        akteur: str,
    ) -> ErhoehungsschreibenTable:
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)
        gesellschaft = self._stammdaten_repository.get_gesellschaft(vertrag.gesellschaft_id)
        objekt = self._stammdaten_repository.objekt_fuer_vertrag(vertrag.id)
        einheit = self._stammdaten_repository.get_einheit(vertrag.einheit_id)
        debitor = self._stammdaten_repository.get_debitor(vertrag.debitor_id)

        mehrkomponenten_blockiert = len(referenzierte_komponenten) != 1
        geaenderte = [
            SchreibenKomponente(
                art=k.art,
                bezeichnung=k.bezeichnung,
                alter_betrag_cent=k.betrag_cent,
                neuer_betrag_cent=(k.betrag_cent + erhoehung_cent) if not mehrkomponenten_blockiert else k.betrag_cent,
                ust_satz_promille=k.ust_satz_promille,
            )
            for k in referenzierte_komponenten
        ]
        unveraendert = [
            SchreibenKomponente(
                art=k.art, bezeichnung=k.bezeichnung, alter_betrag_cent=k.betrag_cent,
                neuer_betrag_cent=k.betrag_cent, ust_satz_promille=k.ust_satz_promille,
            )
            for k in unveraenderte_komponenten
        ]
        kontext = SchreibenKontext(
            gesellschaft_name=gesellschaft.name if gesellschaft else vertrag.gesellschaft_id,
            objekt_bezeichnung=objekt.bezeichnung,
            objekt_adresse=objekt.adresse,
            einheit_bezeichnung=einheit.bezeichnung if einheit else vertrag.einheit_id,
            mieter_name=debitor.name if debitor else vertrag.debitor_id,
            mieter_adresse=debitor.adresse if debitor else None,
            rechtsordnung=profil.rechtsordnung,
            klausel_referenz=profil.klausel_referenz,
            geaenderte_komponenten=geaenderte,
            unveraenderte_komponenten=unveraendert,
            erhoehung_cent=erhoehung_cent,
            massgeblicher_termin=massgeblicher_termin,
            vertrag_beleg_referenz=profil.vertrag_beleg_referenz,
            rechtsprofil_version=profil.version,
            jlb_signatur=self._jlb_signatur,
        )
        text = "" if mehrkomponenten_blockiert else erhoehungsschreiben_text_klausel(kontext)
        row = ErhoehungsschreibenTable(
            vertrag_id=vertrag.id,
            ziel_bewertungsjahr=None,
            rechtsprofil_id=profil.id,
            rechtsprofil_version=profil.version,
            index_anpassung_id=index_anpassung_id,
            status="ENTWURF",
            massgeblicher_termin=massgeblicher_termin,
            erhoehung_cent=erhoehung_cent,
            schreiben_text=text or "(blockiert - Mehrkomponenten-Verteilung nicht unterstützt)",
            idempotenzschluessel=f"{vertrag.id}:klausel:{index_anpassung_id}",
            empfaenger_snapshot=self._empfaenger_snapshot(vertrag),
        )
        return self._entwurf_speichern(row, mehrkomponenten_blockiert=mehrkomponenten_blockiert)

    def versenden(
        self,
        *,
        ctx: AuthContext,
        erhoehungsschreiben_id: int,
        heute,
        send_enabled: bool,
        mailops_allowlist_bestaetigt: bool,
        transport: Transportadapter,
    ) -> VersandErgebnis:
        """Unabhängiger Review (fd8c2b2-Folgereview): der bisherige
        Versand prüfte NUR den Empfänger-Snapshot - weder das aktuelle
        Datum (ein Vertrag konnte inzwischen abgelaufen sein), noch ob
        `massgeblicher_termin` überhaupt schon erreicht ist (kein
        verfrühter Versand vor Wirksamkeit), noch ob das zugrunde
        liegende Rechtsprofil seit der Entwurfserstellung invalidiert
        wurde (geänderte Quelle/Basis/Klausel). Alle drei werden jetzt
        UNMITTELBAR vor dem Claim erneut geprüft."""

        schreiben = self._repository.get(erhoehungsschreiben_id)
        if schreiben is None:
            raise ValueError(f"Unbekanntes Erhoehungsschreiben {erhoehungsschreiben_id}")
        vertrag = self._stammdaten_repository.get_vertrag(schreiben.vertrag_id)
        if vertrag is None:
            raise ValueError(f"Unbekannter Vertrag {schreiben.vertrag_id}")
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)

        if schreiben.status != "BEREIT":
            return VersandErgebnis("BEREITS_VERARBEITET", f"Status ist bereits {schreiben.status}; kein Doppelversand.")

        try:
            self._stammdaten_repository.pruefe_vertrag_nicht_ausgeschlossen(vertrag.id)
        except ObjektAusgeschlossenError as exc:
            self._repository.set_status(schreiben.id, "BLOCKIERT", blockiert_gruende=[str(exc)])
            return VersandErgebnis("BLOCKIERT", str(exc))

        if vertrag.gueltig_bis is not None and vertrag.gueltig_bis < heute:
            grund = f"Vertrag ist seit {vertrag.gueltig_bis.isoformat()} abgelaufen - kein Versand mehr."
            self._repository.set_status(schreiben.id, "BLOCKIERT", blockiert_gruende=[grund])
            return VersandErgebnis("BLOCKIERT", grund)

        if schreiben.massgeblicher_termin > heute:
            grund = (
                f"Wirksamkeitstermin ({schreiben.massgeblicher_termin.isoformat()}) liegt noch in der Zukunft "
                "- kein verfrühter Versand vor Wirksamkeit."
            )
            self._repository.set_status(schreiben.id, "BLOCKIERT", blockiert_gruende=[grund])
            return VersandErgebnis("BLOCKIERT", grund)

        aktuelles_profil = self._rechtsprofil_service.aktives_gueltiges_profil(vertrag.id, heute=heute)
        if (
            aktuelles_profil is None
            or aktuelles_profil.id != schreiben.rechtsprofil_id
            or aktuelles_profil.version != schreiben.rechtsprofil_version
        ):
            grund = (
                "Das zugrunde liegende Rechtsprofil ist seit der Entwurfserstellung nicht mehr die "
                "aktuell gültige, freigegebene Version - neue Prüfung/Entwurf erforderlich."
            )
            self._repository.set_status(schreiben.id, "BLOCKIERT", blockiert_gruende=[grund])
            return VersandErgebnis("BLOCKIERT", grund)

        aktueller_snapshot = self._empfaenger_snapshot(vertrag)
        if aktueller_snapshot != schreiben.empfaenger_snapshot:
            grund = (
                "Empfänger- oder Vertragsstatus hat sich seit der Entwurfserstellung geändert - neue "
                "Prüfung/Entwurf erforderlich, kein Versand mit veralteten Daten."
            )
            self._repository.set_status(schreiben.id, "BLOCKIERT", blockiert_gruende=[grund])
            return VersandErgebnis("BLOCKIERT", grund)

        if not send_enabled or not mailops_allowlist_bestaetigt:
            return VersandErgebnis(
                "BEREITS_VERARBEITET",
                "Versand deaktiviert (SEND_ENABLED und/oder Mailbox-Allowlist-Bestätigung fehlen) - "
                "nur Vorschau/Outbox, kein realer Versand.",
            )

        if not self._repository.claim_fuer_versand(schreiben.id):
            return VersandErgebnis("BEREITS_VERARBEITET", "Ein anderer Worker verarbeitet diesen Fall bereits.")

        auftrag = VersandAuftrag(
            referenz=schreiben.idempotenzschluessel,
            empfaenger_name=aktueller_snapshot["name"] or "",
            empfaenger_adresse=aktueller_snapshot["adresse"] or "",
            empfaenger_email=aktueller_snapshot["email"],
            betreff=f"Anhebung des Mietzinses - Vertrag {vertrag.id}",
            text=schreiben.schreiben_text,
        )
        try:
            bestaetigung = transport.senden(auftrag)
        except TransportFehlerUngewissError as exc:
            self._repository.set_status(schreiben.id, "UNKLAR", fehlergrund=str(exc))
            return VersandErgebnis("UNKLAR", str(exc))

        self._repository.set_status(
            schreiben.id,
            "GESENDET",
            versendet_am=datetime.combine(heute, datetime.min.time(), tzinfo=timezone.utc),
            externe_versandreferenz=bestaetigung.externe_referenz,
        )
        return VersandErgebnis("GESENDET", "Versand angenommen - Zugang muss gesondert bestätigt werden.")

    def markiere_verwaiste_als_unklar(self, *, jetzt: datetime | None = None, max_alter: timedelta = timedelta(minutes=15)):
        grenze = (jetzt or datetime.now(timezone.utc)) - max_alter
        verwaiste = self._repository.verwaiste_in_versand(aelter_als=grenze)
        for row in verwaiste:
            self._repository.set_status(row.id, "UNKLAR", fehlergrund="Verwaist zwischen Claim und Versandergebnis (Absturz-Recovery).")
        return verwaiste

    def zugang_bestaetigen(
        self,
        *,
        ctx: AuthContext,
        erhoehungsschreiben_id: int,
        heute,
        zugang_datum,
        zugangsform: str,
        zugang_beleg: str,
    ) -> ErhoehungsschreibenTable:
        schreiben = self._repository.get(erhoehungsschreiben_id)
        if schreiben is None:
            raise ValueError(f"Unbekanntes Erhoehungsschreiben {erhoehungsschreiben_id}")
        vertrag = self._stammdaten_repository.get_vertrag(schreiben.vertrag_id)
        if vertrag is None:
            raise ValueError(f"Unbekannter Vertrag {schreiben.vertrag_id}")
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)

        if schreiben.status != "GESENDET":
            raise ValueError(
                f"Nur ein GESENDETes Schreiben kann einen Zugang bestätigt bekommen (aktueller Status: "
                f"{schreiben.status})."
            )
        if zugang_datum > heute:
            raise ValueError("Zugangsdatum darf nicht in der Zukunft liegen.")
        if schreiben.versendet_am is not None and zugang_datum < schreiben.versendet_am.date():
            raise ValueError(
                f"Zugangsdatum ({zugang_datum.isoformat()}) liegt vor dem tatsächlichen Versanddatum "
                f"({schreiben.versendet_am.date().isoformat()}) - unplausible Eingabe."
            )
        if zugangsform not in ZUGANGSFORMEN_ALLE:
            raise ValueError(f"Unbekannte Zugangsform '{zugangsform}'.")
        if not (zugang_beleg or "").strip():
            raise QuellenbelegFehltError("Eine Zugangsbestätigung ohne Belegreferenz wird abgelehnt.")
        if zugangsform not in ZUGANGSFORMEN_AUSREICHEND:
            raise ValueError(
                f"Zugangsform '{zugangsform}' gilt bei MRG NICHT automatisch als rechtzeitiger, "
                "fristauslösender Zugang (z. B. eine bloß versendete, unbestätigte E-Mail - keine "
                "automatische Gleichsetzung von SMTP/HTTP-accepted mit Zugang). Bitte einen formal "
                "ausreichenden Nachweis erfassen oder den Fall manuell klären."
            )

        profil = self._rechtsprofil_repository.get(schreiben.rechtsprofil_id)
        if profil is None or profil.rechtsordnung not in _ZUGANGSFRIST_UNTERSTUETZTE_RECHTSORDNUNGEN:
            raise ValueError(
                f"Automatische Zustellungs-/Zahlungspflichtfristen (§ 16 Abs 9 MRG, 14 Tage) sind für die "
                f"Rechtsordnung {profil.rechtsordnung if profil else 'unbekannt'} hier nicht unterstützt - "
                "ungeklärte Fristenlage wird intern gesperrt, nicht mit einem geratenen Termin an den "
                "Mieter kommuniziert. Bitte manuell klären."
            )

        fruehester = zugang_datum + timedelta(days=14)
        zahlungspflicht_ab = naechster_zinstermin_ab(fruehester, faelligkeit_tag=vertrag.faelligkeit_tag)
        return self._repository.set_status(
            schreiben.id,
            "ZUGANG_BESTAETIGT",
            zugang_bestaetigt_am=zugang_datum,
            zugangsform=zugangsform,
            zugang_beleg=zugang_beleg,
            zahlungspflicht_ab=zahlungspflicht_ab,
        )

    def taegliche_pflege(self, *, heute) -> list[ErhoehungsschreibenTable]:
        """Reine Statuspflege ohne neue Fachentscheidung: sobald die
        Zahlungspflicht laut bestätigtem Zugang erreicht ist, wechselt
        der Fall sichtbar auf AUSGEFUEHRT. Löst KEINE Sollstellung/
        Vorschreibungsänderung aus (siehe OFFENE_PUNKTE.md)."""

        aktualisiert = []
        for schreiben in self._repository.liste_nach_status("ZUGANG_BESTAETIGT"):
            if schreiben.zahlungspflicht_ab is not None and heute >= schreiben.zahlungspflicht_ab:
                aktualisiert.append(self._repository.set_status(schreiben.id, "AUSGEFUEHRT"))
        return aktualisiert
