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
        ist_wohnungsnutzung=True,
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
        aktuell_verrechneter_betrag_cent=None,
        aktuell_verrechnet_quellenbeleg=None,
        aktuell_verrechnet_stichtag=None,
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


@pytest.mark.parametrize("ist_wohnungsnutzung", [False, None])
def test_mrg_ohne_bestaetigte_wohnungsnutzung_ist_kein_wohnungsrechner_fall(
    vorschau_service, basis_vertrag, ctx_factory, ist_wohnungsnutzung,
):
    """MieWeG §1 Abs1 gilt nur für Wohnungen - MRG_VOLL/MRG_TEIL allein
    (z. B. ein Geschäftsraum unter Vollanwendung) darf ohne explizit
    bestätigte Wohnungsnutzung NICHT als Wohnungsrechner-Fall behandelt
    werden."""

    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    vorschau = vorschau_service.vorschau_erstellen(
        ctx=ctx, vertrag=vertrag, **{**_basis_kwargs(), "ist_wohnungsnutzung": ist_wohnungsnutzung},
    )
    ergebnis = json.loads(vorschau.ergebnis_json)
    assert vorschau.ist_wohnungsrechner_fall is False
    assert vorschau.massgeblicher_hoechstbetrag_cent is None
    assert "Wohnungsnutzung" in ergebnis["blockiert_grund"]


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


def test_ohne_aktuell_verrechneten_betrag_bleibt_ausfuehrbare_erhoehung_offen(
    vorschau_service, basis_vertrag, ctx_factory,
):
    """Kernfall Markus (historischer Basisbetrag != heute tatsächlich
    verrechneter Betrag): auch wenn Vertrags- und Gesetzesspur sowie
    Zustellnachweis vollständig sind, bleibt OHNE einen dokumentierten
    aktuell verrechneten Betrag Prüfbedarf - massgeblicher_hoechstbetrag_cent
    ist NIE unmittelbar eine ausführbare Erhöhung."""

    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    vorschau = vorschau_service.vorschau_erstellen(
        ctx=ctx, vertrag=vertrag,
        **{
            **_basis_kwargs(),
            "vertraglich_zulaessiger_betrag_cent": 100_500,
            "vertraglicher_quellenbeleg": "Mietvertrag-2024.pdf",
            "vertraglicher_fruehestmoeglicher_termin": date(2025, 4, 1),
            "zustellnachweis_referenz": "Zustellnachweis-2025-04.pdf",
        },
    )
    ergebnis = json.loads(vorschau.ergebnis_json)
    assert vorschau.massgeblicher_hoechstbetrag_cent == 100_500
    assert ergebnis["ausfuehrbare_erhoehung_cent"] is None
    assert vorschau.vollstaendig is False
    assert any("Historie ist keine ausführbare Erhöhung" in h for h in ergebnis["offene_nachweise"])


def test_ausfuehrbare_erhoehung_ist_differenz_zum_aktuell_verrechneten_betrag(
    vorschau_service, basis_vertrag, ctx_factory,
):
    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    vorschau = vorschau_service.vorschau_erstellen(
        ctx=ctx, vertrag=vertrag,
        **{
            **_basis_kwargs(),
            "vertraglich_zulaessiger_betrag_cent": 100_500,
            "vertraglicher_quellenbeleg": "Mietvertrag-2024.pdf",
            "vertraglicher_fruehestmoeglicher_termin": date(2025, 4, 1),
            "zustellnachweis_referenz": "Zustellnachweis-2025-04.pdf",
            "aktuell_verrechneter_betrag_cent": 99_800,
            "aktuell_verrechnet_quellenbeleg": "Vorschreibung-2025-01.pdf",
            "aktuell_verrechnet_stichtag": date(2025, 1, 1),
        },
    )
    ergebnis = json.loads(vorschau.ergebnis_json)
    assert vorschau.massgeblicher_hoechstbetrag_cent == 100_500
    assert ergebnis["ausfuehrbare_erhoehung_cent"] == 700  # 100_500 - 99_800
    assert vorschau.vollstaendig is True


