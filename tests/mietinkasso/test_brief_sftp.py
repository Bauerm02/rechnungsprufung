"""Tests für `mahnwesen/brief_sftp.py` - rein synthetisch, KEIN echter
Netzwerkzugriff (`verbindung_factory` wird in jedem Test durch ein Fake
ersetzt, siehe `indexautomatik/transport.py::FakeTransportadapter` für
denselben etablierten Grundsatz)."""

from __future__ import annotations

import pytest

from mietinkasso.domain.exceptions import TransportFehlerUngewissError
from mietinkasso.mahnwesen.brief_sftp import (
    MAX_SIMPLEX_SEITEN, BriefSftpErgebnis, PostSendevoraussetzungError, PostSftpDateiKonfliktError,
    PostSftpHostkeyFehlerError, PostSftpKonfigurationFehltError, PostSftpTransportadapter, SendeParameter,
    SftpKonfiguration, baue_dateiname, konfiguration_aus_settings, pruefe_sendevoraussetzungen,
)

_FINGERPRINT = "SHA256:echterTestFingerprintXYZ"


def _echtes_pdf(seiten: int = 1) -> bytes:
    """Ein tatsächlich gültiges, von `pypdf` parsebares PDF - die
    bloße `%PDF-`-Kopfzeile allein (wie in vielen anderen, rein
    strukturellen Tests dieses Repos üblich) reicht für die echte
    Seitenzahl-/Parsingprüfung in `pruefe_sendevoraussetzungen` nicht."""

    from fpdf import FPDF

    pdf = FPDF()
    for _ in range(seiten):
        pdf.add_page()
        pdf.set_font("Helvetica", size=12)
        pdf.cell(0, 10, "Synthetischer Testinhalt")
    return bytes(pdf.output())


def _konfiguration(**overrides) -> SftpKonfiguration:
    werte = dict(
        host="transfer.post-business-solutions.at", port=22, pfad="/bulk_tp/",
        username="jlb-test", password="synthetisches-test-passwort", host_key_fingerprint=_FINGERPRINT,
    )
    werte.update(overrides)
    return SftpKonfiguration(**werte)


class _FakeSettings:
    def __init__(self, **overrides):
        self.brief_sftp_host = overrides.get("brief_sftp_host")
        self.brief_sftp_port = overrides.get("brief_sftp_port", 22)
        self.brief_sftp_pfad = overrides.get("brief_sftp_pfad", "/bulk_tp/")
        self.brief_sftp_username = overrides.get("brief_sftp_username")
        self.brief_sftp_password = overrides.get("brief_sftp_password")
        self.brief_sftp_host_key_fingerprint = overrides.get("brief_sftp_host_key_fingerprint")


class _FakeVerbindung:
    """Synthetisches Test-Double - niemals echter Netzwerkzugriff."""

    def __init__(self, *, fingerprint: str = _FINGERPRINT, verhalten: str = "OK"):
        self._fingerprint = fingerprint
        self._verhalten = verhalten
        self.geschriebene_dateien: dict[str, bytes] = {}
        self.geschlossen = False

    def hostkey_fingerprint(self) -> str:
        return self._fingerprint

    def put_exclusive(self, remote_pfad: str, data: bytes) -> None:
        if self._verhalten == "TIMEOUT":
            raise ConnectionResetError("Fake-Verbindungsabbruch (Test).")
        if remote_pfad in self.geschriebene_dateien:
            raise FileExistsError(remote_pfad)
        self.geschriebene_dateien[remote_pfad] = data

    def close(self) -> None:
        self.geschlossen = True


# -- SendeParameter/Dateiname -------------------------------------------


