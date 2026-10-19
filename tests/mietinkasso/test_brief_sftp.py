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
    SftpKonfiguration, _ParamikoVerbindung, baue_dateiname, konfiguration_aus_settings, pruefe_sendevoraussetzungen,
)

_FINGERPRINT = "SHA256:echterTestFingerprintXYZ"
_SCHLUESSEL_1 = "11111111111111111111111111111111"
_SCHLUESSEL_2 = "22222222222222222222222222222222"


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
        host="transfer.post-business-solutions.at", port=22, pfad="in/bulk_tp/",
        username="jlb-test", password="synthetisches-test-passwort", host_key_fingerprint=_FINGERPRINT,
    )
    werte.update(overrides)
    return SftpKonfiguration(**werte)


class _FakeSettings:
    def __init__(self, **overrides):
        self.brief_sftp_host = overrides.get("brief_sftp_host")
        self.brief_sftp_port = overrides.get("brief_sftp_port", 22)
        self.brief_sftp_pfad = overrides.get("brief_sftp_pfad", "in/bulk_tp/")
        self.brief_sftp_username_datei = overrides.get("brief_sftp_username_datei")
        self.brief_sftp_password_datei = overrides.get("brief_sftp_password_datei")
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
    block = SendeParameter(farbmodus=1, duplex=1, tempo=1, produkt=2, ebrief=0).parameterblock()
    assert block == "001110200000000"  # Positionen 3,4,5,7,9 (Index 2,3,4,6,8)


def test_sendeparameter_lehnt_mehrstellige_werte_ab():
    with pytest.raises(ValueError):
        SendeParameter(produkt=10)


def test_baue_dateiname_ist_opaque_und_eindeutig_je_sende_schluessel():
    name_1 = baue_dateiname(sende_schluessel=_SCHLUESSEL_1)
    name_2 = baue_dateiname(sende_schluessel=_SCHLUESSEL_2)
    assert name_1 != name_2
    assert name_1.endswith(".pdf")
    assert name_1.startswith(SendeParameter().parameterblock() + "-")
    assert name_1 == f"000000100000000-{_SCHLUESSEL_1}.pdf"


def test_baue_dateiname_lehnt_unsicheres_format_ab():
    with pytest.raises(ValueError):
        baue_dateiname(sende_schluessel="")
    with pytest.raises(ValueError):
        baue_dateiname(sende_schluessel="../../etc/passwd")
    with pytest.raises(ValueError):
        baue_dateiname(sende_schluessel="hat ein leerzeichen")


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


def test_konfiguration_aus_settings_ist_ohne_vollstaendige_werte_geschlossen(tmp_path):
    passwort_datei = tmp_path / "sftp-passwort.txt"
    passwort_datei.write_text("synthetisches-test-passwort", encoding="utf-8")
    username_datei = tmp_path / "sftp-username.txt"
    username_datei.write_text("jlb-test", encoding="utf-8")

    assert konfiguration_aus_settings(_FakeSettings()) is None
    assert konfiguration_aus_settings(_FakeSettings(brief_sftp_host="h")) is None
    assert konfiguration_aus_settings(_FakeSettings(
        brief_sftp_host="h", brief_sftp_username_datei=str(username_datei),
        brief_sftp_password_datei=str(passwort_datei),
    )) is None  # Hostkey-Fingerprint fehlt
    assert konfiguration_aus_settings(_FakeSettings(
        brief_sftp_host="h", brief_sftp_username_datei=str(username_datei),
        brief_sftp_host_key_fingerprint=_FINGERPRINT,
        brief_sftp_password_datei=str(tmp_path / "existiert-nicht.txt"),
    )) is None  # Passwortdatei nicht lesbar
    assert konfiguration_aus_settings(_FakeSettings(
        brief_sftp_host="h", brief_sftp_username_datei=str(tmp_path / "existiert-nicht.txt"),
        brief_sftp_host_key_fingerprint=_FINGERPRINT, brief_sftp_password_datei=str(passwort_datei),
    )) is None  # Usernamedatei nicht lesbar


def test_konfiguration_aus_settings_liefert_konfiguration_bei_vollstaendigen_werten(tmp_path):
    passwort_datei = tmp_path / "sftp-passwort.txt"
    passwort_datei.write_text("synthetisches-test-passwort\n", encoding="utf-8")
    username_datei = tmp_path / "sftp-username.txt"
    username_datei.write_text("jlb-test\n", encoding="utf-8")

    konfiguration = konfiguration_aus_settings(_FakeSettings(
        brief_sftp_host="transfer.post-business-solutions.at", brief_sftp_username_datei=str(username_datei),
        brief_sftp_password_datei=str(passwort_datei), brief_sftp_host_key_fingerprint=_FINGERPRINT,
    ))
    assert konfiguration is not None
    assert konfiguration.host == "transfer.post-business-solutions.at"
    assert konfiguration.port == 22
    assert konfiguration.pfad == "in/bulk_tp/"
    assert konfiguration.username == "jlb-test"  # Zeilenumbruch abgeschnitten
    assert konfiguration.password == "synthetisches-test-passwort"  # Zeilenumbruch abgeschnitten


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
    assert ergebnis.status == "UEBERMITTELT"  # NICHT "GESENDET" - struktureller Schutz, siehe Moduldoc
    assert ergebnis.externe_referenz == "DATEI-1.pdf"
    assert "in/bulk_tp/DATEI-1.pdf" in ergebnis.provider_referenz
    assert fake.geschriebene_dateien == {"in/bulk_tp/DATEI-1.pdf": pdf_bytes}


