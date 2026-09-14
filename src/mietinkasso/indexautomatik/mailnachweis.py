"""Persist actual sending evidence atomically; acceptance is never delivery.

Shared by the three existing business outboxes. No new business ledger,
no resend, no recipient content in the audit record.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from sqlalchemy import select, update

from mietinkasso.infrastructure.db.tables import AuditEventTable


def nachweis_daten(ergebnis):
    sent = getattr(ergebnis, "versendet_am", None)
    external = getattr(ergebnis, "externe_referenz", None)
    provider = getattr(ergebnis, "provider_referenz", None)
    if (getattr(ergebnis, "status", None) != "GESENDET"
            or not isinstance(sent, datetime) or sent.tzinfo is None
            or sent.utcoffset() is None or not isinstance(external, str) or not external
            or not isinstance(provider, str) or not provider):
        return None
    return {"externe_referenz": external, "provider_referenz": provider,
            "versendet_am": sent.isoformat(), "zugang_bestaetigt": False}


def versand_belegen(session_factory, table, row_id, *, ergebnis, erlaubt,
                    neuer_status, zeitfeld, referenz, referenzfeld=None, zusatz=None):
    """`zusatz`: optionale zusätzliche Spaltenwerte, die ATOMAR in
    DERSELBEN UPDATE-Anweisung wie der Statusübergang geschrieben werden
    (z. B. ein eingefrorener Kosten-/Inhaltssnapshot, siehe
    `mahnwesen/service.py::MahnwesenService.versende_mahnlauf`) - niemals
    in einem separaten, potenziell durch einen Absturz getrennten
    zweiten Schritt. `None` (Default) ändert nichts am bestehenden
    Verhalten der drei bereits vorhandenen Aufrufer."""

    proof = nachweis_daten(ergebnis)
    if proof is None:
        raise ValueError("Nachweislich gesendete Nachricht mit tatsächlichem Versandzeitpunkt erforderlich.")
    with session_factory.begin() as session:
        values = {"status": neuer_status, zeitfeld: ergebnis.versendet_am.astimezone(timezone.utc)}
        if referenzfeld:
            values[referenzfeld] = ergebnis.externe_referenz
        if hasattr(table, "fehlergrund"):
            values["fehlergrund"] = None
        if zusatz:
            values.update(zusatz)
        changed = session.execute(update(table).where(table.id == row_id, table.status.in_(erlaubt)).values(**values))
        if changed.rowcount != 1:
            return False
        session.add(AuditEventTable(entity_typ=table.__tablename__, entity_id=str(row_id),
            aktion="MAILVERSAND_BESTAETIGT", akteur="hv-mailversand",
            payload={"vorgangsreferenz": referenz, **proof}))
    return True


@dataclass(frozen=True)
class RekonstruierterBeleg:
    """Baut aus dem bereits persistierten `AuditEventTable`-Payload (das
    `versand_belegen` beim ursprünglichen tatsächlichen Versand
    geschrieben hat) ein Objekt, das `nachweis_daten`/`versand_belegen`
    exakt wie das ursprüngliche Versandergebnis akzeptieren - für eine
    Recovery, die NICHT erneut sendet, sondern eine bereits TATSÄCHLICH
    bestätigte Quittung übernimmt (Mitgliedsnachweise/Kostenbuchung
    nachziehen, siehe `mahnwesen/service.py::MahnwesenService.
    _vervollstaendige_bestaetigten_mahnlauf`). `status` wird NICHT aus
    dem Payload gelesen (er wurde dort nie mitgespeichert) - die reine
    EXISTENZ eines Audit-Eintrags mit `aktion ==
    "MAILVERSAND_BESTAETIGT"` belegt bereits, dass `status == "GESENDET"`
    beim ursprünglichen Versand zutraf, sonst wäre dieser Eintrag nie
    geschrieben worden."""

    status: str
    versendet_am: datetime
    externe_referenz: str
    provider_referenz: str


def beleg_aus_bestaetigtem_audit(session_factory, table, row_id) -> "RekonstruierterBeleg | None":
    """Lädt den JÜNGSTEN `MAILVERSAND_BESTAETIGT`-Audit-Eintrag für
    `(table.__tablename__, row_id)` und rekonstruiert daraus den
    ursprünglichen Beleg - `None`, wenn (noch) keiner existiert."""

    with session_factory() as session:
        event = session.execute(
            select(AuditEventTable)
            .where(AuditEventTable.entity_typ == table.__tablename__)
            .where(AuditEventTable.entity_id == str(row_id))
            .where(AuditEventTable.aktion == "MAILVERSAND_BESTAETIGT")
            .order_by(AuditEventTable.id.desc())
        ).scalars().first()
    if event is None:
        return None
    payload = event.payload
    return RekonstruierterBeleg(
        status="GESENDET",
        versendet_am=datetime.fromisoformat(payload["versendet_am"]),
        externe_referenz=payload["externe_referenz"],
        provider_referenz=payload["provider_referenz"],
    )
