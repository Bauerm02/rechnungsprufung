"""No AI, no bank transmission, no tenant ledger mutation.

Approved recurring source versions -> immutable monthly SEPA drafts.
One reservation per source identity/month is enforced in SQLite, including
externally imported files. Sources, partial payments and holds stay explicit.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from xml.etree import ElementTree as ET

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select, text

from mietinkasso.eigentuemerzahlungen.models import Datei, Lauf, Reservierung, Vorschrift
from mietinkasso.infrastructure.db.tables import BankKontoTable, BankTransaktionTable, ObjektTable, GesellschaftTable


NS = "urn:iso:std:iso:20022:tech:xsd:pain.001.001.03"
ET.register_namespace("", NS)


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def iban(value):
    value = re.sub(r"\s+", "", value).upper()
    if not re.fullmatch(r"[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}", value):
        raise ValueError("IBAN ungültig")
    lengths = {"AT": 20, "DE": 22}
    if value[:2] in lengths and len(value) != lengths[value[:2]]:
        raise ValueError("IBAN-Länge ungültig")
    digits = "".join(str(ord(c)-55) if c.isalpha() else c for c in value[4:]+value[:4])
    if int(digits) % 97 != 1:
        raise ValueError("IBAN-Prüfsumme ungültig")
    return value


class Profil(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kennung: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    objekt_id: str
    bank_konto_id: str
    einheit: str = Field(min_length=1, max_length=60)
    empfaenger: str = Field(min_length=1, max_length=70)
    empfaenger_iban: str
    empfaenger_bic: str = Field(pattern=r"^[A-Z0-9]{8}([A-Z0-9]{3})?$")
    absender_bic: str = Field(pattern=r"^[A-Z0-9]{8}([A-Z0-9]{3})?$")
    referenz: str = Field(pattern=r"^[A-Za-z0-9-]{1,35}$")
    betrag_cent: int = Field(strict=True, gt=0, le=100000000)
    gueltig_ab: date
    gueltig_bis: date
    zahlungstag: int = Field(default=5, ge=1, le=28)
    kostenende: date | None = None
    status: Literal["FREIGEGEBEN", "KLAEREN", "AUSGESCHIEDEN"]
    hinweis: str = Field(default="", max_length=1500)
    quelle: str = Field(min_length=3, max_length=1000)
    quelle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    start_monat: str = Field(pattern=r"^20\d{2}-(0[1-9]|1[0-2])$")

    _iban = field_validator("empfaenger_iban")(iban)

    @model_validator(mode="after")
    def dates(self):
        if self.gueltig_bis < self.gueltig_ab:
            raise ValueError("Gültigkeitsende liegt vor Beginn")
        if self.status != "FREIGEGEBEN" and not self.hinweis.strip():
            raise ValueError("Sperr-/Ausscheidungsgrund fehlt")
        if self.status == "AUSGESCHIEDEN" and not self.kostenende:
            raise ValueError("Letzten Tag der Kostenpflicht angeben")
        if re.search(r"20\d{2}-(0[1-9]|1[0-2])",self.referenz):
            raise ValueError("Referenz darf keinen Leistungsmonat vortäuschen")
        return self


def month_dates(monat):
    if not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", monat):
        raise ValueError("Monat als JJJJ-MM angeben")
    start = date.fromisoformat(monat+"-01")
    return start, date(start.year, start.month, calendar.monthrange(start.year, start.month)[1])


def execution_day(start, day):
    # TARGET closed days: weekends, Jan 1, Good Friday, Easter Monday,
    # May 1, Dec 25/26. Move forward, show actual requested bank date.
    year = start.year
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b+8)//25
    g = (b-f+1)//3
    h = (19*a+b-d-g+15)%30
    i, k = c//4, c%4
    l = (32+2*e+2*i-h-k)%7
    m = (a+11*h+22*l)//451
    easter = date(year, (h+l-7*m+114)//31, (h+l-7*m+114)%31+1)
    holidays = {date(year,1,1), date(year,5,1), date(year,12,25), date(year,12,26),
                easter-timedelta(days=2), easter+timedelta(days=1)}
    result = start.replace(day=day)
    while result.weekday() >= 5 or result in holidays:
        result += timedelta(days=1)
    return result


def _scope(s, p):
    obj = s.get(ObjektTable, p.objekt_id)
    bank = s.get(BankKontoTable, p.bank_konto_id)
    if not obj or obj.id == "107" or obj.ausgeschlossen or not bank or bank.gesellschaft_id != obj.gesellschaft_id:
        raise ValueError("Objekt/Konto fehlt, ist ausgeschlossen oder gehört zu einer anderen Gesellschaft")
    iban(bank.iban)
    owner = s.get(GesellschaftTable, obj.gesellschaft_id)
    return obj, bank, owner


def _locked(s):
    # Serialize monthly generation, profile updates and external evidence in
    # the same SQLite transaction. No file write can outlive a DB rollback.
    if s.bind.dialect.name != "sqlite":
        raise ValueError("Eigentümer-Zahlungslauf benötigt den freigegebenen SQLite-Betrieb")
    s.execute(text("BEGIN IMMEDIATE"))


def save_profile(factory, value, *, actor, expected_version):
    p = Profil.model_validate(value)
    with factory() as s:
        _locked(s)
        _scope(s, p)
        latest = s.scalar(select(Vorschrift).where(Vorschrift.kennung == p.kennung).order_by(Vorschrift.version.desc()))
        if (latest.version if latest else 0) != expected_version:
            raise ValueError("Zwischenzeitlich geändert. Bitte Seite neu laden.")
        if latest:
            old = Profil.model_validate_json(latest.payload)
            for field in ("kennung", "objekt_id", "bank_konto_id", "referenz", "empfaenger_iban", "start_monat"):
                if getattr(old, field) != getattr(p, field):
                    raise ValueError("Identität/Kontowechsel benötigt gesonderte belegte Migration")
        # Another identity may never disguise the same monthly obligation.
        for row in s.scalars(select(Vorschrift)):
            other = Profil.model_validate_json(row.payload)
            if other.kennung != p.kennung and (other.bank_konto_id, other.referenz) == (p.bank_konto_id, p.referenz):
                raise ValueError("Vorschreibung für Konto/Referenz bereits vorhanden")
        payload = dumps(p.model_dump(mode="json"))
        if latest and latest.payload == payload:
            return latest.version
        version = expected_version + 1
        s.add(Vorschrift(kennung=p.kennung, version=version, payload=payload, sha256=digest(payload), akteur=actor))
        s.commit()
        return version


def profiles(s):
    groups = defaultdict(list)
    for r in s.scalars(select(Vorschrift).order_by(Vorschrift.kennung, Vorschrift.version)):
        groups[r.kennung].append(r)
    return groups


def _bank_paid(s, p, monat, first, last, snapshot=None):
    paid, evidence, unclear, own = 0, [], [], 0
    # Restrict to actual debit entries, explicit beneficiary and unique unit
    # reference. No fuzzy names, no tenant receipts and no total/child sums.
    for tx in s.scalars(select(BankTransaktionTable).where(BankTransaktionTable.bank_konto_id == p.bank_konto_id)):
        if tx.betrag_cent == 0 or tx.waehrung != "EUR":
            continue
        ref = tx.referenz or ""
        if not re.search(r"(?<![A-Za-z0-9-])"+re.escape(p.referenz)+r"(?![A-Za-z0-9-])", ref):
            continue
        if re.sub(r"\s+", "", tx.gegenkonto_iban or "").upper() != p.empfaenger_iban:
            if first <= tx.buchungsdatum <= last:
                unclear.append(tx.id)
            continue
        periods = set(re.findall(r"(?<!\d)(20\d{2}-(?:0[1-9]|1[0-2]))(?![-\d])", ref))
        periods |= {f"{y}-{m}" for m, y in re.findall(r"(?<![\d.])((?:0[1-9]|1[0-2]))/(20\d{2})(?!\d)", ref)}
        if tx.betrag_cent > 0:
            if monat in periods or (not periods and first <= tx.buchungsdatum <= last):
                unclear.append(tx.id)  # possible reversal; do not declare settled
            continue
        if periods == {monat} and tx.hat_native_id:
            paid += -tx.betrag_cent
            evidence.append(tx.id)
            raw = ref+" "+(tx.roh_zeile or "")
            if snapshot and re.search(r"(?<![A-Za-z0-9-])"+re.escape(snapshot["end_to_end_id"])+r"(?![A-Za-z0-9-])",raw):
                own += -tx.betrag_cent
        elif monat in periods or (not periods and first <= tx.buchungsdatum <= last):
            unclear.append(tx.id)
    return paid, evidence, unclear, own


def _plan(s, monat):
    first, last = month_dates(monat)
    rows = []
    for key, versions in profiles(s).items():
        latest = Profil.model_validate_json(versions[-1].payload)
        applicable = [v for v in versions if Profil.model_validate_json(v.payload).gueltig_ab <= first]
        version = max(applicable,key=lambda v:(Profil.model_validate_json(v.payload).gueltig_ab,v.version)) if applicable else versions[-1]
        p = Profil.model_validate_json(version.payload)
        reservation = s.scalar(select(Reservierung).where(Reservierung.kennung == key, Reservierung.monat == monat))
        snapshot = json.loads(reservation.snapshot) if reservation else None
        try:
            obj, bank, owner = _scope(s, p)
        except ValueError as exc:
            rows.append(dict(kennung=key,profil_version=version.version,letzte_version=versions[-1].version,
                objekt_id=p.objekt_id,objekt=p.objekt_id,einheit=p.einheit,bank_konto_id=p.bank_konto_id,
                soll_cent=p.betrag_cent,bezahlt_cent=0,betrag_cent=p.betrag_cent,status="KLAEREN",hinweis=str(exc),
                datei_pruefen=True,datei_id=reservation.datei_id if reservation else None))
            continue
        paid, evidence, ambiguous, own_paid = _bank_paid(s, p, monat, first, last,snapshot)
        status, reason = "BEREIT", "Geprüfte wiederkehrende Vorschreibung"
        if monat < latest.start_monat or p.gueltig_ab > last:
            status, reason = "NICHT_FAELLIG", "Beginn noch nicht erreicht"
        elif latest.kostenende and latest.kostenende < first:
            status, reason = "AUSGESCHIEDEN", latest.hinweis or "Kostenübergang dokumentiert"
        elif latest.status == "KLAEREN":
            status, reason = "KLAEREN", latest.hinweis
        elif latest.kostenende and first <= latest.kostenende < last:
            status, reason = "KLAEREN", "Kostenübergang im Monat – anteilige Abrechnung klären"
        elif any(first < Profil.model_validate_json(v.payload).gueltig_ab <= last for v in versions):
            status, reason = "KLAEREN", "Vorschreibungsänderung im Monat – Teilbeträge prüfen"
        elif p.gueltig_bis < last:
            status, reason = "KLAEREN", "Vorschreibung abgelaufen / nicht für ganzen Monat gültig"
        elif ambiguous:
            status, reason = "KLAEREN", "Passende Bankbelastung ohne eindeutigen Monats-/Einzelnachweis"
        elif paid >= p.betrag_cent:
            status, reason = "BEZAHLT", "Durch eindeutig bezeichnete importierte Bankbelastung gedeckt"
        elif paid:
            reason = "Rest nach belegter Teilzahlung"
        conflict = False
        if reservation:
            previous_status = status
            status = "DATEI_VORHANDEN"
            reason = "Bereits erstellt/importiert – kein erneuter Datenträger; keine Zahlungsbestätigung"
            conflict = (snapshot["betrag_cent"] + snapshot.get("bezahlt_cent",0) != p.betrag_cent
                or paid-own_paid != snapshot.get("bezahlt_cent", 0) or previous_status not in ("BEREIT","BEZAHLT")
                or (own_paid and own_paid != snapshot["betrag_cent"])
                or snapshot["quelle_sha256"] != p.quelle_sha256
                or snapshot["ausfuehrung"] != execution_day(first,p.zahlungstag).isoformat()
                or snapshot["empfaenger"] != p.empfaenger or snapshot["empfaenger_bic"] != p.empfaenger_bic)
            if conflict:
                reason = "Datei vorhanden, aber Vorschreibung/Zahlungsstand/Übergabe geändert: vor Signatur prüfen"
            elif own_paid == snapshot["betrag_cent"] and paid >= p.betrag_cent:
                status, reason = "BEZAHLT", "Eigene Datei durch native Bankbuchung mit Auftragsreferenz nachgewiesen"
        rows.append(dict(kennung=key, profil_version=version.version, letzte_version=versions[-1].version,
            objekt_id=obj.id, objekt=obj.bezeichnung, einheit=p.einheit, bank_konto_id=bank.id,
            gesellschaft=owner.name, absender_iban=iban(bank.iban), absender_bic=p.absender_bic,
            empfaenger=p.empfaenger, empfaenger_iban=p.empfaenger_iban, empfaenger_bic=p.empfaenger_bic,
            referenz=p.referenz, monat=monat, soll_cent=p.betrag_cent, bezahlt_cent=paid,
            betrag_cent=max(0,p.betrag_cent-paid), status=status, hinweis=reason,
            bankbelege=evidence, unklare_bankbelege=ambiguous,
            quelle=p.quelle, quelle_sha256=p.quelle_sha256,
            ausfuehrung=execution_day(first,p.zahlungstag).isoformat(),
            datei_pruefen=conflict, datei_id=reservation.datei_id if reservation else None))
    return rows


def plan(factory, monat):
    with factory() as s:
        return _plan(s, monat)


def xml_file(rows, batch_id, created):
    def add(parent, name, value=None):
        node = ET.SubElement(parent, "{"+NS+"}"+name)
        if value is not None:
            node.text = str(value)
        return node
    def money(cents):
        return f"{cents//100}.{cents%100:02d}"
    if not rows or len({(r["bank_konto_id"], r["absender_iban"], r["absender_bic"], r["ausfuehrung"]) for r in rows}) != 1:
        raise ValueError("Datei benötigt ein Konto und einen Ausführungstag")
    first = rows[0]
    root = ET.Element("{"+NS+"}Document")
    init = add(root,"CstmrCdtTrfInitn")
    head = add(init,"GrpHdr")
    total = money(sum(r["betrag_cent"] for r in rows))
    for n,v in [("MsgId",batch_id),("CreDtTm",created),("NbOfTxs",len(rows)),("CtrlSum",total)]:
        add(head,n,v)
    add(add(head,"InitgPty"),"Nm",first["gesellschaft"][:70])
    payment = add(init,"PmtInf")
    for n,v in [("PmtInfId",batch_id),("PmtMtd","TRF"),("BtchBookg","false"),("NbOfTxs",len(rows)),("CtrlSum",total)]:
        add(payment,n,v)
    add(add(add(payment,"PmtTpInf"),"SvcLvl"),"Cd","SEPA")
    add(payment,"ReqdExctnDt",first["ausfuehrung"])
    add(add(payment,"Dbtr"),"Nm",first["gesellschaft"][:70])
    add(add(add(payment,"DbtrAcct"),"Id"),"IBAN",iban(first["absender_iban"]))
    add(add(add(payment,"DbtrAgt"),"FinInstnId"),"BIC",first["absender_bic"])
    add(payment,"ChrgBr","SLEV")
    for row in rows:
        tx = add(payment,"CdtTrfTxInf")
        add(add(tx,"PmtId"),"EndToEndId",row["end_to_end_id"])
        add(add(tx,"Amt"),"InstdAmt",money(row["betrag_cent"])).set("Ccy","EUR")
        add(add(add(tx,"CdtrAgt"),"FinInstnId"),"BIC",row["empfaenger_bic"])
        add(add(tx,"Cdtr"),"Nm",row["empfaenger"])
        add(add(add(tx,"CdtrAcct"),"Id"),"IBAN",iban(row["empfaenger_iban"]))
        add(add(tx,"RmtInf"),"Ustrd",f'{row["referenz"]} WEG-Vorschreibung {row["monat"]} Top {row["einheit"]}'[:140])
    return ET.tostring(root, encoding="utf-8",xml_declaration=True).decode("utf-8")


def generate(factory, *, today):
    """Only this month's not-yet-reserved items; repeated daily checks are cheap.
    Overdue requested dates are held, never silently rescheduled/collected.
    A partial month run can later add resolved holds without repeating files.
    """
    monat = today.strftime("%Y-%m")
    with factory() as s:
        _locked(s)
        rows = _plan(s, monat)
        groups = defaultdict(list)
        for row in rows:
            if row["status"] == "BEREIT":
                if date.fromisoformat(row["ausfuehrung"]) <= today:
                    row.update(status="KLAEREN", hinweis="Ausführungstermin verstrichen oder heute – gesonderten Zahlungslauf prüfen")
                else:
                    groups[(row["bank_konto_id"], row["ausfuehrung"])].append(row)
        created = datetime.now(timezone.utc).isoformat(timespec="seconds")
        ids = []
        for key, items in sorted(groups.items()):
            batch_id = "JLB-BK-"+monat.replace("-","")+"-"+digest(dumps(items))[:12]
            for r in items:
                r["end_to_end_id"] = "JLB-BK-"+monat.replace("-","")+"-"+digest(r["kennung"])[:16]
            xml = xml_file(items,batch_id,created)
            s.add(Datei(id=batch_id, monat=monat, bank_konto_id=key[0], xml=xml,
                        sha256=digest(xml), status="ERSTELLT", nachweis="Automatisch erstellt; Bankprüfung und Signatur offen"))
            for row in items:
                s.add(Reservierung(kennung=row["kennung"],monat=monat,datei_id=batch_id,snapshot=dumps(row)))
            ids.append(batch_id)
        report = dict(monat=monat, geprueft_am=created, neue_dateien=ids, zeilen=rows,
                      hinweis="Keine Bankausführung. Bankdaten/Deckung vor Signatur prüfen; EBICS noch nicht angebunden.")
        lauf = s.get(Lauf,monat)
        if lauf:
            lauf.bericht = dumps(report)
        else:
            s.add(Lauf(monat=monat,bericht=dumps(report)))
        s.commit()
        return report


def import_existing(factory, *, xml, monat, bank_konto_id, evidence):
    """Adopt an already uploaded original, without generating/transmitting it.
    Exact IBAN, reference, count, sums and dates must match approved profiles.
    Only onboarding evidence; later edits cannot erase reservations.
    """
    if not evidence.strip() or len(xml) > 2000000 or "<!" in xml:
        raise ValueError("Originaldatei/Nachweis ungültig")
    root = ET.fromstring(xml)
    ns = {"p": NS}
    def get(node, path):
        result = node.find(path, ns)
        if result is None or result.text is None:
            raise ValueError("Unvollständige SEPA-Datei")
        return result.text
    if root.tag != "{"+NS+"}Document":
        raise ValueError("SEPA-Format ungültig")
    init = root.find("p:CstmrCdtTrfInitn",ns)
    if init is None or len(init.findall("p:PmtInf",ns)) != 1:
        raise ValueError("Ein Zahlungsblock erforderlich")
    batch_id = get(init,"p:GrpHdr/p:MsgId")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,35}",batch_id):
        raise ValueError("Nachrichten-ID ungültig")
    payment = init.find("p:PmtInf",ns)
    if get(payment,"p:PmtMtd") != "TRF":
        raise ValueError("Nur Überweisungen zulässig")
    from decimal import Decimal
    with factory() as s:
        _locked(s)
        existing = s.get(Datei,batch_id)
        if existing:
            if existing.sha256 != digest(xml) or existing.monat != monat or existing.bank_konto_id != bank_konto_id:
                raise ValueError("Vorhandene Datei weicht ab")
            return batch_id
        rows = [r for r in _plan(s,monat) if r["bank_konto_id"] == bank_konto_id]
        selected = []
        seen = set()
        for tx in payment.findall("p:CdtTrfTxInf",ns):
            ref = get(tx,"p:RmtInf/p:Ustrd")
            candidates = [r for r in rows if "referenz" in r and re.search(r"(?<![A-Za-z0-9-])"+re.escape(r["referenz"])+r"(?![A-Za-z0-9-])",ref)]
            if len(candidates) != 1:
                raise ValueError("Position nicht eindeutig")
            row = candidates[0]
            node = tx.find("p:Amt/p:InstdAmt",ns)
            if node is None or node.get("Ccy") != "EUR":
                raise ValueError("Nur EUR zulässig")
            amount = Decimal(node.text)*100
            if amount != amount.to_integral_value() or int(amount) != row["betrag_cent"] or row["status"] != "BEREIT":
                raise ValueError("Betrag/Freigabe passt nicht zur geprüften Vorschreibung")
            if iban(get(payment,"p:DbtrAcct/p:Id/p:IBAN")) != row["absender_iban"] or iban(get(tx,"p:CdtrAcct/p:Id/p:IBAN")) != row["empfaenger_iban"]:
                raise ValueError("Konten passen nicht")
            if get(payment,"p:ReqdExctnDt") != row["ausfuehrung"]:
                raise ValueError("Ausführungstag passt nicht")
            e2e = get(tx,"p:PmtId/p:EndToEndId")
            if row["kennung"] in seen or e2e in seen:
                raise ValueError("Doppelte Position")
            seen.update([row["kennung"], e2e])
            row["end_to_end_id"] = e2e
            selected.append(row)
        if not selected:
            raise ValueError("Leere Datei")
        for parent in (payment,init.find("p:GrpHdr",ns)):
            if int(get(parent,"p:NbOfTxs")) != len(selected) or Decimal(get(parent,"p:CtrlSum"))*100 != sum(r["betrag_cent"] for r in selected):
                raise ValueError("Kontrollsumme/Anzahl passt nicht")
        s.add(Datei(id=batch_id,monat=monat,bank_konto_id=bank_konto_id,xml=xml,
            sha256=digest(xml),status="GEORGE_IMPORTIERT",nachweis=evidence))
        for row in selected:
            s.add(Reservierung(kennung=row["kennung"],monat=monat,datei_id=batch_id,snapshot=dumps(row)))
        s.commit()
        return batch_id
