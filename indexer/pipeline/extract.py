"""Stage 1: Extract. PDF (or HTML, or Markdown) to text, page by page.

Why this stage matters most: every later stage can only work with the text produced here.
A table that comes out jumbled, a two-column page read straight across, or a scanned page
that comes out empty cannot be repaired by a better embedding model.

How each format is handled:
  * PDF  - the text layer is read with pdfplumber, in READING ORDER: words are split into
           columns at the vertical gutters (the FAST-NUCES handbooks are set in 2 or 3
           columns) and each column is read top to bottom. Lines in a bigger or bold font are
           written as Markdown headings ("## 5.4 Academic Warning"), which is how the chunker
           finds section boundaries. Tables are found separately and written as "a | b | c"
           rows, so a fee table keeps its columns. A page whose text layer is (almost) empty
           is rendered to an image and sent to Tesseract (OCR).
  * HTML - a small parser keeps headings ("# ...") and turns <table> into the same
           "a | b | c" rows. Navigation, scripts and footers are dropped.
  * MD/TXT - read as is (the CI fixtures are Markdown). Front matter is removed.
"""

from __future__ import annotations

import logging
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .models import Document, Page

log = logging.getLogger("rahbar.indexer.extract")
# pdfminer warns "CropBox missing" once per page on many university PDFs; it is noise.
logging.getLogger("pdfminer").setLevel(logging.ERROR)

# A page with fewer letters/digits than this has no usable text layer and is OCR'd.
MIN_TEXT_CHARS = 25
OCR_DPI = 200

EDGE_ZONE = 0.07         # top and bottom 7% of a page are read full width (running header/footer)
MIN_GUTTER = 7           # points of empty space that separate two text columns
MIN_COLUMN_SHARE = 0.12  # a column must hold at least this share of the page's words
LINE_TOLERANCE = 3.0     # words whose top edges differ by less than this are on one line
MAX_HEADING_WORDS = 14
MERGED_CELL_SHARE = 0.4  # a table with more empty cells than this has merged cells, not data


def ocr_enabled() -> bool:
    """OCR_ENABLED=false exists only so docs/FAILURE-ANALYSIS.md can break stage A on purpose."""
    return os.environ.get("OCR_ENABLED", "true").lower() not in ("0", "false", "no")


def _useful_chars(text: str) -> int:
    return sum(ch.isalnum() for ch in text)


def render_table(rows: list[list[str | None]]) -> str:
    """Rows of cells to 'a | b | c' lines. Newlines inside a cell become spaces."""
    cleaned = [[" ".join((c or "").split()) for c in row] for row in rows]
    total = sum(len(r) for r in cleaned)
    empty = sum(1 for r in cleaned for c in r if not c)
    # PDF tables with merged cells come back as "| Lahore |  |  | 16 |  |  | 11 |...". When most
    # cells are empty they are merge artefacts, not data, so each row keeps only its filled cells.
    squeeze = total > 0 and empty / total > MERGED_CELL_SHARE
    lines = []
    for cells in cleaned:
        if squeeze:
            cells = [c for c in cells if c]
        if any(cells):
            lines.append(" | ".join(cells))
    return "\n".join(lines)


def extract(path: str, doc_version: str, superseded: bool) -> Document:
    """Return the document's text page by page.

    Detect pages with no text layer and OCR them; mark those pages ocr=True.
    The OCR libraries need system packages in your Dockerfile (tesseract-ocr,
    poppler-utils), not only pip packages.
    """
    p = Path(path)
    suffix = p.suffix.lower()
    doc = Document(source=p.name, doc_version=doc_version, superseded=superseded)
    if suffix == ".pdf":
        _extract_pdf(p, doc)
    elif suffix in (".html", ".htm"):
        html = p.read_text(encoding="utf-8", errors="replace")
        doc.pages.append(Page(p.name, 1, html_to_text(html)))
    elif suffix in (".md", ".txt"):
        doc.pages.append(Page(p.name, 1, strip_front_matter(p.read_text(encoding="utf-8"))))
    else:
        raise ValueError(f"cannot extract {p.name}: unsupported file type {suffix!r}")
    return doc


