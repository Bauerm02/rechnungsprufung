"""Protected owner BK file view and source amendments; no bank execution."""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from html import escape as h
import json
import hashlib
import io
from pathlib import Path
from sqlalchemy.engine import make_url
from pypdf import PdfReader

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response, RedirectResponse
from sqlalchemy import select

from mietinkasso.backoffice import dependencies as deps
from mietinkasso.backoffice.auth import _current_session, _layout, _verify_csrf, _fehlerseite
from mietinkasso.eigentuemerzahlungen.models import Datei, Lauf, Reservierung, Vorschrift
from mietinkasso.eigentuemerzahlungen.service import Profil, plan, generate, profiles, save_profile, digest
from mietinkasso.indexautomatik.zeit import WIEN

router = APIRouter()


def euros(c):
    return f"{Decimal(c)/100:,.2f}".replace(",","_").replace(".",",").replace("_",".")+" €"


@router.get("/eigentuemerzahlungen")
def overview(request: Request, monat: str = "", session=Depends(_current_session)):
    monat = monat or datetime.now(WIEN).strftime("%Y-%m")
    try:
        rows = plan(deps._session_factory,monat)
    except ValueError as exc:
        return _fehlerseite(session,"Betriebskosten zahlen",str(exc))
    for row in rows:
        if row['status']=='BEREIT' and date.fromisoformat(row['ausfuehrung']) <= datetime.now(WIEN).date():
            row.update(status='KLAEREN',hinweis='Ausführungstermin verstrichen oder heute – gesonderten Zahlungslauf prüfen')
    with deps._session_factory() as s:
        files = list(s.scalars(select(Datei).where(Datei.monat==monat).order_by(Datei.id)))
        run = s.get(Lauf,monat)
        last = datetime.fromisoformat(json.loads(run.bericht)["geprueft_am"]).astimezone(WIEN).strftime('%d.%m.%Y %H:%M') if run else "Noch kein automatischer Lauf für diesen Monat"
    table = "".join(f'<tr><td>{h(r["objekt"])}</td><td>{h(r["einheit"])}</td><td>{euros(r["soll_cent"])}</td>'
        f'<td>{euros(r["bezahlt_cent"])}</td><td>{euros(r["betrag_cent"])}</td><td>{h(r["status"])}</td>'
        f'<td>{h(r["hinweis"])}</td><td><a href="/backoffice/eigentuemerzahlungen/profil/{h(r["kennung"])}">Vorschreibung / Übergabe</a></td></tr>' for r in rows)
    filelist = ""
    for f in files:
        with deps._session_factory() as s:
            items = [json.loads(r.snapshot) for r in s.scalars(select(Reservierung).where(Reservierung.datei_id==f.id))]
        # Original imported files are evidence, not a second upload offer.
        link = f'<a class="btn" href="/backoffice/eigentuemerzahlungen/datei/{h(f.id)}">XML herunterladen</a>' if f.status=="ERSTELLT" else "Bereits in George – dort prüfen und freigeben"
        stale = any(r["datei_id"]==f.id and r["datei_pruefen"] for r in rows)
        settled = any(r["datei_id"]==f.id and r["status"]=="BEZAHLT" for r in rows)
        if stale:
            link = '<strong>Prüfbedarf nach Änderung. Datei nicht erneut einspielen.</strong>'
        elif settled:
            link = "Bankausführung ganz oder teilweise nachgewiesen – kein erneuter Download"
        filelist += f'<div class="card"><h3>{h(items[0]["gesellschaft"]) if items else h(f.bank_konto_id)} · {euros(sum(r["betrag_cent"] for r in items))}</h3><p>{len(items)} Positionen · Ausführung {h(items[0]["ausfuehrung"]) if items else "–"} · {h(f.status)}</p><p>{link}</p><small>{h(f.nachweis)}</small></div>'
    body = f'''<div class="card"><h1>Betriebskosten zahlen</h1>
    <p>Die Eigentümervorschreibungen werden monatlich ohne KI fortgeschrieben. Die Datei bezahlt noch nichts: Bankstand und Deckung prüfen, anschließend in George importieren und freigeben.</p>
    <p><strong>Keine automatische Bankabholung:</strong> berücksichtigt werden nur vorhandene, eindeutig zuordenbare Bankimporte. Andere Zahlungen oder Entwürfe können noch fehlen.</p>
    <form method="get"><label>Monat <input type="month" name="monat" value="{h(monat)}"></label> <button>Anzeigen</button></form>
    <p class="muted">Letzte automatische Prüfung: {h(last)}. Ausführung am Vorschreibungstag, bei TARGET-Schließtagen am nächsten Bankarbeitstag.</p></div>
    {filelist or '<div class="card">Noch keine Datei vorhanden. Gesperrte Positionen stehen unten.</div>'}
    <div class="card"><h2>Vorschreibungen und Ausnahmen</h2><div class="tabelle-scroll"><table><thead><tr><th>Objekt</th><th>Einheit</th><th>Vorschreibung</th><th>Bankzahlung belegt</th><th>Rechnerischer Rest</th><th>Status</th><th>Grund</th><th>Ändern</th></tr></thead><tbody>{table}</tbody></table></div></div>'''
    return _layout(request,session,"Betriebskosten zahlen",body)


