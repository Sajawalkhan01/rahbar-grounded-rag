# Evidence

Real screenshots and command output only, never mocked up. Each item says how to capture it. The
files in this folder are named `NN-what.png` or `NN-what.txt`.

| # | Evidence | How to capture it | File |
| --- | --- | --- | --- |
| 1 | `main` is protected | GitHub repo, Settings, Branches (or Rules): the rule for `main` showing "Require a pull request", 1 approval, required checks | `01-branch-protection.png` |
| 2 | A pull request with failing CI and a blocked merge | open a PR from `dev` with a deliberate regression (for example set `RELEVANCE_FLOOR` to `0.0` so a must-abstain question is answered), wait for the red check, screenshot the greyed-out merge button | `02-pr-blocked.png` |
| 3 | The same PR fixed: green checks, merge enabled | revert the regression, push, screenshot | `03-pr-green.png` |
| 4 | Resources deployed | `kubectl get all,pvc,ingress,hpa,networkpolicy,cronjob,job -n rahbar` | `04-kubectl-get-all.txt` |
| 5 | Resource usage | `kubectl top pods -n rahbar` while the system is serving | `05-kubectl-top.txt` |
| 6 | Three API endpoints behind the Service | `kubectl get endpoints api -n rahbar` | `06-endpoints.txt` |
| 7 | HPA is reading real CPU | `kubectl get hpa -n rahbar` (not `<unknown>`) | `07-hpa.txt` |
| 8 | The application answering through the Ingress | screenshot of `http://rahbar.local` with an answered question (citations visible) and a declined one | `08-ui-answer.png`, `08-ui-abstain.png` |
| 9 | Rollout and rollback | `kubectl set image ...`, `kubectl rollout status`, `kubectl rollout undo`, `kubectl rollout history` | `09-rollout-rollback.txt` |
| 10 | The embedding-model guardrail | change `EMBED_MODEL` in the ConfigMap, restart the api, show `0/1 READY` and the log line naming the conflict | `10-embed-mismatch.txt` |

Items 4 to 7, 9 and 10 are produced by commands in `docs/DEMO.md`. Items 1 to 3 and 8 need
your browser.
