from invoice_automation.validation.normalizers import normalize_vat_id


def test_vat_normalization_applies_austrian_ocr_correction_for_s() -> None:
    assert normalize_vat_id("ATUS2345678") == "ATU52345678"


def test_vat_normalization_applies_austrian_ocr_correction_for_b() -> None:
    assert normalize_vat_id("ATUB2345678") == "ATU82345678"


def test_vat_normalization_applies_austrian_ocr_correction_for_o() -> None:
    assert normalize_vat_id("ATUO2345678") == "ATU02345678"


def test_vat_normalization_inserts_missing_u_for_austrian_number() -> None:
    assert normalize_vat_id("AT 41234567") == "ATU41234567"


def test_vat_normalization_rejects_invalid_austrian_length() -> None:
    assert normalize_vat_id("ATU1234567") is None


def test_vat_normalization_truncates_generic_vat_ids_to_legacy_limit() -> None:
    assert normalize_vat_id("DE1234567890123456789") == "DE1234567890123"
