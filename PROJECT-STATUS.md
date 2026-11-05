# Project status

Updated 5 Nov 2026 (evening). Status words mean exactly this:

* **VERIFIED**: I ran it and saw the result (the evidence is named).
* **WRITTEN**: the code or file exists and unit tests pass, but it has not been run for real.
* **BLOCKED**: needs an action by the student (account, settings, screenshot).
* **TODO**: not done.

## Requirements

| Area | Requirement | Status | Evidence / note |
| --- | --- | --- | --- |
| Corpus | 9 public documents, 7 properties, `sources.yaml`, `MANIFEST.md` with SHA-256 | VERIFIED | `corpus/fetch.py` downloaded all 9; `docs/CORPUS.md` |
| Corpus | OCR of scanned PDFs | VERIFIED | 24 + 8 pages OCR'd in the cluster Job log |
| Indexer | extract, clean, chunk, embed, store as a Job | VERIFIED | 77 tests; Job indexed 572 chunks in 4,195 s on minikube |
| Indexer | three chunk configurations measured | VERIFIED | `eval/chunk_experiment_results.json`, `docs/PIPELINE.md`, ADR 0002 |
| API | hybrid retrieval, version filter, floor, cache, validation, citations | VERIFIED | 98 tests; answered, cached and declined on the live stack |
| API | embedding-pin guardrail in `/readyz` | VERIFIED | `docs/evidence/10-embed-mismatch.txt` |
| Eval | golden set (21 questions), metrics, gate | VERIFIED | `eval/results.json`: recall@5 1.00, citation validity 1.00, 0 false abstentions, 0 must-abstain answered |
| Failure analysis | stages broken on purpose | VERIFIED | all six in `docs/FAILURE-ANALYSIS.md`: A, B, C, E on retrieval; D and F on the live hosted model (no visible effect, reported as is) |
| Docker | three images build, non-root, no fixable HIGH/CRITICAL | VERIFIED | local Trivy scan clean after the ui fix |
| Kubernetes | all objects, probes, HPA, NetworkPolicies, overlays | VERIFIED | rendered and applied on minikube; evidence 04 to 15 |
| Kubernetes | delete a pod, scale, rollout, rollback, persistence | VERIFIED | `docs/evidence/09` to `14` |
| Kubernetes | resources justified with `kubectl top` | VERIFIED | `05`, `16-indexer-memory.txt`, `ENGINEERING-NOTES.md` question 4 |
| CI | lint, test (coverage 60%+), manifests, build | VERIFIED | green on GitHub |
| CI | Trivy scan, e2e-eval | VERIFIED | all 10 jobs green on `dev` (pull request #1): scans clean after base-image, FastAPI/starlette and pdfplumber updates; e2e-eval passes on kind |
| CD | test, build-push, deploy, smoke | VERIFIED | run `42039241137` on the merge of pull request #2: test, three images pushed by commit SHA, deploy to an ephemeral kind cluster, rollout status and smoke test all green (the run on #1 failed on the Ollama connection, fixed by #2) |
| Docs | README, CORPUS, PIPELINE, FAILURE-ANALYSIS, ENGINEERING-NOTES, AI-USAGE, DEMO, ADRs 0001 to 0005 | WRITTEN | |
| Evidence | UI screenshots (`08`) and branch protection (`01`) | VERIFIED | `docs/evidence/08-ui-answer.png`, `08-ui-abstain.png`, `01-branch-protection.png` |
| Evidence | blocked PR and green PR (`02`, `03`) | BLOCKED | screenshots of pull request #1 before and after approval |
| Review | pull requests into `main` reviewed by the second team member | VERIFIED | #1 and #2 approved by abdy879 |

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
