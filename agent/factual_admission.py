"""Evidence-bound admission for structured LLM responses.

Capabilities decide truth, the LLM decides wording, and this module verifies
that wording before a caller can deliver it.  One constrained repair is
allowed; a persistent contradiction becomes deterministic safe copy.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from capabilities.game_truth import deterministic_correction
from engine.game_check import check_post

log = logging.getLogger("elixir.agent.factual_admission")


def _text(value) -> str:
    if isinstance(value, list):
        return "\n".join(str(item) for item in value if item is not None)
    return str(value or "")


def response_copy(response: dict) -> str:
    return "\n".join(
        value
        for value in (
            _text(response.get("content")),
            _text(response.get("share_content")),
        )
        if value
    )


def _repair_preserves_contract(original: dict, repaired: dict) -> bool:
    mutable = {"content", "share_content"}
    return all(
        original.get(key) == repaired.get(key)
        for key in (set(original) | set(repaired)) - mutable
    )


def admit_structured_response(
    response: dict,
    facts: dict,
    *,
    repair_fn: Callable[[dict, list[dict], dict], dict | None] | None = None,
) -> tuple[dict, dict]:
    """Return ``(admitted_response, trace)`` for one structured response."""
    if not isinstance(response, dict) or "_error" in response:
        return response, {"decision": "not_applicable", "findings": []}
    findings = check_post(response_copy(response), facts)
    if not findings:
        return response, {"decision": "pass", "findings": []}

    if repair_fn is not None:
        try:
            repaired = repair_fn(response, findings, facts)
        except Exception:
            log.warning("structured-response wording repair failed", exc_info=True)
            repaired = None
        if (
            isinstance(repaired, dict)
            and "_error" not in repaired
            and _repair_preserves_contract(response, repaired)
            and not check_post(response_copy(repaired), facts)
        ):
            return repaired, {"decision": "repaired", "findings": findings}

    safe = dict(response)
    correction = deterministic_correction(facts)
    safe["content"] = correction
    if "share_content" in safe:
        safe["share_content"] = correction
    return safe, {"decision": "fallback", "findings": findings}


__all__ = ["admit_structured_response", "response_copy"]
