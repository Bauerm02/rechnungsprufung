"""Persist actual sending evidence atomically; acceptance is never delivery.

Shared by the three existing business outboxes. No new business ledger,
no resend, no recipient content in the audit record.
"""
from datetime import datetime, timezone
from sqlalchemy import update

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
