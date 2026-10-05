"""Rückstandsübersicht (Startseite) und die drei Bereichs-Startseiten.

Rein lesend: alle vier Teilansichten der Übersicht stammen aus GENAU
EINER Berechnung (`rueckstaende.service.berechne_rueckstandsuebersicht`);
die Bereichsseiten verlinken nur bestehende Routen."""

from __future__ import annotations

from html import escape as h
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from mietinkasso.abgleichstatus.service import berechne_abgleichstatus
from mietinkasso.backoffice.views import eur, nutzungsstatus_label, option, sperrgrund_label
from mietinkasso.domain.exceptions import MietinkassoError
from mietinkasso.indexautomatik.zeit import heute_wien
from mietinkasso.rueckstaende.service import berechne_rueckstandsuebersicht

from mietinkasso.backoffice import dependencies as deps
from mietinkasso.backoffice.auth import _ctx, _current_session, _fehlerseite, _layout
from mietinkasso.backoffice.routes.shared import listen_links_html


#: Ohne eigenes Prefix - das gemeinsame `/backoffice`-Prefix wird GENAU
#: EINMAL in `backoffice/app.py` gesetzt.
router = APIRouter()


# -- Dashboard: Gesellschaft/Objekt -> Mietkontenübersicht -------------------


def _rueckstaende_objekt_filter_form(uebersicht, *, action: str = "/backoffice/") -> str:
    options = [option("", "Alle Objekte", selected=(uebersicht.objekt_filter is None))]
    je_gesellschaft: dict[str, list] = {}
    for o in uebersicht.objekt_optionen:
        je_gesellschaft.setdefault(o.gesellschaft_name, []).append(o)
    for gesellschaft_name, objekte in je_gesellschaft.items():
        options.append(f'<optgroup label="{h(gesellschaft_name)}">')
        for o in objekte:
            options.append(option(o.id, o.bezeichnung, selected=(o.id == uebersicht.objekt_filter)))
        options.append("</optgroup>")
    return f"""
    <div class="card">
      <form method="get" action="{h(action)}">
        <label>Objekt</label>
        <select name="objekt_id" onchange="this.form.submit()">{''.join(options)}</select>
        <noscript><button type="submit">Anzeigen</button></noscript>
      </form>
    </div>"""


def _rueckstaende_kpi_html(k) -> str:
    """Vier verständliche Kennzahlen für den Alltag (Auftrag
    HV-20260914-UI-EINFACH) statt fünf technisch benannter Summen -
    dieselben bereits vorhandenen Zahlen aus `RueckstandsKennzahlen`,
    NUR neu beschriftet/gruppiert. "Davon fällig" ist die Summe der
    EINZELPOSITIONEN mit bekannter, verstrichener Fälligkeit (nicht
    identisch mit der Kontosaldo-Rechnung - siehe Erklärung), eine
    unbekannte Fälligkeit heißt "Fälligkeit prüfen" (NICHT "strittig"),
    und ein Guthaben wird nie gegen einen Rückstand verrechnet. Die
    bisherige fünfte Zahl ("Noch nicht fällig") bleibt unter "weitere
    Kennzahlen" erreichbar, damit keine Information verloren geht."""

    def _kpi(label: str, cent: int) -> str:
        return f'<div class="kpi"><span class="zahl">{eur(cent)}</span><span class="kpi-label">{h(label)}</span></div>'

    return f"""
    <div class="kpi-grid">
      {_kpi("Offene Beträge", k.summe_positiver_kontostaende_cent)}
      {_kpi("Davon fällig", k.ueberfaellig_cent)}
      {_kpi("Fälligkeit prüfen", k.faelligkeit_unbekannt_cent)}
      {_kpi("Guthaben der Mieter", k.summe_guthaben_cent)}
    </div>
    <details class="card">
      <summary>Was bedeutet das? / weitere Kennzahlen</summary>
      <ul>
        <li><strong>Offene Beträge</strong>: Summe aller positiven Mietkonten (Rückstände) - ein Guthaben
            eines anderen Mieters wird NIE davon abgezogen.</li>
        <li><strong>Davon fällig</strong>: der Teil der offenen Beträge, dessen Fälligkeit bereits verstrichen
            ist (Summe der einzelnen offenen Posten, siehe "Details" je Zeile) - eine bekannte Fälligkeit ist
            KEINE Mahnfreigabe, eine bestehende Sperre gilt unabhängig davon.</li>
        <li><strong>Fälligkeit prüfen</strong>: offene Posten ohne erfasstes Fälligkeitsdatum - das heißt
            NICHT automatisch "strittig", sondern nur: das Datum fehlt noch in den Stammdaten.</li>
        <li><strong>Guthaben der Mieter</strong>: Summe aller negativen Mietkonten - eigenes Geld der Mieter,
            wird nicht automatisch verrechnet.</li>
        <li class="muted">Weitere Kennzahl: Noch nicht fällig {eur(k.nicht_faellig_cent)}.</li>
      </ul>
    </details>"""


