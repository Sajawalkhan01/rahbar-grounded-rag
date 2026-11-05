# Engineering notes

Answers to the seven questions in section 7 of the case study. Every answer points at this
repository's own files; line numbers are from the commit this file was written against.

## 1. Indexing in API startup: what breaks with 3 replicas and one Chroma volume?

**Failure: three writers, one index, and no service while they work.** The API Deployment has
three replicas (`k8s/base/api.yaml:8`). If each one indexed at startup, all three would extract,
OCR and embed the whole corpus at the same moment and write the same chunk ids into the same
collection. On this laptop one full index takes about 70 minutes (572 chunks, 4,195 s in the
indexer log), so for that long no replica could pass `/readyz` and the Service would have no
endpoints. The writes also race: `store()` upserts, then deletes every id it did not just write
(`indexer/pipeline/store.py:64-95`, `_delete_stale`), and `_open_or_rebuild`
(`store.py:97-108`) deletes and recreates the whole collection when the embedding model differs.
Replica A could delete what replica B had just written, or drop the collection under B's feet.
Every rolling update (`maxSurge 1`, `api.yaml:14-15`) and every HPA scale-up would repeat the
work, so autoscaling would make a traffic spike slower, not faster.

**What prevents it:** indexing is a Kubernetes Job, run once to completion, and the API only reads.
The object is the `indexer` Job (`k8s/base/indexer-job.yaml:6`, `kind: Job`) with
`restartPolicy: OnFailure` (`:23`), `backoffLimit: 3` (`:12`) and `activeDeadlineSeconds: 5400`
(`:13`). The API pod has no write path to Chroma at all, and the NetworkPolicy lets the indexer
reach Chroma as a separate allowed client (`k8s/base/networkpolicy.yaml`, policy
`chroma-from-api-and-indexer`). Chroma itself is a single StatefulSet replica whose
`volumeClaimTemplates` (`k8s/base/chroma.yaml:67`) gives it one ReadWriteOnce volume, so there is
exactly one writer process and one copy of the data. Reasoning and the alternatives are in
`docs/adr/0001-indexer-as-job.md`.

## 2. Readiness and liveness swapped, Chroma slow for 90 seconds: what happens?

Today liveness is `/healthz`, which touches nothing (`api/app/routes/health.py:10-13`;
`k8s/base/api.yaml:50-53`, `periodSeconds: 20`, `failureThreshold: 3`), and readiness is `/readyz`,
which checks Chroma, the collection, the embedding pin and Redis (`health.py:16-27`,
`api/app/readiness.py:36`; `api.yaml:54-57`).

Swap them and Chroma stalls for 90 s:
* The liveness probe now calls the Chroma-dependent check. It fails every 20 s, and after three
  failures (60 s) the kubelet **kills the container**. Chroma is slow for all three replicas at
  once, so all three are restarted within the same few seconds.
* Each restart empties the in-process caches (the query-embedding LRU, the BM25 snapshot), and the
  new containers must pass the startup probe (up to 60 s, `api.yaml:46-49`) before they can serve.
  Chroma recovers at 90 s but the API is still restarting, so the outage is longer than the
  Chroma problem that caused it.
* The readiness probe, now checking only `/healthz`, says "ready" the whole time. The Service keeps
  sending questions to pods that cannot reach Chroma, so users get errors instead of being held
  back from the endpoints list.

With the correct wiring, the same 90 s slowness only takes the pods out of the Service
(readiness fails, `0/1`), nothing is restarted, and they return on their own at the next
successful probe. That is what the guardrail demo shows in `docs/evidence/10-embed-mismatch.txt`:
`0/1 Running` with `RESTARTS 0`.

## 3. What does a CPU-based HPA measure on a generation service, and what should scale it?

While the model generates, the API pod is **waiting**, not computing. The call is a blocking HTTP
request to Ollama or the hosted API (`api/app/providers/llm/ollama.py:41`, `hosted.py`), so the pod's
CPU stays low however many questions are in flight. `docs/evidence/05-kubectl-top.txt` shows
six api pods at 5 to 6 m of a 100 m request (`k8s/base/api.yaml:62`) and one pod at 165 m, the one
I was running a debug script in. During the earlier stretch when answers took 70 to 90 seconds each
the pods read 20 to 22 m. The HPA (`k8s/base/hpa.yaml:18-23`, CPU target 70 %) reads that and sees
an idle service while users queue. What it does measure is the short bursts of real work in the
pod: embedding the question, BM25, fusion, JSON validation, start-up, and anything I run inside it.
In `docs/evidence/07b-hpa-scaling-events.txt` the scale-up from 3 to 7 replicas happened while I was
running debug scripts and rolling restarts, not under question traffic, which is the same point
from the other side: CPU moves with our own housekeeping, not with how many people are waiting.

