"""Tests für `mieweg_vorschau/service.py` (Auftrag 12.09., Paket C):
Rechtsprofil-Gating (kein Rateversuch, kein Wohnungsrechner-Hineinraten
für Gewerbe/WGG), Kombination der zwei Rechenspuren, Objektausschluss,
Quellenbeleg-Pflicht der Vertragsspur, Komponenten-Validierung,
Versionierung."""

from __future__ import annotations

import json
from datetime import date

import pytest

from mietinkasso.domain.exceptions import ObjektAusgeschlossenError, QuellenbelegFehltError
from mietinkasso.mieweg_vorschau.repository import MieWegVorschauRepository
from mietinkasso.mieweg_vorschau.service import MieWegVorschauService, VpiWert


@pytest.fixture
def vorschau_service(session_factory, stammdaten_repo) -> MieWegVorschauService:
    return MieWegVorschauService(MieWegVorschauRepository(session_factory), stammdaten_repo)


def _vpi(**jahre_werte: str) -> dict[int, VpiWert]:
    """`jahre_werte`-Keys kommen als Python-Kwargs zwangsläufig als
    Strings ("2023") herein - hier bewusst in echte `int`-Jahre
    umgewandelt, damit sie zu den `int`-Jahresschlüsseln passen, die der
    Berechnungskern erwartet."""

    return {
        int(jahr): VpiWert(wert=wert, quelle="Statistik Austria VPI 2020", datum="2026-01-15")
        for jahr, wert in jahre_werte.items()
    }


def _basis_kwargs(**overrides) -> dict:
    kwargs = dict(
        rechtsordnung="OESTERREICH_MRG_VOLL",
        mrg_zinsbeschraenkung=False,
        ist_altvertrag=False,
        bezugsjahr=2024,
        bezugsmonat=6,
        letzte_basis_war_jahresdurchschnitt=False,
        ziel_bewertungsjahr=2025,
        basis_betrag_cent=100_000,
        basis_komponenten_ids=[],
        vpi_jahresdurchschnitte=_vpi(**{"2023": "100", "2024": "102"}),
        vertraglich_zulaessiger_betrag_cent=None,
        vertraglicher_quellenbeleg=None,
        vertraglicher_fruehestmoeglicher_termin=None,
        zustellnachweis_referenz=None,
        kommentar=None,
        akteur="markus",
    )
    kwargs.update(overrides)
    return kwargs


def test_ungeklaertes_rechtsprofil_ergibt_entwurf_ohne_berechnung(vorschau_service, basis_vertrag, ctx_factory):
    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")

    vorschau = vorschau_service.vorschau_erstellen(
        ctx=ctx, vertrag=vertrag, **{**_basis_kwargs(), "rechtsordnung": "UNGEKLAERT"},
    )
    ergebnis = json.loads(vorschau.ergebnis_json)
    assert vorschau.ist_wohnungsrechner_fall is False
    assert vorschau.massgeblicher_hoechstbetrag_cent is None
    assert "UNGEKLAERT" in ergebnis["blockiert_grund"]
    assert ergebnis["gesetzliche_hoechstgrenze_cent"] is None


@pytest.mark.parametrize("fremdes_profil", ["OESTERREICH_GEWERBE", "OESTERREICH_WGG", "OESTERREICH_MRG_FREI", "DEUTSCHLAND"])
def test_nicht_wohnungsrechner_faehige_rechtsordnung_wird_nicht_hineingeraten(
    vorschau_service, basis_vertrag, ctx_factory, fremdes_profil,
):
    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")

    vorschau = vorschau_service.vorschau_erstellen(
        ctx=ctx, vertrag=vertrag, **{**_basis_kwargs(), "rechtsordnung": fremdes_profil},
    )
    ergebnis = json.loads(vorschau.ergebnis_json)
    assert vorschau.ist_wohnungsrechner_fall is False
    assert vorschau.massgeblicher_hoechstbetrag_cent is None
    assert "kein Wohnungsrechner-Fall" in ergebnis["blockiert_grund"]


def test_zinsbeschraenkung_nur_fuer_mrg_vollanwendung_erlaubt(vorschau_service, basis_vertrag, ctx_factory):
    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    with pytest.raises(ValueError, match="Vollanwendung"):
        vorschau_service.vorschau_erstellen(
            ctx=ctx, vertrag=vertrag,
            **{**_basis_kwargs(), "rechtsordnung": "OESTERREICH_MRG_TEIL", "mrg_zinsbeschraenkung": True},
        )


