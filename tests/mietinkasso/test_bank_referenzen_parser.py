from mietinkasso.bank.referenzen import parse_vertragskennungen as p


def test_einzelne_kennung_bleibt_eindeutig():
    b = p("Miete 09/2026 VERTRAG:V-601-3")
    assert b.ids == frozenset({"V-601-3"}) and b.unklar is False
    assert b.eindeutige_id == "V-601-3"


def test_wiederholung_derselben_id_bleibt_eindeutig():
    assert p("VERTRAG:V-601-3 / VERTRAG:V-601-3").eindeutige_id == "V-601-3"


def test_zwei_verschiedene_ids_sind_mehrdeutig():
    b = p("VERTRAG:V-601-3 und VERTRAG:V-601-4")
    assert b.ids == frozenset({"V-601-3", "V-601-4"}) and b.eindeutige_id is None


def test_unbekannte_zusatz_id_blockiert_ebenfalls():
    assert p("VERTRAG:V-601-3 VERTRAG:XX-999-UNBEKANNT").eindeutige_id is None


def test_laengere_kennung_ist_kein_praefix_treffer():
    b = p("VERTRAG:V-601-3-NACHFOLGER")
    assert b.ids == frozenset({"V-601-3-NACHFOLGER"})
    assert "V-601-3" not in b.ids


def test_unterstrich_fortsetzung_wird_nicht_abgeschnitten():
    b = p("VERTRAG:V-601-3_NACHFOLGER")
    assert b.ids == frozenset() and b.unklar is True and b.eindeutige_id is None


def test_nicht_ascii_fortsetzung_ist_unklar():
    assert p("VERTRAG:V-601-3ä").unklar is True


def test_eingebettetes_keinvertrag_ist_keine_kennung():
    b = p("KEINVERTRAG:V-601-3")
    assert b.ids == frozenset() and b.unklar is True


def test_keinvertrag_neben_echter_kennung_blockiert():
    assert p("VERTRAG:V-601-3 KEINVERTRAG:V-601-9").eindeutige_id is None


def test_leere_unvollstaendige_und_randfaelle():
    assert p(None) == p("") and p(None).unklar is False
    assert p("Miete ohne Kennung").ids == frozenset()
    assert p("VERTRAG:").unklar is True
    assert p("VERTRAG: V-601-3").unklar is True          # Leerzeichen nach Doppelpunkt
    assert p("VERTRAG:V-601-3-").eindeutige_id == "V-601-3-"  # bisherige ID-Grammatik erhalten
    assert p("vertrag:v-601-3").ids == frozenset()       # Großschreibung bleibt Pflicht
    assert p("(VERTRAG:V-601-3)").eindeutige_id == "V-601-3"
