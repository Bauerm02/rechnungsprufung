"""Mahnvorschau, ausdrückliches Planen, Mahnbrief-PDF, Sendebereitschaft
und die Mahnstufen-Konfiguration (genau zwei Stufen).

Bankvollständigkeit und ungeklärte Eingänge werden IMMER serverseitig aus
den persistierten Bankdaten abgeleitet (`_bank_freigabe_ableiten`), nie
aus einem Formularfeld."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from html import escape as h

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.exc import IntegrityError

from mietinkasso.auth.service import require_gesellschaft_access, require_schreibrecht
from mietinkasso.backoffice.views import csrf_feld, eur, flash_error, flash_ok, parse_eur_betrag
from mietinkasso.domain.exceptions import MietinkassoError
from mietinkasso.indexautomatik.mailversand_service import bank_freigabe_ableiten
from mietinkasso.indexautomatik.zeit import heute_wien
from mietinkasso.mahnwesen.brief_pdf import (
    Absender as _BriefAbsender,
    AdressfehlerError as _BriefAdressfehlerError,
    Forderungszeile as _BriefForderungszeile,
    Zinssegment as _BriefZinssegment,
    erzeuge_mahnbrief_pdf as _erzeuge_mahnbrief_pdf,
)
from mietinkasso.mahnwesen.kosten import _profil_wirksam_am as _mahn_profil_wirksam_am
from mietinkasso.mahnwesen.kosten import forderungsgruppe_schluessel as _forderungsgruppe_schluessel
from mietinkasso.mahnwesen.kosten import zins_bis_einschliesslich as _zins_bis_einschliesslich
from mietinkasso.op.service import compute_content_hash as _compute_content_hash

from mietinkasso.backoffice import dependencies as deps
from mietinkasso.backoffice.auth import _ctx, _current_session, _fehlerseite, _layout, _objekt_fuer_vertrag_gesperrt, _verify_csrf


#: Ohne eigenes Prefix - das gemeinsame `/backoffice`-Prefix wird GENAU
#: EINMAL in `backoffice/app.py` gesetzt.
router = APIRouter()


#: Fällt NUR an, solange im Zinsprofil kein eigener, belegter
#: `mahnpauschale_kostenbasis_cent` hinterlegt ist - ein rein
#: unverbindlicher Eigentümer-Prüfwert zum Ausfüllen der Freigabemaske,
#: kein Beleg und keine Freigabe (ein marktüblicher Betrag beweist für
#: sich allein keine Angemessenheit nach §1333 Abs 2 ABGB, siehe
#: OFFENE_PUNKTE.md). Bindet nie automatisch: `tatsaechlicher_aufwand_cent`
#: und die Vertragsquelle bleiben davon unberührt und werden nie damit
#: vorbelegt.
_UNVERBINDLICHER_PRUEFWERT_CENT = 2000


# -- Mahnvorschau (nur Entwürfe, kein Versand) ------------------------------------


def _bank_freigabe_ableiten(gesellschaft_id: str, vertrag_id: str) -> tuple[date | None, bool]:
    """Leitet `bank_bestaetigt_bis`/`ungeklaerte_eingaenge_vorhanden`
    AUSSCHLIESSLICH aus persistierten, serverseitigen Bankdaten ab - die
    Oberfläche darf hierfür NIEMALS ein Formularfeld entgegennehmen
    (sonst könnte eine Vorschau eine Mahnung freischalten, die die echten
    Bankdaten nicht hergeben)."""

    return bank_freigabe_ableiten(deps._bank_repo, deps._bank_service, gesellschaft_id, vertrag_id)


@router.get("/vertrag/{vertrag_id}/mahnvorschau", response_class=HTMLResponse)
def mahnvorschau(request: Request, vertrag_id: str, heute: str | None = None, session=Depends(_current_session)) -> HTMLResponse:
    vertrag = deps._stammdaten_repo.get_vertrag(vertrag_id)
    if vertrag is None:
        return _fehlerseite(session, "Mahnvorschau", f"Unbekannter Vertrag {vertrag_id}.")
    if _objekt_fuer_vertrag_gesperrt(vertrag_id):
        return _fehlerseite(session, "Mahnvorschau", "Objekt ist gesperrt; keine Mahnung möglich.")
    konto = deps._stammdaten_repo.get_konto_by_vertrag(vertrag_id)
    if konto is None:
        return _fehlerseite(session, "Mahnvorschau", "Kein Konto für diesen Vertrag vorhanden.")
    heute_datum = date.fromisoformat(heute) if heute else date.today()
    policy = deps._mahn_policy_repo.aktuelle_freigegebene()

    form = f"""
    <form method="get" action="/backoffice/vertrag/{h(vertrag_id)}/mahnvorschau">
      <label>Heute (Simulationsdatum)</label>
      <input type="date" name="heute" value="{heute_datum.isoformat()}">
      <button type="submit">Neu berechnen</button>
    </form>"""
    inhalt = f'<div class="card"><h1>Mahnvorschau — {h(vertrag_id)}</h1>{form}</div>'

    if policy is None:
        inhalt += flash_error("Keine freigegebene MahnPolicy vorhanden; es kann nichts geplant werden.")
        return _layout(request, session, "Mahnvorschau", inhalt)

    bank_bestaetigt_bis, ungeklaert = _bank_freigabe_ableiten(vertrag.gesellschaft_id, vertrag_id)
    forderungen = deps._op_service.offene_forderungen(konto.id, heute=heute_datum)
    zeilen = []
    for forderung in forderungen:
        # Reine Lesevorschau OHNE Datenbankschreibzugriff (Auftrag Markus
        # 14.09.2026: ein GET darf keine Mahnfälle anlegen) - das
        # tatsächliche, dauerhafte Planen läuft NUR über den separaten,
        # CSRF-geschützten POST-Endpunkt weiter unten.
        ergebnis = deps._mahn_service.vorschau_forderung(
            ctx=_ctx(session), vertrag=vertrag, konto=konto, forderung=forderung, policy=policy, heute=heute_datum,
            bank_bestaetigt_bis=bank_bestaetigt_bis, ungeklaerte_eingaenge_vorhanden=ungeklaert,
        )
        aktion = ""
        status_anzeige = ergebnis.status
        if ergebnis.status == "GEPLANT" and ergebnis.mahnfall_id is None:
            # Noch nicht tatsächlich geplant (nur die reine Vorschau sagt
            # "dürfte geplant werden") - andere Anzeige als ein bereits
            # tatsächlich angelegter Fall, sonst nicht unterscheidbar
            # (Auftrag Markus 14.09.2026). Das dauerhafte Anlegen braucht
            # den ausdrücklich betätigten POST.
            status_anzeige = "Planbar"
            aktion = f"""
            <form method="post" action="/backoffice/vertrag/{h(vertrag_id)}/forderung/{forderung.op_position_id}/planen?heute={heute_datum.isoformat()}" class="inline">
              {csrf_feld(session.csrf_token)}
              <button type="submit">Jetzt planen</button>
            </form>"""
        elif ergebnis.status == "GEPLANT" and ergebnis.mahnfall_id is not None:
            # Bereits zuvor tatsächlich geplant (Lesezugriff auf einen
            # bestehenden Fall, siehe `vorschau_forderung`) - normale
            # Sendebereitschafts-/Versandaktionen wie bisher.
            aktion = f"""
            <form method="post" action="/backoffice/mahnfall/{ergebnis.mahnfall_id}/sendebereitschaft" class="inline">
              {csrf_feld(session.csrf_token)}
              <button type="submit" class="secondary">Sendebereitschaft prüfen (kein Versand)</button>
            </form>"""
            if deps._settings.send_enabled and deps._settings.hv_mail_allowlist_bestaetigt and deps._hv_mail.client:
                aktion += f"""<form method="post" action="/backoffice/mahnfall/{ergebnis.mahnfall_id}/versenden" class="inline">
                  {csrf_feld(session.csrf_token)}
                  <button type="submit">Mahnung senden</button>
                </form>"""
        zeilen.append(f"""
        <tr>
          <td>OP #{forderung.op_position_id}</td><td>{h(forderung.art)}</td><td>{eur(forderung.rest_cent)}</td>
          <td>{forderung.faelligkeit.isoformat() if forderung.faelligkeit else 'unbekannt'}</td>
          <td>{h(status_anzeige)}</td><td>{h(ergebnis.grund)}</td><td>{aktion}</td>
        </tr>""")

    bank_status = (
        f"Bank bestätigt bis {bank_bestaetigt_bis.isoformat()}" if bank_bestaetigt_bis else '<span class="warn">keine ausreichend aktuelle Bankbestätigung</span>'
    ) + (" | <span class='warn'>ungeklärte Eingänge vorhanden</span>" if ungeklaert else "")
    inhalt += f"""
    <div class="card">
      <p class="muted">Bankstatus: {bank_status}</p>
      <table>
        <tr><th>Forderung</th><th>Art</th><th>Rest</th><th>Fälligkeit</th><th>Status</th><th>Grund</th><th></th></tr>
        {''.join(zeilen) if zeilen else '<tr><td colspan=7 class="muted">Keine offenen Forderungen.</td></tr>'}
      </table>
      <p class="muted">Die Vorschau versendet keine Nachricht. Der separate Versand prüft unmittelbar davor den aktuellen Bank- und Forderungsstand.</p>
    </div>"""
    inhalt += _mahnkosten_vorschau_block(vertrag_id, heute_datum)
    return _layout(request, session, "Mahnvorschau", inhalt)


@router.post("/vertrag/{vertrag_id}/forderung/{op_position_id}/planen", response_class=HTMLResponse)
def forderung_planen(
    request: Request, vertrag_id: str, op_position_id: int, csrf_token: str = Form(...),
    heute: str | None = None, session=Depends(_current_session),
):
    """Dauerhaftes, ausdrücklich betätigtes Anlegen EINES Mahnfalls
    (Auftrag Markus 14.09.2026: "dauerhaftes Planen per ausdrücklich
    betätigtem POST mit CSRF") - GENAU DIESELBE Prüfung wie die GET-
    Vorschau (`vorschau_forderung`), aber über `plane_forderung`, das bei
    "GEPLANT" tatsächlich einen `MahnFallTable`-Eintrag anlegt (idempotent
    über den deterministischen `outbox_key` - ein erneuter Klick auf eine
    bereits geplante Forderung legt keinen zweiten Fall an)."""

    _verify_csrf(session, csrf_token)
    vertrag = deps._stammdaten_repo.get_vertrag(vertrag_id)
    if vertrag is None:
        return _fehlerseite(session, "Mahnvorschau", f"Unbekannter Vertrag {vertrag_id}.")
    if _objekt_fuer_vertrag_gesperrt(vertrag_id):
        return _fehlerseite(session, "Mahnvorschau", "Objekt ist gesperrt; keine Mahnung möglich.")
    konto = deps._stammdaten_repo.get_konto_by_vertrag(vertrag_id)
    if konto is None:
        return _fehlerseite(session, "Mahnvorschau", "Kein Konto für diesen Vertrag vorhanden.")
    heute_datum = date.fromisoformat(heute) if heute else date.today()
    policy = deps._mahn_policy_repo.aktuelle_freigegebene()
    if policy is None:
        return _fehlerseite(session, "Mahnvorschau", "Keine freigegebene MahnPolicy vorhanden; es kann nichts geplant werden.")

    forderung = next(
        (f for f in deps._op_service.offene_forderungen(konto.id, heute=heute_datum) if f.op_position_id == op_position_id),
        None,
    )
    if forderung is None:
        return _fehlerseite(
            session, "Mahnvorschau", f"Forderung #{op_position_id} ist nicht (mehr) offen.",
            f"/backoffice/vertrag/{vertrag_id}/mahnvorschau?heute={heute_datum.isoformat()}",
        )
    bank_bestaetigt_bis, ungeklaert = _bank_freigabe_ableiten(vertrag.gesellschaft_id, vertrag_id)
    try:
        ergebnis = deps._mahn_service.plane_forderung(
            ctx=_ctx(session), vertrag=vertrag, konto=konto, forderung=forderung, policy=policy, heute=heute_datum,
            bank_bestaetigt_bis=bank_bestaetigt_bis, ungeklaerte_eingaenge_vorhanden=ungeklaert,
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(
            session, "Mahnvorschau", str(exc), f"/backoffice/vertrag/{vertrag_id}/mahnvorschau?heute={heute_datum.isoformat()}",
        )
    if ergebnis.status != "GEPLANT":
        return _fehlerseite(
            session, "Mahnvorschau", f"Forderung #{op_position_id} ist inzwischen nicht mehr planbar: {ergebnis.grund}",
            f"/backoffice/vertrag/{vertrag_id}/mahnvorschau?heute={heute_datum.isoformat()}",
        )
    return RedirectResponse(
        url=f"/backoffice/vertrag/{vertrag_id}/mahnvorschau?heute={heute_datum.isoformat()}", status_code=303,
    )


def _mahnkosten_vorschau_block(vertrag_id: str, heute_datum: date) -> str:
    """Mahngebühren-/Verzugszinsenvorschau je Stufe (Auftrag Markus
    13.09.2026) - reine Anzeige, bucht nichts. Zeigt Hauptforderung,
    bereits gebuchte Nebenkosten, neue zulässige Gebühr und Zinsen
    (mit Satz/Zeitraum) GETRENNT je Mahnstufe, wie ausdrücklich
    gefordert. `_hv_mail.mahnkosten_service` ist dieselbe Instanz, die
    auch den tatsächlichen Versand bucht (siehe
    `indexautomatik/mailversand_service.py`)."""

    zeilen = []
    for stufe in (1, 2):
        kanal = deps._hv_mail.mahn_service._resolve_kanal(stufe)
        vorschau = deps._hv_mail.mahnkosten_service.vorschau(vertrag_id=vertrag_id, stufe=stufe, heute=heute_datum, kanal=kanal)
        if vorschau is None:
            continue
        satz_text = f"{vorschau.zinssatz_prozent} % p.a." if vorschau.zinssatz_prozent is not None else "ungeklärt"
        # `zins_bis` ist EXKLUSIV (siehe `kosten.py::ZinsSegment`-
        # Docstring) - angezeigt wird der tatsächlich letzte verzinste
        # Tag, nie das exklusive Enddatum selbst.
        zeitraum_text = (
            f"{vorschau.zins_von.isoformat()} – {_zins_bis_einschliesslich(vorschau.zins_bis).isoformat()} (einschließlich)"
            if vorschau.zins_von and vorschau.zins_bis else "–"
        )
        gebuehr_text = eur(vorschau.gebuehr_cent) if vorschau.gebuehr_cent is not None else "keine (bereits erhoben oder ungeklärt)"
        mahnpauschale_zeile = f"""
            <tr><th>Mahnkostenpauschale (§1333 Abs 2, Einzelfallfreigabe)</th>
                <td>{eur(vorschau.mahnpauschale_cent) if vorschau.mahnpauschale_cent is not None else '0,00 € - keine gültige Freigabe'}
                &nbsp;<a href="/backoffice/vertrag/{h(vertrag_id)}/mahnpauschale-freigabe?stufe={stufe}&heute={heute_datum.isoformat()}">Freigabe prüfen/erteilen</a></td></tr>"""
        hinweise_html = "".join(f"<li>{h(hw)}</li>" for hw in vorschau.hinweise)
        ausgeschlossen_html = "".join(f"<li class='warn'>{h(hw)}</li>" for hw in vorschau.ausgeschlossene_forderungen_hinweis)
        segmente_html = ""
        if len(vorschau.zins_segmente) > 1 or vorschau.zins_teilweise_ungeklaert:
            def _satz_zelle(s):
                if s.satz_prozent is None:
                    return "<span class=\"warn\">ungeklärt</span>"
                return h(f"{s.satz_prozent} %")
            zeilen_segmente = "".join(
                f"<tr><td>{s.von.isoformat()} – {_zins_bis_einschliesslich(s.bis).isoformat()}</td><td>{eur(s.rest_cent)}</td>"
                f"<td>{_satz_zelle(s)}</td>"
                f"<td>{eur(s.zinsen_cent)}</td><td>{h(s.quelle)}</td></tr>"
                for s in vorschau.zins_segmente
            )
            segmente_html = f"""<details><summary>Zinssegmente ({len(vorschau.zins_segmente)}, je Forderung/Halbjahr)</summary>
              <table><tr><th>Zeitraum (bis einschließlich)</th><th>Basis</th><th>Satz</th><th>Zinsen</th><th>Quelle</th></tr>{zeilen_segmente}</table>
            </details>"""
        gebuehr_segmente_html = ""
        if len(vorschau.gebuehr_segmente) > 1:
            zeilen_gebuehr = "".join(
                f"<tr><td>{h(g.entgeltforderung_schluessel)}</td><td>{eur(g.betrag_cent)}</td></tr>"
                for g in vorschau.gebuehr_segmente
            )
            gebuehr_segmente_html = f"""<details><summary>Neue Pauschalen je Entgeltforderung ({len(vorschau.gebuehr_segmente)})</summary>
              <table><tr><th>Entgeltforderung</th><th>Betrag</th></tr>{zeilen_gebuehr}</table>
            </details>"""
        # Rückprüfung 14.09.2026, echter Bug: `hauptforderung_cent`
        # enthält seit dem Doppelzählungs-Fix NIE mehr bereits gebuchte,
        # noch offene Mahnkosten (siehe `kosten.py::
        # berechne_mahnkosten_vorschau`) - der tatsächlich verlangte
        # Gesamtbetrag muss sie deshalb HIER separat dazuzählen, sonst
        # würde eine bereits fakturierte, noch unbezahlte Pauschale aus
        # dem Gesamtbetrag verschwinden.
        gesamtbetrag_cent = vorschau.hauptforderung_cent + vorschau.bereits_offene_mahnkosten_cent + vorschau.zusaetzlicher_betrag_cent
        bereits_offene_mahnkosten_zeile = ""
        if vorschau.bereits_offene_mahnkosten_cent:
            bereits_offene_mahnkosten_zeile = f"""
            <tr><th>Bereits gebuchte, noch offene Mahnkosten (frühere Mahnläufe)</th>
                <td>{eur(vorschau.bereits_offene_mahnkosten_cent)}</td></tr>"""
        versandkosten_zeile = ""
        if vorschau.versandkosten_anbieteraufwand_cent is not None:
            ersetzt_cent = vorschau.gebuehr_cent if (vorschau.gebuehr_rechtsgrundlage or "").startswith("§1333") else None
            versandkosten_zeile = f"""
            <tr><th>Versandkosten Anbieteraufwand (Druck/Kuvert/Porto/Nachweis)</th>
                <td>{eur(vorschau.versandkosten_anbieteraufwand_cent)}</td></tr>
            <tr><th>Davon ersatzfähig angesetzt (§1333 Abs 2 ABGB)</th>
                <td>{eur(ersetzt_cent) if ersetzt_cent is not None else "0,00 €"}</td></tr>"""
        brief_status_html = ""
        if kanal == "BRIEF" and not deps._hv_mail.mahn_service._brief_transport_verfuegbar:
            brief_status_html = (
                '<p class="warn">📮 Brief wartet auf Anbindung: EinfachBrief-Versandtransport ist noch nicht '
                "angebunden - es erfolgt KEIN automatischer Versand und KEIN Ersatzversand per E-Mail. "
                "Der Brief kann bereits jetzt vorbereitet/heruntergeladen werden; das ist noch kein "
                "erzeugter/versendeter Brief.</p>"
            )
        brief_pdf_link = (
            f'<p><a href="/backoffice/vertrag/{h(vertrag_id)}/mahnbrief.pdf?stufe={stufe}&heute={heute_datum.isoformat()}">'
            "Brief-PDF für diese Stufe vorbereiten &amp; herunterladen</a> "
            '<span class="muted">(Vorschau-PDF aus diesem Kosten-/Forderungsstand, kein Zustellnachweis)</span></p>'
        )
        zeilen.append(f"""
        <div class="card">
          <h3>Stufe {stufe} (Kanal: {h(kanal)})</h3>
          {brief_status_html}
          <table>
            <tr><th>Hauptforderung</th><td>{eur(vorschau.hauptforderung_cent)}</td></tr>
            {bereits_offene_mahnkosten_zeile}
            <tr><th>Bereits gebuchte Zinsen (je betroffener Forderung, alle Stufen)</th><td>{eur(vorschau.bereits_gebuchte_zinsen_cent)}</td></tr>
            <tr><th>Neu zu bebuchende Zinsen (Delta)</th><td>{eur(vorschau.neue_zinsen_delta_cent)}</td></tr>
            <tr><th>Zinssatz / Basis</th><td>{h(satz_text)} ({h(vorschau.zinsbasis)})</td></tr>
            <tr><th>Zinszeitraum</th><td>{h(zeitraum_text)}</td></tr>
            <tr><th>Neue Mahngebühr</th><td>{h(gebuehr_text)}{f" ({h(vorschau.gebuehr_rechtsgrundlage)})" if vorschau.gebuehr_rechtsgrundlage else ""}</td></tr>
            {versandkosten_zeile}
            {mahnpauschale_zeile}
            <tr><th><strong>Gesamtbetrag (Hauptforderung + bereits offene Mahnkosten + neue Zinsen + neue Gebühr)</strong></th>
                <td><strong>{eur(gesamtbetrag_cent)}</strong></td></tr>
          </table>
          {brief_pdf_link}
          {segmente_html}
          {gebuehr_segmente_html}
          <details><summary>Rechtliche Begründung</summary><ul class="muted">{hinweise_html}</ul></details>
          {"<ul>" + ausgeschlossen_html + "</ul>" if ausgeschlossen_html else ""}
        </div>""")
    if not zeilen:
        return ""
    return f"""<div class="card"><h2>Mahnkosten (Verzugszinsen/Mahngebühren) — reine Vorschau, keine Buchung</h2>
      <p><a href="/backoffice/vertrag/{h(vertrag_id)}/zinsprofil">Zinsprofil erfassen/prüfen</a> &nbsp;|&nbsp;
      <a href="/backoffice/basiszinssatz">OeNB-Basiszinssatz erfassen</a></p></div>{''.join(zeilen)}"""


@router.get("/vertrag/{vertrag_id}/mahnpauschale-freigabe", response_class=HTMLResponse)
def mahnpauschale_freigabe_formular(
    request: Request, vertrag_id: str, stufe: int = 1, heute: str | None = None, session=Depends(_current_session),
) -> HTMLResponse:
    """Einfache Prüfliste/Freigabemaske für die Mahnkostenpauschale
    (§1333 Abs 2 ABGB) JE KONKRETEM Mahnlauf/Forderungsgruppe (Auftrag
    Markus, Fortsetzung HV-20261019-MAHNKOSTEN) - zeigt Hauptforderung,
    unverbindlichen Profil-Vorschlagswert, Verhältnis, Vertragsquelle und
    eine bereits bestehende (ggf. durch Saldenänderung ungültig
    gewordene) Freigabe; erteilt KEINE Buchung selbst - das bleibt
    ausschließlich `kosten.py::berechne_mahnkosten_vorschau` bei der
    nächsten tatsächlichen Mahnlaufverarbeitung vorbehalten."""

    if stufe not in (1, 2):
        return _fehlerseite(session, "Mahnpauschale-Freigabe", "Stufe muss 1 oder 2 sein.")
    vertrag = deps._stammdaten_repo.get_vertrag(vertrag_id)
    if vertrag is None:
        return _fehlerseite(session, "Mahnpauschale-Freigabe", f"Unbekannter Vertrag {vertrag_id}.")
    if _objekt_fuer_vertrag_gesperrt(vertrag_id):
        return _fehlerseite(session, "Mahnpauschale-Freigabe", "Objekt ist gesperrt; keine Freigabe möglich.")
    konto = deps._stammdaten_repo.get_konto_by_vertrag(vertrag_id)
    if konto is None:
        return _fehlerseite(session, "Mahnpauschale-Freigabe", "Kein Konto für diesen Vertrag vorhanden.")
    require_gesellschaft_access(_ctx(session), vertrag.gesellschaft_id)

    heute_datum = date.fromisoformat(heute) if heute else date.today()
    kanal = deps._hv_mail.mahn_service._resolve_kanal(stufe)
    vorschau = deps._hv_mail.mahnkosten_service.vorschau(vertrag_id=vertrag_id, stufe=stufe, heute=heute_datum, kanal=kanal)
    if vorschau is None:
        return _fehlerseite(session, "Mahnpauschale-Freigabe", "Keine Kostenvorschau für diesen Vertrag/diese Stufe verfügbar.")

    schluessel = _forderungsgruppe_schluessel(vorschau.forderung_op_position_ids)
    historie = deps._hv_mail.mahnkosten_repo.mahnpauschale_freigaben_historie(
        vertrag_id=vertrag_id, stufe=stufe, forderungsgruppe_schluessel=schluessel,
    )
    gueltige_freigabe = next((f for f in historie if f.hauptforderung_cent == vorschau.hauptforderung_cent), None)

    zinsprofil_historie = deps._hv_mail.mahnkosten_repo.historie_geprueft(vertrag_id)
    aktuelles_profil, _unberechenbar = _mahn_profil_wirksam_am(zinsprofil_historie, heute_datum)
    vorschlag_cent = aktuelles_profil.mahnpauschale_kostenbasis_cent if aktuelles_profil is not None else None
    vorschlag_beleg = aktuelles_profil.mahnpauschale_kostenbasis_beleg if aktuelles_profil is not None else None
    # Ohne belegten Profilwert nur ein unverbindlicher Prüfwert zum
    # Ausfüllen der Maske (siehe Konstante oben) - NIE als Beleg, NIE als
    # Freigabe und NIE als Grundlage einer automatischen Kostenbuchung.
    anzeige_vorschlag_cent = vorschlag_cent if vorschlag_cent is not None else _UNVERBINDLICHER_PRUEFWERT_CENT
    verhaeltnis_text = (
        f"{(anzeige_vorschlag_cent / vorschau.hauptforderung_cent * 100):.0f} % der Hauptforderung"
        if anzeige_vorschlag_cent and vorschau.hauptforderung_cent else "–"
    )

    kopf = f"""
    <div class="card" style="max-width:720px;">
      <h1>Mahnkostenpauschale-Freigabe — {h(vertrag_id)}, Stufe {stufe} (Kanal: {h(kanal)})</h1>
      <table>
        <tr><th>Hauptforderung dieser Forderungsgruppe (jetzt)</th><td>{eur(vorschau.hauptforderung_cent)}</td></tr>
        <tr><th>Vorgeschlagener Betrag (unverbindlich)</th>
            <td>{eur(vorschlag_cent) + " (Zinsprofil)" if vorschlag_cent
                 else eur(_UNVERBINDLICHER_PRUEFWERT_CENT) + " (kein Profilwert hinterlegt - unverbindlicher Eigentümer-Prüfwert, kein Beleg)"}</td></tr>
        <tr><th>Verhältnis Vorschlag / Hauptforderung</th><td>{h(verhaeltnis_text)}</td></tr>
        <tr><th>Vertragsquelle (Profil-Beleg)</th><td>{h(vorschlag_beleg or '–')}</td></tr>
        <tr><th>Versandschritt</th><td>Stufe {stufe}, Kanal {h(kanal)}, Simulationsdatum {heute_datum.isoformat()}</td></tr>
        <tr><th>Forderungsgruppe-Schlüssel</th><td><code>{h(schluessel[:16])}…</code> ({len(vorschau.forderung_op_position_ids)} Forderung(en))</td></tr>
      </table>
      <p><a href="/backoffice/vertrag/{h(vertrag_id)}/mahnvorschau?heute={heute_datum.isoformat()}">&larr; zurück zur Mahnvorschau</a></p>
    </div>"""

    _status_gueltig = '<span class="ok">gültig für aktuellen Saldo</span>'
    _status_ungueltig = '<span class="warn">ungültig - Saldo seither verändert</span>'
    historie_html = ""
    if historie:
        zeilen_h = "".join(
            f"<tr><td>{eur(f.hauptforderung_cent)}</td><td>{eur(f.betrag_cent)}</td>"
            f"<td>{_status_gueltig if f is gueltige_freigabe else _status_ungueltig}</td>"
            f"<td>{h(f.freigegeben_von)}</td><td>{f.freigegeben_am.strftime('%d.%m.%Y %H:%M') if f.freigegeben_am else ''}</td></tr>"
            for f in historie
        )
        historie_html = f"""
        <div class="card">
          <h2>Bisherige Freigaben für diese Forderungsgruppe</h2>
          <table>
            <tr><th>Damalige Hauptforderung</th><th>Freigegebener Betrag</th><th>Status</th><th>Freigegeben von</th><th>Zeitpunkt</th></tr>
            {zeilen_h}
          </table>
          <p class="muted">Eine Freigabe gilt ausschließlich für GENAU den damaligen Saldo - eine Teilzahlung oder
             sonstige Saldenänderung macht eine bestehende Freigabe automatisch ungültig (0 EUR, Prüffall), ohne sie
             zu löschen. Eine neue Freigabe für den jetzt aktuellen Saldo ist unten jederzeit möglich.</p>
        </div>"""

    if gueltige_freigabe is not None:
        inhalt = kopf + historie_html + f"""
        <div class="card">
          <h2 class="ok">Für den aktuellen Saldo bereits freigegeben</h2>
          <table>
            <tr><th>Freigegebener Betrag</th><td>{eur(gueltige_freigabe.betrag_cent)}</td></tr>
            <tr><th>Erforderlichkeit</th><td>{h(gueltige_freigabe.erforderlichkeit_begruendung)}</td></tr>
            <tr><th>Tatsächlicher Aufwand</th><td>{eur(gueltige_freigabe.tatsaechlicher_aufwand_cent)} — {h(gueltige_freigabe.tatsaechlicher_aufwand_beleg)}</td></tr>
            <tr><th>Schuldhaftigkeit (konkret)</th><td>{h(gueltige_freigabe.schuldhaftigkeit_begruendung)}</td></tr>
            <tr><th>Verhältnismäßigkeit</th><td>{h(gueltige_freigabe.verhaeltnismaessigkeit_begruendung)}</td></tr>
            <tr><th>Freigabe-Owner / Zeitpunkt</th><td>{h(gueltige_freigabe.freigegeben_von)} am {gueltige_freigabe.freigegeben_am.strftime('%d.%m.%Y %H:%M') if gueltige_freigabe.freigegeben_am else ''}</td></tr>
          </table>
        </div>"""
        return _layout(request, session, "Mahnpauschale-Freigabe", inhalt)

    simulations_hinweis = ""
    if heute_datum != date.today():
        simulations_hinweis = (
            f'<p class="warn">Diese Ansicht zeigt eine SIMULATION für {heute_datum.isoformat()} - rein lesend. '
            f"Eine tatsächliche Freigabe wird IMMER für das echte Tagesdatum ({date.today().isoformat()}) "
            "serverseitig neu berechnet, unabhängig von diesem Simulationsdatum.</p>"
        )
    formular = f"""
    <div class="card">
      <h2>Neue Einzelfallfreigabe für den aktuellen Saldo erteilen</h2>
      {simulations_hinweis}
      <p class="muted">Ohne diese vollständig begründete Freigabe bleibt die Pauschale automatisch bei 0 EUR
         (Prüffall) - ein Profil-Vorschlagswert bindet nichts automatisch.</p>
      <form method="post" action="/backoffice/vertrag/{h(vertrag_id)}/mahnpauschale-freigabe/erfassen">
        {csrf_feld(session.csrf_token)}
        <input type="hidden" name="stufe" value="{stufe}">
        <label>Freigegebener Betrag in EUR (Pflicht - nicht notwendig identisch mit dem Vorschlagswert, darf den
               tatsächlichen Aufwand NIE übersteigen)</label>
        <input name="betrag_cent" required value="{f'{anzeige_vorschlag_cent/100:.2f}'.replace('.', ',')}">
        <label>Erforderlichkeit (Pflicht - warum ist eine Mahnung in diesem Fall erforderlich)</label>
        <textarea name="erforderlichkeit_begruendung" rows="2" required></textarea>
        <label>Tatsächlicher Aufwand in EUR (Pflicht)</label>
        <input name="tatsaechlicher_aufwand_cent" required placeholder="z. B. 20,00">
        <label>Beleg tatsächlicher Aufwand (Pflicht - z. B. Zeiterfassung, Druck-/Portokosten)</label>
        <textarea name="tatsaechlicher_aufwand_beleg" rows="2" required></textarea>
        <label>Konkrete Schuldhaftigkeit DIESES Rückstands (Pflicht - z. B. ausdrücklich NICHT bei einem Bank-/SEPA-Fehler)</label>
        <textarea name="schuldhaftigkeit_begruendung" rows="2" required></textarea>
        <label>Verhältnismäßigkeit zur aktuellen Hauptforderung (Pflicht)</label>
        <textarea name="verhaeltnismaessigkeit_begruendung" rows="2" required></textarea>
        <button type="submit">Freigeben</button>
      </form>
    </div>"""
    return _layout(request, session, "Mahnpauschale-Freigabe", kopf + historie_html + formular)


@router.post("/vertrag/{vertrag_id}/mahnpauschale-freigabe/erfassen")
async def mahnpauschale_freigabe_absenden(request: Request, vertrag_id: str, session=Depends(_current_session)):
    form = await request.form()
    _verify_csrf(session, str(form.get("csrf_token", "")))
    vertrag = deps._stammdaten_repo.get_vertrag(vertrag_id)
    if vertrag is None:
        return _fehlerseite(session, "Mahnpauschale-Freigabe", f"Unbekannter Vertrag {vertrag_id}.")
    if _objekt_fuer_vertrag_gesperrt(vertrag_id):
        return _fehlerseite(session, "Mahnpauschale-Freigabe", "Objekt ist gesperrt; keine Freigabe möglich.")
    require_gesellschaft_access(_ctx(session), vertrag.gesellschaft_id)
    require_schreibrecht(_ctx(session))

    try:
        stufe = int(str(form.get("stufe", "1")))
    except ValueError:
        return _fehlerseite(session, "Mahnpauschale-Freigabe", "Ungültige Stufe.")
    if stufe not in (1, 2):
        return _fehlerseite(session, "Mahnpauschale-Freigabe", "Stufe muss 1 oder 2 sein.")
    # Unabhängige Rückprüfung Codex 19.10.2026, echter Bug: die Freigabe
    # selbst darf NIE auf ein vom Formular mitgesendetes, ggf. simuliertes
    # Datum erteilt werden - ein manipuliertes/veraltetes `heute` aus dem
    # verborgenen Feld hätte eine Freigabe für einen fiktiven Vergangen-
    # heits-/Zukunftsstand ermöglicht. Die Simulation (GET, `?heute=...`)
    # bleibt rein lesend; die tatsächliche Freigabe-Entscheidung bindet
    # IMMER an das echte Tagesdatum des Servers.
    heute_datum = date.today()

    # IMMER frisch aus dem aktuellen Server-Stand neu berechnet - NIE ein
    # vom Formular mitgesendeter Schlüssel/Hauptforderungsbetrag (der
    # könnte zwischen Formularanzeige und Absenden veraltet oder
    # manipuliert sein). Genau dieselbe Methode wie die GET-Ansicht.
    kanal = deps._hv_mail.mahn_service._resolve_kanal(stufe)
    vorschau = deps._hv_mail.mahnkosten_service.vorschau(vertrag_id=vertrag_id, stufe=stufe, heute=heute_datum, kanal=kanal)
    if vorschau is None:
        return _fehlerseite(session, "Mahnpauschale-Freigabe", "Keine Kostenvorschau für diesen Vertrag/diese Stufe verfügbar.")
    schluessel = _forderungsgruppe_schluessel(vorschau.forderung_op_position_ids)

    try:
        betrag_cent = parse_eur_betrag(str(form.get("betrag_cent", "")).strip())
        aufwand_cent = parse_eur_betrag(str(form.get("tatsaechlicher_aufwand_cent", "")).strip())
        freigabe = deps._hv_mail.mahnkosten_repo.mahnpauschale_freigabe_anlegen(
            vertrag_id=vertrag_id, stufe=stufe, forderungsgruppe_schluessel=schluessel,
            hauptforderung_cent=vorschau.hauptforderung_cent, betrag_cent=betrag_cent,
            erforderlichkeit_begruendung=str(form.get("erforderlichkeit_begruendung", "")).strip(),
            tatsaechlicher_aufwand_cent=aufwand_cent,
            tatsaechlicher_aufwand_beleg=str(form.get("tatsaechlicher_aufwand_beleg", "")).strip(),
            schuldhaftigkeit_begruendung=str(form.get("schuldhaftigkeit_begruendung", "")).strip(),
            verhaeltnismaessigkeit_begruendung=str(form.get("verhaeltnismaessigkeit_begruendung", "")).strip(),
            freigegeben_von=session.user_id, erstellt_von=session.user_id,
        )
        deps._audit_service.log(
            entity_typ="mahnpauschale_freigabe", entity_id=str(freigabe.id), akteur=session.user_id,
            aktion="FREIGABE_ERTEILT",
            payload={
                "vertrag_id": vertrag_id, "stufe": stufe, "forderungsgruppe_schluessel": schluessel,
                "hauptforderung_cent": vorschau.hauptforderung_cent, "betrag_cent": betrag_cent,
            },
        )
    except (MietinkassoError, ValueError, IntegrityError) as exc:
        return _fehlerseite(
            session, "Mahnpauschale-Freigabe",
            "Für diese Forderungsgruppe und diesen Saldo wurde inzwischen bereits eine Freigabe erteilt."
            if isinstance(exc, IntegrityError) else str(exc),
            f"/backoffice/vertrag/{vertrag_id}/mahnpauschale-freigabe?stufe={stufe}&heute={heute_datum.isoformat()}",
        )
    return RedirectResponse(
        url=f"/backoffice/vertrag/{vertrag_id}/mahnpauschale-freigabe?stufe={stufe}&heute={heute_datum.isoformat()}",
        status_code=303,
    )


@router.get("/vertrag/{vertrag_id}/mahnbrief.pdf")
def mahnbrief_pdf(vertrag_id: str, stufe: int, heute: str | None = None, session=Depends(_current_session)):
    """Bereitet EINEN druckfertigen PDF/A-Mahnbrief aus GENAU demselben
    Kosten-/Forderungsstand vor, den auch `_mahnkosten_vorschau_block`
    anzeigt - reiner Download, keine Buchung, kein Versand, kein
    Zustellnachweis, ausdrücklich eine AKTUELLE Vorschau (kein
    eingefrorener Versandnachweis - jeder Aufruf liest live neu).
    Forderungszeilen/Zinssegmente stammen aus derselben `vorschau()`-
    Berechnung wie der E-Mail-Text (keine zweite Kostenberechnung); die
    itemisierten offenen Forderungen werden zusätzlich EINMAL separat
    gelesen (wie im etablierten Muster in `mailversand_service.py`s
    `versand_fn`) und anhand `vorschau.forderung_op_position_ids`
    gefiltert, nie unabhängig neu berechnet. Der Dateiname trägt einen
    Hash über Kostenkomposition UND Empfängerdaten: nur ein in beidem
    unveränderter Stand liefert denselben Namen."""

    if stufe not in (1, 2):
        raise HTTPException(status_code=422, detail="stufe muss 1 oder 2 sein.")
    try:
        heute_datum = date.fromisoformat(heute) if heute else date.today()
    except ValueError:
        raise HTTPException(status_code=422, detail="Ungültiges Datum für 'heute' (Format YYYY-MM-DD erwartet).")

    vertrag = deps._stammdaten_repo.get_vertrag(vertrag_id)
    konto = deps._stammdaten_repo.get_konto_by_vertrag(vertrag_id) if vertrag else None
    if vertrag is None or konto is None:
        raise HTTPException(status_code=404, detail="Unbekannter Vertrag oder kein Konto.")
    require_gesellschaft_access(_ctx(session), vertrag.gesellschaft_id)

    debitor = deps._stammdaten_repo.get_debitor(konto.debitor_id)
    objekt = deps._stammdaten_repo.objekt_fuer_vertrag(vertrag_id)
    einheit = deps._stammdaten_repo.get_einheit(vertrag.einheit_id)
    if debitor is None or objekt is None or einheit is None:
        raise HTTPException(status_code=404, detail="Stammdaten unvollständig.")

    kanal = deps._hv_mail.mahn_service._resolve_kanal(stufe)
    vorschau = deps._hv_mail.mahnkosten_service.vorschau(vertrag_id=vertrag_id, stufe=stufe, heute=heute_datum, kanal=kanal)
    if vorschau is None:
        raise HTTPException(status_code=404, detail="Keine Kostenvorschau für diesen Vertrag/diese Stufe verfügbar.")

    offene_ids = set(vorschau.forderung_op_position_ids)
    forderungszeilen = [
        _BriefForderungszeile(
            bezeichnung=f.leistungsperiode or f.art,
            faelligkeit=f.faelligkeit if f.faelligkeit_bekannt else None,
            betrag_cent=f.rest_cent,
        )
        for f in deps._op_service.offene_forderungen(konto.id, heute=heute_datum)
        if f.op_position_id in offene_ids
    ]
    zins_segmente = [
        _BriefZinssegment(von=s.von, bis=s.bis, satz_prozent=s.satz_prozent, zinsen_cent=s.zinsen_cent)
        for s in vorschau.zins_segmente
    ]
    gesamtbetrag_cent = vorschau.hauptforderung_cent + vorschau.bereits_offene_mahnkosten_cent + vorschau.zusaetzlicher_betrag_cent
    zinssatz_einheitlich_text = f"{vorschau.zinssatz_prozent} % p.a." if vorschau.zinssatz_prozent is not None else None

    absender = _BriefAbsender(
        name=deps._settings.brief_absender_name, adresse=deps._settings.brief_absender_adresse, fn=deps._settings.brief_absender_fn,
        uid=deps._settings.brief_absender_uid, telefon=deps._settings.brief_absender_telefon, website=deps._settings.brief_absender_website,
        email=deps._settings.brief_absender_email, farbe_anthrazit=deps._settings.brief_farbe_anthrazit, farbe_gold=deps._settings.brief_farbe_gold,
        logo_pfad=deps._settings.brief_logo_pfad, font_regular_pfad=deps._settings.brief_font_regular_pfad, font_bold_pfad=deps._settings.brief_font_bold_pfad,
        font_headline_pfad=deps._settings.brief_font_headline_pfad,
        fenster_links_mm=deps._settings.brief_fenster_links_mm, fenster_oben_mm=deps._settings.brief_fenster_oben_mm,
        fenster_breite_mm=deps._settings.brief_fenster_breite_mm, fenster_hoehe_mm=deps._settings.brief_fenster_hoehe_mm,
    )
    try:
        pdf_bytes = _erzeuge_mahnbrief_pdf(
            absender=absender, empfaenger_name=debitor.name, empfaenger_adresse=(debitor.adresse or "").replace(", ", "\n"),
            objekt_bezeichnung=objekt.bezeichnung, einheit_bezeichnung=einheit.bezeichnung, stufe=stufe,
            rechtsordnung=vertrag.rechtsordnung, heute=heute_datum,
            zahlungsfrist_bis=heute_datum + timedelta(days=vertrag.zahlungsfrist_tage),
            forderungszeilen=forderungszeilen, bereits_offene_mahnkosten_cent=vorschau.bereits_offene_mahnkosten_cent,
            neue_zinsen_delta_cent=vorschau.neue_zinsen_delta_cent, zins_segmente=zins_segmente,
            zinssatz_einheitlich_text=zinssatz_einheitlich_text, neue_gebuehr_cent=(vorschau.gebuehr_cent or 0),
            gebuehr_rechtsgrundlage=vorschau.gebuehr_rechtsgrundlage, gesamtbetrag_cent=gesamtbetrag_cent,
        )
    except _BriefAdressfehlerError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    version = _compute_content_hash({
        "vertrag_id": vertrag_id, "stufe": stufe, "empfaenger_name": debitor.name, "empfaenger_adresse": debitor.adresse,
        "hauptforderung_cent": vorschau.hauptforderung_cent, "bereits_offene_mahnkosten_cent": vorschau.bereits_offene_mahnkosten_cent,
        "zusaetzlicher_betrag_cent": vorschau.zusaetzlicher_betrag_cent, "gebuehr_rechtsgrundlage": vorschau.gebuehr_rechtsgrundlage,
        "forderungszeilen": [(f.bezeichnung, str(f.faelligkeit), f.betrag_cent) for f in forderungszeilen],
        "zins_segmente": [(str(s.von), str(s.bis), str(s.satz_prozent), s.zinsen_cent) for s in zins_segmente],
    })[:12]
    dateiname = f"Mahnbrief_{vertrag_id}_Stufe{stufe}_{heute_datum.isoformat()}_{version}.pdf"
    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{dateiname}"'},
    )


# -- Mahnbrief-Archiv + Anbieterbeleg-Import (Auftrag HV-20261019- ---------
# -- VERSAND-KOSTEN-ABSCHLUSS, Teil 3) --------------------------------------
#
# Zeigt JEDE tatsächlich archivierte (= bereits über den Briefkanal
# versandversuchte) Mahnbrief-PDF-Zeile eines Vertrags, bietet den
# geschützten Original-PDF-Download UND - solange noch kein Beleg
# importiert wurde - das Formular für den echten Anbieterbeleg
# (Original-PDF + Referenz + tatsächliches Postaufgabedatum). Ein
# erfolgreicher Import vervollständigt darüber den eingefrorenen
# Mahnlauf idempotent (siehe `HVMailversandService.
# vervollstaendige_mahnlauf_aus_anbieterbeleg`) - NIE ein erneuter Versand.

_MAX_ANBIETERBELEG_UPLOAD_BYTES = 15 * 1024 * 1024


async def _lese_beleg_begrenzt(upload_file: UploadFile, max_bytes: int = _MAX_ANBIETERBELEG_UPLOAD_BYTES) -> bytes:
    stueck_groesse = 1024 * 1024
    gelesen = bytearray()
    while True:
        stueck = await upload_file.read(stueck_groesse)
        if not stueck:
            break
        gelesen += stueck
        if len(gelesen) > max_bytes:
            raise ValueError(f"Belegdatei überschreitet die zulässige Größe ({max_bytes // (1024 * 1024)} MB).")
    return bytes(gelesen)


def _mahnbrief_archiv_zeile_html(vertrag_id: str, zeile, csrf_token: str) -> str:
    status_html = (
        f"<p>Status: <strong>{h(zeile.status)}</strong>"
        + (f" (übermittelt am {h(zeile.status_aktualisiert_am.isoformat())})" if zeile.status_aktualisiert_am else "")
        + "</p>"
    )
    download_original = (
        f'<p><a href="/backoffice/vertrag/{h(vertrag_id)}/mahnbrief-archiv/{zeile.id}/original.pdf">'
        "Original-Mahnbrief-PDF herunterladen</a></p>"
    )
    if zeile.anbieterbeleg_referenz:
        beleg_html = f"""
        <p><strong>Anbieterbeleg vorhanden:</strong> {h(zeile.anbieterbeleg_referenz)},
           tatsächliches Postaufgabedatum {h(zeile.anbieterbeleg_postaufgabe_datum.isoformat())},
           importiert von {h(zeile.anbieterbeleg_importiert_von or '')}
           am {h(zeile.anbieterbeleg_importiert_am.isoformat() if zeile.anbieterbeleg_importiert_am else '')}.</p>
        <p><a href="/backoffice/vertrag/{h(vertrag_id)}/mahnbrief-archiv/{zeile.id}/anbieterbeleg.pdf">
           Anbieterbeleg (Original-PDF) herunterladen</a></p>"""
    else:
        beleg_html = f"""
        <form method="post" action="/backoffice/vertrag/{h(vertrag_id)}/mahnbrief-archiv/{zeile.id}/beleg"
              enctype="multipart/form-data">
          {csrf_feld(csrf_token)}
          <label>Anbieterbeleg (das tatsächliche Original-PDF aus der Post-Portal-Auftragsliste - ein
                 Referenztext allein ist KEIN Beleg, Pflicht)</label>
          <input type="file" name="beleg_datei" accept="application/pdf" required>
          <label>Belegreferenz (Pflicht, eindeutig - gehört zu GENAU dieser einen Sendung)</label>
          <input name="anbieterbeleg_referenz" required>
          <label>Tatsächliches Postaufgabedatum laut Beleg (Pflicht - nicht der Uploadtag)</label>
          <input type="date" name="postaufgabe_datum" required>
          <button type="submit">Beleg importieren und Mahnlauf vervollständigen</button>
        </form>"""
    return f"""
    <div class="card">
      <h3>Stufe {zeile.stufe} - Mahnlauf {h(zeile.mahnlauf_schluessel)}</h3>
      <p class="muted">Archiviert am {h(zeile.archiviert_am.isoformat() if zeile.archiviert_am else '')}.</p>
      {status_html}
      {download_original}
      {beleg_html}
    </div>"""


@router.get("/vertrag/{vertrag_id}/mahnbrief-archiv", response_class=HTMLResponse)
def mahnbrief_archiv_liste(request: Request, vertrag_id: str, session=Depends(_current_session)) -> HTMLResponse:
    vertrag = deps._stammdaten_repo.get_vertrag(vertrag_id)
    if vertrag is None or _objekt_fuer_vertrag_gesperrt(vertrag_id):
        return _fehlerseite(session, "Mahnbrief-Archiv", "Vertrag nicht verfügbar.", "/backoffice/vertraege")
    require_gesellschaft_access(_ctx(session), vertrag.gesellschaft_id)

    zeilen = deps._hv_mail.mahnbrief_archiv_repo.list_fuer_vertrag(vertrag_id)
    if not zeilen:
        inhalt = "<p>Für diesen Vertrag ist noch kein Mahnbrief archiviert.</p>"
    else:
        inhalt = "".join(_mahnbrief_archiv_zeile_html(vertrag_id, zeile, session.csrf_token) for zeile in zeilen)
    rueck = f'<p><a href="/backoffice/vertrag/{h(vertrag_id)}">&larr; zurück zur Mieterakte</a></p>'
    return _layout(request, session, "Mahnbrief-Archiv", rueck + inhalt)


@router.get("/vertrag/{vertrag_id}/mahnbrief-archiv/{archiv_id}/original.pdf")
def mahnbrief_archiv_original_pdf(vertrag_id: str, archiv_id: int, session=Depends(_current_session)):
    """Geschützter Download des tatsächlich archivierten, unveränderlichen
    Original-PDFs (NICHT die On-Demand-Vorschau) - dieselben Zugriffs-
    prüfungen wie jede andere Mieterakte-Ansicht."""

    vertrag = deps._stammdaten_repo.get_vertrag(vertrag_id)
    if vertrag is None or _objekt_fuer_vertrag_gesperrt(vertrag_id):
        raise HTTPException(status_code=404, detail="Vertrag nicht verfügbar.")
    require_gesellschaft_access(_ctx(session), vertrag.gesellschaft_id)
    zeile = deps._hv_mail.mahnbrief_archiv_repo.get(archiv_id)
    if zeile is None or zeile.vertrag_id != vertrag_id:
        raise HTTPException(status_code=404, detail="Unbekannte Archivzeile.")
    return Response(
        content=zeile.pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="Mahnbrief-Archiv-{archiv_id}.pdf"'},
    )


@router.get("/vertrag/{vertrag_id}/mahnbrief-archiv/{archiv_id}/anbieterbeleg.pdf")
def mahnbrief_archiv_anbieterbeleg_pdf(vertrag_id: str, archiv_id: int, session=Depends(_current_session)):
    """Geschützter Download des tatsächlich importierten Anbieterbelegs
    (das Originaldokument selbst, nicht nur die Referenz)."""

    vertrag = deps._stammdaten_repo.get_vertrag(vertrag_id)
    if vertrag is None or _objekt_fuer_vertrag_gesperrt(vertrag_id):
        raise HTTPException(status_code=404, detail="Vertrag nicht verfügbar.")
    require_gesellschaft_access(_ctx(session), vertrag.gesellschaft_id)
    zeile = deps._hv_mail.mahnbrief_archiv_repo.get(archiv_id)
    if zeile is None or zeile.vertrag_id != vertrag_id or zeile.anbieterbeleg_pdf_bytes is None:
        raise HTTPException(status_code=404, detail="Kein Anbieterbeleg vorhanden.")
    return Response(
        content=zeile.anbieterbeleg_pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="Anbieterbeleg-{archiv_id}.pdf"'},
    )


@router.post("/vertrag/{vertrag_id}/mahnbrief-archiv/{archiv_id}/beleg")
async def mahnbrief_archiv_beleg_importieren(
    request: Request, vertrag_id: str, archiv_id: int,
    csrf_token: str = Form(...), anbieterbeleg_referenz: str = Form(...), postaufgabe_datum: str = Form(...),
    beleg_datei: UploadFile = File(...), session=Depends(_current_session),
):
    _verify_csrf(session, csrf_token)
    vertrag = deps._stammdaten_repo.get_vertrag(vertrag_id)
    if vertrag is None or _objekt_fuer_vertrag_gesperrt(vertrag_id):
        return _fehlerseite(session, "Mahnbrief-Archiv", "Vertrag nicht verfügbar.", "/backoffice/vertraege")
    require_gesellschaft_access(_ctx(session), vertrag.gesellschaft_id)
    require_schreibrecht(_ctx(session))

    rueck_href = f"/backoffice/vertrag/{vertrag_id}/mahnbrief-archiv"
    try:
        postaufgabe_datum_wert = date.fromisoformat(postaufgabe_datum.strip())
    except ValueError:
        return _fehlerseite(session, "Mahnbrief-Archiv", "Ungültiges Postaufgabedatum (Format YYYY-MM-DD erwartet).", rueck_href)
    try:
        beleg_pdf_bytes = await _lese_beleg_begrenzt(beleg_datei)
    except ValueError as exc:
        return _fehlerseite(session, "Mahnbrief-Archiv", str(exc), rueck_href)

    try:
        deps._hv_mail.vervollstaendige_mahnlauf_aus_anbieterbeleg(
            ctx=_ctx(session), archiv_id=archiv_id, anbieterbeleg_pdf_bytes=beleg_pdf_bytes,
            anbieterbeleg_referenz=anbieterbeleg_referenz.strip(), postaufgabe_datum=postaufgabe_datum_wert,
            importiert_von=session.user_id, heute=date.today(),
        )
        deps._audit_service.log(
            entity_typ="mahnbrief_archiv", entity_id=str(archiv_id), akteur=session.user_id,
            aktion="ANBIETERBELEG_IMPORTIERT",
            payload={"vertrag_id": vertrag_id, "anbieterbeleg_referenz": anbieterbeleg_referenz.strip()},
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Mahnbrief-Archiv", str(exc), rueck_href)
    return RedirectResponse(url=rueck_href, status_code=303)


@router.post("/mahnfall/{mahnfall_id}/sendebereitschaft", response_class=HTMLResponse)
def mahnfall_sendebereitschaft(request: Request, mahnfall_id: int, csrf_token: str = Form(...), session=Depends(_current_session)) -> HTMLResponse:
    _verify_csrf(session, csrf_token)
    mahnfall = deps._mahn_fall_repo.get(mahnfall_id)
    if mahnfall is None:
        return _fehlerseite(session, "Mahnvorschau", f"Unbekannter Mahnfall {mahnfall_id}.")
    bank_bestaetigt_bis, ungeklaert = _bank_freigabe_ableiten(mahnfall.gesellschaft_id, mahnfall.vertrag_id)
    ergebnis = deps._mahn_service.versenden(
        ctx=_ctx(session), mahnfall_id=mahnfall_id, heute=date.today(), bank_bestaetigt_bis=bank_bestaetigt_bis,
        ungeklaerte_eingaenge_vorhanden=ungeklaert, send_enabled=False, versand_fn=lambda *_: None,
    )
    inhalt = flash_ok(f"Sendebereitschaft (KEIN echter Versand): {ergebnis.status} — {ergebnis.grund}")
    inhalt += f'<p><a href="/backoffice/vertrag/{h(mahnfall.vertrag_id)}/mahnvorschau">&larr; zurück</a></p>'
    return _layout(request, session, "Sendebereitschaft", inhalt)


# -- Mahnstufen-Konfiguration (genau 2 Stufen, HV-20260912-ECHTBETRIEB) ------


@router.post("/mahnfall/{mahnfall_id}/versenden", response_class=HTMLResponse)
def mahnfall_versenden(request: Request, mahnfall_id: int, csrf_token: str = Form(...), session=Depends(_current_session)):
    _verify_csrf(session, csrf_token)
    try:
        result = deps._hv_mail.mahnung_senden(ctx=_ctx(session), row_id=mahnfall_id, heute=heute_wien())
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Mahnversand", str(exc), "/backoffice/mailversand")
    return _layout(request, session, "Mahnversand", flash_ok(f"{result.status} — {result.grund}") +
        '<p><a href="/backoffice/mailversand">Versandnachweise ansehen</a></p>')


def _policy_zeile_html(policy) -> str:
    aktion = ""
    if policy.status == "ENTWURF":
        aktion = f"""
        <form method="post" action="/backoffice/mahnwesen/policy/{policy.id}/freigeben" class="inline">
          {{csrf}}
          <button type="submit" class="secondary">Freigeben</button>
        </form>"""
    return f"""
    <tr>
      <td>{policy.version}</td>
      <td>{policy.stufe1_tage_nach_faelligkeit} Tage</td>
      <td>{policy.stufe2_mindesttage_nach_stufe1_versand} Tage</td>
      <td>{policy.zinsen_prozent}%</td>
      <td>{policy.gebuehr_cent} Cent</td>
      <td>{h(policy.status)}</td>
      <td>{policy.freigegeben_am.isoformat() if policy.freigegeben_am else '-'}</td>
      <td>{aktion}</td>
    </tr>"""


@router.get("/mahnwesen/policy", response_class=HTMLResponse)
def mahnpolicy_uebersicht(request: Request, session=Depends(_current_session)) -> HTMLResponse:
    aktuelle = deps._mahn_policy_repo.aktuelle_freigegebene()
    alle = deps._mahn_policy_repo.alle()

    aktuelle_html = (
        f"""<div class="card">
          <p class="ok">Aktuell freigegeben: Version {aktuelle.version} — Stufe 1 nach {aktuelle.stufe1_tage_nach_faelligkeit}
          Tagen, Stufe 2 frühestens {aktuelle.stufe2_mindesttage_nach_stufe1_versand} Tage nach tatsächlich
          versandter Stufe 1 UND erst nach deren vertraglicher Zahlungsfrist (serverseitig als Maximum
          erzwungen). Zinsen/Gebühren: {aktuelle.zinsen_prozent}% / {aktuelle.gebuehr_cent} Cent.</p>
        </div>"""
        if aktuelle is not None
        else flash_error("Keine freigegebene MahnPolicy vorhanden — es kann derzeit NICHTS automatisch gemahnt werden.")
    )

    zeilen_html = "".join(_policy_zeile_html(p).replace("{csrf}", csrf_feld(session.csrf_token)) for p in alle)

    inhalt = f"""
    <div class="card" style="max-width:820px;">
      <h1>Mahnstufen-Konfiguration</h1>
      <p class="muted">Genau zwei automatische Mahnstufen (Fachregel 7) — keine dritte Stufe, kein
         Inkasso/RA, Zinsen/Gebühren in diesem Auftrag fest auf 0 (nicht editierbar). Bestehende Sperren
         (RA/Ratenplan/Insolvenz/ungeklärter Eingang/unklarer Eröffnungssaldo/manuelle Sperre) sowie die
         BESTÄTIGTE Bankvollständigkeit gelten unverändert und werden hier NICHT umgangen.
         <code>SEND_ENABLED=false</code> bleibt Standard — diese Seite ändert daran nichts, sie plant nur
         Fristen, sie versendet nichts.</p>
    </div>
    {aktuelle_html}
    <div class="card">
      <h2>Neue Version vorschlagen</h2>
      <form method="post" action="/backoffice/mahnwesen/policy/anlegen">
        {csrf_feld(session.csrf_token)}
        <label>Stufe 1: Tage nach belegter Fälligkeit</label>
        <input type="number" name="stufe1_tage_nach_faelligkeit" min="1" value="{deps._settings.mahn_stufe1_tage_nach_faelligkeit}" required>
        <label>Stufe 2: Mindesttage nach tatsächlich versandter Stufe 1 (zusätzlich wird serverseitig
               IMMER auch die vertragliche Zahlungsfrist abgewartet — das Maximum beider Werte gilt)</label>
        <input type="number" name="stufe2_mindesttage_nach_stufe1_versand" min="1" value="{deps._settings.mahn_stufe2_mindesttage_nach_stufe1}" required>
        <p class="muted">Zinsen: 0% · Gebühr: 0 Cent (in diesem Auftrag fest, nicht editierbar).</p>
        <button type="submit">Als Entwurf anlegen</button>
      </form>
    </div>
    <div class="card">
      <h2>Versionen</h2>
      <table>
        <tr><th>Version</th><th>Stufe 1</th><th>Stufe 2</th><th>Zinsen</th><th>Gebühr</th><th>Status</th><th>Freigegeben am</th><th></th></tr>
        {zeilen_html or '<tr><td colspan=8 class="muted">Noch keine Policy angelegt.</td></tr>'}
      </table>
    </div>"""
    return _layout(request, session, "Mahnstufen-Konfiguration", inhalt)


@router.post("/mahnwesen/policy/anlegen", response_class=HTMLResponse)
def mahnpolicy_anlegen(
    request: Request,
    stufe1_tage_nach_faelligkeit: int = Form(...),
    stufe2_mindesttage_nach_stufe1_versand: int = Form(...),
    csrf_token: str = Form(...),
    session=Depends(_current_session),
) -> HTMLResponse:
    _verify_csrf(session, csrf_token)
    if stufe1_tage_nach_faelligkeit < 1 or stufe2_mindesttage_nach_stufe1_versand < 1:
        return _fehlerseite(session, "Mahnstufen-Konfiguration", "Beide Werte müssen mindestens 1 Tag betragen.", "/backoffice/mahnwesen/policy")
    policy = deps._mahn_policy_repo.anlegen(
        stufe1_tage_nach_faelligkeit=stufe1_tage_nach_faelligkeit,
        stufe2_mindesttage_nach_stufe1_versand=stufe2_mindesttage_nach_stufe1_versand,
        # Fest auf 0 - dieser Auftrag verlangt ausdrücklich "keine Zinsen/Gebühren"; kein Formularfeld,
        # das versehentlich (oder absichtlich via rohem POST) einen anderen Wert setzen könnte.
        zinsen_prozent=Decimal("0"),
        gebuehr_cent=0,
        status="ENTWURF",
    )
    deps._audit_service.log(
        entity_typ="mahn_policy", entity_id=str(policy.id), aktion="angelegt", akteur=session.user_id,
        payload={"version": policy.version, "stufe1_tage": stufe1_tage_nach_faelligkeit, "stufe2_mindesttage": stufe2_mindesttage_nach_stufe1_versand},
    )
    inhalt = flash_ok(f"Policy-Version {policy.version} als ENTWURF angelegt — muss noch freigegeben werden, bevor sie wirkt.")
    inhalt += '<p><a href="/backoffice/mahnwesen/policy">&larr; zurück</a></p>'
    return _layout(request, session, "Mahnstufen-Konfiguration", inhalt)


@router.post("/mahnwesen/policy/{policy_id}/freigeben", response_class=HTMLResponse)
def mahnpolicy_freigeben(request: Request, policy_id: int, csrf_token: str = Form(...), session=Depends(_current_session)) -> HTMLResponse:
    _verify_csrf(session, csrf_token)
    try:
        policy = deps._mahn_policy_repo.freigeben(policy_id)
    except ValueError as exc:
        return _fehlerseite(session, "Mahnstufen-Konfiguration", str(exc), "/backoffice/mahnwesen/policy")
    deps._audit_service.log(entity_typ="mahn_policy", entity_id=str(policy.id), aktion="freigegeben", akteur=session.user_id, payload={"version": policy.version})
    inhalt = flash_ok(f"Policy-Version {policy.version} freigegeben.")
    inhalt += '<p><a href="/backoffice/mahnwesen/policy">&larr; zurück</a></p>'
    return _layout(request, session, "Mahnstufen-Konfiguration", inhalt)