def test_unvollstaendige_gesetzliche_eingaben_ergeben_entwurf(vorschau_service, basis_vertrag, ctx_factory):
    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    vorschau = vorschau_service.vorschau_erstellen(
        ctx=ctx, vertrag=vertrag, **{**_basis_kwargs(), "basis_betrag_cent": None},
    )
    ergebnis = json.loads(vorschau.ergebnis_json)
    assert "unvollständig" in ergebnis["blockiert_grund"]
    assert vorschau.massgeblicher_hoechstbetrag_cent is None


def test_fehlender_vpi_jahresdurchschnitt_ergibt_offenen_nachweis_kein_erfundener_wert(
    vorschau_service, basis_vertrag, ctx_factory,
):
    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    vorschau = vorschau_service.vorschau_erstellen(
        ctx=ctx, vertrag=vertrag,
        **{**_basis_kwargs(), "ziel_bewertungsjahr": 2026, "vpi_jahresdurchschnitte": _vpi(**{"2023": "100", "2024": "102"})},
    )
    ergebnis = json.loads(vorschau.ergebnis_json)
    assert ergebnis["gesetzliche_hoechstgrenze_cent"] is None
    assert any("VPI-Jahresdurchschnitt fehlt" in hinweis for hinweis in ergebnis["offene_nachweise"])
    assert vorschau.vollstaendig is False


def test_vertragsspur_ohne_quellenbeleg_wird_abgelehnt(vorschau_service, basis_vertrag, ctx_factory):
    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    with pytest.raises(QuellenbelegFehltError):
        vorschau_service.vorschau_erstellen(
            ctx=ctx, vertrag=vertrag,
            **{**_basis_kwargs(), "vertraglich_zulaessiger_betrag_cent": 101_000, "vertraglicher_quellenbeleg": "  "},
        )


def test_nur_gesetzliche_spur_vorhanden_ergibt_offenen_massgeblichen_betrag(vorschau_service, basis_vertrag, ctx_factory):
    """Fehlende Belege müssen Prüfbedarf ergeben - eine vollständige
    gesetzliche Berechnung allein darf NIE als maßgeblicher Höchstbetrag
    ausgegeben werden, solange die Vertragsspur unbestimmt ist."""

    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    vorschau = vorschau_service.vorschau_erstellen(ctx=ctx, vertrag=vertrag, **_basis_kwargs())
    ergebnis = json.loads(vorschau.ergebnis_json)
    assert ergebnis["gesetzliche_hoechstgrenze_cent"] is not None  # gesetzliche Spur ist vollständig
    assert vorschau.massgeblicher_hoechstbetrag_cent is None  # aber KEIN maßgeblicher Betrag ohne Vertragsspur
    assert any("Vertraglich zulässiger Betrag" in h for h in ergebnis["offene_nachweise"])
    assert any("Maßgeblicher Höchstbetrag noch offen" in h for h in ergebnis["offene_nachweise"])


def test_beide_spuren_vorhanden_massgeblich_ist_das_minimum(vorschau_service, basis_vertrag, ctx_factory):
    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    vorschau = vorschau_service.vorschau_erstellen(
        ctx=ctx, vertrag=vertrag,
        **{
            **_basis_kwargs(),
            "vertraglich_zulaessiger_betrag_cent": 100_500,  # niedriger als die gesetzliche Grenze
            "vertraglicher_quellenbeleg": "Mietvertrag-2024.pdf, Wertsicherungsklausel",
            "vertraglicher_fruehestmoeglicher_termin": date(2025, 4, 1),
        },
    )
    ergebnis = json.loads(vorschau.ergebnis_json)
    gesetzliche_grenze = ergebnis["gesetzliche_hoechstgrenze_cent"]
    assert gesetzliche_grenze > 100_500  # 2% auf 100.000, ungedämpft -> 102.000
    assert vorschau.massgeblicher_hoechstbetrag_cent == 100_500  # das Minimum, nicht die gesetzliche Grenze


def test_vertraglicher_termin_verzoegert_niemals_hinter_vertragstermin_zurueck(vorschau_service, basis_vertrag, ctx_factory):
    """Vertraglich später ausgelöste Schwelle erst zum nächsten
    gesetzlichen April, nie vor dem Vertragstermin: der frühestmögliche
    Gesamttermin ist das MAXIMUM aus gesetzlichem und vertraglichem
    Termin, niemals nur der gesetzliche (der ggf. früher wäre)."""

    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    spaeterer_vertragstermin = date(2026, 4, 1)  # ein Jahr NACH dem gesetzlichen Termin (2025-04-01)
    vorschau = vorschau_service.vorschau_erstellen(
        ctx=ctx, vertrag=vertrag,
        **{
            **_basis_kwargs(),
            "vertraglich_zulaessiger_betrag_cent": 100_500,
            "vertraglicher_quellenbeleg": "Mietvertrag-2024.pdf",
            "vertraglicher_fruehestmoeglicher_termin": spaeterer_vertragstermin,
        },
    )
    assert vorschau.fruehester_termin == spaeterer_vertragstermin


