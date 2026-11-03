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