def test_ausfuehrbare_erhoehung_wird_nie_negativ_wenn_bereits_verrechnet_ueber_hoechstbetrag(
    vorschau_service, basis_vertrag, ctx_factory,
):
    """Ist der aktuell verrechnete Betrag bereits gleich oder höher als der
    maßgebliche Höchstbetrag, ist dieser Erhöhungsschritt ausgeschöpft -
    max(0, ...) statt einer vorgeschlagenen Senkung."""

    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    vorschau = vorschau_service.vorschau_erstellen(
        ctx=ctx, vertrag=vertrag,
        **{
            **_basis_kwargs(),
            "vertraglich_zulaessiger_betrag_cent": 100_500,
            "vertraglicher_quellenbeleg": "Mietvertrag-2024.pdf",
            "vertraglicher_fruehestmoeglicher_termin": date(2025, 4, 1),
            "zustellnachweis_referenz": "Zustellnachweis-2025-04.pdf",
            "aktuell_verrechneter_betrag_cent": 101_000,
            "aktuell_verrechnet_quellenbeleg": "Vorschreibung-2025-01.pdf",
            "aktuell_verrechnet_stichtag": date(2025, 1, 1),
        },
    )
    ergebnis = json.loads(vorschau.ergebnis_json)
    assert ergebnis["ausfuehrbare_erhoehung_cent"] == 0


def test_aktuell_verrechneter_betrag_ohne_quellenbeleg_wird_abgelehnt(
    vorschau_service, basis_vertrag, ctx_factory,
):
    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    with pytest.raises(QuellenbelegFehltError):
        vorschau_service.vorschau_erstellen(
            ctx=ctx, vertrag=vertrag,
            **{**_basis_kwargs(), "aktuell_verrechneter_betrag_cent": 99_000, "aktuell_verrechnet_quellenbeleg": "  "},
        )


def test_vertraglicher_termin_im_selben_jahr_vor_april_ergibt_gesetzlichen_termin(
    vorschau_service, basis_vertrag, ctx_factory,
):
    """Ein vertraglicher Termin VOR dem gesetzlichen 1. April desselben
    Ziel-Bewertungsjahres ändert nichts - maßgeblich bleibt der 1. April,
    da nur er ein gültiger MieWeG-Anpassungstermin ist (§1 Abs3)."""

    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    vorschau = vorschau_service.vorschau_erstellen(
        ctx=ctx, vertrag=vertrag,
        **{
            **_basis_kwargs(),
            "vertraglich_zulaessiger_betrag_cent": 100_500,
            "vertraglicher_quellenbeleg": "Mietvertrag-2024.pdf",
            "vertraglicher_fruehestmoeglicher_termin": date(2025, 2, 1),
        },
    )
    assert vorschau.fruehester_termin == date(2025, 4, 1)


def test_vertraglicher_termin_in_anderem_jahr_wird_nicht_stillschweigend_uebernommen(
    vorschau_service, basis_vertrag, ctx_factory,
):
    """KORRIGIERT (Folgeauftrag Markus): ein vertraglicher Termin, der
    (nach Aufrundung auf den nächsten gültigen 1. April) in ein ANDERES
    Jahr als das berechnete Ziel-Bewertungsjahr fällt, darf NICHT
    stillschweigend als Ergebnis-Termin ausgegeben werden - dafür wären
    zusätzliche, hier nicht berechnete VPI-Jahreswerte nötig. Frühere
    Fassung gab hier fälschlich den rohen vertraglichen Termin
    (2026-04-01) direkt aus, obwohl nur bis 2025 gerechnet wurde. Auch
    ein NICHT-April-Termin (hier: ein vertraglicher September-Termin)
    darf nie unverändert als Ergebnis erscheinen (§1 Abs3: nur der
    1. April ist ein gültiger Anpassungstermin)."""

    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")

    fuer_naechstes_jahr = vorschau_service.vorschau_erstellen(
        ctx=ctx, vertrag=vertrag,
        **{
            **_basis_kwargs(),
            "vertraglich_zulaessiger_betrag_cent": 100_500,
            "vertraglicher_quellenbeleg": "Mietvertrag-2024.pdf",
            "vertraglicher_fruehestmoeglicher_termin": date(2026, 4, 1),
        },
    )
    ergebnis_naechstes_jahr = json.loads(fuer_naechstes_jahr.ergebnis_json)
    assert fuer_naechstes_jahr.fruehester_termin is None
    assert fuer_naechstes_jahr.vollstaendig is False
    assert any("anderes Jahr" in h for h in ergebnis_naechstes_jahr["offene_nachweise"])

    fuer_september = vorschau_service.vorschau_erstellen(
        ctx=ctx, vertrag=vertrag,
        **{
            **_basis_kwargs(),
            "vertraglich_zulaessiger_betrag_cent": 100_500,
            "vertraglicher_quellenbeleg": "Mietvertrag-2024.pdf",
            "vertraglicher_fruehestmoeglicher_termin": date(2025, 9, 1),
        },
    )
    ergebnis_september = json.loads(fuer_september.ergebnis_json)
    assert fuer_september.fruehester_termin is None
    assert any("anderes Jahr" in h for h in ergebnis_september["offene_nachweise"])


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