def _erledigen_html(uebersicht) -> str:
    """"Das ist zu erledigen" (Auftrag HV-20260914-AUFGABEN-MIETERAKTE,
    ersetzt die bisherigen Sammelzähler aus HV-20260914-UI-EINFACH) -
    reine Umgruppierung/Filterung der bereits in `uebersicht` vorhandenen,
    bereits berechneten Zeilen (offene Positionen, Mietkonten,
    Sperrgründe) je Vertrag (= eine Person/ein Mietverhältnis), KEINE
    neue Berechnung, KEINE neue persistente Aufgaben-Ablage und KEINE
    Berechnung/Versand als Nebeneffekt dieses GET. Mehrere gleichzeitig
    zutreffende Gründe derselben Person werden gesammelt statt sich
    gegenseitig zu verdecken (vorher: eine gemeinsame Zähler-Liste ohne
    Personenbezug, effektiv eine elif-artige Priorisierung). Jeder Grund
    verlinkt direkt zur passenden Sektion der eigenen Mieterakte
    (`/backoffice/vertrag/{vertrag_id}#...`, siehe
    `vertragsanlage_form.py::detail_ansicht`) statt zu einer allgemeinen
    Dashboard-Tabelle. Eine gültige Sperre/ein Ratenplan/Anwaltsfall wird
    NIE als Aufforderung zum Entsperren dargestellt, sondern als
    "vor einer Mahnung berücksichtigen/Status prüfen". Eine Mahnsperre
    auf einem bereits ausgeglichenen/Guthaben-Konto ist HIER weiterhin
    KEINE Handlungsaufgabe (nichts zu mahnen gibt es nicht)."""

    # Vollständige Metadaten (Objekt/Einheit/Mieter) kommen bevorzugt aus
    # `uebersicht.mietkonten` - DIE Zeile deckt JEDEN Vertrag im
    # Objekt-Scope ab, auch ohne eigenes Mietkonto (siehe
    # `rueckstaende/service.py::berechne_rueckstandsuebersicht`).
    # `OffenePositionZeile` (unbekannte Fälligkeit) trägt selbst KEINE
    # Einheit - ohne diese Vor-Befüllung blieb die Einheit für einen
    # Vertrag leer, der NUR diesen einen Grund hatte (Codex-Rückprüfung
    # 14.09.2026).
    mietkonten_by_vertrag = {z.vertrag_id: z for z in uebersicht.mietkonten}
    # Der aktive Objektfilter wird an jeden Aktionslink angehängt, damit
    # die Mieterakte per "zurück"-Link zur GEFILTERTEN Übersicht
    # zurückführt statt stillschweigend auf "Alle Objekte" zu wechseln.
    von_objekt_param = f"?von_objekt={h(uebersicht.objekt_filter)}" if uebersicht.objekt_filter else ""

    eintraege: dict[str, dict] = {}

    def _eintrag(vertrag_id: str, fallback_quelle=None) -> dict:
        e = eintraege.get(vertrag_id)
        if e is not None:
            return e
        z = mietkonten_by_vertrag.get(vertrag_id)
        quelle = z or fallback_quelle
        e = {
            "debitor_name": quelle.debitor_name,
            "objekt_bezeichnung": quelle.objekt_bezeichnung,
            "einheit_bezeichnung": getattr(z, "einheit_bezeichnung", None),
            "gruende": [],
        }
        eintraege[vertrag_id] = e
        return e

    unbekannte_je_vertrag: dict[str, list] = {}
    for p in uebersicht.offene_positionen:
        if p.faelligkeitsklasse == "UNBEKANNT":
            unbekannte_je_vertrag.setdefault(p.vertrag_id, []).append(p)
    for vertrag_id, positionen in unbekannte_je_vertrag.items():
        e = _eintrag(vertrag_id, fallback_quelle=positionen[0])
        summe = sum(pos.rest_cent for pos in positionen)
        e["gruende"].append((
            "warn",
            f"{len(positionen)} offene Position(en) ohne erfasste Fälligkeit ({eur(summe)})",
            "Fälligkeit klären",
            f"/backoffice/vertrag/{h(vertrag_id)}{von_objekt_param}#zahlungen",
        ))

    for z in uebersicht.mietkonten:
        if z.abweichung_saldo_zu_positionen_cent:
            e = _eintrag(z.vertrag_id, fallback_quelle=z)
            e["gruende"].append((
                "warn",
                f"Abweichung Kontostand/Einzelpositionen ({eur(z.abweichung_saldo_zu_positionen_cent)})",
                "Buchungen vergleichen",
                f"/backoffice/vertrag/{h(z.vertrag_id)}{von_objekt_param}#kontodetails",
            ))
        # NUR Sperren auf tatsächlich offenen (positiven) Konten sind ein
        # handlungsbezogener Punkt - eine Sperre auf einem ausgeglichenen/
        # Guthaben-Konto betrifft keinen anstehenden Mahnlauf.
        if z.sperrgruende and z.saldo_cent is not None and z.saldo_cent > 0:
            e = _eintrag(z.vertrag_id, fallback_quelle=z)
            # Verständliche Labels statt Rohcodes (Auftrag HV-20260914-UI-
            # LESBAR: "MANUELL/RECHTSANWALT/RATENPLAN übersetzen") - der
            # Rohcode bleibt zusätzlich als title-Tooltip erhalten, keine
            # Information geht verloren. Kurzer lesbarer Titel + Grund(e)
            # primär sichtbar, der erklärende Satz ("keine Aufforderung
            # zum Entsperren") steht aufklappbar darunter - Betrag und
            # ALLE Gründe bleiben immer sichtbar (Rückprüfung Codex
            # 14.09.2026: kein langer, farbig gefüllter Fließtextbalken
            # mehr für diesen Grund).
            gruende_labels = ", ".join(sperrgrund_label(g) for g in z.sperrgruende)
            gruende_codes = ", ".join(z.sperrgruende)
            e["gruende"].append((
                "error",
                f'<span title="{h(gruende_codes)}"><strong>Mahnsperre aktiv:</strong> {h(gruende_labels)}</span> '
                f"· {eur(z.saldo_cent)} offen "
                '<details><summary>Was bedeutet das?</summary><p class="muted">Eine aktive Sperre ist vor '
                "einer Mahnung zu berücksichtigen - sie ist KEINE Aufforderung, sie aufzuheben.</p></details>",
                "Status prüfen",
                f"/backoffice/vertrag/{h(z.vertrag_id)}{von_objekt_param}#sperren",
            ))

    if not eintraege:
        aufgaben_html = (
            '<p class="muted">Aus den aktuell geprüften Kriterien (offene Positionen ohne Fälligkeit, '
            "Abweichungen Kontostand/Einzelpositionen, aktive Mahnsperren auf offenen Konten) ergeben sich "
            "keine Klärpunkte. Das ist KEINE Aussage über einen vollständigen Bankabgleich und KEINE "
            "Behauptung, dass alle Beträge bereits bezahlt sind - nur diese Kriterien liefern aktuell "
            "keinen Treffer.</p>"
        )
    else:
        karten = []
        for e in eintraege.values():
            gruende_html = "".join(
                f'<li><span class="aufgaben-grund-text"><span class="grund-punkt grund-punkt-{stil}"></span>'
                f'{text}</span>'
                f'<a class="aufgabe-aktion" href="{link}">{h(aktion)}</a></li>'
                for stil, text, aktion, link in e["gruende"]
            )
            einheit_zusatz = f" / {h(e['einheit_bezeichnung'])}" if e["einheit_bezeichnung"] else ""
            karten.append(
                '<li class="aufgaben-karte">'
                f'<div class="aufgaben-karte-kopf"><div class="aufgaben-name">{h(e["debitor_name"])}</div>'
                f'<div class="muted">{h(e["objekt_bezeichnung"])}{einheit_zusatz}</div></div>'
                f'<ul class="aufgaben-gruende">{gruende_html}</ul>'
                "</li>"
            )
        aufgaben_html = f'<ul class="aufgaben-liste">{"".join(karten)}</ul>'

    return f"""
    <div class="card">
      <h2>Das ist zu erledigen</h2>
      {aufgaben_html}
    </div>"""


