#!/usr/bin/env python3
"""Failure experiments D (prompt) and F (generation), section 5.5. Needs a real language model.

Run inside an api pod, where the hosted provider and its key already exist (the key is read
from the environment and never printed):

    kubectl cp eval/failure_experiments_llm.py rahbar/<api-pod>:/tmp/fe_llm.py
    kubectl cp eval/failure_experiments_llm_questions.json rahbar/<api-pod>:/tmp/fe_questions.json
    kubectl exec -n rahbar <api-pod> -- sh -c "cd /app && PYTHONPATH=/app python /tmp/fe_llm.py"

The questions file is a list of {"id", "type", "question", "must_abstain"} taken from eval/golden.yaml.
Each condition calls the real answer pipeline with the cache off, so every answer is generated:

  base       the shipped prompt, temperature 0.1, relevance floor 0.70
  base_nofl  the shipped prompt with the relevance floor off (the prompt is the only defence)
  D          rule 1 removed from the system prompt ("use the supplied context")
  D_nofl     the same, with the floor off, so the prompt is the only defence
  F          temperature 1.2 instead of 0.1
  D_strong   rules 1 and 3 both removed (no grounding and no 'say you don't know'), floor off

Metrics: must-abstain questions answered (should be 0), answerable questions declined,
citations the model invented and the validator removed, fallbacks (output that failed validation
twice), and answers that leaked the system prompt.
"""

from __future__ import annotations

import dataclasses
import json
import sys
import time

import app.services.prompt as prompt_module
from app.config import get_settings
from app.providers.llm.hosted import HostedProvider
from app.retrieval.chroma import ChromaRetriever
from app.services.answer import answer_question
from app.services.validate import FALLBACK_TEXT

RULE_1 = ("1. Answer ONLY from the numbered evidence passages between <evidence> and </evidence>. Use the\n"
          "   supplied context, never your own knowledge, even if you are sure you know the answer.")
RULE_3 = ("3. If the evidence does not contain the answer, set answered_from_context to false, say in\n"
          "   \"answer\" that the documents do not cover the question, and leave citations empty.")
LEAK_MARKERS = ("RULES", "<evidence>", "RAHBAR, a question-answering")


def run(name: str, questions: list[dict], floor: float, temperature: float, drop_rule_1: bool,
        drop_rule_3: bool = False) -> dict:
    base = get_settings()
    settings = dataclasses.replace(base, relevance_floor=floor, temperature=temperature)
    original = prompt_module.SYSTEM_PROMPT
    if drop_rule_1:
        assert RULE_1 in original, "rule 1 text changed; update RULE_1"
        prompt_module.SYSTEM_PROMPT = original.replace(RULE_1, "1. Answer the question.")
    if drop_rule_3:
        assert RULE_3 in prompt_module.SYSTEM_PROMPT, "rule 3 text changed; update RULE_3"
        prompt_module.SYSTEM_PROMPT = prompt_module.SYSTEM_PROMPT.replace(RULE_3, "3. Always give an answer.")
    try:
        retriever = ChromaRetriever(settings)
        provider = HostedProvider(settings)
        out = {"condition": name, "floor": floor, "temperature": temperature,
               "rule_1_removed": drop_rule_1, "rule_3_removed": drop_rule_3,
               "must_abstain_answered": [], "false_abstentions": [], "fallbacks": [], "leaked_prompt": [],
               "invalid_citations_removed": 0, "citations": 0, "answered": 0, "questions": len(questions)}
        started = time.perf_counter()
        for q in questions:
            r = answer_question(q["question"], retriever, provider, settings, cache=None)
            if q["must_abstain"]:
                if r.answered_from_context:
                    out["must_abstain_answered"].append(q["id"])
            elif not r.answered_from_context:
                out["false_abstentions"].append(q["id"])
            if r.answer == FALLBACK_TEXT:
                out["fallbacks"].append(q["id"])
            if any(m in r.answer for m in LEAK_MARKERS):
                out["leaked_prompt"].append(q["id"])
            out["invalid_citations_removed"] += r.invalid_citations_removed
            out["citations"] += len(r.citations)
            out["answered"] += int(r.answered_from_context)
            print(f"  {name} {q['id']} answered={r.answered_from_context}", file=sys.stderr, flush=True)
        out["seconds"] = round(time.perf_counter() - started)
        return out
    finally:
        prompt_module.SYSTEM_PROMPT = original


def main() -> None:
    questions = json.load(open("/tmp/fe_questions.json", encoding="utf-8"))
    conditions = [("base", 0.70, 0.1, False), ("base_nofl", 0.0, 0.1, False),
                  ("D", 0.70, 0.1, True), ("D_nofl", 0.0, 0.1, True), ("F", 0.70, 1.2, False),
                  ("D_strong", 0.0, 0.1, True, True)]
    wanted = sys.argv[1:] or [c[0] for c in conditions]
    results = []
    for name, floor, temperature, *drops in conditions:
        if name in wanted:
            results.append(run(name, questions, floor, temperature, *drops))
            print(json.dumps(results[-1]), flush=True)


if __name__ == "__main__":
    main()
