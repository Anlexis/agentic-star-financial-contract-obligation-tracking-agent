"""AgentCore Platform v1.0"""

# FIN-C2-077 — InputValidateNode
# Inner domain node 1: domain-level validation of the contract payload.
#
# Distinct from PreProcessNode (S-1 trust + structural JSON check):
# this node applies domain-business rules — field types, party
# normalisation, clause normalisation, and contract-value extraction.
#
# Inner node — ANONYMOUS trust (outer PreProcessNode with VERIFIED_EXTERNAL
# already enforced trust; inner nodes must be ANONYMOUS so the outer
# InvocationContext passes through the GraphNode boundary without rejection;
# review finding 5, corrected 2026-07-02, F.Liu-authorized).
#
# Returns ONLY the state keys this node writes (partial-dict contract).

import json
import logging
from typing import Any, ClassVar, Dict, List, Set

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import to_json
from src.validation import PayloadError, parse_amount


# The Marketplace runner seeds input_context with its own conversation history on every
# invocation (shared/bootstrap/marketplace_app.py); the caller neither sends that key nor can
# suppress it, and the build_input_context hook can only overwrite its value, never remove it.
# It is platform plumbing rather than caller data, so it is dropped here, before the caller
# contract runs: the unknown-field guard below stays strict for everything a caller can
# actually send, and no value screen is ever asked to judge a transcript that contains this
# agent's own earlier answers. The value may also be None, which this tolerates.
_PLATFORM_CONTEXT_KEYS = frozenset({"conversation_history"})


def _without_platform_context(raw: Any) -> Any:
    """The caller-supplied half of input_context, platform-injected keys removed."""
    if not isinstance(raw, dict):
        return raw
    return {k: v for k, v in raw.items() if k not in _PLATFORM_CONTEXT_KEYS}


logger = logging.getLogger(__name__)

# Key under `input_context` that carries the validated contract, seeded by the
# inner graph from the context bridge (see src/graph/context_bridge.py).
_CONTRACT_KEY = "contract"

# Known clause-name aliases for normalisation (policy check requires canonical names).
_CLAUSE_ALIASES: Dict[str, str] = {
    "termination clause": "termination",
    "term": "termination",
    "liability cap": "liability",
    "limitation of liability": "liability",
    "confidentiality": "confidentiality",
    "nda": "confidentiality",
    "non-disclosure": "confidentiality",
    "governing law": "governing_law",
    "governinglaw": "governing_law",
    "jurisdiction": "governing_law",
    "payment": "payment_terms",
    "payment terms": "payment_terms",
    "fees": "payment_terms",
    "dispute resolution": "dispute_resolution",
    "arbitration": "dispute_resolution",
    "disputes": "dispute_resolution",
    "indemnity": "indemnification",
    "indemnification": "indemnification",
}


def _normalise_clauses(raw_clauses: Any) -> List[str]:
    """Normalise and deduplicate the clauses list to canonical names."""
    if not isinstance(raw_clauses, list):
        raw_clauses = [str(raw_clauses)] if raw_clauses else []
    normalised: List[str] = []
    seen: Set[str] = set()
    for clause in raw_clauses:
        key = str(clause).lower().strip()
        canonical = _CLAUSE_ALIASES.get(key, key.replace(" ", "_"))
        if canonical and canonical not in seen:
            seen.add(canonical)
            normalised.append(canonical)
    return normalised


def _normalise_parties(raw_parties: Any) -> List[Dict[str, str]]:
    """Normalise the parties list into [{name, role}] dicts."""
    if not isinstance(raw_parties, list):
        raw_parties = [raw_parties] if raw_parties else []
    normalised: List[Dict[str, str]] = []
    for party in raw_parties:
        if isinstance(party, dict):
            name = str(party.get("name", "")).strip()
            role = str(party.get("role", "party")).strip() or "party"
        else:
            name = str(party).strip()
            role = "party"
        if name:
            normalised.append({"name": name, "role": role})
    return normalised


