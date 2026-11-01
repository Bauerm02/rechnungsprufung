"""Tests für `mahnwesen/brief_rest.py` - rein synthetisch über
`httpx.MockTransport`, KEIN echter Netzwerkzugriff."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import ssl
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.domain.exceptions import TransportFehlerUngewissError
from mietinkasso.indexautomatik.mailnachweis import nachweis_daten
from mietinkasso.infrastructure.config import Settings
from mietinkasso.infrastructure.db.base import Base
from mietinkasso.infrastructure.db.session import build_engine
from mietinkasso.infrastructure.db.tables import AuditEventTable
from mietinkasso.mahnwesen import brief_rest
from mietinkasso.mahnwesen.brief_rest import (
    BriefRestVersandClaim, PostRestTransportadapter, RestKonfiguration, konfiguration_aus_settings, transport_auswahl,
)
from mietinkasso.mahnwesen.brief_sftp import MAX_SIMPLEX_SEITEN, PostSendevoraussetzungError
from tests.mietinkasso._brief_rest_test_helpers import STATUS, UPLOAD, VERARBEITEN, FakeEinfachBrief

_KEY = "SYNTHETIC-KEY-abc123"
_PROD = "https://www.einfach-brief.at/be"
_TEST = "https://abn-www.einfach-brief.at/be_t"
_SCHLUESSEL = "a" * 32
_OPENAPI = Path(__file__).resolve().parents[2] / "review-context"


def _pdf(seiten: int = 1) -> bytes:
    from fpdf import FPDF

    pdf = FPDF()
    for _ in range(seiten):
        pdf.add_page()
        pdf.set_font("Helvetica", size=12)
        pdf.cell(0, 10, "Synthetischer Testinhalt")
    return bytes(pdf.output())


def _konfiguration(**overrides) -> RestKonfiguration:
    werte = dict(basis_url=_TEST, registered_mail_type="Einschreiben", preisobergrenze=Decimal("10"), api_key=_KEY)
    werte.update(overrides)
    return RestKonfiguration(**werte)


def _adapter(fake: FakeEinfachBrief, **konfiguration) -> PostRestTransportadapter:
    return PostRestTransportadapter(_konfiguration(**konfiguration), transport_factory=fake.transport)


def _sende(adapter, pdf_bytes=None):
    pdf_bytes = pdf_bytes if pdf_bytes is not None else _pdf()
    return adapter.sende(
        pdf_bytes=pdf_bytes, pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(), sende_schluessel=_SCHLUESSEL,
    )


def _settings(tmp_path, **overrides) -> SimpleNamespace:
    key = tmp_path / "post-api-key"
    key.write_text(_KEY + "\n", encoding="utf-8")
    werte = dict(
        brief_transport="rest", brief_rest_basis_url=_TEST, brief_rest_api_key_datei=str(key),
        brief_rest_registered_mail_type="Einschreiben", brief_rest_preisobergrenze=Decimal("10"),
    )
    werte.update(overrides)
    return SimpleNamespace(**werte)


# -- Konfiguration: geschlossen per Default ------------------------------


def test_default_settings_lassen_sftp_ausgewaehlt_und_rest_geschlossen():
    settings = Settings(_env_file=None)
    assert transport_auswahl(settings) == "sftp"
    assert konfiguration_aus_settings(settings) is None


@pytest.mark.parametrize("basis_url", [_PROD, _TEST])
def test_vollstaendige_konfiguration_fuer_beide_dokumentierten_hosts(tmp_path, basis_url):
    konfiguration = konfiguration_aus_settings(_settings(tmp_path, brief_rest_basis_url=basis_url))
    assert konfiguration.basis_url == basis_url
    assert konfiguration.api_key == _KEY  # Zeilenumbruch abgeschnitten


@pytest.mark.parametrize("auswahl", ["REST", "rest ", "", None, "sftp+rest", "ebrief"])
def test_unbekannte_auswahl_waehlt_keinen_transport(tmp_path, auswahl):
    settings = _settings(tmp_path, brief_transport=auswahl)
    assert transport_auswahl(settings) is None
    assert konfiguration_aus_settings(settings) is None


def test_ausgewaehltes_sftp_ignoriert_vollstaendige_rest_werte(tmp_path):
    assert konfiguration_aus_settings(_settings(tmp_path, brief_transport="sftp")) is None


@pytest.mark.parametrize("feld,wert", [
    ("brief_rest_basis_url", None),
    ("brief_rest_basis_url", "http://www.einfach-brief.at/be"),
    ("brief_rest_basis_url", "https://www.einfach-brief.at/be/"),
    ("brief_rest_basis_url", "https://www.einfach-brief.at/be_t"),
    ("brief_rest_basis_url", "https://www.einfach-brief.at.evil.example/be"),
    ("brief_rest_basis_url", "https://www.einfach-brief.at:8443/be"),
    ("brief_rest_registered_mail_type", None),
    ("brief_rest_registered_mail_type", "NONE"),
    ("brief_rest_registered_mail_type", "E-Brief"),
    ("brief_rest_registered_mail_type", "einschreiben"),
    ("brief_rest_preisobergrenze", None),
    ("brief_rest_preisobergrenze", Decimal("0")),
    ("brief_rest_preisobergrenze", Decimal("-1")),
    ("brief_rest_preisobergrenze", Decimal("NaN")),
    ("brief_rest_preisobergrenze", Decimal("Infinity")),
    ("brief_rest_api_key_datei", None),
    ("brief_rest_api_key_datei", ""),
    ("brief_rest_api_key_datei", "/existiert/nicht"),
])
def test_unvollstaendige_oder_ungueltige_werte_bleiben_geschlossen(tmp_path, feld, wert):
    assert konfiguration_aus_settings(_settings(tmp_path, **{feld: wert})) is None


@pytest.mark.parametrize("inhalt", [b"", b"   \n", b"zwei teile", b"key\r\nX-Inject: 1", "schlüssel".encode(), b"x" * 5000])
def test_ungueltiger_schluesseldateiinhalt_bleibt_geschlossen(tmp_path, inhalt):
    key = tmp_path / "key"
    key.write_bytes(inhalt)
    assert konfiguration_aus_settings(_settings(tmp_path, brief_rest_api_key_datei=str(key))) is None


def test_klartext_schluessel_in_env_wird_nie_gelesen(tmp_path, monkeypatch):
    monkeypatch.setenv("MIETINKASSO_BRIEF_REST_API_KEY", _KEY)
    monkeypatch.setenv("MIETINKASSO_BRIEF_TRANSPORT", "rest")
    monkeypatch.setenv("MIETINKASSO_BRIEF_REST_BASIS_URL", _TEST)
    monkeypatch.setenv("MIETINKASSO_BRIEF_REST_REGISTERED_MAIL_TYPE", "Einschreiben")
    monkeypatch.setenv("MIETINKASSO_BRIEF_REST_PREISOBERGRENZE", "10")
    settings = Settings(_env_file=None)
    assert settings.brief_transport == "rest"
    assert not hasattr(settings, "brief_rest_api_key")
    assert _KEY not in repr(settings)
    assert konfiguration_aus_settings(settings) is None


@pytest.mark.parametrize("modus,privat", [
    (stat.S_IFREG | 0o600, True), (stat.S_IFREG | 0o400, True), (stat.S_IFREG | 0o440, True),
    (stat.S_IFREG | 0o640, True), (stat.S_IFREG | 0o644, False), (stat.S_IFREG | 0o604, False),
    (stat.S_IFREG | 0o660, False), (stat.S_IFREG | 0o666, False), (stat.S_IFLNK | 0o600, False),
    (stat.S_IFDIR | 0o700, False),
])
def test_dateirechtepruefung(modus, privat):
    assert brief_rest._dateirechte_privat(modus) is privat


def test_offene_schluesseldatei_wird_bei_aktiver_rechtepruefung_abgelehnt(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    Path(settings.brief_rest_api_key_datei).chmod(0o644)  # unter Windows meldet lstat ohnehin 0o666
    monkeypatch.setattr(brief_rest, "_POSIX_RECHTE_PRUEFEN", True)
    assert konfiguration_aus_settings(settings) is None


# -- Geheimnisschutz ------------------------------------------------------


def test_schluessel_steht_nicht_im_repr():
    konfiguration = _konfiguration()
    assert _KEY not in repr(konfiguration) and _KEY not in str(konfiguration)
    assert _KEY not in repr(PostRestTransportadapter(konfiguration, transport_factory=FakeEinfachBrief().transport))


def test_schluessel_geht_nur_im_dokumentierten_header_und_nie_in_fehler_oder_log(caplog):
    caplog.set_level(logging.DEBUG)
    fake = FakeEinfachBrief()
    fake.stoerung[UPLOAD] = lambda request: httpx.Response(401, text=f"bad {request.headers['x-api-key']}")
    with pytest.raises(TransportFehlerUngewissError) as fehler:
        _sende(_adapter(fake))
    assert fake.anfragen[0].headers["x-api-key"] == _KEY
    assert _KEY not in str(fake.anfragen[0].url) and _KEY.encode() not in fake.anfragen[0].content
    assert _KEY not in str(fehler.value) and _KEY not in repr(fehler.value)
    assert fehler.value.__cause__ is None
    assert _KEY not in caplog.text


# -- Positivfälle ---------------------------------------------------------


@pytest.mark.parametrize("basis_url", [_PROD, _TEST])
def test_ablauf_payload_und_ergebnis(basis_url):
    fake = FakeEinfachBrief()
    pdf_bytes = _pdf(seiten=2)
    ergebnis = _sende(_adapter(fake, basis_url=basis_url), pdf_bytes)

    assert [(a.method, str(a.url)) for a in fake.anfragen] == [
        ("POST", f"{basis_url}/sendAndPollStatus/v1/uploadToShoppingCart"),
        ("GET", f"{basis_url}/sendAndPollStatus/v1/getDeliveryStatus/CART-1"),
        ("POST", f"{basis_url}/sendAndPollStatus/v1/processDeliveriesFromShoppingCart"),
    ]
    assert fake.bodies(UPLOAD) == [{
        "pageColor": "nocolor", "sided": "single", "registeredMailType": "Einschreiben", "postalPriority": "PRIO",
        "shipmentType": "Einzelbrief", "printOnly": True, "skipAddressCheck": False, "includeAddressPage": False,
        "splitOn": 0,
        "documentPayloadList": [{
            "name": f"{_SCHLUESSEL}.pdf", "mimeType": "application/pdf",
            "payload": base64.b64encode(pdf_bytes).decode(), "foreignId": _SCHLUESSEL,
        }],
    }]
    assert fake.bodies(VERARBEITEN) == [{"deliveries": ["DELIVERY-1"]}]

    assert ergebnis.status == "UEBERMITTELT"
    assert ergebnis.externe_referenz == f"einfachbrief:{_SCHLUESSEL}:order:ORDER-1:delivery:DELIVERY-1"
    assert len(ergebnis.externe_referenz) <= 256
    # Eine API-Quittung ist nie ein Versandnachweis (siehe `brief_sftp`-Moduldoc).
    assert nachweis_daten(ergebnis) is None


@pytest.mark.parametrize("bestellt,rueckmeldung", [
    ("Einschreiben", "EINSCHREIBEN"), ("Einschreiben", "Einschreiben"),
    ("Einschreiben Einfach", "EINSCHREIBEN_EINFACH"), ("Einschreiben Einfach", "Einshreiben Einfach"),
])
def test_registered_mail_type_wird_uebernommen_und_rueckgeprueft(bestellt, rueckmeldung):
    fake = FakeEinfachBrief()
    fake.lieferung["registerMailType"] = rueckmeldung
    fake.dokument["registerMailType"] = "EINSCHREIBEN" if bestellt == "Einschreiben" else "EINSCHREIBEN_EINFACH"
    _sende(_adapter(fake, registered_mail_type=bestellt))
    assert fake.bodies(UPLOAD)[0]["registeredMailType"] == bestellt


def test_fremde_lieferung_im_warenkorb_bleibt_unberuehrt():
    fake = FakeEinfachBrief(fremde_lieferung=True)
    _sende(_adapter(fake))
    assert fake.bodies(VERARBEITEN) == [{"deliveries": ["DELIVERY-1"]}]


def test_kein_alter_oder_fremder_parameter_und_kein_warenkorb_bulk():
    fake = FakeEinfachBrief()
    _sende(_adapter(fake))
    koerper = fake.bodies(UPLOAD)[0]
    assert "registeredMail" not in koerper and "credential" not in koerper
    assert koerper["printOnly"] is True
    assert "receiver" not in koerper["documentPayloadList"][0]
    assert "removeAll" not in fake.bodies(VERARBEITEN)[0]
    assert fake.methoden() == [UPLOAD, STATUS, VERARBEITEN]


@pytest.mark.parametrize("datei", ["post-openapi-prod.json", "post-openapi-test.json"])
def test_nutzdaten_entsprechen_der_openapi(datei):
    pfad = _OPENAPI / datei
    if not pfad.exists():
        pytest.skip("Lokale Primärquelle review-context fehlt.")
    spec = json.loads(pfad.read_text(encoding="utf-8"))
    schemas = spec["components"]["schemas"]
    fake = FakeEinfachBrief()
    _sende(_adapter(fake))
    methoden = [(anfrage.method.lower(), anfrage.url.path.removeprefix("/be_t")) for anfrage in fake.anfragen]
    assert methoden == [
        ("post", "/sendAndPollStatus/v1/uploadToShoppingCart"),
        ("get", "/sendAndPollStatus/v1/getDeliveryStatus/CART-1"),
        ("post", "/sendAndPollStatus/v1/processDeliveriesFromShoppingCart"),
    ]
    assert "post" in spec["paths"]["/sendAndPollStatus/v1/uploadToShoppingCart"]
    assert "get" in spec["paths"]["/sendAndPollStatus/v1/getDeliveryStatus/{shoppingCartId}"]
    assert "post" in spec["paths"]["/sendAndPollStatus/v1/processDeliveriesFromShoppingCart"]
    eingabe = schemas["ForwardDeliveryInput"]["properties"]
    koerper = fake.bodies(UPLOAD)[0]
    assert set(koerper) <= set(eingabe)
    for feld in ("pageColor", "sided", "registeredMailType", "shipmentType", "postalPriority"):
        assert koerper[feld] in eingabe[feld]["enum"]
    assert set(koerper["documentPayloadList"][0]) <= set(schemas["DocumentPayload"]["properties"])
    assert set(fake.bodies(VERARBEITEN)[0]) <= set(schemas["DeliveryInputDTO"]["properties"])
    assert spec["components"]["securitySchemes"]["apiKeyAuth"]["name"] == "x-api-key"
    for gesendet in brief_rest._EINSCHREIBEN_RUECKMELDUNG:
        assert gesendet in eingabe["registeredMailType"]["enum"]
    for rueckmeldungen in brief_rest._EINSCHREIBEN_RUECKMELDUNG.values():
        assert rueckmeldungen <= set(schemas["DeliveryPayloadResult"]["properties"]["registerMailType"]["enum"])


# -- HTTP-Grenzen ---------------------------------------------------------


def test_standardtransport_prueft_tls_ohne_retries():
    adapter = PostRestTransportadapter(_konfiguration())
    transport = adapter._transport_factory()
    try:
        assert transport._pool._retries == 0
        assert transport._pool._ssl_context.verify_mode == ssl.CERT_REQUIRED
        assert transport._pool._ssl_context.check_hostname is True
    finally:
        transport.close()


def test_client_ohne_redirects_ohne_env_und_mit_endlichen_timeouts(monkeypatch):
    erfasst = []
    original = httpx.Client

    def _client(**kwargs):
        erfasst.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(brief_rest.httpx, "Client", _client)
    _sende(_adapter(FakeEinfachBrief()))
    assert len(erfasst) == 3
    for kwargs in erfasst:
        assert kwargs["follow_redirects"] is False and kwargs["trust_env"] is False
        timeout = kwargs["timeout"]
        assert all(wert and wert > 0 for wert in (timeout.connect, timeout.read, timeout.write, timeout.pool))


def test_redirect_wird_nicht_verfolgt():
    fake = FakeEinfachBrief()
    fake.stoerung[UPLOAD] = httpx.Response(302, headers={"Location": "https://evil.example/x"})
    with pytest.raises(TransportFehlerUngewissError):
        _sende(_adapter(fake))
    assert len(fake.anfragen) == 1


def test_gesamtfrist_bricht_haengenden_request_ab():
    fake = FakeEinfachBrief()
    freigabe = threading.Event()

    def _haengt(_request):
        freigabe.wait(timeout=5)
        return httpx.Response(200, json={})

    fake.stoerung[UPLOAD] = _haengt
    adapter = PostRestTransportadapter(_konfiguration(), transport_factory=fake.transport, gesamt_sekunden=0.1)
    try:
        with pytest.raises(TransportFehlerUngewissError, match="Zeitgrenze"):
            _sende(adapter)
    finally:
        freigabe.set()
    assert fake.methoden() == [UPLOAD]


def test_uebergrosse_antwort_ist_ungewiss(monkeypatch):
    monkeypatch.setattr(brief_rest, "_MAX_ANTWORT_BYTES", 10)
    with pytest.raises(TransportFehlerUngewissError):
        _sende(_adapter(FakeEinfachBrief()))


# -- Vorabprüfung: lokal, ohne Netz ---------------------------------------


@pytest.mark.parametrize("pdf_bytes,sha,schluessel", [
    (b"kein pdf", None, _SCHLUESSEL),
    (b"%PDF-1.4 abgeschnitten", None, _SCHLUESSEL),
    (None, "0" * 64, _SCHLUESSEL),
    (None, None, "../../etc/passwd"),
    (None, None, ""),
])
def test_vorabpruefung_lehnt_ohne_netzkontakt_ab(pdf_bytes, sha, schluessel):
    pdf_bytes = pdf_bytes if pdf_bytes is not None else _pdf()
    fake = FakeEinfachBrief()
    with pytest.raises(ValueError):
        _adapter(fake).sende(
            pdf_bytes=pdf_bytes, pdf_sha256=sha or hashlib.sha256(pdf_bytes).hexdigest(), sende_schluessel=schluessel,
        )
    assert fake.anfragen == []


def test_zu_viele_simplexseiten_ohne_netzkontakt():
    fake = FakeEinfachBrief()
    with pytest.raises(PostSendevoraussetzungError):
        _sende(_adapter(fake), _pdf(seiten=MAX_SIMPLEX_SEITEN + 1))
    assert fake.anfragen == []


# -- Negativfälle nach dem Request: nie Verarbeitung, immer ungewiss ------


@pytest.mark.parametrize("feld,wert", [
    ("success", False), ("success", None), ("finished", False), ("infected", True), ("infected", None),
    ("warningExist", True), ("warningExist", None), ("errorFileList", ["x.pdf"]), ("warningFileList", ["x.pdf"]),
    ("successFileList", ["fremd.pdf"]), ("shoppingCartId", None), ("shoppingCartId", ""),
    ("shoppingCartId", "../removeDeliveriesFromShoppingCart"), ("shoppingCartId", 123),
])
def test_upload_ohne_vorbehaltlose_bestaetigung_stoppt(feld, wert):
    fake = FakeEinfachBrief()
    fake.upload[feld] = wert
    with pytest.raises(TransportFehlerUngewissError):
        _sende(_adapter(fake))
    assert fake.methoden() == [UPLOAD]


@pytest.mark.parametrize("ziel,feld,wert", [
    ("lieferung", "valid", False), ("lieferung", "valid", None), ("lieferung", "hasWarning", True),
    ("lieferung", "ebrief", True), ("lieferung", "ebrief", None),
    ("lieferung", "registerMailType", "E-Brief"), ("lieferung", "registerMailType", "NONE"),
    ("lieferung", "registerMailType", "EINSCHREIBEN_EINFACH"),
    ("lieferung", "shipmentType", "Serienbrief"), ("lieferung", "shipmentType", "e-Gehaltszettel"),
    ("lieferung", "postalPriority", "ECO"), ("lieferung", "pageColor", "color"), ("lieferung", "sided", "double"),
    ("lieferung", "numberOfDocuments", 2), ("lieferung", "numberOfPages", 99), ("lieferung", "attachmentExist", True),
    ("lieferung", "paymentLocked", True), ("lieferung", "deliveryId", None), ("lieferung", "deliveryId", "a/b"),
    ("lieferung", "totalPrice", None), ("lieferung", "totalPrice", "4.8"), ("lieferung", "totalPrice", True),
    ("lieferung", "totalPrice", 0), ("lieferung", "totalPrice", 10.01),
    ("dokument", "valid", False), ("dokument", "numberOfPages", 99), ("dokument", "registerMailType", "NONE"),
    ("dokument", "error", [{"errorCode": "E"}]), ("dokument", "warning", [{"errorCode": "W"}]),
    ("dokument", "foreignId", "falscher-schluessel"), ("dokument", "foreignId", None),
    ("warenkorb", "shoppingCartId", "CART-FREMD"), ("warenkorb", "validityResult", None),
    ("warenkorb", "validityResult", []), ("warenkorb", "validityResult", "kaputt"),
])
def test_abweichende_lieferung_stoppt_vor_der_verarbeitung(ziel, feld, wert):
    fake = FakeEinfachBrief()
    getattr(fake, ziel)[feld] = wert
    with pytest.raises(TransportFehlerUngewissError):
        _sende(_adapter(fake))
    assert fake.methoden() == [UPLOAD, STATUS]


def test_preis_genau_an_der_obergrenze_ist_erlaubt():
    fake = FakeEinfachBrief(preis=10.0)
    assert _sende(_adapter(fake)).status == "UEBERMITTELT"


def test_doppelte_foreign_id_stoppt_statt_zu_raten():
    fake = FakeEinfachBrief()
    original = fake._status

    def _doppelt(request):
        daten = json.loads(original(request).content)
        daten["validityResult"].append({**daten["validityResult"][0], "deliveryId": "DELIVERY-2"})
        return httpx.Response(200, json=daten)

    fake.stoerung[STATUS] = _doppelt
    with pytest.raises(TransportFehlerUngewissError):
        _sende(_adapter(fake))
    assert fake.methoden() == [UPLOAD, STATUS]


def test_mehrere_dokumente_in_der_lieferung_stoppen():
    fake = FakeEinfachBrief()
    fake.lieferung["documentsResults"] = [
        {"foreignId": _SCHLUESSEL, "valid": True, "numberOfPages": 1, "registerMailType": "EINSCHREIBEN"},
        {"foreignId": "fremd", "valid": True, "numberOfPages": 1, "registerMailType": "EINSCHREIBEN"},
    ]
    with pytest.raises(TransportFehlerUngewissError):
        _sende(_adapter(fake))
    assert fake.methoden() == [UPLOAD, STATUS]


@pytest.mark.parametrize("feld,wert", [
    ("errorDescription", ["abgelehnt"]), ("orderId", None), ("orderId", ""), ("orderId", "a b"), ("orderId", 5),
])
def test_unklare_verarbeitungsantwort_ist_ungewiss_ohne_replay(feld, wert):
    fake = FakeEinfachBrief()
    fake.verarbeitung[feld] = wert
    with pytest.raises(TransportFehlerUngewissError):
        _sende(_adapter(fake))
    assert fake.methoden() == [UPLOAD, STATUS, VERARBEITEN]


@pytest.mark.parametrize("methode,erwartet", [
    (UPLOAD, [UPLOAD]), (STATUS, [UPLOAD, STATUS]), (VERARBEITEN, [UPLOAD, STATUS, VERARBEITEN]),
])
@pytest.mark.parametrize("stoerung", [
    httpx.ReadTimeout("synthetisch"), httpx.ConnectTimeout("synthetisch"), httpx.ConnectError("synthetisch"),
    httpx.RemoteProtocolError("synthetisch"), RuntimeError("synthetisch"),
    httpx.Response(500, text="Fehler"), httpx.Response(400, text="Fehler"), httpx.Response(204),
    httpx.Response(200, text="<html>kein json</html>"), httpx.Response(200, json=["liste"]),
    httpx.Response(200, json=None),
])
def test_jede_stoerung_ist_ungewiss_und_wird_nie_wiederholt(methode, erwartet, stoerung):
    fake = FakeEinfachBrief()
    if isinstance(stoerung, httpx.Response):
        vorlage = stoerung
        stoerung = lambda _request: httpx.Response(vorlage.status_code, content=vorlage.content)  # noqa: E731
    fake.stoerung[methode] = stoerung
    with pytest.raises(TransportFehlerUngewissError):
        _sende(_adapter(fake))
    assert fake.methoden() == erwartet


def test_fehlermeldung_enthaelt_weder_antwort_noch_pdf():
    fake = FakeEinfachBrief()
    fake.stoerung[UPLOAD] = httpx.Response(500, text="GEHEIME-ANTWORT Max Mustermieter")
    pdf_bytes = _pdf()
    with pytest.raises(TransportFehlerUngewissError) as fehler:
        _sende(_adapter(fake), pdf_bytes)
    assert "GEHEIME-ANTWORT" not in str(fehler.value)
    assert base64.b64encode(pdf_bytes).decode()[:40] not in str(fehler.value)


# -- Claim ----------------------------------------------------------------


def test_claim_gilt_genau_einmal_und_haelt_keine_personendaten(session_factory):
    claim = BriefRestVersandClaim(session_factory)
    assert claim.beanspruche(sende_schluessel=_SCHLUESSEL, pdf_sha256="f" * 64, erstellt_von="test") is True
    assert claim.beanspruche(sende_schluessel=_SCHLUESSEL, pdf_sha256="f" * 64, erstellt_von="test") is False
    assert claim.beanspruche(sende_schluessel="b" * 32, pdf_sha256="f" * 64, erstellt_von="test") is True
    with session_factory() as session:
        zeilen = list(session.execute(select(AuditEventTable).where(AuditEventTable.entity_id == _SCHLUESSEL)).scalars())
    assert len(zeilen) == 1 and zeilen[0].payload == {"pdf_sha256": "f" * 64}


def test_claim_ist_unter_datei_sqlite_auch_nebenlaeufig_exklusiv(tmp_path):
    engine = build_engine(f"sqlite+pysqlite:///{(tmp_path / 'claim.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, future=True, expire_on_commit=False, class_=Session)
    claim = BriefRestVersandClaim(factory)
    with ThreadPoolExecutor(max_workers=8) as pool:
        ergebnisse = list(pool.map(
            lambda _: claim.beanspruche(sende_schluessel=_SCHLUESSEL, pdf_sha256="f" * 64, erstellt_von="test"), range(8),
        ))
    assert ergebnisse.count(True) == 1
    engine.dispose()


def test_claim_lehnt_datenbank_ohne_atomaren_schutz_ab(session_factory, monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def _postgres(_factory):
        yield SimpleNamespace(get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")))

    monkeypatch.setattr(brief_rest, "schreibgesperrte_session", _postgres)
    with pytest.raises(ValueError):
        BriefRestVersandClaim(session_factory).beanspruche(sende_schluessel=_SCHLUESSEL, pdf_sha256="f" * 64, erstellt_von="test")
