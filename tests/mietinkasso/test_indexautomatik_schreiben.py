from __future__ import annotations

from datetime import date

from mietinkasso.indexautomatik.schreiben import (
    SchreibenJahresschritt,
    SchreibenKomponente,
    SchreibenKontext,
    erhoehungsschreiben_text_klausel,
    erhoehungsschreiben_text_mieweg,
)


def _basis_kontext(**overrides) -> SchreibenKontext:
    basis = dict(
        gesellschaft_name="7D Immobilien GmbH",
        objekt_bezeichnung="Am Corso",
        objekt_adresse="Corsogasse 1, 1010 Wien",
        einheit_bezeichnung="Top 3",
        mieter_name="Max Mustermieter",
        mieter_adresse="Corsogasse 1/3, 1010 Wien",
        rechtsordnung="OESTERREICH_MRG_VOLL",
        klausel_referenz="Punkt 5 Wertsicherung",
        geaenderte_komponenten=[
            SchreibenKomponente(
                art="HMZ", bezeichnung="Hauptmietzins", alter_betrag_cent=100_000, neuer_betrag_cent=101_000,
                ust_satz_promille=10_000,
            )
        ],
        unveraenderte_komponenten=[
            SchreibenKomponente(
                art="BK_VORAUSZAHLUNG", bezeichnung="Betriebskosten", alter_betrag_cent=15_000,
                neuer_betrag_cent=15_000, ust_satz_promille=10_000,
            )
        ],
        erhoehung_cent=1_000,
        massgeblicher_termin=date(2026, 4, 1),
        vertrag_beleg_referenz="Mietvertrag V-601-3",
        rechtsprofil_version=1,
        jlb_signatur="JLB Projects GmbH",
        bezugsjahr=2024,
        bezugsmonat=1,
        ziel_bewertungsjahr=2026,
        jahresschritte=[
            SchreibenJahresschritt(
                jahr=2025, vpi_vorjahr="100", vpi_jahr="102", rohe_veraenderung="2%",
                gedaempfte_veraenderung="2%", angewandte_veraenderung="2%",
            )
        ],
        vertraglich_zulaessiger_betrag_cent=101_000,
        vertraglicher_quellenbeleg="Mietvertrag Punkt 5",
    )
    basis.update(overrides)
    return SchreibenKontext(**basis)


def test_ust_prozentsatz_wird_korrekt_skaliert_nicht_als_1000_prozent():
    """Bugfund Zwischenreview 3cec004: `ust_satz_promille / 10` hätte für
    10000 Promille (=10%) fälschlich "1000%" ausgegeben."""

    text = erhoehungsschreiben_text_mieweg(_basis_kontext())
    assert "10,0% USt" in text
    assert "1000" not in text.replace("1.000", "").replace("101.000", "").replace("100.000", "")


def test_neuer_gesamtbetrag_enthaelt_unveraenderte_bk_nicht_nur_hmz():
    """"Keine irreführende Gesamtsumme nur aus HMZ ohne unveränderte
    BK" - der neue Gesamtbetrag muss HMZ-neu (1.010,00) PLUS
    unveränderte BK (150,00) = 1.160,00 sein, nicht nur 1.010,00."""

    text = erhoehungsschreiben_text_mieweg(_basis_kontext())
    assert "Neuer monatlicher Gesamtbetrag (brutto, alle Positionen): 1.160,00 €" in text
    assert "Bisheriger monatlicher Gesamtbetrag (brutto, alle Positionen): 1.150,00 €" in text


def test_unveraenderte_position_wird_als_unveraendert_ausgewiesen():
    text = erhoehungsschreiben_text_mieweg(_basis_kontext())
    assert "Betriebskosten (BK_VORAUSZAHLUNG): 150,00 € brutto" in text
    assert "unverändert" in text


def test_mieweg_schreiben_zitiert_par16_abs9():
    text = erhoehungsschreiben_text_mieweg(_basis_kontext())
    assert "§ 16 Abs 9 MRG" in text


def test_klausel_schreiben_behauptet_par16_abs9_nicht_pauschal():
    """Modellreview 13.09.: "§16(9) gilt im Schreiben nicht pauschal für
    MRG-Teil; Geschäftsraum benötigt eigenes Schreiben"."""

    kontext = _basis_kontext(rechtsordnung="OESTERREICH_MRG_TEIL")
    text = erhoehungsschreiben_text_klausel(kontext)
    # § 16 Abs 9 taucht NUR in der ausdrücklichen Nicht-Behauptung auf,
    # nirgends als Rechtsgrundlage für Wirksamkeit/Zahlungspflicht (wie
    # im MieWeG-Schreiben).
    assert "KEINE pauschale Anwendung von § 16 Abs 9 MRG" in text
    assert "gemäß § 16 Abs 9 MRG" not in text
    assert "Zahlungspflicht" not in text
