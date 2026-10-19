"""Auftrag HV-20261019-MAHNKOSTEN/-VERSAND-KOSTEN-ABSCHLUSS: Tests für
die PDF-Archivierung (`mahnwesen/mahnbrief_archiv.py`) - rein
strukturell (nimmt bereits gerenderte Bytes entgegen)."""

from __future__ import annotations

from datetime import date

import pytest

from mietinkasso.domain.exceptions import VorgangIdKonfliktError
from mietinkasso.mahnwesen.mahnbrief_archiv import MahnbriefArchivRepository


def _echter_beleg_pdf(text: str) -> bytes:
    """Ein TATSÄCHLICH mit `pypdf` lesbares, einseitiges synthetisches PDF
    (Codex-Abnahme 19.10.2026, dritte Korrektur: `importiere_anbieterbeleg`
    prüft jetzt lesbare Seitenzahl/Unverschlüsseltheit, nicht mehr nur die
    `%PDF-`-Kopfzeile - eine bloße Textfolge wie `b"%PDF-1.4 ..."` besteht
    diese Prüfung nicht mehr). Unterschiedlicher `text` ergibt
    unterschiedliche Bytes/Hashes, damit "abweichender Beleg"-Tests weiter
    zwei tatsächlich verschiedene Dokumente vergleichen."""

    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.cell(0, 10, text)
    return bytes(pdf.output())


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


