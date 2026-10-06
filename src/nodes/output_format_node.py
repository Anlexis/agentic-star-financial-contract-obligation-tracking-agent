"""AgentCore Platform v1.0"""

# FIN-C2-077 — OutputFormatNode (inner domain node 5, last in DomainWorkflowGraph)
# Assembles the final contract review & obligation-tracking document from
# review_sections and compliance_flags.  This is the last inner node — it
# produces the contract_review string that the outer PostProcessNode S-3-gates.
#
# Inner node — ANONYMOUS trust (review finding 5, corrected 2026-07-02).
# Returns ONLY the state keys this node writes (partial-dict contract).

import logging
from typing import Any, ClassVar, Dict

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json

logger = logging.getLogger(__name__)

# Section order for the final report.
_SECTION_ORDER = [
    "review_summary",
    "contract_parties",
    "key_terms",
    "obligations_tracking",
    "risk_assessment",
    "compliance_findings",
    "recommended_actions",
]

# Human-readable section headers.
_SECTION_HEADERS: Dict[str, str] = {
    "review_summary": "1. Review Summary",
    "contract_parties": "2. Contract Parties",
    "key_terms": "3. Key Terms",
    "obligations_tracking": "4. Obligations Tracking",
    "risk_assessment": "5. Risk Assessment",
    "compliance_findings": "6. Compliance Findings",
    "recommended_actions": "7. Recommended Actions",
}

_SEPARATOR = "=" * 72
_SUBSEP = "-" * 72


def _assemble_report(
    contract_id: str,
    sections: Dict[str, str],
    compliance: Dict[str, Any],
) -> str:
    """Assemble the full contract review from sections and compliance data."""
    lines = [
        _SEPARATOR,
        "FINANCIAL CONTRACT REVIEW & OBLIGATION TRACKING REPORT",
        f"Contract ID: {contract_id}",
        _SEPARATOR,
        "",
    ]

    for key in _SECTION_ORDER:
        header = _SECTION_HEADERS.get(key, key.replace("_", " ").title())
        content = sections.get(key, "(Section not generated)")
        lines.append(header)
        lines.append(_SUBSEP)
        lines.append(content)
        lines.append("")

    # Append compliance trailer.
    escalation_required = compliance.get("escalation_required", False)
    reason = compliance.get("reason", "N/A")
    policy = compliance.get(
        "policy_basis",
        "Financial Contract Review Policy",
    )
    missing = compliance.get("missing_clauses", [])
    lines += [
        _SEPARATOR,
        "COMPLIANCE DETERMINATION",
        _SUBSEP,
        f"  Policy Basis:          {policy}",
        f"  Escalation Required:   {'YES — route to legal/senior review' if escalation_required else 'NO'}",
        f"  Missing Mandatory Clauses: {', '.join(missing) if missing else 'none'}",
        f"  Determination Basis:   {reason}",
        _SEPARATOR,
    ]

    return "\n".join(lines)


class OutputFormatNode(FunctionNode):
    """Assemble the final contract review document (inner domain node).

    Reads review_sections and compliance_flags from State, renders the
    full contract review & obligation-tracking report text, and writes it
    to contract_review (and result) for the outer PostProcessNode.

    Inner node — ANONYMOUS trust (see module docstring).

    Input state keys:
        review_sections:  str  — JSON-serialised section dict (ADR-005)
        compliance_flags: str  — JSON-serialised compliance result (ADR-005)
        contract_data:    str  — JSON-serialised contract payload (ADR-005)

    Output state keys (partial dict):
        contract_review: str
        result:          str  (same as contract_review — backbone convention)
        status:          str
        error_log:       list[str]  (only on ERROR)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        sections: Dict[str, str] = from_json(state.get("review_sections"), {})
        compliance: Dict[str, Any] = from_json(state.get("compliance_flags"), {})
        contract_data: Dict[str, Any] = from_json(state.get("contract_data"), {})

        contract_id = contract_data.get("contract_id", "unknown")

        if not sections:
            logger.error(
                "OutputFormatNode: review_sections missing in state for contract_id=%s",
                contract_id,
            )
            emit_trace_event(
                "output_format_failed",
                {"reason": "missing_review_sections", "contract_id": contract_id},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"OutputFormatNode: review_sections missing for contract_id={contract_id}"],
                # The runner surfaces `formatted_output or result` as `output`. A reason left only in
                # error_log reaches no one: the terminal result carries just `status`, and get_output()
                # does not copy error_log out of the graph -- the caller sees a blank spinner.
                "formatted_output": "Request could not be completed. "
                + (f"OutputFormatNode: review_sections missing for contract_id={contract_id}"),
            }

        # ── Assemble the report ───────────────────────────────────────────────
        report = _assemble_report(contract_id, sections, compliance)

        logger.info(
            "OutputFormatNode: contract_id=%s report_chars=%d escalation_required=%s",
            contract_id,
            len(report),
            compliance.get("escalation_required", False),
        )
        emit_trace_event(
            "output_format_complete",
            {
                "contract_id": contract_id,
                "report_length": len(report),
                "section_count": len(sections),
                "escalation_required": compliance.get("escalation_required", False),
            },
            state,
        )

        return {
            "contract_review": report,
            "result": report,
            "status": AgentStatus.SUCCESS.value,
        }