def _rueckstaende_kompakt_zeile_html(z, *, unbekannte_faelligkeit: bool) -> str:
    """Kompakte Hauptzeile (Auftrag HV-20260914-UI-EINFACH, verschärft
    Rückprüfung 14.09.2026): Mieter/Einheit, offener Betrag/Guthaben,
    EIN primärer Status PLUS unabhängige Hinweis-Badges (Sperre/
    Abweichung/Fälligkeit prüfen können GLEICHZEITIG zutreffen - vorher
    verdeckte eine `elif`-Kette die anderen). Konto-/Vertrags-ID und die
    getrennten Kontoberechnung-/Positionen-Zahlen bleiben über "Details"
    je Zeile erreichbar - keine neue Saldologik, nur andere Anzeige
    derselben bereits berechneten Werte. "Rückstand offen" wird jetzt aus
    dem tatsächlich FÄLLIGEN Rest abgeleitet
    (`faelliger_unstrittiger_rest_cent`, bereits vorhandene
    Kontoberechnung mit korrekter Fälligkeitsprüfung) statt aus dem
    rohen positiven Saldo - ein rein künftig fälliges Soll (z. B.
    Fälligkeit 2099) hieß vorher fälschlich "Rückstand offen". Ist
    `faelliger_unstrittiger_rest_cent` 0, heißt das NICHT automatisch
    "noch nicht fällig" - z. B. eine reine Eröffnung ohne bekanntes
    Fälligkeitsdatum, oder eine bereits fällige, aber aus dem
    unstrittigen Rest ausgeschlossene Position, wäre damit falsch
    beschriftet. Ohne eine neue Berechnung echter Zukunftsfälligkeit
    bleibt dieser Fall darum bewusst neutral "Offener Betrag" - die
    unabhängigen Hinweise (u. a. "Fälligkeit prüfen") bleiben davon
    unberührt."""

    if z.saldo_cent is None:
        status_html = '<span class="badge badge-muted">kein Mietkonto</span>'
    elif z.saldo_cent > 0 and z.faelliger_unstrittiger_rest_cent:
        status_html = '<span class="badge badge-error">Rückstand offen</span>'
    elif z.saldo_cent > 0:
        status_html = '<span class="badge badge-muted">Offener Betrag</span>'
    elif z.saldo_cent < 0:
        status_html = '<span class="badge badge-ok">Guthaben</span>'
    else:
        status_html = '<span class="badge badge-ok">ausgeglichen</span>'

    hinweise = []
    if z.sperrgruende:
        sperrgrund_labels = ", ".join(sperrgrund_label(g) for g in z.sperrgruende)
        sperrgrund_codes = ", ".join(z.sperrgruende)
        hinweise.append(
            f'<span class="badge badge-error" title="{h(sperrgrund_codes)}">Mahnung gesperrt ({h(sperrgrund_labels)})</span>'
        )
    if z.abweichung_saldo_zu_positionen_cent:
        hinweise.append('<span class="badge badge-warn">Klärung nötig (Kontoabweichung)</span>')
    if unbekannte_faelligkeit:
        hinweise.append('<span class="badge badge-warn">Klärung nötig (Fälligkeit prüfen)</span>')
    if z.historisch:
        hinweise.append('<span class="badge badge-muted">historisch</span>')
    if z.mahnfaelle_anzahl:
        hinweise.append(f'<span class="badge badge-muted">{z.mahnfaelle_anzahl} Mahnfall(e)</span>')
    if hinweise:
        status_html += " " + " ".join(hinweise)

    betrag_html = eur(z.saldo_cent) if z.saldo_cent is not None else "-"
    details_zeilen = []
    if z.konto_id:
        details_zeilen.append(f"Konto: <code>{h(z.konto_id)}</code>")
    details_zeilen.append(f"Vertrag: <code>{h(z.vertrag_id)}</code>")
    if z.faelliger_unstrittiger_rest_cent is not None:
        details_zeilen.append(f"Fällig (Kontoberechnung): {eur(z.faelliger_unstrittiger_rest_cent)}")
    if z.positionen_faelliger_rest_cent is not None:
        details_zeilen.append(f"Fällig (Positionen): {eur(z.positionen_faelliger_rest_cent)}")
    if z.positionen_rest_gesamt_cent is not None:
        details_zeilen.append(f"Rest gesamt (Positionen): {eur(z.positionen_rest_gesamt_cent)}")
    if z.abweichung_saldo_zu_positionen_cent:
        details_zeilen.append(f"Abweichung Konto/Positionen: {eur(z.abweichung_saldo_zu_positionen_cent)}")
    details_html = "<br>".join(details_zeilen)
    mahnvorschau_html = (
        f' · <a href="/backoffice/vertrag/{h(z.vertrag_id)}/mahnvorschau">Mahnvorschau</a>' if z.konto_id else ""
    )
    mieter_label = f"{h(z.debitor_name)}<br><span class='muted'>{h(z.objekt_bezeichnung)} / {h(z.einheit_bezeichnung)} ({h(nutzungsstatus_label(z.nutzungsstatus))})</span>"
    return (
        f"<tr class='{'gesperrt-row' if z.sperrgruende else ''}'>"
        f'<td data-label="Mieter / Einheit">{mieter_label}</td>'
        f'<td data-label="Offener Betrag / Guthaben">{betrag_html}</td>'
        f'<td data-label="Status">{status_html}</td>'
        f'<td data-label="">'
        f'<a href="/backoffice/vertrag/{h(z.vertrag_id)}">Akte öffnen</a>{mahnvorschau_html}'
        f"<details><summary>Details</summary>{details_html}</details></td>"
        "</tr>"
    )


