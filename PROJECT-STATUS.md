# Project status

Updated 4 Nov 2026. Status words mean exactly this:

* **VERIFIED**: I ran it and saw the result (the evidence is named).
* **WRITTEN**: the code or file exists and unit tests pass, but it has not been run for real.
* **BLOCKED**: needs an action by the student (account, settings, screenshot).
* **TODO**: not done.

## Requirements

| Area | Requirement | Status | Evidence / note |
| --- | --- | --- | --- |
| Corpus | 9 public documents, 7 properties, `sources.yaml`, `MANIFEST.md` with SHA-256 | VERIFIED | `corpus/fetch.py` downloaded all 9; `docs/CORPUS.md` |
| Corpus | OCR of scanned PDFs | VERIFIED | 24 + 8 pages OCR'd; clause numbers of the 2023 rules recovered |
| Indexer | extract, clean, chunk, embed, store | WRITTEN | 72 unit tests; real index build in progress |
| Indexer | three chunk configurations measured | TODO | `eval/chunk_experiment.py` is running |
| API | hybrid retrieval, version filter, floor, cache, validation, citations | WRITTEN | 85 unit tests, 93% coverage; no real model yet |
| API | embedding-pin guardrail in `/readyz` | WRITTEN | unit-tested; cluster demo TODO |
| Eval | golden set (21 questions), metrics, gate | WRITTEN | 18 tests; no measured numbers yet |
| Docker | three images build, non-root | VERIFIED | built locally and in CI |
| Kubernetes | all objects, probes, HPA, NetworkPolicies, overlays | WRITTEN | render and pass `kubeconform -strict` (CI); not applied to a cluster yet |
| Kubernetes | deploy, index, ask, delete a pod, scale, rollout, rollback | TODO | |
| CI | lint, test (coverage 60%+), manifests, build | VERIFIED | first GitHub run on `dev`: all green |
| CI | Trivy scan, e2e-eval | IN PROGRESS | see below |
| CD | test, build-push, deploy, smoke | BLOCKED | needs Docker Hub secrets (below) and a merge to `main` |
| Docs | ADRs 0001, 0003, 0004, 0005; CORPUS | WRITTEN | |
| Docs | ADR 0002, PIPELINE, FAILURE-ANALYSIS, ENGINEERING-NOTES, AI-USAGE, README | TODO | need measured numbers |
| Evidence | 10 screenshots / outputs | TODO / BLOCKED | some need the student's browser or GitHub |

## Things only the student can do

* Add the instructors as collaborators (Settings, Collaborators).
* Protect `main` once CI has run (see the README).
* Create the Docker Hub access token and add the secrets `DOCKERHUB_USERNAME` and
  `DOCKERHUB_TOKEN` (Settings, Secrets and variables, Actions). The Docker Hub repositories
  `rahbar-api`, `rahbar-indexer` and `rahbar-ui` must be public so the ephemeral cluster can pull.
* Take the GitHub screenshots (branch protection, blocked merge, green merge).

## Decisions worth defending

See `docs/adr/`. In short: the indexer is a Job (0001); chunking is structure-aware and its
size was measured (0002); Redis is the cache (0003); the LLM is pluggable and the data that
leaves the cluster is stated (0004); the contract additions are backward-compatible (0005).

## Incident log (raw material for docs/ENGINEERING-NOTES.md question 7)

**4 Nov 2026: chroma-0 in CrashLoopBackOff, and the api pods with it.**
* Symptom: in the first GitHub e2e run (`e2e-eval`), `chroma-0` was `0/1 CrashLoopBackOff`, the
  indexer Job never completed (15 min timeout) and all three api pods crash-looped.
* What I believed first: the hardening (`runAsUser: 1000`, `readOnlyRootFilesystem: true`,
  dropped capabilities) was blocking Chroma from writing its SQLite files. I reproduced that
  locally with `docker run --user 1000 --read-only ...`: with a root-owned volume it did fail
  ("unable to open database file"), which seemed to confirm it. With the volume owned by uid 1000
  (what `fsGroup` does) Chroma ran fine, so that was not the cause in the cluster.
* The truth: `kubectl logs chroma-0 --previous` on the real minikube cluster printed
  `Error loading config: invalid type: found string "tcp://10.99.231.230:8000", expected u16 for
  key "PORT" in CHROMA_ environment variable(s)`. Kubernetes injects `<SERVICE>_PORT=tcp://ip:port`
  variables for every Service, so the Service named `chroma` gave the pod `CHROMA_PORT`, and
  Chroma reads every `CHROMA_*` variable as its own configuration.
* Fix: `enableServiceLinks: false` on every pod template (commit "fix(k8s): disable service links").
* Second bug found in the same run: the API crashed at startup instead of reporting not ready,
  because `chromadb.HttpClient` connects in its constructor. The client is now created lazily
  (`api/app/retrieval/chroma.py`) and a regression test covers it.
* What would have found it faster: dumping `kubectl logs --previous` of every crashing pod in CI
  (now done), instead of only the api logs.
