"""Vertrags-Prüfseite (Kontoabdeckung) und Mieterantwort-Entwürfe.

Beide Seiten lesen nur: kein Schreibzugriff auf die Datenbank, keine Aufgabe,
keine Mahnfreigabe, kein Maildraft und kein Versand. Der POST des Entwurfs ist
nur wegen der Eingabelänge ein POST und speichert nichts."""

from __future__ import annotations

from datetime import date
from html import escape as h

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from mietinkasso.abgleichstatus.antwortentwurf import (
    MAX_EINGABE_ZEICHEN,
    STATUS_BELEGT_MIT_GRENZE,
    STATUS_MANUELL,
    STATUS_OFFEN,
    AntwortFakten,
    erstelle_antwortentwurf,
)
from mietinkasso.abgleichstatus.kontoabdeckung import (
    STATUS_UNBEKANNT,
    STATUS_VOLLSTAENDIG_BELEGT,
    VertragNichtVerfuegbarError,
    VertragsCheck,
    berechne_vertragscheck,
)
from mietinkasso.backoffice import dependencies as deps
from mietinkasso.backoffice.auth import _ctx, _current_session, _fehlerseite, _layout, _verify_csrf
from mietinkasso.backoffice.views import eur
from mietinkasso.bank.repository import VertragZahlungszielBindungRepository
from mietinkasso.indexautomatik.mailversand_service import bank_konto_fuer_vertrag_eindeutig
from mietinkasso.indexautomatik.zeit import heute_wien

#: Ohne eigenes Prefix - das gemeinsame `/backoffice`-Prefix setzt `backoffice/app.py`.
router = APIRouter()


