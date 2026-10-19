"""Auftrag HV-20261019-MAHNKOSTEN/-VERSAND-KOSTEN-ABSCHLUSS: Tests für
die PDF-Archivierung (`mahnwesen/mahnbrief_archiv.py`) - rein
strukturell (nimmt bereits gerenderte Bytes entgegen)."""

from __future__ import annotations

from datetime import date

import pytest

from mietinkasso.domain.exceptions import VorgangIdKonfliktError
from mietinkasso.mahnwesen.mahnbrief_archiv import MahnbriefArchivRepository


@pytest.fixture
def archiv_repo(session_factory) -> MahnbriefArchivRepository:
    return MahnbriefArchivRepository(session_factory)


def test_archivierung_speichert_pdf_und_hash(archiv_repo):
    row = archiv_repo.archivieren(
        vertrag_id="V-601-3", stufe=2, mahnlauf_schluessel="GRUPPE-A",
        pdf_bytes=b"%PDF-1.4 synthetischer Testinhalt",
        versandnachweis_referenz="mahnungslauf:test-1",
        empfaenger_name="Max Mustermieter", empfaenger_adresse="Musterstraße 1\n1010 Wien",
        erstellt_von="codex-test",
    )
    assert row.pdf_bytes == b"%PDF-1.4 synthetischer Testinhalt"
    assert len(row.pdf_sha256) == 64
    assert len(row.empfaenger_hash) == 64
    assert row.status == "VORBEREITET"
    # Codex-Abnahme 19.10.2026: permanenter, opaker Sende-Schlüssel wird
    # bereits BEI DER ARCHIVIERUNG reserviert, nicht erst beim Versand.
    assert row.sende_schluessel and len(row.sende_schluessel) >= 16

    gelesen = archiv_repo.fuer_mahnlauf(vertrag_id="V-601-3", stufe=2, mahnlauf_schluessel="GRUPPE-A")
    assert gelesen is not None and gelesen.id == row.id


def test_identischer_retry_liefert_dieselbe_zeile_zurueck(archiv_repo):
    """Ein ECHTER Retry (identisches PDF, identischer Empfänger) bleibt
    ein sicherer No-Op."""

    kwargs = dict(
        vertrag_id="V-601-3", stufe=1, mahnlauf_schluessel="GRUPPE-RETRY",
        pdf_bytes=b"%PDF-1.4 identisch", versandnachweis_referenz="mahnungslauf:test-retry",
        empfaenger_name="Max Mustermieter", empfaenger_adresse="Musterstraße 1\n1010 Wien",
        erstellt_von="codex-test",
    )
    erste = archiv_repo.archivieren(**kwargs)
    zweite = archiv_repo.archivieren(**kwargs)
    assert erste.id == zweite.id
    assert zweite.pdf_bytes == b"%PDF-1.4 identisch"
    # Derselbe permanente Sende-Schlüssel bei einer unveränderten
    # Wiederholung (Codex-Abnahme 19.10.2026).
    assert zweite.sende_schluessel == erste.sende_schluessel


def test_abweichendes_pdf_bei_wiederholung_ist_ein_konflikt_kein_stiller_erfolg(archiv_repo):
    """Codex-Rückmeldung 19.10.2026: ein zweiter Versuch mit abweichendem
    PDF-Inhalt für DENSELBEN Mahnlauf darf NICHT still die ERSTE Zeile
    zurückgeben, sondern muss als Konflikt abgelehnt werden."""

    archiv_repo.archivieren(
        vertrag_id="V-601-3", stufe=1, mahnlauf_schluessel="GRUPPE-B",
        pdf_bytes=b"erstes PDF", versandnachweis_referenz="mahnungslauf:test-1",
        empfaenger_name="Max Mustermieter", empfaenger_adresse="Musterstraße 1\n1010 Wien",
        erstellt_von="codex-test",
    )
    with pytest.raises(VorgangIdKonfliktError):
        archiv_repo.archivieren(
            vertrag_id="V-601-3", stufe=1, mahnlauf_schluessel="GRUPPE-B",
            pdf_bytes=b"zweites PDF (abweichend)", versandnachweis_referenz="mahnungslauf:test-2",
            empfaenger_name="Max Mustermieter", empfaenger_adresse="Musterstraße 1\n1010 Wien",
            erstellt_von="codex-test",
        )
    # Keine Mutation: die ursprüngliche Zeile bleibt unverändert bestehen.
    bestehend = archiv_repo.fuer_mahnlauf(vertrag_id="V-601-3", stufe=1, mahnlauf_schluessel="GRUPPE-B")
    assert bestehend.pdf_bytes == b"erstes PDF"