# --------------------------------------------------------------------------- PDF
def _extract_pdf(path: Path, doc: Document) -> None:
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        for number, pdf_page in enumerate(pdf.pages, start=1):
            text = ""
            try:
                text = _page_text_with_tables(pdf_page)
            except Exception as exc:  # one broken page must not lose the whole document
                doc.problems.append(f"page {number}: text extraction failed ({exc!r})")
            if _useful_chars(text) >= MIN_TEXT_CHARS:
                doc.pages.append(Page(doc.source, number, text))
                continue
            # No usable text layer: this is a scan (or an image-only page).
            if not ocr_enabled():
                doc.problems.append(f"page {number}: no text layer and OCR is disabled")
                doc.pages.append(Page(doc.source, number, text))
                continue
            ocr_text = _ocr_page(path, number, doc)
            doc.pages.append(Page(doc.source, number, ocr_text, ocr=True))


def _page_text_with_tables(pdf_page: Any) -> str:
    """Reading-order text outside tables (columns handled, headings marked), then each table
    as 'a | b | c' rows.

    Two things go wrong with pdfplumber's plain extract_text() on these documents:
      * a page set in 2 or 3 columns is read line by line ACROSS the columns, so a sentence
        from the left column is interleaved with one from the right;
      * a table is read row by row across the page, which mixes neighbouring cells.
    So tables are found first and their area is removed from the page; the remaining words
    are split into columns at the vertical gutters and each column is read top to bottom.
    """
    tables = pdf_page.find_tables()
    body_page = pdf_page
    rendered: list[str] = []
    if tables:
        boxes = [t.bbox for t in tables]

        def outside(obj: dict) -> bool:
            if "x0" not in obj or "top" not in obj:
                return True
            cx, cy = (obj["x0"] + obj["x1"]) / 2, (obj["top"] + obj["bottom"]) / 2
            return not any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in boxes)

        body_page = pdf_page.filter(outside)
        for t in tables:
            text = render_table(t.extract())
            # A one-column "table" is usually a boxed paragraph, not data; skip the pipes.
            if text and " | " in text:
                rendered.append(text)
    words = body_page.extract_words(extra_attrs=["size", "fontname"])
    body = layout_text(words, float(pdf_page.width), float(pdf_page.height))
    return "\n\n".join(x for x in [body.strip(), *rendered] if x)


# ------------------------------------------------------------------ reading order
def find_gutters(words: list[dict], width: float) -> list[float]:
    """x positions of the vertical gutters between text columns (empty for one column).

    Every word covers some x range; a gutter is a run of x positions covered by (almost) no
    word. A few words may straddle it (a heading that spans both columns), so a small
    tolerance is allowed.
    """
    n = len(words)
    if n < 40:
        return []
    occupancy = [0] * (int(width) + 2)
    for w in words:
        for x in range(max(0, int(w["x0"])), min(len(occupancy) - 1, int(w["x1"]) + 1)):
            occupancy[x] += 1
    tolerance = max(2, int(0.012 * n))
    lo, hi = int(0.12 * width), int(0.88 * width)
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for x in range(lo, hi + 1):
        if occupancy[x] <= tolerance:
            start = x if start is None else start
        elif start is not None:
            runs.append((start, x - 1))
            start = None
    if start is not None:
        runs.append((start, hi))
    gutters = [(a + b) / 2 for a, b in runs if b - a + 1 >= MIN_GUTTER]
    # Keep only gutters that leave real text on both sides.
    while gutters:
        edges = [0.0, *gutters, width + 1.0]
        shares = [
            sum(1 for w in words if edges[i] <= (w["x0"] + w["x1"]) / 2 < edges[i + 1]) / n
            for i in range(len(edges) - 1)
        ]
        smallest = shares.index(min(shares))
        if shares[smallest] >= MIN_COLUMN_SHARE:
            break
        gutters.pop(min(smallest, len(gutters) - 1))
    return gutters


def _line_groups(words: list[dict]) -> list[list[dict]]:
    """Cluster words into lines (by their top edge), each line sorted left to right."""
    lines: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (round(w["top"], 1), w["x0"])):
        if lines and abs(w["top"] - lines[-1][0]["top"]) <= LINE_TOLERANCE:
            lines[-1].append(w)
        else:
            lines.append([w])
    return [sorted(line, key=lambda w: w["x0"]) for line in lines]


