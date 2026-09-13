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
    neuer_debitor: dict | None = None,
    komponenten: list[dict] | None = None,
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

    if neuer_debitor is not None:
        paket["debitoren"] = [{
            "id": neuer_debitor["id"], "name": neuer_debitor["name"],
            "email": neuer_debitor.get("email"), "adresse": neuer_debitor.get("adresse"),
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

    if komponenten:
        paket["komponenten"] = [
            {
                "id": k["id"], "vertrag_id": vertrag_id, "art": k["art"], "bezeichnung": k["bezeichnung"],
                "betrag_cent": k["betrag_cent"], "gueltig_von": k["gueltig_von"],
                "ust_satz_promille": k.get("ust_satz_promille", 10000),
            }
            for k in komponenten
        ]

    return json.dumps(paket, ensure_ascii=False)


class PaketFormUngueltigError(Exception):
    """Ein Paket entspricht NICHT der schmalen, erwarteten Form eines
    Vertragsanlage-Vorgangs (siehe `pruefe_schmale_form`)."""


def pruefe_schmale_form(paket, *, erwarteter_vertrag_id: str) -> None:
    """Verteidigung gegen einen manipulierten POST an `/vertragsanlage/
    uebernehmen`, der über zusätzliche Einträge im Paket (Nachbuchungen,
    Eröffnungen, Sperren, eine fremde Komponente, ein fremdes
    Vertragsprofil o. Ä.) mehr durchsetzen will, als die Vorschau
    tatsächlich zeigte - `wende_an` prüft selbst KEINEN Gesellschaftsscope
    (das ist bei einem Datei-Intake durch einen bereits vertrauten Akteur
    beabsichtigt), also MUSS diese Route selbst sicherstellen, dass ein
    Paket exakt diese enge, von `baue_paket_json` erzeugte Form hat:
    höchstens EIN Vertrag, höchstens EIN Mietvertragsprofil, höchstens
    EINE Kaution, höchstens EIN neuer Debitor - alle für GENAU
    `erwarteter_vertrag_id` (der Debitor über den im selben Paket
    referenzierten `debitor_id` des Vertrags) - beliebig viele
    Komponenten, aber ALLE für GENAU diesen Vertrag - und KEINE andere
    Entitätsart."""

    fremde_entitaeten = (
        paket.gesellschaften or paket.objekte or paket.einheiten
        or paket.eroeffnungen or paket.nachbuchungen or paket.eroeffnungskorrekturen or paket.sperren
    )
    if fremde_entitaeten:
        raise PaketFormUngueltigError(
            "Paket enthält nicht zulässige Zusatzeinträge (Gesellschaft/Objekt/Einheit/Eröffnung/"
            "Nachbuchung/Sperre) - Vertragsanlage erlaubt ausschließlich Vertrag/Debitor/"
            "Mietvertragsprofil/Kaution/Komponenten für genau einen Zielvertrag."
        )
    if len(paket.vertraege) > 1 or len(paket.mietvertragsprofile) > 1 or len(paket.kautionen) > 1:
        raise PaketFormUngueltigError("Paket enthält mehr als einen Vertrag/ein Mietvertragsprofil/eine Kaution.")
    if len(paket.debitoren) > 1:
        raise PaketFormUngueltigError("Paket enthält mehr als einen neuen Debitor.")
    for vertrag_zeile in paket.vertraege:
        if vertrag_zeile.id != erwarteter_vertrag_id:
            raise PaketFormUngueltigError(
                f"Paket referenziert einen anderen Vertrag ('{vertrag_zeile.id}') als erwartet ('{erwarteter_vertrag_id}')."
            )
    for zeile in (*paket.mietvertragsprofile, *paket.kautionen, *paket.komponenten):
        if zeile.vertrag_id != erwarteter_vertrag_id:
            raise PaketFormUngueltigError(
                f"Paket referenziert einen anderen Vertrag ('{zeile.vertrag_id}') als erwartet ('{erwarteter_vertrag_id}')."
            )
    if paket.debitoren:
        erlaubte_debitor_id = paket.vertraege[0].debitor_id if paket.vertraege else None
        if paket.debitoren[0].id != erlaubte_debitor_id:
            raise PaketFormUngueltigError(
                "Der neue Debitor muss dem im (neuen) Vertrag referenzierten Mieter entsprechen."
            )
    if not paket.mietvertragsprofile and not paket.kautionen and not paket.vertraege and not paket.komponenten:
        raise PaketFormUngueltigError("Paket ist leer.")