@router.get("/eigentuemerzahlungen/datei/{file_id}")
def download(file_id: str, session=Depends(_current_session)):
    with deps._session_factory() as s:
        f = s.get(Datei,file_id)
        if not f or f.status!="ERSTELLT":
            raise HTTPException(404,"Keine neue Zahlungsdatei")
        rows = plan(deps._session_factory,f.monat)
        if any(r["datei_id"]==file_id and r["datei_pruefen"] for r in rows):
            raise HTTPException(409,"Zahlungsstand/Vorschreibung geändert. Bestehende Datei prüfen.")
        if any(r["datei_id"]==file_id and r["status"]=="BEZAHLT" for r in rows):
            raise HTTPException(409,"Bankausführung nachgewiesen – Datei nicht erneut einspielen.")
        from datetime import date
        items = [json.loads(r.snapshot) for r in s.scalars(select(Reservierung).where(Reservierung.datei_id==file_id))]
        if not items or any(date.fromisoformat(r["ausfuehrung"]) < datetime.now(WIEN).date() for r in items):
            raise HTTPException(409,"Ausführungstermin verstrichen. Gesonderten Zahlungslauf prüfen.")
        if digest(f.xml)!=f.sha256:
            raise HTTPException(409,"Dateiprüfsumme stimmt nicht")
        return Response(f.xml,media_type="application/xml",headers={"Content-Disposition":f'attachment; filename="{file_id}.xml"',"Cache-Control":"no-store"})


