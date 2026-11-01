"""Baut minimale, gültige Test-PDFs von Hand (kein reportlab/weasyprint
als zusätzliche Abhängigkeit nötig) - ausschließlich für Tests der
lokalen PDF-Textextraktion (Auftrag HV-20260913-VERTRAGSANLAGE)."""

from __future__ import annotations


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build_text_pdf(zeilen: list[str]) -> bytes:
    """Ein-Seiten-PDF mit den übergebenen Zeilen als sichtbarer Text
    (echter Textlayer, per Tj-Operator - `extract_text()` findet ihn)."""

    y = 720
    operationen = []
    for zeile in zeilen:
        operationen.append(f"BT /F1 11 Tf 72 {y} Td ({_escape(zeile)}) Tj ET")
        y -= 20
    # cp1252 (statt latin-1) UND ein explizit deklariertes
    # /Encoding /WinAnsiEncoding, damit das Euro-Zeichen (in Latin-1 gar
    # nicht enthalten) korrekt geschrieben UND von `extract_text()` beim
    # Rücklesen wieder korrekt als "€" decodiert wird.
    content = "\n".join(operationen).encode("cp1252", errors="replace")

    objekte = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objekte, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_offset = len(out)
    out += f"xref\n0 {len(objekte) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objekte) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode()
    return bytes(out)


def build_scan_pdf() -> bytes:
    """Ein-Seiten-PDF OHNE jeglichen Textinhalt (leerer Content-Stream) -
    simuliert ein gescanntes Dokument ohne Textlayer/OCR."""

    objekte = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>",
        b"<< /Length 0 >>\nstream\n\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objekte, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_offset = len(out)
    out += f"xref\n0 {len(objekte) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objekte) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode()
    return bytes(out)


def _pdf_string(wert: str) -> bytes:
    """Literal-String; Nicht-Latin-1-Zeichen als UTF-16BE mit BOM (PDF-Textstring)."""

    try:
        return b"(" + _escape(wert).encode("latin-1") + b")"
    except UnicodeEncodeError:
        return b"<FEFF" + wert.encode("utf-16-be").hex().upper().encode() + b">"


def build_form_pdf(seiten: list[dict], *, verwaiste_felder: list[tuple[str, str]] = (), xfa: bool = False) -> bytes:
    """Mehrseitiges PDF mit optionalem Textlayer und echten AcroForm-Widgets.
    `seiten`: [{"text": [...], "felder": [(name, wert, ft), ...]}]; `ft` ist
    "Tx", "Btn", "Sig" oder "Ch", `wert=None` lässt /V weg. Eine Btn-Angabe mit
    Wert "Off" ist ein nicht gesetztes Häkchen. `verwaiste_felder` stehen nur in
    /AcroForm, auf keiner Seite."""

    objekte: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R /AcroForm 3 0 R >>",
        4: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    }
    naechste = 5
    seiten_ids: list[int] = []
    feld_ids: list[int] = []
    for seite in seiten:
        seiten_id, inhalt_id = naechste, naechste + 1
        naechste += 2
        operationen = [
            f"BT /F1 11 Tf 72 {720 - i * 20} Td ({_escape(z)}) Tj ET" for i, z in enumerate(seite.get("text", []))
        ]
        inhalt = "\n".join(operationen).encode("cp1252", errors="replace")
        objekte[inhalt_id] = b"<< /Length " + str(len(inhalt)).encode() + b" >>\nstream\n" + inhalt + b"\nendstream"
        widget_ids = []
        for name, wert, ft in seite.get("felder", []):
            widget_id = naechste
            naechste += 1
            if ft == "Btn":
                v = b" /V /" + (wert or "Off").encode() + b" /AS /" + (wert or "Off").encode()
            elif wert is None:
                v = b""
            elif ft == "Sig":
                v = b" /V << /Type /Sig /Contents <00> >>"
            else:
                v = b" /V " + _pdf_string(wert)
            objekte[widget_id] = (
                b"<< /Type /Annot /Subtype /Widget /FT /" + ft.encode() + b" /T " + _pdf_string(name)
                + v + b" /Rect [72 100 200 120] >>"
            )
            widget_ids.append(widget_id)
            feld_ids.append(widget_id)
        annots = b" /Annots [" + b" ".join(f"{w} 0 R".encode() for w in widget_ids) + b"]" if widget_ids else b""
        objekte[seiten_id] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> "
            b"/Contents " + str(inhalt_id).encode() + b" 0 R" + annots + b" >>"
        )
        seiten_ids.append(seiten_id)
    for name, wert in verwaiste_felder:
        feld_id = naechste
        naechste += 1
        objekte[feld_id] = b"<< /FT /Tx /T " + _pdf_string(name) + b" /V " + _pdf_string(wert) + b" >>"
        feld_ids.append(feld_id)
    kids = b" ".join(f"{i} 0 R".encode() for i in seiten_ids)
    objekte[2] = b"<< /Type /Pages /Kids [" + kids + b"] /Count " + str(len(seiten_ids)).encode() + b" >>"
    felder = b" ".join(f"{i} 0 R".encode() for i in feld_ids)
    objekte[3] = b"<< /Fields [" + felder + b"]" + (b" /XFA (x)" if xfa else b"") + b" >>"

    out = bytearray(b"%PDF-1.7\n")
    offsets = {}
    for nummer in sorted(objekte):
        offsets[nummer] = len(out)
        out += f"{nummer} 0 obj\n".encode() + objekte[nummer] + b"\nendobj\n"
    xref_offset = len(out)
    groesse = max(objekte) + 1
    out += f"xref\n0 {groesse}\n".encode() + b"0000000000 65535 f \n"
    for nummer in range(1, groesse):
        out += f"{offsets[nummer]:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {groesse} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode()
    return bytes(out)
