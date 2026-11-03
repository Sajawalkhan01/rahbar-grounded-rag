"""Builds tiny PDFs for tests, so extraction is tested on real PDF files with no downloads.

text_pdf    a text layer (what a normal PDF has)
table_pdf   a drawn grid with text in each cell (what pdfplumber detects as a table)
scanned_pdf an image of text with NO text layer (what a scan is; needs OCR)
"""

from __future__ import annotations

from pathlib import Path


def _pdf(page_streams: list[str]) -> bytes:
    """Assemble a minimal valid PDF: catalog, page tree, Helvetica, one content stream per page."""
    objects: list[bytes] = []
    n = len(page_streams)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    for i, stream in enumerate(page_streams):
        content_id = 4 + 2 * i
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
            f"/Resources << /Font << /F1 {3 + 2 * n} 0 R >> >> >>".encode()
        )
        data = stream.encode("latin-1")
        objects.append(f"<< /Length {len(data)} >>\nstream\n".encode() + data + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _text_stream(lines: list[str], top: int = 740, size: int = 11, x: int = 72) -> str:
    parts = []
    for i, line in enumerate(lines):
        parts.append(f"BT /F1 {size} Tf {x} {top - i * 16} Td ({_escape(line)}) Tj ET")
    return "\n".join(parts)


def text_pdf(path: Path, pages: list[list[str]]) -> Path:
    path.write_bytes(_pdf([_text_stream(lines) for lines in pages]))
    return path


def table_pdf(path: Path, intro: str, rows: list[list[str]]) -> Path:
    """One page: an intro line, then a ruled table. Cells are 150pt wide, 24pt tall."""
    cols, cell_w, cell_h, x0, y0 = len(rows[0]), 150, 24, 72, 650
    parts = [_text_stream([intro])]
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            x, y = x0 + c * cell_w, y0 - r * cell_h
            parts.append(f"{x} {y - cell_h} {cell_w} {cell_h} re S")
            parts.append(f"BT /F1 10 Tf {x + 6} {y - 16} Td ({_escape(cell)}) Tj ET")
    assert cols
    path.write_bytes(_pdf(["\n".join(parts)]))
    return path


def scanned_pdf(path: Path, lines: list[str]) -> Path:
    """An image-only PDF: the text exists only as pixels. Needs Pillow (a pdf2image dependency)."""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (1700, 2200), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 48)
    except OSError:
        font = ImageFont.load_default(size=48)
    for i, line in enumerate(lines):
        draw.text((120, 160 + i * 90), line, fill="black", font=font)
    image.save(path, "PDF", resolution=200.0)
    return path