def test_adapter_lehnt_ueberschreiben_als_konflikt_ab_remote_exklusives_anlegen():
    fake = _FakeVerbindung()
    adapter = PostSftpTransportadapter(_konfiguration(), verbindung_factory=lambda k: fake)
    erstes_pdf = _echtes_pdf()
    adapter.sende(pdf_bytes=erstes_pdf, dateiname="DATEI-2.pdf")
    with pytest.raises(PostSftpDateiKonfliktError):
        adapter.sende(pdf_bytes=_echtes_pdf(seiten=2), dateiname="DATEI-2.pdf")
    assert fake.geschriebene_dateien["in/bulk_tp/DATEI-2.pdf"] == erstes_pdf  # unverändert


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


def test_adapter_fehlschlagender_verbindungsaufbau_ist_ebenfalls_ungewiss():
    """Codex-Abnahme 19.10.2026: ein Fehler bereits BEIM Verbindungsaufbau
    (die `verbindung_factory` selbst) muss genauso wie ein späterer
    Schreibfehler als ungewiss behandelt werden, nicht unkategorisiert
    durchreichen."""

    def _fehlschlagende_factory(_konfiguration):
        raise OSError("Fake-Verbindungsaufbau fehlgeschlagen (Test).")

    adapter = PostSftpTransportadapter(_konfiguration(), verbindung_factory=_fehlschlagende_factory)
    with pytest.raises(TransportFehlerUngewissError):
        adapter.sende(pdf_bytes=_echtes_pdf(), dateiname="DATEI-5.pdf")


def test_sendeparameter_erzwingt_ebrief_null():
    SendeParameter(ebrief=0)  # erlaubt
    with pytest.raises(ValueError):
        SendeParameter(ebrief=1)


# -- _ParamikoVerbindung: echte Open-Flags/Verbindungsaufbau, nur mit ---
# -- injiziertem Fake-Transport/SFTPClient, KEIN echtes Netz ------------


class _FakeSftpFile:
    def __init__(self):
        self.geschrieben = b""

    def write(self, data: bytes) -> None:
        self.geschrieben += data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeKanal:
    def __init__(self):
        self.timeout_aufrufe: list[float] = []

    def settimeout(self, sekunden: float) -> None:
        self.timeout_aufrufe.append(sekunden)


class _FakeSftpClient:
    def __init__(self):
        self.open_aufrufe: list[tuple[str, str]] = []
        self.geschriebene_dateien: dict[str, bytes] = {}
        self._datei = _FakeSftpFile()
        self.kanal = _FakeKanal()

    def open(self, remote_pfad: str, modus: str):
        self.open_aufrufe.append((remote_pfad, modus))
        self.geschriebene_dateien[remote_pfad] = b""
        return self._datei

    def get_channel(self):
        return self.kanal

    def close(self) -> None:
        pass


class _FakeTransport:
    def __init__(self, fingerprint_bytes: bytes = b"fake-server-key-bytes"):
        self._fingerprint_bytes = fingerprint_bytes
        self.auth_aufrufe: list[tuple[str, str]] = []
        self.geschlossen = False

    def get_remote_server_key(self):
        class _Key:
            def __init__(self, data):
                self._data = data

            def asbytes(self):
                return self._data

        return _Key(self._fingerprint_bytes)

    def auth_password(self, username: str, password: str) -> None:
        self.auth_aufrufe.append((username, password))

    def close(self) -> None:
        self.geschlossen = True


