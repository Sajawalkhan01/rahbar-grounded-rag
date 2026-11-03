"""The embedding-model guardrail (section 4.4).

Vectors from two different embedding models cannot be compared, and the failure is silent:
retrieval returns nonsense and nothing raises. So the rule "the API and the indexer use the
same model" is enforced in code, not by being careful:

  * the indexer writes `embed_model` and `dimension` into the Chroma collection metadata
    (indexer/pipeline/store.py);
  * the API compares both with its own EMBED_MODEL and the vector size its model really
    produces, and /readyz returns 503 on any difference, so Kubernetes keeps the pod out of
    the Service and `kubectl get pods` shows 0/1 READY.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("rahbar.pin")


@dataclass
class PinStatus:
    ok: bool
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)


def compare_pin(
    index_meta: dict[str, Any], api_model: str, api_dimension: int
) -> PinStatus:
    """Compare what the index recorded with what this API process would use."""
    index_model = index_meta.get("embed_model")
    index_dim = index_meta.get("dimension")
    details = {
        "api_embed_model": api_model,
        "index_embed_model": index_model,
        "api_dimension": api_dimension,
        "index_dimension": index_dim,
    }
    if index_model is None or index_dim is None:
        return PinStatus(
            False,
            "the index does not record its embedding model and dimension "
            "(the indexer has not finished, or it is not this version)",
            details,
        )
    problems = []
    if index_model != api_model:
        problems.append(f"embedding model mismatch: API uses {api_model!r}, index was built with {index_model!r}")
    if int(index_dim) != int(api_dimension):
        problems.append(f"vector dimension mismatch: API model gives {api_dimension}, index has {index_dim}")
    if problems:
        return PinStatus(False, "; ".join(problems), details)
    return PinStatus(True, "", details)
