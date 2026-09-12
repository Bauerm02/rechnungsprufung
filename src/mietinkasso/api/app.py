"""Schlanke, versionierte API + Read-Only-Dashboard für das
Mietinkasso-Modul (Fachregel 8: versionierte API/Domain-Events, KEINE
allgemeine Antwort-KI in MVP1). Schreibende Vorgänge (Eröffnung,
Vorschreibung, Bankimport, Mahnlauf) laufen über die Services in
op/vorschreibung/bank/mahnwesen und werden bewusst NICHT unauthentifiziert
über HTTP freigegeben; dieses Modul liefert einen Health-Check und
Read-Only-Übersichten, wie sie ein Worker/Scheduler-Betrieb (DB/Worker
unabhängig vom Browser) zur Kontrolle braucht.

Es gibt in MVP1 KEIN echtes Login/Session-Handling und keine
Gesellschafts-Scoping auf HTTP-Ebene (das ist ein offener Punkt, siehe
docs/hausverwaltung/OFFENE_PUNKTE.md). Bis das existiert, sind die
Datenendpunkte "closed by default": ohne konfigurierten
`MIETINKASSO_API_TOKEN` antworten sie mit 503, statt Kontodaten ohne
jede Prüfung offenzulegen. Mit konfiguriertem Token ist das ein einzelner
geteilter Operator-Schlüssel (ein `X-API-Key`-Header) - eine
Übergangslösung für den internen Pilotbetrieb, KEINE
Mandantentrennung pro Endanwender.
"""

from __future__ import annotations

from datetime import date

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse

from mietinkasso.backoffice.app import router as backoffice_router
from mietinkasso.infrastructure.config import get_settings, pruefe_produktionskonfiguration
from mietinkasso.infrastructure.db.session import build_session_factory
from mietinkasso.mahnwesen.repository import MahnFallRepository
from mietinkasso.op.repository import OPRepository
from mietinkasso.op.service import OPService
from mietinkasso.stammdaten.repository import StammdatenRepository

app = FastAPI(title="Mietinkasso API", version="1")
app.include_router(backoffice_router)

_settings = get_settings()
# Sofortiger, lauter Startfehler statt eines still laufenden Prozesses mit
# unsicherer Konfiguration - siehe infrastructure/config.py.
pruefe_produktionskonfiguration(_settings)
_session_factory = build_session_factory(_settings.database_url)
_stammdaten_repo = StammdatenRepository(_session_factory)
_op_service = OPService(OPRepository(_session_factory), _stammdaten_repo)
_mahn_repo = MahnFallRepository(_session_factory)


def _require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    if not _settings.api_token:
        raise HTTPException(
            status_code=503,
            detail="MIETINKASSO_API_TOKEN ist nicht konfiguriert; Datenendpunkte bleiben geschlossen "
            "(closed by default), bis echte Auth eingerichtet ist.",
        )
    if x_api_key != _settings.api_token:
        raise HTTPException(status_code=401, detail="Ungültiger oder fehlender X-API-Key.")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "environment": _settings.environment, "send_enabled": _settings.send_enabled}


@app.get("/v1/konten/{konto_id}/op", dependencies=[Depends(_require_api_key)])
def op_liste(konto_id: str, stichtag: date | None = None) -> dict:
    konto = _stammdaten_repo.get_konto(konto_id)
    if konto is None:
        raise HTTPException(status_code=404, detail=f"Unbekanntes Konto {konto_id}")
    saldo = _op_service.berechne_saldo(konto_id, stichtag=stichtag)
    return {
        "konto_id": konto_id,
        "saldo_cent": saldo.saldo_cent,
        "faelliger_unstrittiger_rest_cent": saldo.faelliger_unstrittiger_rest_cent,
        "positionen": [
            {
                "id": p.id,
                "typ": p.typ,
                "betrag_cent": p.betrag_cent,
                "belegdatum": p.belegdatum.isoformat(),
                "faelligkeit": p.faelligkeit.isoformat() if p.faelligkeit else None,
                "faelligkeit_bekannt": p.faelligkeit_bekannt,
                "status": p.status,
                "beleg_referenz": p.beleg_referenz,
            }
            for p in saldo.positionen
        ],
    }


@app.get("/v1/mahnwesen/outbox/{vertrag_id}", dependencies=[Depends(_require_api_key)])
def mahn_outbox(vertrag_id: str) -> dict:
    faelle = _mahn_repo.list_fuer_vertrag(vertrag_id)
    return {
        "vertrag_id": vertrag_id,
        "faelle": [
            {
                "id": fall.id,
                "forderung_op_position_id": fall.forderung_op_position_id,
                "stufe": fall.stufe,
                "status": fall.status,
                "betrag_cent": fall.betrag_cent,
                "geplant_am": fall.geplant_am.isoformat() if fall.geplant_am else None,
                "gesendet_am": fall.gesendet_am.isoformat() if fall.gesendet_am else None,
            }
            for fall in faelle
        ],
    }


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    api_status = "konfiguriert (X-API-Key erforderlich)" if _settings.api_token else "NICHT konfiguriert -> Datenendpunkte 503"
    return f"""
    <html>
    <head><title>Hausverwaltung & Mietinkasso — Status</title>
    <style>
      body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #1a1a1a; }}
      code {{ background: #f0f0f0; padding: 0.1rem 0.3rem; border-radius: 3px; }}
      .warn {{ color: #a15c00; font-weight: 600; }}
    </style>
    </head>
    <body>
      <h1>Hausverwaltung & Mietinkasso — Betriebsstatus</h1>
      <p>Umgebung: <code>{_settings.environment}</code></p>
      <p>Versand aktiv (SEND_ENABLED): <span class="warn">{_settings.send_enabled}</span></p>
      <p>API-Token: <span class="warn">{api_status}</span></p>
      <p>Pilotobjekte: <code>{", ".join(_settings.pilot_objekte)}</code>
         &nbsp;|&nbsp; ausgeschlossen: <code>{", ".join(_settings.ausgeschlossene_objekte)}</code></p>
      <h2>Read-Only-Endpunkte (X-API-Key erforderlich)</h2>
      <ul>
        <li><code>GET /health</code> (offen, keine Kontodaten)</li>
        <li><code>GET /v1/konten/{{konto_id}}/op</code> — OP-Liste + Saldo</li>
        <li><code>GET /v1/mahnwesen/outbox/{{vertrag_id}}</code> — Mahnfälle (Preview/Outbox)</li>
      </ul>
      <p>Schreibende Vorgänge (Eröffnung, Vorschreibung, Bankimport, Mahnlauf)
         laufen NICHT über unauthentifizierte HTTP-Schreibendpunkte, sondern
         über die Service-Schicht direkt oder über das bedienbare
         Backoffice unter <a href="/backoffice/login">/backoffice/</a>
         (eigener, session-basierter Login, "closed by default" ohne
         konfigurierten <code>MIETINKASSO_BACKOFFICE_PASSWORD_HASH</code>;
         lokaler Pilot mit synthetischen Demodaten, kein Mehrbenutzer-
         Onlinebetrieb).</p>
    </body>
    </html>
    """
