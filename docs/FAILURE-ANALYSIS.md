# Failure analysis

Four of the six stages in section 5.5 were broken on purpose, one at a time, each against the same
working index and the same golden set (18 retrievable questions, `eval/golden.yaml`), scored with the
same recall functions as the CI gate. Tool: `eval/failure_experiments.py`; raw numbers:
`eval/failure_experiments_results.json`. The working baseline is the 400-word / 50-overlap index of
601 chunks (`rahbar_exp_400_50`), which scores recall@3 1.00 and recall@5 1.00 on every question
type. Stages **A, B, C and E** were run. D (prompt) and F (generation) were not, because they need
the language model and the only one available during these runs was a CPU model that already
declined to answer from correct passages; see the note at the end.

Order of diagnosis, from the lecture: only F is the model's fault, so check A to E first.

## Stage A · Extraction
- **What we broke:** indexed the scanned PDFs with OCR switched off (`OCR_ENABLED=false`,
  `indexer/pipeline/extract.py:49`). Their pages have no text layer, so each comes out empty.
- **Symptom:** recall@5 fell from 1.00 to **0.83** and recall@3 the same. Misses: `q09` (the
  entrepreneurship policy, 8 of 8 pages scanned) and `v02`, `v03` (the 2023 rules, 24 of 26 pages
  scanned). Version questions dropped to 0.33. The index shrank from 601 to 554 chunks. Nothing
  errored: the questions about those documents simply got passages from other documents, or
  abstained. Four pages in each of the two 2026 handbooks were also empty.
- **Diagnosis, as if we had not caused it:** the indexer log. Every page that stays empty produces
  an `extraction problem` line (24 for the 2023 PDF, 8 for the policy; `indexer/pipeline/run.py`) and
  the per-document `extracted` line shows `"ocr_pages": []` for documents that are known scans.
  Second signal: per-document chunk counts, where a 26-page rule book yielding 2 chunks is wrong.
  Third: golden-set recall by source: every miss is in two documents.
- **Remedy:** switch OCR back on (default `true`; the indexer image installs Tesseract and poppler).
  The 24 + 8 empty pages then become OCR text (`ocr_pages` lists them). A further guard is to make
  the Job exit non-zero when a document yields no text at all; today it logs the problem and carries
  on, which is how this failure stays silent.

## Stage B · Chunking
- **What we broke:** fixed 100-word windows, no overlap, no sense of headings or tables
  (`naive_chunks` in `eval/failure_experiments.py`). Section is just `page N`.
- **Symptom:** recall@3 fell from 1.00 to **0.72**, recall@5 to **0.83**. Misses: `q03`, `q04`
  (ordinary) and `e02` (an exact-term question); all three are fee-table questions graded by section
  name ("Tuition Fee", "Admission Fee", "Miscellaneous Fees"). The exact-term type fell to 0.67 and
  ordinary to 0.80. The index grew from 601 to 842 chunks. Be careful what this number says: the
  naive chunker has no headings, so part of the drop is the metric correctly refusing to credit a
  citation whose section is just "page 12". That is also a real cost: a user can no longer be shown
  "Tuition Fee" as the place the answer came from, and a table row is cut from its header.
- **Diagnosis, as if we had not caused it:** recall@3 falls much more than recall@5, which means the
  right text is still in the index but lands lower in the ranking because it is split across two
  chunks. Chunk count per document rose, and the `section` field in `retrieved` and in citations
  no longer holds headings. The chunk-size experiment in `docs/PIPELINE.md` shows the same family of
  effect from the other side (800-word chunks: 0.89).
- **Remedy:** restore the structure-aware chunker (`indexer/pipeline/chunk.py`: headings start
  sections, whole paragraphs and table row groups are packed to 400 words, 50 words of overlap,
  heading prepended to the chunk). The size was chosen by measurement (`docs/adr/0002-chunking-strategy.md`).

## Stage C · Retrieval
- **What we broke:** `TOP_K=1`: only the single best passage reaches the model.
- **Symptom:** recall dropped to **0.86** (recall@1 against 1.00). Misses: `q04`, `q07` and `v01`.
  The version type fell to 0.67. `q04` asks about two things that sit in two sections (Admission Fee
  and Security Deposit) and `q07` spans pages 30 and 31, so one passage cannot hold the whole answer;
  the model gets half the evidence and either answers partly or declines.
- **Diagnosis, as if we had not caused it:** the per-request log line carries `"top_k"` and the list
  of `chunk_ids` (`api/app/services/answer.py`, the `answered` line); with `top_k: 1` it lists one
  id. In the metrics, `rahbar_top_score` is unchanged (the best passage is still found), which
  separates this from an index or embedding problem: the retriever finds the right passage first but
  the answer is incomplete. recall@5 on the golden set is the number that moves.
- **Remedy:** `TOP_K` back to the ConfigMap value (`k8s/base/configmap.yaml`, 5 in prod; the dev
  overlay uses 3 because recall@3 is 1.00 and CPU inference pays for every prompt token). The other
  way to break this stage, `EMBED_MODEL` changed on the API only, was run on the cluster and is
  stopped by the pin check instead of degrading: see `docs/evidence/10-embed-mismatch.txt` and
  `docs/ENGINEERING-NOTES.md` question 6.

## Stage E · Source (versions)
- **What we broke:** `SUPERSEDED_FILTER=false`, so passages from the 2023 rules compete with the
  2026 rules (`api/app/config.py:55`; the filter is applied in `api/app/retrieval/chroma.py`).
- **Symptom:** recall stayed at **1.00**, and that is the point: the right current passage is still
  retrieved. What changes is that a superseded passage now appears in the top 3 for **10** questions
  instead of 2. The 2 that remain with the filter on (`v02`, `v03`) are the questions that ask
  about the 2023 rules on purpose. The other 8 (`q02`, `q06`, `q07`, `q10`, `v01`, `e01`, `e03`,
  `i02`) are questions about the current rules, now sitting next to an older version that says
  something different (75 % attendance against 80 %, for example).
- **Diagnosis, as if we had not caused it:** recall@k cannot see this failure. It shows only in
  (1) the `superseded` flag on each entry of `retrieved` in the API response, (2) the
  `[SUPERSEDED version]` marker the prompt prints for the model (`api/app/services/prompt.py`),
  and (3) the count of "questions with a superseded passage in the top 3", which this experiment
  adds and the CI gate does not yet. A wrong answer would be fluent, cited, and correct for last
  year's rules.
- **Remedy:** restore the filter (default `true`). The version-aware logic
  (`api/app/retrieval/versions.py:32`) lets an older version through only when the question asks
  for one (older words, a named superseded version, or a year that only the old document has). Next
  step, not done: add the superseded-in-top-3 count to `eval/run_eval.py` so the gate can fail on it.

## Stages not run
* **D · Prompt** (remove the "use the supplied context" rule) and **F · Generation** (temperature
  1.2) change only what the language model does with correct passages. During these runs the
  only model available was `llama3.2:3b` on a CPU at about 2 tokens per second, which already
  answered "N/A" to a question whose correct passage was ranked first, so a controlled
  comparison would have measured noise. They are left unrun rather than reported from a
  single sample.
