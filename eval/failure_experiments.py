#!/usr/bin/env python3
"""Break four pipeline stages on purpose and measure the damage (section 5.5).

    python eval/failure_experiments.py --chroma-port 8001 --only A C E B

Every experiment starts from the working index (collection rahbar_exp_400_50, built by
chunk_experiment.py) and changes exactly one thing:

  A  extraction   index the scanned PDFs with OCR switched off (their pages come out empty)
  B  chunking     fixed 100-word windows, no overlap, no headings
  C  retrieval    top_k = 1
  E  source       the superseded-version filter removed

Each is scored with the SAME recall and citation functions as the CI gate, on the same golden
set, so the numbers are comparable with the working system. Results go to
eval/failure_experiments_results.json. A throwaway local Chroma only; never the cluster.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import chunk_experiment as ce  # noqa: E402
import yaml  # noqa: E402
from app.config import Settings  # noqa: E402
from app.retrieval.chroma import ChromaRetriever  # noqa: E402
from pipeline.config import IndexerSettings  # noqa: E402
from pipeline.embed import embed  # noqa: E402
from pipeline.extract import extract  # noqa: E402
from pipeline.models import Chunk  # noqa: E402
from pipeline.run import discover  # noqa: E402
from pipeline.store import get_client, store  # noqa: E402

BASE = "rahbar_exp_400_50"


def settings(args: argparse.Namespace, collection: str, **kw: object) -> Settings:
    return Settings(retriever="chroma", collection=collection, chroma_host=args.chroma_host,
                    chroma_port=args.chroma_port, ollama_url=args.ollama, embed_model=args.model,
                    hybrid=True, candidate_pool=20, **kw)  # type: ignore[arg-type]


def isettings(args: argparse.Namespace, collection: str) -> IndexerSettings:
    return IndexerSettings(args.model, args.chroma_host, args.chroma_port, collection, "-", 400, 50,
                           args.ollama)


def score(args: argparse.Namespace, questions: list[dict], collection: str, k: int = 5,
          **kw: object) -> dict:
    return ce.evaluate(settings(args, collection, **kw), questions, k=k)


def exp_a(args: argparse.Namespace, questions: list[dict]) -> dict:
    """Scanned PDFs indexed without OCR: their pages come out empty."""
    os.environ["OCR_ENABLED"] = "false"
    broken, empty = [], {}
    for path, version, superseded in discover(str(args.corpus)):
        cached = json.loads((args.corpus / "_extracted" / (path.name + ".json")).read_text(encoding="utf-8"))
        if any(p["ocr"] for p in cached["pages"]):
            doc = extract(str(path), version, superseded)
            broken.append({"source": doc.source, "pages": len(doc.pages),
                           "empty_pages": sum(not p.text.strip() for p in doc.pages),
                           "problems_logged": len(doc.problems),
                           "chunks_from_empty_pages": len([c for c in ce.chunk(ce.clean(doc), 400, 50)
                                                           if c.page in {p.page for p in doc.pages if not p.text.strip()}])})
            empty[doc.source] = {p.page for p in doc.pages if not p.text.strip()}
    os.environ["OCR_ENABLED"] = "true"
    # Same index as the working one, minus what the scanned documents contributed (nothing).
    client = get_client(isettings(args, BASE))
    src = client.get_collection(BASE)
    rows = src.get(include=["documents", "metadatas", "embeddings"])
    keep = [i for i, m in enumerate(rows["metadatas"]) if m["page"] not in empty.get(m["source"], set())]
    name = "rahbar_fail_a"
    try:
        client.delete_collection(name)
    except Exception:
        pass
    dst = client.create_collection(name=name, metadata={"hnsw:space": "cosine"})
    for s in range(0, len(keep), 200):
        part = keep[s:s + 200]
        dst.add(ids=[rows["ids"][i] for i in part], documents=[rows["documents"][i] for i in part],
                embeddings=[rows["embeddings"][i].tolist() for i in part],
                metadatas=[rows["metadatas"][i] for i in part])
    return {"stage": "A extraction", "documents_without_text": broken, "chunks_before": src.count(),
            "chunks_after": dst.count(), "working": score(args, questions, BASE),
            "broken": score(args, questions, name)}


def naive_chunks(args: argparse.Namespace) -> list[Chunk]:
    """What a first attempt looks like: 100-word windows cut across the page text."""
    out: list[Chunk] = []
    for doc in (ce.clean(d) for d in ce.load_documents(args.corpus)):
        slug = doc.source.rsplit(".", 1)[0].lower()
        n = 0
        for page in doc.pages:
            w = page.text.split()
            for s in range(0, len(w), 100):
                out.append(Chunk(f"naive-{slug}-{n:05d}", " ".join(w[s:s + 100]), doc.source,
                                 f"page {page.page}", page.page, doc.doc_version, doc.superseded))
                n += 1
    return out


def exp_b(args: argparse.Namespace, questions: list[dict]) -> dict:
    """Fixed 100-word chunks with no overlap and no sense of headings or tables."""
    name = "rahbar_fail_b"
    chunks = naive_chunks(args)
    s = isettings(args, name)
    started = time.perf_counter()
    vectors = embed([c.text for c in chunks], s)
    try:
        get_client(s).delete_collection(name)
    except Exception:
        pass
    store(chunks, vectors, s)
    working = get_client(s).get_collection(BASE).count()
    return {"stage": "B chunking", "chunks_before": working, "chunks_after": len(chunks),
            "embed_seconds": round(time.perf_counter() - started),
            "working": score(args, questions, BASE), "broken": score(args, questions, name)}


def exp_c(args: argparse.Namespace, questions: list[dict]) -> dict:
    """top_k = 1: the answer is only as good as the single best passage."""
    return {"stage": "C retrieval", "working": score(args, questions, BASE, k=5),
            "broken": score(args, questions, BASE, k=1)}


def exp_e(args: argparse.Namespace, questions: list[dict]) -> dict:
    """The superseded filter removed: old versions compete with the current one."""
    def leakage(filter_on: bool) -> dict:
        r = ChromaRetriever(settings(args, BASE, filter_superseded=filter_on))
        hit, ids = 0, []
        for q in questions:
            if q.get("must_abstain"):
                continue
            top = r.retrieve(q["question"], k=3)
            if any(p.superseded for p in top):
                hit += 1
                ids.append(q["id"])
        return {"questions_with_a_superseded_passage_in_top3": hit, "which": ids}

    return {"stage": "E source", "working": {**score(args, questions, BASE), **leakage(True)},
            "broken": {**score(args, questions, BASE, filter_superseded=False), **leakage(False)}}


EXPERIMENTS = {"A": exp_a, "B": exp_b, "C": exp_c, "E": exp_e}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--corpus", type=Path, default=ROOT / "corpus" / "raw")
    ap.add_argument("--golden", type=Path, default=HERE / "golden.yaml")
    ap.add_argument("--only", nargs="+", default=list(EXPERIMENTS), choices=list(EXPERIMENTS))
    ap.add_argument("--chroma-host", default="localhost")
    ap.add_argument("--chroma-port", type=int, default=8001)
    ap.add_argument("--ollama", default="http://localhost:11434")
    ap.add_argument("--model", default="nomic-embed-text")
    ap.add_argument("--out", type=Path, default=HERE / "failure_experiments_results.json")
    args = ap.parse_args()
    questions = yaml.safe_load(args.golden.read_text(encoding="utf-8"))["questions"]
    results = json.loads(args.out.read_text()) if args.out.exists() else {}
    for key in args.only:
        print(f"--- experiment {key}", flush=True)
        results[key] = EXPERIMENTS[key](args, questions)
        w, b = results[key]["working"], results[key]["broken"]
        print(f"    working recall@3 {w['recall_at_3']} recall@5 {w['recall_at_5']} | "
              f"broken recall@3 {b['recall_at_3']} recall@5 {b['recall_at_5']} misses {b['misses']}", flush=True)
        args.out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