def test_altvertrag_bezugsjahr_identisch_zum_vertragsbeginn_wird_als_offener_nachweis_markiert(
    vorschau_service, basis_vertrag, ctx_factory,
):
    """§4 Abs2: bei Altverträgen ist der zuletzt tatsächlich verwendete
    Indexmonat maßgeblich, NICHT blind der ursprüngliche Mietbeginn.
    `basis_vertrag` beginnt am 2024-01-01 - Bezugsjahr/-monat exakt
    2024/1 ist ein konkretes Verdachtsmoment, dass hier fälschlich der
    Vertragsbeginn statt des letzten Indexmonats verwendet wurde."""

    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    vorschau = vorschau_service.vorschau_erstellen(
        ctx=ctx, vertrag=vertrag,
        **{
            **_basis_kwargs(),
            "ist_altvertrag": True,
            "bezugsjahr": 2024,
            "bezugsmonat": 1,
            "ziel_bewertungsjahr": 2026,
            "vpi_jahresdurchschnitte": _vpi(**{"2023": "100", "2024": "102", "2025": "104"}),
        },
    )
    ergebnis = json.loads(vorschau.ergebnis_json)
    assert vorschau.vollstaendig is False
    assert any("ursprünglichen Vertragsbeginn" in h for h in ergebnis["offene_nachweise"])


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


def test_leere_komponenten_id_wird_abgelehnt(vorschau_service, basis_vertrag, ctx_factory):
    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    with pytest.raises(ValueError, match="leere"):
        vorschau_service.vorschau_erstellen(
            ctx=ctx, vertrag=vertrag, **{**_basis_kwargs(), "basis_komponenten_ids": ["  "]},
        )


def test_doppelte_komponenten_id_wird_abgelehnt(vorschau_service, stammdaten_repo, basis_vertrag, ctx_factory):
    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    stammdaten_repo.add_komponente(
        id="K-HMZ-DUP", vertrag_id=vertrag.id, art="HMZ", bezeichnung="Hauptmietzins",
        betrag_cent=55_000, ust_satz_promille=10_000, gueltig_von=date(2024, 1, 1), indexierbar=True,
    )
    with pytest.raises(ValueError, match="doppelte"):
        vorschau_service.vorschau_erstellen(
            ctx=ctx, vertrag=vertrag,
            **{
                **_basis_kwargs(),
                "basis_komponenten_ids": ["K-HMZ-DUP", "K-HMZ-DUP"],
                "basis_betrag_cent": 110_000,
            },
        )


@pytest.mark.parametrize("bk_hk_alias_art", ["BK_VORAUSZAHLUNG", "HEIZ_WW_VORAUSZAHLUNG", "BK_VZ", "HK_VZ", "BK_PARKPLATZ"])
def test_bk_hk_aliasarten_werden_trotz_indexierbar_flag_ausgeschlossen(
    vorschau_service, stammdaten_repo, basis_vertrag, ctx_factory, bk_hk_alias_art,
):
    """Verteidigung in der Tiefe: selbst eine fehlerhaft `indexierbar=True`
    markierte Betriebs-/Heizkosten(-Vorauszahlungs)-Komponente (oder eine
    ihrer Aliasarten) darf nie in die MieWeG-Basis einfließen."""

    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    stammdaten_repo.add_komponente(
        id=f"K-{bk_hk_alias_art}", vertrag_id=vertrag.id, art=bk_hk_alias_art, bezeichnung="BK/HK-Vorauszahlung",
        betrag_cent=8_000, ust_satz_promille=20_000, gueltig_von=date(2024, 1, 1), indexierbar=True,
    )
    with pytest.raises(ValueError, match="NIE automatisch indexiert"):
        vorschau_service.vorschau_erstellen(
            ctx=ctx, vertrag=vertrag,
            **{**_basis_kwargs(), "basis_komponenten_ids": [f"K-{bk_hk_alias_art}"], "basis_betrag_cent": 8_000},
        )


