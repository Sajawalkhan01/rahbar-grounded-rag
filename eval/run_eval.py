#!/usr/bin/env python3
"""Score a running RAHBAR against a golden set, and gate CI on the result (section 5).

    python eval/run_eval.py --api http://<ingress>                 # your corpus
    python eval/run_eval.py --api ... --golden eval/fixtures/golden.fixture.yaml
    python eval/run_eval.py --api ... --update-baseline            # after a deliberate improvement

Exit code 0 when every gate passes, 1 when any fails (2 when the responses could not be
scored). CI relies on that exit code.

The scoring functions (recall_at_k, citation_validity, summarise, gate) are the evaluation;
tests/test_eval.py covers them with made-up responses.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx
import yaml

HERE = Path(__file__).resolve().parent

# The gate from section 5.4. Change these only with a reason written in docs/PIPELINE.md.
MAX_RECALL5_DROP = 0.05        # recall@5 may fall at most 5 points below the baseline
MIN_CITATION_VALIDITY = 0.95   # at least 95% of citations must match retrieved passages
REQUIRED_ABSTENTION = 1.0      # every must-abstain question must be declined


def load_golden(path: Path) -> list[dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    questions = data.get("questions") or []
    if not questions:
        sys.exit(f"no questions in {path}")
    return questions


def ask(client: httpx.Client, api: str, question: str) -> dict:
    """POST one question and return the AskResponse as a dict (see api/app/schemas.py)."""
    response = client.post(f"{api.rstrip('/')}/api/ask", json={"question": question})
    response.raise_for_status()
    return response.json()


def _matches(rel: dict, got: dict) -> bool:
    """Does one retrieved passage satisfy one relevant entry?

    Same `source`, and then either the passage's page is one of the entry's `pages`
    (PDFs: a fact can straddle a page break, so any listed page counts) or the passage's
    `section` starts with the entry's `section` (HTML and Markdown: headings are exact).
    The match is by (source, page or section), not by chunk id, so it survives re-chunking:
    a golden set tied to chunk ids would break every time the chunk size changes.
    """
    if rel.get("source") != got.get("source"):
        return False
    if rel.get("pages"):
        return got.get("page") in rel["pages"]
    wanted = str(rel.get("section", "")).casefold().strip()
    have = str(got.get("section", "")).casefold().strip()
    if not wanted:
        return True
    if have == wanted:
        return True
    return have.startswith(wanted) and not have[len(wanted)].isalnum()


def recall_at_k(relevant: list[dict], retrieved: list[dict], k: int) -> float | None:
    """Fraction of the relevant passages found in the top k retrieved.
    Return None when the question has no relevant passages (must-abstain questions)."""
    if not relevant:
        return None
    top = retrieved[:k]
    found = sum(1 for rel in relevant if any(_matches(rel, got) for got in top))
    return found / len(relevant)


def _norm(text: object) -> str:
    return " ".join(str(text).casefold().split())


def citation_counts(response: dict) -> tuple[int, int]:
    """(citations that match a retrieved passage, all citations the model produced).

    The API drops a citation that matches nothing it retrieved and reports how many it
    dropped in `invalid_citations_removed`; those count as invalid here, so the figure
    measures the model, not just what survived the filter."""
    retrieved = response.get("retrieved") or []
    citations = response.get("citations") or []
    valid = 0
    for c in citations:
        for r in retrieved:
            same = _norm(c.get("source")) == _norm(r.get("source")) and _norm(
                c.get("section")
            ) == _norm(r.get("section"))
            page_ok = c.get("page") is None or r.get("page") is None or c.get("page") == r.get("page")
            if same and page_ok:
                valid += 1
                break
    return valid, len(citations) + int(response.get("invalid_citations_removed", 0))


def citation_validity(response: dict) -> float | None:
    """Fraction of the response's citations that match a passage in response['retrieved'].
    Return None when there are no citations."""
    valid, total = citation_counts(response)
    return valid / total if total else None


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def summarise(rows: list[dict]) -> dict:
    """Overall recall@3, recall@5, citation validity and abstention correctness, plus
    recall by question type. This dict becomes results.json."""
    per_question = []
    r3: list[float] = []
    r5: list[float] = []
    by_type3: dict[str, list[float]] = {}
    by_type5: dict[str, list[float]] = {}
    valid_total = cite_total = 0
    abstain_rows = [r for r in rows if r["must_abstain"]]
    wrongly_answered = [r["id"] for r in abstain_rows if r["response"].get("answered_from_context")]
    false_abstentions = [
        r["id"] for r in rows if not r["must_abstain"] and not r["response"].get("answered_from_context")
    ]
    for row in rows:
        retrieved = row["response"].get("retrieved") or []
        a3 = recall_at_k(row["relevant"], retrieved, 3)
        a5 = recall_at_k(row["relevant"], retrieved, 5)
        valid, total = citation_counts(row["response"])
        valid_total += valid
        cite_total += total
        if a3 is not None and a5 is not None:
            r3.append(a3)
            r5.append(a5)
            by_type3.setdefault(row["type"], []).append(a3)
            by_type5.setdefault(row["type"], []).append(a5)
        per_question.append({
            "id": row["id"],
            "type": row["type"],
            "recall_at_3": a3,
            "recall_at_5": a5,
            "answered_from_context": row["response"].get("answered_from_context"),
            "citations": len(row["response"].get("citations") or []),
            "cached": row["response"].get("cached", False),
            "seconds": row.get("seconds"),
        })
    return {
        "recall_at_3": _mean(r3),
        "recall_at_5": _mean(r5),
        "recall_at_3_by_type": {t: _mean(v) for t, v in sorted(by_type3.items())},
        "recall_at_5_by_type": {t: _mean(v) for t, v in sorted(by_type5.items())},
        "citation_validity": round(valid_total / cite_total, 4) if cite_total else None,
        "citations_checked": cite_total,
        "abstention_correctness": (
            round(1 - len(wrongly_answered) / len(abstain_rows), 4) if abstain_rows else None
        ),
        "must_abstain_answered": wrongly_answered,
        "false_abstentions": false_abstentions,
        "per_question": per_question,
    }


def gate(results: dict, baseline: dict | None) -> list[str]:
    """Return one message per failed gate (section 5.4); an empty list means pass."""
    failures = []
    answered = results.get("must_abstain_answered") or []
    abstention = results.get("abstention_correctness")
    if answered or (abstention is not None and abstention < REQUIRED_ABSTENTION):
        failures.append(
            f"must-abstain question(s) answered from context: {', '.join(answered) or '?'} "
            f"(abstention correctness {abstention}, required {REQUIRED_ABSTENTION})"
        )
    validity = results.get("citation_validity")
    if validity is not None and validity < MIN_CITATION_VALIDITY:
        failures.append(f"citation validity {validity:.1%} is below {MIN_CITATION_VALIDITY:.0%}")
    recall5 = results.get("recall_at_5")
    base5 = (baseline or {}).get("recall_at_5")
    if recall5 is not None and base5 is not None and recall5 < base5 - MAX_RECALL5_DROP:
        failures.append(
            f"recall@5 {recall5:.1%} dropped more than {MAX_RECALL5_DROP * 100:.0f} points "
            f"below the baseline {base5:.1%}"
        )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate RAHBAR against a golden set.")
    parser.add_argument("--api", required=True, help="base URL, e.g. http://192.168.49.2")
    parser.add_argument("--host", help="Host header, when your Ingress rule names a host")
    parser.add_argument("--golden", type=Path, default=HERE / "golden.yaml")
    parser.add_argument("--baseline", type=Path, default=HERE / "baseline.json")
    parser.add_argument("--out", type=Path, default=HERE / "results.json")
    parser.add_argument("--update-baseline", action="store_true",
                        help="write these results as the new baseline")
    args = parser.parse_args()

    questions = load_golden(args.golden)
    rows = []
    headers = {"Host": args.host} if args.host else {}
    with httpx.Client(timeout=300.0, headers=headers) as client:
        for q in questions:
            started = time.perf_counter()
            response = ask(client, args.api, q["question"])
            rows.append({
                "id": q["id"], "type": q.get("type", "ordinary"),
                "must_abstain": bool(q.get("must_abstain", False)),
                "relevant": q.get("relevant") or [],
                "expect": q.get("expect"),
                "response": response,
                "seconds": round(time.perf_counter() - started, 2),
            })
            print(f"  {q['id']:<5} {rows[-1]['seconds']:>6.2f}s  {q['question'][:60]}")

    try:
        results = summarise(rows)
        baseline = json.loads(args.baseline.read_text()) if args.baseline.exists() else None
        failures = gate(results, baseline)
    except (KeyError, TypeError, ValueError) as exc:  # a malformed response is a failed run
        print(f"\ncould not score the responses: {exc!r}")
        return 2

    results["golden"] = str(args.golden)
    results["questions"] = len(rows)
    args.out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nresults written to {args.out}")
    print(f"recall@3 {results['recall_at_3']}  recall@5 {results['recall_at_5']}  "
          f"citation validity {results['citation_validity']}  "
          f"abstention {results['abstention_correctness']}")
    if args.update_baseline:
        args.baseline.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"baseline updated: {args.baseline}")

    if failures:
        print("\nGATE FAILED")
        for message in failures:
            print(f"  - {message}")
        return 1
    print("\ngate passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
