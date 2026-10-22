"""Mieterakte, Vertragsliste und Vertragsanlage/-bearbeitung.

Schreibt über die BESTEHENDE generische Intake-Strecke (parse -> plan ->
apply); die Vorschau hinterlegt ihren Stand serverseitig in der Sitzung,
`uebernehmen` wendet NIE ein vom Browser mitgeschicktes Paket an.

Reihenfolge innerhalb dieses Moduls ist bindend: `GET
/vertrag/weiterleiten` muss VOR `GET /vertrag/{vertrag_id}` registriert
bleiben, sonst verschluckt der Platzhalter den Literalpfad."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from mietinkasso.auth.service import AuthContext, require_gesellschaft_access, require_schreibrecht
from mietinkasso.backoffice.vertragsanlage_form import (
    bestehende_werte as _vertragsanlage_bestehende_werte,
    detail_ansicht as _vertragsanlage_detail_ansicht,
    einheit_label as _vertragsanlage_einheit_label,
    komponenten_werte_aus_form as _vertragsanlage_komponenten_werte_aus_form,
    neu_kontext_formular as _vertragsanlage_neu_kontext_formular,
    neu_kontext_werte as _vertragsanlage_neu_kontext_werte,
    pdf_upload_mini_formular as _vertragsanlage_pdf_upload_mini_formular,
    profil_werte_aus_form as _vertragsanlage_profil_werte_aus_form,
    review_formular as _vertragsanlage_review_formular,
    vertraege_liste_formular as _vertragsanlage_liste_formular,
    vorschau_ansicht as _vertragsanlage_vorschau_ansicht,
)
from mietinkasso.domain.exceptions import MietinkassoError
from mietinkasso.indexautomatik.zeit import heute_wien
from mietinkasso.intake.apply import wende_an as _intake_wende_an
from mietinkasso.intake.parser import IntakeFormatFehlerError, parse_json_paket as _intake_parse_json_paket
from mietinkasso.intake.planner import erstelle_plan as _intake_erstelle_plan
from mietinkasso.op.kontoblatt import lade_kontoblatt
from mietinkasso.rueckstaende.service import berechne_rueckstandsuebersicht
from mietinkasso.stammdaten.telefon import normalisiere_telefon
from mietinkasso.vertragsanlage.ablage import (
    UploadAbgelehntError as _VertragsanlageUploadAbgelehntError,
    speichern as _vertragsanlage_speichern,
)
from mietinkasso.vertragsanlage.paket_bau import (
    PaketFormUngueltigError as _VertragsanlagePaketFormUngueltigError,
    baue_paket_json as _vertragsanlage_baue_paket_json,
    pruefe_schmale_form as _vertragsanlage_pruefe_schmale_form,
)
from mietinkasso.vertragsanlage.pdf_extraktion import (
    PdfNichtLesbarError as _VertragsanlagePdfNichtLesbarError,
    extrahiere as _vertragsanlage_extrahiere,
)
from mietinkasso.vertragsanlage.vorschlaege import (
    mehrdeutigkeiten_aus_extraktion as _vertragsanlage_mehrdeutigkeiten_aus_extraktion,
    vorschlaege_aus_extraktion as _vertragsanlage_vorschlaege_aus_extraktion,
)

from mietinkasso.backoffice import dependencies as deps
from mietinkasso.backoffice.auth import _ctx, _current_session, _fehlerseite, _layout, _objekt_fuer_vertrag_gesperrt, _verify_csrf
from mietinkasso.backoffice.routes.shared import listen_links_html


#: Ohne eigenes Prefix - das gemeinsame `/backoffice`-Prefix wird GENAU
#: EINMAL in `backoffice/app.py` gesetzt.
router = APIRouter()


# -- Vertragsanlage/-anzeige (Auftrag HV-20260913-VERTRAGSANLAGE) -----------
#
# Nutzt bewusst die BESTEHENDE generische Intake-Schreibstrecke
# (`intake/parser.py::parse_json_paket` + `intake/planner.py::erstelle_plan`
# + `intake/apply.py::wende_an`) statt einer zweiten Buchungsstrecke - siehe
# `vertragsanlage/paket_bau.py`. Diese Routen lesen/schreiben NIE ein
# Original-PDF in die Datenbank; der Upload landet ausschließlich über
# `vertragsanlage/ablage.py` in einem privaten Verzeichnis AUSSERHALB des
# Repos (siehe `infrastructure/config.py::vertragsanlage_upload_verzeichnis`).


async def _lese_begrenzt(upload_file: UploadFile, max_bytes: int) -> bytes:
    """Liest `upload_file` in Blöcken und bricht ab, SOBALD `max_bytes`
    überschritten ist - anders als ein einzelnes `await upload_file.read()`
    wird der komplette Körper NIE erst vollständig in den Speicher
    geladen, bevor die Größe geprüft wird (Speichererschöpfungsschutz bei
    einem absichtlich überdimensionierten Upload)."""

    stueck_groesse = 1024 * 1024
    gelesen = bytearray()
    while True:
        stueck = await upload_file.read(stueck_groesse)
        if not stueck:
            break
        gelesen += stueck
        if len(gelesen) > max_bytes:
            raise _VertragsanlageUploadAbgelehntError(
                f"Datei überschreitet die zulässige Größe ({max_bytes // (1024 * 1024)} MB) - nichts wurde gespeichert."
            )
    return bytes(gelesen)


def _hat_gesellschaft_zugriff(ctx: AuthContext, gesellschaft_id: str) -> bool:
    try:
        require_gesellschaft_access(ctx, gesellschaft_id)
        return True
    except MietinkassoError:
        return False


@router.get("/vertraege", response_class=HTMLResponse)
def vertragsanlage_liste(
    request: Request, objekt_id: str = "", q: str = "", session=Depends(_current_session),
) -> HTMLResponse:
    ctx = _ctx(session)
    alle_objekte = []
    zeilen = []
    einheiten_mit_vertrag: set[str] = set()
    for vertrag in deps._stammdaten_repo.list_alle_vertraege():
        if not _hat_gesellschaft_zugriff(ctx, vertrag.gesellschaft_id):
            continue
        einheit = deps._stammdaten_repo.get_einheit(vertrag.einheit_id)
        objekt = deps._stammdaten_repo.get_objekt(einheit.objekt_id) if einheit else None
        debitor = deps._stammdaten_repo.get_debitor(vertrag.debitor_id)
        if einheit is None or objekt is None or debitor is None:
            continue
        einheiten_mit_vertrag.add(einheit.id)
        if objekt_id and objekt.id != objekt_id:
            continue
        if q and q.strip().lower() not in debitor.name.lower():
            continue
        # Auftrag HV-20261020-VERTRAGSDATENBASIS: zentrale Übersicht zeigt
        # je Zeile aufklappbare Kurzdetails - braucht dafür das Profil mit,
        # KEIN zweiter Rechenweg, nur Anzeige bereits gespeicherter Felder.
        profil = deps._stammdaten_repo.neuestes_mietvertragsprofil(vertrag.id)
        zeilen.append({"vertrag": vertrag, "objekt": objekt, "einheit": einheit, "debitor": debitor, "profil": profil})

    # Leerstände/sonstige Einheiten OHNE Vertrag bleiben sichtbar (Auftrag
    # HV-20260914-UI-EINFACH) - Bestandsart kommt direkt aus dem
    # gepflegten `Einheit.nutzungsstatus`, NIE aus einem erratenen
    # Nullsaldo/Fehlen eines Kontos. Der Objekt-/Mieterfilter oben wirkt
    # NUR auf die Mieterliste, Leerstände bleiben immer vollständig
    # sichtbar (sonst würde ein Mieterfilter versehentlich Leerstände
    # ausblenden, die er gar nicht betreffen soll).
    leerstand_zeilen = []
    for objekt in deps._stammdaten_repo.list_objekte():
        if objekt.ausgeschlossen or not _hat_gesellschaft_zugriff(ctx, objekt.gesellschaft_id):
            continue
        alle_objekte.append(objekt)
        for einheit in deps._stammdaten_repo.list_einheiten_fuer_objekt(objekt.id):
            if einheit.id in einheiten_mit_vertrag:
                continue
            leerstand_zeilen.append({"objekt": objekt, "einheit": einheit})

    return _layout(
        request, session, "Mieter & Objekte",
        f'<div class="card">{listen_links_html()}</div>'
        + _vertragsanlage_liste_formular(
            zeilen, session.csrf_token, leerstand_zeilen=leerstand_zeilen,
            objekte=alle_objekte, filter_objekt_id=objekt_id, filter_q=q,
        ),
    )


@router.get("/vertrag/weiterleiten")
def vertragsanlage_weiterleiten(vertrag_id: str, session=Depends(_current_session)) -> RedirectResponse:
    return RedirectResponse(f"/backoffice/vertrag/{vertrag_id}", status_code=303)


@router.get("/vertraege/neu", response_class=HTMLResponse)
def vertragsanlage_neu_formular(request: Request, session=Depends(_current_session)) -> HTMLResponse:
    ctx = _ctx(session)
    einheiten_mit_objekt = []
    for objekt in sorted(deps._stammdaten_repo.list_objekte(), key=lambda o: o.id):
        if objekt.ausgeschlossen or not _hat_gesellschaft_zugriff(ctx, objekt.gesellschaft_id):
            continue
        for einheit in deps._stammdaten_repo.list_einheiten_fuer_objekt(objekt.id):
            einheiten_mit_objekt.append((objekt, einheit))
    # Nur Debitoren zeigen, die bereits über einen Vertrag mit einer für
    # `ctx` erlaubten Gesellschaft verbunden sind - eine ungefilterte
    # Liste ALLER Debitoren würde bei einer künftig eingeschränkten Rolle
    # Mieter fremder Gesellschaften offenlegen (Auftraggeber-Rückprüfung).
    erlaubte_debitor_ids = {
        v.debitor_id for v in deps._stammdaten_repo.list_alle_vertraege() if _hat_gesellschaft_zugriff(ctx, v.gesellschaft_id)
    }
    debitoren = [d for d in deps._stammdaten_repo.list_alle_debitoren() if d.id in erlaubte_debitor_ids]
    gesellschaften = [g for g in deps._stammdaten_repo.list_gesellschaften() if _hat_gesellschaft_zugriff(ctx, g.id)]
    inhalt = _vertragsanlage_neu_kontext_formular(
        einheiten_mit_objekt=einheiten_mit_objekt, debitoren=debitoren, gesellschaften=gesellschaften,
        csrf=session.csrf_token,
    )
    return _layout(request, session, "Neuen Mietvertrag anlegen", inhalt)


@router.get("/vertrag/{vertrag_id}/mietvertragsprofil/bearbeiten", response_class=HTMLResponse)
def vertragsanlage_bearbeiten_formular(request: Request, vertrag_id: str, session=Depends(_current_session)) -> HTMLResponse:
    vertrag = deps._stammdaten_repo.get_vertrag(vertrag_id)
    if vertrag is None or _objekt_fuer_vertrag_gesperrt(vertrag_id):
        return _fehlerseite(session, "Mietvertragsprofil", "Vertrag nicht verfügbar.", "/backoffice/vertraege")
    require_gesellschaft_access(_ctx(session), vertrag.gesellschaft_id)
    profil = deps._stammdaten_repo.neuestes_mietvertragsprofil(vertrag_id)
    kaution = deps._stammdaten_repo.get_kaution(vertrag_id)
    werte = _vertragsanlage_bestehende_werte(profil, kaution)
    upload_mini = _vertragsanlage_pdf_upload_mini_formular(vertrag_id, session.csrf_token)
    review = _vertragsanlage_review_formular(
        ist_neu=False, kontext_hidden={"modus": "BESTEHEND", "vertrag_id": vertrag_id, "quelle_typ": "MANUELL"},
        werte=werte, vorschlaege={}, warnungen=(), csrf=session.csrf_token,
        aktion_url="/backoffice/vertragsanlage/vorschau", zurueck_href=f"/backoffice/vertrag/{vertrag_id}",
        kaution_bereits_vorhanden=kaution is not None,
    )
    return _layout(request, session, "Mietvertragsprofil bearbeiten", upload_mini + review)


@router.get("/vertrag/{vertrag_id}", response_class=HTMLResponse)
def vertragsanlage_detail(
    request: Request, vertrag_id: str, von_objekt: str | None = None, session=Depends(_current_session),
) -> HTMLResponse:
    vertrag = deps._stammdaten_repo.get_vertrag(vertrag_id)
    if vertrag is None:
        return _fehlerseite(session, "Mietvertrag", f"Unbekannter Vertrag {vertrag_id}.", "/backoffice/vertraege")
    ctx = _ctx(session)
    require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
    # Pilotausschluss-Objekt: die gemeinsame Mieterakte zeigt für ein
    # ausgeschlossenes Objekt UEBERHAUPT KEINE Finanz-/Mietdaten -
    # Codex-Rückprüfung 14.09.2026: eine nur SELEKTIVE Unterdrückung
    # einzelner Kartenabschnitte (vorherige Fassung) ließ z. B. die
    # Vorschreibung trotzdem durchrutschen. Blockt hier VOR jedem
    # weiteren Datenread, konsistent mit dem etablierten Muster an
    # dutzenden anderen Stellen dieser Datei (`_objekt_fuer_vertrag_
    # gesperrt`) - bewusst NICHT das laxere Kontoauszug-Verhalten
    # (dort bleibt aus historischen Gründen die reine Saldoansicht
    # sichtbar; das ändert dieser Auftrag nicht).
    if _objekt_fuer_vertrag_gesperrt(vertrag_id):
        return _fehlerseite(
            session, "Mieterakte",
            "Objekt ist von der Pilotphase ausgeschlossen; die gemeinsame Mieterakte zeigt hierfür keine Finanz-/Mietdaten.",
            "/backoffice/vertraege",
        )
    einheit = deps._stammdaten_repo.get_einheit(vertrag.einheit_id)
    objekt = deps._stammdaten_repo.get_objekt(einheit.objekt_id) if einheit else None
    debitor = deps._stammdaten_repo.get_debitor(vertrag.debitor_id)
    gesellschaft = deps._stammdaten_repo.get_gesellschaft(vertrag.gesellschaft_id)
    if einheit is None or objekt is None or debitor is None or gesellschaft is None:
        return _fehlerseite(session, "Mietvertrag", "Stammdaten unvollständig.", "/backoffice/vertraege")

    profil = deps._stammdaten_repo.neuestes_mietvertragsprofil(vertrag_id)
    kaution = deps._stammdaten_repo.get_kaution(vertrag_id)
    konto = deps._stammdaten_repo.get_konto_by_vertrag(vertrag_id)
    komponenten = deps._stammdaten_repo.list_aktive_komponenten(vertrag_id, heute_wien())
    versionen = deps._stammdaten_repo.liste_mietvertragsprofil_versionen(vertrag_id)

    # Freigegebenes Rechtsprofil und ein ggf. NEUERER Entwurf werden
    # GETRENNT ausgewiesen - ein Entwurf darf nie die Kennzeichnung
    # "freigegeben" überschreiben (Codex-Rückprüfung 14.09.2026; die
    # Liste ist nach Version absteigend sortiert, `historie[-1]` traf
    # vorher fälschlich die ÄLTESTE statt die neueste Zeile).
    historie = deps._indexautomatik.rechtsprofil_service.liste_fuer_vertrag(vertrag_id)
    freigegebenes_profil = next((p for p in historie if p.status == "FREIGEGEBEN"), None)
    rechtsprofil_freigegeben_hinweis = (
        f"Version {freigegebenes_profil.version}, Rechtsordnung {freigegebenes_profil.rechtsordnung}"
        if freigegebenes_profil else None
    )
    neuester_entwurf = historie[0] if historie and historie[0].status == "ENTWURF" else None
    rechtsprofil_entwurf_hinweis = None
    if neuester_entwurf is not None and (freigegebenes_profil is None or neuester_entwurf.version > freigegebenes_profil.version):
        rechtsprofil_entwurf_hinweis = (
            f"Version {neuester_entwurf.version}, Rechtsordnung {neuester_entwurf.rechtsordnung} "
            "(ENTWURF, noch nicht freigegeben)"
        )

    index_klausel_hinweis = None
    freigegebene_klausel = deps._indexautomatik.index_repository.freigegebene_klausel(vertrag_id)
    if freigegebene_klausel is not None:
        index_klausel_hinweis = (
            f"Freigegeben: {freigegebene_klausel.basis_reihe} Basis {freigegebene_klausel.basis_wert} "
            f"(Bezugsmonat {freigegebene_klausel.basis_monat})"
        )

    index_pruefbedarf_hinweis = None
    pruefbedarf_liste = deps._vertragspruefung_service.liste_index_pruefbedarf(vertrag_id)
    if pruefbedarf_liste:
        neuester_bedarf = pruefbedarf_liste[0]
        index_pruefbedarf_hinweis = (
            f"{len(pruefbedarf_liste)} Eintrag/Einträge, zuletzt {neuester_bedarf.basis_reihe or '—'} "
            f"{neuester_bedarf.basis_wert if neuester_bedarf.basis_wert is not None else ''} "
            f"({neuester_bedarf.basis_monat or 'kein Monat angegeben'}) - {neuester_bedarf.kommentar or 'ohne Kommentar'}"
        )

    letzte_pruefung_hinweis = None
    letzte_pruefung = deps._vertragspruefung_repo.aktuelle(vertrag_id)
    if letzte_pruefung is not None:
        letzte_pruefung_hinweis = (
            f"Version {letzte_pruefung.version}, Rechtsordnung {letzte_pruefung.rechtsordnung}, "
            f"Status {letzte_pruefung.fachstatus} (Beleg: {letzte_pruefung.quellenbeleg_referenz})"
        )

    # Letzter tatsächlicher monatlicher Indexautomatik-Lauf (reiner Read,
    # KEIN neuer Lauf/keine neue Berechnung) - Codex-Rückprüfung: eine
    # Vertragsprüfung ist KEIN Monatsindexlauf, ein BLOCKIERT-Lauf blieb
    # bisher im Akte-Rendering unsichtbar.
    letzter_indexautomatik_lauf_hinweis = None
    indexautomatik_laeufe = deps._indexautomatik.lauf_repository.liste_fuer_vertrag(vertrag_id)
    if indexautomatik_laeufe:
        letzter_lauf = indexautomatik_laeufe[0]
        blockiergruende_text = (
            f", Blockiergründe: {', '.join(letzter_lauf.blockiert_gruende)}" if letzter_lauf.blockiert_gruende else ""
        )
        letzter_indexautomatik_lauf_hinweis = f"Periode {letzter_lauf.periode}, Status {letzter_lauf.status}{blockiergruende_text}"

    # Tatsächlich persistierte Vorschreibungsdatensätze samt
    # Einzelpositionen (Auftrag HV-20260914-AUFGABEN-MIETERAKTE,
    # Ergänzung Codex-Bestandsprüfung) - NICHT nur die vereinbarten
    # Komponenten. Auswahl des "aktuellen" Monats erfolgt im Renderer
    # anhand von `aktueller_monat`, NICHT durch bloße DESC-Sortierung
    # (ein weit in der Zukunft hinterlegter Entwurf wäre sonst
    # fälschlich "aktuell").
    heute = heute_wien()
    aktueller_monat = f"{heute.year:04d}-{heute.month:02d}"
    vorschreibungen = deps._vorschreibung_repo.list_fuer_vertrag(vertrag_id, limit=24)
    vorschreibung_positionen_je_id = {v.id: deps._vorschreibung_repo.list_positionen(v.id) for v in vorschreibungen}

    uebersicht = berechne_rueckstandsuebersicht(
        ctx=ctx, objekt_id=objekt.id, stammdaten_repository=deps._stammdaten_repo, op_service=deps._op_service,
        mahn_fall_repository=deps._mahn_fall_repo,
    )
    kontostatus = next((z for z in uebersicht.mietkonten if z.vertrag_id == vertrag_id), None)
    unbekannte_faelligkeit_positionen = [
        p for p in uebersicht.offene_positionen if p.vertrag_id == vertrag_id and p.faelligkeitsklasse == "UNBEKANNT"
    ]
    mahnfaelle = [m for m in uebersicht.mahnfaelle if m.vertrag_id == vertrag_id]

    zahlungen_positionen = deps._op_service.berechne_saldo(konto.id).positionen if konto else []
    # Auftrag HV-20261022-EIN-KONTOBLATT: die Mieterakte verwendet
    # GENAU dasselbe `Kontoblatt`/chronologische Journal wie Kontoansicht
    # und PDF (`lade_kontoblatt`, `backoffice/kontoblatt_render.py`) -
    # keine eigene, abweichende zweite Buchungsübersicht mehr.
    kontoblatt = (
        lade_kontoblatt(
            konto_id=konto.id, vertrag_id=vertrag_id, op_service=deps._op_service,
            mahnkosten_repo=deps._hv_mail.mahnkosten_repo, ruecklastschriftkosten_repo=deps._ruecklastschriftkosten_repo,
            mahnlauf_repo=deps._hv_mail.mahnlauf_repo, manueller_mahnversand_repo=deps._hv_mail.manueller_mahnversand_repo,
        ) if konto else None
    )
    sperren = deps._stammdaten_repo.aktive_sperren(vertrag_id)
    mahnlaeufe = deps._hv_mail.mahnlauf_repo.list_fuer_vertrag(vertrag_id)
    erhoehungsschreiben = deps._indexautomatik.outbox_repository.liste_fuer_vertrag(vertrag_id)
    vertragsende_erinnerungen = deps._indexautomatik.vertragsende_repository.list_fuer_vertrag(vertrag_id)
    mitmieter = deps._mitmieter_repo.list_aktiv(vertrag_id)

    inhalt = _vertragsanlage_detail_ansicht(
        vertrag=vertrag, objekt=objekt, einheit=einheit, debitor=debitor, gesellschaft=gesellschaft,
        profil=profil, kaution=kaution, konto_id=konto.id if konto else None, komponenten=komponenten,
        rechtsprofil_freigegeben_hinweis=rechtsprofil_freigegeben_hinweis,
        rechtsprofil_entwurf_hinweis=rechtsprofil_entwurf_hinweis,
        index_klausel_hinweis=index_klausel_hinweis, index_pruefbedarf_hinweis=index_pruefbedarf_hinweis,
        letzter_indexautomatik_lauf_hinweis=letzter_indexautomatik_lauf_hinweis,
        letzte_pruefung_hinweis=letzte_pruefung_hinweis,
        versionen=versionen, csrf=session.csrf_token,
        aktueller_monat=aktueller_monat, vorschreibungen=vorschreibungen,
        vorschreibung_positionen_je_id=vorschreibung_positionen_je_id,
        kontostatus=kontostatus, unbekannte_faelligkeit_positionen=unbekannte_faelligkeit_positionen,
        zahlungen_positionen=zahlungen_positionen, kontoblatt=kontoblatt, sperren=sperren, mahnfaelle=mahnfaelle,
        mahnlaeufe=mahnlaeufe, erhoehungsschreiben=erhoehungsschreiben,
        vertragsende_erinnerungen=vertragsende_erinnerungen, von_objekt=von_objekt,
        mitmieter=mitmieter,
    )
    # Mahnbrief-Archiv + Anbieterbeleg-Import (Auftrag HV-20261019-
    # VERSAND-KOSTEN-ABSCHLUSS, Teil 3) - eigene, separate Seite statt
    # hier mitgerendert, nur ein Verweis aus der Mieterakte heraus.
    inhalt += (
        f'<div class="card"><h3>Mahnbrief-Archiv (Briefkanal)</h3>'
        f'<p><a href="/backoffice/vertrag/{vertrag_id}/mahnbrief-archiv">Archivierte Mahnbriefe, '
        "Original-PDF-Download und Anbieterbeleg-Import ansehen</a></p></div>"
    )
    return _layout(request, session, f"Mieterakte {debitor.name}", inhalt)


@router.post("/vertrag/{vertrag_id}/telefon")
def vertragsanlage_debitor_telefon(
    request: Request,
    vertrag_id: str,
    telefon: str = Form(""),
    csrf_token: str = Form(...),
    session=Depends(_current_session),
):
    """Pflegt AUSSCHLIESSLICH die Telefonnummer des Mieters dieses
    Vertrags (Auftrag HV-20260930-PORTAL-LISTEN) - bewusst NICHT über die
    Intake-Strecke der Vertragsanlage: eine Kontaktangabe braucht keine
    neue Profilversion, und Name/E-Mail/Adresse werden hier gar nicht
    angefasst. Ein leeres Feld löscht die Nummer ausdrücklich."""

    _verify_csrf(session, csrf_token)
    ctx = _ctx(session)
    require_schreibrecht(ctx)
    vertrag = deps._stammdaten_repo.get_vertrag(vertrag_id)
    if vertrag is None or _objekt_fuer_vertrag_gesperrt(vertrag_id):
        return _fehlerseite(session, "Mieterakte", "Vertrag nicht verfügbar.", "/backoffice/vertraege")
    require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
    try:
        geaendert = deps._stammdaten_repo.set_debitor_telefon(debitor_id=vertrag.debitor_id, telefon=telefon)
        if geaendert:
            # Die Nummer selbst steht bewusst NICHT im Audit-Protokoll.
            deps._audit_service.log(
                entity_typ="debitor", entity_id=vertrag.debitor_id, aktion="TELEFON_GEAENDERT", akteur=session.user_id,
                payload={"vertrag_id": vertrag_id, "telefon_hinterlegt": bool(telefon.strip())},
            )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Mieterakte", str(exc), f"/backoffice/vertrag/{vertrag_id}")
    return RedirectResponse(f"/backoffice/vertrag/{vertrag_id}", status_code=303)


@router.post("/vertrag/{vertrag_id}/parteien")
def vertragsanlage_partei_hinzufuegen(
    request: Request,
    vertrag_id: str,
    csrf_token: str = Form(...),
    rolle: str = Form("MITMIETER"),
    name: str = Form(""),
    email: str = Form(""),
    telefon: str = Form(""),
    adresse: str = Form(""),
    zustand: str = Form("BESTAETIGT"),
    debitor_id: str = Form(""),
    session=Depends(_current_session),
):
    """Pflegt die EINE führende Parteien-/Vertreter-Struktur
    (`VertragMitmieterTable` über `deps._mitmieter_repo`) direkt, OHNE
    die generische Intake-Strecke - analog zur Telefon-Route oben: eine
    zusätzliche Partei/ein Vertreter braucht keine neue Mietvertragsprofil-
    Version. `HAUPTMIETER` wird hier bewusst NICHT angeboten (siehe
    `Vertragsparteirolle`-Docstring - der Debitor bleibt die einzige
    Quelle für den Hauptmieter, keine Dublette mit eigenen Kontaktfeldern)."""

    _verify_csrf(session, csrf_token)
    ctx = _ctx(session)
    require_schreibrecht(ctx)
    vertrag = deps._stammdaten_repo.get_vertrag(vertrag_id)
    if vertrag is None or _objekt_fuer_vertrag_gesperrt(vertrag_id):
        return _fehlerseite(session, "Mieterakte", "Vertrag nicht verfügbar.", "/backoffice/vertraege")
    require_gesellschaft_access(ctx, vertrag.gesellschaft_id)

    if rolle not in ("MITMIETER", "VERTRETER"):
        return _fehlerseite(session, "Partei hinzufügen", "Ungültige Rolle (Hauptmieter wird über den Debitor geführt, nicht hier dupliziert).", f"/backoffice/vertrag/{vertrag_id}")
    if zustand not in ("AUSGELESEN", "BESTAETIGT", "FEHLT", "MEHRDEUTIG"):
        return _fehlerseite(session, "Partei hinzufügen", "Ungültiger Zustand.", f"/backoffice/vertrag/{vertrag_id}")
    name = name.strip() or None
    if not name:
        return _fehlerseite(session, "Partei hinzufügen", "Name darf nicht leer sein.", f"/backoffice/vertrag/{vertrag_id}")

    deps._mitmieter_repo.anlegen(
        vertrag_id=vertrag_id, name=name, email=email.strip() or None, adresse=adresse.strip() or None,
        telefon=normalisiere_telefon(telefon) if telefon.strip() else None,
        rolle=rolle, zustand=zustand, debitor_id=debitor_id.strip() or None,
        quelle_typ="BACKOFFICE_MANUELL", erstellt_von=session.user_id,
    )
    deps._audit_service.log(
        entity_typ="vertrag_mitmieter", entity_id=vertrag_id, aktion="PARTEI_HINZUGEFUEGT", akteur=session.user_id,
        payload={"vertrag_id": vertrag_id, "rolle": rolle, "zustand": zustand},
    )
    return RedirectResponse(f"/backoffice/vertrag/{vertrag_id}", status_code=303)


@router.post("/vertragsanlage/pdf-hochladen", response_class=HTMLResponse)
async def vertragsanlage_pdf_hochladen(
    request: Request,
    modus: str = Form(...),
    csrf_token: str = Form(...),
    vertrag_id: str = Form(""),
    einheit_id: str = Form(""),
    debitor_id: str = Form(""),
    gesellschaft_id: str = Form(""),
    rechtsordnung: str = Form(""),
    gueltig_von: str = Form(""),
    gueltig_bis: str = Form(""),
    neuer_debitor_id: str = Form(""),
    neuer_debitor_name: str = Form(""),
    neuer_debitor_email: str = Form(""),
    neuer_debitor_adresse: str = Form(""),
    neuer_debitor_telefon: str = Form(""),
    komponente_hmz: str = Form(""),
    komponente_hmz_netto: str = Form(""),
    komponente_hmz_ust: str = Form(""),
    komponente_bk: str = Form(""),
    komponente_bk_netto: str = Form(""),
    komponente_bk_ust: str = Form(""),
    komponente_hk: str = Form(""),
    komponente_hk_netto: str = Form(""),
    komponente_hk_ust: str = Form(""),
    komponente_kueche: str = Form(""),
    komponente_kueche_netto: str = Form(""),
    komponente_kueche_ust: str = Form(""),
    komponente_parkplatz: str = Form(""),
    komponente_parkplatz_netto: str = Form(""),
    komponente_parkplatz_ust: str = Form(""),
    mitmieter_name: str = Form(""),
    mitmieter_email: str = Form(""),
    mitmieter_telefon: str = Form(""),
    pdf_datei: UploadFile | None = File(None),
    session=Depends(_current_session),
) -> HTMLResponse:
    _verify_csrf(session, csrf_token)
    require_schreibrecht(_ctx(session))
    ist_neu = modus == "NEU"
    komponenten_roh_felder = {
        "komponente_hmz": komponente_hmz, "komponente_hmz_netto": komponente_hmz_netto, "komponente_hmz_ust": komponente_hmz_ust,
        "komponente_bk": komponente_bk, "komponente_bk_netto": komponente_bk_netto, "komponente_bk_ust": komponente_bk_ust,
        "komponente_hk": komponente_hk, "komponente_hk_netto": komponente_hk_netto, "komponente_hk_ust": komponente_hk_ust,
        "komponente_kueche": komponente_kueche, "komponente_kueche_netto": komponente_kueche_netto, "komponente_kueche_ust": komponente_kueche_ust,
        "komponente_parkplatz": komponente_parkplatz, "komponente_parkplatz_netto": komponente_parkplatz_netto, "komponente_parkplatz_ust": komponente_parkplatz_ust,
    }
    mitmieter_roh_felder = {"mitmieter_name": mitmieter_name, "mitmieter_email": mitmieter_email, "mitmieter_telefon": mitmieter_telefon}

    if ist_neu:
        try:
            kontext = _vertragsanlage_neu_kontext_werte({
                "vertrag_id": vertrag_id, "einheit_id": einheit_id, "debitor_id": debitor_id,
                "gesellschaft_id": gesellschaft_id, "rechtsordnung": rechtsordnung,
                "gueltig_von": gueltig_von, "gueltig_bis": gueltig_bis,
                "neuer_debitor_id": neuer_debitor_id, "neuer_debitor_name": neuer_debitor_name,
                "neuer_debitor_email": neuer_debitor_email, "neuer_debitor_adresse": neuer_debitor_adresse,
                "neuer_debitor_telefon": neuer_debitor_telefon,
            })
        except ValueError as exc:
            return _fehlerseite(session, "Neuer Mietvertrag", str(exc), "/backoffice/vertraege/neu")
        vertrag_id = kontext["vertrag_id"]
        einheit = deps._stammdaten_repo.get_einheit(kontext["einheit_id"])
        if einheit is None:
            return _fehlerseite(session, "Neuer Mietvertrag", "Unbekannte Einheit.", "/backoffice/vertraege/neu")
        objekt = deps._stammdaten_repo.get_objekt(einheit.objekt_id)
        if objekt is None or objekt.ausgeschlossen or objekt.gesellschaft_id != kontext["gesellschaft_id"]:
            return _fehlerseite(session, "Neuer Mietvertrag", "Einheit gehört nicht zur gewählten Gesellschaft oder ist gesperrt.", "/backoffice/vertraege/neu")
        if deps._stammdaten_repo.get_vertrag(vertrag_id) is not None:
            return _fehlerseite(session, "Neuer Mietvertrag", f"Vertrag-ID '{vertrag_id}' existiert bereits.", "/backoffice/vertraege/neu")
        if kontext["neuer_debitor"] is None and deps._stammdaten_repo.get_debitor(kontext["debitor_id"]) is None:
            return _fehlerseite(session, "Neuer Mietvertrag", "Unbekannter Mieter.", "/backoffice/vertraege/neu")
        require_gesellschaft_access(_ctx(session), kontext["gesellschaft_id"])
        basis_werte = {}
        # Abnahme-Folgefix (Codex, PDF-zuerst-Weg): Vertragsbeginn/-ende
        # und Mietbestandteile werden jetzt wie die neuer_debitor_*-Felder
        # als SICHTBARE, editierbare Werte behandelt statt blind in
        # `kontext_hidden` durchgereicht - nur so kann der PDF-Merge unten
        # ein erkanntes `urspruenglicher_mietbeginn` tatsächlich als
        # Vorschlag für `gueltig_von` einsetzen (siehe weiter unten).
        basis_werte["gueltig_von"] = kontext["gueltig_von"] or ""
        basis_werte["gueltig_bis"] = kontext["gueltig_bis"] or ""
        # Abnahme-Folgefix (Codex, Endabnahme-Rückmeldung): die
        # VERBINDLICHE Rechtsordnung ist jetzt ebenfalls ein sichtbares,
        # editierbares Review-Feld (siehe `review_formular`) statt eines
        # reinen Hidden-Durchreichens aus Schritt 1.
        basis_werte["rechtsordnung"] = kontext["rechtsordnung"]
        basis_werte.update(komponenten_roh_felder)
        basis_werte.update(mitmieter_roh_felder)
        if kontext["neuer_debitor"] is not None:
            # Bereits in Schritt 1 eingegebene Kontaktangaben eines neuen
            # Mieters bleiben führend - der PDF-Merge unten befüllt NUR
            # noch leere Felder (siehe "bestehende Angaben nicht
            # zurücksetzen").
            basis_werte.update({
                "neuer_debitor_name": kontext["neuer_debitor"]["name"],
                "neuer_debitor_email": kontext["neuer_debitor"].get("email") or "",
                "neuer_debitor_adresse": kontext["neuer_debitor"].get("adresse") or "",
                "neuer_debitor_telefon": kontext["neuer_debitor"].get("telefon") or "",
            })
        kaution_bereits_vorhanden = False
    else:
        vertrag = deps._stammdaten_repo.get_vertrag(vertrag_id)
        if vertrag is None or _objekt_fuer_vertrag_gesperrt(vertrag_id):
            return _fehlerseite(session, "Mietvertragsprofil", "Vertrag nicht verfügbar.", "/backoffice/vertraege")
        require_gesellschaft_access(_ctx(session), vertrag.gesellschaft_id)
        profil = deps._stammdaten_repo.neuestes_mietvertragsprofil(vertrag_id)
        kaution = deps._stammdaten_repo.get_kaution(vertrag_id)
        basis_werte = _vertragsanlage_bestehende_werte(profil, kaution)
        kaution_bereits_vorhanden = kaution is not None
        kontext = None

    vorschlaege: dict = {}
    mehrdeutigkeiten: dict = {}
    warnungen: tuple = ()
    quelle_typ = "MANUELL"
    quelle_referenz: str | None = None
    if pdf_datei is not None and pdf_datei.filename:
        try:
            rohbytes = await _lese_begrenzt(pdf_datei, deps._settings.vertragsanlage_max_upload_bytes)
        except _VertragsanlageUploadAbgelehntError as exc:
            return _fehlerseite(
                session, "PDF-Aufnahme", str(exc),
                "/backoffice/vertraege/neu" if ist_neu else f"/backoffice/vertrag/{vertrag_id}/mietvertragsprofil/bearbeiten",
            )
        try:
            dokument = _vertragsanlage_speichern(
                rohbytes, konfiguriertes_verzeichnis=deps._settings.vertragsanlage_upload_verzeichnis,
                max_bytes=deps._settings.vertragsanlage_max_upload_bytes,
            )
            ergebnis = _vertragsanlage_extrahiere(rohbytes, max_seiten=deps._settings.vertragsanlage_max_seiten)
        except (_VertragsanlageUploadAbgelehntError, _VertragsanlagePdfNichtLesbarError) as exc:
            return _fehlerseite(
                session, "PDF-Aufnahme", str(exc),
                "/backoffice/vertraege/neu" if ist_neu else f"/backoffice/vertrag/{vertrag_id}/mietvertragsprofil/bearbeiten",
            )
        vorschlaege = _vertragsanlage_vorschlaege_aus_extraktion(ergebnis)
        mehrdeutigkeiten = _vertragsanlage_mehrdeutigkeiten_aus_extraktion(ergebnis)
        warnungen = ergebnis.warnungen
        quelle_typ = "PDF_EXTRAKTION"
        quelle_referenz = f"pdf-sha256:{dokument.ablage_id}"

        # Abnahme-Folgefix (Codex, zweiter Praxistest, "DOPPELANLAGE"):
        # Vertrag-ID/neue Debitor-ID werden beim PDF-zuerst-Weg intern
        # generiert (siehe `neu_kontext_werte`) - zwei unabhängige
        # Upload-Versuche DESSELBEN Dokuments für dieselbe Einheit
        # erzeugen dadurch bisher ZWEI frische IDs und damit zwei
        # Verträge/Debitoren für ein und dasselbe Mietverhältnis, weil
        # die bestehende "Vertrag-ID existiert bereits"-Prüfung nie
        # anschlägt. Ein belegter Wiederupload (gleicher Dokumenthash +
        # gleiche Einheit) wird deshalb HIER erkannt und auf die
        # bestehende Akte verwiesen - KEINE zweite abrechenbare
        # Verpflichtung, KEINE Löschung historischer Daten.
        if ist_neu:
            bestehender_vertrag = deps._stammdaten_repo.finde_vertrag_mit_quelle_referenz(
                einheit_id=kontext["einheit_id"], quelle_referenz=quelle_referenz,
            )
            if bestehender_vertrag is not None:
                return _fehlerseite(
                    session, "Neuer Mietvertrag",
                    f"Dieses Dokument wurde für diese Einheit bereits verwendet - Vertrag "
                    f"'{bestehender_vertrag.id}' ist bereits daraus angelegt. Es wird KEIN zweiter Vertrag "
                    "erzeugt; bitte die bestehende Akte prüfen/bearbeiten statt erneut hochzuladen.",
                    f"/backoffice/vertrag/{bestehender_vertrag.id}",
                )

    werte = dict(basis_werte)
    _REINE_HINWEISE = (
        "mieter_name_hinweis", "mieter_email_hinweis", "mieter_telefon_hinweis", "mieter_adresse_hinweis",
        "vermieter_name_hinweis", "mitmieter_name_hinweis", "mitmieter_telefon_hinweis", "mitmieter_email_hinweis",
    )
    for feld, vorschlag in vorschlaege.items():
        if feld in _REINE_HINWEISE:
            continue  # reine Hinweise, kein Mietvertragsprofil-Feld (siehe Sonderbehandlung unten/Eckdaten-Block)
        if not werte.get(feld):
            werte[feld] = vorschlag.formularwert

    # Abnahme-Folgefix (Codex, zweiter Praxistest, "Quellenbelege/Termine
    # weiterreichen"): JEDER `FeldTreffer` trägt bereits Seite+Textauszug
    # (`Vorschlag.seite`/`.auszug`), aber nur der reine WERT wurde bisher
    # ins Review übernommen - der Beleg selbst ging verloren, obwohl die
    # meisten betroffenen Felder ein eigenes, im Formular tatsächlich
    # gerendertes `*_quellenbeleg`-Pendant haben. Nur Felder aus dieser
    # festen Allowlist werden befüllt (ein beliebiger Tippfehler soll
    # keinen toten Formularschlüssel erzeugen).
    _QUELLENBELEG_FELDER = (
        "vertragliche_kaution_cent", "mahngebuehr_cent", "unterzeichnungsdatum", "befristungsstatus",
        "rechtsachse_mrg_anwendungsbereich", "rechtsachse_mietzinsbildung", "bk_hk_abrechnungsart",
    )
    for feld in _QUELLENBELEG_FELDER:
        vorschlag = vorschlaege.get(feld)
        beleg_feld = f"{feld}_quellenbeleg"
        if vorschlag is not None and not werte.get(beleg_feld):
            werte[beleg_feld] = f"Seite {vorschlag.seite}: {vorschlag.auszug}"

    # Index-/Verzugszins-Klauseltext+Seite sind je EIN gemeinsames
    # Feldpaar für die GESAMTE Klausel (nicht je Einzelwert) - befüllt
    # aus dem jeweils aussagekräftigsten vorliegenden Einzeltreffer.
    if not werte.get("index_klauseltext_auszug"):
        for quellfeld in ("index_schwelle_prozent", "index_urspruenglicher_basiswert", "index_urspruenglicher_basismonat"):
            vorschlag = vorschlaege.get(quellfeld)
            if vorschlag is not None:
                werte["index_klauseltext_auszug"] = vorschlag.auszug
                werte["index_klauseltext_seite"] = str(vorschlag.seite)
                break
    if not werte.get("verzugszins_klauseltext_auszug") and "verzugszins_satz_prozent" in vorschlaege:
        vorschlag = vorschlaege["verzugszins_satz_prozent"]
        werte["verzugszins_klauseltext_auszug"] = vorschlag.auszug
        werte["verzugszins_klauseltext_seite"] = str(vorschlag.seite)

    # Abnahme-Folgefix (Codex, zweiter Praxistest, "Zinsprofil gueltig_ab
    # bleibt leer trotz neuem Mietbeginn"): die Zinsprofil-Form braucht
    # ein eigenes, vom Unterschriftsdatum GETRENNTES "gültig ab"
    # (Wirksamkeit DIESER Zinsprofil-Version) - sinnvoller Standardwert
    # dafür ist der tatsächliche Mietbeginn, NICHT das Unterschriftsdatum
    # (das bleibt eigens als `unterzeichnungsdatum` erhalten). Wird hier
    # NICHT ins Mietvertragsprofil-Formular geschrieben (das kennt dieses
    # Feld nicht), sondern bei der Zinsprofil-Anlage selbst vorbefüllt -
    # siehe `zinsprofil_form.py`/`routes/zinsprofile.py`.

    # Abnahme-Folgefix (Codex 20.10.2026): "Name bleibt Hinweis" -
    # richtige Mieterkontakte müssen tatsächlich editierbar geprüft
    # werden können, nicht nur als Vergleichstext neben einem BEREITS
    # gewählten bestehenden Debitor erscheinen. Wird GERADE ein NEUER
    # Mieter angelegt (`kontext["neuer_debitor"]` gesetzt), werden Name/
    # E-Mail/Telefon aus dem PDF deshalb DIREKT zu editierbaren
    # Vorbelegungen von dessen Kontaktfeldern (nur wenn noch nicht
    # manuell befüllt - Auftrag: "bestehende Angaben nicht zurücksetzen").
    # Für einen BEREITS bestehenden, ausgewählten Debitor bleibt es beim
    # reinen Vergleichshinweis (`mieter_name_hinweis` in der Eckdaten-
    # Karte) - dessen Kontaktdaten werden hier NIE überschrieben.
    if ist_neu and kontext["neuer_debitor"] is not None:
        if not werte.get("neuer_debitor_name") and "mieter_name_hinweis" in vorschlaege:
            werte["neuer_debitor_name"] = vorschlaege["mieter_name_hinweis"].formularwert
        if not werte.get("neuer_debitor_email") and "mieter_email_hinweis" in vorschlaege:
            werte["neuer_debitor_email"] = vorschlaege["mieter_email_hinweis"].formularwert
        if not werte.get("neuer_debitor_telefon") and "mieter_telefon_hinweis" in vorschlaege:
            werte["neuer_debitor_telefon"] = vorschlaege["mieter_telefon_hinweis"].formularwert
        if not werte.get("neuer_debitor_adresse") and "mieter_adresse_hinweis" in vorschlaege:
            werte["neuer_debitor_adresse"] = vorschlaege["mieter_adresse_hinweis"].formularwert

    if ist_neu:
        # Abnahme-Folgefix (Codex, "echter PDF-zuerst-Weg"): ein im
        # Dokument erkannter Mietbeginn wird zum editierbaren Vorschlag
        # für den (in Schritt 1 ABSICHTLICH optional gelassenen)
        # Vertragsbeginn - NIEMALS automatisch das heutige Datum
        # (`neu_kontext_werte`-Docstring). Bereits in Schritt 1
        # eingegebene Werte bleiben führend.
        if not werte.get("gueltig_von") and "urspruenglicher_mietbeginn" in vorschlaege:
            werte["gueltig_von"] = vorschlaege["urspruenglicher_mietbeginn"].formularwert
        # Mitmieter-Hinweis wird - ANDERS als bei einer bestehenden Partei
        # (siehe Eckdaten-Block, reiner Vergleichshinweis) - für einen
        # GERADE neu entstehenden Vertrag direkt zum editierbaren
        # Vorschlag für die atomare Mitmieter-Anlage im selben Intake.
        if not werte.get("mitmieter_name") and "mitmieter_name_hinweis" in vorschlaege:
            werte["mitmieter_name"] = vorschlaege["mitmieter_name_hinweis"].formularwert
        if not werte.get("mitmieter_email") and "mitmieter_email_hinweis" in vorschlaege:
            werte["mitmieter_email"] = vorschlaege["mitmieter_email_hinweis"].formularwert
        if not werte.get("mitmieter_telefon") and "mitmieter_telefon_hinweis" in vorschlaege:
            werte["mitmieter_telefon"] = vorschlaege["mitmieter_telefon_hinweis"].formularwert

        # Abnahme-Folgefix (Codex, zweiter Praxistest): die Rechtsordnung
        # (Schritt 1, VOR dem PDF gewählt) entscheidet, ob das später
        # erstellbare Rechtsprofil/die Indexautomatik überhaupt rechnen
        # KANN - bleibt sie UNGEKLAERT, obwohl das Dokument eine klare
        # MRG-Einordnung nennt, bleibt die gesamte Berechnung blockiert,
        # ohne dass das im Review auffällt. Dies leitet die Rechtsordnung
        # NICHT automatisch ab (Auftrag: "Nicht Büro=>MRG frei ableiten")
        # - es macht den Widerspruch nur sichtbar, damit ein Mensch ihn
        # bewusst auflöst (zurück zu Schritt 1, richtige Rechtsordnung
        # wählen).
        if kontext["rechtsordnung"] == "UNGEKLAERT" and "rechtsachse_mrg_anwendungsbereich" in vorschlaege:
            warnungen = (
                *warnungen,
                "Das Dokument nennt eine MRG-Einordnung "
                f"({vorschlaege['rechtsachse_mrg_anwendungsbereich'].formularwert}), die Rechtsordnung aus "
                "Schritt 1 ist aber weiterhin UNGEKLAERT - ohne eine passende Rechtsordnung bleibt jede "
                "Rechtsprofil-/Indexberechnung blockiert. Bitte in Schritt 1 die zutreffende Rechtsordnung wählen.",
            )

    kontext_hidden = {"modus": modus, "vertrag_id": vertrag_id, "quelle_typ": quelle_typ}
    if quelle_referenz:
        kontext_hidden["quelle_referenz"] = quelle_referenz
    eckdaten = None
    if ist_neu:
        kontext_hidden.update({
            "einheit_id": kontext["einheit_id"], "debitor_id": kontext["debitor_id"],
            "gesellschaft_id": kontext["gesellschaft_id"],
            # "rechtsordnung" ist KEIN Hidden-Feld mehr - siehe
            # `review_formular`s neues, sichtbares/editierbares
            # Rechtsordnung-Auswahlfeld (gespeist aus `werte`).
        })
        if kontext["neuer_debitor"] is not None:
            # Abnahme-Folgefix (Codex 20.10.2026): Name/E-Mail/Telefon
            # sind jetzt SICHTBARE, editierbare Felder im Review (siehe
            # `werte` oben, befüllt aus Schritt 1 UND/ODER PDF-Hinweis) -
            # NUR die ID bleibt hier als technischer, in Schritt 1 bereits
            # fixierter Identifikator versteckt.
            kontext_hidden["neuer_debitor_id"] = kontext["neuer_debitor"]["id"]
            mieter_anzeige = f"{werte.get('neuer_debitor_name') or '(Name folgt)'} (neu: {kontext['neuer_debitor']['id']})"
        else:
            bestehender_debitor = deps._stammdaten_repo.get_debitor(kontext["debitor_id"])
            mieter_anzeige = bestehender_debitor.name if bestehender_debitor else kontext["debitor_id"]
        gesellschaft_obj = deps._stammdaten_repo.get_gesellschaft(kontext["gesellschaft_id"])
        eckdaten = {
            "vertrag_id": vertrag_id, "objekt_einheit_label": _vertragsanlage_einheit_label(objekt, einheit),
            "mieter_anzeige": mieter_anzeige, "gesellschaft_name": gesellschaft_obj.name if gesellschaft_obj else kontext["gesellschaft_id"],
            "rechtsordnung": werte.get("rechtsordnung") or kontext["rechtsordnung"],
            # Abnahme-Folgefix: zeigt den ggf. aus dem PDF vorbefüllten
            # Stand aus `werte`, NICHT mehr den in Schritt 1 (ABSICHTLICH
            # optional) eingegebenen Rohwert aus `kontext` - der
            # editierbare Review-Input unten bleibt die maßgebliche Stelle.
            "gueltig_von": werte.get("gueltig_von") or "",
            "gueltig_bis": werte.get("gueltig_bis") or "",
        }

    inhalt = _vertragsanlage_review_formular(
        ist_neu=ist_neu, kontext_hidden=kontext_hidden, werte=werte, vorschlaege=vorschlaege,
        warnungen=warnungen, csrf=session.csrf_token, aktion_url="/backoffice/vertragsanlage/vorschau",
        zurueck_href="/backoffice/vertraege/neu" if ist_neu else f"/backoffice/vertrag/{vertrag_id}",
        kaution_bereits_vorhanden=kaution_bereits_vorhanden, eckdaten=eckdaten, mehrdeutigkeiten=mehrdeutigkeiten,
    )
    return _layout(request, session, "Mietvertragsprofil prüfen", inhalt)


@router.post("/vertragsanlage/vorschau", response_class=HTMLResponse)
async def vertragsanlage_vorschau(request: Request, session=Depends(_current_session)) -> HTMLResponse:
    form = await request.form()
    _verify_csrf(session, str(form.get("csrf_token", "")))
    modus = str(form.get("modus", ""))
    ist_neu = modus == "NEU"
    vertrag_id = str(form.get("vertrag_id", "")).strip()
    if not vertrag_id:
        return _fehlerseite(session, "Vertragsanlage", "Fehlende Vertrag-ID.", "/backoffice/vertraege")

    ctx = _ctx(session)
    require_schreibrecht(ctx)

    bestehende_profil_version: int | None = None
    bestehende_kaution_vorhanden = False
    neuer_debitor: dict | None = None
    komponenten: list[dict] = []
    mitmieter: dict | None = None
    if ist_neu:
        neuer_vertrag = {
            "einheit_id": str(form.get("einheit_id", "")), "debitor_id": str(form.get("debitor_id", "")),
            "gesellschaft_id": str(form.get("gesellschaft_id", "")), "rechtsordnung": str(form.get("rechtsordnung", "")),
            "gueltig_von": str(form.get("gueltig_von", "")), "gueltig_bis": str(form.get("gueltig_bis", "")) or None,
        }
        # Abnahme-Folgefix (Codex, "vertraglicher Fälligkeitstag"): ein
        # belegter abweichender Zahlungstermin (z. B. Gewerbevertrag "bis
        # zum 1./15. des Monats") darf NICHT stillschweigend auf den
        # technischen Default 5 zurückfallen - ein leeres Feld bleibt
        # dennoch ausdrücklich der technische Standard, KEIN erfundener
        # Beleg (siehe Formular-Hinweistext).
        faelligkeit_tag_roh = str(form.get("faelligkeit_tag", "")).strip()
        if faelligkeit_tag_roh:
            try:
                faelligkeit_tag = int(faelligkeit_tag_roh)
            except ValueError:
                return _fehlerseite(session, "Vertragsanlage", "Fälligkeitstag muss eine Zahl sein.", "/backoffice/vertraege/neu")
            if not (1 <= faelligkeit_tag <= 28):
                return _fehlerseite(session, "Vertragsanlage", "Fälligkeitstag muss zwischen 1 und 28 liegen.", "/backoffice/vertraege/neu")
            neuer_vertrag["faelligkeit_tag"] = faelligkeit_tag
        # Abnahme-Folgefix (Codex, "echter PDF-zuerst-Weg"): der
        # Vertragsbeginn ist in Schritt 1 ABSICHTLICH optional
        # (`neu_kontext_werte`) und wird spätestens HIER, beim
        # tatsächlichen Übernahmeversuch, verbindlich verlangt - NIEMALS
        # automatisch auf das heutige Datum gesetzt.
        if not neuer_vertrag["gueltig_von"]:
            return _fehlerseite(
                session, "Vertragsanlage",
                "Vertragsbeginn fehlt - bitte im erkannten PDF-Vorschlag prüfen oder von Hand ergänzen "
                "(wird nie automatisch auf das heutige Datum gesetzt).",
                "/backoffice/vertraege/neu",
            )
        require_gesellschaft_access(ctx, neuer_vertrag["gesellschaft_id"])
        einheit = deps._stammdaten_repo.get_einheit(neuer_vertrag["einheit_id"])
        if einheit is None:
            return _fehlerseite(session, "Vertragsanlage", "Unbekannte Einheit.", "/backoffice/vertraege/neu")
        objekt = deps._stammdaten_repo.get_objekt(einheit.objekt_id)
        if objekt is None or objekt.ausgeschlossen or objekt.gesellschaft_id != neuer_vertrag["gesellschaft_id"]:
            return _fehlerseite(session, "Vertragsanlage", "Einheit gehört nicht zur gewählten Gesellschaft oder ist gesperrt.", "/backoffice/vertraege/neu")
        if deps._stammdaten_repo.get_vertrag(vertrag_id) is not None:
            return _fehlerseite(session, "Vertragsanlage", f"Vertrag-ID '{vertrag_id}' existiert bereits.", "/backoffice/vertraege/neu")

        # Abnahme-Folgefix (Codex 20.10.2026): Auslöser für "neuer Mieter"
        # ist jetzt die ID (siehe `vertragsanlage_form.py::neu_kontext_
        # werte`) - der NAME kann an DIESER Stelle (nach PDF-Prüfung/
        # -Korrektur im Review) noch fehlen und wird dann erst HIER,
        # beim tatsächlichen Übernahmeversuch, verbindlich verlangt.
        neuer_debitor_id = str(form.get("neuer_debitor_id", "")).strip()
        neuer_debitor_name = str(form.get("neuer_debitor_name", "")).strip()
        if neuer_debitor_id:
            if not neuer_debitor_name:
                return _fehlerseite(
                    session, "Vertragsanlage",
                    "Name des neuen Mieters darf nicht leer sein - bitte prüfen/ergänzen (ggf. aus dem PDF).",
                    "/backoffice/vertraege/neu",
                )
            neuer_debitor = {
                "id": neuer_debitor_id, "name": neuer_debitor_name,
                "email": str(form.get("neuer_debitor_email", "")).strip() or None,
                "adresse": str(form.get("neuer_debitor_adresse", "")).strip() or None,
                # Geprüft wird im Intake-Parser (ungültig -> Fehlerseite unten).
                "telefon": str(form.get("neuer_debitor_telefon", "")).strip() or None,
            }
            neuer_vertrag["debitor_id"] = neuer_debitor_id
        elif deps._stammdaten_repo.get_debitor(neuer_vertrag["debitor_id"]) is None:
            return _fehlerseite(session, "Vertragsanlage", "Unbekannter Mieter.", "/backoffice/vertraege/neu")

        try:
            komponenten = _vertragsanlage_komponenten_werte_aus_form(form, vertrag_id=vertrag_id, gueltig_von=neuer_vertrag["gueltig_von"])
        except ValueError as exc:
            return _fehlerseite(session, "Vertragsanlage", str(exc), "/backoffice/vertraege/neu")

        # Abnahme-Folgefix: "Mitmieter atomar im selben bestätigten
        # Intake" - ein optional mitgeschickter Mitmieter-Name wird in
        # GENAU DIESEM Paket (siehe `paket_bau.py::baue_paket_json`)
        # mitgeschrieben, NICHT über einen separaten Folge-POST.
        mitmieter_name = str(form.get("mitmieter_name", "")).strip()
        if mitmieter_name:
            mitmieter = {
                "name": mitmieter_name,
                "email": str(form.get("mitmieter_email", "")).strip() or None,
                "telefon": str(form.get("mitmieter_telefon", "")).strip() or None,
            }
        zurueck_href = "/backoffice/vertraege/neu"
    else:
        vertrag = deps._stammdaten_repo.get_vertrag(vertrag_id)
        if vertrag is None or _objekt_fuer_vertrag_gesperrt(vertrag_id):
            return _fehlerseite(session, "Vertragsanlage", "Vertrag nicht verfügbar.", "/backoffice/vertraege")
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        neuer_vertrag = None
        zurueck_href = f"/backoffice/vertrag/{vertrag_id}/mietvertragsprofil/bearbeiten"
        bestehendes_profil = deps._stammdaten_repo.neuestes_mietvertragsprofil(vertrag_id)
        bestehende_profil_version = bestehendes_profil.version if bestehendes_profil else None
        bestehende_kaution_vorhanden = deps._stammdaten_repo.get_kaution(vertrag_id) is not None

    try:
        profil_werte = _vertragsanlage_profil_werte_aus_form(form)
    except ValueError as exc:
        return _fehlerseite(session, "Vertragsanlage", str(exc), zurueck_href)

    kaution_werte = None if bestehende_kaution_vorhanden else profil_werte

    quelle_typ = str(form.get("quelle_typ", "MANUELL")) or "MANUELL"
    quelle_referenz = str(form.get("quelle_referenz", "")) or None

    paket_json = _vertragsanlage_baue_paket_json(
        quelle=f"backoffice-vertragsanlage:{session.user_id}", vertrag_id=vertrag_id, neuer_vertrag=neuer_vertrag,
        profil_werte=profil_werte, kaution_werte=kaution_werte, quelle_typ=quelle_typ, quelle_referenz=quelle_referenz,
        neuer_debitor=neuer_debitor, komponenten=komponenten, mitmieter=mitmieter,
    )
    try:
        paket = _intake_parse_json_paket(paket_json)
        _vertragsanlage_pruefe_schmale_form(paket, erwarteter_vertrag_id=vertrag_id)
    except (IntakeFormatFehlerError, _VertragsanlagePaketFormUngueltigError) as exc:
        return _fehlerseite(session, "Vertragsanlage", str(exc), zurueck_href)
    plan = _intake_erstelle_plan(paket, session_factory=deps._session_factory)

    # Serverseitiger Zwischenstand statt eines vom Browser mitgeschickten
    # Pakets (siehe `BackofficeSession.vertragsanlage_review`-Docstring) -
    # `uebernehmen` vertraut NIE einem client-seitig übertragenen Paket.
    session.vertragsanlage_review = {
        "paket_json": paket_json, "vertrag_id": vertrag_id, "ist_neu": ist_neu,
        "bestehende_profil_version": bestehende_profil_version,
        "bestehende_kaution_vorhanden": bestehende_kaution_vorhanden,
    }

    inhalt = _vertragsanlage_vorschau_ansicht(
        ist_neu=ist_neu, befunde=list(plan.befunde), anwendbar=plan.anwendbar, hinweise=plan.hinweise,
        paket_json=paket_json, csrf=session.csrf_token, aktion_url="/backoffice/vertragsanlage/uebernehmen",
        zurueck_href=zurueck_href,
    )
    return _layout(request, session, "Vorschau Vertragsanlage", inhalt)


@router.post("/vertragsanlage/uebernehmen", response_class=HTMLResponse)
async def vertragsanlage_uebernehmen(request: Request, session=Depends(_current_session)) -> HTMLResponse:
    form = await request.form()
    _verify_csrf(session, str(form.get("csrf_token", "")))
    ctx = _ctx(session)
    require_schreibrecht(ctx)

    review = session.vertragsanlage_review
    if review is None:
        return _fehlerseite(
            session, "Vertragsanlage",
            "Keine (mehr gültige) Vorschau auf dieser Sitzung vorhanden - bitte erneut prüfen.",
            "/backoffice/vertraege",
        )
    # Das im Formular mitgeschickte `paket_json` wird NIE verwendet -
    # ausschließlich der serverseitig bei der Vorschau gespeicherte Stand
    # dieser SITZUNG wird angewendet (siehe Docstring von
    # `BackofficeSession.vertragsanlage_review`).
    paket_json = review["paket_json"]
    vertrag_id = review["vertrag_id"]
    ist_neu = review["ist_neu"]

    try:
        paket = _intake_parse_json_paket(paket_json)
        _vertragsanlage_pruefe_schmale_form(paket, erwarteter_vertrag_id=vertrag_id)
    except (IntakeFormatFehlerError, _VertragsanlagePaketFormUngueltigError) as exc:
        session.vertragsanlage_review = None
        return _fehlerseite(session, "Vertragsanlage", f"Ungültiges Paket: {exc}", "/backoffice/vertraege")

    if ist_neu:
        gesellschaft_id = paket.vertraege[0].gesellschaft_id if paket.vertraege else None
        if gesellschaft_id is None:
            session.vertragsanlage_review = None
            return _fehlerseite(session, "Vertragsanlage", "Paket enthält keinen neuen Vertrag.", "/backoffice/vertraege")
        require_gesellschaft_access(ctx, gesellschaft_id)
        if deps._stammdaten_repo.get_vertrag(vertrag_id) is not None:
            session.vertragsanlage_review = None
            return _fehlerseite(
                session, "Vertragsanlage",
                f"Vertrag '{vertrag_id}' wurde inzwischen anderweitig angelegt - bitte neu prüfen.",
                "/backoffice/vertraege/neu",
            )
    else:
        bestehender_vertrag = deps._stammdaten_repo.get_vertrag(vertrag_id)
        if bestehender_vertrag is None or _objekt_fuer_vertrag_gesperrt(vertrag_id):
            session.vertragsanlage_review = None
            return _fehlerseite(session, "Vertragsanlage", "Vertrag nicht (mehr) verfügbar.", "/backoffice/vertraege")
        require_gesellschaft_access(ctx, bestehender_vertrag.gesellschaft_id)

        # Stand seit der Vorschau erneut prüfen: bei zwischenzeitlicher
        # Änderung wird NICHT blind auf dem alten Snapshot eine neue
        # Version erzeugt, sondern eine frische Prüfung verlangt.
        aktuelles_profil = deps._stammdaten_repo.neuestes_mietvertragsprofil(vertrag_id)
        aktuelle_profil_version = aktuelles_profil.version if aktuelles_profil else None
        if aktuelle_profil_version != review["bestehende_profil_version"]:
            session.vertragsanlage_review = None
            return _fehlerseite(
                session, "Vertragsanlage",
                "Das Mietvertragsprofil wurde seit der Vorschau geändert - bitte neu prüfen.",
                f"/backoffice/vertrag/{vertrag_id}/mietvertragsprofil/bearbeiten",
            )
        aktuelle_kaution_vorhanden = deps._stammdaten_repo.get_kaution(vertrag_id) is not None
        if aktuelle_kaution_vorhanden != review["bestehende_kaution_vorhanden"]:
            session.vertragsanlage_review = None
            return _fehlerseite(
                session, "Vertragsanlage",
                "Die Kaution wurde seit der Vorschau geändert - bitte neu prüfen.",
                f"/backoffice/vertrag/{vertrag_id}/mietvertragsprofil/bearbeiten",
            )

    plan = _intake_erstelle_plan(paket, session_factory=deps._session_factory)
    if not plan.anwendbar:
        session.vertragsanlage_review = None
        return _fehlerseite(
            session, "Vertragsanlage",
            "Der Stand hat sich seit der Vorschau geändert oder enthält ungeklärte Punkte; nichts wurde übernommen.",
            "/backoffice/vertraege",
        )
    try:
        ergebnis = _intake_wende_an(
            paket, bestaetigter_hash=plan.paket_hash, stammdaten_repo=deps._stammdaten_repo, op_service=deps._op_service,
            session_factory=deps._session_factory, akteur=session.user_id,
        )
        deps._audit_service.log(
            entity_typ="vertragsanlage", entity_id=vertrag_id, aktion="UEBERNOMMEN", akteur=session.user_id,
            payload={
                "anzahl_vertraege": ergebnis.anzahl_vertraege,
                "anzahl_mietvertragsprofile": ergebnis.anzahl_mietvertragsprofile,
                "anzahl_kautionen": ergebnis.anzahl_kautionen,
                "anzahl_mitmieter": ergebnis.anzahl_mitmieter,
            },
        )
    except (MietinkassoError, ValueError) as exc:
        session.vertragsanlage_review = None
        return _fehlerseite(session, "Vertragsanlage", f"Übernahme abgebrochen, NICHTS wurde gespeichert: {exc}", "/backoffice/vertraege")
    session.vertragsanlage_review = None
    return RedirectResponse(f"/backoffice/vertrag/{vertrag_id}", status_code=303)
