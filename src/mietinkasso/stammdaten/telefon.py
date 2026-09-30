"""Prüfung/Normalisierung der optionalen Telefonnummer eines Debitors
(Auftrag HV-20260930-PORTAL-LISTEN, Ergänzung `debitoren.telefon`).

EINE gemeinsame Regel für Repository, Intake-Parser und Backoffice-
Formular - bewusst eng: nur Ziffern und die üblichen Trennzeichen, damit
der gespeicherte Wert ohne weitere Behandlung als Text angezeigt und
(wo eindeutig) als `tel:`-Link verwendet werden kann. Es wird nichts
umformatiert oder ergänzt (keine Ländervorwahl geraten)."""

from __future__ import annotations

import re

#: Großzügig für internationale Nummern samt Durchwahl, deutlich unter
#: der Spaltenbreite von `DebitorTable.telefon`.
TELEFON_MAX_LAENGE = 40

_ERLAUBTE_ZEICHEN = re.compile(r"[0-9+()/\-. ]+")
_MINDEST_ZIFFERN = 3


class TelefonUngueltigError(ValueError):
    """Die Telefonnummer ist zu lang oder enthält unzulässige Zeichen."""


def normalisiere_telefon(wert: object) -> str | None:
    """Leer/`None` -> `None` (= keine Nummer hinterlegt). Sonst der
    getrimmte Wert mit einfachen Leerzeichen; Ziffernfolge und
    Schreibweise bleiben unverändert."""

    text = " ".join(str(wert if wert is not None else "").split())
    if not text:
        return None
    if len(text) > TELEFON_MAX_LAENGE:
        raise TelefonUngueltigError(f"Telefonnummer ist zu lang (höchstens {TELEFON_MAX_LAENGE} Zeichen).")
    if not _ERLAUBTE_ZEICHEN.fullmatch(text) or sum(1 for zeichen in text if zeichen.isdigit()) < _MINDEST_ZIFFERN:
        raise TelefonUngueltigError(
            "Telefonnummer darf nur Ziffern, Leerzeichen und die Zeichen + ( ) / - . enthalten "
            f"und braucht mindestens {_MINDEST_ZIFFERN} Ziffern."
        )
    return text


def tel_href(telefon: str | None) -> str | None:
    """`tel:`-Ziel NUR, wenn die Nummer eindeutig wählbar ist. Eine
    Schreibweise mit Klammern (z. B. „+43 (0) 664 …“) oder einem „+“
    außerhalb der ersten Stelle wird NICHT in eine Wählfolge übersetzt -
    die Anzeige bleibt dann reiner Text, statt eine falsche Nummer zu
    verlinken."""

    if not telefon or "(" in telefon or ")" in telefon or "+" in telefon[1:]:
        return None
    ziffern = re.sub(r"[^0-9]", "", telefon)
    if len(ziffern) < _MINDEST_ZIFFERN:
        return None
    return f"tel:{'+' if telefon.startswith('+') else ''}{ziffern}"
