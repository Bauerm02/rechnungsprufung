"""Gemeinsame Betrags- und Bezugsregeln für Buchungen und Ersatzbuchungen."""
from mietinkasso.domain.enums import OPTyp
from mietinkasso.domain.exceptions import ZahlungsbindungInkonsistentError
from mietinkasso.infrastructure.db.tables import OPPositionTable


def pruefe_betrag_positiv(typ: OPTyp, betrag_cent: int) -> None:
    # Eröffnung/Korrektur tragen ihr Vorzeichen selbst; unveränderte Fachregel.
    if typ in (OPTyp.EROEFFNUNG, OPTyp.KORREKTUR):
        return
    if betrag_cent <= 0:
        raise ValueError(f"betrag_cent muss für {typ.value} positiv sein (erhalten: {betrag_cent}).")


def validiere_zahlungsbindung(zahlung: OPPositionTable, forderung: OPPositionTable) -> None:
    if zahlung.konto_id != forderung.konto_id:
        raise ZahlungsbindungInkonsistentError(
            f"Zahlung #{zahlung.id} (Konto {zahlung.konto_id}) referenziert über bezieht_sich_auf_id eine "
            f"Forderung #{forderung.id} eines ANDEREN Kontos ({forderung.konto_id})."
        )
    if zahlung.leistungsperiode and forderung.leistungsperiode and zahlung.leistungsperiode != forderung.leistungsperiode:
        raise ZahlungsbindungInkonsistentError(
            f"Zahlung #{zahlung.id} (Leistungsperiode {zahlung.leistungsperiode}) referenziert über "
            f"bezieht_sich_auf_id eine Forderung #{forderung.id} mit ABWEICHENDER Leistungsperiode "
            f"({forderung.leistungsperiode})."
        )


def pruefe_bezug(quelle: OPPositionTable, ziel: OPPositionTable | None) -> None:
    """Prüft eine explizite Bindung vor dem Schreiben; ungebunden bleibt zulässig."""
    if quelle.bezieht_sich_auf_id is None:
        return
    if ziel is None or ziel.status != "AKTIV" or (quelle.id is not None and ziel.id == quelle.id):
        raise ZahlungsbindungInkonsistentError(
            f"OPPosition #{quelle.id}: Bezugsposition #{quelle.bezieht_sich_auf_id} "
            "fehlt, ist storniert oder verweist auf sich selbst."
        )
    if ziel.konto_id != quelle.konto_id:
        raise ZahlungsbindungInkonsistentError("Bezugsposition gehört zu einem anderen Konto.")
    typ = OPTyp(quelle.typ)
    if typ in (OPTyp.ZAHLUNG, OPTyp.GUTSCHRIFT):
        if OPTyp(ziel.typ) not in (OPTyp.EROEFFNUNG, OPTyp.SOLL, OPTyp.RUECKLASTSCHRIFT) or ziel.betrag_cent <= 0:
            raise ZahlungsbindungInkonsistentError("Zahlungsbindung verlangt eine positive Forderung.")
        validiere_zahlungsbindung(quelle, ziel)
    elif typ is OPTyp.RUECKLASTSCHRIFT:
        if OPTyp(ziel.typ) is not OPTyp.ZAHLUNG:
            raise ZahlungsbindungInkonsistentError("Rücklastschrift muss sich auf eine Zahlung beziehen.")
    else:
        raise ZahlungsbindungInkonsistentError(f"Für {typ.value} ist keine ausdrückliche OP-Bindung definiert.")