def test_abweichender_empfaenger_bei_wiederholung_ist_ebenfalls_ein_konflikt(archiv_repo):
    archiv_repo.archivieren(
        vertrag_id="V-601-3", stufe=1, mahnlauf_schluessel="GRUPPE-EMPF",
        pdf_bytes=b"dasselbe PDF", versandnachweis_referenz="mahnungslauf:test-1",
        empfaenger_name="Max Mustermieter", empfaenger_adresse="Musterstraße 1\n1010 Wien",
        erstellt_von="codex-test",
    )
    with pytest.raises(VorgangIdKonfliktError):
        archiv_repo.archivieren(
            vertrag_id="V-601-3", stufe=1, mahnlauf_schluessel="GRUPPE-EMPF",
            pdf_bytes=b"dasselbe PDF", versandnachweis_referenz="mahnungslauf:test-1",
            empfaenger_name="Max Mustermieter", empfaenger_adresse="ABWEICHENDE Adresse 9\n1010 Wien",
            erstellt_von="codex-test",
        )


def test_unterschiedliche_mahnlaeufe_desselben_vertrags_werden_getrennt_archiviert(archiv_repo):
    stufe1 = archiv_repo.archivieren(
        vertrag_id="V-601-3", stufe=1, mahnlauf_schluessel="GRUPPE-C",
        pdf_bytes=b"Stufe 1", versandnachweis_referenz="mahnungslauf:s1",
        empfaenger_name="Max Mustermieter", empfaenger_adresse="Musterstraße 1\n1010 Wien",
        erstellt_von="codex-test",
    )
    stufe2 = archiv_repo.archivieren(
        vertrag_id="V-601-3", stufe=2, mahnlauf_schluessel="GRUPPE-D",
        pdf_bytes=b"Stufe 2", versandnachweis_referenz="mahnungslauf:s2",
        empfaenger_name="Max Mustermieter", empfaenger_adresse="Musterstraße 1\n1010 Wien",
        erstellt_von="codex-test",
    )
    assert stufe1.id != stufe2.id


def test_aktualisiere_status_wechselt_vorbereitet_zu_uebermittelt(archiv_repo):
    row = archiv_repo.archivieren(
        vertrag_id="V-601-3", stufe=1, mahnlauf_schluessel="GRUPPE-STATUS",
        pdf_bytes=b"%PDF-1.4 status", versandnachweis_referenz="mahnungslauf:status-1",
        empfaenger_name="Max Mustermieter", empfaenger_adresse="Musterstraße 1\n1010 Wien",
        erstellt_von="codex-test",
    )
    aktualisiert = archiv_repo.aktualisiere_status(
        archiv_id=row.id, neuer_status="UEBERMITTELT", beleg_referenz="000000000000000000001.pdf",
        erfasst_von="codex-test",
    )
    assert aktualisiert.status == "UEBERMITTELT"
    assert aktualisiert.status_beleg_referenz == "000000000000000000001.pdf"
    assert aktualisiert.status_aktualisiert_am is not None


def test_aktualisiere_status_identischer_retry_ist_no_op(archiv_repo):
    row = archiv_repo.archivieren(
        vertrag_id="V-601-3", stufe=1, mahnlauf_schluessel="GRUPPE-STATUS-RETRY",
        pdf_bytes=b"%PDF-1.4 status", versandnachweis_referenz="mahnungslauf:status-2",
        empfaenger_name="Max Mustermieter", empfaenger_adresse="Musterstraße 1\n1010 Wien",
        erstellt_von="codex-test",
    )
    archiv_repo.aktualisiere_status(
        archiv_id=row.id, neuer_status="UEBERMITTELT", beleg_referenz="DATEI-A", erfasst_von="codex-test",
    )
    wiederholt = archiv_repo.aktualisiere_status(
        archiv_id=row.id, neuer_status="UEBERMITTELT", beleg_referenz="DATEI-A", erfasst_von="codex-test",
    )
    assert wiederholt.status == "UEBERMITTELT"