def _extract_value(raw_value: Any) -> Dict[str, Any]:
    """Extract {amount:int, currency:str} from the contract_value field.

    THIS IS THE SINGLE ENFORCEMENT POINT for the contract's only decision-driving
    number, and it FAILS CLOSED — `parse_amount` raises `PayloadError` rather
    than substituting a default.

    The previous implementation defaulted to 0 on any parse failure, which was
    fail-OPEN in both directions and was measured on the real wheel:

      * `{"amount": NaN}` — `json.loads` accepts the bare `NaN` literal, `int()`
        raises ValueError, the except substituted 0, and a contract of unknown
        value was reported as "Contract Value: 0 JPY", below the escalation
        threshold, escalation NO. A fabricated figure in a legal review, and a
        silent fail-open on the exact decision this agent exists to make.
      * `{"amount": Infinity}` — `int(inf)` raises OverflowError, which the
        except did not name, so the node crashed and the caller got an opaque
        error with nothing pointing at the field.
      * `{"amount": true}` — `bool` is an `int` in Python, so it became 1 JPY.
    """
    if isinstance(raw_value, dict):
        currency = str(raw_value.get("currency", "JPY")).strip() or "JPY"
        return {
            "amount": parse_amount(raw_value.get("amount", 0), "contract_value.amount"),
            "currency": currency,
        }
    return {
        "amount": parse_amount(raw_value if raw_value is not None else 0, "contract_value"),
        "currency": "JPY",
    }


