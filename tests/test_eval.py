"""The evaluation metrics and the CI gate (section 5). Made-up responses, no API needed."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "run_eval", Path(__file__).resolve().parents[1] / "eval" / "run_eval.py"
)
assert SPEC and SPEC.loader
ev = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ev)


def ref(source: str, section: str, page: int | None = 1) -> dict:
    return {"source": source, "section": section, "page": page}


# ------------------------------------------------------------------ recall@k
def test_recall_counts_relevant_passages_found_in_the_top_k() -> None:
    relevant = [{"source": "a.md", "section": "1. A"}, {"source": "b.md", "section": "2. B"}]
    retrieved = [ref("a.md", "1. A"), ref("x.md", "9. X"), ref("b.md", "2. B")]
    assert ev.recall_at_k(relevant, retrieved, 3) == 1.0
    assert ev.recall_at_k(relevant, retrieved, 2) == 0.5
    assert ev.recall_at_k(relevant, retrieved, 1) == 0.5


def test_recall_is_none_when_nothing_is_relevant() -> None:
    assert ev.recall_at_k([], [ref("a.md", "1")], 5) is None


def test_recall_matches_a_section_by_prefix_but_not_by_a_longer_number() -> None:
    rel = [{"source": "r.pdf", "section": "5.4"}]
    assert ev.recall_at_k(rel, [ref("r.pdf", "5.4 Academic Warning")], 5) == 1.0
    assert ev.recall_at_k(rel, [ref("r.pdf", "5.45 Something else")], 5) == 0.0
    assert ev.recall_at_k(rel, [ref("other.pdf", "5.4 Academic Warning")], 5) == 0.0


def test_recall_matches_pdf_pages_and_any_listed_page_counts() -> None:
    rel = [{"source": "h.pdf", "pages": [24, 25]}]
    assert ev.recall_at_k(rel, [ref("h.pdf", "x", 25)], 5) == 1.0
    assert ev.recall_at_k(rel, [ref("h.pdf", "x", 26)], 5) == 0.0


# -------------------------------------------------------------- citation validity
def test_citation_validity_checks_every_citation_against_the_retrieved_list() -> None:
    response = {
        "retrieved": [ref("a.md", "1. A", 1)],
        "citations": [{"source": "a.md", "section": "1. A", "page": 1},
                      {"source": "ghost.md", "section": "9", "page": 1}],
    }
    assert ev.citation_validity(response) == 0.5


def test_citation_validity_counts_citations_the_api_had_to_remove() -> None:
    response = {"retrieved": [ref("a.md", "1. A")],
                "citations": [{"source": "a.md", "section": "1. A", "page": 1}],
                "invalid_citations_removed": 3}
    assert ev.citation_validity(response) == pytest.approx(0.25)


def test_citation_validity_is_none_without_citations() -> None:
    assert ev.citation_validity({"retrieved": [], "citations": []}) is None


def test_citation_with_the_wrong_page_is_invalid() -> None:
    response = {"retrieved": [ref("a.md", "1. A", 2)],
                "citations": [{"source": "a.md", "section": "1. A", "page": 7}]}
    assert ev.citation_validity(response) == 0.0


# ------------------------------------------------------------------- summary + gate
def row(qid: str, qtype: str, relevant: list[dict], retrieved: list[dict], answered: bool = True,
        abstain: bool = False, citations: list[dict] | None = None) -> dict:
    return {"id": qid, "type": qtype, "must_abstain": abstain, "relevant": relevant,
            "response": {"retrieved": retrieved, "answered_from_context": answered,
                         "citations": citations or []}}


def sample_rows() -> list[dict]:
    rel = [{"source": "a.md", "section": "1. A"}]
    hit = [ref("a.md", "1. A")]
    miss = [ref("z.md", "9")] * 5
    return [
        row("o1", "ordinary", rel, hit),
        row("o2", "ordinary", rel, miss),
        row("e1", "exact", rel, miss),
        row("a1", "abstain", [], miss, answered=False, abstain=True),
    ]


def test_summary_reports_recall_by_type_and_abstention() -> None:
    s = ev.summarise(sample_rows())
    assert s["recall_at_5"] == pytest.approx(1 / 3, abs=1e-3)
    assert s["recall_at_5_by_type"] == {"exact": 0.0, "ordinary": 0.5}
    assert s["abstention_correctness"] == 1.0 and s["must_abstain_answered"] == []


def test_summary_lists_must_abstain_questions_that_were_answered() -> None:
    rows = sample_rows()
    rows[3]["response"]["answered_from_context"] = True
    s = ev.summarise(rows)
    assert s["must_abstain_answered"] == ["a1"] and s["abstention_correctness"] == 0.0


def test_summary_flags_answerable_questions_that_were_declined() -> None:
    rows = sample_rows()
    rows[0]["response"]["answered_from_context"] = False
    assert ev.summarise(rows)["false_abstentions"] == ["o1"]


def good() -> dict:
    return {"recall_at_5": 0.8, "citation_validity": 1.0, "abstention_correctness": 1.0,
            "must_abstain_answered": []}


def test_gate_passes_when_nothing_regressed() -> None:
    assert ev.gate(good(), {"recall_at_5": 0.8}) == []


def test_gate_fails_when_recall_drops_more_than_five_points() -> None:
    worse = {**good(), "recall_at_5": 0.74}
    assert any("recall@5" in m for m in ev.gate(worse, {"recall_at_5": 0.8}))


def test_gate_tolerates_a_drop_of_exactly_five_points() -> None:
    assert ev.gate({**good(), "recall_at_5": 0.75}, {"recall_at_5": 0.8}) == []


def test_gate_fails_when_a_must_abstain_question_is_answered() -> None:
    bad = {**good(), "abstention_correctness": 0.5, "must_abstain_answered": ["a2"]}
    assert any("a2" in m for m in ev.gate(bad, None))


def test_gate_fails_when_citation_validity_is_below_95_percent() -> None:
    assert any("citation validity" in m for m in ev.gate({**good(), "citation_validity": 0.94}, None))
    assert ev.gate({**good(), "citation_validity": 0.95}, None) == []


def test_gate_without_a_baseline_only_checks_the_absolute_rules() -> None:
    assert ev.gate({**good(), "recall_at_5": 0.0}, None) == []


def test_golden_sets_are_well_formed() -> None:
    root = Path(__file__).resolve().parents[1] / "eval"
    questions = ev.load_golden(root / "golden.yaml")
    types = [q["type"] for q in questions]
    assert len(questions) >= 15
    assert {t: types.count(t) for t in set(types)} == {
        "ordinary": 10, "version": 3, "exact": 3, "abstain": 3, "injection": 2}
    assert len({q["id"] for q in questions}) == len(questions)
    for q in questions:
        assert (q["type"] == "abstain") == bool(q.get("must_abstain"))
        assert (q["type"] == "abstain") == (not q["relevant"])
    fixture = ev.load_golden(root / "fixtures" / "golden.fixture.yaml")
    assert len(fixture) == 6
