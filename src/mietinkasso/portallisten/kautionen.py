"""Rein lesende Kautionsübersicht auf Basis der bestehenden Mieterliste."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from mietinkasso.portallisten.service import berechne_mieterliste

KEINE_KAUTION = "Keine Kaution vereinbart"
EINGANG_BESTAETIGT = "Eingang bestätigt"
EINGANG_NICHT_BELEGT = "Eingang nicht belegt"
VEREINBARUNG_FEHLT = "Kautionsvereinbarung fehlt"


@dataclass(frozen=True)
class KautionsZeile:
    mieter: Any
    vereinbart_cent: int | None
    vereinbart_quelle: str | None
    eingang_cent: int | None
    eingang_stichtag: date | None
    eingang_referenz: str | None
    eingang_zukuenftig: bool
    status_label: str


@dataclass(frozen=True)
class KautionsListe:
    basis: Any
    zeilen: tuple[KautionsZeile, ...]
    vereinbart_summe_cent: int | None
    vereinbart_bekannt: int
    vereinbart_fehlend: int
    eingang_summe_cent: int | None
    eingang_bekannt: int
    eingang_fehlend: int
    eingang_zukuenftig: int


def _lese_optional(repository: Any, methode: str, vertrag_id: Any) -> Any:
    lesen = getattr(repository, methode, None)
    return lesen(vertrag_id) if callable(lesen) else None


def _status_label(
    vereinbart_cent: int | None,
    eingang_cent: int | None,
    zukuenftig: bool,
) -> str:
    eingang_wirksam = eingang_cent is not None and not zukuenftig
    if eingang_wirksam and not (eingang_cent == 0 and vereinbart_cent == 0):
        return EINGANG_BESTAETIGT
    if vereinbart_cent == 0:
        return KEINE_KAUTION
    if vereinbart_cent is None:
        return VEREINBARUNG_FEHLT
    return EINGANG_NICHT_BELEGT


def _baue_zeile(mieter: Any, repository: Any, listenstichtag: date) -> KautionsZeile:
    profil = _lese_optional(repository, "neuestes_mietvertragsprofil", mieter.vertrag_id)
    kaution = _lese_optional(repository, "get_kaution", mieter.vertrag_id)
    vereinbart_cent = getattr(profil, "vertragliche_kaution_cent", None)
    eingang_cent = kaution.betrag_cent if kaution is not None else None
    eingang_stichtag = kaution.stichtag if kaution is not None else None
    zukuenftig = eingang_stichtag is not None and eingang_stichtag > listenstichtag
    return KautionsZeile(
        mieter=mieter,
        vereinbart_cent=vereinbart_cent,
        vereinbart_quelle=getattr(profil, "vertragliche_kaution_quellenbeleg", None),
        eingang_cent=eingang_cent,
        eingang_stichtag=eingang_stichtag,
        eingang_referenz=getattr(kaution, "referenz", None),
        eingang_zukuenftig=zukuenftig,
        status_label=_status_label(vereinbart_cent, eingang_cent, zukuenftig),
    )


def berechne_kautionsliste(
    ctx: Any,
    objekt_id: Any,
    stammdaten_repository: Any,
    status: str = "alle",
    suche: str | None = None,
    heute: date | None = None,
) -> KautionsListe:
    """Prüft zuerst den Scope über die Mieterliste und liest danach Detaildaten."""
    basis = berechne_mieterliste(
        ctx=ctx,
        objekt_id=objekt_id,
        stammdaten_repository=stammdaten_repository,
        status=status,
        suche=suche,
        heute=heute,
    )
    zeilen = tuple(
        _baue_zeile(mieter, stammdaten_repository, basis.stichtag) for mieter in basis.zeilen
    )
    vereinbart = [z.vereinbart_cent for z in zeilen if z.vereinbart_cent is not None]
    eingang = [
        z.eingang_cent
        for z in zeilen
        if z.eingang_cent is not None and not z.eingang_zukuenftig
    ]
    zukuenftig = sum(1 for z in zeilen if z.eingang_zukuenftig)
    return KautionsListe(
        basis=basis,
        zeilen=zeilen,
        vereinbart_summe_cent=sum(vereinbart) if vereinbart else None,
        vereinbart_bekannt=len(vereinbart),
        vereinbart_fehlend=len(zeilen) - len(vereinbart),
        eingang_summe_cent=sum(eingang) if eingang else None,
        eingang_bekannt=len(eingang),
        eingang_fehlend=sum(z.eingang_cent is None and z.vereinbart_cent != 0 for z in zeilen),
        eingang_zukuenftig=zukuenftig,
    )