def test_indexierbare_komponente_wird_akzeptiert(vorschau_service, stammdaten_repo, basis_vertrag, ctx_factory):
    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    stammdaten_repo.add_komponente(
        id="K-HMZ", vertrag_id=vertrag.id, art="HMZ", bezeichnung="Hauptmietzins",
        betrag_cent=55_000, ust_satz_promille=10_000, gueltig_von=date(2024, 1, 1), indexierbar=True,
    )
    # basis_betrag_cent MUSS zur Summe der referenzierten Komponenten passen
    # (siehe test_basis_betrag_muss_zur_summe_der_referenzierten_komponenten_passen) -
    # ursprünglich stand hier noch der unveränderte 100_000-Default aus
    # _basis_kwargs(), obwohl die Komponente nur 55_000 trägt; das war genau
    # die im Folgeauftrag gefundene Lücke ("Komponenten statt Gesamtbrutto").
    vorschau = vorschau_service.vorschau_erstellen(
        ctx=ctx, vertrag=vertrag,
        **{**_basis_kwargs(), "basis_komponenten_ids": ["K-HMZ"], "basis_betrag_cent": 55_000},
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


# ---------------------------------------------------------------------------
# Auftrag Markus (Folgeauftrag): Schutzlogik gegen bereits enthaltene
# Erhöhungen - unabhängige Prüfung anhand konkreter Reproduktionsfälle.
# Diese Tests dokumentieren/reproduzieren Lücken, die VOR dieser Prüfung im
# Service bestanden (kein Cross-Check zwischen basis_betrag_cent und den
# referenzierten Komponenten, keine Gültigkeits-/Zeitraumprüfung der
# referenzierten Komponenten, keine Warnung bei überlappenden
# Bezugsjahr-/Ziel-Bewertungsjahr-Bereichen über mehrere Vorschau-Versionen
# desselben Vertrags hinweg).
# ---------------------------------------------------------------------------


def test_basis_betrag_muss_zur_summe_der_referenzierten_komponenten_passen(
    vorschau_service, stammdaten_repo, basis_vertrag, ctx_factory,
):
    """Komponenten statt Gesamtbrutto: wer explizit Komponenten referenziert,
    behauptet damit "meine Basis besteht aus GENAU diesen Beträgen" - ein
    davon unabhängiger, frei erfundener basis_betrag_cent (hier: 100.000,
    obwohl die referenzierte Komponente nur 55.000 trägt) darf nicht
    stillschweigend akzeptiert werden, sonst kann ein bereits in der
    Komponente enthaltener Erhöhungsschritt unbemerkt mit einem falschen,
    zu hohen (oder zu niedrigen) Ausgangswert weitergerechnet werden."""

    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    stammdaten_repo.add_komponente(
        id="K-HMZ", vertrag_id=vertrag.id, art="HMZ", bezeichnung="Hauptmietzins",
        betrag_cent=55_000, ust_satz_promille=10_000, gueltig_von=date(2024, 1, 1), indexierbar=True,
    )
    with pytest.raises(ValueError, match="Summe"):
        vorschau_service.vorschau_erstellen(
            ctx=ctx, vertrag=vertrag,
            **{**_basis_kwargs(), "basis_komponenten_ids": ["K-HMZ"], "basis_betrag_cent": 100_000},
        )


def test_basis_betrag_konsistent_mit_mehreren_unterschiedlichen_komponentenstaenden(
    vorschau_service, stammdaten_repo, basis_vertrag, ctx_factory,
):
    """Mehrere Komponenten mit UNTERSCHIEDLICHEN Beständen (HMZ/Küche) sind
    zulässig, solange ihre Summe exakt dem angegebenen basis_betrag_cent
    entspricht - keine Vermischung, kein Verlust einer der beiden Stände."""

    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    stammdaten_repo.add_komponente(
        id="K-HMZ", vertrag_id=vertrag.id, art="HMZ", bezeichnung="Hauptmietzins",
        betrag_cent=55_000, ust_satz_promille=10_000, gueltig_von=date(2024, 1, 1), indexierbar=True,
    )
    stammdaten_repo.add_komponente(
        id="K-KUECHE", vertrag_id=vertrag.id, art="KUECHE", bezeichnung="Küche",
        betrag_cent=15_000, ust_satz_promille=10_000, gueltig_von=date(2024, 1, 1), indexierbar=True,
    )
    vorschau = vorschau_service.vorschau_erstellen(
        ctx=ctx, vertrag=vertrag,
        **{**_basis_kwargs(), "basis_komponenten_ids": ["K-HMZ", "K-KUECHE"], "basis_betrag_cent": 70_000},
    )
    eingaben = json.loads(vorschau.eingaben_json)
    assert eingaben["basis_betrag_cent"] == 70_000


def test_referenzierte_komponente_muss_zum_bezugszeitpunkt_bereits_bestanden_haben(
    vorschau_service, stammdaten_repo, basis_vertrag, ctx_factory,
):
    """Eine Komponente, die laut Stammdaten erst NACH dem angegebenen
    Bezugsjahr/-monat entstanden ist (z. B. eine erst 2025 vereinbarte
    Küche, während mit Bezugsjahr 2024 gerechnet wird), darf nicht in die
    Basis einfließen - sonst würde ein Bestandteil, der zum historischen
    Bezugszeitpunkt noch gar nicht Teil des Mietverhältnisses war, über den
    gesamten Kumulierungszeitraum mit hochgerechnet."""

    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    stammdaten_repo.add_komponente(
        id="K-KUECHE-NEU", vertrag_id=vertrag.id, art="KUECHE", bezeichnung="Küche (neu vereinbart)",
        betrag_cent=15_000, ust_satz_promille=10_000, gueltig_von=date(2025, 6, 1), indexierbar=True,
    )
    with pytest.raises(ValueError, match="noch nicht"):
        vorschau_service.vorschau_erstellen(
            ctx=ctx, vertrag=vertrag,
            **{
                **_basis_kwargs(),
                "bezugsjahr": 2024,
                "bezugsmonat": 6,
                "basis_komponenten_ids": ["K-KUECHE-NEU"],
                "basis_betrag_cent": 15_000,
            },
        )


def test_ueberlappende_folgevorschau_wird_als_offener_nachweis_markiert(
    vorschau_service, basis_vertrag, ctx_factory,
):
    """Kernfall der Doppelzählungs-Schutzlogik: eine bereits vollständig
    berechnete Vorschau (Version 1, Bezugsjahr 2024 -> Ziel 2025) deckt den
    Erhöhungsschritt 2024->2025 bereits ab. Eine Folgevorschau (Version 2)
    für ein SPÄTERES Ziel-Bewertungsjahr, die aber wieder mit demselben
    ALTEN Bezugsjahr 2024 statt mit dem Ziel der Vorversion (2025) rechnet,
    würde genau diesen Schritt ein zweites Mal in die Kumulierung
    einbeziehen - das MUSS als offener Nachweis markiert werden (nicht
    automatisch blockiert, aber nie stillschweigend als vollständig
    ausgewiesen)."""

    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    vollstaendige_kwargs = {
        **_basis_kwargs(),
        "vertraglich_zulaessiger_betrag_cent": 100_500,
        "vertraglicher_quellenbeleg": "Mietvertrag-2024.pdf, Wertsicherungsklausel",
        "vertraglicher_fruehestmoeglicher_termin": date(2025, 4, 1),
        "aktuell_verrechneter_betrag_cent": 100_000,
        "aktuell_verrechnet_quellenbeleg": "Vorschreibung-2025-01.pdf",
        "aktuell_verrechnet_stichtag": date(2025, 1, 1),
        "zustellnachweis_referenz": "Zustellnachweis-2025-04.pdf",
    }

    erste = vorschau_service.vorschau_erstellen(ctx=ctx, vertrag=vertrag, **vollstaendige_kwargs)
    assert erste.vollstaendig is True  # Ausgangslage: v1 ist vollständig und deckt 2024->2025 ab.

    zweite = vorschau_service.vorschau_erstellen(
        ctx=ctx, vertrag=vertrag,
        **{
            **vollstaendige_kwargs,
            "ziel_bewertungsjahr": 2026,
            "vpi_jahresdurchschnitte": _vpi(**{"2023": "100", "2024": "102", "2025": "104"}),
            "vertraglicher_fruehestmoeglicher_termin": date(2026, 4, 1),
        },
    )
    ergebnis = json.loads(zweite.ergebnis_json)
    assert zweite.vollstaendig is False
    assert any("Version 1" in h and "2025" in h for h in ergebnis["offene_nachweise"])
