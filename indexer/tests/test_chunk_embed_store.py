"""Stages 3, 4 and 5: chunking, embedding and storing, plus the stage runner on the fixtures."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from pipeline import store as store_module
from pipeline.chunk import chunk, heading_of, words
from pipeline.config import IndexerSettings
from pipeline.embed import DOCUMENT_PREFIX, embed
from pipeline.models import Chunk, Document, Page
from pipeline.run import discover, run_pipeline
from pipeline.store import store, validate

S = IndexerSettings(embed_model="fake-embed", chroma_host="x", chroma_port=1, collection="rahbar",
                    corpus_dir="x", chunk_size=60, chunk_overlap=12, ollama_url="http://ollama:11434")


def doc(*pages: str, version: str = "2026", superseded: bool = False) -> Document:
    return Document("rules.pdf", version, superseded, [Page("rules.pdf", i + 1, t) for i, t in enumerate(pages)])


SENT = "A student must attend lectures regularly and sit every examination on time."   # 12 words


def para(n: int) -> str:
    return " ".join(f"Sentence {i} " + SENT for i in range(n))


# --------------------------------------------------------------------------- headings
@pytest.mark.parametrize(
    "line, expected",
    [
        ("5.4 Academic Warning", "5.4 Academic Warning"),
        ("1. Attendance", "1. Attendance"),
        ("## 3. Repeating a course", "3. Repeating a course"),
        ("CHAPTER 5 GRADING", "CHAPTER 5 GRADING"),
        ("Appendix A", "Appendix A"),
        ("1.1 A student must attend at least 80 percent of the lectures.", None),   # a clause, not a title
        ("Late registration | 3,000", None),
        ("the body of a paragraph without a number", None),
        ("Total 2,500", None),
        ("1.21 Students are required to maintain 100% attendance in", None),   # a wrapped clause line
        ("8.17 An extreme act of dishonesty, e.g.,", None),
        ("5.4 Academic Warning/Probation", "5.4 Academic Warning/Probation"),
    ],
)
def test_heading_detection(line: str, expected: str | None) -> None:
    assert heading_of(line) == expected


# ------------------------------------------------------------------------------ chunk
def test_chunks_carry_source_section_page_version_and_superseded() -> None:
    d = doc("1. Attendance\nStudents attend 80 percent.\n\n2. Warning\nBelow 2.00 is a warning.", version="2023-08",
            superseded=True)
    cs = chunk(d, 60, 12)
    assert [c.section for c in cs] == ["1. Attendance", "2. Warning"]
    assert all(c.source == "rules.pdf" and c.doc_version == "2023-08" and c.superseded for c in cs)
    assert cs[0].text.startswith("1. Attendance\n") and "80 percent" in cs[0].text
    assert set(cs[0].metadata()) == {"source", "section", "page", "doc_version", "superseded"}


def test_section_carries_across_a_page_break_and_pages_are_tracked() -> None:
    d = doc("1. Attendance\nFirst page text.", "Second page text still in attendance.")
    cs = chunk(d, 60, 12)
    assert len(cs) == 1 and cs[0].section == "1. Attendance" and cs[0].page == 1
    assert "Second page text" in cs[0].text


def test_a_new_page_starting_a_new_section_gets_that_page() -> None:
    cs = chunk(doc("1. Alpha\nText one.", "2. Bravo\nText two."), 60, 12)
    assert [(c.section, c.page) for c in cs] == [("1. Alpha", 1), ("2. Bravo", 2)]


def test_chunks_respect_the_size_and_neighbours_overlap() -> None:
    cs = chunk(doc("1. Attendance\n" + para(30)), 60, 12)
    assert len(cs) > 3
    assert all(words(c.text) <= 60 + 3 for c in cs)                  # size plus the heading
    for a, b in zip(cs, cs[1:], strict=False):
        tail = a.text.split()[-6:]
        assert " ".join(tail) in b.text, "neighbouring chunks share their boundary"


def test_overlap_zero_gives_disjoint_chunks() -> None:
    cs = chunk(doc("1. Alpha\n" + para(30)), 60, 0)
    sentences = [s for c in cs for s in c.text.split("Sentence ")[1:]]
    assert len(sentences) == len(set(sentences)) == 30


def test_nothing_overlaps_across_a_section_boundary() -> None:
    cs = chunk(doc("1. Alpha\n" + para(8) + "\n\n2. Bravo\n" + para(8)), 60, 12)
    b_chunks = [c for c in cs if c.section == "2. Bravo"]
    assert "Sentence 7" not in b_chunks[0].text.split("\n", 1)[1][:30]
    first_b = b_chunks[0].text.split("\n", 1)[1]
    assert first_b.startswith("Sentence 0")


def test_a_long_definition_is_not_cut_in_half() -> None:
    definition = ("Academic Integrity means " + " ".join(["honest scholarly work"] * 12)
                  + " and every submitted item must be the student's own work.")
    pages = "1. Definitions\n" + para(3) + "\n\n" + definition + "\n\n" + para(3)
    cs = chunk(doc(pages), 90, 10)
    holders = [c for c in cs if "Academic Integrity means" in c.text]
    assert len(holders) >= 1 and any(definition in c.text for c in holders)


def test_an_oversized_paragraph_is_split_by_sentences_with_no_loss() -> None:
    cs = chunk(doc("1. Alpha\n" + para(40)), 60, 0)
    joined = " ".join(c.text.split("\n", 1)[1] for c in cs)
    assert all(f"Sentence {i} " in joined for i in range(40))


def test_a_sentence_longer_than_the_size_is_split_by_words() -> None:
    long_sentence = " ".join(f"word{i}" for i in range(200)) + "."
    cs = chunk(doc("1. Alpha\n" + long_sentence), 60, 0)
    assert len(cs) >= 4 and all(words(c.text) <= 64 for c in cs)


def test_a_table_is_kept_together_and_split_by_rows_with_the_header_repeated() -> None:
    rows = "\n".join(f"Course {i} | Fee {i * 100}" for i in range(40))
    cs = chunk(doc("1. Fees\nCourse | Fee\n" + rows), 40, 0)
    assert len(cs) > 1
    assert all("Course | Fee" in c.text for c in cs)
    assert "Course 39 | Fee 3900" in " ".join(c.text for c in cs)


def test_a_small_table_stays_in_one_chunk_with_its_rows_intact() -> None:
    cs = chunk(doc("2. Other charges\nCharge | Amount\nLate registration | 3,000\nTranscript | 1,500"), 60, 12)
    assert len(cs) == 1 and "Late registration | 3,000" in cs[0].text


def test_chunk_ids_are_stable_and_unique() -> None:
    d = doc("1. Alpha\n" + para(20), "2. Bravo\n" + para(20))
    a, b = chunk(d, 60, 12), chunk(d, 60, 12)
    assert [c.id for c in a] == [c.id for c in b]
    assert len({c.id for c in a}) == len(a) and a[0].id == "rules-0000"


def test_text_before_any_heading_still_gets_a_section() -> None:
    cs = chunk(doc("Just some opening text with no heading at all, long enough to keep."), 60, 12)
    assert cs and cs[0].section == "Introduction"


def test_empty_document_gives_no_chunks() -> None:
    assert chunk(doc(""), 60, 12) == []


@pytest.mark.parametrize("size, overlap", [(10, 0), (60, 30), (60, -1)])
def test_bad_chunk_parameters_are_rejected(size: int, overlap: int) -> None:
    with pytest.raises(ValueError):
        chunk(doc("1. Alpha\ntext"), size, overlap)


# ------------------------------------------------------------------------------ embed
def _patch_post(monkeypatch: pytest.MonkeyPatch, dim: int = 4, fail_first: int = 0) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def post(self: httpx.Client, url: str, json: dict[str, Any]) -> httpx.Response:
        calls.append({"url": url, **json})
        if len(calls) <= fail_first:
            raise httpx.ConnectError("not yet")
        vectors = [[float(len(t)), 1.0, 0.0, 0.5][:dim] + [0.0] * (dim - 4) for t in json["input"]]
        return httpx.Response(200, json={"embeddings": vectors}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.Client, "post", post)
    monkeypatch.setattr("pipeline.embed.time.sleep", lambda _s: None)
    return calls


def test_embed_uses_the_pinned_model_prefix_and_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_post(monkeypatch)
    vectors = embed([f"text {i}" for i in range(40)], S)
    assert len(vectors) == 40 and {len(v) for v in vectors} == {4}
    assert len(calls) == 3                                           # 16 + 16 + 8
    assert all(c["model"] == "fake-embed" and c["url"] == "http://ollama:11434/api/embed" for c in calls)
    assert all(t.startswith(DOCUMENT_PREFIX) for c in calls for t in c["input"])


def test_embed_retries_a_flaky_service(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_post(monkeypatch, fail_first=2)
    assert len(embed(["a", "b"], S)) == 2 and len(calls) == 3


def test_embed_gives_up_after_the_attempts_and_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_post(monkeypatch, fail_first=99)
    with pytest.raises(RuntimeError, match="embedding failed after"):
        embed(["a"], S)


def test_embed_nothing_is_nothing() -> None:
    assert embed([], S) == []


# ------------------------------------------------------------------------------ store
class FakeCollection:
    def __init__(self, name: str, metadata: dict[str, Any]) -> None:
        self.name, self.metadata, self.rows = name, dict(metadata), {}

    def upsert(self, ids: list[str], documents: list[str], embeddings: list[list[float]],
               metadatas: list[dict[str, Any]]) -> None:
        for i, d, e, m in zip(ids, documents, embeddings, metadatas, strict=True):
            self.rows[i] = (d, e, m)

    def get(self, include: list[str] | None = None) -> dict[str, Any]:
        return {"ids": list(self.rows)}

    def delete(self, ids: list[str]) -> None:
        for i in ids:
            del self.rows[i]

    def modify(self, metadata: dict[str, Any]) -> None:
        self.metadata = {"hnsw:space": self.metadata.get("hnsw:space"), **metadata}

    def count(self) -> int:
        return len(self.rows)


class FakeClient:
    def __init__(self) -> None:
        self.collections: dict[str, FakeCollection] = {}
        self.deleted: list[str] = []

    def get_collection(self, name: str) -> FakeCollection:
        if name not in self.collections:
            raise ValueError("does not exist")
        return self.collections[name]

    def create_collection(self, name: str, metadata: dict[str, Any]) -> FakeCollection:
        self.collections[name] = FakeCollection(name, metadata)
        return self.collections[name]

    def delete_collection(self, name: str) -> None:
        self.deleted.append(name)
        del self.collections[name]


def mk(i: int, text: str = "some text") -> Chunk:
    return Chunk(f"c{i}", f"{text} {i}", "a.pdf", f"{i}. Sec", 1, "2026", False)


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    c = FakeClient()
    monkeypatch.setattr(store_module, "get_client", lambda _s: c)
    return c


def vec(n: int, dim: int = 4) -> list[list[float]]:
    return [[1.0] + [0.0] * (dim - 1)] * n


def test_store_writes_chunks_and_records_the_embedding_pin(client: FakeClient) -> None:
    assert store([mk(1), mk(2)], vec(2), S) == 2
    col = client.collections["rahbar"]
    assert col.metadata["embed_model"] == "fake-embed" and col.metadata["dimension"] == 4
    assert col.metadata["index_id"] and col.metadata["chunks"] == 2
    assert col.metadata["hnsw:space"] == "cosine"
    for _doc, _vec, meta in col.rows.values():
        assert set(meta) == {"source", "section", "page", "doc_version", "superseded"}


def test_reindexing_upserts_in_place_prunes_stale_chunks_and_changes_the_index_id(client: FakeClient) -> None:
    store([mk(1), mk(2), mk(3)], vec(3), S)
    before = client.collections["rahbar"].metadata["index_id"]
    store([mk(2, "changed text"), mk(4)], vec(2), S)
    col = client.collections["rahbar"]
    assert client.deleted == []                                      # never deleted: no empty window
    assert sorted(col.rows) == ["c2", "c4"] and "changed text" in col.rows["c2"][0]
    assert col.metadata["index_id"] != before


def test_running_the_same_index_twice_does_not_duplicate(client: FakeClient) -> None:
    store([mk(1), mk(2)], vec(2), S)
    store([mk(1), mk(2)], vec(2), S)
    assert client.collections["rahbar"].count() == 2


def test_a_new_embedding_model_rebuilds_the_collection(client: FakeClient) -> None:
    store([mk(1)], vec(1, 4), S)
    other = IndexerSettings(**{**S.__dict__, "embed_model": "other-model"})
    store([mk(1)], vec(1, 8), other)
    assert client.deleted == ["rahbar"]
    assert client.collections["rahbar"].metadata["embed_model"] == "other-model"
    assert client.collections["rahbar"].metadata["dimension"] == 8


def test_a_new_vector_size_rebuilds_the_collection(client: FakeClient) -> None:
    store([mk(1)], vec(1, 4), S)
    store([mk(1)], vec(1, 8), S)
    assert client.deleted == ["rahbar"]


@pytest.mark.parametrize(
    "chunks, vectors, message",
    [
        ([], [], "no chunks"),
        ([mk(1), mk(2)], vec(1), "1 vectors"),
        ([mk(1), mk(1)], vec(2), "duplicate"),
        ([mk(1), mk(2)], [[1.0, 0.0], [1.0]], "inconsistent"),
        ([Chunk("c1", "  ", "a", "s", 1, "v", False)], vec(1), "empty"),
        ([Chunk("c1", "t", "a", "", 1, "v", False)], vec(1), "missing metadata"),
    ],
)
def test_inconsistent_input_is_rejected_before_anything_is_written(
    client: FakeClient, chunks: list[Chunk], vectors: list[list[float]], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        store(chunks, vectors, S)
    assert client.collections == {}


def test_validate_returns_the_dimension() -> None:
    assert validate([mk(1)], vec(1, 6)) == 6


# ------------------------------------------------------------------------------ runner
FIXTURES = Path(__file__).resolve().parents[2] / "eval" / "fixtures"


def test_run_pipeline_on_the_fixture_corpus(monkeypatch: pytest.MonkeyPatch, client: FakeClient) -> None:
    _patch_post(monkeypatch)
    s = IndexerSettings(**{**S.__dict__, "corpus_dir": str(FIXTURES), "chunk_size": 120, "chunk_overlap": 20})
    count = run_pipeline(s)
    rows = client.collections["rahbar"].rows
    assert count == len(rows) > 5
    sources = {m["source"] for _d, _v, m in rows.values()}
    assert sources == {"academic-rules-2023.md", "academic-rules-2026.md", "fees-2026.md"}
    superseded = {m["source"]: m["superseded"] for _d, _v, m in rows.values()}
    assert superseded["academic-rules-2023.md"] is True and superseded["academic-rules-2026.md"] is False
    sections = {m["section"] for _d, _v, m in rows.values() if m["source"] == "academic-rules-2026.md"}
    assert {"1. Attendance", "2. Academic warning", "3. Repeating a course"} <= sections
    fee_text = " ".join(d for d, _v, m in rows.values() if m["source"] == "fees-2026.md")
    assert "BS (all disciplines) | 10,000" in fee_text


def test_discover_prefers_the_index_json_the_fetch_script_writes(tmp_path: Path) -> None:
    (tmp_path / "a.pdf").write_bytes(b"%PDF")
    (tmp_path / "_index.json").write_text(
        '[{"file": "a.pdf", "doc_version": "2023-08", "superseded": true}]', encoding="utf-8")
    assert discover(str(tmp_path)) == [(tmp_path / "a.pdf", "2023-08", True)]


def test_pipeline_fails_loudly_when_a_document_has_no_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client: FakeClient
) -> None:
    (tmp_path / "empty.md").write_text("---\ndoc_version: 1\n---\n", encoding="utf-8")
    s = IndexerSettings(**{**S.__dict__, "corpus_dir": str(tmp_path)})
    with pytest.raises(SystemExit, match="no chunks"):
        run_pipeline(s)
