"""Tests für den generischen Echtbetrieb-Intake (Auftrag
HV-20260912-ECHTBETRIEB, `src/mietinkasso/intake/`). Alle Testdaten sind
frei erfunden (synthetisch) - keine echten Personen/Konten/IBANs.

Deckt die im Auftrag genannten Akzeptanzkriterien ab: Einspielung ohne
Rohdaten in Git (rein synthetisch), centgenau rücklesbare Salden,
Dublettentest/Rollback, Objekt 107 ausgeschlossen, keine erfundenen
Werte bei ungeprüften Anfangssalden."""

from __future__ import annotations

import json
from datetime import date

import pytest

from mietinkasso.domain.exceptions import IntakeNichtAnwendbarError
from mietinkasso.intake.apply import wende_an
from mietinkasso.intake.parser import IntakeFormatFehlerError, parse_csv_buendel, parse_json_paket
from mietinkasso.intake.planner import erstelle_plan
from mietinkasso.intake.schema import paket_hash


def _basispaket(**overrides) -> dict:
    basis = {
        "quelle": "test",
        "gesellschaften": [{"id": "JLB", "name": "JLB Projects GmbH"}],
        "objekte": [{"id": "601", "gesellschaft_id": "JLB", "bezeichnung": "Am Corso"}],
        "einheiten": [{"id": "601-T1", "objekt_id": "601", "bezeichnung": "Top 1", "nutzungsstatus": "DAUERVERMIETUNG"}],
        "debitoren": [{"id": "DEB-1", "name": "Erika Musterfrau", "email": "erika@example.at"}],
        "vertraege": [
            {
                "id": "V-1", "einheit_id": "601-T1", "debitor_id": "DEB-1", "gesellschaft_id": "JLB",
                "rechtsordnung": "OESTERREICH_MRG_VOLL", "gueltig_von": "2020-01-01",
            }
        ],
    }
    basis.update(overrides)
    return basis


def _paket(**overrides):
    return parse_json_paket(json.dumps(_basispaket(**overrides)))


def _plan(paket, session_factory):
    return erstelle_plan(paket, session_factory=session_factory)


