"""Tests für die Erweiterung des Mahnwesens auf vier Stufen (Auftrag
HV-20261020-VIER-MAHNSTUFEN) - End-to-End gegen den ECHTEN
`MahnwesenService` (reale DB-Session, keine gemockten Repositories),
analog zu `test_mahnwesen.py`, aber mit `vier_stufen_profil_aktiv=True`
und dem neuen zugangsnachweis-basierten Fristenlauf."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from mietinkasso.domain.exceptions import MahnstufeReihenfolgeError, QuellenbelegFehltError
from mietinkasso.domain.enums import OPTyp
from mietinkasso.indexautomatik.mailops_client import MailOpsErgebnis
from mietinkasso.mahnwesen.repository import (
    MahnEmpfaengerZugangRepository,
    MahnFallRepository,
    MahnLaufRepository,
    MahnPolicyRepository,
    VertragMitmieterRepository,
)
from mietinkasso.mahnwesen.service import MahnwesenService


def _test_receipt(heute):
    return MailOpsErgebnis(
        "GESENDET", "SYNTHETIC-REF", "SYNTHETIC-SENT", datetime.combine(heute, datetime.min.time(), tzinfo=timezone.utc),
    )


@pytest.fixture
def mahn_policy_repo(session_factory) -> MahnPolicyRepository:
    return MahnPolicyRepository(session_factory)


@pytest.fixture
def mahn_fall_repo(session_factory) -> MahnFallRepository:
    return MahnFallRepository(session_factory)


@pytest.fixture
def mahnlauf_repo(session_factory) -> MahnLaufRepository:
    return MahnLaufRepository(session_factory)


@pytest.fixture
def empfaenger_zugang_repo(session_factory) -> MahnEmpfaengerZugangRepository:
    return MahnEmpfaengerZugangRepository(session_factory)


@pytest.fixture
def mitmieter_repo(session_factory) -> VertragMitmieterRepository:
    return VertragMitmieterRepository(session_factory)


@pytest.fixture
def mahn_service(mahn_fall_repo, stammdaten_repo, op_service, mahn_policy_repo, mahnlauf_repo, empfaenger_zugang_repo, mitmieter_repo) -> MahnwesenService:
    return MahnwesenService(
        mahn_fall_repo, stammdaten_repo, op_service, mahn_policy_repo, bank_stand_max_age_days=2,
        mahnlauf_repository=mahnlauf_repo, empfaenger_zugang_repository=empfaenger_zugang_repo,
        mitmieter_repository=mitmieter_repo,
    )


@pytest.fixture
def freigegebene_vier_stufen_policy(mahn_policy_repo):
    policy = mahn_policy_repo.anlegen(
        stufe1_tage_nach_faelligkeit=7,
        stufe2_mindesttage_nach_stufe1_versand=14,
        zinsen_prozent=Decimal("0"), gebuehr_cent=0, status="ENTWURF",
        vier_stufen_profil_aktiv=True,
        stufe2_mindesttage_nach_stufe1_zugang=14,
        stufe3_mindesttage_nach_stufe2_zugang=14,
        stufe4_mindesttage_nach_stufe3_zugang=14,
    )
    return mahn_policy_repo.freigeben(policy.id)


def _mit_faelligem_soll(op_service, konto, ctx, betrag_cent=60_000, faelligkeit=date(2026, 4, 5)):
    op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=betrag_cent,
        belegdatum=date(2026, 4, 1), buchungsdatum=date(2026, 4, 1), faelligkeit=faelligkeit, beleg_referenz="Miete April",
    )


def _einzige_forderung(op_service, konto, heute):
    forderungen = op_service.offene_forderungen(konto.id, heute=heute)
    assert len(forderungen) == 1
    return forderungen[0]


def _plane_und_versende_stufe(mahn_service, *, ctx, vertrag, konto, policy, heute, stufe):
    """Reproduziert den ECHTEN, gebündelten Produktionspfad (`plane_
    alle_offenen_forderungen` + `plane_mahnlauf` + `versende_mahnlauf`,
    siehe `indexautomatik/mailversand_service.py::HVMailversandService.
    mahnlauf`) - NICHT den einfachen Einzelfall-`versenden()`, der unter
    dem Vier-Stufen-Profil mangels `MahnLaufTable`-Zugangsnachweis nie
    über Stufe 1 hinaus eskalieren kann (siehe `_pruefe_vorstufe_
    abstand`-Docstring)."""

    heute_dt = heute - timedelta(days=0)
    geplant = mahn_service.plane_alle_offenen_forderungen(
        ctx=ctx, vertrag=vertrag, konto=konto, policy=policy, heute=heute, bank_bestaetigt_bis=heute,
    )
    assert any(p.status == "GEPLANT" and p.stufe == stufe for p in geplant), geplant
    mahnlauf = mahn_service.plane_mahnlauf(
        ctx=ctx, vertrag=vertrag, konto=konto, stufe=stufe, heute=heute, bank_bestaetigt_bis=heute,
    )
    assert mahnlauf is not None
    ergebnis = mahn_service.versende_mahnlauf(
        ctx=ctx, mahnlauf_id=mahnlauf.id, heute=heute, bank_bestaetigt_bis=heute, ungeklaerte_eingaenge_vorhanden=False,
        send_enabled=True, versand_fn=lambda mitglieder: _test_receipt(heute),
    )
    assert ergebnis.status == "GESENDET", ergebnis
    return mahn_service._mahnlauf_repository.get(mahnlauf.id)


def test_vier_stufen_literalbeispiel_zugang_10_oktober_keine_eskalation_25_oktober(
    mahn_service, op_service, stammdaten_repo, basis_vertrag, ctx_factory, freigegebene_vier_stufen_policy, empfaenger_zugang_repo,
):
    """Wörtliches Grenzfall-Beispiel aus dem Auftrag: Zugang der Stufe-1-
    Mahnung am 10.10.2026 - am 25.10.2026 darf NOCH NICHT auf Stufe 2
    eskaliert werden (Fristende ist erst der 27.10.2026, siehe
    `fristen.py`). §903 ABGB lässt die Frist erst mit ABLAUF des
    Fristendetages enden - auch am 27.10.2026 SELBST noch keine
    Eskalation, erst ab 28.10.2026."""

    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    _mit_faelligem_soll(op_service, konto, ctx, faelligkeit=date(2026, 9, 1))

    heute_stufe1 = date(2026, 9, 10)
    mahnlauf_1 = _plane_und_versende_stufe(mahn_service, ctx=ctx, vertrag=vertrag, konto=konto, policy=freigegebene_vier_stufen_policy, heute=heute_stufe1, stufe=1)

    # OHNE bestätigten Zugang bleibt Stufe 2 strukturell "noch zu früh",
    # unabhängig davon, wie viel Zeit seit dem bloßen VERSAND vergangen ist.
    geplant_ohne_zugang = mahn_service.plane_alle_offenen_forderungen(
        ctx=ctx, vertrag=vertrag, konto=konto, policy=freigegebene_vier_stufen_policy, heute=date(2026, 10, 25),
        bank_bestaetigt_bis=date(2026, 10, 25),
    )
    assert all(p.status != "GEPLANT" or p.stufe != 2 for p in geplant_ohne_zugang)

    # Zugang wird am 10.10.2026 bestätigt (RLS-Beleg).
    eintraege = empfaenger_zugang_repo.fuer_mahnlauf(mahnlauf_1.id)
    assert len(eintraege) == 1  # nur der Hauptmieter, keine Mitmieter erfasst
    aktualisiert = mahn_service.zugang_bestaetigen_fuer_empfaenger(
        ctx=ctx, mahnlauf_id=mahnlauf_1.id, mitmieter_id=None, heute=date(2026, 10, 11),
        zugang_datum=date(2026, 10, 10), zugangsform="EINSCHREIBEN_RUECKSCHEIN", zugang_beleg="RUECKSCHEIN-1",
        akteur="codex-test",
    )
    assert aktualisiert.zugang_bestaetigt_am == date(2026, 10, 10)

    # 25.10.2026: Fristende ist erst der 27.10.2026 (Samstag->Sonntag->
    # Nationalfeiertag) - KEINE Eskalation.
    geplant_25 = mahn_service.plane_alle_offenen_forderungen(
        ctx=ctx, vertrag=vertrag, konto=konto, policy=freigegebene_vier_stufen_policy, heute=date(2026, 10, 25),
        bank_bestaetigt_bis=date(2026, 10, 25),
    )
    assert all(p.status != "GEPLANT" or p.stufe != 2 for p in geplant_25), geplant_25

    # 27.10.2026 (der Fristendetag SELBST): §903 ABGB lässt die Frist
    # erst mit dessen ABLAUF enden - auch an diesem Tag selbst NOCH KEINE
    # Eskalation (Korrektur nach unabhängiger Abnahme, Codex 20.10.2026 -
    # siehe `fristen.py::frist_abgelaufen`).
    geplant_27 = mahn_service.plane_alle_offenen_forderungen(
        ctx=ctx, vertrag=vertrag, konto=konto, policy=freigegebene_vier_stufen_policy, heute=date(2026, 10, 27),
        bank_bestaetigt_bis=date(2026, 10, 27),
    )
    assert all(p.status != "GEPLANT" or p.stufe != 2 for p in geplant_27), geplant_27

    # 28.10.2026: der Fristendetag ist abgelaufen - Stufe 2 wird geplant.
    geplant_28 = mahn_service.plane_alle_offenen_forderungen(
        ctx=ctx, vertrag=vertrag, konto=konto, policy=freigegebene_vier_stufen_policy, heute=date(2026, 10, 28),
        bank_bestaetigt_bis=date(2026, 10, 28),
    )
    assert any(p.status == "GEPLANT" and p.stufe == 2 for p in geplant_28), geplant_28


def test_zugangsbestaetigung_mit_unzureichender_zugangsform_abgelehnt(mahn_service, op_service, basis_vertrag, ctx_factory, freigegebene_vier_stufen_policy):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    _mit_faelligem_soll(op_service, konto, ctx, faelligkeit=date(2026, 9, 1))
    mahnlauf = _plane_und_versende_stufe(mahn_service, ctx=ctx, vertrag=vertrag, konto=konto, policy=freigegebene_vier_stufen_policy, heute=date(2026, 9, 10), stufe=1)

    with pytest.raises(ValueError, match="gilt NICHT automatisch"):
        mahn_service.zugang_bestaetigen_fuer_empfaenger(
            ctx=ctx, mahnlauf_id=mahnlauf.id, mitmieter_id=None, heute=date(2026, 9, 11),
            zugang_datum=date(2026, 9, 10), zugangsform="EMAIL_UNBESTAETIGT", zugang_beleg="MAIL-LOG-1", akteur="codex-test",
        )


def test_zugangsbestaetigung_ohne_beleg_wird_abgelehnt(mahn_service, op_service, basis_vertrag, ctx_factory, freigegebene_vier_stufen_policy):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    _mit_faelligem_soll(op_service, konto, ctx, faelligkeit=date(2026, 9, 1))
    mahnlauf = _plane_und_versende_stufe(mahn_service, ctx=ctx, vertrag=vertrag, konto=konto, policy=freigegebene_vier_stufen_policy, heute=date(2026, 9, 10), stufe=1)

    with pytest.raises(QuellenbelegFehltError):
        mahn_service.zugang_bestaetigen_fuer_empfaenger(
            ctx=ctx, mahnlauf_id=mahnlauf.id, mitmieter_id=None, heute=date(2026, 9, 11),
            zugang_datum=date(2026, 9, 10), zugangsform="EINSCHREIBEN_RUECKSCHEIN", zugang_beleg="", akteur="codex-test",
        )


def test_zugang_in_der_zukunft_wird_abgelehnt(mahn_service, op_service, basis_vertrag, ctx_factory, freigegebene_vier_stufen_policy):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    _mit_faelligem_soll(op_service, konto, ctx, faelligkeit=date(2026, 9, 1))
    mahnlauf = _plane_und_versende_stufe(mahn_service, ctx=ctx, vertrag=vertrag, konto=konto, policy=freigegebene_vier_stufen_policy, heute=date(2026, 9, 10), stufe=1)

    with pytest.raises(ValueError, match="Zukunft"):
        mahn_service.zugang_bestaetigen_fuer_empfaenger(
            ctx=ctx, mahnlauf_id=mahnlauf.id, mitmieter_id=None, heute=date(2026, 9, 11),
            zugang_datum=date(2026, 9, 30), zugangsform="EINSCHREIBEN_RUECKSCHEIN", zugang_beleg="X", akteur="codex-test",
        )


def test_mitmieter_muss_ebenfalls_zugang_bestaetigen_bevor_eskaliert_wird(
    mahn_service, op_service, stammdaten_repo, mitmieter_repo, empfaenger_zugang_repo, basis_vertrag, ctx_factory, freigegebene_vier_stufen_policy,
):
    """Nutzervorgabe wörtlich: 'Mahnung gegen jeden Mitmieter; Nachweis
    Zugang aller'. Solange NICHT jeder erwartete Empfänger (Hauptmieter
    UND Mitmieter) einen bestätigten Zugang hat, bleibt die nächste
    Stufe gesperrt."""

    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    mitmieter_repo.anlegen(vertrag_id=vertrag.id, name="Erika Mustermieter", email="erika@example.at", adresse=None, erstellt_von="codex-test")
    _mit_faelligem_soll(op_service, konto, ctx, faelligkeit=date(2026, 9, 1))
    mahnlauf = _plane_und_versende_stufe(mahn_service, ctx=ctx, vertrag=vertrag, konto=konto, policy=freigegebene_vier_stufen_policy, heute=date(2026, 9, 10), stufe=1)

    eintraege = empfaenger_zugang_repo.fuer_mahnlauf(mahnlauf.id)
    assert len(eintraege) == 2  # Hauptmieter + Erika
    mitmieter_eintrag = next(e for e in eintraege if e.mitmieter_id is not None)

    # Nur der Hauptmieter bestätigt - Erikas Zugang fehlt noch.
    mahn_service.zugang_bestaetigen_fuer_empfaenger(
        ctx=ctx, mahnlauf_id=mahnlauf.id, mitmieter_id=None, heute=date(2026, 9, 11),
        zugang_datum=date(2026, 9, 10), zugangsform="EINSCHREIBEN_RUECKSCHEIN", zugang_beleg="RS-HAUPT", akteur="codex-test",
    )
    noch_nicht_vollstaendig = mahn_service._mahnlauf_repository.get(mahnlauf.id)
    assert noch_nicht_vollstaendig.zugang_bestaetigt_am is None  # Erika fehlt noch

    geplant_ohne_erika = mahn_service.plane_alle_offenen_forderungen(
        ctx=ctx, vertrag=vertrag, konto=konto, policy=freigegebene_vier_stufen_policy, heute=date(2026, 10, 27),
        bank_bestaetigt_bis=date(2026, 10, 27),
    )
    assert all(p.status != "GEPLANT" or p.stufe != 2 for p in geplant_ohne_erika)

    # Erika bestätigt NACHTRÄGLICH, später als der Hauptmieter - der
    # SPÄTESTE (konservativste) Zugang wird für die Frist maßgeblich.
    vollstaendig = mahn_service.zugang_bestaetigen_fuer_empfaenger(
        ctx=ctx, mahnlauf_id=mahnlauf.id, mitmieter_id=mitmieter_eintrag.mitmieter_id, heute=date(2026, 9, 15),
        zugang_datum=date(2026, 9, 12), zugangsform="UEBERGABE_BESTAETIGT", zugang_beleg="UEBERGABE-ERIKA", akteur="codex-test",
    )
    assert vollstaendig.zugang_bestaetigt_am == date(2026, 9, 12)  # später als Hauptmieters 10.09.


# -- Auftrag HV-20261020-VERTRAGSDATENBASIS: EINE führende Parteien-/ ------
# -- Vertreter-Struktur statt zwei unabhängig gepflegter Mitmieterlisten ---


def test_hauptmieter_zeile_verknuepft_debitor_ohne_kontaktduplizierung(mitmieter_repo, basis_vertrag, stammdaten_repo):
    """Nutzerdesignvorgabe 20.10.2026: 'Hauptmieter nicht mit abweichend
    editierbaren Kontakten neben Debitor duplizieren; bestehende
    Debitor-ID verknüpfen.' Eine HAUPTMIETER-Zeile trägt deshalb KEINE
    eigenen Kontaktfelder - der Debitor bleibt die einzige Quelle."""

    vertrag, konto = basis_vertrag
    zeile = mitmieter_repo.anlegen(
        vertrag_id=vertrag.id, name=None, email=None, adresse=None, erstellt_von="codex-test",
        rolle="HAUPTMIETER", debitor_id=vertrag.debitor_id, zustand="BESTAETIGT",
        quelle_typ="PDF_EXTRAKTION", quelle_referenz="vertrag.pdf#1",
    )
    assert zeile.rolle == "HAUPTMIETER"
    assert zeile.debitor_id == vertrag.debitor_id
    assert zeile.name is None and zeile.email is None and zeile.adresse is None  # KEINE Duplizierung
    debitor = stammdaten_repo.get_debitor(zeile.debitor_id)
    assert debitor is not None  # Name/Kontakt kommen ausschließlich von hier


def test_list_bestaetigte_mitmieter_schliesst_hauptmieter_und_vertreter_aus(mitmieter_repo, basis_vertrag):
    """NUR bestätigte MITMIETER-Zeilen werden automatisch Mahnempfänger -
    der Hauptmieter ist bereits der Debitor-Anker (siehe Aufrufer in
    `mahnwesen/service.py`), ein VERTRETER wird NIE automatisch
    mitadressiert (bewusste menschliche Entscheidung, siehe
    `Vertragsparteirolle`-Docstring)."""

    vertrag, konto = basis_vertrag
    mitmieter_repo.anlegen(
        vertrag_id=vertrag.id, name=None, email=None, adresse=None, erstellt_von="codex-test",
        rolle="HAUPTMIETER", debitor_id=vertrag.debitor_id, zustand="BESTAETIGT",
    )
    mitmieter_repo.anlegen(
        vertrag_id=vertrag.id, name="Dr. Rechtsanwalt Vertreter", email="vertreter@example.at", adresse=None,
        erstellt_von="codex-test", rolle="VERTRETER", zustand="BESTAETIGT",
    )
    erika = mitmieter_repo.anlegen(
        vertrag_id=vertrag.id, name="Erika Mustermieter", email="erika@example.at", adresse=None,
        erstellt_von="codex-test", rolle="MITMIETER", zustand="BESTAETIGT",
    )

    bestaetigt = mitmieter_repo.list_bestaetigte_mitmieter(vertrag.id)
    assert [m.id for m in bestaetigt] == [erika.id]


def test_list_bestaetigte_mitmieter_schliesst_unbestaetigte_mitmieter_aus(mitmieter_repo, basis_vertrag):
    """Eine erst AUSGELESENE (noch nicht menschlich bestätigte) Mitmieter-
    Zeile darf NIE automatisch Mahnempfänger werden - kein Versand an
    eine noch ungeprüfte, per PDF-Extraktion erratene Angabe."""

    vertrag, konto = basis_vertrag
    mitmieter_repo.anlegen(
        vertrag_id=vertrag.id, name="Noch ungeprüft", email="unklar@example.at", adresse=None,
        erstellt_von="codex-test", rolle="MITMIETER", zustand="AUSGELESEN",
        quelle_typ="PDF_EXTRAKTION", quelle_referenz="vertrag.pdf#2",
    )
    assert mitmieter_repo.list_bestaetigte_mitmieter(vertrag.id) == []
    # `list_aktiv` bleibt die vollständige Übersicht (für die
    # Vertragsanlage-Ansicht) - zeigt die Zeile weiterhin.
    assert len(mitmieter_repo.list_aktiv(vertrag.id)) == 1


def test_mahnlauf_empfaenger_snapshot_entsteht_automatisch_ohne_vertreter(
    mahn_service, op_service, mitmieter_repo, empfaenger_zugang_repo, basis_vertrag, ctx_factory, freigegebene_vier_stufen_policy,
):
    """Ein VERTRETER mit BESTAETIGT-Zustand erzeugt KEINEN eigenen
    Zugangs-Platzhalter beim Versand - nur Hauptmieter(Debitor) + echte,
    bestätigte Mitmieter werden automatisch adressiert."""

    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    mitmieter_repo.anlegen(
        vertrag_id=vertrag.id, name="Dr. Rechtsanwalt Vertreter", email="vertreter@example.at", adresse=None,
        erstellt_von="codex-test", rolle="VERTRETER", zustand="BESTAETIGT",
    )
    mitmieter_repo.anlegen(
        vertrag_id=vertrag.id, name="Erika Mustermieter", email="erika@example.at", adresse=None,
        erstellt_von="codex-test", rolle="MITMIETER", zustand="BESTAETIGT",
    )
    _mit_faelligem_soll(op_service, konto, ctx, faelligkeit=date(2026, 9, 1))
    mahnlauf = _plane_und_versende_stufe(mahn_service, ctx=ctx, vertrag=vertrag, konto=konto, policy=freigegebene_vier_stufen_policy, heute=date(2026, 9, 10), stufe=1)

    eintraege = empfaenger_zugang_repo.fuer_mahnlauf(mahnlauf.id)
    assert len(eintraege) == 2  # Hauptmieter(Debitor) + Erika - NICHT der Vertreter


def test_versuchte_stufe_ohne_gesendete_vorstufe_wirft_reihenfolgefehler(mahn_service, op_service, basis_vertrag, ctx_factory, freigegebene_vier_stufen_policy):
    """`_pruefe_vorstufe_abstand` muss auch für Stufe 3/4 (nicht nur
    Stufe 2) die Reihenfolge erzwingen."""

    from mietinkasso.domain.enums import MahnStufe

    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    with pytest.raises(MahnstufeReihenfolgeError):
        mahn_service._pruefe_vorstufe_abstand(
            forderung_op_position_id=999_999, stufe=MahnStufe.STUFE_3, policy=freigegebene_vier_stufen_policy,
            vertrag=vertrag, heute=date(2026, 1, 1),
        )


def test_alte_zwei_stufen_policy_eskaliert_weiterhin_nicht_ueber_stufe_2(mahn_service, op_service, basis_vertrag, ctx_factory, mahn_policy_repo):
    """Rückwärtskompatibilität: eine Policy OHNE `vier_stufen_profil_
    aktiv` (Default `False`) bleibt exakt beim alten Zwei-Stufen-
    Verhalten - KEIN stillschweigender Wechsel auf vier Stufen."""

    alte_policy = mahn_policy_repo.freigeben(mahn_policy_repo.anlegen(
        stufe1_tage_nach_faelligkeit=7, stufe2_mindesttage_nach_stufe1_versand=14,
        zinsen_prozent=Decimal("0"), gebuehr_cent=0, status="ENTWURF",
    ).id)
    assert alte_policy.vier_stufen_profil_aktiv is False

    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    _mit_faelligem_soll(op_service, konto, ctx, faelligkeit=date(2026, 9, 1))
    mahnlauf_1 = _plane_und_versende_stufe(mahn_service, ctx=ctx, vertrag=vertrag, konto=konto, policy=alte_policy, heute=date(2026, 9, 10), stufe=1)
    mahnlauf_2 = _plane_und_versende_stufe(mahn_service, ctx=ctx, vertrag=vertrag, konto=konto, policy=alte_policy, heute=date(2026, 9, 30), stufe=2)
    assert mahnlauf_2 is not None

    geplant_nach_stufe2 = mahn_service.plane_alle_offenen_forderungen(
        ctx=ctx, vertrag=vertrag, konto=konto, policy=alte_policy, heute=date(2026, 12, 1), bank_bestaetigt_bis=date(2026, 12, 1),
    )
    assert all(p.status == "BLOCKIERT" for p in geplant_nach_stufe2)
