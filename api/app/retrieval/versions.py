"""Decides whether a question is about an OLDER version of a document.

By default retrieval excludes chunks whose metadata says `superseded: true` (failure mode E:
the retriever faithfully returns the wrong year). The exception is a user who asks about the
old rules on purpose: "what was the attendance rule in 2023?", "what changed?".

The rule is deliberately simple and testable: either the question says it wants older
material ("previous", "old", "superseded", "compare"...), or it names a version that only a
superseded document has (a year that no current document carries).
"""

from __future__ import annotations

import re

_OLDER_WORDS = re.compile(
    r"\b(older|old|previous(?:ly)?|earlier|former(?:ly)?|prior|past|superseded|repealed|outdated|"
    r"original|historic(?:al)?|used to|was|were|changed?|differen(?:ce|t)|compare|comparison|"
    r"versus|vs)\b",
    re.I,
)
_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")


def _years(version: str) -> set[str]:
    return set(_YEAR.findall(version))


def wants_older_versions(
    question: str, superseded_versions: set[str], current_versions: set[str]
) -> bool:
    """True when the question should also search superseded documents."""
    if _OLDER_WORDS.search(question):
        return True
    only_old_years = set().union(*(_years(v) for v in superseded_versions), set())
    only_old_years -= set().union(*(_years(v) for v in current_versions), set())
    asked_years = set(_YEAR.findall(question))
    if asked_years & only_old_years:
        return True
    q = question.lower()
    return any(v.lower() in q for v in superseded_versions if len(v) > 3)
