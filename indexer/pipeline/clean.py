"""Stage 2: Clean. Remove artefacts without destroying structure.

Rules (also listed in docs/PIPELINE.md):
  1. Unicode NFKC, so ligatures ("ﬁ") and non-breaking spaces become plain text.
  2. Drop control characters, soft hyphens and zero-width characters.
  3. Dot leaders ("Fees ........ 12") collapse to one space.
  4. Repeated runs of spaces become one space; three or more newlines become two.
  5. A word split by a line-end hyphen is joined ("exam-\\nination" -> "examination"),
     but only when the next line continues in lower case, so "COVID-\\n19" is untouched.
  6. Headers and footers: a line (digits ignored) that appears in the top or bottom three
     lines of at least half of the pages is removed from those places. So are bare page
     numbers ("12", "Page 12 of 53", "- 12 -") at the edge of a page.

Deliberately NOT stripped: lines that look like headings (numbered clauses, ALL CAPS
titles) and table rows ("a | b | c"), including rows that are only numbers. Chunking and
citations depend on the headings; a fee table is its numbers.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

from .models import Document, Page

EDGE_LINES = 3                  # how many lines at the top and bottom of a page can be a header/footer
REPEAT_SHARE = 0.5              # in what share of the pages a line must appear to be a header/footer
MIN_PAGES_FOR_REPEATS = 4       # with fewer pages "repeats on most pages" means nothing

_PAGE_NUMBER = re.compile(
    r"^(?:page\s*)?\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?$|^[-–—]\s*\d{1,4}\s*[-–—]$", re.I
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f­​-‏﻿]")


def normalise_text(text: str) -> str:
    """Rules 1 to 5 for one page's text."""
    text = unicodedata.normalize("NFKC", text)
    text = _CONTROL.sub("", text).replace("\xa0", " ")
    text = re.sub(r"\.{4,}|(?:\. ){4,}", " ", text)                 # dot leaders
    text = re.sub(r"(\w)-\n([a-z])", r"\1\2", text)                  # hyphenated line breaks
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _key(line: str) -> str:
    """A line with its digits blanked, so 'Page 3' and 'Page 4' count as the same line."""
    return re.sub(r"\d+", "#", line.strip().lower())


def _edge_indexes(lines: list[str]) -> list[int]:
    """Positions where a header or footer can be: the first and last few non-empty lines.
    A page with fewer than 2 * EDGE_LINES lines has no real "middle", so only its very first
    and very last line count; otherwise body text could be mistaken for a header."""
    non_empty = [i for i, ln in enumerate(lines) if ln.strip()]
    if len(non_empty) < 2 * EDGE_LINES + 1:
        return non_empty[:1] + non_empty[-1:]
    return non_empty[:EDGE_LINES] + non_empty[-EDGE_LINES:]


def _is_table_row(line: str) -> bool:
    return " | " in line


def repeated_edge_keys(pages: list[list[str]]) -> set[str]:
    """Keys of lines that sit at the top or bottom of at least half of the pages."""
    if len(pages) < MIN_PAGES_FOR_REPEATS:
        return set()
    counts: Counter[str] = Counter()
    for lines in pages:
        seen = {_key(lines[i]) for i in _edge_indexes(lines) if not _is_table_row(lines[i])}
        counts.update(k for k in seen if k and len(k) > 3)
    need = REPEAT_SHARE * len(pages)
    return {k for k, n in counts.items() if n >= need}


def clean(doc: Document) -> Document:
    """Strip headers and footers that repeat on every page; keep headings and
    section boundaries, because chunking and citations depend on them."""
    lines_per_page = [normalise_text(p.text).split("\n") for p in doc.pages]
    repeats = repeated_edge_keys(lines_per_page)
    cleaned: list[Page] = []
    for page, lines in zip(doc.pages, lines_per_page, strict=True):
        drop = set()
        for i in _edge_indexes(lines):
            line = lines[i].strip()
            if _is_table_row(line):
                continue
            if _PAGE_NUMBER.match(line) or _key(line) in repeats:
                drop.add(i)
        kept = [ln for i, ln in enumerate(lines) if i not in drop]
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
        cleaned.append(Page(page.source, page.page, text, page.ocr))
    return Document(doc.source, doc.doc_version, doc.superseded, cleaned, list(doc.problems))
