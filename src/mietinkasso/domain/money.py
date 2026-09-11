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


#: Die in Österreich für Vermietung/Verpachtung tatsächlich relevanten
#: UStG-Sätze - 0 % (unecht steuerbefreit / Kleinunternehmer), 10 %
#: (ermäßigt, Wohnraum) und 20 % (Normalsatz, z. B. Garagen/Geschäftsraum).
#: Jeder andere in einer Quelle auftauchende Wert ist keine plausible
#: Interpretationsfrage, sondern eine inkonsistente Quelle und wird
#: geblockt statt stillschweigend gerechnet (siehe `zerlege_brutto_cent`).
ZULAESSIGE_UST_SAETZE_PROMILLE = frozenset({0, 10_000, 20_000})


def zerlege_brutto_cent(brutto_cent: int, ust_satz_promille: int) -> tuple[int, int]:
    """Zerlegt einen BRUTTO-Betrag (das, was tatsächlich vorgeschrieben/
    gebucht wird) in (netto_cent, ust_cent) für den Beleg-Ausweis.

    Definition (verbindlich für dieses Modul, siehe
    `vorschreibung/service.py`): `VertragsKomponenteTable.betrag_cent` /
    `VorschreibungPositionTable.betrag_cent` sind BRUTTO - das ist der
    Betrag, der tatsächlich als SOLL gebucht wird (unverändert gegenüber
    dem bisherigen Verhalten, das `betrag_cent` direkt aufsummiert und
    bucht). `ust_satz_promille` dient AUSSCHLIESSLICH dazu, für den
    Beleg/die Vorschau Netto und USt getrennt auszuweisen, wie es UStG
    verlangt - er verändert NIEMALS den gebuchten/vorgeschriebenen
    Gesamtbetrag.

    Rundung: `netto_cent` wird kaufmännisch (ROUND_HALF_UP) auf ganze
    Cent gerundet; `ust_cent` ist stets `brutto_cent - netto_cent`, damit
    Netto + USt exakt wieder Brutto ergibt (kein Rundungsrest, der sich
    über mehrere Positionen aufsummieren könnte).

    Nur Decimal/Integer-Arithmetik, niemals Float.
    """

    if ust_satz_promille not in ZULAESSIGE_UST_SAETZE_PROMILLE:
        from mietinkasso.domain.exceptions import UStSatzUngueltigError

        raise UStSatzUngueltigError(
            f"USt-Satz {ust_satz_promille} Promille ist keiner der in Österreich für Vermietung "
            f"zulässigen Sätze ({sorted(ZULAESSIGE_UST_SAETZE_PROMILLE)}); inkonsistente Quelle wird "
            "geblockt statt stillschweigend gerechnet."
        )
    if ust_satz_promille == 0:
        return brutto_cent, 0
    satz = Decimal(ust_satz_promille) / Decimal(100_000)  # z. B. 10000/100000 = 0.10
    netto = (Decimal(brutto_cent) / (Decimal("1") + satz)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    netto_cent = int(netto)
    return netto_cent, brutto_cent - netto_cent
