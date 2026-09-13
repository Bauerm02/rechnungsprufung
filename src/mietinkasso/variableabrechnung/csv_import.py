"""Atomarer, idempotenter CSV-Import für monatliche variable
Nutzungsentgelt-Meldungen (KURZZEITVERMIETUNG/SELFSTORAGE, Auftrag
13.09., HV-20260913-DASHBOARD) - Vorschau (`erstelle_plan`, rein
lesend) und bewusste Übernahme (`wende_an`, bindet sich an den
Plan-Hash) GETRENNT, analog zu `intake/planner.py`/`intake/apply.py`
und `op/eroeffnung_import.py`.

Eine Zeile, die eine bestehende AKTUELLE Version mit ABWEICHENDEM
Inhalt vorfindet, ist eine KORREKTUR und wird NUR übernommen, wenn der
Aufrufer das explizit bestätigt (`korrekturen_bestaetigt=True`) UND die
Zeile einen `aenderungsgrund` trägt - "Korrigierte Quelle nur explizit
als neue Version". Die GESAMTE Datei läuft in EINER Transaktion; jede
GESPERRTE/KONFLIKTbehaftete Zeile blockiert den gesamten Import, keine
Teilübernahme. `pruefe_paket` wird identisch von `erstelle_plan`
(Lesesession) UND `wende_an` (unmittelbar vor dem Schreiben, auf
derselben Session wie die Schreibung) benutzt - kein separater
Prüfpfad, der von der tatsächlichen Schreiblogik abweichen könnte."""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from mietinkasso.auth.service import AuthContext, require_gesellschaft_access, require_schreibrecht
from mietinkasso.domain.exceptions import MietinkassoError
from mietinkasso.domain.money import to_cents
from mietinkasso.infrastructure.db.tables import EinheitTable, ObjektTable
from mietinkasso.variableabrechnung.repository import VariableAbrechnungRepository
from mietinkasso.variableabrechnung.service import VariableAbrechnungService, inhalts_felder, validiere_felder


class VariableAbrechnungImportNichtAnwendbarError(MietinkassoError):
    """`wende_an` wurde mit einem Paket aufgerufen, das GESPERRTE/
    KONFLIKT-Zeilen enthält, ODER KORREKTUR-Zeilen ohne die explizite
    `korrekturen_bestaetigt=True`-Bestätigung. Nichts wird geschrieben."""


@dataclass(frozen=True)
class VariableAbrechnungCsvZeile:
    zeilennummer: int
    einheit_id: str
    art: str
    leistungsmonat: str
    belegdatum: date
    quelle_referenz: str
    quelle_hash: str | None
    status: str
    berichteter_betrag_cent: int | None
    berichteter_betragsart: str | None
    unser_netto_anteil_cent: int | None
    betriebskosten_hinweis_cent: int | None
    reinigungskosten_hinweis_cent: int | None
    verwaltungskosten_hinweis_cent: int | None
    tatsaechlicher_zahlungseingang_cent: int | None
    vermietete_einheiten: int | None
    vermietete_flaeche_qm: str | None
    aenderungsgrund: str | None
    import_id: str

    @property
    def vermietete_flaeche_qm_decimal(self) -> Decimal | None:
        return None if self.vermietete_flaeche_qm is None else Decimal(self.vermietete_flaeche_qm)


@dataclass(frozen=True)
class ZeilenBefund:
    zeilennummer: int
    einheit_id: str
    art: str
    leistungsmonat: str
    status: str  # NEU | UNVERAENDERT | KORREKTUR | GESPERRT | KONFLIKT
    grund: str | None = None
    aktuelle_version_id: int | None = None


@dataclass(frozen=True)
class VariableAbrechnungPlan:
    plan_hash: str
    befunde: tuple[ZeilenBefund, ...]

    @property
    def neu(self) -> list[ZeilenBefund]:
        return [b for b in self.befunde if b.status == "NEU"]

    @property
    def unveraendert(self) -> list[ZeilenBefund]:
        return [b for b in self.befunde if b.status == "UNVERAENDERT"]

    @property
    def korrektur(self) -> list[ZeilenBefund]:
        return [b for b in self.befunde if b.status == "KORREKTUR"]

    @property
    def gesperrt(self) -> list[ZeilenBefund]:
        return [b for b in self.befunde if b.status == "GESPERRT"]

    @property
    def konflikte(self) -> list[ZeilenBefund]:
        return [b for b in self.befunde if b.status == "KONFLIKT"]

    def anwendbar(self, *, korrekturen_bestaetigt: bool) -> bool:
        if self.konflikte or self.gesperrt:
            return False
        if self.korrektur and not korrekturen_bestaetigt:
            return False
        return True


