"""Money handling: integer cents everywhere outside of pure legal formulas.

Two distinct rounding rules exist in this domain and must never be
conflated:

- ``round_money_half_up`` for ordinary monetary amounts (Vorschreibung,
  BK, Mahngebühren, ...).
- ``round_index_half_cent_down`` for index/Richtwert adjustments under
  MRG/MieWeG §1 Abs. 2 Z. 3, where an amount landing on *exactly* half a
  cent must be rounded DOWN, unlike normal commercial rounding.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
HALF_CENT = Decimal("0.005")


def to_cents(amount: Decimal | int | float | str) -> int:
    """Convert a Decimal/number to integer cents using standard half-up rounding."""

    quantized = round_money_half_up(Decimal(str(amount)))
    return int((quantized * 100).to_integral_value())


def cents_to_decimal(cents: int) -> Decimal:
    return (Decimal(cents) / 100).quantize(CENT)


def round_money_half_up(amount: Decimal) -> Decimal:
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def round_index_half_cent_down(amount: Decimal) -> Decimal:
    """Round to full cents; a value exactly at the half-cent boundary rounds down.

    Example: 10.005 -> 10.00 (not 10.01, which ROUND_HALF_UP would give).
    Everything else uses standard half-up rounding, matching how the
    Fachabteilung reads §1 Abs. 2 Z. 3 MRG/MieWeG: only the *exact* half
    cent is special-cased downward, not general rounding behaviour.
    """

    scaled = amount * 100
    remainder = scaled - scaled.to_integral_value(rounding=ROUND_DOWN)
    if remainder == Decimal("0.5"):
        cents = scaled.to_integral_value(rounding=ROUND_DOWN)
    else:
        cents = scaled.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return (cents / 100).quantize(CENT)


def sum_cents(values: list[int]) -> int:
    return sum(values) if values else 0