def test_paramiko_verbindung_oeffnet_exklusiv_und_schreibend_wxb():
    """Codex-Abnahme 19.10.2026: `"xb"` allein setzt in paramikos
    `SFTPClient.open`-Modusübersetzung KEIN Schreib-Flag - die Datei
    muss exklusiv UND schreibend geöffnet werden (`"wxb"`)."""

    fake_transport = _FakeTransport()
    fake_sftp_client = _FakeSftpClient()

    class _FakeSftpClientCls:
        @staticmethod
        def from_transport(transport):
            assert transport is fake_transport
            return fake_sftp_client

    konfiguration = _konfiguration()
    verbindung = _ParamikoVerbindung(
        konfiguration, transport_factory=lambda: fake_transport, sftp_client_cls=_FakeSftpClientCls,
    )
    verbindung.put_exclusive("in/bulk_tp/DATEI-1.pdf", b"%PDF-1.4 Inhalt")

    assert fake_sftp_client.open_aufrufe == [("in/bulk_tp/DATEI-1.pdf", "wxb")]
    assert fake_sftp_client._datei.geschrieben == b"%PDF-1.4 Inhalt"
    assert fake_transport.auth_aufrufe == [(konfiguration.username, konfiguration.password)]
    # Der tatsächliche SFTP-Kanal selbst wird ebenfalls begrenzt (nicht nur
    # Verbindungsaufbau/Authentifizierung) - Codex-Abnahme 19.10.2026.
    from mietinkasso.mahnwesen.brief_sftp import _VERBINDUNGSTIMEOUT_SEKUNDEN
    assert fake_sftp_client.kanal.timeout_aufrufe == [_VERBINDUNGSTIMEOUT_SEKUNDEN]


def test_paramiko_verbindung_setzt_banner_und_auth_timeout_bei_echtem_verbindungsaufbau(monkeypatch):
    """Codex-Abnahme 19.10.2026: `_ParamikoVerbindung` begrenzt beim
    ECHTEN Verbindungsaufbau (kein `transport_factory` injiziert) sowohl
    `banner_timeout` als auch `auth_timeout` - nicht nur den Socket-
    Connect. Ersetzt `socket.create_connection`/`paramiko.Transport`
    durch Fakes, damit kein echter Netzwerkzugriff stattfindet."""

    import paramiko

    from mietinkasso.mahnwesen import brief_sftp as brief_sftp_modul

    fake_transport = _FakeTransport()
    aufgerufene_adressen = []

    def _fake_create_connection(adresse, timeout):
        aufgerufene_adressen.append((adresse, timeout))
        return object()

    def _fake_transport_cls(sock):
        return fake_transport

    monkeypatch.setattr(brief_sftp_modul.socket, "create_connection", _fake_create_connection)
    monkeypatch.setattr(paramiko, "Transport", _fake_transport_cls)
    fake_transport.start_client = lambda timeout: None

    verbindung = _ParamikoVerbindung(_konfiguration())
    assert aufgerufene_adressen == [((_konfiguration().host, _konfiguration().port), 20)]
    assert fake_transport.banner_timeout == 20
    assert fake_transport.auth_timeout == 20


def test_paramiko_verbindung_schliesst_transport_bei_fehlschlagendem_start_client(monkeypatch):
    """Codex-Abnahme 19.10.2026: scheitert `start_client` beim ECHTEN
    Verbindungsaufbau, bleibt KEINE offene Transport-/Socket-Ressource
    zurück - `close()` wird auch dann aufgerufen. Kein echter
    Netzwerkzugriff (Socket-Connect/`paramiko.Transport` gefakt)."""

    import paramiko

    from mietinkasso.mahnwesen import brief_sftp as brief_sftp_modul

    class _FehlschlagenderTransport(_FakeTransport):
        def start_client(self, timeout):
            raise RuntimeError("Fake-Kanalverhandlung fehlgeschlagen (Test).")

    fake_transport = _FehlschlagenderTransport()
    monkeypatch.setattr(brief_sftp_modul.socket, "create_connection", lambda adresse, timeout: object())
    monkeypatch.setattr(paramiko, "Transport", lambda sock: fake_transport)

    with pytest.raises(RuntimeError):
        _ParamikoVerbindung(_konfiguration())
    assert fake_transport.geschlossen


def test_paramiko_verbindung_hostkey_fingerprint_ist_sha256_base64():
    fake_transport = _FakeTransport(fingerprint_bytes=b"irgendein-test-schluessel")
    verbindung = _ParamikoVerbindung(
        _konfiguration(), transport_factory=lambda: fake_transport, sftp_client_cls=_FakeSftpClient,
    )
    fingerprint = verbindung.hostkey_fingerprint()
    assert fingerprint.startswith("SHA256:")

    import base64
    import hashlib

    erwartet = "SHA256:" + base64.b64encode(hashlib.sha256(b"irgendein-test-schluessel").digest()).decode().rstrip("=")
    assert fingerprint == erwartet


def test_paramiko_verbindung_close_schliesst_transport():
    fake_transport = _FakeTransport()
    fake_sftp_client = _FakeSftpClient()

    class _FakeSftpClientCls:
        @staticmethod
        def from_transport(transport):
            return fake_sftp_client

    verbindung = _ParamikoVerbindung(
        _konfiguration(), transport_factory=lambda: fake_transport, sftp_client_cls=_FakeSftpClientCls,
    )
    verbindung.put_exclusive("in/bulk_tp/DATEI-1.pdf", b"%PDF-1.4 x")
    verbindung.close()
    assert fake_transport.geschlossen