def _heading_prefix(line: list[dict], body_size: float) -> str:
    """'## ' for a line set bigger than the body text, '### ' for a short all-bold line."""
    text = " ".join(w["text"] for w in line)
    if len(line) > MAX_HEADING_WORDS or not any(c.isalpha() for c in text):
        return ""
    if text.endswith((",", ";")) or (text.endswith(".") and not re.match(r"^\d+(\.\d+)*\.? ", text)):
        return ""
    if re.search(r"\s\d[\d,.%]*$", text):      # "G. Total 428": a labelled value, not a heading
        return ""
    sizes = sorted(w["size"] for w in line)
    size = sizes[len(sizes) // 2]
    if size >= body_size + 1.0:
        return "## "
    bold = all("bold" in str(w["fontname"]).lower() for w in line)
    if bold and len(line) <= 10 and size >= body_size - 0.1:
        return "### "
    return ""


def _group_text(words: list[dict], body_size: float) -> str:
    out: list[str] = []
    previous_bottom: float | None = None
    for line in _line_groups(words):
        top = min(w["top"] for w in line)
        size = max(w["size"] for w in line)
        if previous_bottom is not None and top - previous_bottom > 0.8 * size:
            out.append("")                       # a paragraph gap
        out.append(_heading_prefix(line, body_size) + " ".join(w["text"] for w in line))
        previous_bottom = max(w["bottom"] for w in line)
    return "\n".join(out)


def layout_text(words: list[dict], width: float, height: float) -> str:
    """Words of one page to text: running header, then each column top to bottom, then the
    running footer. Headings (bigger or bold lines) are prefixed with Markdown '#'."""
    if not words:
        return ""
    sizes = [round(w["size"], 1) for w in words for _ in w["text"]]
    body_size = max(set(sizes), key=sizes.count)
    top_cut, bottom_cut = EDGE_ZONE * height, (1 - EDGE_ZONE) * height
    header = [w for w in words if w["bottom"] <= top_cut]
    footer = [w for w in words if w["top"] >= bottom_cut]
    edge_ids = {id(w) for w in header} | {id(w) for w in footer}
    body = [w for w in words if id(w) not in edge_ids]
    gutters = find_gutters(body, width)
    edges = [0.0, *gutters, width + 1.0]
    columns = [
        [w for w in body if edges[i] <= (w["x0"] + w["x1"]) / 2 < edges[i + 1]]
        for i in range(len(edges) - 1)
    ]
    parts = [_group_text(g, body_size) for g in [header, *columns, footer] if g]
    return "\n\n".join(p for p in parts if p.strip())


# ---------------------------------------------------------------------------- OCR
def _ocr_page(path: Path, number: int, doc: Document) -> str:
    try:
        import pytesseract
        from pdf2image import convert_from_path

        images = convert_from_path(str(path), dpi=OCR_DPI, first_page=number, last_page=number)
        text = "\n".join(pytesseract.image_to_string(img, lang="eng") for img in images)
    except Exception as exc:
        doc.problems.append(f"page {number}: OCR failed ({exc!r})")
        return ""
    if _useful_chars(text) < MIN_TEXT_CHARS:
        doc.problems.append(f"page {number}: OCR found almost no text (blank or image-only page)")
    return text


# -------------------------------------------------------------------- Markdown/HTML
def strip_front_matter(text: str) -> str:
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return "\n".join(lines[i + 1 :]).lstrip("\n")
    return text


class _HtmlText(HTMLParser):
    """Visible text with headings as '# ' lines and tables as 'a | b' rows."""

    SKIP = {"script", "style", "nav", "header", "footer", "noscript", "form", "svg", "head"}
    BLOCK = {"p", "div", "li", "br", "section", "article", "ul", "ol", "tr", "table"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip = 0
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self._skip += 1
        if self._skip:
            return
        if re.fullmatch(r"h[1-6]", tag):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if re.fullmatch(r"h[1-6]", tag):
            self.out.append("\n\n")
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(self._row):
                self.out.append("\n" + " | ".join(self._row) + "\n")
            self._row = None
        elif tag == "table":
            self.out.append("\n\n")
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._cell is not None:
            self._cell.append(data)
        else:
            self.out.append(data)


def html_to_text(html: str) -> str:
    parser = _HtmlText()
    parser.feed(html)
    text = "".join(parser.out)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