def _parse_optional_betrag(roh: str) -> int | None:
    roh = (roh or "").strip()
    if not roh:
        return None
    return to_cents(roh.replace(",", "."))


def _parse_optional_int(roh: str) -> int | None:
    roh = (roh or "").strip()
    return int(roh) if roh else None


def _parse_date(roh: str) -> date:
    return datetime.strptime(roh.strip(), "%Y-%m-%d").date()


def parse_csv(text: str) -> list[VariableAbrechnungCsvZeile]:
    zeilen: list[VariableAbrechnungCsvZeile] = []
    reader = csv.DictReader(io.StringIO(text))
    for nummer, roh in enumerate(reader, start=1):
        import_id = (roh.get("import_id") or "").strip()
        if not import_id:
            fingerprint = hashlib.sha256(
                "|".join(
                    [
                        roh.get("einheit_id", ""), roh.get("art", ""), roh.get("leistungsmonat", ""),
                        roh.get("status", ""), roh.get("belegdatum", ""), roh.get("quelle_referenz", ""),
                        roh.get("berichteter_betrag", ""), roh.get("berichteter_betragsart", ""),
                        roh.get("unser_netto_anteil", ""), roh.get("tatsaechlicher_zahlungseingang", ""),
                    ]
                ).encode("utf-8")
            ).hexdigest()[:16]
            import_id = f"VARIABLE-ABRECHNUNG-CSV:{roh.get('einheit_id','')}:{roh.get('leistungsmonat','')}:{fingerprint}"
        zeilen.append(
            VariableAbrechnungCsvZeile(
                zeilennummer=nummer,
                einheit_id=roh["einheit_id"].strip(),
                art=roh["art"].strip().upper(),
                leistungsmonat=roh["leistungsmonat"].strip(),
                belegdatum=_parse_date(roh["belegdatum"]),
                quelle_referenz=roh.get("quelle_referenz", "").strip(),
                quelle_hash=roh.get("quelle_hash", "").strip() or None,
                status=roh.get("status", "").strip().upper() or "ENTWURF",
                berichteter_betrag_cent=_parse_optional_betrag(roh.get("berichteter_betrag", "")),
                berichteter_betragsart=roh.get("berichteter_betragsart", "").strip().upper() or None,
                unser_netto_anteil_cent=_parse_optional_betrag(roh.get("unser_netto_anteil", "")),
                betriebskosten_hinweis_cent=_parse_optional_betrag(roh.get("betriebskosten_hinweis", "")),
                reinigungskosten_hinweis_cent=_parse_optional_betrag(roh.get("reinigungskosten_hinweis", "")),
                verwaltungskosten_hinweis_cent=_parse_optional_betrag(roh.get("verwaltungskosten_hinweis", "")),
                tatsaechlicher_zahlungseingang_cent=_parse_optional_betrag(roh.get("tatsaechlicher_zahlungseingang", "")),
                vermietete_einheiten=_parse_optional_int(roh.get("vermietete_einheiten", "")),
                vermietete_flaeche_qm=(roh.get("vermietete_flaeche_qm", "").strip().replace(",", ".") or None),
                aenderungsgrund=roh.get("aenderungsgrund", "").strip() or None,
                import_id=import_id,
            )
        )
    return zeilen


def plan_hash(zeilen: list[VariableAbrechnungCsvZeile], befunde: tuple[ZeilenBefund, ...]) -> str:
    """Bindet den Hash NICHT nur an den Dateiinhalt, sondern zusätzlich
    an die je Zeile GESEHENE aktuelle Version (`ZeilenBefund.
    aktuelle_version_id`, `None` für eine neue Gruppe) - unabhängiger
    Review: "Planhash nur Dateidaten genügt nicht. Gesehene
    Versions-IDs/Zustandsfingerprint an Vorschau binden". Ändert sich
    zwischen Planung und Übernahme die aktuelle Version irgendeiner
    betroffenen Zeile (z. B. durch eine zwischenzeitliche manuelle
    Korrektur), weicht der bei der erneuten Prüfung unmittelbar vor dem
    Schreiben (`wende_an`) frisch berechnete Hash vom vom Aufrufer
    bestätigten Hash ab - der gesamte Import wird dann abgelehnt, STATT
    die Korrektur auf die inzwischen andere aktuelle Version
    umzubasieren und diese damit stillschweigend zu überschreiben."""

    befund_je_zeile = {b.zeilennummer: b for b in befunde}
    kanonisch = "\n".join(
        "|".join(
            str(wert)
            for wert in (
                z.einheit_id, z.art, z.leistungsmonat, z.belegdatum, z.quelle_referenz, z.quelle_hash, z.status,
                z.berichteter_betrag_cent, z.berichteter_betragsart, z.unser_netto_anteil_cent,
                z.betriebskosten_hinweis_cent, z.reinigungskosten_hinweis_cent, z.verwaltungskosten_hinweis_cent,
                z.tatsaechlicher_zahlungseingang_cent, z.vermietete_einheiten, z.vermietete_flaeche_qm,
                z.aenderungsgrund, z.import_id,
                f"gesehen={befund_je_zeile[z.zeilennummer].aktuelle_version_id}",
            )
        )
        for z in zeilen
    )
    return hashlib.sha256(kanonisch.encode("utf-8")).hexdigest()