@router.get("/eigentuemerzahlungen/profil/{key}")
def edit(request: Request,key: str, session=Depends(_current_session)):
    with deps._session_factory() as s:
        group = profiles(s).get(key)
        if not group:
            raise HTTPException(404,"Vorschreibung nicht gefunden")
        row = group[-1]
        p = Profil.model_validate_json(row.payload)
    def field(name,label,value,typ="text"):
        return f'<p><label>{label}<br><input type="{typ}" name="{name}" value="{h(str(value))}" required></label></p>'
    fields = field("betrag","Gesamtbetrag Eigentümervorschreibung (EUR)",f"{p.betrag_cent/100:.2f}")
    fields += field("gueltig_ab","Gültig ab",p.gueltig_ab,"date")+field("gueltig_bis","Gültig bis",p.gueltig_bis,"date")
    fields += f'<p>Aktueller Beleg: {h(p.quelle)}</p><p><label>Neue Vorschreibung als PDF (bei geändertem Betrag oder Gültigkeitszeitraum erforderlich)<br><input type="file" name="beleg" accept="application/pdf"></label></p>'
    options = "".join(f'<option value="{v}" {"selected" if p.status==v else ""}>{label}</option>' for v,label in [("FREIGEGEBEN","Laufend / bestätigt"),("KLAEREN","Zurückstellen / klären"),("AUSGESCHIEDEN","Kostenpflicht beendet")])
    body = f'''<div class="card"><h1>Vorschreibung · {h(p.einheit)}</h1><p>Aktuelle Fassung {row.version}. Neue Beträge gelten nur ab dem angegebenen Datum. Bereits erstellte Zahlungsdateien bleiben unverändert und werden bei Abweichungen zur Prüfung gesperrt.</p>
    <p>{h(p.empfaenger)} · Referenz {h(p.referenz)}. Kontowechsel bitte gesondert belegen.</p>
    <form method="post" enctype="multipart/form-data"><input type="hidden" name="csrf_token" value="{h(session.csrf_token)}"><input type="hidden" name="version" value="{row.version}">{fields}
    <p><label>Status <select name="status">{options}</select></label></p>
    <p><label>Letzter Tag unserer Kostenpflicht (optional)<br><input type="date" name="kostenende" value="{p.kostenende or ''}"></label></p>
    <p><label>Begründung / Nachweis der Übergabe<br><textarea name="hinweis" rows="3" required>{h(p.hinweis)}</textarea></label></p>
    <p><label><input type="checkbox" name="bestaetigt" value="ja" required> Betrag, Gültigkeit und Kostenübergang anhand des Belegs geprüft.</label></p><button>Geprüfte Änderung speichern</button></form></div>'''
    return _layout(request,session,"Eigentümervorschreibung",body)


@router.post("/eigentuemerzahlungen/profil/{key}")
async def update(request: Request,key: str, session=Depends(_current_session)):
    form = await request.form()
    _verify_csrf(session,str(form.get("csrf_token","")))
    try:
        if form.get("bestaetigt") != "ja":
            raise ValueError("Belegprüfung bestätigen")
        with deps._session_factory() as s:
            group = profiles(s).get(key)
            if not group:
                raise ValueError("Vorschreibung nicht gefunden")
            data = json.loads(group[-1].payload)
        old = dict(data)
        for field in ("gueltig_ab","gueltig_bis","status","hinweis"):
            data[field] = str(form.get(field,""))
        data["kostenende"] = form.get("kostenende") or None
        cents = Decimal(str(form.get("betrag","")).replace(",","."))*100
        if not cents.is_finite() or cents != cents.to_integral_value():
            raise ValueError("Betrag auf Cent genau eingeben")
        data["betrag_cent"] = int(cents)
        upload = form.get("beleg")
        if upload is not None and getattr(upload,"filename",""):
            content = await upload.read(15*1024*1024+1)
            if len(content)>15*1024*1024 or not content.startswith(b"%PDF-"):
                raise ValueError("Bitte ein PDF bis 15 MB hochladen")
            try:
                if not PdfReader(io.BytesIO(content)).pages:
                    raise ValueError("Leeres PDF")
            except Exception as exc:
                raise ValueError("PDF nicht lesbar") from exc
            sha = hashlib.sha256(content).hexdigest()
            db_path = make_url(deps._settings.database_url).database
            if not db_path or db_path==":memory:":
                raise ValueError("Geschützte Belegablage nicht verfügbar")
            folder = Path(db_path).resolve().parent/"eigentuemer-belege"
            folder.mkdir(mode=0o700,exist_ok=True)
            target = folder/(sha+".pdf")
            if not target.exists():
                with target.open("xb") as stream:
                    stream.write(content)
                target.chmod(0o600)
            data.update(quelle=str(target),quelle_sha256=sha)
        elif any(data[k]!=old[k] for k in ("betrag_cent","gueltig_ab","gueltig_bis")):
            raise ValueError("Für die neue Vorschreibung bitte den PDF-Beleg hochladen")
        save_profile(deps._session_factory,data,actor=session.user_id,expected_version=int(form.get("version","0")))
    except (ValueError, InvalidOperation) as exc:
        return _fehlerseite(session,"Vorschreibung prüfen",str(exc))
    return RedirectResponse("/backoffice/eigentuemerzahlungen",status_code=303)
