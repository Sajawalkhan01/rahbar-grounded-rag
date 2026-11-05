"""Decides whether a question is about an OLDER version of a document.

By default retrieval excludes chunks whose metadata says `superseded: true` (failure mode E:
the retriever faithfully returns the wrong year). The exception is a user who asks about the
old rules on purpose: "what was the attendance rule in 2023?", "what changed?".

The rule is deliberately simple and testable: either the question says it wants older
material ("previous", "old", "superseded", "compare"...), or it names a version that belongs to
a superseded document. A year range such as "2023-24" names a document version in its own
right (the current annual report), so it is removed before looking for a bare year: "2023-24
annual report" is not a request for the 2023 rules, "the 2023 rules" is.
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
_YEAR_RANGE = re.compile(r"(?<!\d)(?:19|20)\d{2}\s*[-/]\s*(?:\d{4}|\d{2})(?!\d)")


def _years(version: str) -> set[str]:
    return set(_YEAR.findall(version))


def wants_older_versions(
    question: str, superseded_versions: set[str], current_versions: set[str]
) -> bool:
    """True when the question should also search superseded documents."""
    if _OLDER_WORDS.search(question):
        return True
    q = question.lower()
    # A named superseded version ("2019-20", "2023-08") is an explicit request for it.
    if any(v.lower() in q for v in superseded_versions if len(v) > 3):
        return True
    # A named CURRENT version ("2023-24") is a request for that one: do not read its year as old.
    remaining = q
    for v in current_versions:
        if len(v) > 3:
            remaining = remaining.replace(v.lower(), " ")
    remaining = _YEAR_RANGE.sub(" ", remaining)
    old_years = set().union(*(_years(v) for v in superseded_versions), set())
    return bool(set(_YEAR.findall(remaining)) & old_years)
