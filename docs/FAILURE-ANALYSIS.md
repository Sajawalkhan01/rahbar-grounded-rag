# Failure analysis

Four of the six stages in section 5.5 were broken on purpose, one at a time, each against the same
working index and the same golden set (18 retrievable questions, `eval/golden.yaml`), scored with the
same recall functions as the CI gate. Tool: `eval/failure_experiments.py`; raw numbers:
`eval/failure_experiments_results.json`. The working baseline is the 400-word / 50-overlap index of
601 chunks (`rahbar_exp_400_50`), which scores recall@3 1.00 and recall@5 1.00 on every question
type. Stages **A, B, C and E** change only what is retrieved, so they were measured first, on the
retrieval side. Stages **D (prompt) and F (generation)** need a real language model and were run
afterwards on the live stack with the hosted model (section at the end): they did not reproduce a
visible failure, and that is reported as it is. All six stages were run.

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

## Stages D and F · measured on the live model

Tool: `eval/failure_experiments_llm.py` (run inside an api pod, where the hosted provider and its
key already exist); data: `eval/failure_experiments_llm_results.json`. Model: `openai/gpt-oss-20b`
on Groq. Twelve golden questions (the three must-abstain, both injection questions, three ordinary,
three version, one exact term), cache off so every answer is generated, **one run per condition**.

| Condition | What changed | Answered (of 12) | Must-abstain answered | False abstentions | Fallbacks | Invented citations removed | Prompt leaked |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| base | nothing (floor 0.70, temperature 0.1) | 9 | 0 | 0 | 0 | 0 | 0 |
| base, floor off | relevance floor 0.0 | 9 | 0 | 0 | 0 | 0 | 0 |
| **D** | rule 1 removed from the system prompt | 9 | 0 | 0 | 0 | **1** | 0 |
| D, floor off | rule 1 removed and floor 0.0 | 9 | 0 | 0 | 0 | 0 | 0 |
| D, strong | rules 1 and 3 removed ("answer only from the passages", "say you do not know"), floor 0.0 | 9 | 0 | 0 | 0 | 0 | 0 |
| **F** | temperature 1.2 | 9 | 0 | 0 | 0 | 0 | 0 |

### Stage D · Prompt
- **What we broke:** removed rule 1 ("Answer ONLY from the numbered evidence passages ... use the
  supplied context, never your own knowledge") from `SYSTEM_PROMPT` (`api/app/services/prompt.py:19`);
  then, as the strongest version, rule 3 as well (the instruction to decline when the evidence does
  not contain the answer) with the relevance floor off, so the prompt was the only defence left.
- **Symptom:** almost none. All three must-abstain questions were still declined in every variant,
  nothing leaked the prompt, and no answerable question was declined. The only movement: with only
  rule 1 removed (floor on) the model produced **one citation that matched no retrieved passage**, and
  the citation check removed it (`invalid_citations_removed` 1 against 0). In the other D conditions
  that number was 0, so it is one observation in one run, not a trend.
- **Diagnosis, as if we had not caused it:** the numbers that would show a weakened prompt are
  `rahbar_citations_removed_total` (the model invents citations), the must-abstain answers in the
  evaluation gate, and answers whose text is not in the retrieved passages. Here the first moved
  once and the others did not move at all, so on this evidence alone the change would have gone
  unnoticed. That is itself the finding: the prompt rule is one layer of several. What held was the
  structured output (the model sets `answered_from_context` itself), the validator, the citation
  check, and the model's own behaviour; the relevance floor was not needed for these twelve questions.
- **Remedy:** keep rule 1 in the prompt (it is cheap and it stops the first fabricated citation),
  and keep the guardrails that do not depend on the model's good behaviour.
- **What this does not show.** One model, one run per condition, twelve questions. A weaker or
  differently trained model (the local `llama3.2:3b` was already unreliable at the task) may well
  answer from its own memory without rule 1. This experiment found no effect on this model; it did
  not prove there is none.

### Stage F · Generation
- **What we broke:** `TEMPERATURE` 1.2 instead of 0.1 (`k8s/base/configmap.yaml`, read through
  `api/app/config.py`; the hosted provider sends it in the request, `api/app/providers/llm/hosted.py`).
- **Symptom:** none measurable on this model: the same nine questions were answered, the same three
  declined, no fallback (output that failed validation twice), no invented citation.
- **Diagnosis, as if we had not caused it:** the numbers that would show it are
  `rahbar_fallbacks_total` (the model's JSON failed validation twice), the `"attempts": 2` field in the
  per-request log line, `rahbar_citations_removed_total`, and answers that differ between two runs of
  the same question. None of these moved. Whether the provider limits the value, or this model is
  simply robust at 1.2, was not tested, so no explanation is claimed.
- **Remedy:** `TEMPERATURE` back to 0.1; facts and JSON belong at 0 to 0.3 (the comment in the
  ConfigMap and `docs/adr/0004-llm-provider-and-data-governance.md`).

### What D and F, together with A to E, say
Stages A, B, C and E each cost measurable recall or put an old version in the answer's context, and
they were all caught by the retrieval metrics. D and F, which are the model's part of the pipeline,
did not change any metric here, because the pipeline checks the model's output (JSON schema,
citation matching, validation with a retry and a fallback) instead of trusting it. The lecture's
advice to check A to E before blaming the model is consistent with that: on this system the damage
came from retrieval and sources, not from the generation step.