def _apply(paket, plan, *, stammdaten_repo, op_service, session_factory, akteur="test"):
    return wende_an(
        paket, bestaetigter_hash=plan.paket_hash, stammdaten_repo=stammdaten_repo,
        op_service=op_service, session_factory=session_factory, akteur=akteur,
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_json_paket_liest_vollstaendiges_beispiel():
    paket = _paket(
        eroeffnungen=[
            {"import_id": "E1", "vertrag_id": "V-1", "modus": "GESAMTSALDO", "betrag_cent": 150000,
             "stichtag": "2026-08-31", "quelle_bestaetigt": True}
        ],
        nachbuchungen=[
            {"import_id": "N1", "vertrag_id": "V-1", "typ": "SOLL", "betrag_cent": 76000,
             "belegdatum": "2026-09-01", "buchungsdatum": "2026-09-01", "faelligkeit": "2026-09-05"}
        ],
    )
    assert len(paket.gesellschaften) == 1
    assert paket.eroeffnungen[0].quelle_bestaetigt is True
    assert paket.nachbuchungen[0].betrag_cent == 76000


def test_parse_json_lehnt_fehlendes_pflichtfeld_ab():
    kaputt = _basispaket()
    del kaputt["gesellschaften"][0]["name"]
    with pytest.raises(IntakeFormatFehlerError):
        parse_json_paket(json.dumps(kaputt))


def test_parse_json_lehnt_ungueltiges_json_ab():
    with pytest.raises(IntakeFormatFehlerError):
        parse_json_paket("{kaputt")


def test_parse_csv_buendel_entspricht_json_semantisch():
    csv_dateien = {
        "gesellschaften": "id,name\r\nJLB,JLB Projects GmbH\r\n",
        "objekte": "id,gesellschaft_id,bezeichnung\r\n601,JLB,Am Corso\r\n",
        "einheiten": "id,objekt_id,bezeichnung,nutzungsstatus\r\n601-T1,601,Top 1,DAUERVERMIETUNG\r\n",
        "debitoren": "id,name,email\r\nDEB-1,Erika Musterfrau,erika@example.at\r\n",
        "vertraege": "id,einheit_id,debitor_id,gesellschaft_id,rechtsordnung,gueltig_von\r\nV-1,601-T1,DEB-1,JLB,OESTERREICH_MRG_VOLL,2020-01-01\r\n",
    }
    paket = parse_csv_buendel(quelle="csv-test", dateien=csv_dateien)
    assert paket.gesellschaften == _paket().gesellschaften
    assert paket.vertraege == _paket().vertraege


def test_parse_csv_buendel_lehnt_unbekannte_datei_ab():
    with pytest.raises(IntakeFormatFehlerError):
        parse_csv_buendel(quelle="t", dateien={"unbekannt": "a,b\r\n1,2\r\n"})


# ---------------------------------------------------------------------------
# Plan (Dry-run)
# ---------------------------------------------------------------------------


def test_plan_zeigt_neue_stammdaten_als_neu(session_factory):
    paket = _paket()
    plan = _plan(paket, session_factory)
    assert plan.anwendbar
    assert {b.status for b in plan.befunde} == {"NEU"}
    assert len(plan.befunde) == 5  # Gesellschaft, Objekt, Einheit, Debitor, Vertrag


def test_plan_zeigt_unbekannte_referenz_als_konflikt(session_factory):
    paket = _paket(vertraege=[
        {"id": "V-1", "einheit_id": "NICHT-VORHANDEN", "debitor_id": "DEB-1", "gesellschaft_id": "JLB",
         "rechtsordnung": "OESTERREICH_MRG_VOLL", "gueltig_von": "2020-01-01"}
    ])
    plan = _plan(paket, session_factory)
    assert not plan.anwendbar
    vertrag_befund = next(b for b in plan.befunde if b.entitaet == "Vertrag")
    assert vertrag_befund.status == "KONFLIKT"


def test_plan_zeigt_ungueltigen_nutzungsstatus_als_konflikt(session_factory):
    paket = _paket(einheiten=[{"id": "601-T1", "objekt_id": "601", "bezeichnung": "Top 1", "nutzungsstatus": "UNSINN"}])
    plan = _plan(paket, session_factory)
    assert not plan.anwendbar


def test_plan_zaehlt_fehlende_email_und_faelligkeit_als_hinweis_nicht_als_sperre(session_factory):
    paket = _paket(
        debitoren=[{"id": "DEB-1", "name": "Erika Musterfrau"}],  # keine E-Mail
        eroeffnungen=[
            {"import_id": "E1", "vertrag_id": "V-1", "modus": "EINZEL_OP", "typ": "SOLL", "betrag_cent": 1000,
             "stichtag": "2026-01-01", "quelle_bestaetigt": True}  # keine Fälligkeit
        ],
    )
    plan = _plan(paket, session_factory)
    assert plan.anwendbar  # NICHT blockierend
    assert any("E-Mail" in h for h in plan.hinweise)
    assert any("Fälligkeit" in h for h in plan.hinweise)


# ---------------------------------------------------------------------------
# Objekt 107 / ausgeschlossen
# ---------------------------------------------------------------------------


def test_objekt_107_wird_gesperrt(session_factory, stammdaten_repo, op_service):
    paket = _paket(objekte=[{"id": "107", "gesellschaft_id": "JLB", "bezeichnung": "Sieben Dörfer"}])
    plan = _plan(paket, session_factory)
    assert not plan.anwendbar
    objekt_befund = next(b for b in plan.befunde if b.entitaet == "Objekt")
    assert objekt_befund.status == "GESPERRT"
    with pytest.raises(IntakeNichtAnwendbarError):
        _apply(paket, plan, stammdaten_repo=stammdaten_repo, op_service=op_service, session_factory=session_factory)
    assert stammdaten_repo.get_objekt("107") is None  # nichts geschrieben


def test_objekt_ausgeschlossen_flag_wird_gesperrt_auch_ohne_id_107(session_factory):
    paket = _paket(objekte=[{"id": "999", "gesellschaft_id": "JLB", "bezeichnung": "Anderes", "ausgeschlossen": True}])
    plan = _plan(paket, session_factory)
    assert not plan.anwendbar


def test_einheit_und_vertrag_zu_ausgeschlossenem_objekt_werden_transitiv_gesperrt(session_factory):
    paket = _paket(
        objekte=[{"id": "107", "gesellschaft_id": "JLB", "bezeichnung": "Sieben Dörfer"}],
        einheiten=[{"id": "601-T1", "objekt_id": "107", "bezeichnung": "Top 1", "nutzungsstatus": "DAUERVERMIETUNG"}],
    )
    plan = _plan(paket, session_factory)
    einheit_befund = next(b for b in plan.befunde if b.entitaet == "Einheit")
    vertrag_befund = next(b for b in plan.befunde if b.entitaet == "Vertrag")
    assert einheit_befund.status == "GESPERRT"
    assert vertrag_befund.status == "GESPERRT"


# ---------------------------------------------------------------------------
# Apply: Kontosalden centgenau rücklesbar
# ---------------------------------------------------------------------------


def test_apply_bucht_eroeffnung_und_nachbuchung_centgenau(session_factory, stammdaten_repo, op_service):
    paket = _paket(
        eroeffnungen=[
            {"import_id": "E1", "vertrag_id": "V-1", "modus": "GESAMTSALDO", "betrag_cent": 150000,
             "stichtag": "2026-08-31", "quelle_bestaetigt": True}
        ],
        nachbuchungen=[
            {"import_id": "N1", "vertrag_id": "V-1", "typ": "SOLL", "betrag_cent": 76000,
             "belegdatum": "2026-09-01", "buchungsdatum": "2026-09-01", "faelligkeit": "2026-09-05"}
        ],
    )
    plan = _plan(paket, session_factory)
    assert plan.anwendbar
    ergebnis = _apply(paket, plan, stammdaten_repo=stammdaten_repo, op_service=op_service, session_factory=session_factory)
    assert ergebnis.anzahl_eroeffnungen == 1
    assert ergebnis.anzahl_nachbuchungen == 1

    konto = stammdaten_repo.get_konto_by_vertrag("V-1")
    saldo = op_service.berechne_saldo(konto.id)
    assert saldo.saldo_cent == 150000 + 76000


def test_apply_erfasst_nutzungsstatus_ohne_vertrag_leerstand_kzv_selfstorage(session_factory, stammdaten_repo, op_service):
    paket = _paket(
        einheiten=[
            {"id": "601-T1", "objekt_id": "601", "bezeichnung": "Top 1", "nutzungsstatus": "DAUERVERMIETUNG"},
            {"id": "601-T2", "objekt_id": "601", "bezeichnung": "Top 2 (leer)", "nutzungsstatus": "LEERSTAND"},
            {"id": "601-T3", "objekt_id": "601", "bezeichnung": "Top 3 (KZV)", "nutzungsstatus": "KURZZEITVERMIETUNG"},
            {"id": "601-T4", "objekt_id": "601", "bezeichnung": "Keller (Selfstorage)", "nutzungsstatus": "SELFSTORAGE"},
        ],
    )
    plan = _plan(paket, session_factory)
    assert plan.anwendbar
    _apply(paket, plan, stammdaten_repo=stammdaten_repo, op_service=op_service, session_factory=session_factory)
    assert stammdaten_repo.get_einheit("601-T2").nutzungsstatus == "LEERSTAND"
    assert stammdaten_repo.get_einheit("601-T3").nutzungsstatus == "KURZZEITVERMIETUNG"
    assert stammdaten_repo.get_einheit("601-T4").nutzungsstatus == "SELFSTORAGE"


# ---------------------------------------------------------------------------
# Ungeprüfte Anfangssalden
# ---------------------------------------------------------------------------


def test_eroeffnung_ohne_quelle_bestaetigt_wird_gesperrt(session_factory, stammdaten_repo, op_service):
    paket = _paket(eroeffnungen=[
        {"import_id": "E1", "vertrag_id": "V-1", "modus": "GESAMTSALDO", "betrag_cent": 150000,
         "stichtag": "2026-08-31", "quelle_bestaetigt": False}
    ])
    plan = _plan(paket, session_factory)
    assert not plan.anwendbar
    eroeffnung_befund = next(b for b in plan.befunde if b.entitaet == "Eröffnung")
    assert eroeffnung_befund.status == "GESPERRT"
    with pytest.raises(IntakeNichtAnwendbarError):
        _apply(paket, plan, stammdaten_repo=stammdaten_repo, op_service=op_service, session_factory=session_factory)
    assert stammdaten_repo.get_vertrag("V-1") is None  # GESAMTER Lauf blockiert, auch die Stammdaten


# ---------------------------------------------------------------------------
# Wiederholung/Dublettentest, Konflikt, atomarer Rollback
# ---------------------------------------------------------------------------


def test_wiederholter_identischer_lauf_ist_wirkungslos(session_factory, stammdaten_repo, op_service):
    paket = _paket(eroeffnungen=[
        {"import_id": "E1", "vertrag_id": "V-1", "modus": "GESAMTSALDO", "betrag_cent": 150000,
         "stichtag": "2026-08-31", "quelle_bestaetigt": True}
    ])
    plan1 = _plan(paket, session_factory)
    _apply(paket, plan1, stammdaten_repo=stammdaten_repo, op_service=op_service, session_factory=session_factory)

    plan2 = _plan(paket, session_factory)
    assert plan2.anwendbar
    assert {b.status for b in plan2.befunde} == {"UNVERAENDERT"}
    _apply(paket, plan2, stammdaten_repo=stammdaten_repo, op_service=op_service, session_factory=session_factory)

    konto = stammdaten_repo.get_konto_by_vertrag("V-1")
    assert op_service.berechne_saldo(konto.id).saldo_cent == 150000  # keine Verdopplung
    assert len(op_service.list_alle_positionen(konto.id)) == 1


def test_geaenderter_gleicher_inhalt_ist_konflikt(session_factory, stammdaten_repo, op_service):
    paket1 = _paket()
    plan1 = _plan(paket1, session_factory)
    _apply(paket1, plan1, stammdaten_repo=stammdaten_repo, op_service=op_service, session_factory=session_factory)

    paket2 = _paket(debitoren=[{"id": "DEB-1", "name": "ANDERER NAME", "email": "erika@example.at"}])
    plan2 = _plan(paket2, session_factory)
    assert not plan2.anwendbar
    debitor_befund = next(b for b in plan2.befunde if b.entitaet == "Debitor")
    assert debitor_befund.status == "KONFLIKT"
    with pytest.raises(IntakeNichtAnwendbarError):
        _apply(paket2, plan2, stammdaten_repo=stammdaten_repo, op_service=op_service, session_factory=session_factory)
    assert stammdaten_repo.get_debitor("DEB-1").name == "Erika Musterfrau"  # unverändert


def test_atomarer_rollback_bei_einer_kaputten_zeile(session_factory, stammdaten_repo, op_service):
    paket = _paket(nachbuchungen=[
        {"import_id": "N1", "vertrag_id": "V-1", "typ": "SOLL", "betrag_cent": 100, "belegdatum": "2026-01-01", "buchungsdatum": "2026-01-01"},
        {"import_id": "N2", "vertrag_id": "UNBEKANNT", "typ": "SOLL", "betrag_cent": 200, "belegdatum": "2026-01-01", "buchungsdatum": "2026-01-01"},
    ])
    plan = _plan(paket, session_factory)
    assert not plan.anwendbar
    with pytest.raises(IntakeNichtAnwendbarError):
        _apply(paket, plan, stammdaten_repo=stammdaten_repo, op_service=op_service, session_factory=session_factory)
    # NICHTS wurde geschrieben - auch nicht die für sich genommen "guten" Stammdaten/N1.
    assert stammdaten_repo.get_gesellschaft("JLB") is None
    assert stammdaten_repo.get_vertrag("V-1") is None


def test_doppelte_widersprechende_gesamtsaldo_eroeffnung_im_selben_paket_ist_konflikt(session_factory):
    paket = _paket(eroeffnungen=[
        {"import_id": "E1", "vertrag_id": "V-1", "modus": "GESAMTSALDO", "betrag_cent": 100000,
         "stichtag": "2026-08-31", "quelle_bestaetigt": True},
        {"import_id": "E2", "vertrag_id": "V-1", "modus": "GESAMTSALDO", "betrag_cent": 999999,
         "stichtag": "2026-08-31", "quelle_bestaetigt": True},
    ])
    plan = _plan(paket, session_factory)
    assert not plan.anwendbar


def test_nachbuchung_vor_gesamtsaldo_stichtag_ist_konflikt_journal_nicht_doppelt(session_factory):
    paket = _paket(
        eroeffnungen=[
            {"import_id": "E1", "vertrag_id": "V-1", "modus": "GESAMTSALDO", "betrag_cent": 150000,
             "stichtag": "2026-08-31", "quelle_bestaetigt": True}
        ],
        nachbuchungen=[
            {"import_id": "N1", "vertrag_id": "V-1", "typ": "SOLL", "betrag_cent": 5000,
             "belegdatum": "2026-08-15", "buchungsdatum": "2026-08-15"}  # VOR dem Stichtag
        ],
    )
    plan = _plan(paket, session_factory)
    assert not plan.anwendbar
    nachbuchung_befund = next(b for b in plan.befunde if b.entitaet == "Nachbuchung")
    assert nachbuchung_befund.status == "KONFLIKT"
    assert "Journal" in nachbuchung_befund.grund


def test_einzel_op_ohne_typ_ist_konflikt(session_factory):
    paket = _paket(eroeffnungen=[
        {"import_id": "E1", "vertrag_id": "V-1", "modus": "EINZEL_OP", "betrag_cent": 1000,
         "stichtag": "2026-01-01", "quelle_bestaetigt": True}
    ])
    plan = _plan(paket, session_factory)
    assert not plan.anwendbar


# ---------------------------------------------------------------------------
# Apply bindet sich an identischen Inhalt (Hash-Prüfung)
# ---------------------------------------------------------------------------


def test_apply_lehnt_falschen_bestaetigungshash_ab(session_factory, stammdaten_repo, op_service):
    paket = _paket()
    with pytest.raises(ValueError, match="stimmt nicht"):
        wende_an(
            paket, bestaetigter_hash="offensichtlich-falscher-hash", stammdaten_repo=stammdaten_repo,
            op_service=op_service, session_factory=session_factory, akteur="test",
        )
    assert stammdaten_repo.get_gesellschaft("JLB") is None


def test_apply_lehnt_ab_wenn_datei_sich_seit_plan_geaendert_hat(session_factory, stammdaten_repo, op_service):
    paket_alt = _paket()
    plan_alt = _plan(paket_alt, session_factory)
    paket_neu = _paket(debitoren=[{"id": "DEB-1", "name": "Geänderter Name", "email": "erika@example.at"}])
    with pytest.raises(ValueError, match="stimmt nicht"):
        _apply(paket_neu, plan_alt, stammdaten_repo=stammdaten_repo, op_service=op_service, session_factory=session_factory)


def test_paket_hash_ist_deterministisch_fuer_identischen_inhalt():
    paket_a = _paket()
    paket_b = _paket()
    assert paket_hash(paket_a) == paket_hash(paket_b)


def test_paket_hash_aendert_sich_bei_geaendertem_inhalt():
    paket_a = _paket()
    paket_b = _paket(debitoren=[{"id": "DEB-1", "name": "Anderer Name", "email": "erika@example.at"}])
    assert paket_hash(paket_a) != paket_hash(paket_b)


# ---------------------------------------------------------------------------
# Ergänzung HV-20260912-ECHTBETRIEB: Rechtsordnung UNGEKLAERT
# ---------------------------------------------------------------------------


def test_rechtsordnung_ungeklaert_ist_gueltig_aber_wird_als_hinweis_gezaehlt(session_factory):
    paket = _paket(vertraege=[
        {"id": "V-1", "einheit_id": "601-T1", "debitor_id": "DEB-1", "gesellschaft_id": "JLB",
         "rechtsordnung": "UNGEKLAERT", "gueltig_von": "2020-01-01"}
    ])
    plan = _plan(paket, session_factory)
    assert plan.anwendbar  # keine Pflichtkategorie erraten -> kein Sperrgrund
    vertrag_befund = next(b for b in plan.befunde if b.entitaet == "Vertrag")
    assert vertrag_befund.status == "NEU"
    assert any("UNGEKLAERT" in h for h in plan.hinweise)


# ---------------------------------------------------------------------------
# Ergänzung HV-20260912-ECHTBETRIEB: optionale Sperren (RECHTSANWALT/
# RATENPLAN/MANUELL/...) je Vertrag, dauerhaft über die bestehende
# SperreTable/aktive_sperren gespeichert (dieselbe, die
# mahnwesen/service.py als harte Mahnsperre auswertet).
# ---------------------------------------------------------------------------


def test_sperre_wird_dauerhaft_gespeichert_und_von_stammdaten_repo_gelesen(session_factory, stammdaten_repo, op_service):
    paket = _paket(sperren=[{"vertrag_id": "V-1", "grund": "RECHTSANWALT", "kommentar": "RA Dr. Muster beauftragt"}])
    plan = _plan(paket, session_factory)
    assert plan.anwendbar
    sperre_befund = next(b for b in plan.befunde if b.entitaet == "Sperre")
    assert sperre_befund.status == "NEU"
    _apply(paket, plan, stammdaten_repo=stammdaten_repo, op_service=op_service, session_factory=session_factory)

    aktive = stammdaten_repo.aktive_sperren("V-1")
    assert len(aktive) == 1
    assert aktive[0].grund == "RECHTSANWALT"
    assert aktive[0].kommentar == "RA Dr. Muster beauftragt"


def test_sperre_ungueltiger_grund_ist_konflikt(session_factory):
    paket = _paket(sperren=[{"vertrag_id": "V-1", "grund": "UNSINN"}])
    plan = _plan(paket, session_factory)
    assert not plan.anwendbar


def test_sperre_unbekannter_vertrag_ist_konflikt(session_factory):
    paket = _paket(sperren=[{"vertrag_id": "UNBEKANNT", "grund": "MANUELL"}])
    plan = _plan(paket, session_factory)
    assert not plan.anwendbar


def test_sperre_replay_erzeugt_keine_zweite_zeile(session_factory, stammdaten_repo, op_service):
    paket = _paket(sperren=[{"vertrag_id": "V-1", "grund": "RATENPLAN", "kommentar": "3 Raten"}])
    plan1 = _plan(paket, session_factory)
    _apply(paket, plan1, stammdaten_repo=stammdaten_repo, op_service=op_service, session_factory=session_factory)

    plan2 = _plan(paket, session_factory)
    sperre_befund = next(b for b in plan2.befunde if b.entitaet == "Sperre")
    assert sperre_befund.status == "UNVERAENDERT"
    _apply(paket, plan2, stammdaten_repo=stammdaten_repo, op_service=op_service, session_factory=session_factory)

    assert len(stammdaten_repo.aktive_sperren("V-1")) == 1  # keine Dublette


def test_zwei_verschiedene_sperren_desselben_vertrags_bleiben_beide_bestehen(session_factory, stammdaten_repo, op_service):
    paket = _paket(sperren=[
        {"vertrag_id": "V-1", "grund": "RECHTSANWALT", "kommentar": "RA beauftragt"},
        {"vertrag_id": "V-1", "grund": "RATENPLAN", "kommentar": "3 Raten"},
    ])
    plan = _plan(paket, session_factory)
    assert plan.anwendbar
    _apply(paket, plan, stammdaten_repo=stammdaten_repo, op_service=op_service, session_factory=session_factory)
    gruende = {s.grund for s in stammdaten_repo.aktive_sperren("V-1")}
    assert gruende == {"RECHTSANWALT", "RATENPLAN"}


# ---------------------------------------------------------------------------
# Ergänzung HV-20260912-ECHTBETRIEB: optionale Vertragskomponenten (HMZ/
# Küche/Parkplatz/BK-VZ) über denselben atomaren Intake, keine Indexfreigabe.
# ---------------------------------------------------------------------------


def test_komponente_wird_atomar_angelegt_und_lesbar(session_factory, stammdaten_repo, op_service):
    paket = _paket(komponenten=[
        {"id": "K-HMZ", "vertrag_id": "V-1", "art": "HMZ", "bezeichnung": "Hauptmietzins",
         "betrag_cent": 50000, "gueltig_von": "2020-01-01", "indexierbar": True},
    ])
    plan = _plan(paket, session_factory)
    assert plan.anwendbar
    komponente_befund = next(b for b in plan.befunde if b.entitaet == "Komponente")
    assert komponente_befund.status == "NEU"
    _apply(paket, plan, stammdaten_repo=stammdaten_repo, op_service=op_service, session_factory=session_factory)

    komponente = stammdaten_repo.get_komponente("K-HMZ")
    assert komponente is not None
    assert komponente.betrag_cent == 50000
    assert komponente.indexierbar is True
    # Keine Indexklausel/-freigabe wurde durch das bloße Einspielen ausgelöst.
    from mietinkasso.infrastructure.db.tables import IndexKlauselTable
    with session_factory() as session:
        from sqlalchemy import select
        assert session.execute(select(IndexKlauselTable)).first() is None


def test_komponente_unbekannter_vertrag_ist_konflikt(session_factory):
    paket = _paket(komponenten=[
        {"id": "K-1", "vertrag_id": "UNBEKANNT", "art": "HMZ", "bezeichnung": "Hauptmietzins",
         "betrag_cent": 50000, "gueltig_von": "2020-01-01"},
    ])
    plan = _plan(paket, session_factory)
    assert not plan.anwendbar


def test_komponente_zu_ausgeschlossenem_objekt_ist_gesperrt(session_factory):
    paket = _paket(
        objekte=[{"id": "107", "gesellschaft_id": "JLB", "bezeichnung": "Sieben Dörfer"}],
        einheiten=[{"id": "601-T1", "objekt_id": "107", "bezeichnung": "Top 1", "nutzungsstatus": "DAUERVERMIETUNG"}],
        komponenten=[
            {"id": "K-1", "vertrag_id": "V-1", "art": "HMZ", "bezeichnung": "Hauptmietzins",
             "betrag_cent": 50000, "gueltig_von": "2020-01-01"},
        ],
    )
    plan = _plan(paket, session_factory)
    komponente_befund = next(b for b in plan.befunde if b.entitaet == "Komponente")
    assert komponente_befund.status == "GESPERRT"


def test_komponente_replay_ist_wirkungslos_geaenderter_inhalt_ist_konflikt(session_factory, stammdaten_repo, op_service):
    paket1 = _paket(komponenten=[
        {"id": "K-1", "vertrag_id": "V-1", "art": "HMZ", "bezeichnung": "Hauptmietzins",
         "betrag_cent": 50000, "gueltig_von": "2020-01-01"},
    ])
    plan1 = _plan(paket1, session_factory)
    _apply(paket1, plan1, stammdaten_repo=stammdaten_repo, op_service=op_service, session_factory=session_factory)

    plan2 = _plan(paket1, session_factory)
    komponente_befund = next(b for b in plan2.befunde if b.entitaet == "Komponente")
    assert komponente_befund.status == "UNVERAENDERT"
    _apply(paket1, plan2, stammdaten_repo=stammdaten_repo, op_service=op_service, session_factory=session_factory)  # No-Op

    paket2 = _paket(komponenten=[
        {"id": "K-1", "vertrag_id": "V-1", "art": "HMZ", "bezeichnung": "Hauptmietzins",
         "betrag_cent": 99999, "gueltig_von": "2020-01-01"},  # abweichender Betrag, gleiche ID
    ])
    plan3 = _plan(paket2, session_factory)
    assert not plan3.anwendbar
    with pytest.raises(IntakeNichtAnwendbarError):
        _apply(paket2, plan3, stammdaten_repo=stammdaten_repo, op_service=op_service, session_factory=session_factory)
    assert stammdaten_repo.get_komponente("K-1").betrag_cent == 50000  # unverändert


# ---------------------------------------------------------------------------
# Ergänzung HV-20260912-ECHTBETRIEB: Eröffnungskorrektur (nachweislich im
# bestätigten Gesamtsaldo fehlender Posten, echtes Datum vor dem Stichtag).
# ---------------------------------------------------------------------------


def _paket_mit_gesamtsaldo(**overrides):
    return _paket(
        eroeffnungen=[
            {"import_id": "E1", "vertrag_id": "V-1", "modus": "GESAMTSALDO", "betrag_cent": 150000,
             "stichtag": "2026-08-31", "quelle_bestaetigt": True}
        ],
        **overrides,
    )


def test_eroeffnungskorrektur_bucht_original_belegdatum_und_uebernahmetag(session_factory, stammdaten_repo, op_service):
    paket = _paket_mit_gesamtsaldo(eroeffnungskorrekturen=[
        {"import_id": "KORR-1", "vertrag_id": "V-1", "typ": "ZAHLUNG", "betrag_cent": 20000,
         "original_belegdatum": "2026-08-20", "grund": "Zahlung fehlt im Original-Gesamtsaldo",
         "quelle_referenz": "BANKBELEG-XY"},
    ])
    plan = _plan(paket, session_factory)
    assert plan.anwendbar
    befund = next(b for b in plan.befunde if b.entitaet == "Eröffnungskorrektur")
    assert befund.status == "NEU"
    ergebnis = wende_an(
        paket, bestaetigter_hash=plan.paket_hash, stammdaten_repo=stammdaten_repo, op_service=op_service,
        session_factory=session_factory, akteur="test", heute=date(2026, 9, 12),
    )
    assert ergebnis.anzahl_eroeffnungskorrekturen == 1

    konto = stammdaten_repo.get_konto_by_vertrag("V-1")
    saldo = op_service.berechne_saldo(konto.id)
    assert saldo.saldo_cent == 150000 - 20000
    korrektur_zeile = next(p for p in saldo.positionen if p.quelle_system == "eroeffnungskorrektur")
    assert korrektur_zeile.belegdatum == date(2026, 8, 20)
    assert korrektur_zeile.buchungsdatum == date(2026, 9, 12)


def test_eroeffnungskorrektur_ohne_gesamtsaldo_ist_konflikt(session_factory):
    paket = _paket(eroeffnungskorrekturen=[
        {"import_id": "KORR-1", "vertrag_id": "V-1", "typ": "ZAHLUNG", "betrag_cent": 20000,
         "original_belegdatum": "2026-08-20", "grund": "Test", "quelle_referenz": "Q1"},
    ])
    plan = _plan(paket, session_factory)
    assert not plan.anwendbar
    befund = next(b for b in plan.befunde if b.entitaet == "Eröffnungskorrektur")
    assert befund.status == "KONFLIKT"


def test_eroeffnungskorrektur_replay_ist_wirkungslos(session_factory, stammdaten_repo, op_service):
    paket = _paket_mit_gesamtsaldo(eroeffnungskorrekturen=[
        {"import_id": "KORR-1", "vertrag_id": "V-1", "typ": "ZAHLUNG", "betrag_cent": 20000,
         "original_belegdatum": "2026-08-20", "grund": "Test", "quelle_referenz": "Q1"},
    ])
    plan1 = _plan(paket, session_factory)
    wende_an(
        paket, bestaetigter_hash=plan1.paket_hash, stammdaten_repo=stammdaten_repo, op_service=op_service,
        session_factory=session_factory, akteur="test", heute=date(2026, 9, 12),
    )
    plan2 = _plan(paket, session_factory)
    befund = next(b for b in plan2.befunde if b.entitaet == "Eröffnungskorrektur")
    assert befund.status == "UNVERAENDERT"
    # Replay an einem SPÄTEREN Übernahmetag -> weiterhin No-Op, keine Verdopplung
    wende_an(
        paket, bestaetigter_hash=plan2.paket_hash, stammdaten_repo=stammdaten_repo, op_service=op_service,
        session_factory=session_factory, akteur="test", heute=date(2026, 9, 20),
    )
    konto = stammdaten_repo.get_konto_by_vertrag("V-1")
    assert op_service.berechne_saldo(konto.id).saldo_cent == 150000 - 20000
    assert len(op_service.list_alle_positionen(konto.id)) == 2  # Eröffnung + genau EINE Korrektur


# ---------------------------------------------------------------------------
# Codex-Rückprüfung: explizite Betragssemantik - ZAHLUNG/GUTSCHRIFT/SOLL/
# RUECKLASTSCHRIFT sind IMMER positiv einzugeben (Vorzeichen kommt aus dem
# typ); nur der GESAMTSALDO (Eröffnung) darf ein Guthaben (negativ) sein.
# ---------------------------------------------------------------------------


def test_negativer_betrag_bei_nachbuchung_gutschrift_ist_konflikt(session_factory):
    """Ein negativer Betrag würde bei GUTSCHRIFT/ZAHLUNG ein zweites Mal
    negiert und die Schuld versehentlich ERHÖHEN statt zu mindern -
    das muss geblockt werden, nicht stillschweigend verbucht."""

    paket = _paket_mit_gesamtsaldo(nachbuchungen=[
        {"import_id": "N1", "vertrag_id": "V-1", "typ": "GUTSCHRIFT", "betrag_cent": -5000,
         "belegdatum": "2026-09-01", "buchungsdatum": "2026-09-01"},
    ])
    plan = _plan(paket, session_factory)
    assert not plan.anwendbar
    befund = next(b for b in plan.befunde if b.entitaet == "Nachbuchung")
    assert befund.status == "KONFLIKT"
    assert "positiv" in befund.grund


def test_negativer_betrag_bei_einzel_op_eroeffnung_ist_konflikt(session_factory):
    paket = _paket(eroeffnungen=[
        {"import_id": "E1", "vertrag_id": "V-1", "modus": "EINZEL_OP", "typ": "SOLL", "betrag_cent": -1000,
         "stichtag": "2026-01-01", "quelle_bestaetigt": True},
    ])
    plan = _plan(paket, session_factory)
    assert not plan.anwendbar
    befund = next(b for b in plan.befunde if b.entitaet == "Eröffnung")
    assert befund.status == "KONFLIKT"
    assert "positiv" in befund.grund


def test_negativer_betrag_bei_eroeffnungskorrektur_ist_konflikt(session_factory):
    paket = _paket_mit_gesamtsaldo(eroeffnungskorrekturen=[
        {"import_id": "KORR-1", "vertrag_id": "V-1", "typ": "ZAHLUNG", "betrag_cent": -20000,
         "original_belegdatum": "2026-08-20", "grund": "Test", "quelle_referenz": "Q1"},
    ])
    plan = _plan(paket, session_factory)
    assert not plan.anwendbar
    befund = next(b for b in plan.befunde if b.entitaet == "Eröffnungskorrektur")
    assert befund.status == "KONFLIKT"
    assert "positiv" in befund.grund


def test_negativer_gesamtsaldo_guthaben_bleibt_erlaubt(session_factory, stammdaten_repo, op_service):
    """Gegenprobe: der GESAMTSALDO selbst (eine Nettosumme, kein
    typ-Vorzeichen) darf weiterhin negativ sein (ein Guthaben)."""

    paket = _paket(eroeffnungen=[
        {"import_id": "E1", "vertrag_id": "V-1", "modus": "GESAMTSALDO", "betrag_cent": -15000,
         "stichtag": "2026-08-31", "quelle_bestaetigt": True},
    ])
    plan = _plan(paket, session_factory)
    assert plan.anwendbar
    ergebnis = _apply(paket, plan, stammdaten_repo=stammdaten_repo, op_service=op_service, session_factory=session_factory)
    assert ergebnis.anzahl_eroeffnungen == 1
    konto = stammdaten_repo.get_konto_by_vertrag("V-1")
    assert op_service.berechne_saldo(konto.id).saldo_cent == -15000
