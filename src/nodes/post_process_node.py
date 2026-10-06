"""AgentCore Platform v1.0"""

# FIN-C2-077 — PostProcessNode
# Outer backbone post_process slot: S-3 output gate + expose the final contract
# review as formatted_output and result.
#
# S-3 responsibility: scan the assembled contract_review string for
# credential-like material before it is returned to the caller. On a violation,
# return a sanitised stub for BOTH formatted_output and result, CLEAR every
# other output-bearing field, and set status=ERROR.
#
# THE GATE IS A UNION, NOT A CHOICE (measured on this repo, real 1.0.2 wheel):
#
#   * The template's own patterns catch `password=...` / `api_key: ...`
#     assignments. The framework's detector does NOT — its patterns describe
#     credential FORMATS and match none of that shape. Deleting the local set to
#     "delegate to the framework" makes the gate NARROWER while looking like a
#     tightening.
#   * The framework's detector catches `AKIA...`, `sk_live_...` and
#     a database URI carrying inline credentials. The local set did NOT. A value the
#     framework catches and this node misses makes the framework's own @final
#     S-3 gate raise INSIDE post_process; the wrapper then returns a bare ERROR
#     partial and DISCARDS the clearing below. A detector gap is a containment
#     bypass, not just a missed match.
#
#   So the gate takes the union. Wider is safe; narrower is a bypass.
#
# The domain gate is a module-level function (`evaluate_output_gate`) called
# from inside execute() — NOT an instance method on the node class, which the
# real SDK marks @final and would refuse at class-definition time. The agent
# class in graph.py delegates to this same function so the two cannot drift.
#
# Returns ONLY the state keys this node writes (partial-dict contract).

import logging
import re
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from framework.security.credential_detector import detect_credentials_in_value
from shared.utils.audit_logger import emit_trace_event
from src.services.llm_factory import resolve_llm
from src.services.llm_review import render_review, review_result
import json

logger = logging.getLogger(__name__)

# Domain patterns kept BECAUSE the framework detector does not carry them.
# Each entry below was checked against `detect_credentials_in_value` on the
# installed wheel; `credential_assignment` is the one that is genuinely
# framework-invisible, and `api_key_pattern` / `jwt_pattern` / `bearer_token`
# are retained as a defence against a narrowing change upstream.
_CREDENTIAL_PATTERNS: List[Tuple[str, str]] = [
    (r"(?:sk|pk|ak)-[A-Za-z0-9]{16,}", "api_key_pattern"),
    (r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "jwt_pattern"),
    (r"Bearer\s+[A-Za-z0-9_\-\.]{8,}", "bearer_token"),
    (
        r"(?:password|passwd|secret|api_key|token|access_key|private_key)" r"\s*[:=]\s*\S{8,}",
        "credential_assignment",
    ),
]

# Fields that can carry released review text out of this node. On a violation
# every one of them is cleared: `AgentBaseGraph.get_output()` falls back to
# `state.get("result")` even on an error status, so an output gate that only
# raises — or that leaves a populated field behind — still ships the un-gated
# report inside the ERROR envelope.
_OUTPUT_BEARING_FIELDS = ("contract_review", "review_sections", "compliance_flags")


def evaluate_output_gate(content: str) -> Optional[str]:
    """Return the first S-3 violation class found in *content*, else None.

    Union of the domain patterns above and the framework's own
    `detect_credentials_in_value` — the framework result is the FLOOR, never
    the replacement.
    """
    if not content:
        return None
    for pattern, name in _CREDENTIAL_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            return name
    findings = detect_credentials_in_value(content)
    if findings:
        finding_type = "framework_credential_pattern"
        if isinstance(findings, list) and findings and isinstance(findings[0], dict):
            finding_type = str(findings[0].get("type", finding_type))
        return finding_type
    return None


# Backwards-compatible alias: the module-level gate name used before the union.
_security_gate_output = evaluate_output_gate


class PostProcessNode(FunctionNode):
    """Apply the S-3 output gate and expose the final contract review.

    Outer backbone post_process slot. Declared ANONYMOUS — trust was already
    enforced at PreProcessNode (VERIFIED_EXTERNAL).

    Input state keys:
        contract_review:     str   — formatted review from inner OutputFormatNode
        escalation_required: bool  — legal/senior-review escalation flag

    Output state keys (partial dict):
        formatted_output: str
        result:           str
        status:           str
        error_log:        list[str]  (only on ERROR)
        contract_review / review_sections / compliance_flags — cleared on ERROR
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        contract_review: str = state.get("contract_review") or ""
        escalation_required: bool = bool(state.get("escalation_required"))

        # ── Fallback for empty review ─────────────────────────────────────────
        if not contract_review.strip():
            logger.warning("PostProcessNode: contract_review is empty — using fallback message")
            contract_review = (
                "[Contract Review Report] No review content generated. " "Check error_log for upstream failures."
            )

        # ── S-3 domain output gate ────────────────────────────────────────────
        _llm, _ = resolve_llm(None, state)
        # The advisory must judge the answer against the SAME request the answer was built
        # from. `user_input` has been PII-masked by the S-2 input gate before this node runs,
        # so on the Marketplace path it reads `[MASKED]` where `input_context.contract` holds a
        # real name -- and the advisory then reports the report's own correct values as "not
        # supported by the input". The record is not masked, so it is the honest basis.
        _basis_record = (state.get("input_context") or {}).get("contract")
        _basis = json.dumps(_basis_record, ensure_ascii=False) if _basis_record else str(state.get("user_input") or "")
        _remarks = review_result(
            _llm,
            user_input=_basis,
            result=contract_review,
            domain="FIN FinancialContractReviewAgent",
        )
        _review = render_review(_remarks)
        # Remarks are LLM text derived from the caller's raw words, so they pass through the
        # same gate the answer does -- appending after the gate would put unscanned text past
        # it. A tripped review is dropped on its own: withholding a correct answer because an
        # advisory remark quoted an identifier would let the review change the outcome, and
        # the whole design rests on it being unable to.
        if _review and isinstance(contract_review, str) and not evaluate_output_gate(contract_review + _review):
            contract_review = contract_review + _review

        violation = evaluate_output_gate(contract_review)
        if violation:
            logger.error("PostProcessNode: S-3 violation detected in output — %s", violation)
            emit_trace_event(
                "post_process_s3_violation",
                {"violation": violation},
                state,
            )
            # Truthy replacement, deliberately: a FALSY formatted_output re-opens
            # `get_output()`'s `formatted_output or result` fallback.
            sanitised = (
                f"[CONTRACT REVIEW WITHHELD: the assembled report contained a "
                f"disallowed pattern ({violation}). Contact the legal/compliance "
                f"team for the original report.]"
            )
            withheld: Dict[str, Any] = {
                "formatted_output": sanitised,
                "result": sanitised,
                "status": AgentStatus.ERROR.value,
                # Closed-set label only — never the matched text, never a path,
                # never a traceback.
                "error_log": [f"PostProcessNode: S-3 output gate withheld the report — {violation}"],
            }
            for field in _OUTPUT_BEARING_FIELDS:
                withheld[field] = None
            return withheld

        logger.info(
            "PostProcessNode: output gate passed — length=%d escalation_required=%s",
            len(contract_review),
            escalation_required,
        )
        emit_trace_event(
            "post_process_complete",
            {
                "output_length": len(contract_review),
                "escalation_required": escalation_required,
            },
            state,
        )

        return {
            "formatted_output": contract_review,
            "result": contract_review,
            "status": AgentStatus.SUCCESS.value,
        }
