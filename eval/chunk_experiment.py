#!/usr/bin/env python3
"""Measure retrieval quality for several (chunk size, overlap) settings (section 3.1).

    python eval/chunk_experiment.py --corpus corpus/raw --chroma-port 8001 \\
        --configs 150:20 400:50 800:100

For each setting: chunk the whole corpus, embed it with the pinned model, store it in its own
Chroma collection (rahbar_exp_<size>_<overlap>), then ask every golden question and compute
recall@3 and recall@5 with the SAME functions as the CI gate (eval/run_eval.py). It is a
development tool run against a throwaway local Chroma; in the cluster only the indexer Job
writes to the production collection.

Extraction (with OCR) is slow, so each document is extracted once and cached as JSON in
<corpus>/_extracted/. Delete that folder to extract again.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "indexer"), str(ROOT / "api")]

import yaml  # noqa: E402
from app.config import Settings  # noqa: E402
from app.retrieval.chroma import ChromaRetriever  # noqa: E402
from pipeline.chunk import chunk, words  # noqa: E402
from pipeline.clean import clean  # noqa: E402
from pipeline.config import IndexerSettings  # noqa: E402
from pipeline.embed import embed  # noqa: E402
from pipeline.extract import extract  # noqa: E402
from pipeline.models import Document, Page  # noqa: E402
from pipeline.run import discover  # noqa: E402
from pipeline.store import get_client, store  # noqa: E402

_spec = importlib.util.spec_from_file_location("run_eval", ROOT / "eval" / "run_eval.py")
assert _spec and _spec.loader
run_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_eval)


def load_documents(corpus: Path) -> list[Document]:
    cache = corpus / "_extracted"
    cache.mkdir(exist_ok=True)
    docs = []
    for path, version, superseded in discover(str(corpus)):
        cached = cache / (path.name + ".json")
        if cached.exists():
            raw = json.loads(cached.read_text(encoding="utf-8"))
            doc = Document(raw["source"], raw["doc_version"], raw["superseded"],
                           [Page(raw["source"], p["page"], p["text"], p["ocr"]) for p in raw["pages"]],
                           raw["problems"])
        else:
            started = time.perf_counter()
            doc = extract(str(path), version, superseded)
            cached.write_text(json.dumps({
                "source": doc.source, "doc_version": doc.doc_version, "superseded": doc.superseded,
                "problems": doc.problems,
                "pages": [{"page": p.page, "text": p.text, "ocr": p.ocr} for p in doc.pages],
            }), encoding="utf-8")
            print(f"  extracted {path.name}: {len(doc.pages)} pages, "
                  f"{sum(p.ocr for p in doc.pages)} OCR, {time.perf_counter() - started:.0f}s", flush=True)
        # keep the version flags from _index.json current even when the cache is older
        doc.doc_version, doc.superseded = version, superseded
        docs.append(doc)
    return docs


def evaluate(settings: Settings, questions: list[dict], k: int = 5) -> dict:
    retriever = ChromaRetriever(settings)
    r3, r5 = [], []
    by_type: dict[str, list[float]] = {}
    misses = []
    for q in questions:
        if q.get("must_abstain"):
            continue
        passages = retriever.retrieve(q["question"], k=k)
        got = [{"source": p.source, "section": p.section, "page": p.page} for p in passages]
        a3 = run_eval.recall_at_k(q["relevant"], got, 3)
        a5 = run_eval.recall_at_k(q["relevant"], got, 5)
        r3.append(a3)
        r5.append(a5)
        by_type.setdefault(q["type"], []).append(a5)
        if a5 < 1.0:
            misses.append(q["id"])
    mean = lambda v: round(sum(v) / len(v), 4) if v else None  # noqa: E731
    return {"recall_at_3": mean(r3), "recall_at_5": mean(r5),
            "by_type": {t: mean(v) for t, v in sorted(by_type.items())}, "misses": misses}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--corpus", type=Path, default=ROOT / "corpus" / "raw")
    ap.add_argument("--golden", type=Path, default=ROOT / "eval" / "golden.yaml")
    ap.add_argument("--configs", nargs="+", default=["150:20", "400:50", "800:100"],
                    help="size:overlap pairs (words)")
    ap.add_argument("--chroma-host", default="localhost")
    ap.add_argument("--chroma-port", type=int, default=8001)
    ap.add_argument("--ollama", default="http://localhost:11434")
    ap.add_argument("--model", default="nomic-embed-text")
    ap.add_argument("--out", type=Path, default=ROOT / "eval" / "chunk_experiment_results.json")
    args = ap.parse_args()

    questions = yaml.safe_load(args.golden.read_text(encoding="utf-8"))["questions"]
    docs = load_documents(args.corpus)
    cleaned = [clean(d) for d in docs]
    original_words = sum(words(p.text) for d in cleaned for p in d.pages)
    print(f"{len(docs)} documents, {sum(len(d.pages) for d in docs)} pages, {original_words:,} words")

    results = []
    for spec in args.configs:
        size, overlap = (int(x) for x in spec.split(":"))
        name = f"rahbar_exp_{size}_{overlap}"
        chunks = [c for d in cleaned for c in chunk(d, size, overlap)]
        stored_words = sum(words(c.text) for c in chunks)
        isettings = IndexerSettings(args.model, args.chroma_host, args.chroma_port, name, "-", size,
                                    overlap, args.ollama)
        started = time.perf_counter()
        vectors = embed([c.text for c in chunks], isettings)
        client = get_client(isettings)
        try:
            client.delete_collection(name)
        except Exception:
            pass
        store(chunks, vectors, isettings)
        embed_seconds = time.perf_counter() - started
        row = {"size": size, "overlap": overlap, "chunks": len(chunks),
               "amplification": round(stored_words / original_words, 3),
               "embed_seconds": round(embed_seconds)}
        for label, hybrid in (("hybrid", True), ("dense", False)):
            s = Settings(retriever="chroma", collection=name, chroma_host=args.chroma_host,
                         chroma_port=args.chroma_port, ollama_url=args.ollama, embed_model=args.model,
                         hybrid=hybrid, candidate_pool=20)
            row[label] = evaluate(s, questions)
        results.append(row)
        print(f"size {size:>4} overlap {overlap:>3}: {len(chunks):>5} chunks, "
              f"amplification {row['amplification']}, hybrid recall@5 {row['hybrid']['recall_at_5']}, "
              f"dense recall@5 {row['dense']['recall_at_5']}", flush=True)
    args.out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nwritten to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