def _rueckstaende_mietkonto_zeile_html(z) -> str:
    status_tags = []
    if z.historisch:
        status_tags.append('<span class="badge badge-warn">historisch</span>')
    if z.nutzungsstatus == "LEERSTAND":
        status_tags.append('<span class="badge badge-warn">Leerstand</span>')
    if z.sperrgruende:
        sperrgrund_labels = ", ".join(sperrgrund_label(g) for g in z.sperrgruende)
        status_tags.append(f'<span class="badge badge-error">Sperre: {h(sperrgrund_labels)}</span>')
    if z.mahnfaelle_anzahl:
        status_tags.append(f'<span class="badge badge-muted">{z.mahnfaelle_anzahl} Mahnfall(e), siehe Tabelle unten</span>')
    konto_link = f'<a href="/backoffice/konto/{h(z.konto_id)}">{h(z.konto_id)}</a>' if z.konto_id else "-"
    mahnvorschau_link = (
        f'<a href="/backoffice/vertrag/{h(z.vertrag_id)}/mahnvorschau">Mahnvorschau</a>' if z.konto_id else ""
    )
    # Abweichung Kontostand <-> Summe der Einzelpositionen NIE
    # verschweigen (z. B. eine Korrekturbuchung ohne eigene offene
    # Position) - nur bei Gleichstand "–" zeigen, sonst deutlich als
    # Betrag mit Vorzeichen.
    if z.abweichung_saldo_zu_positionen_cent:
        abweichung_html = f'<span class="badge badge-warn">{eur(z.abweichung_saldo_zu_positionen_cent)}</span>'
    elif z.abweichung_saldo_zu_positionen_cent == 0:
        abweichung_html = "–"
    else:
        abweichung_html = "-"
    return (
        f"<tr class='{'gesperrt-row' if z.sperrgruende else ''}'>"
        f"<td>{h(z.objekt_bezeichnung)}</td>"
        f"<td>{h(z.vertrag_id)}</td><td>{h(z.einheit_bezeichnung)}</td>"
        f"<td>{h(z.nutzungsstatus)}</td>"
        f"<td>{h(z.debitor_name)}</td>"
        f"<td>{konto_link}</td>"
        f"<td>{eur(z.saldo_cent) if z.saldo_cent is not None else '-'}</td>"
        f"<td>{eur(z.faelliger_unstrittiger_rest_cent) if z.faelliger_unstrittiger_rest_cent is not None else '-'}</td>"
        f"<td>{eur(z.positionen_faelliger_rest_cent) if z.positionen_faelliger_rest_cent is not None else '-'}</td>"
        f"<td>{eur(z.positionen_rest_gesamt_cent) if z.positionen_rest_gesamt_cent is not None else '-'}</td>"
        f"<td>{abweichung_html}</td>"
        f"<td>{' '.join(status_tags)}</td>"
        f"<td>{mahnvorschau_link}</td>"
        "</tr>"
    )


_FAELLIGKEITSKLASSE_BADGE = {
    "UEBERFAELLIG": '<span class="badge badge-error">fällig/überfällig</span>',
    "NICHT_FAELLIG": '<span class="badge badge-muted">noch nicht fällig</span>',
    "UNBEKANNT": '<span class="badge badge-warn">Fälligkeit unbekannt</span>',
}


def _rueckstaende_position_zeile_html(p) -> str:
    # `faelligkeit_bekannt` ist die maßgebliche Angabe - ein trotzdem
    # gespeichertes Datum bei faelligkeit_bekannt=False (inkonsistente
    # Altdaten) darf NIE wie ein bestätigtes Datum aussehen.
    faelligkeit_html = (
        p.faelligkeit.isoformat() if (p.faelligkeit_bekannt and p.faelligkeit) else '<span class="muted">unbekannt</span>'
    )
    return (
        "<tr>"
        f"<td>{h(p.objekt_bezeichnung)}</td>"
        f"<td>{h(p.vertrag_id)}</td><td>{h(p.debitor_name)}</td>"
        f"<td>#{p.op_position_id}</td>"
        f"<td>{h(p.beleg_referenz)}</td>"
        f"<td>{h(p.art)}</td>"
        f"<td>{h(p.leistungsperiode or '')}</td>"
        f"<td>{p.belegdatum.isoformat()}</td>"
        f"<td>{faelligkeit_html}</td>"
        f"<td>{eur(p.rest_cent)}</td>"
        f"<td>{_FAELLIGKEITSKLASSE_BADGE.get(p.faelligkeitsklasse, '')}</td>"
        f"<td><a href='/backoffice/konto/{h(p.konto_id)}'>Konto</a></td>"
        "</tr>"
    )


def _rueckstaende_mahnfall_zeile_html(m) -> str:
    return (
        "<tr>"
        f"<td>{h(m.objekt_bezeichnung)}</td><td>{h(m.vertrag_id)}</td>"
        f"<td>#{m.forderung_op_position_id}</td>"
        f"<td>{m.stufe}</td><td>{h(m.status)}</td>"
        f"<td>{eur(m.betrag_cent)}</td>"
        f"<td>{m.geplant_am.date().isoformat()}</td>"
        "</tr>"
    )


# -- Zahlungs- und Abgleichstand (Auftrag HV-20261005-ABGLEICHSTATUS) --------
# Reine Anzeige von `abgleichstatus.service.berechne_abgleichstatus`, das
# auf DERSELBEN `RueckstandsUebersicht` aufsetzt - keine eigene Saldologik,
# keine Schreibzugriffe.


def _datum(d) -> str:
    return d.strftime("%d.%m.%Y")


def _abgleich_link(objekt_filter: str | None) -> str:
    parameter = f"?{urlencode({'objekt_id': objekt_filter})}" if objekt_filter else ""
    return f"/backoffice/abgleichstatus{parameter}"


def _abgleich_nachweis_satz(status) -> str:
    if not status.objekte:
        return '<span class="muted">Keine Objekte im Filter.</span>'
    ohne = status.objekte_ohne_nachweis
    if ohne == len(status.objekte):
        return '<span class="badge badge-warn">Noch kein Abgleichnachweis</span>'
    teile = [f"{len(status.objekte) - ohne} von {len(status.objekte)} Objekt(en) mit Abgleichnachweis"]
    aeltester = status.aeltester_stand
    if aeltester is not None and aeltester < status.stichtag:
        teile.append(f'<span class="warn">Stand vom {_datum(aeltester)}; neuere Eingänge noch prüfen</span>')
    elif aeltester is not None:
        teile.append(f"Geprüft bis {_datum(aeltester)}")
    if ohne:
        teile.append(f'<span class="warn">{ohne} Objekt(e): Noch kein Abgleichnachweis</span>')
    return " · ".join(teile)


def _abgleich_karte_html(status) -> str:
    return f"""
    <div class="card" id="abgleichstand">
      <h2>Zahlungs- und Abgleichstand</h2>
      <p class="status-zeile">
        <span>Offene Beträge: <strong>{eur(status.summe_offen_cent)}</strong></span>
        <span>Guthaben der Mieter: <strong>{eur(status.summe_guthaben_cent)}</strong></span>
        <span>Eingereicht, Bankeingang noch offen: <strong>{eur(status.einzuege_offen_cent)}</strong>
          ({status.einzuege_offen_anzahl})</span>
      </p>
      <p>{_abgleich_nachweis_satz(status)}</p>
      <p><a href="{h(_abgleich_link(status.objekt_filter))}">Abgleichstand im Detail &rarr;</a></p>
    </div>"""


