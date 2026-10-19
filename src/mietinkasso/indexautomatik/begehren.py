"""Versioned tenant notice, separate from the calculation and legal approval.

Primary sources and release limits: docs/hausverwaltung/ERHOEHUNGSBEGEHREN.md.
No AI, network calls, new accounting entries or implied legal approval.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, timedelta
from decimal import Decimal
from hashlib import sha256
from html import escape

from mietinkasso.indexautomatik.schreiben import (
    SchreibenKontext, SchreibenKomponente, SchreibenJahresschritt,
    _abschluss, _gesamtbetraege, _eur,
)
from mietinkasso.indexautomatik.zeit import naechster_zinstermin_ab

VERSION = "JLB-INDEX-20260930-1"
FULL = "OESTERREICH_MRG_VOLL"


def frist_tage(profil) -> int:
    configured = profil.frist_tage_zugang_bis_wirksamkeit
    if configured is not None and (configured < 0 or not (profil.frist_quellenbeleg or '').strip()):
        raise ValueError("Zugangsfrist ohne gültigen Quellenbeleg.")
    if profil.rechtsordnung != FULL and configured is None:
        raise ValueError("Vertragliche Zugangsfrist ist noch nicht belegt.")
    return max(14 if profil.rechtsordnung == FULL else 0, configured or 0)


def kontext_snapshot(k: SchreibenKontext) -> dict:
    data = asdict(k)
    for key in ('erstellt_am', 'massgeblicher_termin'):
        data[key] = data[key].isoformat()
    return data


def kontext_aus_snapshot(data: dict) -> SchreibenKontext:
    data = dict(data)
    for key in ('erstellt_am', 'massgeblicher_termin'):
        data[key] = date.fromisoformat(data[key])
    for key in ('geaenderte_komponenten', 'unveraenderte_komponenten'):
        data[key] = [SchreibenKomponente(**row) for row in data[key]]
    data['jahresschritte'] = [SchreibenJahresschritt(**row) for row in data['jahresschritte']]
    return SchreibenKontext(**data)


def _percent(value: str) -> str:
    # Persisted annual steps are fractions (0.03), not percent points.
    raw = str(value).strip()
    if raw.endswith('%'):
        return raw.replace('.', ',')
    return f"{Decimal(raw) * 100:.4f}".rstrip('0').rstrip('.').replace('.', ',') + ' %'


def vorbereiten(k: SchreibenKontext, *, profil, faelligkeit_tag: int,
                heute: date, berechnung: dict | None = None) -> tuple[str, dict]:
    """Return tenant text and immutable notice metadata. Incomplete => internal block."""
    errors = []
    if not (k.klausel_referenz or '').strip():
        errors.append('Vertragliche Wertsicherungsklausel/Fundstelle fehlt.')
    if not k.vertrag_beleg_referenz.strip():
        errors.append('Vertragsbeleg fehlt.')
    if not k.gesellschaft_name.strip() or not k.mieter_name.strip() or not (k.mieter_adresse or '').strip():
        errors.append('Vermieter oder Empfängeranschrift unvollständig.')
    old, new = _gesamtbetraege(k)
    if not k.geaenderte_komponenten or k.erhoehung_cent <= 0 or new - old != k.erhoehung_cent:
        errors.append('Betragsvergleich und Erhöhungsbetrag sind nicht konsistent.')
    if any(p.neuer_betrag_cent != p.alter_betrag_cent for p in k.unveraenderte_komponenten):
        errors.append('Als unverändert ausgewiesene Position wurde verändert.')
    try:
        days = frist_tage(profil)
    except ValueError as exc:
        errors.append(str(exc))
        days = None
    wohnung = profil.ist_wohnungsnutzung is True
    if profil.ist_wohnungsnutzung is None:
        errors.append('Wohnungs-/Gewerbenutzung ist ungeklärt.')
    if wohnung and (not k.jahresschritte or k.vertraglich_zulaessiger_betrag_cent is None
                    or k.gesetzliche_basis_cent is None or k.gesetzliche_grenze_cent is None):
        errors.append('Wohnung: gesetzlicher und vertraglicher Rechenweg fehlen.')
    if not wohnung and not berechnung:
        errors.append('Vertragliche Indexberechnung mit Reihe, Basis- und Vergleichsmonat fehlt.')
    if wohnung and k.gesetzliche_grenze_cent is not None and k.vertraglich_zulaessiger_betrag_cent is not None:
        if sum(p.neuer_betrag_cent for p in k.geaenderte_komponenten) > min(k.gesetzliche_grenze_cent, k.vertraglich_zulaessiger_betrag_cent):
            errors.append('Neuer Betrag überschreitet die geprüfte Obergrenze.')
    if berechnung and berechnung['basis_cent'] != sum(p.alter_betrag_cent for p in k.geaenderte_komponenten):
        errors.append('Berechnungsbasis stimmt nicht mit den bisherigen betroffenen Positionen überein.')
    meta = {'version': VERSION, 'erstellt_am': heute.isoformat(), 'fehler': errors,
            'kontext': kontext_snapshot(k), 'berechnung': berechnung}
    if errors:
        return 'ENTWURF – nicht versandfähig.\n' + '\n'.join(errors) + '\n\n' + '\n'.join(_abschluss(k)), meta
    # Earliest proposed due date assumes receipt no sooner than tomorrow.
    # It is explicitly conditional, never evidence of actual receipt.
    earliest_receipt = max(heute + timedelta(days=1), k.massgeblicher_termin + timedelta(days=1))
    start = naechster_zinstermin_ab(earliest_receipt + timedelta(days=days), faelligkeit_tag=faelligkeit_tag)
    deadline = start - timedelta(days=days)
    meta.update(zahlungstermin=start.isoformat(), zugang_spaetestens=deadline.isoformat(), frist_tage=days,
                faelligkeit_tag=faelligkeit_tag)
    d = lambda x: x.strftime('%d.%m.%Y')
    lines = ['JLB Projects GmbH · Hausverwaltung',
             'Marc-Aurel-Straße 4/16 · 1010 Wien',
             'hausverwaltung@jlb-immo.at · +43 1 435 10 11', '',
             k.mieter_name, k.mieter_adresse, '', f'Wien, {d(heute)}', '',
             f'Erhöhungsbegehren – {k.objekt_bezeichnung}, {k.einheit_bezeichnung}',
             f'Mietobjekt: {k.objekt_adresse or k.objekt_bezeichnung}', '',
             'Sehr geehrte Damen und Herren,', '']
    if k.gesellschaft_name.casefold() == 'jlb projects gmbh':
        lines.append('als Ihre Vermieterin machen wir die vereinbarte Wertsicherung geltend.')
    else:
        lines.append(f'im Namen und Auftrag Ihrer Vermieterin / Ihres Vermieters {k.gesellschaft_name} '
                     'machen wir die vereinbarte Wertsicherung geltend.')
    lines += [f'Grundlage ist die Wertsicherungsvereinbarung in Ihrem Mietvertrag ({k.klausel_referenz}).', '']
    if wohnung:
        lines += ['Die vertragliche Berechnung wird durch die Grenzen des § 1 Mieten-Wertsicherungsgesetz '
                  '(MieWeG) begrenzt; bei vor 2026 geschlossenen Verträgen gilt zusätzlich § 4 Abs. 2 MieWeG.',
                  'Berechnung der gesetzlichen Höchstgrenze (VPI 2020, Jahresdurchschnitte):']
        for s in k.jahresschritte:
            lines.append(f'{s.jahr}: {s.vpi_vorjahr} → {s.vpi_jahr}; Veränderung {_percent(s.rohe_veraenderung)}, '
                         f'gedämpft {_percent(s.gedaempfte_veraenderung)}, berücksichtigt {_percent(s.angewandte_veraenderung)}.')
        lines.append(f'Vertraglich zulässiger Betrag der betroffenen Positionen (brutto): '
                     f'{_eur(k.vertraglich_zulaessiger_betrag_cent)}. Maßgeblich ist die niedrigere zulässige Grenze.')
        lines.append(f'Gesetzliche Berechnungsbasis (brutto): {_eur(k.gesetzliche_basis_cent)}; '
                     f'gesetzliche Höchstgrenze (brutto): {_eur(k.gesetzliche_grenze_cent)}. '
                     'Allfällige zeitanteilige Berücksichtigungen sind in den angewandten Jahresschritten enthalten.')
    else:
        lines += [f"Indexreihe: {berechnung['reihe']}; Basis {berechnung['basis_monat']}: {berechnung['alter_wert']}; "
                  f"Vergleich {berechnung['vergleich_monat']}: {berechnung['neuer_wert']}.",
                  f"Indexveränderung: ({berechnung['neuer_wert']} / {berechnung['alter_wert']} − 1) × 100; "
                  f"vertraglich angewandt: {berechnung['prozent']} %.",
                  f"Indexierbarer bisheriger Betrag (brutto): {_eur(berechnung['basis_cent'])}. "
                  'Bereits berücksichtigte Erhöhungen sind in dieser Berechnungsbasis enthalten.']
    lines += ['', f'Die für diese Berechnung maßgebliche Indexänderung ist seit {d(k.massgeblicher_termin)} wirksam.']
    if k.rechtsordnung == FULL:
        lines.append('Das Erhöhungsbegehren erfolgt nach § 16 Abs. 9 MRG. Die Bekanntgabe muss spätestens '
                     '14 Tage vor dem maßgeblichen Zinstermin erfolgen; längere vereinbarte Fristen bleiben berücksichtigt.')
    lines += [f'Den unten ausgewiesenen neuen Gesamtbetrag begehren wir erstmals zum {d(start)}, '
              f'sofern Ihnen dieses Schreiben spätestens am {d(deadline)} zugeht.',
              f'Bei späterem Zugang gilt der nächste monatliche Zinstermin nach Ablauf der maßgeblichen Frist '
              f'von {days} Tagen. Für frühere Zeiträume wird mit diesem Schreiben keine Nachforderung erhoben.', '',
              *_abschluss(k), '', 'Für Rückfragen wenden Sie sich bitte an hausverwaltung@jlb-immo.at.',
              'JLB Projects GmbH · FN 631126 b · Handelsgericht Wien · UID ATU81269707', 'Werte, die bleiben.']
    text = '\n'.join(lines)
    for p in k.geaenderte_komponenten + k.unveraenderte_komponenten:
        text = text.replace(f' ({p.art}):', ':')
    meta['text_sha256'] = sha256(text.encode()).hexdigest()
    return text, meta


def versandfehler(text: str, meta: dict | None, *, heute: date) -> list[str]:
    if not meta or meta.get('version') != VERSION:
        return ['Vorlage veraltet: Schreiben aus geprüften Grundlagen neu erstellen.']
    if meta.get('fehler'):
        return list(meta['fehler'])
    try:
        if sha256(text.encode()).hexdigest() != meta['text_sha256']:
            return ['Schreibentext wurde seit der Vorlagenprüfung verändert.']
        if heute > date.fromisoformat(meta['zugang_spaetestens']):
            return ['Zugangsfrist des geplanten Termins ist abgelaufen: Schreiben neu erstellen.']
        if heute < date.fromisoformat(meta['erstellt_am']):
            return ['Schreibendatum liegt in der Zukunft.']
    except (KeyError, TypeError, ValueError):
        return ['Vorlagen-/Terminnachweis ist unvollständig.']
    return []


def druckansicht(text: str, *, status: str, logo_data_uri: str | None = None) -> str:
    # Only a locally configured approved logo is supplied by the authenticated route.
    logo = f'<img alt="JLB" src="{escape(logo_data_uri, quote=True)}">' if logo_data_uri else ''
    return '''<!doctype html><html lang="de"><meta charset="utf-8"><title>JLB · Erhöhungsbegehren</title>
<style>body{background:#F5F2EC;color:#1F2125;font:17px/1.5 "EB Garamond",Constantia,Georgia,serif;margin:0}
main{background:white;max-width:760px;margin:28px auto;padding:42px 54px}header{border-bottom:2px solid #C9A86A;padding-bottom:18px;display:flex;align-items:center;gap:24px}header img{width:110px;height:auto}h1{font:28px Forum,Cambria,serif;margin:0}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;margin-top:28px}.status{padding:12px;background:#F5F2EC;font:14px Arial,sans-serif}p{margin:0}@page{size:A4;margin:18mm}@media print{body,main{background:white}main{margin:0;padding:0;max-width:none}header{break-after:avoid}.status{border:1px solid #C9A86A}}
</style><main><header>''' + logo + '<h1>Erhöhungsbegehren</h1></header><p class="status">' + escape(status) + '</p><pre>' + escape(text) + '</pre></main></html>'
