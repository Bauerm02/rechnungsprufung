"""Tests für die Vier-Stufen-Erweiterung des täglichen Mahnlauf-Treibers
(`HVMailversandService.mahnlauf`, Auftrag HV-20261020-VIER-MAHNSTUFEN) -
Tagesgate (10./25.) und "maximal ein Schreiben pro Vertrag pro Lauf"."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal

import httpx
import pytest

from mietinkasso.domain.enums import OPTyp
from mietinkasso.indexautomatik.bootstrap import bauen
from mietinkasso.indexautomatik.mailops_client import MailOpsClient
from mietinkasso.indexautomatik.mailversand_service import HVMailversandService
from mietinkasso.infrastructure.config import Settings
from mietinkasso.infrastructure.db.tables import MahnFallTable, MahnLaufTable


@pytest.fixture
def hv(session_factory, basis_vertrag, tmp_path):
    token = tmp_path / "synthetic-token"
    token.write_text("SYNTHETIC-TOKEN", encoding="utf-8")
    # Anders als das `hv`-Fixture in test_hv_mailversand.py startet dieses
    # bereits bei "GESENDET" (statt "ANGENOMMEN") - diese Tests prüfen
    # das Tagesgate/die Ein-Schreiben-Bündelung, NICHT den UNSICHER-
    # Zwischenzustand (der ist dort bereits eigenständig abgedeckt).
    state = {"status": "GESENDET", "calls": [], "sent": "2026-09-14T22:30:00Z"}

    def provider(request):
        state["calls"].append(request)
        payload = json.loads(request.content) if request.method == "POST" else None
        ref = payload["referenz"] if payload else request.url.params["referenz"]
        external = "JLBHV-" + hashlib.sha256(json.dumps(ref, ensure_ascii=False).encode()).hexdigest()
        return httpx.Response(200, json={"status": state["status"], "externe_referenz": external,
            "provider_referenz": "SYNTHETIC-SENT-1", "versendet_am": state["sent"] if state["status"] == "GESENDET" else None})

    client = MailOpsClient(token_file=token, transport_factory=lambda: httpx.MockTransport(provider))
    settings = Settings(owner_email="mb@jlb-immo.at", send_enabled=True, hv_mail_allowlist_bestaetigt=True)
    bundle = bauen(session_factory, settings)
    return HVMailversandService(session_factory, bundle, settings, client=client), state


def _seed_debt(hv, contract, account, ctx, *, vier_stufen: bool, day=date(2026, 9, 13), faelligkeit=date(2026, 9, 5)):
    service, _ = hv
    policy = service.policy_repo.anlegen(
        stufe1_tage_nach_faelligkeit=7, stufe2_mindesttage_nach_stufe1_versand=14,
        zinsen_prozent=Decimal("0"), gebuehr_cent=0, status="ENTWURF", vier_stufen_profil_aktiv=vier_stufen,
        stufe2_mindesttage_nach_stufe1_zugang=14, stufe3_mindesttage_nach_stufe2_zugang=14, stufe4_mindesttage_nach_stufe3_zugang=14,
    )
    service.policy_repo.freigeben(policy.id)
    service.op_service.buchen(
        ctx=ctx, konto=account, typ=OPTyp.SOLL, betrag_cent=83000, belegdatum=date(2026, 9, 1),
        buchungsdatum=date(2026, 9, 1), faelligkeit=faelligkeit, beleg_referenz="SYNTHETIC September",
    )
    service.bank_repo.upsert_bank_konto(id="SYNTHETIC-BANK", gesellschaft_id=contract.gesellschaft_id,
        iban="SYNTHETIC-NOT-A-REAL-IBAN", bezeichnung="Testkonto")
    service.bank_service.bestaetige_bankvollstaendigkeit(bank_konto_id="SYNTHETIC-BANK", bestaetigt_bis=day, bestaetigt_von="SYNTHETIC-TEST")


def test_vier_stufen_policy_eskaliert_nur_am_10_oder_25(hv, basis_vertrag, admin_ctx, session_factory):
    """Auftrag HV-20261020-VIER-MAHNSTUFEN: unter einer Policy mit
    `vier_stufen_profil_aktiv=True` darf `mahnlauf()` NUR am 10./25. eines
    Monats tatsächlich planen/versenden - an jedem anderen Tag ist der
    Aufruf ein strukturelles No-op (0/0/0), OHNE dass die Recovery-
    Schritte (bereits vorher im Methodenkörper, hier nicht relevant)
    dadurch ausfallen."""

    service, state = hv
    contract, account = basis_vertrag
    _seed_debt(hv, contract, account, admin_ctx, vier_stufen=True, faelligkeit=date(2026, 9, 5))

    # 12.09.2026 ist weder der 10. noch der 25. - kein Prüflauf.
    service.bank_service.bestaetige_bankvollstaendigkeit(bank_konto_id="SYNTHETIC-BANK", bestaetigt_bis=date(2026, 9, 12), bestaetigt_von="SYNTHETIC-TEST")
    ergebnis_kein_pruefttag = service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 12))
    assert ergebnis_kein_pruefttag == {"geplant": 0, "gesendet": 0, "blockiert": 0}
    with session_factory() as session:
        assert session.query(MahnFallTable).count() == 0

    # 25.09.2026 ist ein Prüftag - Stufe 1 ist bereits seit 7 Tagen fällig
    # (Fälligkeit 05.09., >= 7 Tage ab dem 12.09.) und wird tatsächlich
    # gesendet.
    service.bank_service.bestaetige_bankvollstaendigkeit(bank_konto_id="SYNTHETIC-BANK", bestaetigt_bis=date(2026, 9, 25), bestaetigt_von="SYNTHETIC-TEST")
    ergebnis_pruefttag = service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 25))
    assert ergebnis_pruefttag["gesendet"] == 1
    with session_factory() as session:
        assert session.query(MahnFallTable).count() == 1


def test_alte_zwei_stufen_policy_laeuft_weiterhin_taeglich(hv, basis_vertrag, admin_ctx, session_factory):
    """Rückwärtskompatibilität: eine Policy OHNE `vier_stufen_profil_
    aktiv` wird NICHT auf das Tagesgate umgestellt - kein stillschweigender
    Verhaltenswechsel für bereits laufende Zwei-Stufen-Mahnzyklen."""

    service, state = hv
    contract, account = basis_vertrag
    _seed_debt(hv, contract, account, admin_ctx, vier_stufen=False, faelligkeit=date(2026, 9, 5))

    # 12.09.2026 ist KEIN Prüftag des Vier-Stufen-Profils - für die alte
    # Policy spielt das keine Rolle, der tägliche Lauf sendet trotzdem.
    service.bank_service.bestaetige_bankvollstaendigkeit(bank_konto_id="SYNTHETIC-BANK", bestaetigt_bis=date(2026, 9, 12), bestaetigt_von="SYNTHETIC-TEST")
    ergebnis = service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 12))
    assert ergebnis["gesendet"] == 1


def test_zwei_verschiedene_stufen_desselben_vertrags_ergeben_nur_einen_mahnlauf_pro_tag(
    hv, basis_vertrag, admin_ctx, session_factory,
):
    """Nutzervorgabe wörtlich: 'Maximal EIN gebündeltes Schreiben pro
    Vertrag/Monatslauftermin trotz neuer OPs/verschiedener alter Stufen'.
    Eine Forderung erreicht in einem früheren Lauf bereits Stufe 2
    (gesendet + Zugang bestätigt); eine ZWEITE, neue Forderung desselben
    Vertrags wird erst jetzt fällig (Stufe 1). Trotz BEIDER gleichzeitig
    versandreifer Stufen darf dieser EINE Lauf nur EIN Schreiben
    verschicken."""

    service, state = hv
    contract, account = basis_vertrag
    _seed_debt(hv, contract, account, admin_ctx, vier_stufen=True, faelligkeit=date(2026, 9, 5))

    # Lauf 1 (25.09., ein Prüftag - 7-Tage-Wartefrist seit Fälligkeit
    # 05.09. ist längst erreicht): Stufe 1 der ersten (September-)
    # Forderung wird gesendet.
    state["sent"] = "2026-09-25T08:00:00Z"
    service.bank_service.bestaetige_bankvollstaendigkeit(bank_konto_id="SYNTHETIC-BANK", bestaetigt_bis=date(2026, 9, 25), bestaetigt_von="SYNTHETIC-TEST")
    ergebnis_lauf1 = service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 25))
    assert ergebnis_lauf1["gesendet"] == 1

    with session_factory() as session:
        erster_lauf = session.query(MahnLaufTable).order_by(MahnLaufTable.id.desc()).first()
    service.mahn_service.zugang_bestaetigen_fuer_empfaenger(
        ctx=admin_ctx, mahnlauf_id=erster_lauf.id, mitmieter_id=None, heute=date(2026, 9, 26),
        zugang_datum=date(2026, 9, 25), zugangsform="EINSCHREIBEN_RUECKSCHEIN", zugang_beleg="RS-1", akteur="codex-test",
    )

    # Eine ZWEITE, unabhängige Forderung (Oktober-Miete) wird neu fällig.
    service.op_service.buchen(
        ctx=admin_ctx, konto=account, typ=OPTyp.SOLL, betrag_cent=83000, belegdatum=date(2026, 10, 1),
        buchungsdatum=date(2026, 10, 1), faelligkeit=date(2026, 10, 1), beleg_referenz="SYNTHETIC Oktober",
    )
    service.bank_service.bestaetige_bankvollstaendigkeit(bank_konto_id="SYNTHETIC-BANK", bestaetigt_bis=date(2026, 10, 25), bestaetigt_von="SYNTHETIC-TEST")

    # Lauf 2 (25.09.2026 wäre noch zu früh für Stufe 2 - siehe Fristen-
    # Grenzfalltest -, also testen wir direkt den 27.10.2026-Lauf, an dem
    # die ERSTE Forderung Stufe 2 erreicht UND die ZWEITE Forderung
    # (fällig 01.10., Stufe1-Wartefrist 7 Tage) Stufe 1 erreicht hat.
    ergebnis_lauf2 = service.mahnlauf(ctx=admin_ctx, heute=date(2026, 10, 25))
    # Trotz ZWEI versandreifer Stufen (1 für die neue, 2 für die alte
    # Forderung) wird in DIESEM Lauf nur EIN Mahnlauf (die höchste Stufe)
    # tatsächlich dispatcht.
    assert ergebnis_lauf2["gesendet"] == 1
    with session_factory() as session:
        gesendete_laeufe_lauf2 = (
            session.query(MahnLaufTable)
            .filter(MahnLaufTable.id != erster_lauf.id, MahnLaufTable.status == "GESENDET")
            .count()
        )
    assert gesendete_laeufe_lauf2 == 1

    # Die niedrigere Stufe (1, für die neue Forderung) bleibt GEPLANT -
    # kein Verlust, nur verschoben auf einen SPÄTEREN Lauf.
    with session_factory() as session:
        offene_stufe1_faelle = (
            session.query(MahnFallTable)
            .filter(MahnFallTable.stufe == 1, MahnFallTable.status == "GEPLANT")
            .count()
        )
    assert offene_stufe1_faelle == 1


def test_zweiter_aufruf_desselben_pruefdatums_sendet_nicht_erneut_bei_neuer_op(
    hv, basis_vertrag, admin_ctx, session_factory,
):
    """Korrektur nach unabhängiger Abnahme (Codex, 20.10.2026,
    reproduzierter Fehler): `geplante_stufen[-1:]` schützt nur INNERHALB
    EINES Aufrufs - ein zweiter Aufruf für DASSELBE Prüfdatum, bei dem
    inzwischen eine NEUE, bereits fällige Forderung desselben Vertrags
    hinzugekommen ist, löste bisher einen ZWEITEN tatsächlichen Versand
    für denselben Vertrag am selben Tag aus. Der DB-seitige
    Tageslauf-Claim (`MahnTageslaufTable`) muss das verhindern."""

    service, state = hv
    contract, account = basis_vertrag
    _seed_debt(hv, contract, account, admin_ctx, vier_stufen=True, faelligkeit=date(2026, 9, 5))

    state["sent"] = "2026-09-25T08:00:00Z"
    service.bank_service.bestaetige_bankvollstaendigkeit(bank_konto_id="SYNTHETIC-BANK", bestaetigt_bis=date(2026, 9, 25), bestaetigt_von="SYNTHETIC-TEST")
    ergebnis_lauf1 = service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 25))
    assert ergebnis_lauf1["gesendet"] == 1
    with session_factory() as session:
        versendete_laeufe_nach_lauf1 = session.query(MahnLaufTable).filter(MahnLaufTable.status == "GESENDET").count()
    assert versendete_laeufe_nach_lauf1 == 1

    # Zwischenzeitlich (z. B. durch einen manuellen Nachtrag) wird eine
    # ZWEITE, unabhängige Forderung DESSELBEN Vertrags buchbar, die SOFORT
    # (Fälligkeit weit in der Vergangenheit) Stufe-1-reif ist.
    service.op_service.buchen(
        ctx=admin_ctx, konto=account, typ=OPTyp.SOLL, betrag_cent=83000, belegdatum=date(2026, 8, 1),
        buchungsdatum=date(2026, 8, 1), faelligkeit=date(2026, 8, 1), beleg_referenz="SYNTHETIC Nachtrag",
    )

    # Ein ZWEITER Aufruf für DASSELBE Prüfdatum (Crash-Retry/Parallelität/
    # erneuter manueller Trigger) darf für diesen Vertrag NICHTS mehr
    # versenden - der Slot für (Vertrag, 25.09.2026) ist bereits VERSENDET.
    ergebnis_lauf1_wiederholt = service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 25))
    assert ergebnis_lauf1_wiederholt["gesendet"] == 0

    with session_factory() as session:
        versendete_laeufe_gesamt = session.query(MahnLaufTable).filter(MahnLaufTable.status == "GESENDET").count()
        offene_stufe1_faelle = (
            session.query(MahnFallTable)
            .filter(MahnFallTable.stufe == 1, MahnFallTable.status == "GEPLANT")
            .count()
        )
    assert versendete_laeufe_gesamt == 1  # KEIN zweiter tatsächlicher Versand am selben Tag.
    assert offene_stufe1_faelle == 1  # Die neue Forderung bleibt GEPLANT, verschoben auf einen SPÄTEREN Lauf.


def test_freigegebener_slot_erlaubt_legitimen_retry_am_selben_tag(
    hv, basis_vertrag, admin_ctx, session_factory,
):
    """Ein Aufruf, bei dem NICHTS tatsächlich versandt wurde (hier: die
    Bankbestätigung fehlte noch), darf den Tageslauf-Slot NICHT dauerhaft
    schließen - ein SPÄTERER, legitimer Retry DESSELBEN Tages muss noch
    senden können, sobald die Blockade behoben ist."""

    service, state = hv
    contract, account = basis_vertrag
    # `_seed_debt` bestätigt die Bank standardmäßig nur bis 2026-09-13
    # (weit mehr als `bank_stand_max_age_days` vor dem 25.09.) - OHNE
    # eine erneute Bestätigung bleibt der Bankstand am 25.09. veraltet,
    # der Lauf blockiert vollständig (NICHTS wird tatsächlich versandt).
    _seed_debt(hv, contract, account, admin_ctx, vier_stufen=True, faelligkeit=date(2026, 9, 5))

    ergebnis_blockiert = service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 25))
    assert ergebnis_blockiert["gesendet"] == 0

    # Die Bank wird jetzt tatsächlich bestätigt - ein Retry DESSELBEN
    # Prüfdatums muss trotzdem noch senden können (Slot war freigegeben).
    state["sent"] = "2026-09-25T09:00:00Z"
    service.bank_service.bestaetige_bankvollstaendigkeit(bank_konto_id="SYNTHETIC-BANK", bestaetigt_bis=date(2026, 9, 25), bestaetigt_von="SYNTHETIC-TEST")
    ergebnis_retry = service.mahnlauf(ctx=admin_ctx, heute=date(2026, 9, 25))
    assert ergebnis_retry["gesendet"] == 1