def test_abweichende_versandnachweis_referenz_bei_wiederholung_ist_ebenfalls_ein_konflikt(archiv_repo):
    """Codex-Abnahme 19.10.2026, zweite Korrektur: `versandnachweis_
    referenz` wurde bislang NICHT in den Konfliktvergleich einbezogen -
    ein Retry mit abweichender Referenz wäre still akzeptiert worden."""

    archiv_repo.archivieren(
        vertrag_id="V-601-3", stufe=1, mahnlauf_schluessel="GRUPPE-REF",
        pdf_bytes=b"dasselbe PDF", versandnachweis_referenz="mahnungslauf:test-1",
        empfaenger_name="Max Mustermieter", empfaenger_adresse="Musterstraße 1\n1010 Wien",
        erstellt_von="codex-test",
    )
    with pytest.raises(VorgangIdKonfliktError):
        archiv_repo.archivieren(
            vertrag_id="V-601-3", stufe=1, mahnlauf_schluessel="GRUPPE-REF",
            pdf_bytes=b"dasselbe PDF", versandnachweis_referenz="mahnungslauf:ABWEICHEND",
            empfaenger_name="Max Mustermieter", empfaenger_adresse="Musterstraße 1\n1010 Wien",
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


def test_importiere_anbieterbeleg_lehnt_abgeschnittenes_oder_beschaedigtes_pdf_ab(archiv_repo):
    """Codex-Abnahme 19.10.2026, dritte Korrektur: die bisherige Prüfung
    akzeptierte JEDE Bytefolge mit `%PDF-`-Kopfzeile anstandslos als
    Beleg (z. B. `b"%PDF-1.4 Text"`) - abgeschnittene/strukturell kaputte
    Bytes (gültige Kopfzeile, aber kein von `pypdf` tatsächlich
    parsebares Dokument) werden jetzt kontrolliert abgelehnt."""

    row = _archiviere_fuer_beleg(archiv_repo, "GRUPPE-BELEG-ABGESCHNITTEN")
    with pytest.raises(ValueError):
        archiv_repo.importiere_anbieterbeleg(
            archiv_id=row.id, anbieterbeleg_pdf_bytes=b"%PDF-1.4 abgeschnitten und strukturell kaputt",
            anbieterbeleg_referenz="PORTAL-AUFTRAG-KAPUTT", postaufgabe_datum=date(2026, 10, 20),
            importiert_von="codex-test",
        )
    assert archiv_repo.get(row.id).anbieterbeleg_referenz is None


def test_importiere_anbieterbeleg_lehnt_verschluesseltes_pdf_ab(archiv_repo):
    """Ein PASSWORTGESCHÜTZTES PDF ist kein lesbarer Beleg - `pypdf`
    meldet `is_encrypted`, bevor überhaupt eine Seitenzahl ermittelbar
    wäre; das muss kontrolliert abgelehnt werden, nicht als "0 Seiten"
    oder mit einem unklaren Parsing-Fehler."""

    import io

    from pypdf import PdfReader, PdfWriter

    roh = _echter_beleg_pdf("geheimer Inhalt")
    writer = PdfWriter()
    for seite in PdfReader(io.BytesIO(roh)).pages:
        writer.add_page(seite)
    writer.encrypt(user_password="synthetisch-geheim", owner_password="synthetisch-owner")
    puffer = io.BytesIO()
    writer.write(puffer)
    verschluesselt = puffer.getvalue()

    row = _archiviere_fuer_beleg(archiv_repo, "GRUPPE-BELEG-VERSCHLUESSELT")
    with pytest.raises(ValueError):
        archiv_repo.importiere_anbieterbeleg(
            archiv_id=row.id, anbieterbeleg_pdf_bytes=verschluesselt,
            anbieterbeleg_referenz="PORTAL-AUFTRAG-VERSCHLUESSELT", postaufgabe_datum=date(2026, 10, 20),
            importiert_von="codex-test",
        )
    assert archiv_repo.get(row.id).anbieterbeleg_referenz is None


def test_importiere_anbieterbeleg_lehnt_pdf_ohne_seite_ab(archiv_repo):
    """Ein strukturell gültiges, aber SEITENLOSES PDF (0 Seiten) ist
    ebenfalls kein gültiger Beleg - mindestens eine Seite ist verlangt."""

    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    puffer = io.BytesIO()
    writer.write(puffer)
    seitenlos = puffer.getvalue()

    row = _archiviere_fuer_beleg(archiv_repo, "GRUPPE-BELEG-SEITENLOS")
    with pytest.raises(ValueError):
        archiv_repo.importiere_anbieterbeleg(
            archiv_id=row.id, anbieterbeleg_pdf_bytes=seitenlos,
            anbieterbeleg_referenz="PORTAL-AUFTRAG-SEITENLOS", postaufgabe_datum=date(2026, 10, 20),
            importiert_von="codex-test",
        )
    assert archiv_repo.get(row.id).anbieterbeleg_referenz is None


def test_importiere_anbieterbeleg_setzt_groessenlimit_im_repository_selbst_durch(archiv_repo):
    """Codex-Abnahme 19.10.2026, dritte Korrektur: das Größenlimit war
    bislang NUR beim Lesen des Backoffice-Uploads durchgesetzt
    (`backoffice/routes/mahnwesen.py::_lese_beleg_begrenzt`) - ein
    anderer Aufrufer dieses Repositorys (z. B. ein künftiger Batch-
    Import) hätte sich daran nicht gehalten. Hier DIREKT gegen das
    Repository geprüft, ohne die Backoffice-Route."""

    from mietinkasso.mahnwesen.mahnbrief_archiv import _MAX_ANBIETERBELEG_PDF_BYTES

    zu_gross = b"%PDF-1.4\n" + b"x" * (_MAX_ANBIETERBELEG_PDF_BYTES + 1)
    row = _archiviere_fuer_beleg(archiv_repo, "GRUPPE-BELEG-ZU-GROSS")
    with pytest.raises(ValueError):
        archiv_repo.importiere_anbieterbeleg(
            archiv_id=row.id, anbieterbeleg_pdf_bytes=zu_gross,
            anbieterbeleg_referenz="PORTAL-AUFTRAG-ZU-GROSS", postaufgabe_datum=date(2026, 10, 20),
            importiert_von="codex-test",
        )
    assert archiv_repo.get(row.id).anbieterbeleg_referenz is None


def test_importiere_anbieterbeleg_ist_idempotent_und_lehnt_abweichenden_beleg_ab(archiv_repo):
    row = _archiviere_fuer_beleg(archiv_repo, "GRUPPE-BELEG")
    beleg_pdf = _echter_beleg_pdf("Original-Postaufgabebeleg")

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
            archiv_id=row.id, anbieterbeleg_pdf_bytes=_echter_beleg_pdf("ein anderes Dokument"),
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
    beleg_pdf = _echter_beleg_pdf("Original-Postaufgabebeleg")

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


def test_importiere_anbieterbeleg_mehrfachverwendung_ist_race_sicher_echte_threads_datei_sqlite(tmp_path):
    """Codex-Abnahme 19.10.2026, zweite Korrektur: die Mehrfachverwendungs-
    prüfung ist ein "lesen, dann entscheiden, dann schreiben"-Ablauf -
    ohne echten Schreib-Lock (`schreibgesperrte_session`) könnten zwei
    ECHT gleichzeitige Importe derselben Belegreferenz für ZWEI
    verschiedene Archivzeilen beide die Prüfung passieren, bevor einer
    von beiden committet. Reproduktion mit zwei echten Threads auf eine
    echte Datei-SQLite-DB (derselbe Grundsatz wie
    `test_ruecklastschriftkosten.py::
    test_nachtraegliche_zurechenbarkeit_datei_sqlite_konkurrierende_abweichende_begruendung_ist_konflikt`)."""

    import threading

    from mietinkasso.infrastructure.db.base import Base
    from mietinkasso.infrastructure.db.session import build_engine, build_session_factory

    db_pfad = tmp_path / "mahnbrief-archiv-nebenlaeufig.db"
    engine = build_engine(f"sqlite:///{db_pfad}")
    Base.metadata.create_all(engine)
    engine.dispose()
    session_factory = build_session_factory(f"sqlite:///{db_pfad}")
    repo = MahnbriefArchivRepository(session_factory)

    # Zwei UNABHÄNGIGE Archivzeilen (verschiedene Mahnlauf-Schlüssel) -
    # beide "wollen" gleich darauf dieselbe Belegreferenz übernehmen.
    zeile_a = repo.archivieren(
        vertrag_id="V-601-3", stufe=1, mahnlauf_schluessel="GRUPPE-RACE-A",
        pdf_bytes=b"%PDF-1.4 A", versandnachweis_referenz="mahnungslauf:a",
        empfaenger_name="Max Mustermieter", empfaenger_adresse="Musterstraße 1\n1010 Wien",
        erstellt_von="codex-test",
    )
    zeile_b = repo.archivieren(
        vertrag_id="V-601-3", stufe=2, mahnlauf_schluessel="GRUPPE-RACE-B",
        pdf_bytes=b"%PDF-1.4 B", versandnachweis_referenz="mahnungslauf:b",
        empfaenger_name="Max Mustermieter", empfaenger_adresse="Musterstraße 1\n1010 Wien",
        erstellt_von="codex-test",
    )

    barrier = threading.Barrier(2)
    ergebnisse: dict[str, tuple[str, object]] = {}

    def _importieren(schluessel: str, archiv_id: int, akteur: str):
        barrier.wait(timeout=5)
        try:
            zeile = repo.importiere_anbieterbeleg(
                archiv_id=archiv_id, anbieterbeleg_pdf_bytes=_echter_beleg_pdf("geteilter Beleg"),
                anbieterbeleg_referenz="PORTAL-AUFTRAG-GETEILT-RACE", postaufgabe_datum=date(2026, 10, 20),
                importiert_von=akteur,
            )
            ergebnisse[schluessel] = ("ok", zeile)
        except Exception as exc:  # noqa: BLE001 - Ergebnis wird unten geprüft, nicht verschluckt
            ergebnisse[schluessel] = ("fehler", exc)

    thread_a = threading.Thread(target=_importieren, args=("a", zeile_a.id, "codex-a"))
    thread_b = threading.Thread(target=_importieren, args=("b", zeile_b.id, "codex-b"))
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=15)
    thread_b.join(timeout=15)

    assert set(ergebnisse) == {"a", "b"}  # beide Threads sind tatsächlich fertig geworden, kein Hänger
    erfolgreiche = [k for k, (status, _) in ergebnisse.items() if status == "ok"]
    fehlgeschlagene = [(k, exc) for k, (status, exc) in ergebnisse.items() if status == "fehler"]
    assert len(erfolgreiche) == 1  # GENAU EINER der beiden gewinnt
    assert len(fehlgeschlagene) == 1
    assert isinstance(fehlgeschlagene[0][1], VorgangIdKonfliktError)
