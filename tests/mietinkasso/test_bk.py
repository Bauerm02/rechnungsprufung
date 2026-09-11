from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from mietinkasso.bk.repository import BKRepository
from mietinkasso.bk.service import BKAnteilEingabe, BKAbrechnungNichtFreigegebenError, BKService
from mietinkasso.domain.enums import BKPositionsart
from mietinkasso.op.repository import OPRepository
from mietinkasso.op.service import OPService


@pytest.fixture
def bk_service(session_factory, stammdaten_repo) -> BKService:
    op_service = OPService(OPRepository(session_factory), stammdaten_repo)
    return BKService(BKRepository(session_factory), op_service)


def test_weg_ruecklage_erzeugt_keinen_automatischen_mieter_op(bk_service, basis_vertrag, ctx_factory):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    abrechnung = bk_service.abrechnung_anlegen(objekt_id="601", abrechnungsjahr=2025)
    bk_service.position_hinzufuegen(
        bk_abrechnung_id=abrechnung.id, bezeichnung="Instandhaltungsrücklage", betrag_cent=500_000,
        art=BKPositionsart.EIGENTUEMER, quelle="WEG-Beschluss 2025-03", profil_referenz="WEG-EIGENTUEMER",
    )
    bk_service.position_hinzufuegen(
        bk_abrechnung_id=abrechnung.id, bezeichnung="Hausbetreuung", betrag_cent=120_000,
        art=BKPositionsart.UMLAGEFAEHIG, quelle="Belege 2025", profil_referenz="MRG-21-24",
    )
    assert bk_service.umlagefaehige_summe_cent(abrechnung.id) == 120_000  # Rücklage bleibt außen vor

    bk_service.anteile_berechnen(
        bk_abrechnung_id=abrechnung.id,
        eingaben=[BKAnteilEingabe(vertrag_id=vertrag.id, anteil_prozent=Decimal("100"), vorauszahlung_cent=100_000)],
    )
    bk_service.pruefen(abrechnung.id)
    bk_service.freigeben(abrechnung.id)
    gebuchte = bk_service.ergebnisse_buchen(ctx=ctx, bk_abrechnung_id=abrechnung.id, konten_je_vertrag={vertrag.id: konto})
    assert len(gebuchte) == 1
    saldo = bk_service._op_service.berechne_saldo(konto.id)
    # Nachbelastung nur aus der umlagefähigen Position (120.000 - 100.000 Vorauszahlung = 20.000), NICHT aus der Rücklage
    assert saldo.saldo_cent == 20_000


def test_bk_entwurf_kann_nicht_gebucht_werden(bk_service, basis_vertrag, ctx_factory):
    """Ein ENTWURF darf keine Forderung und damit auch keine Mahnung auslösen."""

    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    abrechnung = bk_service.abrechnung_anlegen(objekt_id="601", abrechnungsjahr=2026)
    bk_service.position_hinzufuegen(
        bk_abrechnung_id=abrechnung.id, bezeichnung="Hausbetreuung", betrag_cent=120_000,
        art=BKPositionsart.UMLAGEFAEHIG, quelle="Belege 2026", profil_referenz="MRG-21-24",
    )
    bk_service.anteile_berechnen(
        bk_abrechnung_id=abrechnung.id,
        eingaben=[BKAnteilEingabe(vertrag_id=vertrag.id, anteil_prozent=Decimal("100"), vorauszahlung_cent=0)],
    )
    with pytest.raises(BKAbrechnungNichtFreigegebenError):
        bk_service.ergebnisse_buchen(ctx=ctx, bk_abrechnung_id=abrechnung.id, konten_je_vertrag={vertrag.id: konto})

    saldo = bk_service._op_service.berechne_saldo(konto.id)
    assert saldo.saldo_cent == 0  # kein OP, solange die Abrechnung ENTWURF ist
