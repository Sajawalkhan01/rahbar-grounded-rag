"""Follow-up detection (section 3.2 step 2: "skip the cache for follow-up questions").

A follow-up ("and for labs?", "what about that?") only makes sense with the earlier turn, so
its answer depends on the conversation, and a cached copy would be served to someone whose
conversation is different. The rule is a cheap heuristic: there IS an earlier turn in this
session, and the question either opens like a continuation or is short and leans on a pronoun.
It errs on the side of "follow-up", because the cost of a wrong guess is one skipped cache hit,
while the cost of the opposite error is a wrong answer served from the cache.
"""

from __future__ import annotations

import re

_OPENERS = re.compile(
    r"^\s*(and|also|but|then|so|what about|how about|what if|why|how come|ok(?:ay)?\b|thanks|"
    r"can you (?:explain|elaborate|clarify)|explain (?:that|it|more)|tell me more|more details)",
    re.I,
)
_PRONOUNS = re.compile(
    r"\b(it|its|that|this|those|these|they|them|their|he|she|his|her|the same|the above|"
    r"the latter|the former|there)\b",
    re.I,
)
SHORT = 8   # words


def is_followup(question: str, has_previous_turn: bool) -> bool:
    if not has_previous_turn:
        return False
    if _OPENERS.match(question):
        return True
    return len(question.split()) <= SHORT and bool(_PRONOUNS.search(question))
