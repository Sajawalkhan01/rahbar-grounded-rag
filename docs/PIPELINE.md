# Indexing and retrieval pipeline

Corpus: 9 documents, 281 pages, about 70,400 words after cleaning (`docs/CORPUS.md`). The indexer
(`indexer/pipeline/`) runs as a Kubernetes Job and is the only writer; the API (`api/app/`) only
reads. Sizes below are in **words** (whitespace-separated), not model tokens; one word is about
1.3 `nomic-embed-text` tokens.

## Extraction (stage 1: `extract.py`)

**Which documents needed OCR.**

| Document | Pages | OCR'd pages | Notes |
| --- | ---: | ---: | --- |
| Undergraduate Academic Rules (Aug 2023) | 26 | 24 | scanned; page 3 is blank |
| Faculty Entrepreneurship and Technical Start-ups Policy | 8 | 8 | scanned, empty font table (`pdffonts`) |
| Academic Rules Handbook (Jun 2026) | 58 | 5 | pages without a text layer (the first two and last three) |
| Student Handbook (Jun 2026) | 64 | 4 | pages without a text layer (the first two and last two) |
| Annual Report 2019-20 / 2023-24 | 33 / 85 | 1 / 2 | pages without a text layer |
| Prospectus, Fee page, Grading page | 5 / 1 / 1 | 0 | |

A page whose text layer holds fewer than 25 letters or digits is rendered at 200 dpi and read by
Tesseract; the page is marked `ocr=True`, and a page that stays empty is recorded as a problem and
logged by the Job (`extraction problem`), never silently dropped. OCR of the 32 scanned pages takes
15 to 30 minutes on a laptop CPU, which is why the Job has a 90 minute deadline.

**Before and after: a two-column page.** The Academic Rules Handbook is set in columns. Plain
`extract_text()` reads straight across them (page 9, first lines):

```
1.9 A theory course is normally assigned 2 to 4 credit hours, d) For pre-planned activities (conferences, etc.) during
depending on its content. regular class hours, the concerned Head of
```

Clause 1.9 is spliced into the middle of an unrelated sentence from the other column, so no chunk
could ever contain either one intact. Reading each column on its own (`find_gutters`, `layout_text`)
gives:

```
d) For pre-planned activities (conferences, etc.) during
regular class hours, the concerned Head of
Department must be informed and formal permission
sought beforehand.

1.15 If the requirement of minimum lectures is not being met (due
```

**Before and after: a table.** Annual Report 2023-24, page 13 ("Faculty Summary"). pdfplumber's
table finder keeps the columns, but a table with merged cells comes back as
`| Chiniot-Faisalabad |  |  | 2 |  |  | 4 |  |  | 17 |...`, mostly empty cells that waste tokens and
blur the embedding. When more than 40% of a table's cells are empty the empties are dropped
(`render_table`):

```
Campuses | Prof. | Assoc. Profs. | Asst. Profs. | Asst. Profs. | Lect. | Instr. | Lab Engrs. | Total
Chiniot-Faisalabad | 2 | 4 | 17 | 8 | 58 | 12 | 2 | 103
Lahore | 16 | 11 | 29 | 21 | 82 | 19 | 10 | 188
```

A short table stays in one chunk; a long one is split by rows with its header row repeated.

**What could not be recovered.**
* OCR of numbered lists detaches the numbers from their text ("6:1. 6.2. 6.3." on one line, the
  sentences below), so clause numbers in the scanned entrepreneurship policy cannot be used to
  cite it; its golden question is therefore matched by page.
* OCR errors: "A class" becomes "Aclass", "1½ hour" becomes "1% hour". Numbers inside OCR'd tables
  are the least trustworthy text in the index.
* Charts (annual report page 63) are read as labels and numbers in separate groups ("160", "128",
  then "57 62"); the values survive but not which bar they belong to. The adjacent table of the
  same figures is what retrieval relies on.
* Urdu text, if any, is not recognised (Tesseract runs with the English model only).

## Cleaning (stage 2: `clean.py`)

Rules: Unicode NFKC (ligatures, non-breaking spaces); control characters, soft hyphens and
zero-width characters removed; dot leaders ("Fees ........ 12") collapsed; runs of spaces collapsed
and three or more newlines made two; a word split by a line-end hyphen joined when the next line
continues in lower case; headers and footers removed when the same line (digits ignored) is in the
top or bottom three lines of at least half the pages, and bare page numbers at the page edge.
A page with fewer than seven lines only looks at its first and last line, so a short page's body is
never mistaken for a header (found by a unit test).

**Deliberately not stripped:** table rows, including rows that are only numbers, and lines that
look like headings (numbered clauses, bold or larger lines). Chunking and citations depend on the
headings, and a fee table is its numbers.

## Chunking (stage 3: `chunk.py`)