def _pruefe_paket(
    zeilen: list[VariableAbrechnungCsvZeile], *, ctx: AuthContext, session: Session, repository: VariableAbrechnungRepository
) -> tuple[ZeilenBefund, ...]:
    befunde: list[ZeilenBefund] = []
    gesehene_gruppen: set[tuple[str, str, str]] = set()
    for z in zeilen:
        gruppe = (z.einheit_id, z.art, z.leistungsmonat)
        if gruppe in gesehene_gruppen:
            befunde.append(
                ZeilenBefund(
                    z.zeilennummer, z.einheit_id, z.art, z.leistungsmonat, "KONFLIKT",
                    f"Gruppe (Einheit '{z.einheit_id}', Art '{z.art}', Monat '{z.leistungsmonat}') kommt "
                    "mehrfach im selben Paket vor - eindeutig je Zeile ist Pflicht.",
                )
            )
            continue
        gesehene_gruppen.add(gruppe)
        befunde.append(_pruefe_einzelzeile(z, ctx=ctx, session=session, repository=repository))
    return tuple(befunde)


def _pruefe_einzelzeile(
    z: VariableAbrechnungCsvZeile, *, ctx: AuthContext, session: Session, repository: VariableAbrechnungRepository
) -> ZeilenBefund:
    einheit = session.get(EinheitTable, z.einheit_id)
    if einheit is None:
        return ZeilenBefund(
            z.zeilennummer, z.einheit_id, z.art, z.leistungsmonat, "KONFLIKT", f"Unbekannte Einheit '{z.einheit_id}'."
        )
    objekt = session.get(ObjektTable, einheit.objekt_id)
    if objekt is None or objekt.ausgeschlossen:
        return ZeilenBefund(
            z.zeilennummer, z.einheit_id, z.art, z.leistungsmonat, "GESPERRT",
            f"Einheit '{z.einheit_id}' gehört zu einem ausgeschlossenen/unbekannten Objekt.",
        )
    # Unabhängiger Review: "Neue Lese-/Planservices und Routen sind ohne
    # ctx/Scopeprüfung (... Vorschau ...)" - die Planungsprüfung (aus
    # `erstelle_plan` UND aus `wende_an` unmittelbar vor dem Schreiben
    # aufgerufen) liest sonst JEDE Einheit/JEDES Objekt über alle
    # Gesellschaften hinweg unrestringiert. Ein scope-gebundener oder
    # LESEZUGRIFF-Ctx ohne Zugriff auf die Gesellschaft der Zeile erhält
    # daher schon in der Vorschau NUR ein generisches GESPERRT statt
    # Fachdaten der fremden Zeile (z. B. ob dort ohnehin schon eine
    # aktuelle Version existiert).
    if not ctx.has_zugriff(objekt.gesellschaft_id):
        return ZeilenBefund(
            z.zeilennummer, z.einheit_id, z.art, z.leistungsmonat, "GESPERRT",
            f"Kein Zugriff auf die Gesellschaft der Einheit '{z.einheit_id}'.",
        )

    try:
        validiere_felder(
            art=z.art, leistungsmonat=z.leistungsmonat, status=z.status,
            berichteter_betrag_cent=z.berichteter_betrag_cent, berichteter_betragsart=z.berichteter_betragsart,
            unser_netto_anteil_cent=z.unser_netto_anteil_cent,
            betriebskosten_hinweis_cent=z.betriebskosten_hinweis_cent,
            reinigungskosten_hinweis_cent=z.reinigungskosten_hinweis_cent,
            verwaltungskosten_hinweis_cent=z.verwaltungskosten_hinweis_cent,
            tatsaechlicher_zahlungseingang_cent=z.tatsaechlicher_zahlungseingang_cent,
            vermietete_einheiten=z.vermietete_einheiten, vermietete_flaeche_qm=z.vermietete_flaeche_qm_decimal,
            quelle_referenz=z.quelle_referenz,
        )
    except ValueError as exc:
        return ZeilenBefund(z.zeilennummer, z.einheit_id, z.art, z.leistungsmonat, "KONFLIKT", str(exc))

    aktuelle = repository.aktuelle_version(z.einheit_id, z.art, z.leistungsmonat, session=session)
    if aktuelle is None:
        return ZeilenBefund(z.zeilennummer, z.einheit_id, z.art, z.leistungsmonat, "NEU")

    neue_felder = {
        "einheit_id": z.einheit_id, "art": z.art, "leistungsmonat": z.leistungsmonat, "status": z.status,
        "belegdatum": str(z.belegdatum), "quelle_referenz": z.quelle_referenz, "quelle_hash": z.quelle_hash,
        "berichteter_betrag_cent": z.berichteter_betrag_cent, "berichteter_betragsart": z.berichteter_betragsart,
        "unser_netto_anteil_cent": z.unser_netto_anteil_cent,
        "betriebskosten_hinweis_cent": z.betriebskosten_hinweis_cent,
        "reinigungskosten_hinweis_cent": z.reinigungskosten_hinweis_cent,
        "verwaltungskosten_hinweis_cent": z.verwaltungskosten_hinweis_cent,
        "tatsaechlicher_zahlungseingang_cent": z.tatsaechlicher_zahlungseingang_cent,
        "vermietete_einheiten": z.vermietete_einheiten, "vermietete_flaeche_qm": z.vermietete_flaeche_qm_decimal,
    }
    if inhalts_felder(aktuelle) == neue_felder:
        return ZeilenBefund(z.zeilennummer, z.einheit_id, z.art, z.leistungsmonat, "UNVERAENDERT", aktuelle_version_id=aktuelle.id)

    if not (z.aenderungsgrund or "").strip():
        return ZeilenBefund(
            z.zeilennummer, z.einheit_id, z.art, z.leistungsmonat, "KONFLIKT",
            f"Weicht von der aktuellen Version #{aktuelle.id} ab, aber 'aenderungsgrund' fehlt in der "
            "Zeile - eine Korrektur ohne Änderungsgrund wird abgelehnt.",
        )
    return ZeilenBefund(
        z.zeilennummer, z.einheit_id, z.art, z.leistungsmonat, "KORREKTUR",
        f"Weicht von der aktuellen Version #{aktuelle.id} ab.", aktuelle_version_id=aktuelle.id,
    )


