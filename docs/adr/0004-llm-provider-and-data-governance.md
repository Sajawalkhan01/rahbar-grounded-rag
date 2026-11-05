# ADR 0004: A pluggable LLM provider, and what data leaves the cluster

## Status
Accepted · 4 Nov 2026

## Context
Answer generation needs a language model. The handout asks for a provider behind one
interface, selected in the ConfigMap, with at least one mode fully working. The choice also
decides where user questions and document text travel, so it is a data-governance decision,
not only a technical one.

## Decision
`LLM_PROVIDER` (ConfigMap) selects an implementation of `LLMProvider.generate(question,
passages) -> Answer` (`api/app/providers/llm/`). Swapping providers is one file.

| Value | Where the model runs | Status |
| --- | --- | --- |
| `stub` | nowhere; deterministic | used by every unit test and `make dev`, never calls a model |
| `ollama-host` | Ollama on the developer's laptop, reached at `host.minikube.internal:11434` | the default and the fully verified mode |
| `ollama-incluster` | Ollama as a Deployment | same code, only `OLLAMA_URL` differs; not deployed (optional bonus) |
| `hosted` | an OpenAI-compatible free-tier API (Groq, Google AI Studio, OpenRouter) | implemented and unit-tested with a faked HTTP layer; needs a key the student creates |

Embeddings are pinned separately (`EMBED_MODEL=nomic-embed-text`) and served by the same
Ollama host for both the indexer and the API; this is stated in the README.

**Enforcement.** Ollama gets the `Answer` JSON schema in `format` (constrained decoding);
hosted providers get JSON mode. The reply is always validated with Pydantic afterwards,
retried once, and replaced by a safe fallback on a second failure. Temperature is 0.1.

**Data governance.** What leaves the cluster in each mode:
* `ollama-host`: the question and retrieved passages go to the laptop only. Nothing leaves the
  machine.
* `hosted`: the question and up to `TOP_K` retrieved passages go to a third-party API over
  HTTPS. That is acceptable here because the corpus is public FAST-NUCES documents and the
  questions are about them. It would NOT be acceptable for private records (student grades,
  transcripts, internal minutes): those would need `ollama-incluster` or a provider under a
  data-processing agreement. No student identifiers are sent or logged.
* The API key exists only as `LLM_API_KEY` from a Kubernetes Secret created on the command
  line (`make secret`). It is never in a manifest, an image, a log line or an error message
  (`tests/` assert this), and a key that reaches Git history must be rotated.
* Logs carry request ids, chunk ids and scores, never the key.

## Options considered
* **Only a hosted API**: fastest answers, no local GPU or RAM needs, but sends every question
  to a third party and depends on an account, a quota and the network.
* **Only in-cluster Ollama**: nothing leaves the cluster, but needs at least 8 GB for minikube
  and slow CPU inference inside it.
* **Hard-code one provider**: rejected; the handout requires a one-file swap.

## Consequences
* The default needs Ollama running on the laptop (`OLLAMA_HOST=0.0.0.0`) and a NetworkPolicy
  hole to the host IP on port 11434 (`k8s/overlays/dev/egress-llm-host.yaml`).
* A 3B local model is the weakest link for abstention and citation formatting. That is
  measured, not assumed: `eval/run_eval.py` gates on citation validity and abstention.
* Free-tier hosted APIs rate-limit; the provider maps any HTTP error to a `ProviderError`,
  which becomes one retry and then the fallback answer, not a 500.