def test_aktualisiere_status_abweichende_belegreferenz_ist_ein_konflikt(archiv_repo):
    row = archiv_repo.archivieren(
        vertrag_id="V-601-3", stufe=1, mahnlauf_schluessel="GRUPPE-STATUS-KONFLIKT",
        pdf_bytes=b"%PDF-1.4 status", versandnachweis_referenz="mahnungslauf:status-3",
        empfaenger_name="Max Mustermieter", empfaenger_adresse="Musterstraße 1\n1010 Wien",
        erstellt_von="codex-test",
    )
    archiv_repo.aktualisiere_status(
        archiv_id=row.id, neuer_status="UEBERMITTELT", beleg_referenz="DATEI-A", erfasst_von="codex-test",
    )
    with pytest.raises(VorgangIdKonfliktError):
        archiv_repo.aktualisiere_status(
            archiv_id=row.id, neuer_status="UEBERMITTELT", beleg_referenz="DATEI-ABWEICHEND", erfasst_von="codex-test",
        )


def test_aktualisiere_status_lehnt_rueckschritt_ab(archiv_repo):
    row = archiv_repo.archivieren(
        vertrag_id="V-601-3", stufe=1, mahnlauf_schluessel="GRUPPE-STATUS-RUECK",
        pdf_bytes=b"%PDF-1.4 status", versandnachweis_referenz="mahnungslauf:status-4",
        empfaenger_name="Max Mustermieter", empfaenger_adresse="Musterstraße 1\n1010 Wien",
        erstellt_von="codex-test",
    )
    archiv_repo.aktualisiere_status(
        archiv_id=row.id, neuer_status="UEBERMITTELT", beleg_referenz="DATEI-A", erfasst_von="codex-test",
    )
    with pytest.raises(ValueError):
        archiv_repo.aktualisiere_status(
            archiv_id=row.id, neuer_status="VORBEREITET", beleg_referenz="DATEI-A", erfasst_von="codex-test",
        )


def _archiviere_fuer_beleg(archiv_repo, mahnlauf_schluessel: str):
    return archiv_repo.archivieren(
        vertrag_id="V-601-3", stufe=1, mahnlauf_schluessel=mahnlauf_schluessel,
        pdf_bytes=b"%PDF-1.4 beleg", versandnachweis_referenz=f"mahnungslauf:{mahnlauf_schluessel}",
        empfaenger_name="Max Mustermieter", empfaenger_adresse="Musterstraße 1\n1010 Wien",
        erstellt_von="codex-test",
    )


def test_importiere_anbieterbeleg_verlangt_tatsaechliches_pdf_kein_bloszer_referenztext(archiv_repo):
    row = _archiviere_fuer_beleg(archiv_repo, "GRUPPE-BELEG-OHNE-PDF")
    with pytest.raises(ValueError):
        archiv_repo.importiere_anbieterbeleg(
            archiv_id=row.id, anbieterbeleg_pdf_bytes=b"", anbieterbeleg_referenz="PORTAL-AUFTRAG-1",
            postaufgabe_datum=date(2026, 10, 20), importiert_von="codex-test",
        )
    with pytest.raises(ValueError):
        archiv_repo.importiere_anbieterbeleg(
            archiv_id=row.id, anbieterbeleg_pdf_bytes=b"kein pdf", anbieterbeleg_referenz="PORTAL-AUFTRAG-1",
            postaufgabe_datum=date(2026, 10, 20), importiert_von="codex-test",
        )


