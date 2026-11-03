# ADR 0001: The indexer is a Kubernetes Job, not part of the API's startup

## Status
Accepted · 4 Nov 2026

## Context
RAHBAR has two jobs that look similar and are not. Indexing (extract, clean, chunk, embed,
store) reads every document, runs OCR on scanned pages and embeds thousands of chunks. It is
slow, memory-hungry and only needs to happen when the documents change. Answering a question
is fast and happens on every request.

The obvious design is "the API builds the index when it starts". It runs three API replicas
against one Chroma volume, and that is where it breaks:

1. **Three writers, one volume.** Each replica would start indexing at the same time and write
   to the same collection, each halfway through a different document. The result is an index
   that no document produced.
2. **Every restart re-indexes.** A rolling update, an OOM kill, a node drain or a readiness
   blip would repeat the whole extraction and embedding bill.
3. **Readiness stops meaning anything.** A pod either reports ready before the index exists
   and serves empty retrievals, or it takes many minutes to become ready and Kubernetes kills
   it halfway, so a rollout swaps three working pods for three that are still chunking PDFs.
4. **No re-index without a redeploy.** Changing the documents would mean rebuilding and
   rolling out the API image.

## Decision
Indexing is a separate image (`indexer/`) run as a **Job** (`k8s/base/indexer-job.yaml`), and
as a weekly **CronJob** (`reindex-cronjob.yaml`) with the same image and arguments. The Job
is the only writer to Chroma. The API only reads: nothing is chunked, embedded or added to a
collection in `api/app/main.py`'s startup, and the NetworkPolicy lets only the API and the
indexer reach Chroma.

The API's readiness probe (`/readyz`) reports not ready until Chroma is reachable, the
collection exists and is non-empty, and the index was built with the API's embedding model
(ADR 0005 and `api/app/readiness.py`). A pod without an index is withheld from the Service.

Re-indexing is one command with no redeploy and no downtime:
`kubectl create job --from=cronjob/reindex manual-reindex-1 -n rahbar`.

### What happens to the old index while the Job writes
(Decision asked for in `indexer/pipeline/store.py`.)
* Same embedding model and vector size: the Job **upserts** every chunk under a stable id
  (document + position), deletes ids that no longer exist, then writes a new `index_id` into
  the collection metadata. The collection is never empty or missing, so readiness does not
  flap while the API keeps serving.
* Different model or vector size: the old vectors are meaningless next to the new ones, so
  the collection is deleted and rebuilt. The pin check keeps the API out of the Service for
  that window instead of serving nonsense.
* Before anything is written the Job validates the batch (unique ids, equal vector sizes,
  required metadata), so a broken run fails before it can corrupt the index.
* The CronJob uses `concurrencyPolicy: Forbid`, so two indexers never overlap.

## Options considered
* **Index in API startup**: rejected for the four reasons above.
* **A leader-elected API replica indexes**: removes the race but keeps coupling indexing to
  serving, restarts and readiness, and adds a lock to maintain.
* **Bake the index into the image**: no runtime writer, but every document change needs an
  image build and a redeploy, and Chroma's data would no longer live on its PVC.
* **An init container on the API pods**: still runs once per pod, so three times and on every
  rollout.

## Consequences
* A new cluster has an empty index until the Job has run; readiness says so honestly.
* The Job needs more memory than the API (OCR and embedding); it has its own requests and
  limits (`indexer-job.yaml`), justified in `docs/ENGINEERING-NOTES.md`.
* Job pod templates are immutable: re-running means deleting the Job or creating one from the
  CronJob.
* Watch for: a long OCR run hitting `activeDeadlineSeconds`, and an OOMKill (exit code 137)
  on the indexer.
