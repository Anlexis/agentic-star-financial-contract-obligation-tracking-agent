"""AgentCore Platform v1.0"""

# FIN-C2-077 — ParseContractDataNode
# Inner domain node 2: enrich and classify the validated contract data.
#
# Responsibilities:
#   - Compute contract term length (months) from effective/expiry dates
#   - Classify contract risk tier from value + term
#   - Pre-check mandatory-clause coverage (advisory hint for the report)
#   - Annotate contract_data with derived fields for downstream nodes
#
# Inner node — ANONYMOUS trust (review finding 5, corrected 2026-07-02).
# Returns ONLY the state keys this node writes (partial-dict contract).

import logging
from datetime import date
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json, to_json

logger = logging.getLogger(__name__)

# Financial-contract review thresholds.
_HIGH_VALUE_THRESHOLD = 50_000_000  # JPY
_MEDIUM_VALUE_THRESHOLD = 10_000_000  # JPY
_LONG_TERM_MONTHS = 36  # months
_MEDIUM_TERM_MONTHS = 12  # months

# Mandatory clauses for a financial contract (advisory pre-check here; the
# authoritative determination is made by ComplianceCheckNode).
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


def _parse_iso_date(value: Any) -> Optional[date]:
    """Parse an ISO-8601 date string (YYYY-MM-DD prefix); None if unparseable."""
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _term_months(effective: Optional[date], expiry: Optional[date]) -> int:
    """Whole months between effective and expiry dates (0 if unknown/invalid)."""
    if effective is None or expiry is None or expiry < effective:
        return 0
    return (expiry.year - effective.year) * 12 + (expiry.month - effective.month)


def _classify_risk_tier(value_amount: int, term_months: int) -> str:
    """Classify contract risk tier from value and term.

    Returns "high", "medium", or "low".
    """
    if value_amount >= _HIGH_VALUE_THRESHOLD or term_months >= _LONG_TERM_MONTHS:
        return "high"
    if value_amount >= _MEDIUM_VALUE_THRESHOLD or term_months >= _MEDIUM_TERM_MONTHS:
        return "medium"
    return "low"


class ParseContractDataNode(FunctionNode):
    """Enrich contract data with risk classification and derived fields.

    Inner node — ANONYMOUS trust (see module docstring).

    Input state keys:
        contract_data: str  — JSON-serialised contract payload (ADR-005)

    Output state keys (partial dict):
        contract_data: str  — enriched JSON string (ADR-005), same key updated
        status:        str
        error_log:     list[str]  (only on ERROR)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        contract_data: Dict[str, Any] = from_json(state.get("contract_data"), {})

        if not contract_data:
            logger.error("ParseContractDataNode: contract_data is empty or missing")
            emit_trace_event(
                "parse_contract_data_failed",
                {"reason": "missing_contract_data"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ParseContractDataNode: contract_data missing in state"],
                # The runner surfaces `formatted_output or result` as `output`. A reason left only in
                # error_log reaches no one: the terminal result carries just `status`, and get_output()
                # does not copy error_log out of the graph -- the caller sees a blank spinner.
                "formatted_output": "Request could not be completed. "
                + ("ParseContractDataNode: contract_data missing in state"),
            }

        contract_id = contract_data.get("contract_id", "unknown")
        value_amount = int(contract_data.get("value_amount", 0))

        # ── Term computation ──────────────────────────────────────────────────
        effective = _parse_iso_date(contract_data.get("effective_date"))
        expiry = _parse_iso_date(contract_data.get("expiry_date"))
        term_months = _term_months(effective, expiry)

        # ── Risk classification ───────────────────────────────────────────────
        risk_tier = _classify_risk_tier(value_amount, term_months)

        # ── Mandatory-clause pre-check (advisory) ─────────────────────────────
        present_clauses: List[str] = list(contract_data.get("clauses", []))
        missing_clauses = sorted(_MANDATORY_CLAUSES - set(present_clauses))
        mandatory_ok = not missing_clauses

        # ── Enrich contract_data (build a local copy — no module-global mutation) ──
        enriched: Dict[str, Any] = dict(contract_data)
        enriched["term_months"] = term_months
        enriched["risk_tier"] = risk_tier
        enriched["value_amount"] = value_amount
        enriched["obligations_count"] = len(contract_data.get("obligations", []))
        enriched["missing_mandatory_clauses"] = missing_clauses
        enriched["mandatory_clauses_ok"] = mandatory_ok

        logger.info(
            "ParseContractDataNode: contract_id=%s risk_tier=%s term_months=%d mandatory_ok=%s",
            contract_id,
            risk_tier,
            term_months,
            mandatory_ok,
        )
        emit_trace_event(
            "parse_contract_data_complete",
            {
                "contract_id": contract_id,
                "risk_tier": risk_tier,
                "term_months": term_months,
                "value_amount": value_amount,
                "mandatory_ok": mandatory_ok,
            },
            state,
        )

        return {
            "contract_data": to_json(enriched),
            "status": AgentStatus.SUCCESS.value,
        }
