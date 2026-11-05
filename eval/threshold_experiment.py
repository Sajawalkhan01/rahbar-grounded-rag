#!/usr/bin/env python3
"""Choose the relevance floor and the cache threshold with data (sections 3.3 and 3.4).

    python eval/threshold_experiment.py --collection rahbar_exp_400_50 --chroma-port 8001

1. RELEVANCE FLOOR. For every golden question, the best dense similarity among the retrieved
   passages. Answerable questions should sit above the floor and must-abstain questions below it.
2. CACHE THRESHOLD. Pairs of questions with the same meaning (a cache hit is right) and
   near-miss pairs that differ in one detail (a hit would serve a WRONG answer to someone).
   For each candidate threshold, count the false hits; the lowest threshold with zero false hits
   is the one to ship ("lowest" keeps the true hits as high as possible).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))

import numpy as np  # noqa: E402
import yaml  # noqa: E402
from app.config import Settings  # noqa: E402
from app.retrieval.chroma import ChromaRetriever  # noqa: E402
from app.retrieval.embedder import OllamaEmbedder  # noqa: E402

SAME = [  # same meaning: a cache hit is correct
    ("What minimum CGPA must an undergraduate keep to avoid academic warning?",
     "Which CGPA do I need to stay off academic warning?"),
    ("What is the tuition fee per credit hour for BS students in 2026-27?",
     "How much is the BS tuition per credit hour for 2026-27?"),
    ("Is the fee refunded when a student withdraws from a course?",
     "Do I get my money back if I withdraw from a course?"),
    ("What does clause 1.21 of the academic rules require?",
     "What does rule 1.21 of the academic rules say?"),
    ("How many courses can a student repeat in the summer semester?",
     "What is the maximum number of courses I may repeat in summer?"),
    ("What CGPA earns the distinguishing remark Suma cum laude on a degree?",
     "Which CGPA is needed for Suma cum laude?"),
]
DIFFERENT = [  # one detail differs: a cache hit would serve a wrong answer
    ("What does clause 1.21 of the academic rules require?",
     "What does clause 1.22 of the academic rules require?"),
    ("What minimum CGPA must an undergraduate keep to avoid academic warning?",
     "What minimum CGPA is required for graduation?"),
    ("What is the tuition fee per credit hour for BS students in 2026-27?",
     "What is the tuition fee per credit hour for BS students in 2023-24?"),
    ("What is the one-time admission fee?", "What is the refundable security deposit?"),
    ("How many courses can a student repeat in the summer semester?",
     "How many courses can a student register in the summer semester?"),
    ("What does the 2023 rule say about attendance?", "What does the 2026 rule say about attendance?"),
    ("What is the Late Registration Fee in the fee structure?",
     "What is the Transcript Fee in the fee structure?"),
    ("What happens after three consecutive semesters below 2.00 CGPA?",
     "What happens after one semester below 2.00 CGPA?"),
]


def cosine(a: list[float], b: list[float]) -> float:
    x, y = np.asarray(a), np.asarray(b)
    return float(x @ y / (np.linalg.norm(x) * np.linalg.norm(y)))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--collection", required=True)
    ap.add_argument("--chroma-host", default="localhost")
    ap.add_argument("--chroma-port", type=int, default=8001)
    ap.add_argument("--ollama", default="http://localhost:11434")
    ap.add_argument("--model", default="nomic-embed-text")
    ap.add_argument("--golden", type=Path, default=ROOT / "eval" / "golden.yaml")
    ap.add_argument("--out", type=Path, default=ROOT / "eval" / "threshold_experiment_results.json")
    args = ap.parse_args()

    settings = Settings(retriever="chroma", collection=args.collection, chroma_host=args.chroma_host,
                        chroma_port=args.chroma_port, ollama_url=args.ollama, embed_model=args.model)
    retriever = ChromaRetriever(settings)

    # 1. relevance floor
    rows = []
    for q in yaml.safe_load(args.golden.read_text(encoding="utf-8"))["questions"]:
        passages = retriever.retrieve(q["question"], k=5)
        rows.append({"id": q["id"], "type": q["type"], "must_abstain": bool(q.get("must_abstain")),
                     "top_score": round(max((p.score for p in passages), default=0.0), 4)})
    answerable = [r["top_score"] for r in rows if not r["must_abstain"]]
    declined = [r["top_score"] for r in rows if r["must_abstain"]]
    print("best dense similarity per question:")
    for r in sorted(rows, key=lambda r: r["top_score"]):
        print(f"  {r['id']:<4} {r['type']:<9} {r['top_score']:.3f} {'(must abstain)' if r['must_abstain'] else ''}")
    print(f"\nanswerable: min {min(answerable):.3f}   must-abstain: max {max(declined):.3f}")

    # 2. cache threshold
    embedder = OllamaEmbedder(args.ollama, args.model)
    same = [cosine(embedder.embed_query(a), embedder.embed_query(b)) for a, b in SAME]
    diff = [cosine(embedder.embed_query(a), embedder.embed_query(b)) for a, b in DIFFERENT]
    print("\nsame-meaning pairs:", [round(s, 3) for s in same])
    print("near-miss pairs:   ", [round(s, 3) for s in diff])
    table = []
    for threshold in (0.85, 0.88, 0.90, 0.92, 0.94, 0.96):
        false_hits = sum(1 for s in diff if s >= threshold)
        true_hits = sum(1 for s in same if s >= threshold)
        table.append({"threshold": threshold, "false_hits": false_hits, "true_hits": true_hits,
                      "of_same": len(same)})
        print(f"threshold {threshold:.2f}: false hits {false_hits}/{len(diff)}, true hits {true_hits}/{len(same)}")
    args.out.write_text(json.dumps({"floor": rows, "same": same, "different": diff, "cache": table},
                                   indent=2), encoding="utf-8")
    print(f"\nwritten to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
