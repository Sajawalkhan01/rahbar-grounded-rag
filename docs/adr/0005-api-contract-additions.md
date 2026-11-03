# ADR 0005: Additions to the API contract, and the embedding pin in readiness

## Status
Accepted · 4 Nov 2026

## Context
The starter kit says to explain any change to the request, the response or the chunk metadata
in an ADR. Three requirements could not be met by the original contract:
* **Follow-up questions must skip the cache**, but `POST /api/ask` carried only a question, so
  the server could not know there was an earlier turn.
* **The UI shows a cache chip**, reading `cached` from the response.
* **Citation validity must be measurable.** The API removes any citation that matches no
  retrieved passage, so counting only the returned ones would read 100% whatever the model did.

## Decision
Backward-compatible additions only (`api/app/schemas.py`); every starter field and test is
unchanged.

| Where | Field | Meaning |
| --- | --- | --- |
| request | `session_id` (optional, 8-64 chars of `[A-Za-z0-9_-]`) | groups turns so a follow-up can be recognised; a client that omits it is treated as asking standalone questions |
| response | `cached` (bool, default false) | the answer came from the semantic cache |
| response | `invalid_citations_removed` (int, default 0) | model citations that matched no retrieved passage and were dropped |
| `Passage`, `RetrievedRef` | `doc_version`, `superseded` | shown in the retrieved-passages panel and logged |

The chunk metadata keeps exactly the five required fields (`source`, `section`, `page`,
`doc_version`, `superseded`). The collection metadata gains `embed_model`, `dimension`,
`index_id`, `indexed_at`, `chunk_size`, `chunk_overlap` and `chunks`.

**The embedding pin lives in readiness.** `/readyz` returns 503 naming each failed check
(`chroma`, `collection`, `pin`, `redis`). The `pin` check compares the collection's
`embed_model` and `dimension` with the API's own `EMBED_MODEL` and the vector size its model
really produces, and logs the exact conflict. `/healthz` touches nothing.

## Options considered
* **Detect follow-ups from the text alone**: impossible for "and for labs?" without history.
* **Send the whole chat history in every request**: grows without bound and needs the
  validation and limits the 500-character question rule exists to avoid.
* **A new endpoint**: forces the UI and eval script to change; optional fields do not.

## Consequences
* The shipped UI sends no `session_id`, so every question there is standalone unless it is
  updated to send one (a two-line change). The API and tests support it either way.
* The follow-up detector is a heuristic (`api/app/services/followup.py`); it errs towards
  "follow-up", which only costs a skipped cache hit.
