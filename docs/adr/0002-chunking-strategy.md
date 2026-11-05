# ADR 0002: Structure-aware chunking at 400 words with 50 words of overlap

## Status
Accepted · 4 Nov 2026

## Context
A chunk is the unit retrieval returns and a citation points at. Too small, and a clause is cut
from the condition that qualifies it; too large, and one embedding averages several topics so a
specific question no longer sits near it. The corpus has long numbered clauses, definitions that run
for a page, tables, and a scanned policy whose structure is partly lost. The handout requires
structure-aware splitting (sections, then paragraphs, then sentences) with overlap, and requires the
size to be chosen by measurement, not by habit.

## Decision
`indexer/pipeline/chunk.py`:
* A heading starts a new section and is never overlapped across. Headings come from the extraction
  stage (a larger or bold font on PDFs, `#` on HTML and Markdown) or from a numbered capitalised title.
* Within a section, whole paragraphs and table row groups are packed up to **400 words**. A paragraph
  is cut only when it alone exceeds the size, first by sentence, then by word. A table is cut by
  rows with its header row repeated.
* Neighbouring chunks of a section share **50 words**, whole sentences where possible. A tail that
  would push the next piece over the size is dropped.
* The heading is prepended to the chunk text so the embedding and the keyword index both see it.
* Size is counted in words (about 1.3 model tokens each): no tokenizer is needed in the indexer and
  the unit is easy to explain.

The size and overlap were measured at three settings against the golden set (`docs/PIPELINE.md`):

| Size / overlap | Chunks | recall@3 | recall@5 |
| --- | ---: | ---: | ---: |
| 150 / 20 | 862 | 0.917 | 1.000 |
| **400 / 50** | **601** | **1.000** | **1.000** |
| 800 / 100 | 559 | 0.889 | 0.889 |

## Options considered
* **Fixed-size windows (for example 100 words, no overlap)**: simplest, but cuts definitions and
  tables mid-item and ignores headings, so a citation cannot name a section. It is also the
  deliberate break in `docs/FAILURE-ANALYSIS.md`, where its effect is measured.
* **150 words**: more, smaller chunks (862); recall@3 falls because a clause loses its context.
* **800 words**: fewer chunks (559) but recall drops to 0.889: a chunk spans several topics and two
  ordinary questions no longer find theirs.
* **Chunking by page**: pages cut sections in two and bear no relation to the meaning of the text.
* **Semantic chunking with embeddings**: would need an embedding call per sentence at index time for
  a gain this corpus's headings already provide.

## Consequences
* Storage amplification is 1.014 (stored words divided by original words), so overlap is cheap.
* Headings must be detected reliably or sections degrade. Three detection bugs were found and fixed by
  measurement (wrapped clause lines read as headings, a labelled value "G. Total 428" read as a heading).
* Scanned documents carry no font information, so their sections rely on numbered titles and are
  coarser; the scanned policy's golden question is matched by page for that reason.
* The result is specific to this corpus and this 18-question golden set. If the corpus or the
  embedding model changes, re-run `eval/chunk_experiment.py` before keeping 400/50.
