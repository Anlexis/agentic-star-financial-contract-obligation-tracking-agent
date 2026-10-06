"""AgentCore Platform v1.0"""

# FIN-C2-077 — ComplianceCheckNode
# Inner domain node 4: financial-contract policy compliance check.
#
# Determines whether the contract satisfies the mandatory-clause policy and
# whether it must be escalated to legal / senior review before approval.
#
# Escalation criteria (v1: rule-based):
#   - any mandatory clause is missing, OR
#   - contract value >= threshold, OR
#   - risk tier is "high".
#
# Inner node — ANONYMOUS trust (review finding 5, corrected 2026-07-02).
# Returns ONLY the state keys this node writes (partial-dict contract).

import logging
from typing import Any, ClassVar, Dict, List

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json, to_json

logger = logging.getLogger(__name__)

# Mandatory clauses for a financial contract (authoritative policy set).
_MANDATORY_CLAUSES = frozenset(
    {
        "termination",
        "liability",
        "confidentiality",
        "governing_law",
        "payment_terms",
        "dispute_resolution",
    }
)

# Contract value at or above which senior/legal escalation is required (JPY).
_ESCALATION_VALUE_THRESHOLD = 10_000_000


def _evaluate_compliance(
    present_clauses: List[str],
    value_amount: int,
    risk_tier: str,
) -> Dict[str, Any]:
    """Evaluate mandatory-clause coverage and escalation criteria.

    Returns a dict with escalation_required, reason, and the threshold /
    coverage details for full traceability in the report's compliance trailer.
    """
    present = set(present_clauses)
    missing = sorted(_MANDATORY_CLAUSES - present)
    clauses_ok = not missing

    value_threshold_met = value_amount >= _ESCALATION_VALUE_THRESHOLD
    high_risk = risk_tier == "high"
    escalation_required = bool(missing) or value_threshold_met or high_risk

    reasons: List[str] = []
    if missing:
        reasons.append(f"Missing mandatory clause(s): {', '.join(missing)}")
    if value_threshold_met:
        reasons.append(
            f"Contract value ({value_amount:,}) >= escalation threshold " f"({_ESCALATION_VALUE_THRESHOLD:,})"
        )
    if high_risk:
        reasons.append("Risk tier is HIGH")
    if not reasons:
        reasons.append("All mandatory clauses present; value below threshold; risk tier not high")

    return {
        "escalation_required": escalation_required,
        "reason": "; ".join(reasons),
        "mandatory_clauses": sorted(_MANDATORY_CLAUSES),
        "present_clauses": sorted(present),
        "missing_clauses": missing,
        "mandatory_coverage_complete": clauses_ok,
        "value_threshold": _ESCALATION_VALUE_THRESHOLD,
        "actual_value": value_amount,
        "value_threshold_met": value_threshold_met,
        "risk_tier": risk_tier,
        "policy_basis": "Financial Contract Review Policy — mandatory-clause + value/risk escalation",
    }


class ComplianceCheckNode(FunctionNode):
    """Financial-contract policy compliance check for FIN-C2-077.

    Evaluates mandatory-clause coverage and escalation criteria, then
    produces a compliance_flags dict with full traceability data for
    inclusion in the contract review's compliance section.

    Inner node — ANONYMOUS trust (see module docstring).

    Input state keys:
        contract_data: str  — JSON-serialised enriched contract payload (ADR-005)

    Output state keys (partial dict):
        compliance_flags:    str   — JSON-serialised compliance result dict (ADR-005)
        escalation_required: bool  — True if legal/senior review is required
        status:              str
        error_log:           list[str]  (only on ERROR)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        contract_data: Dict[str, Any] = from_json(state.get("contract_data"), {})

        if not contract_data:
            logger.error("ComplianceCheckNode: contract_data missing in state")
            emit_trace_event(
                "compliance_check_failed",
                {"reason": "missing_contract_data"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ComplianceCheckNode: contract_data missing in state"],
                # The runner surfaces `formatted_output or result` as `output`. A reason left only in
                # error_log reaches no one: the terminal result carries just `status`, and get_output()
                # does not copy error_log out of the graph -- the caller sees a blank spinner.
                "formatted_output": "Request could not be completed. "
                + ("ComplianceCheckNode: contract_data missing in state"),
            }

        contract_id = contract_data.get("contract_id", "unknown")
        present_clauses: List[str] = list(contract_data.get("clauses", []))
        value_amount = int(contract_data.get("value_amount", 0))
        risk_tier = str(contract_data.get("risk_tier", "low"))

        # ── Policy evaluation ─────────────────────────────────────────────────
        compliance_result = _evaluate_compliance(present_clauses, value_amount, risk_tier)
        escalation_required = bool(compliance_result["escalation_required"])

        logger.info(
            "ComplianceCheckNode: contract_id=%s escalation_required=%s " "missing=%d value=%d risk=%s",
            contract_id,
            escalation_required,
            len(compliance_result["missing_clauses"]),
            value_amount,
            risk_tier,
        )
        emit_trace_event(
            "compliance_check_complete",
            {
                "contract_id": contract_id,
                "escalation_required": escalation_required,
                "mandatory_coverage_complete": compliance_result["mandatory_coverage_complete"],
                "value_threshold_met": compliance_result["value_threshold_met"],
            },
            state,
        )

        return {
            "compliance_flags": to_json(compliance_result),
            "escalation_required": escalation_required,
            "status": AgentStatus.SUCCESS.value,
        }
