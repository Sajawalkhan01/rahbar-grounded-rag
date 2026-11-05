# AI usage

This project was built with heavy AI assistance, and this page says exactly how. The rule in the
handout is honest attribution, so nothing here is softened.

## Tools

* **Claude Code** (Anthropic's coding agent, running Claude Sonnet 5.5 in VS Code). It read the
  handout and the starter kit and wrote most of the repository: the indexer pipeline, the API
  query pipeline, the evaluation scripts, the Dockerfiles, the Kubernetes manifests, the GitHub
  Actions workflows, the unit tests and the first draft of every document in `docs/`. It also ran
  the commands (tests, linters, Docker builds, the minikube deployment) and read their output.
* **No other AI tool** is credited here. (If either student used ChatGPT or another assistant for
  anything in this repository, add it below before submitting: an unlisted tool is the thing
  the policy penalises.)

## What the students did

* Chose the repository, set up GitHub (collaborators, secrets, branch protection) and Docker Hub,
  and approved or rejected every command the agent proposed that touched their machine. Two were
  rejected (an installer run as root, and a Docker command) and the agent changed approach.
* Decided the scope and direction ("complete the whole assignment", "commit often with no tool
  attribution", the repository name) and reviewed progress reports.
* Take the screenshots and evidence that need a browser, a GitHub account or a local terminal
  (`docs/evidence/`).

## What was written by the AI, and what was checked, corrected or changed afterwards

The code was not accepted on trust. These are the corrections that testing and CI forced, all
visible in the commit history:

| Found by | What was wrong | Change |
| --- | --- | --- |
| reading the real PDFs | the handbooks are set in 2 and 3 columns; plain text extraction interleaved the columns | column detection and reading-order extraction (`indexer/pipeline/extract.py`) |
| reading the real PDFs | the 2023 rules are 24 of 26 image-only pages | OCR path, with every OCR'd page flagged and logged |
| unit tests | an overlap tail plus a full-size piece overshot the chunk size (70 words against a limit of 60) | split with room left for the overlap; drop a tail that would overflow |
| the first CI run | the API crashed at startup when Chroma was unreachable (the client connected in its constructor) | lazy client creation and a regression test |
| the first CI run, then `kubectl logs --previous` | `chroma-0` crash-looped: Kubernetes service-link variables set `CHROMA_PORT=tcp://...` | `enableServiceLinks: false` on every pod |
| the first CI run | the Trivy action tag did not exist | pinned a real tag |
| the chunking experiment | exact clause numbers ("clause 1.21") never reached the top 5, because rank fusion let two weak matches beat one strong keyword match | a third, triple-weighted exact-term ranking |
| the chunking experiment | "the 2023 rules" was not treated as a request for the old version, because the current 2023-24 annual report shares the year | year ranges are removed before looking for a bare year |
| the chunking experiment | wrapped clause lines became section names | titles must be capitalised |

Design choices the AI proposed and the students must be able to defend are recorded as ADRs in
`docs/adr/`. Numbers in `docs/PIPELINE.md` and `eval/` are measured results of the code in this
repository, not copied from anywhere.

## What the students must still do before submitting

* Read the code and the ADRs until each design decision can be explained without the agent.
  `docs/ENGINEERING-NOTES.md` points at the exact files and lines for the viva questions.
* Add anything you changed or wrote by hand to the table above.


## Contribution of the second team member (abdy879)

This project was implemented mainly by Sajawalkhan01 together with the AI assistant Claude Code;
the sections above describe what the AI did. My part was reviewing, merging and documenting.

- I reviewed pull request #1 (the whole project, `dev` into `main`) and approved it, and I
  reviewed and approved pull request #2, the fix that lets the CI runner's Ollama server be
  reached from the cluster. I merged pull request #2 myself.
- [KEEP ONLY IF TRUE: While reviewing I read the answer pipeline (`api/app/services/answer.py`),
  the network policies (`k8s/base/networkpolicy.yaml`) and the engineering notes.]
- I made two commits under my own account: an update of `PROJECT-STATUS.md` after the CI and CD
  runs turned green, and the branch-protection screenshot in `docs/evidence/`. This section is
  my third commit.
- [KEEP ONLY IF TRUE, IN YOUR OWN WORDS: One thing I understood from the project is that
  the indexer runs as a Kubernetes Job rather than inside the API, because three API replicas
  would otherwise all re-index the same Chroma volume at once.]

Split of work: Sajawalkhan01 implemented, tested and deployed the system with AI assistance;
I reviewed the changes, merged the second pull request, and documented the status and evidence.
