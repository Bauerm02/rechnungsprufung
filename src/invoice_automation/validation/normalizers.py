from __future__ import annotations

import re
import unicodedata


_WHITESPACE_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^A-Z0-9]")
_VAT_OCR_REPLACEMENTS = str.maketrans({
    "O": "0",
    "I": "1",
    "L": "1",
    "B": "8",
    "S": "5",
    "G": "6",
    "Z": "2",
    "T": "7",
})


def fold_to_ascii(value: str) -> str:
    replaced = (
        value.replace("ae", "ae")
        .replace("oe", "oe")
        .replace("ue", "ue")
        .replace("Ae", "Ae")
        .replace("Oe", "Oe")
        .replace("Ue", "Ue")
        .replace("ä", "ae")
        .replace("ö", "oe")
        .replace("ü", "ue")
        .replace("Ä", "Ae")
        .replace("Ö", "Oe")
        .replace("Ü", "Ue")
        .replace("ß", "ss")
    )
    return unicodedata.normalize("NFKD", replaced).encode("ascii", "ignore").decode("ascii")


def normalize_sender_name(value: str | None) -> str | None:
    if not value:
        return None
    ascii_value = fold_to_ascii(value.strip()).lower()
    return _WHITESPACE_RE.sub(" ", ascii_value)


def normalize_invoice_number(value: str | None) -> str | None:
    if not value:
        return None
    compact = _NON_ALNUM_RE.sub("", value.upper())
    return compact or None


def normalize_content_hash(value: str | None) -> str | None:
    if not value:
        return None
    compact = _WHITESPACE_RE.sub("", value.strip()).lower()
    return compact or None


def normalize_vat_id(value: str | None) -> str | None:
    if not value:
        return None
    compact = _NON_ALNUM_RE.sub("", value.upper())[:15]
    if compact in {"", "LEER", "NICHTLESBAR"}:
        return None
    if len(compact) < 2:
        return None

    prefix = compact[:2]
    suffix = compact[2:]

    if prefix == "AT":
        if not suffix.startswith("U"):
            suffix = f"U{suffix.lstrip('U')}"
        number = suffix[1:].translate(_VAT_OCR_REPLACEMENTS)
        if len(number) != 8 or not number.isdigit():
            return None
        return f"ATU{number}"

    corrected_suffix = suffix.translate(_VAT_OCR_REPLACEMENTS)
    normalized = f"{prefix}{corrected_suffix}"[:15]
    return normalized or None


def normalize_iban(value: str | None) -> str | None:
    if not value:
        return None
    compact = _WHITESPACE_RE.sub("", value.upper())
    if "X" in compact or "*" in compact:
        return None
    compact = compact.replace("O", "0").replace("I", "1").replace("S", "5")
    return compact or None


def is_valid_iban_checksum(iban: str | None) -> bool | None:
    if not iban:
        return None
    if len(iban) < 15 or len(iban) > 34:
        return False
    if not re.fullmatch(r"[A-Z]{2}[0-9]{2}[A-Z0-9]+", iban):
        return False
    rearranged = iban[4:] + iban[:4]
    numeric = "".join(str(ord(char) - 55) if char.isalpha() else char for char in rearranged)
    remainder = 0
    for char in numeric:
        remainder = (remainder * 10 + int(char)) % 97
    return remainder == 1
