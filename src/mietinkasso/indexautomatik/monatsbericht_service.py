"""Persistenter, deterministischer Owner-Monatsbericht (Auftrag
HV-20260919-INDEX-MONATSBERICHT). Leitet Umfang A AUSSCHLIESSLICH aus dem
bereits vom monatlichen Indexautomatik-Lauf real berechneten
`IndexautomatikLaufTable`/`ErhoehungsschreibenTable` ab - KEINE zweite
Berechnung des MieWeG-Wohnungsrechner-Pfads.

Für den GESCHÄFTSRAUM-Pfad OHNE freigegebene Klausel erlaubt
`_gewerbe_rechenvorschlag` einen eng begrenzten, rein lesenden
Rechenvorschlag ("Rechenvorschlag – Ausführung noch nicht freigegeben"),
AUSSCHLIESSLICH über die bereits bestehenden, reinen `IndexService`-
Bausteine (`effektive_veraenderung_prozent`/`ueberschreitet_schwelle`) -
KEINE zweite Formel. Voraussetzung ist ein VOLLSTÄNDIG belegter
`IndexQuellenFaktenTable`-Datensatz INKLUSIVE `betrag_basisbindung_belegt`
(nie der heute bereits erhöhte Betrag gegen die ursprüngliche Basis
gerechnet). Schreibt NIE eine `IndexAnpassungTable`/ein
`ErhoehungsschreibenTable`/eine Soll-Umsetzung - reine Anzeige.

`IndexQuellenFaktenTable` ist ansonsten NUR Text-/Anzeige-Anreicherung
(nie Eingabe für eine echte Berechnung)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.domain.exceptions import TransportFehlerUngewissError
from mietinkasso.domain.money import cents_to_decimal, round_index_half_cent_down, to_cents
from mietinkasso.index.service import IndexService
from mietinkasso.indexautomatik.mailnachweis import nachweis_daten, versand_belegen
from mietinkasso.indexautomatik.repository import (
    ErhoehungsschreibenRepository,
    IndexMonatsberichtRepository,
    IndexQuellenFaktenRepository,
    RechtsprofilRepository,
    VpiRepository,
)
from mietinkasso.indexautomatik.service import _naechster_gueltiger_kalendermonat
from mietinkasso.infrastructure.db.tables import (
    IndexautomatikLaufTable,
    IndexMonatsberichtTable,
    IndexQuellenFaktenTable,
    IndexSollUmsetzungTable,
    RechtsprofilTable,
)
from mietinkasso.stammdaten.repository import StammdatenRepository

#: Muss WORTGLEICH mit `indexautomatik/service.py::_verarbeiten` sein -
#: nur dann wird ein BLOCKIERT-Fall mit dieser Ursache über
#: `IndexQuellenFaktenTable` angereichert statt den generischen Text
#: unverändert zu zeigen.
_GENERISCHER_KEIN_PROFIL_GRUND = (
    "Kein gültiges, freigegebenes Rechtsprofil (fehlt, nie freigegeben, oder seit Freigabe entwertet)."
)

_WOHNUNGSRECHNER_RECHTSORDNUNGEN = {"OESTERREICH_MRG_VOLL", "OESTERREICH_MRG_TEIL"}

#: Codex-Korrektur: Quellenfakten sind NIE "ungeprüft" (sie SIND belegte,
#: nachgewiesene Fakten) - nur die AUSFÜHRUNG (Übernahme in eine echte
#: Berechnung/Buchung) fehlt noch. Diese Kennzeichnung wird konsistent für
#: jede aus `IndexQuellenFaktenTable` stammende Anzeige verwendet.
_QUELLE_OHNE_AUSFUEHRUNGSFREIGABE = "QUELLENFAKTEN_OHNE_AUSFUEHRUNGSFREIGABE"

#: Codex-Präzisierung: "In Portal UND Owner-Mail verständliche Statusnamen
#: statt PRUEFUNG_NOETIG/MOEGLICH" - EINE zentrale Übersetzung, von
#: `_text_fuer_bericht` (Mail) UND vom Backoffice (`backoffice/app.py`)
#: gemeinsam genutzt, damit beide Oberflächen nie auseinanderlaufen. Der
#: interne Statuscode (`IndexMonatsberichtZeileTable.status`) bleibt
#: unverändert die technische Datenbank-/API-Wahrheit.
STATUS_LABELS = {
    "MOEGLICH": "Erhöhung möglich",
    "NOCH_NICHT_MOEGLICH": "Noch nicht möglich",
    "PRUEFUNG_NOETIG": "Prüfung nötig",
}


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def _naechste_gesetzliche_april_grenze(heute: date) -> date:
    """Dieselbe Formel wie `indexautomatik/service.py::_monatslauf_mieweg`
    (Codex: "Januar-/Apriltermine aus Regeln dynamisch ableiten, nicht
    2027 ewig als Festtext konservieren") - NIE ein hartkodiertes Jahr."""

    ziel_jahr = heute.year if heute >= date(heute.year, 4, 1) else heute.year - 1
    return date(ziel_jahr + 1, 4, 1)


class IndexMonatsberichtService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        repository: IndexMonatsberichtRepository,
        stammdaten_repository: StammdatenRepository,
        rechtsprofil_repository: RechtsprofilRepository,
        outbox_repository: ErhoehungsschreibenRepository,
        quellen_fakten_repository: IndexQuellenFaktenRepository,
        vpi_repository: VpiRepository,
        owner_email: str | None,
        backoffice_basis_url: str = "",
    ):
        self._session_factory = session_factory
        self._repository = repository
        self._stammdaten_repository = stammdaten_repository
        self._rechtsprofil_repository = rechtsprofil_repository
        self._outbox_repository = outbox_repository
        self._quellen_fakten_repository = quellen_fakten_repository
        self._vpi_repository = vpi_repository
        self._owner_email = owner_email
        self._backoffice_basis_url = backoffice_basis_url.rstrip("/")

    # -- Umfang A: Bericht erzeugen -----------------------------------------
    def erstellen_fuer_periode(
        self, *, periode: str, laeufe: list[IndexautomatikLaufTable], heute: date,
    ) -> IndexMonatsberichtTable | None:
        """Aufrufer: `scripts/indexautomatik_monatslauf.py`, UNMITTELBAR
        nach `monatslauf_alle` - `laeufe` ist exakt deren Rückgabe (ein
        Lauf je aktivem Vertrag dieser Periode). Baut ALLE Zeilen rein
        lesend im Speicher und schreibt sie DANN in EINER Transaktion
        (siehe `IndexMonatsberichtRepository.ersetze_zeilen_falls_bereit`).
        Liefert `None`, wenn die Kopfzeile dieser Periode bereits
        IN_VERSAND/GESENDET/UNKLAR ist - ein Monatsretry darf einen
        bereits eingefrorenen/versendeten Bericht NIE mehr verändern."""

        zeilen_felder: list[dict] = []
        zaehler = {"MOEGLICH": 0, "NOCH_NICHT_MOEGLICH": 0, "PRUEFUNG_NOETIG": 0}
        for lauf in laeufe:
            felder = self._zeile_felder_aus_lauf(lauf=lauf, periode=periode, heute=heute)
            zeilen_felder.append(felder)
            zaehler[felder["status"]] += 1
        zusammenfassung = {
            "anzahl_vertraege": len(laeufe), "anzahl_moeglich": zaehler["MOEGLICH"],
            "anzahl_noch_nicht_moeglich": zaehler["NOCH_NICHT_MOEGLICH"], "anzahl_pruefung_noetig": zaehler["PRUEFUNG_NOETIG"],
        }
        return self._repository.ersetze_zeilen_falls_bereit(periode, zeilen_felder, zusammenfassung=zusammenfassung)

    def _zeile_felder_aus_lauf(self, *, lauf: IndexautomatikLaufTable, periode: str, heute: date) -> dict:
        vertrag = self._stammdaten_repository.get_vertrag(lauf.vertrag_id)
        profil = self._rechtsprofil_repository.get(lauf.rechtsprofil_id) if lauf.rechtsprofil_id else None
        status, grund, vorschlag_cent, differenz_cent, termin, termin_status, erhoehungsschreiben_id = (
            self._status_aus_lauf(lauf, heute=heute, vertrag=vertrag, profil=profil)
        )

        qf = self._quellen_fakten_repository.neueste_fuer_vertrag(lauf.vertrag_id) if vertrag else None
        if status == "PRUEFUNG_NOETIG" and grund == _GENERISCHER_KEIN_PROFIL_GRUND and qf is not None:
            # Codex-Korrektur: `VertragTable.rechtsordnung` (MRG_VOLL/
            # MRG_TEIL) allein sagt NICHTS über Wohnungsnutzung aus - ein
            # MRG_TEIL-Objekt kann Büro/Geschäftsraum sein. NUR eine
            # EXPLIZIT verifizierte `qf.ist_wohnungsnutzung is False`
            # erlaubt den Gewerbe-Rechenvorschlag (fail-closed: `None`
            # (ungeklärt) sperrt genauso wie `True`).
            vorschau = self._gewerbe_rechenvorschlag(qf, heute=heute) if qf.ist_wohnungsnutzung is False else None
            if vorschau is not None:
                vorschlag_cent, differenz_cent, grund = vorschau
            elif qf.pruefhinweis:
                grund = qf.pruefhinweis
        if termin is None and qf is not None and qf.bedingter_naechster_monat is not None:
            termin = _naechster_gueltiger_kalendermonat(heute, qf.bedingter_naechster_monat)
            termin_status = "BEDINGT"

        gesamtvorschreibung_cent, gesamtvorschreibung_quelle, indexierbarer_mietanteil_cent, indexierbar_hinweis = (
            self._gesamtvorschreibung(lauf.vertrag_id, heute=heute, qf=qf)
        )
        snapshot = self._snapshot(vertrag, profil=profil, qf=qf)

        return {
            "vertrag_id": lauf.vertrag_id, "periode": periode, "status": status, "status_grund": grund,
            "vorschlag_cent": vorschlag_cent, "differenz_cent": differenz_cent, "fruehester_termin": termin,
            "fruehester_termin_status": termin_status, "gesamtvorschreibung_cent": gesamtvorschreibung_cent,
            "gesamtvorschreibung_quelle": gesamtvorschreibung_quelle,
            "indexierbarer_mietanteil_cent": indexierbarer_mietanteil_cent,
            "indexierbarer_mietanteil_hinweis": indexierbar_hinweis, "snapshot_json": snapshot,
            "rechtsprofil_id": lauf.rechtsprofil_id, "rechtsprofil_version": lauf.rechtsprofil_version,
            "quellen_fakten_id": qf.id if qf is not None else None, "indexautomatik_lauf_id": lauf.id,
            "erhoehungsschreiben_id": erhoehungsschreiben_id,
        }

    def _status_aus_lauf(
        self, lauf: IndexautomatikLaufTable, *, heute: date, vertrag, profil: RechtsprofilTable | None,
    ) -> tuple[str, str, int | None, int | None, date | None, str | None, int | None]:
        """Reine Abbildung des bereits real berechneten Lauf-Status auf
        die drei Berichts-Buckets - KEINE eigene Neuberechnung. Rückgabe:
        (status, grund, vorschlag_cent, differenz_cent, fruehester_termin,
        fruehester_termin_status, erhoehungsschreiben_id)."""

        # Codex-Korrektur: die gesetzliche 1.-April-Grenze gilt NUR für
        # tatsächlich bestätigte Wohnungsnutzung - NICHT pauschal für
        # jeden MRG_TEIL-Fall (der auch Geschäftsraum sein kann).
        bedingter_termin = None
        if (
            profil is not None and profil.rechtsordnung in _WOHNUNGSRECHNER_RECHTSORDNUNGEN
            and profil.ist_wohnungsnutzung is True
        ):
            bedingter_termin = _naechste_gesetzliche_april_grenze(heute)

        if lauf.status in ("ERHOEHUNG_ERZEUGT", "BEREITS_ERFASST"):
            schreiben = self._outbox_repository.get(lauf.erhoehungsschreiben_id) if lauf.erhoehungsschreiben_id else None
            if schreiben is None:
                return (
                    "PRUEFUNG_NOETIG",
                    "Erhöhungsschreiben-Referenz des Monatslaufs ist nicht (mehr) auflösbar - manuelle Prüfung nötig.",
                    None, None, None, None, None,
                )
            vorschlag_cent = self._neue_gesamtvorschreibung_aus_schreiben(schreiben, heute=heute)
            if lauf.status == "ERHOEHUNG_ERZEUGT":
                grund = (
                    f"Erhöhung möglich: Erhöhungsschreiben #{schreiben.id}, Status {schreiben.status}, "
                    f"maßgeblicher Termin {schreiben.massgeblicher_termin.isoformat()}."
                )
            else:
                grund = (
                    f"Bereits erfasst: Erhöhungsschreiben #{schreiben.id} (Status {schreiben.status}, Termin "
                    f"{schreiben.massgeblicher_termin.isoformat()}) ist bereits in Bearbeitung - keine erneute "
                    "Freigabe nötig, nur zur Nachverfolgung. Nicht doppelt umsetzen."
                )
            return (
                "MOEGLICH", grund, vorschlag_cent, schreiben.erhoehung_cent, schreiben.massgeblicher_termin,
                "GEPRUEFT", schreiben.id,
            )

        if lauf.status == "KEIN_ERHOEHUNGSBEDARF":
            return (
                "NOCH_NICHT_MOEGLICH",
                "Berechnete Veränderung ergibt aktuell keinen Erhöhungsbedarf (Vergleichswert unverändert oder niedriger).",
                None, None, bedingter_termin, "BEDINGT" if bedingter_termin else None, None,
            )

        if lauf.status == "TERMIN_NICHT_ERREICHT":
            grund = "; ".join(lauf.blockiert_gruende) or (
                "Nächster gesetzlicher/vertraglicher Anpassungstermin ist laut aktuellem Rechtsprofil noch nicht erreicht."
            )
            return ("NOCH_NICHT_MOEGLICH", grund, None, None, bedingter_termin, "BEDINGT" if bedingter_termin else None, None)

        if lauf.status == "SENKUNG_PRUEFBEDARF":
            grund = "; ".join(lauf.blockiert_gruende) or "Berechnete Veränderung ist eine Senkung - manuelle Prüfung erforderlich."
            return ("PRUEFUNG_NOETIG", grund, None, None, None, None, None)

        # BLOCKIERT (und jeder sonstige, unbekannte Status - sicherer Fehlschlag statt stillem Default).
        grund = "; ".join(lauf.blockiert_gruende) or "Monatslauf blockiert, kein konkreter Grund hinterlegt."
        return ("PRUEFUNG_NOETIG", grund, None, None, None, None, None)

    def _neue_gesamtvorschreibung_aus_schreiben(self, schreiben, *, heute: date) -> int | None:
        """`komponenten_verteilung` trägt eine LISTE unter 'eintraege'
        (`{"eintraege": [{"komponente_id","alter_betrag_cent",
        "neuer_betrag_cent"}, ...]}`, siehe `outbox_service.py`) - NICHT
        einen einzelnen Top-Level-Betrag. Die neue Gesamtvorschreibung
        (Vorschlag) ergibt sich aus der Summe ALLER aktuell aktiven
        Komponenten, wobei jede in `eintraege` geänderte Komponente ihren
        NEUEN Betrag beiträgt und jede unveränderte ihren aktuellen -
        exakt dieselbe Summe, die die reale Vorschreibung nach Umsetzung
        hätte."""

        eintraege = {e["komponente_id"]: e["neuer_betrag_cent"] for e in (schreiben.komponenten_verteilung or {}).get("eintraege", [])}
        if not eintraege:
            return None
        aktive_komponenten = self._stammdaten_repository.list_aktive_komponenten(schreiben.vertrag_id, heute)
        if not aktive_komponenten:
            return None
        return sum(eintraege.get(k.id, k.betrag_cent) for k in aktive_komponenten)

    def _gewerbe_rechenvorschlag(
        self, qf: IndexQuellenFaktenTable, *, heute: date,
    ) -> tuple[int | None, int, str] | None:
        """Read-only Vorschau AUSSCHLIESSLICH über bestehende
        `IndexService`-Bausteine - schreibt NICHTS. `None`, wenn die
        Quelle nicht vollständig belegt ist (dann bleibt der generische/
        pruefhinweis-Text unverändert die einzige Anreicherung).

        Rückgabe `(vorschlag_cent, differenz_cent, grund)`:
        `differenz_cent` ist IMMER die Veränderung des dokumentierten
        indexierten Anteils (`urspruenglicher_indexbetrag_cent`).
        `vorschlag_cent` (Codex-Korrektur: "indexierbarer Teil nicht mit
        neuer GESAMTvorschreibung verwechseln") ist NUR gesetzt, wenn
        zusätzlich eine bestätigte AKTUELLE Gesamtmiete
        (`bestaetigte_gesamtmiete_cent`) vorliegt - dann: neue Gesamtsumme
        = aktuelles Gesamt + Differenz. Ohne bestätigte Gesamtmiete bleibt
        `vorschlag_cent` `None` (nur der isolierte Indexanteil ist
        bekannt) statt eine Gesamtsumme zu unterstellen."""

        if qf is None or not qf.betrag_basisbindung_belegt:
            return None
        if not qf.urspruengliche_klauselbasis_reihe or qf.urspruengliche_klauselbasis_wert is None:
            return None
        if qf.urspruenglicher_indexbetrag_cent is None:
            return None
        if qf.schwelle_prozent is None or qf.schwelle_inklusive is None:
            return None
        aktueller_vpi = self._vpi_repository.neuester_endgueltiger_monatswert_mit_periode(
            qf.urspruengliche_klauselbasis_reihe, heute
        )
        if aktueller_vpi is None:
            return None

        alter_wert = Decimal(qf.urspruengliche_klauselbasis_wert)
        # Codex-Korrektur: Rohveränderung (reiner VPI-Quotient) und
        # wirksame (gedämpfte/geschwellte) Veränderung getrennt ausweisen
        # - bei unterschrittener Schwelle bleibt die WIRKSAME Änderung 0
        # (kein Betrag), die tatsächliche VPI-Rohveränderung bleibt aber
        # im Text sichtbar, statt als "0%" zu erscheinen.
        rohe_veraenderung = (aktueller_vpi.wert - alter_wert) / alter_wert * 100
        daempfung = Decimal(qf.daempfung_prozent) if qf.daempfung_prozent is not None else None
        grenze = Decimal(qf.vertragliche_grenze_prozent) if qf.vertragliche_grenze_prozent is not None else None
        effektive_veraenderung = IndexService.effektive_veraenderung_prozent(
            alter_wert=alter_wert, neuer_wert=aktueller_vpi.wert, daempfung_prozent=daempfung, vertragliche_grenze_prozent=grenze,
        )
        schwelle_prozent = Decimal(qf.schwelle_prozent)
        ueberschritten = IndexService.ueberschreitet_schwelle(
            effektive_veraenderung, schwelle_prozent=schwelle_prozent, schwelle_inklusive=qf.schwelle_inklusive,
        )
        betragswirksame_veraenderung = effektive_veraenderung if ueberschritten else Decimal("0")

        neuer_indexanteil_cent = to_cents(
            round_index_half_cent_down(
                cents_to_decimal(qf.urspruenglicher_indexbetrag_cent) * (1 + betragswirksame_veraenderung / 100)
            )
        )
        differenz_cent = neuer_indexanteil_cent - qf.urspruenglicher_indexbetrag_cent
        if qf.bestaetigte_gesamtmiete_cent is not None:
            vorschlag_cent = qf.bestaetigte_gesamtmiete_cent + differenz_cent
        else:
            vorschlag_cent = None

        schwelle_text = "Schwelle überschritten" if ueberschritten else "Schwelle NICHT überschritten"
        vorschlag_hinweis = (
            "" if vorschlag_cent is not None
            else " Keine bestätigte aktuelle Gesamtmiete hinterlegt - nur der isolierte Indexanteil ist berechenbar, "
            "keine neue Gesamtvorschreibung."
        )
        grund = (
            f"Rechenvorschlag (unverbindliche Vorschau aus Quellenfakten) – Ausführung noch nicht freigegeben: "
            f"VPI {qf.urspruengliche_klauselbasis_reihe} {aktueller_vpi.jahr}-{aktueller_vpi.monat:02d}="
            f"{aktueller_vpi.wert} gegen Basis {alter_wert} (Rohveränderung {rohe_veraenderung}%, wirksame "
            f"Veränderung {betragswirksame_veraenderung}%, {schwelle_text})." + vorschlag_hinweis + " "
            + (qf.pruefhinweis or "Weitere Nachweise/eine echte Vertragsklausel für die Ausführung sind erforderlich.")
        )
        return vorschlag_cent, differenz_cent, grund

    def _gesamtvorschreibung(
        self, vertrag_id: str, *, heute: date, qf: IndexQuellenFaktenTable | None,
    ) -> tuple[int | None, str | None, int | None, str | None]:
        komponenten = self._stammdaten_repository.list_aktive_komponenten(vertrag_id, heute)
        if komponenten:
            gesamt = sum(k.betrag_cent for k in komponenten)
            indexierbare = [k for k in komponenten if k.indexierbar]
            if indexierbare:
                return gesamt, "KOMPONENTEN", sum(k.betrag_cent for k in indexierbare), None
            # Codex-Korrektur: "15 Ist-Komponentensätze haben
            # indexierbar=false bis Freigabe; daraus nicht 'indexierbarer
            # Mietanteil 0 EUR' als materielle Aussage" - `None` statt
            # eines geratenen/materiellen Nullwerts, mit erklärendem
            # Hinweis statt stillem Verschwinden.
            return (
                gesamt, "KOMPONENTEN", None,
                "Keine der aktiven Komponenten ist aktuell als indexierbar markiert (Freigabe/Prüfung ausständig) - "
                "kein materieller 0-EUR-Anteil.",
            )
        # Codex-Beispiel Pietsch/IMG: bestätigter Gesamtbetrag OHNE jede
        # Komponente - NIE eine Nullmiete, aber auch NIE eine geratene
        # Aufteilung auf einen indexierbaren Anteil.
        if qf is not None and qf.bestaetigte_gesamtmiete_cent is not None:
            return (
                qf.bestaetigte_gesamtmiete_cent, "BESTAETIGT_OHNE_KOMPONENTEN", None,
                "Keine Vertragskomponenten hinterlegt - Aufteilung Miete/BK/USt unbekannt.",
            )
        return None, None, None, None

    def _letzte_tatsaechliche_indexierung(self, vertrag_id: str) -> dict | None:
        """Codex-Korrektur: "Zustellbestätigung allein ist KEINE bereits
        verrechnete Indexierung: letzte tatsächlich umgesetzte Anpassung
        aus erfolgreicher Sollumsetzung/Historie" - ein zugestelltes,
        aber (noch) nicht umgesetztes Schreiben (auch mit künftigem
        Termin) zählt NICHT als bereits enthalten. Einzige echte Quelle
        ist `IndexSollUmsetzungTable.status == 'UMGESETZT'`."""

        with self._session_factory() as session:
            statement = (
                select(IndexSollUmsetzungTable)
                .where(IndexSollUmsetzungTable.vertrag_id == vertrag_id)
                .where(IndexSollUmsetzungTable.status == "UMGESETZT")
                .order_by(IndexSollUmsetzungTable.wirksam_ab.desc())
            )
            neueste = session.execute(statement).scalars().first()
        if neueste is None or neueste.wirksam_ab is None:
            return None
        schreiben = self._outbox_repository.get(neueste.erhoehungsschreiben_id)
        return {
            "wirksam_ab": neueste.wirksam_ab.isoformat(),
            "erhoehung_cent": schreiben.erhoehung_cent if schreiben else None,
            "quelle": "SOLL_UMSETZUNG",
        }

    def _snapshot(self, vertrag, *, profil: RechtsprofilTable | None, qf: IndexQuellenFaktenTable | None) -> dict:
        objekt = self._stammdaten_repository.objekt_fuer_vertrag(vertrag.id) if vertrag else None
        einheit = self._stammdaten_repository.get_einheit(vertrag.einheit_id) if vertrag else None
        debitor = self._stammdaten_repository.get_debitor(vertrag.debitor_id) if vertrag else None

        # Codex-Korrektur: URSPRÜNGLICHE Vertragsbasis (Klauselwortlaut
        # bei Vertragsabschluss) und ZULETZT dokumentierte/angewandte
        # Basis (laut freigegebenem Rechtsprofil) sind ZWEI GETRENNTE
        # Dinge - eines überschreibt NIE das andere.
        urspruengliche_vertragsbasis = None
        if qf is not None and qf.urspruengliche_klauselbasis_reihe:
            urspruengliche_vertragsbasis = {
                "reihe": qf.urspruengliche_klauselbasis_reihe, "monat": qf.urspruengliche_klauselbasis_monat,
                "wert": qf.urspruengliche_klauselbasis_wert, "quelle": _QUELLE_OHNE_AUSFUEHRUNGSFREIGABE,
            }
        zuletzt_dokumentierte_basis = None
        if profil is not None and (profil.bezugsjahr is not None or profil.bezugsmonat is not None):
            zuletzt_dokumentierte_basis = {
                "reihe": profil.vpi_reihe,
                "monat": (
                    f"{profil.bezugsjahr:04d}-{profil.bezugsmonat:02d}"
                    if profil.bezugsjahr is not None and profil.bezugsmonat is not None else None
                ),
                "quelle": "RECHTSPROFIL_FREIGEGEBEN",
            }

        # Echte Ledger-Historie (tatsächlich UMGESETZTE Anpassung) hat
        # IMMER Vorrang vor der bloß dokumentierten Quellenfakten-Angabe.
        letzte_indexierung = self._letzte_tatsaechliche_indexierung(vertrag.id) if vertrag else None
        if letzte_indexierung is None and qf is not None and (
            qf.letzte_tatsaechliche_basis_jahr is not None or qf.letzte_tatsaechliche_basis_hinweis
        ):
            letzte_indexierung = {
                "jahr": qf.letzte_tatsaechliche_basis_jahr, "monat": qf.letzte_tatsaechliche_basis_monat,
                "hinweis": qf.letzte_tatsaechliche_basis_hinweis, "quelle": _QUELLE_OHNE_AUSFUEHRUNGSFREIGABE,
            }

        return {
            "mieter_name": debitor.name if debitor else None,
            "objekt_bezeichnung": objekt.bezeichnung if objekt else None,
            "einheit_bezeichnung": einheit.bezeichnung if einheit else None,
            "urspruengliche_vertragsbasis": urspruengliche_vertragsbasis,
            "zuletzt_dokumentierte_basis": zuletzt_dokumentierte_basis,
            "letzte_tatsaechliche_indexierung": letzte_indexierung,
            "quellenfakten_hinweise": (
                {
                    "klauselregel_text": qf.klauselregel_text,
                    "bereits_enthaltene_erhoehungen_hinweis": qf.bereits_enthaltene_erhoehungen_hinweis,
                    "bestaetigte_gesamtmiete_quelle": qf.bestaetigte_gesamtmiete_quelle,
                    "bestaetigte_gesamtmiete_stichtag": (
                        qf.bestaetigte_gesamtmiete_stichtag.isoformat() if qf.bestaetigte_gesamtmiete_stichtag else None
                    ),
                    "bedingter_fruehester_termin_hinweis": qf.bedingter_fruehester_termin_hinweis,
                    "quellenreferenzen": list(qf.quellenreferenzen or []),
                    "quelle_status": "Noch keine Ausführungsfreigabe",
                }
                if qf is not None else None
            ),
        }

    # -- VPI-Ausfall: sichtbarer Fehlerbericht statt stillem Jobabbruch -------
    def markiere_vpi_fehler(self, *, periode: str, fehlergrund: str) -> IndexMonatsberichtTable:
        """Aufrufer: `scripts/indexautomatik_monatslauf.py`, BEVOR
        `monatslauf_alle`/`erstellen_fuer_periode` überhaupt erreicht
        werden - Codex-Auftrag: "Bei fehlgeschlagenem VPI-Abruf muss ein
        sichtbarer Monats-Fehlerbericht/Owner-Hinweis entstehen, nicht nur
        Jobabbruch ohne Nachricht; keine stille Berechnung mit alten
        Werten." Der Monatslauf selbst bricht weiterhin sichtbar mit
        Fehler/Exitcode ab (kein stiller Erfolg) - dies schreibt
        ZUSÄTZLICH einen für Portal und Owner-Mail sichtbaren Datensatz."""

        return self._repository.markiere_vpi_fehler(periode, fehlergrund=fehlergrund)

    # -- Umfang B: Owner-Sammelmail ------------------------------------------
    def _text_fuer_bericht(self, periode: str, bericht: IndexMonatsberichtTable) -> str:
        if bericht.status == "VPI_FEHLER":
            return (
                f"Index-Monatsbericht {periode}: FEHLGESCHLAGEN - kein Bericht erzeugt.\n\n"
                f"Grund: {bericht.fehlergrund or 'unbekannt'}\n\n"
                "Es wurde bewusst NICHT mit veralteten/zuletzt erfolgreichen VPI-Werten weitergerechnet. "
                "Bitte den amtlichen VPI-Abruf/-Import prüfen und den Monatslauf danach erneut ausführen; "
                "erst dann wird für diese Periode ein vollständiger Bericht erzeugt."
            )
        zeilen = self._repository.zeilen_fuer_periode(periode)
        teile = [
            f"Index-Monatsbericht {periode}",
            f"{bericht.anzahl_vertraege} Verträge geprüft: {bericht.anzahl_moeglich} möglich, "
            f"{bericht.anzahl_noch_nicht_moeglich} noch nicht möglich, {bericht.anzahl_pruefung_noetig} Prüfung nötig.",
            "",
        ]
        for zeile in zeilen:
            snap = zeile.snapshot_json or {}
            mieter = snap.get("mieter_name") or zeile.vertrag_id
            objekt = snap.get("objekt_bezeichnung") or "-"
            betrag_text = (
                f"Vorschlag {zeile.vorschlag_cent / 100:,.2f} EUR (Differenz {zeile.differenz_cent / 100:,.2f} EUR)"
                .replace(",", "X").replace(".", ",").replace("X", ".")
                if zeile.vorschlag_cent is not None else "noch nicht berechenbar"
            )
            termin_text = (
                f"{zeile.fruehester_termin.isoformat()} ({zeile.fruehester_termin_status})"
                if zeile.fruehester_termin else "kein Termin ermittelbar"
            )
            teile.append(
                f"- {objekt} / {mieter} (Vertrag {zeile.vertrag_id}): {status_label(zeile.status)} - {betrag_text} - "
                f"Termin: {termin_text} - {zeile.status_grund}"
            )
        teile.append("")
        # Codex-Präzisierung: "Mail-Link absolut, nicht relativ" - eine Mail
        # wird außerhalb des Backoffice-Browserkontexts gelesen.
        teile.append(f"Vollständige Details im Backoffice unter {self._backoffice_basis_url}/backoffice/indexautomatik/monatsbericht/{periode}")
        return "\n".join(teile)

    def benachrichtige_faellige(
        self, *, heute: date, send_enabled: bool, send_ab_periode: str | None, versand_fn: Callable[[dict], None],
    ) -> list[IndexMonatsberichtTable]:
        """Analog `VertragsendeErinnerungService.benachrichtige_faellige`:
        ohne aktivierten Versand bleibt die Kopfzeile unverändert BEREIT
        (nächster Lauf prüft erneut), MIT aktiviertem Versand läuft ein
        echter atomarer Claim (BEREIT->IN_VERSAND) vor jedem Aufruf.
        `send_ab_periode`: siehe `IndexMonatsberichtRepository.
        liste_faellig` - eine Periode VOR der Aktivierungsperiode wird
        NIE automatisch versendet, bleibt aber im Portal sichtbar. Codex-
        Korrektur: `heute` wird jetzt tatsächlich zur aktuellen Periode
        verrechnet und als `bis_periode` an `liste_faellig` durchgereicht -
        eine (z. B. versehentlich vorab erzeugte) KÜNFTIGE Periode wird
        NIE automatisch versendet, unabhängig von `send_ab_periode`."""

        if not send_enabled or not (self._owner_email or "").strip():
            return []
        aktuelle_periode = f"{heute.year:04d}-{heute.month:02d}"
        benachrichtigt: list[IndexMonatsberichtTable] = []
        for bericht in self._repository.liste_faellig(ab_periode=send_ab_periode, bis_periode=aktuelle_periode):
            if not self._repository.claim_fuer_versand(bericht.id):
                continue
            text = self._text_fuer_bericht(bericht.periode, bericht)
            try:
                beleg = versand_fn({
                    "empfaenger": self._owner_email, "text": text, "periode": bericht.periode,
                    "idempotenzschluessel": f"index_monatsbericht:{bericht.periode}",
                })
            except TransportFehlerUngewissError as exc:
                self._repository.set_status(bericht.id, "UNKLAR", fehlergrund=str(exc))
                continue
            except ValueError:
                self._repository.set_status(
                    bericht.id, "UNKLAR", fehlergrund="Mailauftrag oder Mailkonfiguration unvollständig; Status prüfen."
                )
                continue
            if nachweis_daten(beleg) is None:
                self._repository.set_status(
                    bericht.id, "UNKLAR",
                    fehlergrund="Noch kein tatsächlicher Versandnachweis; nur Statusabfrage, kein erneuter Versand.",
                )
                continue
            versand_belegen(
                self._repository._session_factory, IndexMonatsberichtTable, bericht.id, ergebnis=beleg,
                erlaubt={"IN_VERSAND", "UNKLAR"}, neuer_status="GESENDET", zeitfeld="versendet_am",
                referenz=f"index_monatsbericht:{bericht.periode}", referenzfeld="externe_versandreferenz",
            )
            benachrichtigt.append(bericht)
        return benachrichtigt

    def markiere_verwaiste_als_unklar(self, *, jetzt: datetime | None = None, max_alter=None):
        from datetime import timedelta

        grenze = (jetzt or datetime.now(timezone.utc)) - (max_alter or timedelta(minutes=15))
        verwaiste = self._repository.verwaiste_in_versand(aelter_als=grenze)
        for row in verwaiste:
            self._repository.set_status(row.id, "UNKLAR", fehlergrund="Verwaist zwischen Claim und Versandergebnis (Absturz-Recovery).")
        return verwaiste