Structure-aware: the current heading (found by font size or bold on PDFs, `#` on HTML and Markdown,
numbered capitalised titles otherwise) is the chunk's `section` and carries across page breaks.
Under a heading, blocks (paragraphs, table row groups) are packed whole up to `size` words, so a
long definition stays in one piece; an over-long block is split by sentences, then by words.
Neighbours in one section share `overlap` words (whole sentences where possible); nothing overlaps
across a heading.

**The measurement.** Three (size, overlap) settings, recall measured on the golden set
(`eval/golden.yaml`: 18 answerable questions; abstain questions have no relevant passages) with the
same functions as the CI gate. Tool: `eval/chunk_experiment.py`; raw output:
`eval/chunk_experiment_results.json`.

| Size (words) | Overlap (words) | Chunks stored | Amplification | recall@3 | recall@5 | recall@5, dense only |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 150 | 20 | 862 | 1.086 | 0.917 | 1.000 | 0.833 |
| **400** | **50** | **601** | **1.014** | **1.000** | **1.000** | 0.889 |
| 800 | 100 | 559 | 0.988 | 0.889 | 0.889 | 0.778 |

Amplification is stored words divided by original (cleaned) words: 70,381 original words. The
handout's example was 150 / 120 = 1.25; overlap and the repeated heading cost about 1.4% at 400/50,
and the 800/100 figure is below 1 because very large chunks absorb page headings.

**Shipped: 400 words, overlap 50.** It is the only setting with perfect recall@3 and recall@5, and
it stores 30% fewer chunks than 150/20. At 150 words a clause is cut from its context and recall@3
falls; at 800 words a chunk covers several topics, its embedding is an average of them, and two
ordinary questions (`q08`, `q09`) stop finding their passage.

**An honest history.** The first run of this experiment (`eval/chunk_experiment_results_run1_before_fixes.json`)
gave recall@5 of 0.833, 0.833 and 0.722 for the same three settings. Looking at why `e01` ("what does
clause 1.21 require") and `v02` (a question about the 2023 rules) missed in every setting found three
bugs, not a chunk-size effect: the keyword match lost the rank fusion, "2023" was not recognised as
a request for the old version, and wrapped clause lines were taken for headings. They are fixed
(commit "fix(retrieval): weight exact terms in the fusion..."), with regression tests, and the table
above is the second run. Caveat: the exact-term weight was chosen after seeing those misses on this
golden set, so the perfect 1.000 is an optimistic figure for questions of that shape; the set has
only 18 answerable questions.

After this table two small extraction changes were made (merged-cell squeeze, labelled values are not
headings). They alter chunk text slightly; the index that ships is evaluated on its own in
`eval/results.json`.

## Embedding (stage 4: `embed.py`)

| Item | Value |
| --- | --- |
| Model | `nomic-embed-text` (Ollama), served by Ollama on the host |
| Vector dimension | 768 |
| Where the pin lives | `EMBED_MODEL` in `k8s/base/configmap.yaml`, read by the indexer and the api |
| Recorded in | Chroma collection metadata: `embed_model`, `dimension` (written by `store.py`) |
| Checked by | `api/app/retrieval/pin.py` and `api/app/readiness.py` on every `/readyz` |

The model expects task prefixes: documents are embedded as `search_document: <text>` and questions
as `search_query: <text>`. Both halves are needed; a mismatch silently worsens recall.

## Storage (stage 5: `store.py`)

601 chunks at 400/50 in the experiment (the production count is in the Job log and in the collection
metadata `chunks`). Each chunk: id (`<document>-<position>`, stable), text, vector, and the metadata
schema below. The collection metadata adds `embed_model`, `dimension`, `index_id`, `indexed_at`,
`chunk_size`, `chunk_overlap`, `chunks`.

| Metadata field | Type | Meaning |
| --- | --- | --- |
| `source` | string | file name, e.g. `academic-rules-handbook-jun-2026.pdf` |
| `section` | string | the heading the chunk sits under |
| `page` | int | PDF page where the chunk starts |
| `doc_version` | string | from `corpus/sources.yaml`, e.g. `2026-06` |
| `superseded` | bool | true for an older version of a policy |

Re-indexing upserts in place when the model and dimension are unchanged and rebuilds the collection
otherwise (ADR 0001).

## Retrieval (`api/app/retrieval/chroma.py`)

1. Embed the question with the pinned model.
2. Version filter: `where={"superseded": False}` unless the question asks for an older version
   (`versions.py`: words such as "previous" or "old", a named superseded version such as "2019-20", or
   a bare year that only a superseded document carries; a year range like "2023-24" names a current
   document and is not read as old).
3. Dense search with Chroma `query()` (cosine), and BM25 over the chunk text held in memory.
4. A third ranking of passages that contain the question's **exact terms** (clause numbers like 1.21,
   decimals like 2.00, amounts like 10,000, codes like CS-4032), counted three times in the fusion.
5. Reciprocal rank fusion (k = 60) of the rankings; the top `TOP_K` (5) go to the model.

Each passage's score is its cosine similarity, so abstention does not depend on which list found it.

