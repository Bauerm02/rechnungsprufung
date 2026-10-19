"""Auftrag HV-20261019-MAHNKOSTEN: Tests für die PDF-Archivierung
(`mahnwesen/mahnbrief_archiv.py`) - rein strukturell (nimmt bereits
gerenderte Bytes entgegen), siehe dortige Moduldoc für den Live-Gate-
Hinweis (noch kein echter Brief-Versandweg)."""

from __future__ import annotations

import pytest

from mietinkasso.mahnwesen.mahnbrief_archiv import MahnbriefArchivRepository


@pytest.fixture
def archiv_repo(session_factory) -> MahnbriefArchivRepository:
    return MahnbriefArchivRepository(session_factory)


def test_archivierung_speichert_pdf_und_hash(archiv_repo):
    row = archiv_repo.archivieren(
        vertrag_id="V-601-3", stufe=2, mahnlauf_schluessel="GRUPPE-A",
        pdf_bytes=b"%PDF-1.4 synthetischer Testinhalt",
        versandnachweis_referenz="mahnungslauf:test-1", erstellt_von="codex-test",
    )
    assert row.pdf_bytes == b"%PDF-1.4 synthetischer Testinhalt"
    assert len(row.pdf_sha256) == 64

    gelesen = archiv_repo.fuer_mahnlauf(vertrag_id="V-601-3", stufe=2, mahnlauf_schluessel="GRUPPE-A")
    assert gelesen is not None and gelesen.id == row.id


def test_doppelte_archivierung_desselben_mahnlaufs_bucht_nicht_doppelt(archiv_repo):
    erste = archiv_repo.archivieren(
        vertrag_id="V-601-3", stufe=1, mahnlauf_schluessel="GRUPPE-B",
        pdf_bytes=b"erstes PDF", versandnachweis_referenz="mahnungslauf:test-1", erstellt_von="codex-test",
    )
    zweite = archiv_repo.archivieren(
        vertrag_id="V-601-3", stufe=1, mahnlauf_schluessel="GRUPPE-B",
        pdf_bytes=b"zweites PDF (sollte ignoriert werden)",
        versandnachweis_referenz="mahnungslauf:test-2", erstellt_von="codex-test",
    )
    assert erste.id == zweite.id
    assert zweite.pdf_bytes == b"erstes PDF"  # das Archiv bleibt beim ERSTEN tatsächlich belegten Versand


def test_unterschiedliche_mahnlaeufe_desselben_vertrags_werden_getrennt_archiviert(archiv_repo):
    stufe1 = archiv_repo.archivieren(
        vertrag_id="V-601-3", stufe=1, mahnlauf_schluessel="GRUPPE-C",
        pdf_bytes=b"Stufe 1", versandnachweis_referenz="mahnungslauf:s1", erstellt_von="codex-test",
    )
    stufe2 = archiv_repo.archivieren(
        vertrag_id="V-601-3", stufe=2, mahnlauf_schluessel="GRUPPE-D",
        pdf_bytes=b"Stufe 2", versandnachweis_referenz="mahnungslauf:s2", erstellt_von="codex-test",
    )
    assert stufe1.id != stufe2.id
