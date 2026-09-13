"""Baut aus den Formularwerten der Vertragsanlage-Backoffice-UI (Auftrag
HV-20260913-VERTRAGSANLAGE) ein JSON-Paket im GENAU dokumentierten
Intake-Format (`docs/hausverwaltung/IMPORT_VERTRAG.md`). Das ist
bewusst KEINE zweite Buchungsstrecke: das erzeugte JSON läuft durch
exakt denselben `intake/parser.py::parse_json_paket` +
`intake/planner.py::erstelle_plan` + `intake/apply.py::wende_an`-Pfad
wie ein datei-basierter Echtbetrieb-Import."""

from __future__ import annotations

import json
from decimal import Decimal

_PROFIL_FELDER = (
    "nutzungsart", "urspruenglicher_mietbeginn", "verwaltungsuebernahme_am", "verwaltung_bezeichnung",
    "vertragliche_kaution_cent", "vertragliche_kaution_quellenbeleg", "mahngebuehr_cent",
    "mahngebuehr_quellenbeleg", "index_reihe", "index_urspruenglicher_basismonat",
    "index_urspruenglicher_basiswert", "index_schwelle_prozent", "index_schwelle_inklusive",
    "index_anpassungsmonat", "index_mindestintervall_monate", "index_klauseltext_auszug",
    "index_klauseltext_seite",
)


def _json_wert(wert):
    if isinstance(wert, Decimal):
        return str(wert)
    return wert


def baue_paket_json(
    *,
    quelle: str,
    vertrag_id: str,
    neuer_vertrag: dict | None,
    profil_werte: dict,
    kaution_werte: dict | None,
    quelle_typ: str,
    quelle_referenz: str | None,
) -> str:
    paket: dict = {"quelle": quelle}

    if neuer_vertrag is not None:
        paket["vertraege"] = [{
            "id": vertrag_id,
            "einheit_id": neuer_vertrag["einheit_id"],
            "debitor_id": neuer_vertrag["debitor_id"],
            "gesellschaft_id": neuer_vertrag["gesellschaft_id"],
            "rechtsordnung": neuer_vertrag["rechtsordnung"],
            "gueltig_von": neuer_vertrag["gueltig_von"],
            "gueltig_bis": neuer_vertrag.get("gueltig_bis"),
        }]

    profil_eintrag: dict = {"vertrag_id": vertrag_id, "quelle_typ": quelle_typ}
    for feld in _PROFIL_FELDER:
        wert = profil_werte.get(feld)
        if wert is not None:
            profil_eintrag[feld] = _json_wert(wert)
    if quelle_referenz:
        profil_eintrag["quelle_referenz"] = quelle_referenz
    paket["mietvertragsprofile"] = [profil_eintrag]

    if kaution_werte and kaution_werte.get("kaution_eingegangen_cent") is not None and kaution_werte.get("kaution_eingegangen_stichtag"):
        paket["kautionen"] = [{
            "vertrag_id": vertrag_id,
            "betrag_cent": kaution_werte["kaution_eingegangen_cent"],
            "stichtag": kaution_werte["kaution_eingegangen_stichtag"],
            "referenz": kaution_werte.get("kaution_eingegangen_referenz"),
        }]

    return json.dumps(paket, ensure_ascii=False)
