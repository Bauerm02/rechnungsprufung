"""Genau EINE Instanz jeder gemeinsamen Backoffice-Abhängigkeit.

Konfiguration, Session-Factory, Repositories/Services, Session-Speicher,
Login-Rate-Limiter, Cookie-Name und das Umgebungs-Kennzeichen entstehen
hier - und NUR hier. Jede Route liest sie ausdrücklich über dieses Modul
(`deps._stammdaten_repo`, `deps._settings`, `deps._DEMO_UMGEBUNG`, ...),
nie über einen kopierten `from ... import _stammdaten_repo`. Das hat zwei
Gründe, die beide zum Verhalten gehören:

* Eine zweite Instanz eines Repositories/Services (oder ein zweiter
  `SessionStore`) würde den Zustand still aufspalten - Sitzungen,
  Rate-Limit-Fenster und Caches müssen prozessweit identisch sein.
* Ein zur Laufzeit gesetzter Wert (Tests setzen `_DEMO_UMGEBUNG`) muss an
  der Stelle wirken, an der die Route ihn liest. Ein kopierter Import
  wäre beim Import eingefroren.

Dieses Modul enthält bewusst KEINE Route, keine HTML-Erzeugung und keine
Fachlogik. Es bleibt eine Modulebenen-Initialisierung (Import erzeugt
DB-Engine und Servicegraph) - dieser Auftrag bündelt das an einer Stelle,
löst die Initialisierung beim Import aber NICHT auf."""

from __future__ import annotations

from mietinkasso.audit.service import AuditService
from mietinkasso.backoffice.security import LoginRateLimiter, SessionStore
from mietinkasso.backoffice.views import ist_bekannte_demo_umgebung
from mietinkasso.bank.repository import BankRepository
from mietinkasso.bank.service import BankImportService
from mietinkasso.index.repository import IndexRepository
from mietinkasso.index.service import IndexService
from mietinkasso.indexautomatik.bootstrap import bauen as _indexautomatik_bauen
from mietinkasso.indexautomatik.mailversand_service import HVMailversandService
from mietinkasso.infrastructure.config import get_settings
from mietinkasso.infrastructure.db.session import build_session_factory
from mietinkasso.mahnwesen.repository import (
    BriefAnbieterProfilRepository,
    MahnFallRepository,
    MahnKanalregelRepository,
    MahnPolicyRepository,
)
from mietinkasso.mahnwesen.service import MahnwesenService
from mietinkasso.mieweg_vorschau.repository import MieWegVorschauRepository
from mietinkasso.mieweg_vorschau.service import MieWegVorschauService
from mietinkasso.op.repository import OPRepository
from mietinkasso.op.service import OPService
from mietinkasso.stammdaten.repository import StammdatenRepository
from mietinkasso.variableabrechnung.bootstrap import bauen as _variableabrechnung_bauen
from mietinkasso.vertragspruefung.repository import IndexPruefbedarfRepository, VertragPruefungRepository
from mietinkasso.vertragspruefung.service import VertragspruefungService
from mietinkasso.vorschreibung.repository import VorschreibungRepository
from mietinkasso.vorschreibung.service import VorschreibungService


_settings = get_settings()


# Für Banner-/Titel-Anzeige (views.py::betriebsmodus_banner) UND für die
# Autozuordnungs-Routensperre unten - dieselbe Klassifizierung an BEIDEN
# Stellen (views.py::ist_bekannte_demo_umgebung), damit sie nie
# auseinanderlaufen können. Codex-Rückprüfung (Paket A): NUR bekannte
# Demo-Umgebungen (development/test/ci) gelten als sicher synthetisch -
# jede andere (production, staging, ein Zwischenschritt wie
# "local_realdata_staged") wird als Echtbetrieb behandelt, auch wenn sie
# nicht exakt "production" heißt.
_DEMO_UMGEBUNG = ist_bekannte_demo_umgebung(_settings.environment)


_session_factory = build_session_factory(_settings.database_url)


_stammdaten_repo = StammdatenRepository(_session_factory)


_op_repo = OPRepository(_session_factory)


_op_service = OPService(_op_repo, _stammdaten_repo)


_bank_repo = BankRepository(_session_factory)


_bank_service = BankImportService(_bank_repo, _stammdaten_repo, _op_service)


_vorschreibung_repo = VorschreibungRepository(_session_factory)


_vorschreibung_service = VorschreibungService(_vorschreibung_repo, _stammdaten_repo, _op_service)


_mahn_fall_repo = MahnFallRepository(_session_factory)


_mahn_policy_repo = MahnPolicyRepository(_session_factory)


# Dieselben Repositories/Default-Werte wie in `HVMailversandService`
# (siehe dortiger Kommentar) - EINE gemeinsame Datenbank, daher überall
# konsistent: Kanal bleibt EMAIL für beide Stufen ohne freigegebene
# Kanalregel, Briefkanal bleibt ohne echten Transport vollständig
# blockiert.
_mahn_kanalregel_repo = MahnKanalregelRepository(_session_factory)


_brief_anbieterprofil_repo = BriefAnbieterProfilRepository(_session_factory)


_mahn_service = MahnwesenService(
    _mahn_fall_repo, _stammdaten_repo, _op_service, _mahn_policy_repo, bank_stand_max_age_days=_settings.bank_stand_max_age_days,
    kanalregel_repository=_mahn_kanalregel_repo, brief_anbieterprofil_repository=_brief_anbieterprofil_repo,
    brief_transport_verfuegbar=False,
)


_index_repo = IndexRepository(_session_factory)


_index_service = IndexService(_index_repo, _stammdaten_repo)


_vertragspruefung_repo = VertragPruefungRepository(_session_factory)


_index_pruefbedarf_repo = IndexPruefbedarfRepository(_session_factory)


_vertragspruefung_service = VertragspruefungService(_vertragspruefung_repo, _index_pruefbedarf_repo, _stammdaten_repo)


_mieweg_vorschau_repo = MieWegVorschauRepository(_session_factory)


_mieweg_vorschau_service = MieWegVorschauService(_mieweg_vorschau_repo, _stammdaten_repo)


_audit_service = AuditService(_session_factory)


_indexautomatik = _indexautomatik_bauen(_session_factory, _settings)


_hv_mail = HVMailversandService(_session_factory, _indexautomatik, _settings)


_variableabrechnung = _variableabrechnung_bauen(_session_factory, _stammdaten_repo)


_sessions = SessionStore(ttl_sekunden=_settings.backoffice_session_ttl_minuten * 60)


# 5 Fehlversuche innerhalb von 5 Minuten -> 5 Minuten GLOBALE Sperre (siehe
# LoginRateLimiter-Docstring: kein Vertrauen in Proxy-Header, ein Operator).
_login_rate_limiter = LoginRateLimiter(max_versuche=5, fenster_sekunden=300, sperre_sekunden=300)


# `__Host-`-Präfix nur zulässig/sinnvoll mit Secure-Attribut (siehe unten
# `secure=_settings.backoffice_cookie_secure`), ohne Domain-Attribut (wird
# hier nie gesetzt) und mit Path=/ (Default von `Response.set_cookie`) -
# alle drei Bedingungen sind bereits erfüllt, sobald Cookies sicher sind.
# Das Präfix lässt den Browser das Cookie zusätzlich ablehnen, falls es
# jemals versucht würde, es unsicher (HTTP) oder mit einer abweichenden
# Domain zu setzen.
_COOKIE_NAME = "__Host-mietinkasso_biz_session" if _settings.backoffice_cookie_secure else "mietinkasso_biz_session"