def test_importiere_anbieterbeleg_ist_idempotent_und_lehnt_abweichenden_beleg_ab(archiv_repo):
    row = _archiviere_fuer_beleg(archiv_repo, "GRUPPE-BELEG")
    beleg_pdf = b"%PDF-1.4 Original-Postaufgabebeleg"

    erster_import = archiv_repo.importiere_anbieterbeleg(
        archiv_id=row.id, anbieterbeleg_pdf_bytes=beleg_pdf, anbieterbeleg_referenz="PORTAL-AUFTRAG-4711",
        postaufgabe_datum=date(2026, 10, 20), importiert_von="codex-test",
    )
    assert erster_import.anbieterbeleg_referenz == "PORTAL-AUFTRAG-4711"
    assert erster_import.anbieterbeleg_pdf_bytes == beleg_pdf
    assert len(erster_import.anbieterbeleg_pdf_sha256) == 64
    assert erster_import.anbieterbeleg_postaufgabe_datum == date(2026, 10, 20)
    assert erster_import.anbieterbeleg_importiert_am is not None

    # Echter Retry mit identischem Beleg/Referenz/Datum: sicherer No-Op.
    retry = archiv_repo.importiere_anbieterbeleg(
        archiv_id=row.id, anbieterbeleg_pdf_bytes=beleg_pdf, anbieterbeleg_referenz="PORTAL-AUFTRAG-4711",
        postaufgabe_datum=date(2026, 10, 20), importiert_von="codex-test",
    )
    assert retry.anbieterbeleg_referenz == "PORTAL-AUFTRAG-4711"

    # Abweichende Referenz für DIESELBE Zeile -> Konflikt.
    with pytest.raises(VorgangIdKonfliktError):
        archiv_repo.importiere_anbieterbeleg(
            archiv_id=row.id, anbieterbeleg_pdf_bytes=beleg_pdf, anbieterbeleg_referenz="PORTAL-AUFTRAG-ABWEICHEND",
            postaufgabe_datum=date(2026, 10, 20), importiert_von="codex-test",
        )
    # Abweichendes Dokument für DIESELBE Referenz -> ebenfalls Konflikt.
    with pytest.raises(VorgangIdKonfliktError):
        archiv_repo.importiere_anbieterbeleg(
            archiv_id=row.id, anbieterbeleg_pdf_bytes=b"%PDF-1.4 ein anderes Dokument",
            anbieterbeleg_referenz="PORTAL-AUFTRAG-4711", postaufgabe_datum=date(2026, 10, 20),
            importiert_von="codex-test",
        )
    # Abweichendes Postaufgabedatum -> ebenfalls Konflikt.
    with pytest.raises(VorgangIdKonfliktError):
        archiv_repo.importiere_anbieterbeleg(
            archiv_id=row.id, anbieterbeleg_pdf_bytes=beleg_pdf, anbieterbeleg_referenz="PORTAL-AUFTRAG-4711",
            postaufgabe_datum=date(2026, 10, 21), importiert_von="codex-test",
        )
    # Keine Mutation durch die abgelehnten Versuche - Original bleibt unverändert.
    unveraendert = archiv_repo.get(row.id)
    assert unveraendert.anbieterbeleg_pdf_bytes == beleg_pdf
    assert unveraendert.anbieterbeleg_postaufgabe_datum == date(2026, 10, 20)


def test_importiere_anbieterbeleg_lehnt_fremde_mehrfach_genutzte_referenz_ab(archiv_repo):
    """Codex-Abnahme 19.10.2026: eine Belegreferenz, die bereits einer
    ANDEREN Archivzeile zugeordnet ist, darf nicht für eine zweite Zeile
    übernommen werden - jeder echte Postaufgabebeleg gehört zu GENAU
    EINER Sendung."""

    erste_zeile = _archiviere_fuer_beleg(archiv_repo, "GRUPPE-BELEG-A")
    zweite_zeile = _archiviere_fuer_beleg(archiv_repo, "GRUPPE-BELEG-B")
    beleg_pdf = b"%PDF-1.4 Original-Postaufgabebeleg"

    archiv_repo.importiere_anbieterbeleg(
        archiv_id=erste_zeile.id, anbieterbeleg_pdf_bytes=beleg_pdf, anbieterbeleg_referenz="PORTAL-AUFTRAG-GETEILT",
        postaufgabe_datum=date(2026, 10, 20), importiert_von="codex-test",
    )
    with pytest.raises(VorgangIdKonfliktError):
        archiv_repo.importiere_anbieterbeleg(
            archiv_id=zweite_zeile.id, anbieterbeleg_pdf_bytes=beleg_pdf,
            anbieterbeleg_referenz="PORTAL-AUFTRAG-GETEILT", postaufgabe_datum=date(2026, 10, 20),
            importiert_von="codex-test",
        )
    # Die zweite Zeile bleibt unberührt (kein Beleg importiert).
    assert archiv_repo.get(zweite_zeile.id).anbieterbeleg_referenz is None
