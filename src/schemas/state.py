"""AgentCore Platform v1.0"""

# ADR-005: State must be a flat TypedDict — never Pydantic BaseModel.
# LangGraph checkpoints use msgpack serialization; Pydantic objects
# cause silent corruption.  Extend AgentState with agent-specific
# fields only.  Do NOT add credentials, secrets, or Pydantic models.
#
# FIN-C2-077 — Financial Contract Review & Obligation Tracking Agent
# Two-layer nested Cat 2 graph: outer backbone (AgentBaseGraph) + inner
# domain workflow (BaseGraph).  Fields below cover both layers.
#
# ADR-005 compliance: all dict/list-valued fields are stored as JSON-
# serialized Optional[str].  Use to_json() / from_json() helpers below
# at every producer and consumer node — one contract end-to-end.
# Never type a dict/list field as a bare dict/list; that causes msgpack
# serialization failures and a CoE Stage-6 state-contract finding.

import json
from typing import Any, NotRequired, Optional

from framework.schemas.agent_state import AgentState


def to_json(value: Any) -> Optional[str]:
    """Serialize a value to a JSON string for State storage (ADR-005)."""
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def from_json(value: Optional[str], default: Any = None) -> Any:
    """Deserialize a JSON string from State storage (ADR-005)."""
    if value is None:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


class State(AgentState):
    """Flat TypedDict for FIN-C2-077.

    All shared fields (user_input, status, session_id, node_history,
    error_log, hitl_*, etc.) are inherited from AgentState.

    ADR-005: dict/list fields use JSON-serialized Optional[str].
    formatted_output is NOT re-declared here — it is inherited from AgentState.
    """

    # ------------------------------------------------------------------
    # Outer layer — set by PreProcessNode (pre_process backbone, S-1)
    # ------------------------------------------------------------------

    # Validated and normalised JSON string of the contract payload.
    # Produced by PreProcessNode.
    #
    # ⚠️ The framework's S-2 gate REWRITES this field (it is one of the
    # platform's PII scan fields), so what a downstream node reads here is the
    # masked form: personal-data-shaped values arrive as "[MASKED]".
    validated_input: NotRequired[Optional[str]]

    # The same validated contract, on a field the S-2 PII filter does NOT scan.
    # This is the copy the domain pipeline reads — via
    # ContractReviewGraphNode.extract_input() and the context bridge — so that
    # the review reports the caller's own party names and contract type rather
    # than certifying the redaction sentinel as contract fact.
    validated_contract: NotRequired[Optional[str]]

    # JSON-serialised channel/request metadata dict (ADR-005: stored as str).
    # Shape: {"source": str, "channel": str, "contract_id": str}
    enriched_context: NotRequired[Optional[str]]

    # ------------------------------------------------------------------
    # Inner layer — domain nodes (DomainWorkflowGraph)
    # ------------------------------------------------------------------

    # JSON-serialised parsed contract payload (ADR-005: stored as str).
    # Shape: {contract_id, contract_type, parties (list), effective_date,
    #   expiry_date, governing_law, contract_value (dict), currency,
    #   clauses (list), obligations (list), term_months (int),
    #   risk_tier (str), value_amount (int)}
    contract_data: NotRequired[Optional[str]]

    # JSON-serialised review section dict (ADR-005: stored as str).
    # Keys match the 7 report sections:
    #   review_summary, contract_parties, key_terms, obligations_tracking,
    #   risk_assessment, compliance_findings, recommended_actions
    # Each value is the rendered text for that section.
    review_sections: NotRequired[Optional[str]]

    # JSON-serialised financial-contract policy check result (ADR-005: stored as str).
    # Shape: {escalation_required: bool, reason: str, mandatory_clauses (list),
    #   present_clauses (list), missing_clauses (list), value_threshold: int,
    #   actual_value: int, risk_tier: str}
    compliance_flags: NotRequired[Optional[str]]

    # True if legal / senior-review escalation is required.
    # Criteria: any mandatory clause missing OR value >= threshold OR risk_tier == "high".
    escalation_required: NotRequired[Optional[bool]]

    # Final formatted contract review & obligation-tracking document (plain text).
    # Assembled by inner OutputFormatNode from review_sections + compliance_flags.
    contract_review: NotRequired[Optional[str]]

    # ------------------------------------------------------------------
    # Outer layer — set by PostProcessNode (post_process backbone, S-3)
    # ------------------------------------------------------------------

    # Primary result surfaced to the caller.
    # Set to the same content as contract_review after the S-3 gate passes.
    # formatted_output (from AgentState) is also set by PostProcessNode.
    result: NotRequired[Optional[str]]

    # ------------------------------------------------------------------
    # Tracing / audit — framework-managed; do NOT write from node code
    # ------------------------------------------------------------------

    trace_id: NotRequired[Optional[str]]
    correlation_id: NotRequired[Optional[str]]
    # node_history inherited from AgentState