def test_zustellnachweis_fehlt_wird_immer_als_offener_nachweis_gemeldet(vorschau_service, basis_vertrag, ctx_factory):
    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    vorschau = vorschau_service.vorschau_erstellen(
        ctx=ctx, vertrag=vertrag,
        **{
            **_basis_kwargs(),
            "vertraglich_zulaessiger_betrag_cent": 100_500,
            "vertraglicher_quellenbeleg": "Mietvertrag-2024.pdf",
            "vertraglicher_fruehestmoeglicher_termin": date(2025, 4, 1),
        },
    )
    ergebnis = json.loads(vorschau.ergebnis_json)
    assert any("Zustellnachweis fehlt" in h for h in ergebnis["offene_nachweise"])
    assert vorschau.vollstaendig is False  # Zustellnachweis fehlt -> nicht "vollständig"


def test_altvertrag_vor_2026_wird_abgelehnt(vorschau_service, basis_vertrag, ctx_factory):
    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    with pytest.raises(ValueError, match="2026"):
        vorschau_service.vorschau_erstellen(
            ctx=ctx, vertrag=vertrag,
            **{**_basis_kwargs(), "ist_altvertrag": True, "ziel_bewertungsjahr": 2025},
        )


def test_nicht_indexierbare_komponente_wird_abgelehnt(vorschau_service, stammdaten_repo, basis_vertrag, ctx_factory):
    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    stammdaten_repo.add_komponente(
        id="K-GARAGE", vertrag_id=vertrag.id, art="GARAGE", bezeichnung="Garage",
        betrag_cent=5_000, ust_satz_promille=20_000, gueltig_von=date(2024, 1, 1),
    )  # indexierbar=False (Default)
    with pytest.raises(ValueError, match="indexierbar"):
        vorschau_service.vorschau_erstellen(
            ctx=ctx, vertrag=vertrag, **{**_basis_kwargs(), "basis_komponenten_ids": ["K-GARAGE"]},
        )


def test_indexierbare_komponente_wird_akzeptiert(vorschau_service, stammdaten_repo, basis_vertrag, ctx_factory):
    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    stammdaten_repo.add_komponente(
        id="K-HMZ", vertrag_id=vertrag.id, art="HMZ", bezeichnung="Hauptmietzins",
        betrag_cent=55_000, ust_satz_promille=10_000, gueltig_von=date(2024, 1, 1), indexierbar=True,
    )
    vorschau = vorschau_service.vorschau_erstellen(
        ctx=ctx, vertrag=vertrag, **{**_basis_kwargs(), "basis_komponenten_ids": ["K-HMZ"]},
    )
    eingaben = json.loads(vorschau.eingaben_json)
    assert eingaben["basis_komponenten_ids"] == ["K-HMZ"]


def test_vorschau_auf_ausgeschlossenem_objekt_wird_blockiert(vorschau_service, stammdaten_repo, ctx_factory):
    stammdaten_repo.upsert_gesellschaft(id="7DI", name="7D Immobilien GmbH")
    stammdaten_repo.upsert_objekt(id="107", gesellschaft_id="7DI", bezeichnung="Sieben Dörfer", ausgeschlossen=True)
    stammdaten_repo.upsert_einheit(id="107-TOP1", objekt_id="107", bezeichnung="Top 1", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten_repo.upsert_debitor(id="DEB-107", name="Mieterin 107")
    stammdaten_repo.upsert_vertrag(
        id="V-107-1", einheit_id="107-TOP1", debitor_id="DEB-107", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    vertrag = stammdaten_repo.get_vertrag("V-107-1")
    ctx = ctx_factory("7DI")
    with pytest.raises(ObjektAusgeschlossenError):
        vorschau_service.vorschau_erstellen(ctx=ctx, vertrag=vertrag, **_basis_kwargs())


def test_mehrere_vorschauen_bilden_aufsteigende_versionsgeschichte(vorschau_service, basis_vertrag, ctx_factory):
    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    vorschau_service.vorschau_erstellen(ctx=ctx, vertrag=vertrag, **_basis_kwargs())
    vorschau_service.vorschau_erstellen(ctx=ctx, vertrag=vertrag, **_basis_kwargs())
    historie = vorschau_service.liste_fuer_vertrag(vertrag.id)
    assert [v.version for v in historie] == [2, 1]
    assert vorschau_service.aktuelle(vertrag.id).version == 2