The signal that should scale it is **how many requests are waiting on the model**: requests in
flight per replica, or the p95 of `rahbar_generation_seconds` (`api/app/routes/metrics.py:36-40`,
already exported). To scale on it we would add a gauge of in-flight requests, run Prometheus to
scrape `/metrics`, and expose that series to the HPA through prometheus-adapter or KEDA
(`autoscaling/v2` with a `Pods` or `External` metric). The extra gauge and the adapter are the
parts not built here. Note also that more API replicas do not help when the bottleneck is the single
Ollama on the laptop: the real scale unit there is the model server.

## 4. The indexer's peak memory, its limit, the margin, and the exit code when it is wrong

**Measured.** I re-ran the indexer from the CronJob template and sampled `kubectl top` every 15 s
(`docs/evidence/16-indexer-memory.txt`). The peak was **1,365 MiB**, reached about 15 minutes in,
while it was extracting and OCR-ing the scanned PDFs (Tesseract renders each page to an image). The
embedding phase is much smaller: 0.46 to 0.55 GiB in the later samples.

**Limit and margin.** The indexer container has a request of 1 GiB and a **limit of 3 GiB**
(`k8s/base/indexer-job.yaml:60-62`, copied in `k8s/base/reindex-cronjob.yaml`). The margin is about
1.6 GiB, or 2.2 times the peak. It is not tighter on purpose: peak memory depends on the page size of
the next scanned document, and the cost of a limit that is too low is a Job that dies after an hour of
work. The request (1 GiB) is below the peak, which is allowed: the pod is scheduled with 1 GiB
reserved and may burst up to the limit. The fetch init container is a different, small container
with a 256 MiB limit (`indexer-job.yaml:42`).

**If the limit were wrong.** A limit below the peak (for example 1 GiB) makes the kernel kill the
process when it crosses it: the container ends with `OOMKilled` and **exit code 137** (128 + signal
9), visible in `kubectl describe pod` under "Last State". It is not a Python traceback, so the
indexer's own error handling (`indexer/pipeline/run.py:113-115`, which returns exit code 1) never runs.
With `restartPolicy: OnFailure` (`indexer-job.yaml:23`) the container is restarted in the same pod,
and after `backoffLimit: 3` (`:12`) the Job is marked Failed. The mistaken-limit failure therefore
looks like a slow Job that keeps restarting, not like an obvious error.

**Caveat on this measurement.** The re-run did not finish: the laptop went to sleep for about 11.5
hours after the OCR phase, so the Job's 90-minute `activeDeadlineSeconds` (`:13`) expired by wall
clock and Kubernetes ended it. The peak above is from the phase that matters (OCR), which the
sampler captured in full, but I have no end-to-end sample for a complete run. The first complete run
(572 chunks in 4,195 s, from the Job log) ran at the same limits without an OOM kill.

## 5. recall@5 by question type: where is it worst, and why?

Measured with `eval/chunk_experiment.py` on the 18 retrievable golden questions
(`eval/chunk_experiment_results.json`, 400-word chunks, 50 overlap):

| Type | dense only recall@5 | hybrid (final) recall@5 |
| --- | ---: | ---: |
| ordinary (10) | 1.00 | 1.00 |
| version (3) | 1.00 | 1.00 |
| exact term (3) | **0.67** | 1.00 |
| injection (2) | **0.50** | 1.00 |

The worst type for embeddings alone is the **exact-term** questions (clause numbers, amounts), and
the injection questions, which hide a clause number in a longer sentence. A dense embedding
captures meaning and blurs digits: "clause 1.21" and "clause 1.22" look almost the same to the
model, and "PKR 10,000" sits near any text about fees. The missed ids were `e01` and `i02`.

