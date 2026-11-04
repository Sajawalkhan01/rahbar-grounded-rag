"""A small BM25 index, held in memory, for the keyword half of hybrid retrieval.

Why it exists: embeddings blur exact terms. "clause 1.21", "CS-4032" or "10,000" mean
little to a sentence embedding, but they are rare, exact tokens that BM25 scores highly.
The API builds this index from the chunk text it reads out of Chroma (read-only), so the
vector database stays the single source of truth and nothing here is written back.
"""

from __future__ import annotations

import math
import re
from collections import Counter

# Numbers (with thousands commas or decimals) and words, keeping dotted/dashed codes whole.
_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?|[a-z0-9]+(?:[.\-/][a-z0-9]+)*")
_STOP = frozenset(
    "a an and are as at be by for from has have how i in is it its of on or that the this to "
    "was what when where which who will with do does can my me student students".split()
)


def tokenize(text: str) -> list[str]:
    """Lower-case tokens. A compound such as 'cs-4032' or '1.21' is kept whole AND split,
    so 'CS 4032' still matches. '10,000' also yields '10000'."""
    out: list[str] = []
    for tok in _TOKEN.findall(text.lower()):
        tok = tok.rstrip(".,")
        if not tok or tok in _STOP:
            continue
        out.append(tok)
        if "," in tok and tok.replace(",", "").isdigit():
            out.append(tok.replace(",", ""))
        elif re.search(r"[.\-/]", tok) and not re.fullmatch(r"\d+\.\d+", tok):
            out.extend(p for p in re.split(r"[.\-/]", tok) if p and p not in _STOP)
    return out


def exact_terms(text: str) -> list[str]:
    """Tokens that behave like codes: a clause number (1.21), a decimal threshold (2.00), an
    amount (10,000), a course code (cs-4032), a year range (2023-24). A question that contains
    one is asking for THAT string, which an embedding cannot be trusted to find."""
    found: list[str] = []
    for tok in _TOKEN.findall(text.lower()):
        tok = tok.rstrip(".,")
        if re.search(r"\d", tok) and re.search(r"[.\-/,]", tok) and tok not in found:
            found.append(tok)
    return found


class BM25:
    def __init__(self, documents: list[str], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self._docs = [Counter(tokenize(d)) for d in documents]
        self._lengths = [sum(c.values()) for c in self._docs]
        self._avg = (sum(self._lengths) / len(self._lengths)) if self._lengths else 0.0
        df: Counter[str] = Counter()
        for counts in self._docs:
            df.update(counts.keys())
        n = len(self._docs)
        self._idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def __len__(self) -> int:
        return len(self._docs)

    def scores(self, query: str) -> list[float]:
        terms = set(tokenize(query))
        result = []
        for counts, length in zip(self._docs, self._lengths, strict=True):
            score = 0.0
            for term in terms:
                tf = counts.get(term)
                if not tf:
                    continue
                norm = tf + self.k1 * (1 - self.b + self.b * length / (self._avg or 1.0))
                score += self._idf[term] * tf * (self.k1 + 1) / norm
            result.append(score)
        return result

    def top(self, query: str, n: int, allowed: list[bool] | None = None) -> list[tuple[int, float]]:
        """(document index, score) for the best n documents with a positive score."""
        ranked = sorted(
            ((i, s) for i, s in enumerate(self.scores(query)) if s > 0 and (allowed is None or allowed[i])),
            key=lambda pair: pair[1],
            reverse=True,
        )
        return ranked[:n]