def _datum(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def _lade_check(ctx, vertrag_id: str, von: date | None = None, bis: date | None = None) -> VertragsCheck:
    vertrag = deps._stammdaten_repo.get_vertrag(vertrag_id)
    if vertrag is None or not ctx.has_zugriff(vertrag.gesellschaft_id):
        raise VertragNichtVerfuegbarError("Vertrag ist unbekannt, nicht zugänglich oder ausgeschlossen.")
    bindung_repo = VertragZahlungszielBindungRepository(deps._bank_repo.session_factory)
    bank = bank_konto_fuer_vertrag_eindeutig(
        deps._bank_repo, bindung_repo, gesellschaft_id=vertrag.gesellschaft_id, vertrag_id=vertrag_id,
    )
    return berechne_vertragscheck(
        ctx=ctx, vertrag_id=vertrag_id, stammdaten_repository=deps._stammdaten_repo, op_service=deps._op_service,
        bank_repository=deps._bank_repo, bank_service=deps._bank_service,
        nachweis_repository=deps._abgleich_nachweis_repo, mahn_fall_repository=deps._mahn_fall_repo,
        mitmieter_repository=deps._mitmieter_repo, zahlungsziel_konto_id=bank.id if bank else None,
        von=von, bis=bis, heute=heute_wien(),
    )


def _liste(zeilen: list[str], leer: str) -> str:
    if not zeilen:
        return f'<p class="muted">{h(leer)}</p>'
    return "<ul>" + "".join(f"<li>{z}</li>" for z in zeilen) + "</ul>"


def _abdeckung_html(check: VertragsCheck) -> str:
    if check.bank_konto_id is None:
        return '<p><span class="badge badge-warn">UNBEKANNT</span> Kein eindeutig zugeordnetes Bankkonto.</p>'
    abdeckung = check.abdeckung
    iban = f" (IBAN endet auf {h(check.bank_iban_letzte4)})" if check.bank_iban_letzte4 else ""
    teile = [f"<p><strong>Bank: {h(check.bank_bezeichnung or '')}</strong>{iban}</p>"]
    if abdeckung.status == STATUS_UNBEKANNT:
        teile.append(
            f'<p><span class="badge badge-warn">UNBEKANNT</span> Belegte Perioden fehlen für '
            f"{_datum(check.von)} bis {_datum(check.bis)}.</p>"
        )
    else:
        klasse = "badge-ok" if abdeckung.status == STATUS_VOLLSTAENDIG_BELEGT else "badge-warn"
        teile.append(f'<p><span class="badge {klasse}">{h(abdeckung.status)}</span></p>')
        teile.append(_liste(
            [
                f"Belegt {_datum(i.von)} bis {_datum(i.bis)}: "
                + ", ".join(f"{h(beleg)} (Prüfsumme {h(sha[:12])}…)" for beleg, sha in i.belege)
                for i in abdeckung.abgedeckt
            ],
            "Keine belegten Perioden.",
        ))
        teile.append(_liste(
            [f"Lücke {_datum(l.von)} bis {_datum(l.bis)}: unbekannt" for l in abdeckung.luecken], "Keine Lücken.",
        ))
    vollstaendig = _datum(check.bankvollstaendigkeit_bis) if check.bankvollstaendigkeit_bis else "nicht bestätigt"
    teile.append(
        f'<p class="muted">Bankvollständigkeit (getrennte Bestätigung, keine Periode mit Beleg): {vollstaendig}</p>'
    )
    for stand in check.abgleich_staende:
        teile.append(
            f'<p class="muted">Abgleichnachweis {h(stand.bank_bezeichnung)}: {_datum(stand.geprueft_von)} bis '
            f"{_datum(stand.geprueft_bis)} (Prüfung der Mieteingänge, kein Periodennachweis).</p>"
        )
    return "".join(teile)


def _check_html(check: VertragsCheck, zurueck_href: str) -> str:
    stand = check.zahlungsstand
    if stand.konto_id is None or stand.saldo_cent is None:
        saldo = '<p><span class="badge badge-warn">Kein Mietkonto</span> Kein Saldo vorhanden (nicht 0).</p>'
    else:
        rest = check.faelliger_unstrittiger_rest_cent
        saldo = (
            f"<p>Kontostand: <strong>{eur(stand.saldo_cent)}</strong> · Offen: {eur(stand.offen_cent)} · "
            f"Guthaben: {eur(stand.guthaben_cent)}"
            + (f" · Fälliger Rest laut OP: {eur(rest)}" if rest is not None else "")
            + "</p>"
        )
    zeitraeume = _liste(
        [f"{h(z.bezeichnung)} ({h(z.art)}): {eur(z.rest_cent)}" for z in stand.offene_zeitraeume],
        "Keine offenen Posten aus gebuchten Vorschreibungen.",
    )
    teilzahlungen = _liste(
        [f"{h(t.bezeichnung)}: {eur(t.betrag_cent)}, noch offen {eur(t.rest_cent)}" for t in check.teilzahlungen],
        "Keine teilweise gedeckten Positionen.",
    )
    letzte = _datum(stand.letzte_erfasste_zahlung) if stand.letzte_erfasste_zahlung else "keine erfasst"

    def _zahler(z) -> str:
        zahler = f"Zahler laut Bank (ungeprüft): {h(z.zahler_laut_bank)}" if z.zahler_laut_bank else "Zahler laut Bank nicht angegeben"
        sammel = (
            f' <span class="warn">Bankzeile auch {z.weitere_konten_derselben_bankzeile} anderem/n Mietkonto/en zugeordnet.</span>'
            if z.weitere_konten_derselben_bankzeile else ""
        )
        return f"{_datum(z.buchungsdatum)} · {eur(z.betrag_cent)} · {zahler}{sammel}"

    zahlungen = _liste([_zahler(z) for z in check.zahlungen_mit_bank], "Keine gebuchten Zahlungen mit Bankbezug.")
    kandidaten = _liste(
        [
            f"Bankzeile {k.bank_transaktion_id}: {_datum(k.buchungsdatum)} · {eur(k.betrag_cent)}"
            + (f" · Referenz nennt {k.genannte_vertraege} Verträge" if k.genannte_vertraege > 1 else "")
            + (" · Referenz teilweise unklar" if k.offener_rest_unbestimmt else "")
            for k in check.kandidaten
        ],
        "Keine nicht zugeordneten Bankzeilen mit dieser Vertragskennung.",
    )
    ruecklastschriften = _liste(
        [f"{_datum(r.buchungsdatum)} · {eur(r.betrag_cent)} · {h(r.beleg_referenz or 'ohne Referenz')}" for r in check.ruecklastschriften],
        "Keine gebuchten Rücklastschriften.",
    )
    kaution = check.kaution
    kaution_html = (
        "<ul>"
        f"<li>Vereinbart: {eur(kaution.vereinbart_cent) if kaution.vereinbart_cent is not None else 'unbekannt'}"
        + (f" ({h(kaution.vereinbart_beleg)})" if kaution.vereinbart_beleg else "")
        + "</li>"
        f"<li>Eingang: {eur(kaution.eingang_cent) if kaution.eingang_cent is not None else 'unbekannt'}"
        + (f", Stichtag {_datum(kaution.eingang_stichtag)}" if kaution.eingang_stichtag else "")
        + "</li>"
        + (f"<li>Abgeschlossen am {_datum(kaution.abgeschlossen_am)}</li>" if kaution.abgeschlossen_am else "")
        + "</ul><p class=\"muted\">Kaution ist nicht Teil des Saldos und wird nie verrechnet.</p>"
    )
    parteien = _liste(
        [f"{h(p.rolle)}: {h(p.name or '(ohne Name)')} ({h(p.zustand)})" for p in check.weitere_parteien],
        "Keine weiteren Parteien erfasst.",
    )
    weitere_vertraege = _liste(
        [f'<a href="/backoffice/vertrag/{h(v)}/kontoabdeckung">{h(v)}</a>' for v in check.weitere_vertraege_desselben_debitors],
        "Kein weiterer Vertrag mit derselben Debitor-ID.",
    )
    hinweise = "".join(
        f'<li data-hinweis="{h(x.schluessel)}"><strong>{h(x.art)}</strong>: {h(x.text)}</li>' for x in check.hinweise
    )
    return f"""
    <div class="card">
      <h2>Kontoabdeckung und Prüfstand</h2>
      <p class="muted">Nur Anzeige: keine Mahnfreigabe, keine Aufgabe, keine Buchung. Stand {_datum(check.stichtag)}.</p>
      <form method="get" class="filter-form">
        <label>Von <input type="date" name="von" value="{check.von.isoformat()}"></label>
        <label>Bis <input type="date" name="bis" value="{check.bis.isoformat()}"></label>
        <button type="submit">Zeitraum prüfen</button>
      </form>
      {_abdeckung_html(check)}
    </div>
    <div class="card"><h3>Saldo (OP-Berechnung)</h3>{saldo}{zeitraeume}<p>Letzte erfasste Zahlung: {letzte}
      <span class="muted">(keine Vollständigkeitsaussage)</span></p></div>
    <div class="card"><h3>Teilzahlungen</h3>{teilzahlungen}</div>
    <div class="card"><h3>Zahlungen mit Bankbezug / mögliche Fremdzahler</h3>{zahlungen}
      <p class="muted">Es findet kein Namensvergleich statt. Ohne Bankbezug: {check.zahlungen_ohne_bankbezug}.</p></div>
    <div class="card"><h3>Kandidaten (nicht gebucht)</h3>{kandidaten}</div>
    <div class="card"><h3>Rücklastschriften</h3>{ruecklastschriften}</div>
    <div class="card"><h3>Kaution</h3>{kaution_html}</div>
    <div class="card"><h3>Mehrere Mieter / weitere Verträge</h3>{parteien}{weitere_vertraege}</div>
    <div class="card"><h3>Konkrete Lücken</h3><ul>{hinweise or '<li class="muted">Keine berechneten Hinweise.</li>'}</ul></div>
    <p><a href="{h(zurueck_href)}">&larr; Zurück zur Mieterakte</a></p>"""


def _parse_datum(wert: str | None) -> date | None:
    return date.fromisoformat(wert) if wert else None


@router.get("/vertrag/{vertrag_id}/kontoabdeckung", response_class=HTMLResponse)
def vertrag_kontoabdeckung(
    request: Request, vertrag_id: str, von: str | None = None, bis: str | None = None, session=Depends(_current_session),
) -> HTMLResponse:
    zurueck = f"/backoffice/vertrag/{vertrag_id}"
    try:
        check = _lade_check(_ctx(session), vertrag_id, _parse_datum(von), _parse_datum(bis))
    except VertragNichtVerfuegbarError as exc:
        return _fehlerseite(session, "Kontoabdeckung", str(exc), "/backoffice/vertraege")
    except ValueError as exc:
        return _fehlerseite(session, "Kontoabdeckung", f"Ungültiger Zeitraum: {exc}", zurueck)
    return _layout(request, session, f"Kontoabdeckung {vertrag_id}", _check_html(check, zurueck))


def _lade_fakten(ctx, vertrag_id: str) -> AntwortFakten:
    check = _lade_check(ctx, vertrag_id)  # prüft Zugriff und Ausschluss
    vertrag = deps._stammdaten_repo.get_vertrag(vertrag_id)
    einheit = deps._stammdaten_repo.get_einheit(vertrag.einheit_id)
    objekt = deps._stammdaten_repo.get_objekt(einheit.objekt_id)
    debitor = deps._stammdaten_repo.get_debitor(vertrag.debitor_id)
    return AntwortFakten(
        vertrag_id=vertrag_id, objekt_id=objekt.id, objekt_bezeichnung=objekt.bezeichnung,
        einheit_bezeichnung=einheit.bezeichnung, gueltig_bis=vertrag.gueltig_bis,
        profil=deps._stammdaten_repo.neuestes_mietvertragsprofil(vertrag_id),
        komponenten=tuple(deps._stammdaten_repo.list_aktive_komponenten(vertrag_id, check.stichtag)),
        debitor_email=debitor.email if debitor else None, debitor_telefon=debitor.telefon if debitor else None,
        debitor_adresse=debitor.adresse if debitor else None, check=check,
    )


def _entwurf_formular(vertrag_id: str, csrf: str, frage: str = "") -> str:
    return f"""
    <div class="card">
      <h2>Mieterantwort-Entwurf</h2>
      <p class="muted">Erzeugt nur einen ENTWURF aus vorhandenen, belegten Daten dieses Vertrags. Es wird nichts
        gespeichert und nichts versendet. Die Zuordnung per fester Regel ersetzt keine Prüfung.</p>
      <form method="post" action="/backoffice/vertrag/{h(vertrag_id)}/antwortentwurf">
        <input type="hidden" name="csrf_token" value="{h(csrf)}">
        <label for="frage">Frage(n), ein Punkt je Zeile</label>
        <textarea id="frage" name="frage" rows="8" maxlength="{MAX_EINGABE_ZEICHEN}">{h(frage)}</textarea>
        <label>
          <input type="checkbox" name="bezug_vertrag_id" value="{h(vertrag_id)}">
          Der Bezug dieser Frage auf Vertrag {h(vertrag_id)} ist über eine eindeutige Kennung geprüft (nicht über einen Namen).
        </label>
        <button type="submit">Entwurf erzeugen</button>
      </form>
    </div>"""


@router.get("/vertrag/{vertrag_id}/antwortentwurf", response_class=HTMLResponse)
def vertrag_antwortentwurf_formular(request: Request, vertrag_id: str, session=Depends(_current_session)) -> HTMLResponse:
    try:
        _lade_check(_ctx(session), vertrag_id)
    except VertragNichtVerfuegbarError as exc:
        return _fehlerseite(session, "Antwortentwurf", str(exc), "/backoffice/vertraege")
    except ValueError as exc:
        return _fehlerseite(session, "Antwortentwurf", f"Ungeklärter Vertragszeitraum: {exc}", "/backoffice/vertraege")
    return _layout(request, session, f"Antwortentwurf {vertrag_id}", _entwurf_formular(vertrag_id, session.csrf_token))


@router.post("/vertrag/{vertrag_id}/antwortentwurf", response_class=HTMLResponse)
def vertrag_antwortentwurf_erzeugen(
    request: Request,
    vertrag_id: str,
    csrf_token: str = Form(...),
    frage: str = Form(""),
    teilfrage: list[str] = Form(default=[]),
    bezug_vertrag_id: str = Form(""),
    session=Depends(_current_session),
) -> HTMLResponse:
    _verify_csrf(session, csrf_token)
    zurueck = f"/backoffice/vertrag/{vertrag_id}/antwortentwurf"
    if len(frage) + sum(len(t) for t in teilfrage) > MAX_EINGABE_ZEICHEN:
        return _fehlerseite(session, "Antwortentwurf", f"Eingabe länger als {MAX_EINGABE_ZEICHEN} Zeichen.", zurueck)
    try:
        fakten = _lade_fakten(_ctx(session), vertrag_id)
    except VertragNichtVerfuegbarError as exc:
        return _fehlerseite(session, "Antwortentwurf", str(exc), "/backoffice/vertraege")
    except ValueError as exc:
        return _fehlerseite(session, "Antwortentwurf", f"Ungeklärter Vertragszeitraum: {exc}", "/backoffice/vertraege")
    try:
        entwurf = erstelle_antwortentwurf(frage=frage, teilfragen=teilfrage, fakten=fakten, bezug_vertrag_id=bezug_vertrag_id)
    except ValueError as exc:
        return _fehlerseite(session, "Antwortentwurf", str(exc), zurueck)
    if not entwurf.punkte:
        return _fehlerseite(session, "Antwortentwurf", "Keine Frage eingegeben.", zurueck)

    badge = {STATUS_OFFEN: "badge-warn", STATUS_MANUELL: "badge-warn", STATUS_BELEGT_MIT_GRENZE: "badge-warn"}
    zeilen = "".join(
        f'<tr><td data-label="Nr.">{p.nummer}</td><td data-label="Frage">{h(p.frage)}</td>'
        f'<td data-label="Status"><span class="badge {badge.get(p.status, "badge-ok")}">{h(p.status)}</span></td>'
        f'<td data-label="Antwort oder Grund">{h(p.antwort or p.grund or "")}</td>'
        f'<td data-label="Beleg">{"<br>".join(h(b) for b in p.belege) or "-"}</td></tr>'
        for p in entwurf.punkte
    )
    bezug = "" if entwurf.bezug_bestaetigt else (
        '<p class="warn">Bezug zu diesem Vertrag nicht bestätigt: alle Fragen gehen in die manuelle Prüfung.</p>'
    )
    inhalt = f"""
    <div class="card">
      <h2>ENTWURF - nicht versendet</h2>
      {bezug}
      <p>{len(entwurf.punkte)} Punkt(e), davon {entwurf.offene_punkte} offen oder in manueller Prüfung.</p>
      <table><thead><tr><th>Nr.</th><th>Frage</th><th>Status</th><th>Antwort oder Grund</th><th>Beleg</th></tr></thead>
      <tbody>{zeilen}</tbody></table>
      <h3>Entwurfstext</h3>
      <pre style="white-space:pre-wrap">{h(entwurf.text)}</pre>
      <p class="muted">Versand ausschließlich über den Mail-Verantwortlichen; von hier führt kein Sendeweg weg.</p>
    </div>
    {_entwurf_formular(vertrag_id, session.csrf_token, frage)}"""
    return _layout(request, session, f"Antwortentwurf {vertrag_id}", inhalt)
