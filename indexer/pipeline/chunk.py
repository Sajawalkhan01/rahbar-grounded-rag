"""Stage 3: Chunk. Structure-aware, with overlap.

Size is counted in WORDS (whitespace-separated), not model tokens: it needs no tokenizer,
is easy to explain, and a word is roughly 1.3 nomic-embed-text tokens. docs/PIPELINE.md
says so wherever it quotes a size.

How a document becomes chunks:
  1. Walk the pages line by line and find headings. The current heading is the chunk's
     `section` metadata, and it carries across page breaks.
  2. Group the lines under a heading into blocks: paragraphs (a numbered clause starts a new
     one) and tables (consecutive "a | b" rows).
  3. Pack whole blocks into chunks up to `size` words. A paragraph is never cut unless it is
     longer than `size` by itself, so a long definition stays in one piece.
  4. A block longer than `size` is split by sentences; a table is split by rows with the
     header row repeated; a single sentence longer than `size` is split by words.
  5. Neighbouring chunks of the same section share `overlap` words (whole sentences where
     possible). A section boundary is a hard stop: nothing overlaps across headings.
Every chunk starts with its heading so the embedding and the BM25 index both see it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Chunk, Document

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(\"'])")
_NUMBERED_TITLE = re.compile(r"^(\d+(?:\.\d+){0,3})\.?\s+([A-Z][^|]{2,90})$")
_KEYWORD_TITLE = re.compile(r"^(chapter|section|article|part|appendix|annex(?:ure)?)\s+[\w.-]+", re.I)
_CLAUSE_START = re.compile(r"^(\d+(?:\.\d+)+\.?|\(?[a-z0-9]{1,3}\)|[-•*])\s+\S")
_MD_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*$")

DEFAULT_SECTION = "Introduction"
MAX_HEADING_WORDS = 12


def words(text: str) -> int:
    return len(text.split())


_SMALL_WORDS = {"of", "and", "the", "to", "for", "in", "on", "a", "an", "or", "with", "at", "by"}


def _looks_like_title(text: str) -> bool:
    """A real title ("Academic Warning/Probation") has capitalised content words. A wrapped line
    of a clause ("Students are required to maintain 100% attendance in") does not, and must not
    become a section name."""
    content = [w for w in text.split() if w.lower() not in _SMALL_WORDS and any(c.isalpha() for c in w)]
    if not content:
        return False
    capitalised = sum(1 for w in content if w[0].isupper())
    return capitalised / len(content) >= 0.7


def heading_of(line: str) -> str | None:
    """The heading text if `line` is a heading, else None.

    Recognised: Markdown headings; short numbered titles ("5.4 Academic Warning", no full
    stop at the end); "Chapter 3 ..." / "Appendix A" style lines; short ALL-CAPS lines.
    """
    s = line.strip()
    if not s or " | " in s:
        return None
    md = _MD_HEADING.match(s)
    if md:
        return md.group(1).strip()
    if words(s) > MAX_HEADING_WORDS:
        return None
    if _KEYWORD_TITLE.match(s) and not s.endswith((".", ",", ";")):
        return s
    m = _NUMBERED_TITLE.match(s)
    if (m and not s.endswith((".", ",", ";", ":")) and not re.search(r"\d{3,}$", s)
            and _looks_like_title(m.group(2))):
        return s
    letters = [c for c in s if c.isalpha()]
    if len(letters) >= 4 and words(s) <= 8 and all(c.isupper() for c in letters):
        return s
    return None


@dataclass
class _Block:
    section: str
    page: int
    text: str
    is_table: bool = False


def _blocks(doc: Document) -> list[_Block]:
    blocks: list[_Block] = []
    section = DEFAULT_SECTION
    for page in doc.pages:
        current: list[str] = []
        table = False
        start_page = page.page

        def flush() -> None:
            nonlocal current, table
            if current:
                text = "\n".join(current) if table else " ".join(current)
                blocks.append(_Block(section, start_page, text, table))
            current, table = [], False

        for raw in page.text.split("\n"):
            line = raw.strip()
            if not line:
                flush()
                continue
            heading = heading_of(line)
            if heading:
                flush()
                section = heading
                continue
            is_row = " | " in line
            if is_row != table:
                flush()
            if not current:
                start_page = page.page
            elif not is_row and _CLAUSE_START.match(line):
                flush()
                start_page = page.page
            table = is_row
            current.append(line)
        flush()
    return blocks


def _split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_END.split(text) if s.strip()]


def _split_long_text(text: str, budget: int) -> list[str]:
    """Split one over-long paragraph by sentences; a sentence that is itself too long is
    split by words (the last resort)."""
    pieces: list[str] = []
    current: list[str] = []
    count = 0
    for sentence in _split_sentences(text):
        n = words(sentence)
        if n > budget:
            if current:
                pieces.append(" ".join(current))
                current, count = [], 0
            tokens = sentence.split()
            pieces.extend(" ".join(tokens[i : i + budget]) for i in range(0, len(tokens), budget))
            continue
        if count + n > budget and current:
            pieces.append(" ".join(current))
            current, count = [], 0
        current.append(sentence)
        count += n
    if current:
        pieces.append(" ".join(current))
    return pieces


def _split_table(text: str, budget: int) -> list[str]:
    rows = text.split("\n")
    header, body = rows[0], rows[1:]
    if words(text) <= budget or not body:
        return [text]
    pieces: list[str] = []
    current = [header]
    count = words(header)
    for row in body:
        n = words(row)
        if count + n > budget and len(current) > 1:
            pieces.append("\n".join(current))
            current, count = [header], words(header)
        current.append(row)
        count += n
    if len(current) > 1:
        pieces.append("\n".join(current))
    return pieces


def _tail(text: str, overlap: int) -> str:
    """The end of `text`, about `overlap` words, made of whole sentences when it can."""
    if overlap <= 0:
        return ""
    picked: list[str] = []
    count = 0
    for sentence in reversed(_split_sentences(text.replace("\n", " "))):
        n = words(sentence)
        if count + n > overlap:
            break
        picked.insert(0, sentence)
        count += n
    if picked:
        return " ".join(picked)
    return " ".join(text.split()[-overlap:])


def _slug(name: str) -> str:
    stem = name.rsplit(".", 1)[0]
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[:60] or "doc"


def chunk(doc: Document, size: int, overlap: int) -> list[Chunk]:
    """Split on sections, then paragraphs, then sentences, keeping each chunk under
    `size` words, with `overlap` words shared between neighbours. Every chunk keeps
    the section heading it sits under, its page, doc_version and superseded."""
    if size < 20:
        raise ValueError("chunk size must be at least 20 words")
    if not 0 <= overlap < size // 2:
        raise ValueError("overlap must be at least 0 and less than half of the chunk size")

    # Group consecutive blocks that share a section; a new section starts a new group.
    groups: list[list[_Block]] = []
    for block in _blocks(doc):
        if groups and groups[-1][0].section == block.section:
            groups[-1].append(block)
        else:
            groups.append([block])

    chunks: list[Chunk] = []
    slug = _slug(doc.source)

    def emit(section: str, page: int, body: str) -> None:
        text = body if section == DEFAULT_SECTION else f"{section}\n{body}"
        chunks.append(Chunk(f"{slug}-{len(chunks):04d}", text, doc.source, section, page,
                            doc.doc_version, doc.superseded))

    for group in groups:
        section = group[0].section
        budget = max(20, size - words(section))
        pieces: list[tuple[str, int, bool]] = []   # (text, page, is_table)
        # Leave room for the overlap tail when splitting, so tail + piece still fits `size`.
        room = max(20, budget - overlap)
        for block in group:
            if block.is_table:
                pieces.extend((t, block.page, True) for t in _split_table(block.text, room))
            elif words(block.text) > budget:
                pieces.extend((t, block.page, False) for t in _split_long_text(block.text, room))
            else:
                pieces.append((block.text, block.page, False))

        current: list[str] = []
        count = 0
        first_page = group[0].page
        carried = 0          # words at the start of `current` that are overlap from before
        for text, page, _is_table in pieces:
            n = words(text)
            if current and count > carried and count + n > budget:
                body = "\n".join(current)
                emit(section, first_page, body)
                tail = _tail(body, overlap)
                current = [tail] if tail else []
                count = carried = words(tail)
                first_page = page
            if carried and len(current) == 1 and count + n > budget:
                current, count, carried = [], 0, 0   # the tail would push this piece over `size`
            if len(current) == (1 if carried else 0):
                first_page = page
            current.append(text)
            count += n
        if current and (count > carried or not chunks):
            emit(section, first_page, "\n".join(current))
    return chunks
