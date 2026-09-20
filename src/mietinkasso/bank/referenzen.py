"""Reines Parsen von Vertragskennungen aus einer Bankreferenz.

Gemeinsame Grundlage fuer Automatch/Vorschau (Service) und die
Mahnrelevanz-Pruefung (Repository). Keine DB, keine Seiteneffekte.
Gueltig ist ausschliesslich `VERTRAG:<id>` mit <id> aus [A-Za-z0-9-],
weder am Wortanfang angeklebt (KEINVERTRAG:) noch hinten fortgesetzt
(V-601-3_NACHFOLGER). Solche Faelle werden NICHT abgeschnitten,
sondern als `unklar` gemeldet.
"""

from __future__ import annotations

import string
from dataclasses import dataclass

_PRAEFIX = "VERTRAG:"
_ID_ZEICHEN = frozenset(string.ascii_letters + string.digits + "-")


def _ist_wortzeichen(zeichen: str) -> bool:
    """Alles, was eine Kennung fortsetzen koennte - inkl. '_' und Nicht-ASCII."""

    return zeichen.isalnum() or zeichen == "_"


@dataclass(frozen=True)
class ReferenzBefund:
    """ids: Menge der sauber geparsten, distinkten Kennungen.
    unklar: mindestens ein 'VERTRAG:'-Vorkommen war nicht verwertbar."""

    ids: frozenset[str]
    unklar: bool

    @property
    def eindeutige_id(self) -> str | None:
        if self.unklar or len(self.ids) != 1:
            return None
        return next(iter(self.ids))


def parse_vertragskennungen(referenz: str | None) -> ReferenzBefund:
    if not referenz:
        return ReferenzBefund(frozenset(), False)
    ids: set[str] = set()
    unklar = False
    suchpos = 0
    while True:
        start = referenz.find(_PRAEFIX, suchpos)
        if start < 0:
            break
        suchpos = start + len(_PRAEFIX)
        if start > 0 and _ist_wortzeichen(referenz[start - 1]):
            unklar = True  # z. B. KEINVERTRAG:
            continue
        ende = suchpos
        while ende < len(referenz) and referenz[ende] in _ID_ZEICHEN:
            ende += 1
        kandidat = referenz[suchpos:ende]
        nachfolger = referenz[ende] if ende < len(referenz) else ""
        if (
            not kandidat
            or (nachfolger and _ist_wortzeichen(nachfolger))
        ):
            unklar = True
            continue
        ids.add(kandidat)
    return ReferenzBefund(frozenset(ids), unklar)
