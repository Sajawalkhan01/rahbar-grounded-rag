"""Citation check (section 3.4): every citation must match a chunk retrieved for THIS request.

A citation the retriever never returned is fabricated, and the handout deducts for it. The
model's citations are therefore never passed through: each one is matched against the
retrieved passages, canonicalised to the passage's own spelling, and dropped if no passage
matches. The number dropped is reported (`invalid_citations_removed`) so the evaluation can
measure how often the model invents one.
"""

from __future__ import annotations

import re

from ..schemas import Citation, Passage


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.casefold()).strip()


def match_passage(citation: Citation, passages: list[Passage]) -> Passage | None:
    """The retrieved passage this citation points at, or None.

    Source and section must match (ignoring case and extra spaces). The page must match when
    both sides have one: a right section on the wrong page is still a wrong citation.
    """
    for p in passages:
        if _norm(p.source) != _norm(citation.source) or _norm(p.section) != _norm(citation.section):
            continue
        if citation.page is not None and p.page is not None and citation.page != p.page:
            continue
        return p
    return None


def validate_citations(
    citations: list[Citation], passages: list[Passage]
) -> tuple[list[Citation], int]:
    """(valid citations in the passages' own spelling, number removed). Duplicates are merged."""
    valid: list[Citation] = []
    seen: set[tuple[str, str, int | None]] = set()
    removed = 0
    for c in citations:
        p = match_passage(c, passages)
        if p is None:
            removed += 1
            continue
        key = (p.source, p.section, p.page)
        if key not in seen:
            seen.add(key)
            valid.append(Citation(source=p.source, section=p.section, page=p.page))
    return valid, removed
