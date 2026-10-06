"""AgentCore Platform v1.0"""

# FIN-C2-077 — outer -> inner caller-data bridge (Cat 2 nested graph).
#
# WHY:
#   GraphNode.execute() invokes the inner graph as
#   `subgraph.invoke(user_input, session_id=..., ctx=...)` — it does NOT forward
#   `input_context`. So the inner DomainWorkflowGraph starts with
#   `input_context = {}` and every inner node that reads the caller's contract
#   from that channel would see nothing.
#
#   The only channel GraphNode does forward is the single `user_input` STRING
#   returned by `extract_input()`, and both `user_input` and `validated_input`
#   are scanned and REWRITTEN by the framework's S-2 PII filter before any
#   template code runs. Measured on this repo against the real wheel: the
#   shipped sign-off payload came back with `Contract Type: [MASKED]` and a
#   party rendered as `FPT [MASKED] K.K.` — the review certified the redaction
#   sentinel as contract facts. A contract review that names a party "[MASKED]"
#   is not a contract review.
#
#   So the validated contract travels out-of-band, in a ContextVar, from
#   `ContractReviewGraphNode.extract_input()` to the inner graph's
#   `_extra_initial_state()`.
#
#   THE SET AND THE TAKE MUST SIT IN THE SAME NODE EXECUTION. LangGraph runs
#   each node in its own copied context, so a value set in one node is NOT
#   visible in the next — setting it in PreProcessNode looks right and silently
#   delivers nothing. `extract_input()` and the inner `invoke()` are both inside
#   `GraphNode.execute()`, one call stack, so the value is there when the inner
#   graph builds its initial state.
#
# SAFETY:
#   * `take_contract()` POPS. A value is consumed exactly once, so a payload can
#     never be picked up by a later, unrelated invocation.
#   * ContextVar, not a module global: concurrent requests each carry their own
#     copy, and an async server does not interleave one caller's contract into
#     another caller's report.
#   * The bridge carries only data that has already passed PreProcessNode's
#     bounds, injection screen and control-character rejection.

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Dict, Optional

_CONTRACT: ContextVar[Optional[Dict[str, Any]]] = ContextVar("fin_c2_077_validated_contract", default=None)


def set_contract(contract: Optional[Dict[str, Any]]) -> None:
    """Publish the validated contract for the inner graph of THIS invocation."""
    _CONTRACT.set(contract)


def take_contract() -> Optional[Dict[str, Any]]:
    """Return the validated contract and clear it (consume-once)."""
    contract = _CONTRACT.get()
    _CONTRACT.set(None)
    return contract


def clear_contract() -> None:
    """Drop any pending contract — used on the validation-failure path."""
    _CONTRACT.set(None)