def test_parameterblock_hat_die_offiziell_bestaetigten_ziffernpositionen():
    """1-basiert: Position 3 Farbmodus, 4 Duplex, 5 Tempo, 7 Produkt,
    9 E-Brief, alle übrigen Positionen fest 0 (Vorgabe Markus 19.10.2026,
    keine weitere Recherche nötig)."""

    block = SendeParameter().parameterblock()
    assert len(block) == 15
    assert block == "000000100000000"
    # Position 7 (Index 6) = Produkt (1 = Inland Einschreiben Einwurf).
    assert block[6] == "1"
    # alle übrigen Positionen (außer 3/4/5/7/9) sind fest "0".
    for index in (0, 1, 3, 5, 7, 9, 10, 11, 12, 13, 14):
        assert block[index] == "0"


def test_parameterblock_mit_abweichenden_werten():
    block = SendeParameter(farbmodus=1, duplex=1, tempo=1, produkt=2, ebrief=1).parameterblock()
    assert block == "001110201000000"  # Positionen 3,4,5,7,9 (Index 2,3,4,6,8)


def test_sendeparameter_lehnt_mehrstellige_werte_ab():
    with pytest.raises(ValueError):
        SendeParameter(produkt=10)


def test_baue_dateiname_ist_opaque_und_eindeutig_je_archiv_id():
    name_1 = baue_dateiname(archiv_id=1)
    name_2 = baue_dateiname(archiv_id=2)
    assert name_1 != name_2
    assert name_1.endswith(".pdf")
    assert name_1.startswith(SendeParameter().parameterblock() + "-")
    assert name_1 == "000000100000000-000000000001.pdf"


def test_baue_dateiname_lehnt_nicht_positive_id_ab():
    with pytest.raises(ValueError):
        baue_dateiname(archiv_id=0)
    with pytest.raises(ValueError):
        baue_dateiname(archiv_id=-1)


# -- Sendevoraussetzungen (PDF/A, max 14 Simplexseiten) -----------------


def test_pruefe_sendevoraussetzungen_lehnt_nicht_pdf_ab():
    with pytest.raises(PostSendevoraussetzungError):
        pruefe_sendevoraussetzungen(b"kein pdf", duplex=0)


def test_pruefe_sendevoraussetzungen_lehnt_zu_viele_simplexseiten_ab():
    pdf_bytes = _echtes_pdf(seiten=MAX_SIMPLEX_SEITEN + 1)
    with pytest.raises(PostSendevoraussetzungError):
        pruefe_sendevoraussetzungen(pdf_bytes, duplex=0)


def test_pruefe_sendevoraussetzungen_akzeptiert_grenzwert_bei_simplex():
    pdf_bytes = _echtes_pdf(seiten=MAX_SIMPLEX_SEITEN)
    pruefe_sendevoraussetzungen(pdf_bytes, duplex=0)  # darf NICHT werfen


def test_pruefe_sendevoraussetzungen_ignoriert_seitenlimit_bei_duplex():
    pdf_bytes = _echtes_pdf(seiten=MAX_SIMPLEX_SEITEN + 1)
    pruefe_sendevoraussetzungen(pdf_bytes, duplex=1)  # Simplex-Limit gilt nicht bei Duplex


# -- konfiguration_aus_settings: geschlossen per Default ----------------


def test_konfiguration_aus_settings_ist_ohne_vollstaendige_werte_geschlossen():
    assert konfiguration_aus_settings(_FakeSettings()) is None
    assert konfiguration_aus_settings(_FakeSettings(brief_sftp_host="h")) is None
    assert konfiguration_aus_settings(_FakeSettings(
        brief_sftp_host="h", brief_sftp_username="u", brief_sftp_password="p",
    )) is None  # Hostkey-Fingerprint fehlt


def test_konfiguration_aus_settings_liefert_konfiguration_bei_vollstaendigen_werten():
    konfiguration = konfiguration_aus_settings(_FakeSettings(
        brief_sftp_host="transfer.post-business-solutions.at", brief_sftp_username="u",
        brief_sftp_password="p", brief_sftp_host_key_fingerprint=_FINGERPRINT,
    ))
    assert konfiguration is not None
    assert konfiguration.host == "transfer.post-business-solutions.at"
    assert konfiguration.port == 22
    assert konfiguration.pfad == "/bulk_tp/"


