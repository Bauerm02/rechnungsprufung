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
    abgeschlossen_am: date | None = None
    abschluss_referenz: str | None = None
    abschluss_rueckzahlung_cent: int | None = None
    abgeschlossen_durch: str | None = None


@dataclass(frozen=True)
class KautionsGruppensumme:
    bezeichnung: str
    anzahl: int
    vereinbart_cent: int | None
    eingang_cent: int | None
    nachweis_fehlend: int
    vereinbarung_fehlend: int = 0
    eingang_zukuenftig: int = 0


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
    gesellschaft_filter: str | None = None
    abschluss_filter: str = "offen"
    gruppierung: str = "gesellschaft"
    gruppensummen: tuple[KautionsGruppensumme, ...] = ()
    erledigte_ausgeblendet: int = 0


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
    abschluss = getattr(kaution, "abgeschlossen_am", None)
    abschluss_gueltig = (
        abschluss is not None and abschluss <= listenstichtag
        and eingang_stichtag is not None and abschluss >= eingang_stichtag
        and bool((getattr(kaution, "abschluss_referenz", None) or "").strip())
        and bool((getattr(kaution, "abgeschlossen_durch", None) or "").strip())
        and getattr(kaution, "abschluss_rueckzahlung_cent", None) == eingang_cent
        and getattr(mieter.status, "value", mieter.status) == "BEENDET"
    )
    if not abschluss_gueltig:
        abschluss = None
    return KautionsZeile(
        mieter=mieter,
        vereinbart_cent=vereinbart_cent,
        vereinbart_quelle=getattr(profil, "vertragliche_kaution_quellenbeleg", None),
        eingang_cent=eingang_cent,
        eingang_stichtag=eingang_stichtag,
        eingang_referenz=getattr(kaution, "referenz", None),
        eingang_zukuenftig=zukuenftig,
        status_label="Zurückbezahlt / erledigt" if abschluss else _status_label(vereinbart_cent, eingang_cent, zukuenftig),
        abgeschlossen_am=abschluss,
        abschluss_referenz=getattr(kaution, "abschluss_referenz", None) if abschluss else None,
        abschluss_rueckzahlung_cent=getattr(kaution, "abschluss_rueckzahlung_cent", None) if abschluss else None,
        abgeschlossen_durch=getattr(kaution, "abgeschlossen_durch", None) if abschluss else None,
    )


def berechne_kautionsliste(
    ctx: Any,
    objekt_id: Any,
    stammdaten_repository: Any,
    status: str = "alle",
    suche: str | None = None,
    heute: date | None = None,
    gesellschaft_id: str | None = None,
    abschluss: str = "offen",
    gruppierung: str = "gesellschaft",
) -> KautionsListe:
    """Prüft zuerst den Scope über die Mieterliste und liest danach Detaildaten."""
    if abschluss not in ("offen", "erledigt", "alle"):
        raise ValueError("Kautionsfälle: erlaubt sind offen, erledigt und alle.")
    if gruppierung not in ("gesamt", "gesellschaft", "objekt"):
        raise ValueError("Summen: erlaubt sind gesamt, gesellschaft und objekt.")
    basis = berechne_mieterliste(
        ctx=ctx,
        objekt_id=objekt_id,
        stammdaten_repository=stammdaten_repository,
        status=status,
        suche=suche,
        heute=heute,
    )
    optionen = {str(o.id): o for o in basis.objekt_optionen}
    if gesellschaft_id and not any(str(o.gesellschaft_id) == gesellschaft_id for o in optionen.values()):
        raise ValueError("Gesellschaft ist unbekannt oder nicht zugänglich.")
    if any(str(m.objekt_id) not in optionen for m in basis.zeilen):
        raise ValueError("Objektzuordnung in der Kautionsliste ist unvollständig.")
    mieter = [m for m in basis.zeilen if not gesellschaft_id or str(optionen[str(m.objekt_id)].gesellschaft_id) == gesellschaft_id]
    alle = tuple(_baue_zeile(m, stammdaten_repository, basis.stichtag) for m in mieter)
    zeilen = tuple(z for z in alle if abschluss == "alle" or bool(z.abgeschlossen_am) == (abschluss == "erledigt"))
    vereinbart = [z.vereinbart_cent for z in zeilen if z.vereinbart_cent is not None]
    eingang = [
        z.eingang_cent
        for z in zeilen
        if z.eingang_cent is not None and not z.eingang_zukuenftig
    ]
    zukuenftig = sum(1 for z in zeilen if z.eingang_zukuenftig)
    gruppen: dict[str, list[KautionsZeile]] = {}
    labels: dict[str, str] = {}
    for z in zeilen:
        o = optionen.get(str(z.mieter.objekt_id))
        if gruppierung == "gesellschaft":
            key = str(o.gesellschaft_id) if o else str(z.mieter.objekt_id)
            label = o.gesellschaft_name if o else z.mieter.objekt_bezeichnung
        elif gruppierung == "objekt":
            key = str(z.mieter.objekt_id)
            label = f"{z.mieter.objekt_bezeichnung} ({o.gesellschaft_name})" if o else z.mieter.objekt_bezeichnung
        else:
            key, label = "gesamt", "Gesamt"
        gruppen.setdefault(key, []).append(z)
        labels[key] = label
    summen = []
    for key in sorted(gruppen, key=lambda k: (labels[k].casefold(), k)):
        gruppe = gruppen[key]
        soll = [z.vereinbart_cent for z in gruppe if z.vereinbart_cent is not None]
        ist = [z.eingang_cent for z in gruppe if z.eingang_cent is not None and not z.eingang_zukuenftig]
        summen.append(KautionsGruppensumme(labels[key], len(gruppe), sum(soll) if soll else None, sum(ist) if ist else None,
            sum(z.eingang_cent is None and z.vereinbart_cent != 0 for z in gruppe),
            sum(z.vereinbart_cent is None for z in gruppe), sum(z.eingang_zukuenftig for z in gruppe)))
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
        gesellschaft_filter=gesellschaft_id or None,
        abschluss_filter=abschluss,
        gruppierung=gruppierung,
        gruppensummen=tuple(summen),
        erledigte_ausgeblendet=sum(z.abgeschlossen_am is not None for z in alle) if abschluss == "offen" else 0,
    )