class InputValidateNode(FunctionNode):
    """Domain validation of the contract payload for FIN-C2-077.

    Applies business-rule checks beyond the structural JSON check in
    PreProcessNode: field types, party normalisation, clause normalisation,
    and contract-value extraction.

    Inner node — ANONYMOUS trust (see module docstring).

    Input state keys:
        input_context: dict — {"contract": {...}}, the validated contract bridged
                              from the outer graph. PREFERRED: this channel is
                              not rewritten by the framework's S-2 PII filter.
        validated_input: str — normalised JSON string; the fallback channel, used
                              for direct-invocation tests and legacy callers. Note
                              that the S-2 filter DOES rewrite this field, so
                              personal-data-shaped values arrive as "[MASKED]".

    Output state keys (partial dict):
        contract_data: str   — JSON-serialised normalised contract payload (ADR-005)
        status:        str
        error_log:     list[str]  (only on ERROR)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        # PREFERRED CHANNEL: the validated contract seeded onto `input_context`
        # by DomainWorkflowGraph._extra_initial_state() from the context bridge.
        # The framework's S-2 filter rewrites `user_input` / `validated_input`
        # before this node runs, so reading the contract from those fields means
        # reviewing "[MASKED]" instead of the caller's party names. The context
        # channel is not rewritten, and PreProcessNode has already bounded it.
        context = _without_platform_context(state.get("input_context")) or {}
        payload: Dict[str, Any] = {}
        if isinstance(context, dict) and isinstance(context.get(_CONTRACT_KEY), dict):
            payload = dict(context[_CONTRACT_KEY])
        else:
            raw = state.get("validated_input") or state.get("user_input", "")
            # ── Fallback parse (direct-invocation / legacy string channel) ────
            try:
                payload = json.loads(raw) if isinstance(raw, str) else {}
            except (json.JSONDecodeError, ValueError) as exc:
                logger.error("InputValidateNode: JSON parse error — %s", exc)
                emit_trace_event(
                    "input_validate_failed",
                    {"reason": "json_parse_error", "detail": str(exc)},
                    state,
                )
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [f"InputValidateNode: JSON parse error — {exc}"],
                    # The runner surfaces `formatted_output or result` as `output`. A reason left only in
                    # error_log reaches no one: the terminal result carries just `status`, and get_output()
                    # does not copy error_log out of the graph -- the caller sees a blank spinner.
                    "formatted_output": "Request could not be completed. "
                    + (f"InputValidateNode: JSON parse error — {exc}"),
                }

        if not isinstance(payload, dict):
            emit_trace_event(
                "input_validate_failed",
                {"reason": "payload_not_dict"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["InputValidateNode: payload is not a JSON object"],
                # The runner surfaces `formatted_output or result` as `output`. A reason left only in
                # error_log reaches no one: the terminal result carries just `status`, and get_output()
                # does not copy error_log out of the graph -- the caller sees a blank spinner.
                "formatted_output": "Request could not be completed. "
                + ("InputValidateNode: payload is not a JSON object"),
            }

        contract_id = str(payload.get("contract_id", ""))
        if not contract_id:
            emit_trace_event(
                "input_validate_failed",
                {"reason": "empty_contract_id"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["InputValidateNode: contract_id is missing or empty"],
                # The runner surfaces `formatted_output or result` as `output`. A reason left only in
                # error_log reaches no one: the terminal result carries just `status`, and get_output()
                # does not copy error_log out of the graph -- the caller sees a blank spinner.
                "formatted_output": "Request could not be completed. "
                + ("InputValidateNode: contract_id is missing or empty"),
            }

        # ── Normalise parties ─────────────────────────────────────────────────
        normalised_parties = _normalise_parties(payload.get("parties", []))
        if not normalised_parties:
            emit_trace_event(
                "input_validate_failed",
                {"reason": "no_parties"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["InputValidateNode: parties is empty"],
                # The runner surfaces `formatted_output or result` as `output`. A reason left only in
                # error_log reaches no one: the terminal result carries just `status`, and get_output()
                # does not copy error_log out of the graph -- the caller sees a blank spinner.
                "formatted_output": "Request could not be completed. " + ("InputValidateNode: parties is empty"),
            }

        # ── Normalise clauses ─────────────────────────────────────────────────
        normalised_clauses = _normalise_clauses(payload.get("clauses", []))
        if not normalised_clauses:
            emit_trace_event(
                "input_validate_failed",
                {"reason": "no_clauses"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["InputValidateNode: clauses is empty"],
                # The runner surfaces `formatted_output or result` as `output`. A reason left only in
                # error_log reaches no one: the terminal result carries just `status`, and get_output()
                # does not copy error_log out of the graph -- the caller sees a blank spinner.
                "formatted_output": "Request could not be completed. " + ("InputValidateNode: clauses is empty"),
            }

        # ── Extract value + obligations (fail CLOSED on a non-finite value) ───
        try:
            contract_value = _extract_value(payload.get("contract_value", {}))
        except PayloadError as exc:
            logger.warning("InputValidateNode: contract value rejected — %s", exc)
            emit_trace_event(
                "input_validate_failed",
                {"reason": "invalid_contract_value", "detail": str(exc)},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"InputValidateNode: {exc}"],
                # The runner surfaces `formatted_output or result` as `output`. A reason left only in
                # error_log reaches no one: the terminal result carries just `status`, and get_output()
                # does not copy error_log out of the graph -- the caller sees a blank spinner.
                "formatted_output": "Request could not be completed. " + (f"InputValidateNode: {exc}"),
            }
        obligations = [o for o in payload.get("obligations", []) if isinstance(o, dict)]

        # ── Build normalised contract_data ────────────────────────────────────
        contract_data: Dict[str, Any] = {
            "contract_id": contract_id,
            "contract_type": str(payload.get("contract_type", "unspecified")),
            "parties": normalised_parties,
            "effective_date": str(payload.get("effective_date", "")) or None,
            "expiry_date": str(payload.get("expiry_date", "")) or None,
            "governing_law": str(payload.get("governing_law", "")) or None,
            "contract_value": contract_value,
            "value_amount": contract_value["amount"],
            "currency": contract_value["currency"],
            "clauses": normalised_clauses,
            "obligations": obligations,
        }

        logger.info(
            "InputValidateNode: contract_id=%s parties=%d clauses=%d value=%d %s",
            contract_id,
            len(normalised_parties),
            len(normalised_clauses),
            contract_value["amount"],
            contract_value["currency"],
        )
        emit_trace_event(
            "input_validate_complete",
            {
                "contract_id": contract_id,
                "party_count": len(normalised_parties),
                "clause_count": len(normalised_clauses),
                "obligation_count": len(obligations),
            },
            state,
        )

        return {
            "contract_data": to_json(contract_data),
            "status": AgentStatus.SUCCESS.value,
        }
