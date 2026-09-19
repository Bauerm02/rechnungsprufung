#!/usr/bin/env python3
"""CLI für den monatlichen Indexautomatik-Lauf (Auftrag 13.09.,
HV-20260913-INDEXAUTOMATIK). Prüft JEDEN aktiven Vertrag genau einmal
pro Kalendermonat (`JobRunner`/`JobLockTable`, fachschluessel = die
Periode "YYYY-MM" - ein zweiter Aufruf im selben Monat, egal ob
Parallelstart oder Neustart nach Absturz, führt den Lauf NICHT erneut
aus) und erzeugt bei rechtlich ausführbarer Erhöhung ein
Erhöhungsschreiben (Status ENTWURF/BEREIT/BLOCKIERT je nach Vollständigkeit).

    python scripts/indexautomatik_monatslauf.py \\
        --database-url sqlite:////pfad/ausserhalb/repo/produktiv.db

Sendet NIE selbst (das übernimmt `indexautomatik_taegliche_pflege.py`
unter `SEND_ENABLED`/Allowlist-Kontrolle) - dieser Lauf berechnet nur
und legt die Outbox-Zeile an.

Erzeugt zusätzlich (Auftrag HV-20260919-INDEX-MONATSBERICHT, alleiniger
Erzeuger, kein neuer Scheduler) den persistenten Owner-Monatsbericht
für diese Periode (`IndexMonatsberichtService.erstellen_fuer_periode`) -
auch versendet wird dieser NIE hier, sondern über die tägliche Pflege.

Ist `MIETINKASSO_INDEXAUTOMATIK_VPI_AUTOMATISCHER_ABRUF=true` gesetzt,
werden VOR der Monatsprüfung die vier amtlichen VPI-Reihen über den
geprüften Statistik-Austria-Client abgerufen und atomar importiert
(Betriebsverdrahtung, unabhängiger Review PUBLIC_CLIENT_REVIEW: "die
vier verifizierten amtlichen Reihen vor Monatsprüfung aktualisieren").
Schlägt DAS fehl (Netzwerk, Schema, Validierung), scheitert der
gesamte Lauf sichtbar - es wird NIE mit veralteten/stillschweigend
übernommenen Werten weitergerechnet. Zusätzlich wird für die betroffene
Periode ein sichtbarer VPI-Fehlerbericht angelegt
(`IndexMonatsberichtService.markiere_vpi_fehler`, Status VPI_FEHLER) -
im Portal sichtbar und über denselben Owner-Only-Kanal wie ein normaler
Monatsbericht versendbar, statt eines stillen Jobabbruchs ohne
fachliche Nachricht."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mietinkasso.auth.service import AuthContext  # noqa: E402
from mietinkasso.domain.enums import Rolle  # noqa: E402
from mietinkasso.indexautomatik.bootstrap import bauen  # noqa: E402
from mietinkasso.indexautomatik.repository import VpiRepository  # noqa: E402
from mietinkasso.indexautomatik.statistik_austria_client import StatistikAustriaClient, VpiAbrufFehlerError  # noqa: E402
from mietinkasso.indexautomatik.vpi_import import _REIHEN_SCHEMA, VpiImportFehlerError, importiere_ogd_csv  # noqa: E402
from mietinkasso.indexautomatik.zeit import heute_wien  # noqa: E402
from mietinkasso.infrastructure.config import Settings  # noqa: E402
from mietinkasso.infrastructure.db.session import build_session_factory, create_all_tables  # noqa: E402
from mietinkasso.jobs.runner import JobRunner  # noqa: E402

_ADMIN_CTX = AuthContext(user_id="indexautomatik-monatslauf", rolle=Rolle.ADMIN, gesellschaft_ids=None)


class VpiAktualisierungFehlgeschlagenError(RuntimeError):
    """Der automatische VPI-Abruf/-Import ist fehlgeschlagen - der
    gesamte Monatslauf wird NICHT gestartet, damit nie mit veralteten
    Werten weitergerechnet wird."""


def _baue_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", required=True, help="z. B. sqlite:////pfad/ausserhalb/repo/produktiv.db")
    return parser


def _aktualisiere_vpi_reihen(*, settings: Settings, vpi_repository: VpiRepository) -> dict[str, int]:
    if not settings.indexautomatik_vpi_automatischer_abruf:
        return {}
    if not settings.indexautomatik_vpi_ablage_verzeichnis:
        raise VpiAktualisierungFehlgeschlagenError(
            "indexautomatik_vpi_automatischer_abruf ist aktiv, aber kein Ablageverzeichnis "
            "(MIETINKASSO_INDEXAUTOMATIK_VPI_ABLAGE_VERZEICHNIS) konfiguriert."
        )
    client = StatistikAustriaClient(ziel_verzeichnis=settings.indexautomatik_vpi_ablage_verzeichnis)
    monatszeilen_je_reihe: dict[str, int] = {}
    for reihe in sorted(_REIHEN_SCHEMA):
        try:
            abruf = client.abrufen(reihe)
            ergebnis = importiere_ogd_csv(
                str(abruf.pfad), reihe=reihe, repository=vpi_repository,
                importiert_von="indexautomatik-monatslauf", abgerufen_am=abruf.abgerufen_am, quelle_url=abruf.url,
            )
        except (VpiAbrufFehlerError, VpiImportFehlerError) as exc:
            raise VpiAktualisierungFehlgeschlagenError(
                f"Automatischer VPI-Abruf/-Import für Reihe {reihe} fehlgeschlagen: {exc}"
            ) from exc
        monatszeilen_je_reihe[reihe] = ergebnis.monatszeilen
    return monatszeilen_je_reihe


def main(argv: list[str] | None = None) -> int:
    args = _baue_parser().parse_args(argv)
    settings = Settings(database_url=args.database_url)
    create_all_tables(settings)
    session_factory = build_session_factory(settings.database_url)
    bundle = bauen(session_factory, settings)
    runner = JobRunner(session_factory)

    from datetime import datetime

    heute = heute_wien(datetime.now(ZoneInfo("Europe/Vienna")))
    periode = f"{heute.year:04d}-{heute.month:02d}"

    def _arbeit() -> dict:
        try:
            vpi_aktualisiert = _aktualisiere_vpi_reihen(settings=settings, vpi_repository=bundle.vpi_repository)
        except VpiAktualisierungFehlgeschlagenError as exc:
            # Auftrag HV-20260919-INDEX-MONATSBERICHT (Codex-Ergänzung):
            # "Bei fehlgeschlagenem VPI-Abruf muss ein sichtbarer Monats-
            # Fehlerbericht/Owner-Hinweis entstehen, nicht nur Jobabbruch
            # ohne Nachricht; keine stille Berechnung mit alten Werten."
            # Der Monatslauf bricht weiterhin sichtbar ab (Exception wird
            # unten weitergereicht, JobLockTable markiert FEHLGESCHLAGEN,
            # kein stiller Erfolg) - ZUSÄTZLICH entsteht hier ein für
            # Portal und Owner-Mail sichtbarer Fehlerbericht, damit ein
            # Ausbleiben nicht nur als leerer/verschwundener Bericht
            # wahrgenommen wird. Kein neuer Job, keine neue Berechnung mit
            # veralteten Werten - `monatslauf_alle` wird NICHT erreicht.
            bundle.monatsbericht_service.markiere_vpi_fehler(periode=periode, fehlergrund=str(exc))
            raise
        laeufe = bundle.index_automatik_service.monatslauf_alle(
            ctx=_ADMIN_CTX, heute=heute, akteur="indexautomatik-monatslauf"
        )
        zusammenfassung: dict[str, int] = {}
        for lauf in laeufe:
            zusammenfassung[lauf.status] = zusammenfassung.get(lauf.status, 0) + 1

        # Auftrag HV-20260919-INDEX-MONATSBERICHT: alleiniger Erzeuger des
        # Owner-Monatsberichts (Umfang A) - unmittelbar nach dem
        # Monatslauf, kein neuer Scheduler. Sendet NIE selbst (das
        # übernimmt `indexautomatik_taegliche_pflege.py`).
        bericht = bundle.monatsbericht_service.erstellen_fuer_periode(periode=periode, laeufe=laeufe, heute=heute)
        monatsbericht_status = bericht.status if bericht is not None else "BEREITS_EINGEFROREN_UEBERSPRUNGEN"

        return {
            "periode": periode, "vertraege_geprueft": len(laeufe), "nach_status": zusammenfassung,
            "vpi_monatszeilen_aktualisiert": vpi_aktualisiert, "monatsbericht_status": monatsbericht_status,
        }

    ergebnis = runner.einmalig_ausfuehren(job_name="indexautomatik_monatslauf", fachschluessel=periode, fn=_arbeit)
    if ergebnis is None:
        print(f"Monatslauf für Periode {periode} lief bereits (oder läuft gerade) - kein Doppellauf.")
        return 0

    print(f"Monatslauf {periode}: {ergebnis['vertraege_geprueft']} Verträge geprüft.")
    for status, anzahl in sorted(ergebnis["nach_status"].items()):
        print(f"  {status}: {anzahl}")
    print(f"Index-Monatsbericht {periode}: {ergebnis['monatsbericht_status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