def erstelle_plan(
    zeilen: list[VariableAbrechnungCsvZeile], *, ctx: AuthContext, repository: VariableAbrechnungRepository
) -> VariableAbrechnungPlan:
    """Öffentlicher Dry-run-Einstieg: öffnet eine reine Lesesession (es
    wird nie geschrieben/committet). `ctx` wird bereits hier durchgereicht,
    damit die Vorschau selbst keine Fachdaten fremder Gesellschaften
    offenlegt (siehe `_pruefe_einzelzeile`)."""

    with repository.session_factory() as session:
        befunde = _pruefe_paket(zeilen, ctx=ctx, session=session, repository=repository)
    return VariableAbrechnungPlan(plan_hash=plan_hash(zeilen, befunde), befunde=befunde)


@dataclass(frozen=True)
class VariableAbrechnungImportErgebnis:
    plan_hash: str
    anzahl_neu: int
    anzahl_unveraendert: int
    anzahl_korrektur: int


def wende_an(
    zeilen: list[VariableAbrechnungCsvZeile],
    *,
    ctx: AuthContext,
    bestaetigter_hash: str,
    korrekturen_bestaetigt: bool,
    service: VariableAbrechnungService,
    repository: VariableAbrechnungRepository,
    akteur: str,
) -> VariableAbrechnungImportErgebnis:
    with repository.session_factory() as session:
        try:
            # Der Plan-Hash wird ERST nach einer FRISCHEN Prüfung
            # (`_pruefe_paket`, dieselbe Session wie die spätere
            # Schreibung) berechnet und dann GEGEN den vom Aufrufer
            # bestätigten Hash geprüft - so erkennt der Vergleich nicht
            # nur eine geänderte Datei, sondern auch eine zwischen
            # Planung und Übernahme veränderte AKTUELLE Version
            # irgendeiner betroffenen Zeile (siehe `plan_hash`-Docstring).
            befunde = _pruefe_paket(zeilen, ctx=ctx, session=session, repository=repository)
            aktueller_hash = plan_hash(zeilen, befunde)
            if aktueller_hash != bestaetigter_hash:
                raise ValueError(
                    f"Bestätigter Hash ({bestaetigter_hash}) stimmt nicht mehr mit dem aktuellen Stand "
                    f"({aktueller_hash}) überein - entweder hat sich die Datei geändert ODER es gab "
                    "zwischenzeitlich eine andere Änderung an mindestens einer betroffenen Zeile. Nichts "
                    "wurde eingespielt; bitte erneut planen und den NEUEN Stand bestätigen."
                )
            plan = VariableAbrechnungPlan(plan_hash=aktueller_hash, befunde=befunde)
            if not plan.anwendbar(korrekturen_bestaetigt=korrekturen_bestaetigt):
                relevante = [
                    *plan.konflikte, *plan.gesperrt,
                    *([] if korrekturen_bestaetigt else plan.korrektur),
                ]
                gruende = "; ".join(
                    f"Zeile {b.zeilennummer} ({b.einheit_id}/{b.art}/{b.leistungsmonat}): {b.status} - {b.grund}"
                    for b in relevante
                )
                raise VariableAbrechnungImportNichtAnwendbarError(
                    f"Paket ist nicht anwendbar, nichts wurde eingespielt: {gruende}"
                )

            befund_je_zeile = {b.zeilennummer: b for b in plan.befunde}
            anzahl_neu = anzahl_unveraendert = anzahl_korrektur = 0
            for z in zeilen:
                befund = befund_je_zeile[z.zeilennummer]
                # Auth-/Scope-Prüfung für JEDE Zeile, AUCH UNVERAENDERT -
                # unabhängiger Review: "Ein UNVERAENDERT-CSV-Zweig umgeht
                # aktuell auch Schreib-/Scopeprüfung in wende_an." Bis
                # hierher ist jede verbleibende Zeile NEU/UNVERAENDERT/
                # KORREKTUR (KONFLIKT/GESPERRT wurden oben bereits
                # abgefangen), Einheit/Objekt existieren also sicher.
                einheit = session.get(EinheitTable, z.einheit_id)
                objekt = session.get(ObjektTable, einheit.objekt_id)
                require_gesellschaft_access(ctx, objekt.gesellschaft_id)
                require_schreibrecht(ctx)
                if befund.status == "UNVERAENDERT":
                    anzahl_unveraendert += 1
                    continue
                gemeinsame_felder = dict(
                    belegdatum=z.belegdatum, quelle_referenz=z.quelle_referenz, status=z.status,
                    quelle_hash=z.quelle_hash, berichteter_betrag_cent=z.berichteter_betrag_cent,
                    berichteter_betragsart=z.berichteter_betragsart,
                    unser_netto_anteil_cent=z.unser_netto_anteil_cent,
                    betriebskosten_hinweis_cent=z.betriebskosten_hinweis_cent,
                    reinigungskosten_hinweis_cent=z.reinigungskosten_hinweis_cent,
                    verwaltungskosten_hinweis_cent=z.verwaltungskosten_hinweis_cent,
                    tatsaechlicher_zahlungseingang_cent=z.tatsaechlicher_zahlungseingang_cent,
                    vermietete_einheiten=z.vermietete_einheiten, vermietete_flaeche_qm=z.vermietete_flaeche_qm_decimal,
                    erstellt_von=akteur, quelle_system="CSV_IMPORT", import_id=z.import_id, session=session,
                )
                if befund.status == "NEU":
                    service.erfassen(ctx=ctx, einheit_id=z.einheit_id, art=z.art, leistungsmonat=z.leistungsmonat, **gemeinsame_felder)
                    anzahl_neu += 1
                elif befund.status == "KORREKTUR":
                    service.korrigieren(
                        ctx=ctx, ausgehend_von_id=befund.aktuelle_version_id, aenderungsgrund=z.aenderungsgrund,
                        **gemeinsame_felder,
                    )
                    anzahl_korrektur += 1
            session.commit()
        except Exception as exc:
            session.rollback()
            exc.args = (f"Variable-Abrechnung-CSV-Import abgebrochen: {exc}. Nichts wurde eingespielt.",)
            raise

    return VariableAbrechnungImportErgebnis(
        plan_hash=aktueller_hash, anzahl_neu=anzahl_neu, anzahl_unveraendert=anzahl_unveraendert,
        anzahl_korrektur=anzahl_korrektur,
    )
