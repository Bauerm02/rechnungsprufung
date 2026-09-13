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
