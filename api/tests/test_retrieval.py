"""BM25, version filtering and hybrid retrieval (sections 3.2 and 4.4)."""

from __future__ import annotations

import pytest
from app.retrieval.bm25 import BM25, tokenize
from app.retrieval.chroma import ChromaRetriever
from app.retrieval.versions import wants_older_versions

from tests.fakes import FakeChroma, FakeEmbedder, meta, settings


def test_tokenizer_keeps_exact_terms_whole_and_split() -> None:
    toks = tokenize("Clause 1.21 and CS-4032 cost 10,000 rupees")
    assert "1.21" in toks and "cs-4032" in toks and "4032" in toks
    assert "10,000" in toks and "10000" in toks


def test_bm25_ranks_the_exact_clause_first() -> None:
    docs = ["attendance rules and general policy", "1.21 attendance at least 80 percent",
            "fees tuition per credit hour"]
    top = BM25(docs).top("clause 1.21", 3)
    assert top and top[0][0] == 1


def test_bm25_ignores_documents_that_are_not_allowed() -> None:
    bm = BM25(["clause 1.21 old rule", "clause 1.21 new rule"])
    assert [i for i, _ in bm.top("1.21", 5, allowed=[False, True])] == [1]


@pytest.mark.parametrize(
    "question, expected",
    [
        ("What is the attendance requirement?", False),
        ("What was the attendance rule in 2023?", True),     # a year only the old version has
        ("How did the previous policy treat repeats?", True),
        ("What changed between the old and new rules?", True),
        ("Annual report 2023-24 faculty count", False),      # 2023 also appears in a CURRENT version
        ("What does the 2019-20 report say about graduates?", True),
    ],
)
def test_wants_older_versions(question: str, expected: bool) -> None:
    assert wants_older_versions(question, {"2023-08", "2019-20"}, {"2026-06", "2023-24"}) is expected


def _retriever(**overrides: object) -> tuple[ChromaRetriever, FakeChroma]:
    chroma = FakeChroma()
    col = chroma.make()
    col.add_chunk("new-1", "Attendance: a student must attend 80 percent of lectures.",
                  meta("rules-2026.pdf", "1. Attendance", 3, "2026-06", False))
    col.add_chunk("old-1", "Attendance: a student must attend 75 percent of lectures.",
                  meta("rules-2023.pdf", "1. Attendance", 2, "2023-08", True))
    col.add_chunk("fee-1", "Late registration fee is 3,000 rupees per course.",
                  meta("fees.html", "2. Other charges", 1, "2026-27", False))
    col.add_chunk("cl-1", "1.21 Withdrawal: the dean may approve a withdrawal after week ten.",
                  meta("rules-2026.pdf", "1.21 Withdrawal", 9, "2026-06", False))
    r = ChromaRetriever(settings(**overrides), embedder=FakeEmbedder(), client=chroma)
    return r, chroma


def test_superseded_documents_are_excluded_by_default() -> None:
    r, _ = _retriever()
    hits = r.retrieve("How many percent attendance must a student have?", k=5)
    assert hits and all(not p.superseded for p in hits)
    assert hits[0].chunk_id == "new-1"


def test_older_version_is_included_when_the_user_asks_for_it() -> None:
    r, _ = _retriever()
    hits = r.retrieve("What was the attendance requirement in 2023?", k=5)
    assert "old-1" in [p.chunk_id for p in hits]


def test_turning_the_filter_off_lets_outdated_chunks_through() -> None:
    """This is failure mode E (docs/FAILURE-ANALYSIS.md): the guard is the metadata filter."""
    r, _ = _retriever(filter_superseded=False)
    hits = r.retrieve("How many percent attendance must a student have?", k=5)
    assert "old-1" in [p.chunk_id for p in hits]


def test_scores_are_cosine_similarities_and_metadata_is_carried() -> None:
    r, _ = _retriever()
    p = r.retrieve("late registration fee", k=3)[0]
    assert p.chunk_id == "fee-1" and 0.0 < p.score <= 1.0
    assert (p.source, p.section, p.page, p.doc_version) == ("fees.html", "2. Other charges", 1, "2026-27")


def test_hybrid_finds_an_exact_clause_the_dense_search_ranks_low() -> None:
    """Dense vectors blur '1.21'; BM25 does not. With k=1 only the fused winner survives."""
    r, _ = _retriever(hybrid=True, candidate_pool=4)
    hits = r.retrieve("clause 1.21", k=1)
    assert hits[0].chunk_id == "cl-1"


def test_keyword_only_hits_still_get_a_dense_similarity() -> None:
    r, _ = _retriever(hybrid=True, candidate_pool=1)
    hits = r.retrieve("clause 1.21 withdrawal", k=3)
    assert all(0.0 <= p.score <= 1.0 for p in hits)


def test_index_snapshot_follows_the_index_id() -> None:
    r, chroma = _retriever()
    r.retrieve("attendance", k=1)
    col = chroma.collections["rahbar"]
    col.add_chunk("new-2", "Brand new chunk about hostels.", meta("x.pdf", "9. Hostels"))
    col.metadata["index_id"] = "idx-2"
    r._checked_at = 0.0                      # the refresh interval has passed
    assert "new-2" in [p.chunk_id for p in r.retrieve("hostels", k=5)]
    assert r.index_id() == "idx-2"


def test_exact_terms_finds_codes_but_not_plain_words_or_bare_years() -> None:
    from app.retrieval.bm25 import exact_terms

    assert exact_terms("clause 1.21 and CS-4032 cost 10,000 or 2.00") == ["1.21", "cs-4032", "10,000", "2.00"]
    assert exact_terms("what is the attendance rule in 2023") == []


def test_an_exact_clause_beats_passages_found_by_both_lists() -> None:
    """Regression: with plain rank fusion, the passage that holds clause 1.21 was found only by
    the keyword search and lost to passages the two lists both ranked mid-way."""
    chroma = FakeChroma()
    col = chroma.make()
    for i in range(12):
        col.add_chunk(f"gen-{i}", f"academic rules clause general attendance policy part {i} of the rules",
                      meta("rules.pdf", f"{i}. General", i + 1))
    col.add_chunk("target", "1.21 Students are required to maintain 100% attendance",
                  meta("rules.pdf", "Attendance Requirements", 10))
    r = ChromaRetriever(settings(candidate_pool=15), embedder=FakeEmbedder(), client=chroma)
    ids = [p.chunk_id for p in r.retrieve("What does clause 1.21 of the academic rules require?", k=3)]
    assert "target" in ids


def test_a_bare_year_of_a_superseded_document_asks_for_the_old_version_even_if_a_current_one_shares_it() -> None:
    old, current = {"2023-08", "2019-20"}, {"2026-06", "2023-24"}
    assert wants_older_versions("According to the 2023 academic rules, how many credit hours?", old, current)
    assert not wants_older_versions("faculty publications in the 2023-24 annual report", old, current)
    assert wants_older_versions("faculty publications in the 2019-20 annual report", old, current)