def _abgleich_stand_html(s) -> str:
    if s.veraltet:
        stand = f'<span class="badge badge-warn">Stand vom {_datum(s.geprueft_bis)}; neuere Eingänge noch prüfen</span>'
    else:
        stand = f'<span class="badge badge-ok">Geprüft bis {_datum(s.geprueft_bis)}</span>'
    iban = f" (IBAN endet auf {h(s.iban_letzte4)})" if s.iban_letzte4 else ""
    vollstaendig = (
        f"bestätigt bis {_datum(s.bankvollstaendigkeit_bis)}" if s.bankvollstaendigkeit_bis else "nicht bestätigt"
    )
    korrektur = (
        f'<li class="muted">Ersetzt {s.fruehere_nachweise} früher erfasste(n) Nachweis(e) für dieses Bankkonto.</li>'
        if s.fruehere_nachweise else ""
    )
    return f"""
      <div class="abgleich-stand">
        <p><strong>Bank: {h(s.bank_bezeichnung)}</strong>{iban}</p>
        <p>{stand}</p>
        <details><summary>Prüfbeleg und Umfang</summary><ul>
          <li>Geprüfter Zeitraum: {_datum(s.geprueft_von)} bis {_datum(s.geprueft_bis)}</li>
          <li>Geprüft am {s.geprueft_am.strftime('%d.%m.%Y %H:%M')} durch {h(s.geprueft_durch)}</li>
          <li>Umfang: {h(s.umfang)}</li>
          <li class="muted">Quelle hinterlegt (Prüfsumme {h(s.quelle_sha256[:12])}…)</li>
          <li class="muted">Bankvollständigkeit (getrennte Bestätigung): {vollstaendig}</li>
          {korrektur}
        </ul></details>
      </div>"""


def _abgleich_objekt_html(o) -> str:
    staende = "".join(_abgleich_stand_html(s) for s in o.staende) or (
        '<p><span class="badge badge-warn">Noch kein Abgleichnachweis</span></p>'
    )
    ignoriert = (
        f'<p class="warn">{o.ignorierte_nachweise} Nachweis(e) mit nicht passendem Bankkonto nicht berücksichtigt.</p>'
        if o.ignorierte_nachweise else ""
    )
    return (
        f'<div class="card"><h3>{h(o.objekt_bezeichnung)}</h3>'
        f'<p class="muted">{h(o.gesellschaft_name)}</p>{staende}{ignoriert}</div>'
    )


