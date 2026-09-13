"""HTTP-Abruf der amtlichen Statistik-Austria-VPI-OGD-Dateien (Auftrag
13.09., Betriebspräzisierung: "tatsächlich angebundenen regelmäßigen
Abruf ergänzen, keine monatliche Hand-Konvertierung voraussetzen").

WICHTIGE EHRLICHE EINSCHRÄNKUNG: Claude hat in dieser Sitzung KEINEN
Internet-/Serverzugriff (siehe AGENTS.md) und konnte diesen Client
deshalb NICHT gegen den echten Endpunkt ausführen/verifizieren. Nur die
URL für VPI20C18 wurde vom Betreiber wörtlich bestätigt
(data.statistik.gv.at/data/OGD_vpi20c18_VPI_2020COICOP18_1.csv); die
URLs für VPI15C18/VPI00/VPI96 in `vpi_import.py::_REIHEN_SCHEMA` folgen
nur demselben Namensmuster und sind NICHT bestätigt. Codex muss diesen
Client vor produktivem Einsatz gegen die echten Endpunkte verifizieren
(siehe OFFENE_PUNKTE.md). Standardmäßig NICHT automatisch in den
Monatslauf eingebunden (`Settings.indexautomatik_vpi_automatischer_abruf
= False`) - der CLI-Befehl `--vpi-abrufen` ruft ihn explizit auf."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from mietinkasso.indexautomatik.vpi_import import _REIHEN_SCHEMA


class VpiAbrufFehlerError(Exception):
    """Der HTTP-Abruf ist fehlgeschlagen oder lieferte keinen Erfolgsstatus."""


@dataclass(frozen=True)
class VpiAbrufErgebnis:
    reihe: str
    pfad: Path
    url: str
    abgerufen_am: datetime


class StatistikAustriaClient:
    def __init__(self, *, ziel_verzeichnis: str, timeout_sekunden: float = 30.0, http_get=None):
        self._ziel_verzeichnis = Path(ziel_verzeichnis)
        self._timeout_sekunden = timeout_sekunden
        if http_get is None:
            import httpx

            http_get = httpx.get
        self._http_get = http_get

    def abrufen(self, reihe: str) -> VpiAbrufErgebnis:
        schema = _REIHEN_SCHEMA.get(reihe)
        if schema is None:
            raise VpiAbrufFehlerError(f"Unbekannte VPI-Reihe '{reihe}'.")
        url = schema["url"]
        try:
            response = self._http_get(url, timeout=self._timeout_sekunden)
        except Exception as exc:
            raise VpiAbrufFehlerError(f"Abruf von {reihe} fehlgeschlagen ({type(exc).__name__}).") from exc
        if response.status_code != 200:
            raise VpiAbrufFehlerError(f"Abruf von {reihe} lieferte Statuscode {response.status_code}.")

        abgerufen_am = datetime.now(timezone.utc)
        self._ziel_verzeichnis.mkdir(parents=True, exist_ok=True)
        zeitstempel = abgerufen_am.strftime("%Y%m%dT%H%M%SZ")
        pfad = self._ziel_verzeichnis / f"{reihe}_{zeitstempel}.csv"
        pfad.write_bytes(response.content)
        return VpiAbrufErgebnis(reihe=reihe, pfad=pfad, url=url, abgerufen_am=abgerufen_am)
