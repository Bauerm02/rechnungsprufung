#!/usr/bin/env python3
"""CLI für die tägliche Indexautomatik-Pflege (Auftrag 13.09.,
HV-20260913-INDEXAUTOMATIK): fällige Zustellungen (Outbox: bereite
Erhöhungsschreiben versenden, Zahlungspflicht-Übergänge nachziehen,
verwaiste IN_VERSAND-Fälle auf UNKLAR setzen) und fällige
Vertragsende-Erinnerungen (Owner-only) nachziehen.

    python scripts/indexautomatik_taegliche_pflege.py \\
        --database-url sqlite:////pfad/ausserhalb/repo/produktiv.db

Realer Versand (Erhöhungsschreiben UND Vertragsende-Erinnerung)
erfordert ZWEI unabhängige, getrennte Konfigurationsflags
(`MIETINKASSO_INDEXAUTOMATIK_SEND_ENABLED`,
`MIETINKASSO_INDEXAUTOMATIK_MAILOPS_ALLOWLIST_BESTAETIGT`,
`MIETINKASSO_VERTRAGSENDE_ERINNERUNG_SEND_ENABLED`) - alle drei Default
false. Ohne konfigurierten Transport-Endpunkt bleibt der Versandschritt
strukturell blockiert."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mietinkasso.auth.service import AuthContext  # noqa: E402
from mietinkasso.domain.enums import Rolle  # noqa: E402
from mietinkasso.domain.exceptions import ObjektAusgeschlossenError  # noqa: E402
from mietinkasso.indexautomatik.bootstrap import bauen  # noqa: E402
from mietinkasso.indexautomatik.transport import HttpTransportadapter, Transportadapter, VersandAuftrag  # noqa: E402
from mietinkasso.indexautomatik.zeit import heute_wien  # noqa: E402
from mietinkasso.infrastructure.config import Settings  # noqa: E402
from mietinkasso.infrastructure.db.session import build_session_factory, create_all_tables  # noqa: E402
from mietinkasso.jobs.runner import JobRunner  # noqa: E402

_ADMIN_CTX = AuthContext(user_id="indexautomatik-taegliche-pflege", rolle=Rolle.ADMIN, gesellschaft_ids=None)


def _baue_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", required=True, help="z. B. sqlite:////pfad/ausserhalb/repo/produktiv.db")
    return parser


def _transport_fuer(settings: Settings) -> Transportadapter | None:
    if not settings.indexautomatik_transport_endpoint_url or not settings.indexautomatik_transport_api_key:
        return None
    return HttpTransportadapter(
        endpoint_url=settings.indexautomatik_transport_endpoint_url,
        api_key=settings.indexautomatik_transport_api_key,
    )


def _owner_versand_fn(transport: Transportadapter):
    """Echter Versandaufruf für Vertragsende-Erinnerungen über denselben
    Transportadapter-Vertrag wie die Erhöhungsschreiben-Outbox - ersetzt
    das vorherige `versand_fn=lambda auftrag: None` (unabhängiger Review
    b31-Folgereview: "Keine No-op/Fake-Versandfunktion produktiv: echten
    Adapter verdrahten, ohne Adapter hart blockieren"). Der stabile
    Idempotenzschlüssel (`vertragsende:{vertrag_id}:{end_datum}`) wird
    unverändert als `referenz` an den Transport weitergereicht, damit ein
    Retry beim Provider erkennbar bleibt."""

    def _senden(auftrag: dict) -> None:
        transport.senden(
            VersandAuftrag(
                referenz=auftrag["idempotenzschluessel"],
                empfaenger_name="",
                empfaenger_adresse="",
                empfaenger_email=auftrag["empfaenger"],
                betreff="Vertragsende-Erinnerung",
                text=auftrag["text"],
            )
        )

    return _senden


def _kein_transport_versand_fn(_auftrag: dict) -> None:
    """Wird nie tatsächlich aufgerufen: `benachrichtige_faellige` prüft
    `send_enabled` VOR jedem `versand_fn`-Aufruf, und `send_enabled`
    wird unten hart auf False gesetzt, sobald kein Transport konfiguriert
    ist. Diese Funktion ist nur eine defensive Absicherung gegen eine
    künftige Änderung an dieser Reihenfolge - kein stiller No-op, der
    fälschlich als Versand durchginge, sondern ein sichtbarer Fehler."""

    raise RuntimeError("Kein Transport-Endpunkt konfiguriert - Versand darf nicht stattfinden.")


def main(argv: list[str] | None = None) -> int:
    args = _baue_parser().parse_args(argv)
    settings = Settings(database_url=args.database_url)
    create_all_tables(settings)
    session_factory = build_session_factory(settings.database_url)
    bundle = bauen(session_factory, settings)
    runner = JobRunner(session_factory)

    heute = heute_wien(datetime.now(ZoneInfo("Europe/Vienna")))
    fachschluessel = heute.isoformat()
    transport = _transport_fuer(settings)

    def _arbeit() -> dict:
        verwaiste = bundle.outbox_service.markiere_verwaiste_als_unklar()
        verwaiste_erinnerungen = bundle.vertragsende_service.markiere_verwaiste_als_unklar()

        versendet, uebersprungen = 0, 0
        if transport is not None:
            for schreiben in bundle.outbox_repository.liste_nach_status("BEREIT"):
                ergebnis = bundle.outbox_service.versenden(
                    ctx=_ADMIN_CTX,
                    erhoehungsschreiben_id=schreiben.id,
                    heute=heute,
                    send_enabled=settings.indexautomatik_send_enabled,
                    mailops_allowlist_bestaetigt=settings.indexautomatik_mailops_allowlist_bestaetigt,
                    transport=transport,
                )
                if ergebnis.status == "GESENDET":
                    versendet += 1
                else:
                    uebersprungen += 1
        else:
            uebersprungen = len(bundle.outbox_repository.liste_nach_status("BEREIT"))

        ausgefuehrt = bundle.outbox_service.taegliche_pflege(heute=heute)

        # Unabhängiger Review (b31-Folgereview): "Keine No-op/Fake-
        # Versandfunktion produktiv: echten Adapter verdrahten, ohne
        # Adapter hart blockieren" - `versand_fn=lambda auftrag: None`
        # hätte bei SEND_ENABLED=True echte Erinnerungen fälschlich als
        # BENACHRICHTIGT markiert, OHNE dass je eine Mail versendet
        # wurde. `send_enabled` wird jetzt hart auf False gezwungen,
        # sobald kein Transport konfiguriert ist - unabhängig vom
        # gesetzten Konfigurationsflag.
        vertragsende_send_enabled = settings.vertragsende_erinnerung_send_enabled and transport is not None
        vertragsende_geplant = bundle.vertragsende_service.plane_alle(ctx=_ADMIN_CTX, heute=heute)
        vertragsende_benachrichtigt = bundle.vertragsende_service.benachrichtige_faellige(
            heute=heute,
            send_enabled=vertragsende_send_enabled,
            versand_fn=_owner_versand_fn(transport) if transport is not None else _kein_transport_versand_fn,
        )

        return {
            "verwaiste_als_unklar_markiert": len(verwaiste),
            "erhoehungsschreiben_versendet": versendet,
            "erhoehungsschreiben_uebersprungen_oder_blockiert": uebersprungen,
            "erhoehungsschreiben_ausgefuehrt": len(ausgefuehrt),
            "vertragsende_verwaiste_als_unklar_markiert": len(verwaiste_erinnerungen),
            "vertragsende_neu_geplant": len(vertragsende_geplant),
            "vertragsende_benachrichtigt": len(vertragsende_benachrichtigt),
        }

    ergebnis = runner.einmalig_ausfuehren(job_name="indexautomatik_taegliche_pflege", fachschluessel=fachschluessel, fn=_arbeit)
    if ergebnis is None:
        print(f"Tägliche Pflege für {fachschluessel} lief bereits (oder läuft gerade) - kein Doppellauf.")
        return 0

    if transport is None:
        print("Hinweis: kein Transport-Endpunkt konfiguriert - Versand strukturell blockiert (nur Vorschau/Outbox).")
    for schluessel, wert in ergebnis.items():
        print(f"  {schluessel}: {wert}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