**Limitations.** BM25 is built in every replica's memory from the chunk text, so a re-index is picked
up within 30 seconds, not instantly. The tokenizer is English-only. Rank fusion and the exact-term
weight are tuned on a small golden set. Dense similarity compresses into a narrow band (all scores
here are between 0.59 and 0.88), so no floor separates answerable from unanswerable questions
perfectly (below).

## Relevance floor and abstention

Best dense similarity per golden question (`eval/threshold_experiment.py`):

| Group | Scores |
| --- | --- |
| must-abstain `a01`, `a02` (hostel) | 0.591, 0.604 |
| must-abstain `a03` (transport timetable and fee) | 0.760 |
| answerable, with an exact term (`i02`, `e02`, `e01`) | 0.641, 0.667, 0.680 |
| answerable, plain (15 questions) | 0.756 to 0.881 |

**Shipped: `RELEVANCE_FLOOR=0.70`**, between the declined hostel questions (at most 0.604) and the
lowest plain answerable question (0.756). Passages that contain the question's exact term count as
relevant whatever their score, because embeddings blur those strings (0.64 to 0.68 against the floor).
Below the floor the API declines **without calling the model**. `a03` is the honest weak spot: the
corpus does mention transport, so its similarity (0.760) is above any workable floor and the decision
falls to the model's `answered_from_context`. Measured on the live stack (below): the hosted model
declined it. A second rescue was added after the first live run: `e02` (the Late Registration Fee, a table
cell reading "Late Registration Fee/Freeze Fee") scored 0.667 and was wrongly declined before the model
saw it, so a passage that contains three consecutive content words of the question also counts as relevant
(`api/app/services/answer.py`, `_phrases`). A single shared word is not enough; a test covers both.

## Semantic cache threshold

Pairs of questions (`eval/threshold_experiment.py`): six with the same meaning (a hit is right) and
eight near-misses that differ in one detail (a hit serves a wrong answer).

| Threshold | False hits (of 8) | True hits (of 6) |
| --- | ---: | ---: |
| 0.85 | 5 | 6 |
| 0.88 | 5 | 3 |
| 0.90 | 5 | 3 |
| 0.92 | 3 | 2 |
| **0.94** | **0** | **1** |
| 0.96 | 0 | 1 |

The false hits that ruled out 0.92 and below: "tuition fee for 2023-24" answered from "2026-27"
(similarity 0.938), "clause 1.22" from "clause 1.21" (0.927), "repeat" from "register" in summer
(0.920). **Shipped: `CACHE_THRESHOLD=0.94`**, the lowest tried with zero false hits. The cost is a low
hit rate for paraphrases; an identical question scores 1.0 and always hits.

## Evaluation method

`eval/run_eval.py` asks every golden question and matches each relevant entry to the retrieved list
by **source plus page** (PDFs: a fact on a page break lists both pages) or **source plus section prefix**
(HTML and Markdown), not by chunk id, so the golden set survives re-chunking. recall@k is the fraction
of a question's relevant passages found in the top k, reported overall and by question type.
Citation validity counts citations that match a retrieved passage, including those the API had to
remove (`invalid_citations_removed`). Abstention correctness must be 1.0. The CI gate fails when recall@5
drops more than 5 points below `eval/baseline.json`, when any must-abstain question is answered, or when
citation validity is below 95%. Recall is worst for exact-term questions when dense-only (0.67), which is
why retrieval is hybrid; the lecture technique that addresses it is hybrid search.

## Final measured results (live stack)

Run through the Ingress on minikube with the 572-chunk index, `LLM_PROVIDER=hosted` (Groq,
`openai/gpt-oss-20b`, temperature 0.1), embeddings from Ollama `nomic-embed-text`, Redis cache emptied
first so no answer came from the cache (`eval/results.json`, 21 questions):

| Metric | Result |
| --- | ---: |
| recall@3, recall@5 (all 18 answerable questions) | 1.00, 1.00 |
| recall@5 by type: ordinary, version, exact term, injection | 1.00 each |
| citation validity (28 citations checked) | 1.00 |
| must-abstain questions answered | 0 of 3 |
| false abstentions (answerable but declined) | 0 of 18 |
| slowest question | 25 s (rate-limit waits on the free tier) |

What these numbers do and do not say. The golden set is small, so 1.00 means "no miss found", not
"cannot miss". Recall was already 1.00 for the hybrid retriever before generation was available; the
generation-side numbers (citations, abstention, false abstentions) are the ones that needed the model,
and two real defects were found by this run and fixed: the floor wrongly declining a fee-table question
(above) and the hosted client giving up on a rate limit instead of waiting for it. The earlier attempt with
the local CPU model (`llama3.2:3b`, about 2 tokens per second) declined to answer from a correct passage,
which is why the hosted mode is what produced these figures. The default mode in the ConfigMap remains
`ollama-host`; the hosted mode is selected by the `dev-hosted` and `prod` overlays.