The technique from the lecture is **hybrid retrieval**: BM25 keyword search fused with the vector
ranking by reciprocal rank fusion (`api/app/retrieval/chroma.py:164-210`). A first hybrid attempt
still lost the exact clause because a passage found weakly by both lists beat one found strongly by
only one, so `chroma.py:45` adds a third ranking, passages containing the question's exact terms
(`bm25.py`, `exact_terms`), weighted 3x (`EXACT_TERM_WEIGHT`, `chroma.py:196`). With it every type
reaches 1.00 on this set. Caveat: the set is small (18 questions), so 1.00 means "no miss found",
not "cannot miss". `docs/PIPELINE.md` has the chunk-size table and the failure of the first attempt.

## 6. EMBED_MODEL changed on the API only: what does your system do?

It refuses to serve. The indexer writes the model name and vector size into the collection metadata
(`indexer/pipeline/store.py:71-72`). The API compares them with its own `EMBED_MODEL`
(`k8s/base/configmap.yaml:11`) in `/readyz` (`api/app/readiness.py:69-90`, `retrieval/pin.py:30-53`).
On a mismatch `/readyz` returns 503 naming both models, the pod stays `0/1 READY`, and the log gets
one line with `api_embed_model` and `index_embed_model`. I ran it on minikube
(`docs/evidence/10-embed-mismatch.txt`): all three pods `0/1`, and

```
embedding model mismatch: API uses 'mxbai-embed-large', index was built with 'nomic-embed-text'
```

A rolling update keeps the old ready pods (`maxUnavailable: 0`), so a bad ConfigMap edit followed by
a rollout cannot take down a working service. The check also names the wrong model when that model is
not installed on the host; the first version reported only "cannot verify" in that case, which I
changed after seeing it (`readiness.py:73-78`, test in `api/tests/test_readiness_pin.py`).

Without the guardrail the failure is silent. Two embedding models put text in different vector
spaces. If the sizes differ, Chroma raises a dimension error on the first question (500s). If they
match, which is the dangerous case, every query vector is compared with document vectors from a
different space: similarities are meaningless, retrieval returns plausible-looking but unrelated
chunks, the relevance floor may still pass some, and the LLM answers from the wrong text with
citations that match what was retrieved. Nothing would raise. Only the golden-set recall would show
it, and only if someone ran it.

## 7. The failure that cost us more than an hour

**Symptom (4 Nov 2026, first `e2e-eval` run in GitHub Actions):** `chroma-0` in `CrashLoopBackOff`,
the indexer Job never completed (15 minute wait expired) and all three api pods crash-looped. It
worked in unit tests and on my laptop.

**What I wrongly believed first:** that my own security hardening was the cause: non-root user,
`readOnlyRootFilesystem: true`, dropped capabilities (`k8s/base/chroma.yaml`). I reproduced a
plausible failure locally with `docker run --user 1000 --read-only ...` and a root-owned volume
("unable to open database file"), which looked like confirmation. I then spent time on `fsGroup`
and volume ownership. With the volume owned by uid 1000 Chroma ran fine, so that was not what
happened in the cluster; I had reproduced a different bug.

**What told me the truth:** deploying the same manifests to the real minikube cluster and running

```
kubectl logs chroma-0 -n rahbar --previous
Error loading config: invalid type: found string "tcp://10.99.231.230:8000",
expected u16 for key "PORT" in CHROMA_ environment variable(s)
```

Kubernetes injects `<SERVICE>_PORT=tcp://ip:port` into every pod for every Service in the
namespace. The Service named `chroma` therefore gave the Chroma pod an environment variable
`CHROMA_PORT`, and Chroma reads every `CHROMA_*` variable as its own configuration. The fix is
`enableServiceLinks: false` on every pod template (`k8s/base/chroma.yaml:23`, `api.yaml:23`,
`indexer-job.yaml:21`). The same run exposed a second bug: the API crashed at startup instead of
reporting "not ready", because `chromadb.HttpClient` connects in its constructor. The client is now
created lazily (`api/app/retrieval/chroma.py`) with a regression test.

**What would have found it faster:** reading `--previous` logs of the crashing pod first, instead
of the API's logs. The CI job now dumps `kubectl logs --previous` for every crashing pod
(`.github/workflows/ci.yml`, step "Show the cluster on failure"). The lesson I keep from it: a
failure that reproduces only in the cluster should be investigated in the cluster before building a
local theory.
