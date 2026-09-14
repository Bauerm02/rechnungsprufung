"""Orchestriert Mahnkosten-Vorschau und -Buchung (Auftrag Markus
13.09.2026, ergänzt 14.09.2026) - siehe `kosten.py` für die reine
Berechnung und `kosten_repository.py` für die Persistenz.

`vorschau()` ist eine reine Lesefunktion (bucht nichts). `buche_vorschau()`
ist die EINZIGE Stelle, die tatsächlich Zinsen-/Gebühren-OP-Positionen
bucht - und zwar IMMER exakt die in der übergebenen `MahnkostenVorschau`
berechneten Werte, NIE eine intern neu berechnete, potenziell abweichende
Vorschau. Das garantiert, dass ein einmal in einen Mahntext geschriebener
Kosten-/Zinsnachweis exakt den danach gebuchten Zusatzpositionen
entspricht: `mahnwesen/service.py::MahnwesenService.versenden()` und die
Mahnlauf-Bündelung in `indexautomatik/mailversand_service.py` berechnen
die Vorschau GENAU EINMAL (unmittelbar vor dem tatsächlichen Versand,
damit auch kurz zuvor eingegangene Zahlungen berücksichtigt sind),
verwenden sie für den Brieftext UND übergeben DASSELBE Objekt an
`buche_vorschau`. `buche_bei_versand` bleibt als dünner
Kompatibilitäts-Wrapper (berechnet intern frisch) für einfache,
nicht gebündelte Aufrufer erhalten.

Eine bloße Vorschau, ein fehlgeschlagener Versand oder ein Retry ohne
neuen Nachweis bucht NICHTS (siehe `service.py`). Ein wiederholter Aufruf
am selben Tag oder eine Stufe 2, die dieselben, bei Stufe 1 bereits
fakturierten Zinstage beträfe, ist idempotent - nicht über eine
Existenzabfrage, sondern weil das vertragsweite Delta aus `vorschau()`
dann natürlich <= 0 ist. Die DB-Unique-Constraint auf
`(vertrag_id, stufe, zins_bis)` ist nur ein Race-Condition-
Sicherheitsnetz für zwei gleichzeitige Aufrufe; für die §458-Pauschale
ist `uq_mahnkosten_gebuehr_forderung` dagegen das FACHLICHE Gate selbst
(siehe `kosten.py`-Moduldoc)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from sqlalchemy.exc import IntegrityError

from mietinkasso.auth.service import AuthContext, require_gesellschaft_access
from mietinkasso.domain.enums import OPTyp
from mietinkasso.mahnwesen.kosten import MahnkostenVorschau, berechne_mahnkosten_vorschau
from mietinkasso.mahnwesen.kosten_repository import MahnkostenRepository
from mietinkasso.op.service import OPService, compute_content_hash
from mietinkasso.stammdaten.repository import StammdatenRepository


def _mahnlauf_schluessel(op_position_ids: list[int]) -> str:
    return compute_content_hash({"forderung_ids": sorted(op_position_ids)})


def _segmente_als_json(vorschau: MahnkostenVorschau) -> str:
    return json.dumps([
        {
            "op_position_id": s.op_position_id, "von": s.von.isoformat(), "bis": s.bis.isoformat(),
            "rest_cent": s.rest_cent, "satz_prozent": str(s.satz_prozent) if s.satz_prozent is not None else None,
            "quelle": s.quelle, "zinsen_cent": s.zinsen_cent,
        }
        for s in vorschau.zins_segmente
    ])


class MahnkostenService:
    def __init__(
        self, repository: MahnkostenRepository, op_service: OPService, stammdaten_repository: StammdatenRepository,
    ):
        self._repository = repository
        self._op_service = op_service
        self._stammdaten_repository = stammdaten_repository

    def vorschau(
        self, *, vertrag_id: str, stufe: int, heute: date,
        nur_op_position_ids: frozenset[int] | None = None,
    ) -> MahnkostenVorschau | None:
        """`nur_op_position_ids`: unabhängige Rückprüfung Codex
        14.09.2026 - die Kostenbasis (Hauptforderung/Zinsen/§458-Gebühr)
        MUSS an exakt dieselben, tatsächlich freigegebenen/versandbereiten
        Forderungs-Ids gebunden werden können, wenn ein Aufrufer (der
        gebündelte Mahnlauf-Versand, siehe `mahnwesen/service.py::
        MahnwesenService.versende_mahnlauf`) über eine EXAKTE,
        eingefrorene Gruppe verfügt - KEINE Gebühren/Zinsen auf
        zurückgestellte, strittige, SEPA-gebundene oder aktuell nicht
        gemahnte offene Posten desselben Vertrags. `None` (Default)
        erhält das bisherige Verhalten (alle offenen Forderungen des
        Vertrags) für einfache, nicht gebündelte Aufrufer bei."""

        konto = self._stammdaten_repository.get_konto_by_vertrag(vertrag_id)
        if konto is None:
            return None
        forderungen = self._op_service.offene_forderungen(konto.id, heute=heute)
        if nur_op_position_ids is not None:
            forderungen = [f for f in forderungen if f.op_position_id in nur_op_position_ids]
        alle_positionen = self._op_service.berechne_saldo(konto.id, stichtag=heute).positionen
        zinsprofil_historie = self._repository.historie_geprueft(vertrag_id)
        # Vertragsweit (NICHT je Stufe) - Stufe 2 rechnet dieselben, bei
        # Stufe 1 bereits fakturierten Zinstage nie erneut ab; eine bereits
        # erhobene §458-Pauschale wird nie ein zweites Mal für dieselbe
        # Entgeltforderung angesetzt (siehe `kosten_repository.py`). Die
        # bereits gebuchten Zinsen werden JE FORDERUNG nachgeschlagen
        # (nicht als vertragsweite Blanko-Summe - Rückprüfung Codex
        # 14.09.2026, siehe `kosten.py::berechne_mahnkosten_vorschau`).
        bereits_gebuchte_zinsen_je_op = self._repository.bereits_gebuchte_zinsen_je_op_position(vertrag_id=vertrag_id)
        bereits_erhobene_gebuehren = self._repository.bereits_erhobene_gebuehr_schluessel(vertrag_id=vertrag_id)
        return berechne_mahnkosten_vorschau(
            vertrag_id=vertrag_id, stufe=stufe, forderungen=forderungen, alle_positionen=alle_positionen,
            heute=heute, zinsprofil_historie=zinsprofil_historie, basiszinssatz_lookup=self._repository.basiszinssatz_fuer_datum,
            naechster_basiszinssatz_lookup=self._repository.naechster_basiszinssatz_ab,
            bereits_gebuchte_zinsen_je_op_position=bereits_gebuchte_zinsen_je_op,
            bereits_erhobene_gebuehr_schluessel=bereits_erhobene_gebuehren,
        )

    def buche_bei_versand(
        self, *, ctx: AuthContext, vertrag_id: str, stufe: int, heute: date,
        versandnachweis_referenz: str, akteur: str,
    ):
        """Dünner Kompatibilitäts-Wrapper für einfache, NICHT gebündelte
        Aufrufer: berechnet die Vorschau intern frisch und bucht sie
        sofort. Ein gebündelter Mahnlauf (mehrere Forderungen/MahnFälle
        in EINEM Brief) MUSS stattdessen `buche_vorschau` mit der
        BEREITS für den Brieftext verwendeten Vorschau aufrufen, damit
        Text und Buchung garantiert exakt übereinstimmen (siehe
        Moduldoc)."""

        vertrag = self._stammdaten_repository.get_vertrag(vertrag_id)
        konto = self._stammdaten_repository.get_konto_by_vertrag(vertrag_id)
        if vertrag is None or konto is None:
            return None
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)

        vorschau = self.vorschau(vertrag_id=vertrag_id, stufe=stufe, heute=heute)
        if vorschau is None or not vorschau.forderung_op_position_ids:
            return None
        return self.buche_vorschau(ctx=ctx, vorschau=vorschau, heute=heute, versandnachweis_referenz=versandnachweis_referenz, akteur=akteur)

    def buche_vorschau(
        self, *, ctx: AuthContext, vorschau: MahnkostenVorschau, heute: date,
        versandnachweis_referenz: str, akteur: str,
    ):
        """Bucht EXAKT die in `vorschau` berechneten Werte - ruft NIE
        selbst erneut `berechne_mahnkosten_vorschau` auf. Bucht NUR das
        Delta (neue Zinsen seit der zuletzt für DIESEN VERTRAG - über
        alle Stufen hinweg - gebuchten Zinssumme, plus alle in `vorschau.
        gebuehr_segmente` enthaltenen, noch nicht erhobenen §458-
        Pauschalen) - NIE die volle `neue_zinsen_cent`-Summe erneut.
        Gibt `None` zurück, wenn nichts zu buchen ist."""

        vertrag_id = vorschau.vertrag_id
        vertrag = self._stammdaten_repository.get_vertrag(vertrag_id)
        konto = self._stammdaten_repository.get_konto_by_vertrag(vertrag_id)
        if vertrag is None or konto is None or not vorschau.forderung_op_position_ids:
            return None
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)

        # Primäre Idempotenz: NICHT über eine "existiert bereits für
        # diesen Schlüssel"-Abfrage, sondern über das JE FORDERUNG
        # gebildete Delta aus `vorschau` (`neue_zinsen_delta_cent`, siehe
        # `kosten.py`-Moduldoc) - das ist bei einem Wiederholaufruf am
        # selben Tag oder bei Stufe 2 (dieselben, bei Stufe 1 bereits
        # fakturierten Zinstage) natürlich <= 0/leer.
        neu_anzusetzende_zinsen = vorschau.neue_zinsen_delta_cent
        gebuehr_betrag_gesamt = sum(s.betrag_cent for s in vorschau.gebuehr_segmente)
        if neu_anzusetzende_zinsen <= 0 and gebuehr_betrag_gesamt <= 0:
            return None  # nichts Neues zu buchen (u.a. der Idempotenz-Fall)

        stufe = vorschau.stufe
        mahnlauf_schluessel = _mahnlauf_schluessel(list(vorschau.forderung_op_position_ids))
        zins_bis_fuer_stichtag = vorschau.zins_bis or heute

        session_factory = self._repository._session_factory
        try:
            with session_factory() as session:
                zinsen_position_id = None
                if neu_anzusetzende_zinsen > 0:
                    satz_text = f"{vorschau.zinssatz_prozent}% p.a." if vorschau.zinssatz_prozent is not None else "siehe Zinssegmente (Halbjahreswechsel)"
                    zinsen_position = self._op_service.buchen(
                        ctx=ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=neu_anzusetzende_zinsen,
                        belegdatum=heute, buchungsdatum=heute, faelligkeit=None,
                        beleg_referenz=f"Verzugszinsen {satz_text} ({vorschau.zinsbasis}), "
                                       f"{vorschau.zins_von}–{vorschau.zins_bis}",
                        aenderungsgrund="Mahnkosten - Verzugszinsen",
                        import_id=f"MAHNKOSTEN-ZINSEN:{vertrag_id}:{stufe}:{mahnlauf_schluessel}",
                        quelle_system="mahnkosten", session=session,
                    )
                    zinsen_position_id = zinsen_position.id

                gebuehr_position_id = None
                if gebuehr_betrag_gesamt > 0:
                    gebuehr_position = self._op_service.buchen(
                        ctx=ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=gebuehr_betrag_gesamt,
                        belegdatum=heute, buchungsdatum=heute, faelligkeit=None,
                        beleg_referenz=f"Mahnspesen §458 UGB ({len(vorschau.gebuehr_segmente)} Entgeltforderung(en))",
                        aenderungsgrund="Mahnkosten - Mahnspesen",
                        import_id=f"MAHNKOSTEN-GEBUEHR:{vertrag_id}:{stufe}:{mahnlauf_schluessel}",
                        quelle_system="mahnkosten", session=session,
                    )
                    gebuehr_position_id = gebuehr_position.id

                ledger = self._repository.buchung_anlegen(
                    vertrag_id=vertrag_id, stufe=stufe, mahnlauf_schluessel=mahnlauf_schluessel,
                    forderung_op_position_ids=list(vorschau.forderung_op_position_ids),
                    hauptforderung_cent=vorschau.hauptforderung_cent, zinsbasis=vorschau.zinsbasis,
                    zinssatz_prozent=vorschau.zinssatz_prozent or Decimal(0), zins_von=vorschau.zins_von or heute,
                    zins_bis=zins_bis_fuer_stichtag, zinsen_cent=neu_anzusetzende_zinsen,
                    gebuehr_cent=(gebuehr_betrag_gesamt or None), rechtsgrundlage_gebuehr=vorschau.gebuehr_rechtsgrundlage,
                    versandnachweis_referenz=versandnachweis_referenz, zinsen_op_position_id=zinsen_position_id,
                    gebuehr_op_position_id=gebuehr_position_id, zins_segmente_json=_segmente_als_json(vorschau),
                    zinsen_delta_je_op_json=json.dumps(vorschau.neue_zinsen_delta_je_op_position),
                    erstellt_von=akteur, session=session,
                )

                # Jede §458-Pauschale wird EINZELN, dauerhaft je zugrunde
                # liegender Entgeltforderung erhoben - die Unique-
                # Constraint ist hier das fachliche Gate: wenn ein
                # gleichzeitiger anderer Vorgang dieselbe Entgeltforderung
                # bereits bepauschalt hat, scheitert dieser INSERT und die
                # gesamte Buchung wird zurückgerollt (Alles-oder-nichts).
                for segment in vorschau.gebuehr_segmente:
                    self._repository.gebuehr_erheben(
                        vertrag_id=vertrag_id, entgeltforderung_schluessel=segment.entgeltforderung_schluessel,
                        betrag_cent=segment.betrag_cent, rechtsgrundlage=segment.rechtsgrundlage,
                        mahnkosten_buchung_id=ledger.id, gebuehr_op_position_id=gebuehr_position_id,
                        erstellt_von=akteur, session=session,
                    )

                session.commit()
                return ledger
        except IntegrityError:
            # Ein anderer, gleichzeitiger Aufruf hat entweder exakt diesen
            # (vertrag_id, stufe, zins_bis)-Mahnlauf (Race-Sicherheitsnetz)
            # oder dieselbe Entgeltforderungs-Pauschale (fachliches Gate,
            # siehe oben) zwischenzeitlich bereits gebucht - kein Fehler,
            # sondern ein Idempotenz-/Konsistenzfall.
            return self._repository.buchung_fuer_stichtag(vertrag_id=vertrag_id, stufe=stufe, zins_bis=zins_bis_fuer_stichtag)