def _abgleich_vertrag_zeile_html(v, von_objekt_param: str) -> str:
    if v.konto_id is None:
        zeitraeume = '<span class="muted">kein Mietkonto</span>'
    elif not v.offene_zeitraeume:
        zeitraeume = '<span class="muted">Keine offenen Posten aus gebuchten Vorschreibungen</span>'
    else:
        zeitraeume = "<br>".join(
            f"{h(z.bezeichnung)} ({h(z.art)}): {eur(z.rest_cent)}"
            + (' <span class="badge badge-warn">Fälligkeit prüfen</span>' if z.faelligkeitsklasse == "UNBEKANNT" else "")
            for z in v.offene_zeitraeume
        )
    einzuege = "<br>".join(
        f"{_datum(e.einzug_am)} · {eur(e.betrag_cent)} · "
        f'<span class="badge {"badge-warn" if e.offen else "badge-muted"}">{h(e.status_label)}</span>'
        f'<span class="muted"> · {h(e.bank_bezeichnung)}{" ••" + h(e.iban_letzte4) if e.iban_letzte4 else ""}'
        f" · Ref. {h(e.referenz)}</span>"
        for e in v.einzuege
    ) or '<span class="muted">-</span>'
    if v.ignorierte_einzuege:
        einzuege += f'<br><span class="warn">{v.ignorierte_einzuege} Einzug/Einzüge mit nicht passendem Bankkonto nicht berücksichtigt.</span>'
    letzte = _datum(v.letzte_erfasste_zahlung) if v.letzte_erfasste_zahlung else '<span class="muted">keine erfasst</span>'
    return (
        "<tr>"
        f'<td data-label="Mieter / Einheit"><a href="/backoffice/vertrag/{h(v.vertrag_id)}{von_objekt_param}">'
        f"{h(v.debitor_name)}</a><br><span class='muted'>{h(v.objekt_bezeichnung)} / {h(v.einheit_bezeichnung)}</span></td>"
        f'<td data-label="Offener Betrag">{eur(v.offen_cent) if v.offen_cent is not None else "-"}</td>'
        f'<td data-label="Guthaben">{eur(v.guthaben_cent) if v.guthaben_cent is not None else "-"}</td>'
        f'<td data-label="Offene Zeiträume">{zeitraeume}</td>'
        f'<td data-label="Letzte erfasste Zahlung">{letzte}</td>'
        f'<td data-label="Einzüge">{einzuege}</td>'
        "</tr>"
    )


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, objekt_id: str | None = None, session=Depends(_current_session)) -> HTMLResponse:
    """Zentrale Rückstandsübersicht - Standard "Alle Objekte" (nur
    erlaubte, nicht ausgeschlossene), mit gemeinsamem Objektfilter, der
    Summen/Mietkontentabelle/Einzelpositionen/Mahnsperren-Anzeige
    identisch mitfiltert (Auftrag 13.09.2026, HV-20260913-RUECKSTAENDE:
    "Aktuell ist /backoffice/ ohne Objekt leer" - alle vier Ansichten
    stammen jetzt aus GENAU EINER Berechnung,
    `rueckstaende.service.berechne_rueckstandsuebersicht`, REIN LESEND,
    keine Mahnplanung als GET-Seiteneffekt)."""

    ctx = _ctx(session)
    try:
        uebersicht = berechne_rueckstandsuebersicht(
            ctx=ctx, objekt_id=objekt_id or None, stammdaten_repository=deps._stammdaten_repo, op_service=deps._op_service,
            mahn_fall_repository=deps._mahn_fall_repo,
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Rückstandsübersicht", str(exc), "/backoffice/")

    # Auftrag HV-20260930-PORTAL-LISTEN: die drei automatischen Listen
    # sind direkt von der Übersicht erreichbar, der aktive Objektfilter
    # wird mitgenommen - reine Verlinkung.
    auswahl_form = _rueckstaende_objekt_filter_form(uebersicht) + (
        f'<div class="card">{listen_links_html(objekt_id=uebersicht.objekt_filter)}</div>'
    )
    kpi_html = _rueckstaende_kpi_html(uebersicht.kennzahlen)
    abgleich_html = _abgleich_karte_html(berechne_abgleichstatus(
        uebersicht=uebersicht, stammdaten_repository=deps._stammdaten_repo, op_service=deps._op_service,
        bank_repository=deps._bank_repo, bank_service=deps._bank_service,
        nachweis_repository=deps._abgleich_nachweis_repo,
    ))

    if uebersicht.objekt_filter is None:
        titel_zusatz = "Alle Objekte"
    else:
        gefiltertes_objekt = next((o for o in uebersicht.objekt_optionen if o.id == uebersicht.objekt_filter), None)
        titel_zusatz = (
            f"{h(gefiltertes_objekt.bezeichnung)} ({h(gefiltertes_objekt.id)})"
            if gefiltertes_objekt is not None else h(uebersicht.objekt_filter)
        )
    erledigen_html = _erledigen_html(uebersicht)

    # Für die kompakte Statusspalte: welche Verträge haben mindestens
    # eine offene Position OHNE erfasste Fälligkeit - reine Gruppierung
    # der bereits klassifizierten `offene_positionen`, keine neue
    # Fälligkeitslogik.
    vertraege_mit_unbekannter_faelligkeit = {
        p.vertrag_id for p in uebersicht.offene_positionen if p.faelligkeitsklasse == "UNBEKANNT"
    }

    def _zeile(z):
        return _rueckstaende_kompakt_zeile_html(
            z, unbekannte_faelligkeit=(z.vertrag_id in vertraege_mit_unbekannter_faelligkeit)
        )

    # Positive offene Konten ZUERST, absteigend nach Betrag - Nullsalden/
    # Guthaben/Konten ohne Mietkonto stehen NIE dazwischen, sondern
    # gesammelt in "weitere Konten" (Rückprüfung 14.09.2026). Reine
    # Anzeige-Sortierung derselben bereits berechneten `mietkonten`-Liste.
    positive_konten = sorted(
        (z for z in uebersicht.mietkonten if z.saldo_cent is not None and z.saldo_cent > 0),
        key=lambda z: z.saldo_cent, reverse=True,
    )
    weitere_konten = [z for z in uebersicht.mietkonten if not (z.saldo_cent is not None and z.saldo_cent > 0)]

    _kompakt_kopf = "<thead><tr><th>Mieter / Einheit</th><th>Offener Betrag / Guthaben</th><th>Status</th><th></th></tr></thead>"
    kompakt_html = "".join(_zeile(z) for z in positive_konten)
    kompakt_tabelle = f"""
    <div class="card" id="mietkonten-uebersicht">
      <h2>Mietkontenübersicht — {titel_zusatz}</h2>
      <div class="tabelle-kompakt">
      <table>
        {_kompakt_kopf}
        <tbody>
        {kompakt_html or '<tr><td colspan=4 class="muted">Keine offenen Beträge.</td></tr>'}
        </tbody>
      </table>
      </div>
      <details>
        <summary>Weitere Konten (ausgeglichen, Guthaben, ohne Mietkonto) ({len(weitere_konten)})</summary>
        <div class="tabelle-kompakt">
        <table>
          {_kompakt_kopf}
          <tbody>
          {"".join(_zeile(z) for z in weitere_konten) or '<tr><td colspan=4 class="muted">Keine weiteren Konten.</td></tr>'}
          </tbody>
        </table>
        </div>
      </details>
    </div>"""

    mietkonten_html = "".join(_rueckstaende_mietkonto_zeile_html(z) for z in uebersicht.mietkonten)
    mietkonten_tabelle = f"""
    <details class="card" id="mietkonten-details">
      <summary>Alle Mietkonten im Detail (Konto-/Vertrags-IDs, Kontoberechnung vs. Positionen)</summary>
      <p class="muted">"Kontostand" = Eröffnung + Vorschreibungen − Zahlungen/Gutschriften (positiv: offener
         Betrag; negativ: Guthaben). Zwei getrennte Berechnungen desselben Kontos stehen nebeneinander:
         "Fällig (Kontoberechnung)" ist die bestehende Kontostand-Rechnung, "Fällig (Positionen)"/
         "Rest gesamt (Positionen)" ist die Summe der einzelnen offenen Posten weiter unten - beide können
         voneinander abweichen (z. B. bei einer Korrekturbuchung ohne eigene Einzelposition), die Spalte
         "Abweichung" zeigt das dann als Betrag statt es zu verstecken. Eine unbekannte Fälligkeit ist
         NICHT automatisch strittig, und eine bekannte Fälligkeit ist KEINE Mahnfreigabe - eine aktive
         Sperre (Spalte "Hinweise") blockiert unabhängig davon; Mahnfälle stammen aus zuvor bereits
         geplanten Fällen (Tabelle weiter unten), diese Übersicht plant selbst keine neuen.</p>
      <div class="tabelle-scroll">
      <table>
        <tr><th>Objekt</th><th>Vertrag</th><th>Einheit</th><th>Nutzungsstatus</th><th>Mieter</th><th>Konto</th>
            <th>Kontostand (offen/Guthaben)</th><th>Fällig (Kontoberechnung)</th><th>Fällig (Positionen)</th>
            <th>Rest gesamt (Positionen)</th><th>Abweichung</th><th>Hinweise</th><th></th></tr>
        {mietkonten_html or '<tr><td colspan=13 class="muted">Keine Verträge.</td></tr>'}
      </table>
      </div>
    </details>"""

    positionen_html = "".join(_rueckstaende_position_zeile_html(p) for p in uebersicht.offene_positionen)
    positionen_tabelle = f"""
    <details class="card" id="offene-positionen">
      <summary>Offene Einzelpositionen im Detail — {titel_zusatz}</summary>
      <p class="muted">Jede Zeile ist ein einzelner offener Posten (nicht der Kontosaldo) - eine Zahlung
         wird zuerst der ältesten offenen Position zugeordnet, gezeigt wird nur der danach verbleibende
         Rest. OP-Nr. und Beleg identifizieren die zugrunde liegende Buchung eindeutig, auch wenn mehrere
         Positionen ähnlich aussehen.</p>
      <div class="tabelle-scroll">
      <table>
        <tr><th>Objekt</th><th>Vertrag</th><th>Mieter</th><th>OP-Nr.</th><th>Beleg</th><th>Art</th>
            <th>Zeitraum</th><th>Belegdatum</th><th>Fälligkeit</th><th>Rest</th><th>Status</th><th></th></tr>
        {positionen_html or '<tr><td colspan=12 class="muted">Keine offenen Positionen.</td></tr>'}
      </table>
      </div>
    </details>"""

    mahnfaelle_html = "".join(_rueckstaende_mahnfall_zeile_html(m) for m in uebersicht.mahnfaelle)
    mahnfaelle_tabelle = f"""
    <details class="card">
      <summary>Mahnfälle im Detail — {titel_zusatz}</summary>
      <p class="muted">Alle bereits geplanten Mahnfälle je Forderung, nicht nur der zuletzt angelegte - so
         bleiben auch ältere Stufen/Forderungen nachvollziehbar. Der Fallbetrag ist der ursprünglich
         festgehaltene Betrag zum Planungszeitpunkt und fließt in KEINE Summe oben ein. Diese Übersicht
         plant selbst keine neuen Mahnfälle.</p>
      <div class="tabelle-scroll">
      <table>
        <tr><th>Objekt</th><th>Vertrag</th><th>OP-Nr.</th><th>Stufe</th><th>Status</th><th>Fallbetrag</th><th>Geplant am</th></tr>
        {mahnfaelle_html or '<tr><td colspan=7 class="muted">Keine Mahnfälle.</td></tr>'}
      </table>
      </div>
    </details>"""

    bestand_html = "".join(
        f"<tr><td>{h(e.objekt_bezeichnung)}</td><td>{h(e.einheit_id)}</td><td>{h(e.einheit_bezeichnung)}</td>"
        f"<td>{h(nutzungsstatus_label(e.nutzungsstatus))} <span class='muted'>({h(e.nutzungsstatus)})</span></td></tr>"
        for e in uebersicht.einheiten_ohne_konto
    )
    bestand_tabelle = f"""
    <details class="card">
      <summary>Leerstände &amp; sonstige Einheiten ohne Mietkonto — {titel_zusatz}</summary>
      <p class="muted">Nutzungsstatus wird eingespielt/gepflegt, unabhängig davon, ob eine Mietforderung
         besteht (z. B. Leerstand, Kurzzeitvermietung, Selfstorage, Eigennutzung) - das ist BESTAND, kein
         erfundener Nullsaldo/Rückstand.</p>
      <div class="tabelle-scroll">
      <table>
        <tr><th>Objekt</th><th>Einheit</th><th>Bezeichnung</th><th>Nutzungsstatus</th></tr>
        {bestand_html or '<tr><td colspan=4 class="muted">Keine Einheiten ohne Mietkonto.</td></tr>'}
      </table>
      </div>
    </details>"""

    return _layout(
        request, session, "Übersicht",
        # Auftrag HV-20260914-UI-LESBAR: die vier Kennzahlen stehen VOR
        # "Das ist zu erledigen" (Reihenfolge aus dem Auftrag), unmittelbar
        # nach dem kompakten Objektfilter - reine Reihenfolgeänderung,
        # alle Werte/Berechnungen bleiben unverändert.
        auswahl_form + kpi_html + abgleich_html + erledigen_html + kompakt_tabelle
        + mietkonten_tabelle + positionen_tabelle + mahnfaelle_tabelle + bestand_tabelle,
    )


@router.get("/abgleichstatus", response_class=HTMLResponse)
def abgleichstatus(request: Request, objekt_id: str | None = None, session=Depends(_current_session)) -> HTMLResponse:
    """Zahlungs- und Abgleichstand im Detail - gleicher Login, gleicher
    Objektfilter/Scope wie die Übersicht, bei jedem Aufruf live gelesen,
    REIN LESEND (kein Formular in diesem Ausbauschritt)."""

    heute = heute_wien()
    try:
        uebersicht = berechne_rueckstandsuebersicht(
            ctx=_ctx(session), objekt_id=objekt_id or None, stammdaten_repository=deps._stammdaten_repo,
            op_service=deps._op_service, mahn_fall_repository=deps._mahn_fall_repo, heute=heute,
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Zahlungs- und Abgleichstand", str(exc), "/backoffice/abgleichstatus")
    status = berechne_abgleichstatus(
        uebersicht=uebersicht, stammdaten_repository=deps._stammdaten_repo, op_service=deps._op_service,
        bank_repository=deps._bank_repo, bank_service=deps._bank_service,
        nachweis_repository=deps._abgleich_nachweis_repo, heute=heute,
    )

    von_objekt_param = f"?{urlencode({'von_objekt': status.objekt_filter})}" if status.objekt_filter else ""
    zurueck_href = f"/backoffice/?{urlencode({'objekt_id': status.objekt_filter})}" if status.objekt_filter else "/backoffice/"
    zeilen_html = "".join(_abgleich_vertrag_zeile_html(v, h(von_objekt_param)) for v in status.vertraege) or (
        '<tr><td colspan=6 class="muted">Keine Verträge im Filter.</td></tr>'
    )
    objekte_html = "".join(_abgleich_objekt_html(o) for o in status.objekte) or (
        '<div class="card muted">Keine Objekte im Filter.</div>'
    )
    inhalt = f"""
    {_rueckstaende_objekt_filter_form(uebersicht, action="/backoffice/abgleichstatus")}
    <div class="card">
      <h1>Zahlungs- und Abgleichstand</h1>
      <p class="status-zeile">
        <span>Offene Beträge: <strong>{eur(status.summe_offen_cent)}</strong></span>
        <span>Guthaben der Mieter: <strong>{eur(status.summe_guthaben_cent)}</strong></span>
        <span>Eingereicht, Bankeingang noch offen: <strong>{eur(status.einzuege_offen_cent)}</strong>
          ({status.einzuege_offen_anzahl})</span>
      </p>
      <p>{_abgleich_nachweis_satz(status)}</p>
      <p class="muted">Live berechnet am {_datum(status.stichtag)} aus den gebuchten Vorschreibungen und Zahlungen.</p>
      <details>
        <summary>Was bedeutet das?</summary>
        <ul>
          <li><strong>Offene Beträge</strong> und <strong>Guthaben</strong> stammen aus derselben Berechnung wie die
              Übersicht und werden nie gegeneinander verrechnet.</li>
          <li><strong>Abgleichnachweis</strong>: ausdrücklich erfasste Prüfung der Mieteingänge je Objekt und
              Bankkonto. Gilt nur für den genannten Zeitraum - spätere Eingänge sind noch nicht geprüft. Das ist
              keine Bestätigung der Bankvollständigkeit und keine Mahnfreigabe.</li>
          <li><strong>Eingereicht, Bankeingang noch offen</strong>: ein eingereichter Einzug ist noch kein
              Zahlungseingang. Er verringert keinen offenen Betrag und gilt nicht als bezahlt.</li>
          <li><strong>Letzte erfasste Zahlung</strong>: Buchungsdatum der jüngsten gebuchten Zahlung - keine Aussage,
              dass alle Zahlungen erfasst sind.</li>
          <li><strong>Keine offenen Posten</strong> heißt nur: alle bisher gebuchten Vorschreibungen sind gedeckt.
              Ein „bezahlt bis“ wird daraus nicht abgeleitet.</li>
        </ul>
      </details>
    </div>
    <h2>Abgleichnachweise je Objekt</h2>
    <div class="bereich-karten">{objekte_html}</div>
    <div class="card">
      <h2>Zahlungsstand je Mietvertrag</h2>
      <div class="tabelle-kompakt">
      <table>
        <thead><tr><th>Mieter / Einheit</th><th>Offener Betrag</th><th>Guthaben</th><th>Offene Zeiträume</th>
            <th>Letzte erfasste Zahlung</th><th>Einzüge</th></tr></thead>
        <tbody>{zeilen_html}</tbody>
      </table>
      </div>
    </div>
    <p><a href="{h(zurueck_href)}">&larr; zur Übersicht</a></p>"""
    return _layout(request, session, "Zahlungs- und Abgleichstand", inhalt)


# -- Bereichs-Startseiten (Auftrag HV-20260914-UI-EINFACH) -------------------
# Bündeln die bisher als 15 technische Einzellinks in der Hauptnavigation
# aufgeführten Arbeitsabläufe zu genau drei fachlichen Sammelseiten - JEDE
# Route bleibt exakt wie zuvor erreichbar, nur der Weg dorthin führt jetzt
# über eine dieser drei Seiten statt über die Navigation direkt. Reine
# Verlinkung bestehender Seiten, keine eigene Datenabfrage/Berechnung.


def _bereich_karten_html(karten: list[tuple[str, str, str]]) -> str:
    """`karten`: Liste von (href, Titel, Beschreibung)."""

    return '<div class="bereich-karten">' + "".join(
        f'<div class="card"><h3><a href="{h(href)}">{h(titel)}</a></h3><p class="muted">{h(beschreibung)}</p></div>'
        for href, titel, beschreibung in karten
    ) + "</div>"


@router.get("/zahlungen", response_class=HTMLResponse)
def zahlungen_hub(request: Request, session=Depends(_current_session)) -> HTMLResponse:
    inhalt = f"""
    <div class="card"><h1>Zahlungen &amp; Mahnungen</h1>
      <p class="muted">Bankdatei einlesen, offene Zahlungen zuordnen und den Mahnstand samt Versandnachweisen
         einsehen. Die Mahnvorschau selbst gehört zu einem konkreten Mietvertrag und wird aus dessen Akte
         geöffnet (Mieter &amp; Objekte).</p>
    </div>
    {_bereich_karten_html([
        ("/backoffice/salden", "Salden", "Alle Mietkonten mit offenem Saldo, fälligem Betrag, Guthaben und Bankstand."),
        ("/backoffice/bank", "Bankdatei einlesen", "CSV-/CAMT.053-Import mit Vorschau vor der Übernahme."),
        ("/backoffice/bank/unzugeordnet", "Offene Zahlungen zuordnen", "Bankbuchungen, die noch keinem Mietkonto zugeordnet sind."),
        ("/backoffice/bank/vollstaendigkeit", "Bankvollständigkeit", "Bestätigt je Bankkonto, dass ein Zeitraum lückenlos eingelesen ist."),
        ("/backoffice/mailversand", "Mailversand und Nachweise", "Mahn-Mailversand, Status je Fall, Zustellnachweise."),
    ])}
    <p><a href="/backoffice/">&larr; zur Übersicht</a></p>"""
    return _layout(request, session, "Zahlungen & Mahnungen", inhalt)


@router.get("/abrechnungen", response_class=HTMLResponse)
def abrechnungen_hub(request: Request, session=Depends(_current_session)) -> HTMLResponse:
    inhalt = f"""
    <div class="card"><h1>Abrechnungen</h1>
      <p class="muted">Variable Kurzzeit-/Selfstorage-Abrechnungen und die daraus abgeleitete
         Netto-Monatsübersicht sind hier zusammen erreichbar.</p>
    </div>
    {_bereich_karten_html([
        (
            "/backoffice/eigentuemerzahlungen", "Betriebskosten zahlen",
            "Monatliche Eigentümervorschreibungen, Übergaben und Zahlungsdateien für George.",
        ),
        (
            "/backoffice/zinsliste", "Zinsliste",
            "Monatliche vertragliche Beträge je Einheit - inkl. Leerstand, Kurzzeitvermietung, Selfstorage.",
        ),
        (
            "/backoffice/variable-abrechnung", "Kurzzeit-/Selfstorage-Abrechnung",
            "Variable Monatsabrechnung erfassen/prüfen, CSV-Import, Versionen.",
        ),
        (
            "/backoffice/dashboard/monatsuebersicht", "Netto-Monatsübersicht",
            "Nettomieterlös je Monat aus bestätigten Abrechnungen, inkl. Datenlücken.",
        ),
    ])}
    <div class="card">
      <h3>Betriebskostenabrechnung (BK)</h3>
      <p class="muted">Noch nicht bedienbar - diese Abrechnungsart ist im Backoffice noch nicht freigeschaltet.</p>
    </div>
    <p><a href="/backoffice/">&larr; zur Übersicht</a></p>"""
    return _layout(request, session, "Abrechnungen", inhalt)


@router.get("/einstellungen", response_class=HTMLResponse)
def einstellungen_hub(request: Request, session=Depends(_current_session)) -> HTMLResponse:
    inhalt = f"""
    <div class="card"><h1>Einstellungen</h1>
      <p class="muted">Technische Regeln und Konfiguration, die im Alltag selten gebraucht werden - vom
         einmaligen Dateneinstieg bis zu Index-/Mahnregeln.</p>
    </div>
    {_bereich_karten_html([
        ("/backoffice/eroeffnung", "Eröffnungsimport", "Eröffnungssalden-CSV: Vorschau, dann bestätigter atomarer Import."),
        ("/backoffice/mahnwesen/policy", "Mahnstufen-Konfiguration", "Freigegebene Mahnpolicy, Kanalregel je Stufe."),
        ("/backoffice/basiszinssatz", "OeNB-Basiszinssatz", "Erfasste Basiszinssätze für die Verzugszinsenberechnung."),
        ("/backoffice/indexautomatik/laeufe", "Indexautomatik: Monatsläufe", "Protokoll der monatlichen Indexlauf-Durchgänge."),
        (
            "/backoffice/indexautomatik/monatsbericht", "Index-Monatsbericht (Owner)",
            "Ein Bericht je Monat: Betrag bisher, Vorschlag, Termin, verständlicher Grund je Vertrag.",
        ),
        ("/backoffice/indexautomatik/outbox", "Indexautomatik: Outbox", "Erhöhungsschreiben vor Versand/Zugangsbestätigung."),
        (
            "/backoffice/indexautomatik/soll-umsetzung", "Indexautomatik: Soll-Umsetzung",
            "Freigegebene Erhöhungen in neue Mietkomponenten umsetzen.",
        ),
        ("/backoffice/indexautomatik/vpi", "VPI-Werte", "Veröffentlichte Monatswerte für die Indexberechnung erfassen."),
        (
            "/backoffice/indexautomatik/vertragsende", "Vertragsende-Erinnerungen",
            "Verträge, deren befristete Laufzeit demnächst endet.",
        ),
    ])}
    <p><a href="/backoffice/">&larr; zur Übersicht</a></p>"""
    return _layout(request, session, "Einstellungen", inhalt)
