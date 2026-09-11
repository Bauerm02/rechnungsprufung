"""Schlanke, versionierte API + Read-Only-Dashboard für das
Mietinkasso-Modul (Fachregel 8: versionierte API/Domain-Events, KEINE
allgemeine Antwort-KI in MVP1). Schreibende Vorgänge (Eröffnung,
Vorschreibung, Bankimport, Mahnlauf) laufen über die Services in
op/vorschreibung/bank/mahnwesen und werden bewusst NICHT unauthentifiziert
über HTTP freigegeben; dieses Modul liefert einen Health-Check und
Read-Only-Übersichten, wie sie ein Worker/Scheduler-Betrieb (DB/Worker
unabhängig vom Browser) zur Kontrolle braucht.
"""

from __future__ import annotations

from datetime import date

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from mietinkasso.infrastructure.config import get_settings
from mietinkasso.infrastructure.db.session import build_session_factory
from mietinkasso.mahnwesen.repository import MahnFallRepository
from mietinkasso.op.repository import OPRepository
from mietinkasso.op.service import OPService
from mietinkasso.stammdaten.repository import StammdatenRepository

app = FastAPI(title="Mietinkasso API", version="1")

_settings = get_settings()
_session_factory = build_session_factory(_settings.database_url)
_stammdaten_repo = StammdatenRepository(_session_factory)
_op_service = OPService(OPRepository(_session_factory), _stammdaten_repo)
_mahn_repo = MahnFallRepository(_session_factory)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "environment": _settings.environment, "send_enabled": _settings.send_enabled}


@app.get("/v1/konten/{konto_id}/op")
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


@app.get("/v1/mahnwesen/outbox/{vertrag_id}")
def mahn_outbox(vertrag_id: str) -> dict:
    letzter = _mahn_repo.letzter_mahnfall(vertrag_id)
    if letzter is None:
        return {"vertrag_id": vertrag_id, "letzter_fall": None}
    return {
        "vertrag_id": vertrag_id,
        "letzter_fall": {
            "id": letzter.id,
            "stufe": letzter.stufe,
            "status": letzter.status,
            "betrag_cent": letzter.betrag_cent,
            "geplant_am": letzter.geplant_am.isoformat() if letzter.geplant_am else None,
            "gesendet_am": letzter.gesendet_am.isoformat() if letzter.gesendet_am else None,
        },
    }


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return f"""
    <html>
    <head><title>Mietinkasso — Status</title>
    <style>
      body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #1a1a1a; }}
      code {{ background: #f0f0f0; padding: 0.1rem 0.3rem; border-radius: 3px; }}
      .warn {{ color: #a15c00; font-weight: 600; }}
    </style>
    </head>
    <body>
      <h1>Mietinkasso — Betriebsstatus</h1>
      <p>Umgebung: <code>{_settings.environment}</code></p>
      <p>Versand aktiv (SEND_ENABLED): <span class="warn">{_settings.send_enabled}</span></p>
      <p>Pilotobjekte: <code>{", ".join(_settings.pilot_objekte)}</code>
         &nbsp;|&nbsp; ausgeschlossen: <code>{", ".join(_settings.ausgeschlossene_objekte)}</code></p>
      <h2>Read-Only-Endpunkte</h2>
      <ul>
        <li><code>GET /health</code></li>
        <li><code>GET /v1/konten/{{konto_id}}/op</code> — OP-Liste + Saldo</li>
        <li><code>GET /v1/mahnwesen/outbox/{{vertrag_id}}</code> — letzter Mahnfall (Preview/Outbox)</li>
      </ul>
      <p>Schreibende Vorgänge (Eröffnung, Vorschreibung, Bankimport, Mahnlauf)
         laufen ausschließlich über die Service-Schicht (Worker/Scheduler
         bzw. ein noch zu bauendes authentifiziertes Backoffice), nicht über
         unauthentifizierte HTTP-Schreibendpunkte.</p>
    </body>
    </html>
    """
