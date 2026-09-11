#!/usr/bin/env python3
"""CLI-Vorschau für einen George-Business-CSV-Export.

Rein lesend: liest genau die angegebene Datei, gibt einen Prüfbericht auf
stdout aus und beendet sich. KEIN Datenbankimport, KEINE Bankverbindung,
KEIN Versand, KEINE Buchung, KEIN Serverstart, KEIN Netzwerkzugriff.
Deterministisch — derselbe Aufruf auf derselben Datei liefert immer
denselben Bericht (Replay-sicher).

Aufruf:
    python scripts/george_business_preview.py \\
        --datei pfad/zum/export.csv \\
        --konto AT00 0000 0000 0000 0000 \\
        --von 01.01.2026 --bis 31.01.2026

Optionale Saldenkontrolle (nur wenn BEIDE Werte angegeben werden):
        --anfangssaldo 1.234,56 --endsaldo 2.345,67

Siehe `src/mietinkasso/bank/george_business_csv.py` für die
Klassifizierungslogik (Kandidat/Sammel-Summenzeile/Prüffall/Abgelehnt)
und `src/mietinkasso/importtemplates/README.md` für das Exportformat und
die bekannten Grenzen dieses Bausteins.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mietinkasso.bank.george_business_csv import (  # noqa: E402
    GeorgeFormatFehlerError,
    GeorgeZeilenErgebnis,
    erstelle_preview,
    parse_oesterreichischen_betrag,
)


def _datum(text: str) -> date:
    try:
        return datetime.strptime(text, "%d.%m.%Y").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"'{text}' ist kein gültiges Datum im Format TT.MM.JJJJ.") from exc


def _betrag_cent(text: str) -> int:
    try:
        return parse_oesterreichischen_betrag(text.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _baue_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--datei", required=True, type=Path, help="Pfad zur George-Business-CSV-Exportdatei.")
    parser.add_argument("--konto", required=True, dest="konto_iban", help="Erwartete eigene IBAN (Preview-Parameter).")
    parser.add_argument("--von", required=True, type=_datum, help="Erwartetes Periodenbeginn-Datum (TT.MM.JJJJ).")
    parser.add_argument("--bis", required=True, type=_datum, help="Erwartetes Periodenende-Datum (TT.MM.JJJJ).")
    parser.add_argument(
        "--anfangssaldo",
        type=_betrag_cent,
        default=None,
        help="Bestätigter Anfangssaldo (österr. Format, z. B. 1.234,56). Nur zusammen mit --endsaldo wirksam.",
    )
    parser.add_argument(
        "--endsaldo",
        type=_betrag_cent,
        default=None,
        help="Bestätigter Endsaldo (österr. Format). Nur zusammen mit --anfangssaldo wirksam.",
    )
    return parser


def _eur(cent: int) -> str:
    vorzeichen = "-" if cent < 0 else ""
    betrag = f"{abs(cent) // 100:,}".replace(",", ".") + f",{abs(cent) % 100:02d}"
    return f"{vorzeichen}{betrag} €"


def _zeile_bericht(zeile: GeorgeZeilenErgebnis) -> str:
    if zeile.kandidat is not None:
        k = zeile.kandidat
        return (
            f"  Zeile {zeile.zeile_nr} (Datei-Zeile {zeile.csv_zeile}): {zeile.status} — "
            f"{k.buchungsdatum.strftime('%d.%m.%Y')} {_eur(k.betrag_cent)} Partner={k.partner_name or '-'} "
            f"Ref={k.zahlungsreferenz or '-'} Kandidaten-ID={k.kandidaten_id[:12]}…"
        )
    return f"  Zeile {zeile.zeile_nr} (Datei-Zeile {zeile.csv_zeile}): {zeile.status} — {zeile.grund}"


def main(argv: list[str] | None = None) -> int:
    args = _baue_parser().parse_args(argv)

    try:
        rohbytes = args.datei.read_bytes()
    except OSError as exc:
        print(f"Datei nicht lesbar: {exc}", file=sys.stderr)
        return 2

    try:
        preview = erstelle_preview(
            rohbytes,
            erwartetes_konto_iban=args.konto_iban,
            von=args.von,
            bis=args.bis,
            anfangssaldo_cent=args.anfangssaldo,
            endsaldo_cent=args.endsaldo,
        )
    except GeorgeFormatFehlerError as exc:
        print(f"Format abgelehnt: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"Ungültige Parameter: {exc}", file=sys.stderr)
        return 2

    print(f"George-Business-CSV-Vorschau — Datei: {args.datei}")
    print(f"  SHA256 (Dateiinhalt): {preview.datei_sha256}")
    print(f"  Erwartetes Konto: {preview.erwartetes_konto_iban}  Zeitraum: {preview.von:%d.%m.%Y}–{preview.bis:%d.%m.%Y}")
    print(
        f"  Zeilen gesamt: {len(preview.zeilen)}  Kandidaten: {len(preview.kandidaten)}  "
        f"Sammel-Summenzeilen: {len(preview.sammel_summen)}  Prüffälle: {len(preview.pruefffaelle)}  "
        f"Abgelehnt: {len(preview.abgelehnt)}"
    )
    print(f"  Eingänge (im Konto/Zeitraum): {_eur(preview.eingaenge_cent)}")
    print(f"  Ausgänge (im Konto/Zeitraum): {_eur(preview.ausgaenge_cent)}")
    if preview.saldo_kontrolle is not None:
        sk = preview.saldo_kontrolle
        if not sk.stimmt_ueberein:
            status = "ABWEICHUNG"
        elif preview.vollstaendig:
            status = "OK"
        else:
            # Rechnerisch geht die Kontrolle auf, aber die Datei ist wegen
            # Prüffällen/Ablehnungen unvollständig - ein "OK" hier allein
            # wäre ein Erfolgssignal auf Basis fehlender statt geprüfter
            # Zeilen. Niemals "OK" anzeigen, bevor der Gesamtstatus unten
            # nicht ebenfalls VOLLSTÄNDIG ist.
            status = "rechnerisch OK, aber Datei UNVOLLSTÄNDIG (siehe Prüffälle/Abgelehnt unten)"
        print(
            f"  Saldenkontrolle: Anfang {_eur(sk.anfangssaldo_cent)} + Eingänge + Ausgänge = "
            f"{_eur(sk.berechneter_endsaldo_cent)} vs. gemeldetes Ende {_eur(sk.endsaldo_cent)} -> {status}"
        )
    else:
        print("  Saldenkontrolle: nicht geprüft (Anfangs-/Endsaldo nicht beide angegeben).")

    for hinweis in preview.warnungen:
        print(f"  HINWEIS: {hinweis}")

    if preview.sammel_summen:
        print("\nSammel-Summenzeilen (strukturell eindeutig, kein Kandidat):")
        for zeile in preview.sammel_summen:
            print(_zeile_bericht(zeile))

    if preview.pruefffaelle:
        print("\nPrüffälle (manuelle Klärung nötig, NICHT automatisch normalisiert):")
        for zeile in preview.pruefffaelle:
            print(_zeile_bericht(zeile))

    if preview.abgelehnt:
        print("\nAbgelehnte Zeilen (Format-/Wertefehler):")
        for zeile in preview.abgelehnt:
            print(_zeile_bericht(zeile))

    print(f"\nKandidaten ({len(preview.kandidaten)}):")
    for zeile in preview.kandidaten:
        print(_zeile_bericht(zeile))

    if preview.vollstaendig:
        print("\nGesamtstatus: VOLLSTÄNDIG (keine Prüffälle, keine Ablehnungen, Saldenkontrolle bestätigt oder nicht angefordert).")
        return 0

    gruende = []
    if preview.pruefffaelle:
        gruende.append(f"{len(preview.pruefffaelle)} Prüffall/Prüffälle")
    if preview.abgelehnt:
        gruende.append(f"{len(preview.abgelehnt)} abgelehnte Zeile(n)")
    if preview.saldo_kontrolle is not None and not preview.saldo_kontrolle.stimmt_ueberein:
        gruende.append("Saldenkontrolle weicht ab")
    print(f"\nGesamtstatus: UNVOLLSTÄNDIG — {', '.join(gruende)}. Kein Erfolgssignal ohne manuelle Klärung.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