# -- PostSftpTransportadapter --------------------------------------------


def test_adapter_ohne_konfiguration_macht_garantiert_keinen_netzzugriff():
    adapter = PostSftpTransportadapter(None, verbindung_factory=lambda k: (_ for _ in ()).throw(
        AssertionError("verbindung_factory darf ohne Konfiguration NIE aufgerufen werden")
    ))
    with pytest.raises(PostSftpKonfigurationFehltError):
        adapter.sende(pdf_bytes=b"%PDF-1.4 x", dateiname="x.pdf")


def test_adapter_erzwingt_hostkey_pinning_vor_jeder_authentifizierung():
    fake = _FakeVerbindung(fingerprint="SHA256:FALSCHER-FINGERPRINT")
    adapter = PostSftpTransportadapter(_konfiguration(), verbindung_factory=lambda k: fake)
    with pytest.raises(PostSftpHostkeyFehlerError):
        adapter.sende(pdf_bytes=_echtes_pdf(), dateiname="x.pdf")
    assert fake.geschriebene_dateien == {}  # keine Datei geschrieben vor dem Hostkey-Check
    assert fake.geschlossen  # Verbindung wird trotzdem sauber geschlossen


def test_adapter_erfolgreicher_versand_liefert_ehrliches_uebermittelt_ergebnis():
    fake = _FakeVerbindung()
    adapter = PostSftpTransportadapter(_konfiguration(), verbindung_factory=lambda k: fake)
    pdf_bytes = _echtes_pdf()
    ergebnis = adapter.sende(pdf_bytes=pdf_bytes, dateiname="DATEI-1.pdf")
    assert isinstance(ergebnis, BriefSftpErgebnis)
    assert ergebnis.status == "GESENDET"  # == an den SFTP-Server übermittelt, KEIN Zustellnachweis
    assert ergebnis.externe_referenz == "DATEI-1.pdf"
    assert "/bulk_tp/DATEI-1.pdf" in ergebnis.provider_referenz
    assert fake.geschriebene_dateien == {"/bulk_tp/DATEI-1.pdf": pdf_bytes}


def test_adapter_lehnt_ueberschreiben_als_konflikt_ab_remote_exklusives_anlegen():
    fake = _FakeVerbindung()
    adapter = PostSftpTransportadapter(_konfiguration(), verbindung_factory=lambda k: fake)
    erstes_pdf = _echtes_pdf()
    adapter.sende(pdf_bytes=erstes_pdf, dateiname="DATEI-2.pdf")
    with pytest.raises(PostSftpDateiKonfliktError):
        adapter.sende(pdf_bytes=_echtes_pdf(seiten=2), dateiname="DATEI-2.pdf")
    assert fake.geschriebene_dateien["/bulk_tp/DATEI-2.pdf"] == erstes_pdf  # unverändert


def test_adapter_verbindungsstoerung_ist_immer_ungewiss_kein_automatischer_retry():
    fake = _FakeVerbindung(verhalten="TIMEOUT")
    adapter = PostSftpTransportadapter(_konfiguration(), verbindung_factory=lambda k: fake)
    with pytest.raises(TransportFehlerUngewissError):
        adapter.sende(pdf_bytes=_echtes_pdf(), dateiname="DATEI-3.pdf")


def test_adapter_lehnt_zu_viele_simplexseiten_vor_jedem_verbindungsaufbau_ab():
    aufgerufen = []
    adapter = PostSftpTransportadapter(_konfiguration(), verbindung_factory=lambda k: aufgerufen.append(k) or _FakeVerbindung())
    with pytest.raises(PostSendevoraussetzungError):
        adapter.sende(pdf_bytes=b"kein pdf", dateiname="DATEI-4.pdf")
    assert aufgerufen == []  # kein Verbindungsaufbau für eine von vornherein ungültige Datei
