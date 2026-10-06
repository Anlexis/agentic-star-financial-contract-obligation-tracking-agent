"""AgentCore Platform v1.0"""

# FIN-C2-077 — PreProcessNode
# Outer backbone pre_process slot. This node owns the CALLER-DATA CONTRACT.
#
# Responsibilities:
#   - Enforce VERIFIED_EXTERNAL trust (required_trust_level = S-1 gate)
#   - Screen every caller field for prompt-injection signatures (S-2 extension)
#   - Accept the contract from the `input_context` channel (preferred) or from
#     the legacy `user_input` JSON string (still supported)
#   - Bound every caller field: structural caps, control-character rejection,
#     identifier form for values that render into the report
#   - Publish the validated contract to the inner graph through the context
#     bridge, and write validated_input + enriched_context to State
#   - Emit an S-4 audit event for every validation decision
#
# WHY THE `input_context` CHANNEL:
#   The framework's S-2 gate masks personal-data shapes in `user_input` and
#   `validated_input` BEFORE template code runs. Party names are exactly that
#   shape. Measured against the real wheel on this repo's own sign-off payload,
#   the review came back with `Contract Type: [MASKED]` and a party rendered as
#   `FPT [MASKED] K.K.` — the agent reported the redaction sentinel as contract
#   fact. `input_context` is not rewritten, so the contract travels there and
#   this node owns the screening the framework would otherwise have applied.
#
# Returns ONLY the state keys this node writes (partial-dict contract).

import json
import logging
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.injection_screen import InjectionRefused, screen_or_raise
from src.schemas.state import to_json
from src.validation import LIMITS, PayloadError, clean_identifier, clean_text, require_list


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

# Required top-level keys for a valid contract payload.
# contract_id is the mandatory identifier; parties and clauses are the minimum
# needed to produce a meaningful contract review & obligation-tracking report.
_REQUIRED_CONTRACT_KEYS = frozenset(
    {
        "contract_id",
        "parties",
        "clauses",
    }
)

# Key under `input_context` that carries the contract object.
_CONTRACT_KEY = "contract"


def _bound_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of *payload* with every caller field bounded and inert.

    Raises PayloadError (naming the field, never the value) on the first
    violation. Every string below is rendered verbatim into a plain-text report
    whose section structure is carried by line breaks, so a control character in
    any of them is a document-forgery primitive, not a formatting quirk.
    """
    # Built from scratch, NOT copied from the caller's dict: an undeclared key is
    # DROPPED rather than carried. Validators that merely ignore unknown keys do
    # not stop them travelling — and anything still present here is echoed into
    # InitializeNode's result on the context channel, where the framework's S-3
    # gate scans it and a credential-shaped value kills the run at node 1.
    bounded: Dict[str, Any] = {}

    bounded["contract_id"] = clean_identifier(payload.get("contract_id"), "contract_id")
    bounded["contract_type"] = clean_text(
        payload.get("contract_type", "unspecified"), "contract_type", LIMITS["name_chars"]
    )
    bounded["governing_law"] = clean_text(payload.get("governing_law", ""), "governing_law", LIMITS["name_chars"])
    bounded["effective_date"] = clean_text(payload.get("effective_date", ""), "effective_date", LIMITS["name_chars"])
    bounded["expiry_date"] = clean_text(payload.get("expiry_date", ""), "expiry_date", LIMITS["name_chars"])

    parties = require_list(payload.get("parties"), "parties", LIMITS["parties"])
    bounded_parties: List[Any] = []
    for index, party in enumerate(parties):
        if isinstance(party, dict):
            bounded_parties.append(
                {
                    "name": clean_text(party.get("name", ""), f"parties[{index}].name", LIMITS["name_chars"]),
                    "role": clean_text(party.get("role", "party"), f"parties[{index}].role", LIMITS["name_chars"]),
                }
            )
        else:
            bounded_parties.append(clean_text(party, f"parties[{index}]", LIMITS["name_chars"]))
    bounded["parties"] = bounded_parties

    clauses = require_list(payload.get("clauses"), "clauses", LIMITS["clauses"])
    bounded["clauses"] = [
        clean_text(clause, f"clauses[{index}]", LIMITS["name_chars"]) for index, clause in enumerate(clauses)
    ]

    obligations = require_list(payload.get("obligations"), "obligations", LIMITS["obligations"])
    bounded_obligations: List[Dict[str, str]] = []
    for index, obligation in enumerate(obligations):
        if not isinstance(obligation, dict):
            raise PayloadError(f"obligations[{index}]: must be an object")
        bounded_obligations.append(
            {
                "description": clean_text(
                    obligation.get("description", ""),
                    f"obligations[{index}].description",
                    LIMITS["text_chars"],
                ),
                "responsible_party": clean_text(
                    obligation.get("responsible_party", ""),
                    f"obligations[{index}].responsible_party",
                    LIMITS["name_chars"],
                ),
                "due_date": clean_text(
                    obligation.get("due_date", ""),
                    f"obligations[{index}].due_date",
                    LIMITS["name_chars"],
                ),
                "type": clean_text(
                    obligation.get("type", "general"),
                    f"obligations[{index}].type",
                    LIMITS["name_chars"],
                ),
            }
        )
    bounded["obligations"] = bounded_obligations

    # contract_value.amount stays raw here on purpose: the finite/bounded parse
    # is the domain validation node's single enforcement point (InputValidateNode
    # ._extract_value). Duplicating it here would make each copy unfalsifiable.
    raw_value = payload.get("contract_value", {})
    if isinstance(raw_value, dict):
        bounded["contract_value"] = {
            "amount": raw_value.get("amount", 0),
            "currency": clean_identifier(
                str(raw_value.get("currency", "JPY")).strip() or "JPY",
                "contract_value.currency",
            ),
        }
    else:
        bounded["contract_value"] = {"amount": raw_value, "currency": "JPY"}

    return bounded


class PreProcessNode(FunctionNode):
    """S-1 trust gate and caller-data contract for FIN-C2-077.

    Validates the caller-supplied contract payload before the domain workflow
    runs. This is the outer backbone's pre_process slot — the only node with
    VERIFIED_EXTERNAL trust, so unauthenticated or anonymous callers are
    rejected here (fail-fast; inner domain nodes carry ANONYMOUS trust and never
    see untrusted input directly).

    Input state keys:
        input_context: dict — {"contract": {...}} (preferred channel)
        user_input:    str  — caller-supplied JSON contract payload (fallback)

    Output state keys (partial dict):
        validated_input:  str        — normalised JSON string (re-serialised)
        enriched_context: str        — JSON-serialised channel metadata (ADR-005)
        status:           str        — AgentStatus.SUCCESS or ERROR
        error_log:        list[str]  — set only on ERROR
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    # ── S-2 extension: template-owned injection screen ───────────────────────
    def _extra_security_gate_input(self, state: AgentState) -> AgentState:
        """Screen every caller field for prompt-injection signatures.

        The framework's default gate covers `user_input` / `validated_input`
        only, and scores `<<SYS>>` as non-blocking. This template also carries
        caller data on `input_context`, which the framework does not scan at
        all. Both channels are screened here — keys included, raw and
        markup-stripped — and a hit rejects the request.

        Contract (framework): must not raise; surface the refusal via state.
        """
        try:
            screen_or_raise(state.get("user_input", ""), "user_input")
            screen_or_raise(_without_platform_context(state.get("input_context", {})), "input_context")
        except InjectionRefused as refusal:
            logger.warning("PreProcessNode: injection screen refused input — %s", refusal)
            emit_trace_event(
                "pre_process_injection_refused",
                {"finding": str(refusal)},
                state,
            )
            gated = dict(state)
            gated["status"] = AgentStatus.ERROR.value
            gated["error_log"] = list(state.get("error_log") or []) + [
                f"PreProcessNode: input refused by the injection screen — {refusal}"
            ]
            return gated
        return state

    def execute(self, state: AgentState) -> Dict[str, Any]:
        input_context = _without_platform_context(state.get("input_context")) or {}
        if not isinstance(input_context, dict):
            input_context = {}
        user_input = state.get("user_input", "")

        payload, channel, failure = self._read_payload(user_input, input_context)
        if failure is not None:
            emit_trace_event("pre_process_validation_failed", failure["event"], state)
            _error_lines = [failure["message"]]
            # The runner surfaces `formatted_output or result` as `output`. A reason left only in
            # error_log reaches no one: the terminal result carries just `status`, and get_output()
            # does not copy error_log out of the graph -- the caller sees a blank spinner.
            # The list is bound once: repeating the expression inline would evaluate it twice.
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": _error_lines,
                "formatted_output": "Request could not be completed. "
                + "; ".join(str(_line) for _line in _error_lines),
            }
        assert payload is not None  # narrowed by `failure is None`

        # ── Required field check ──────────────────────────────────────────────
        missing = _REQUIRED_CONTRACT_KEYS - payload.keys()
        if missing:
            emit_trace_event(
                "pre_process_validation_failed",
                {"reason": "missing_required_fields", "missing": sorted(missing)},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: missing required fields: {sorted(missing)}"],
                # The runner surfaces `formatted_output or result` as `output`. A reason left only in
                # error_log reaches no one: the terminal result carries just `status`, and get_output()
                # does not copy error_log out of the graph -- the caller sees a blank spinner.
                "formatted_output": "Request could not be completed. "
                + (f"PreProcessNode: missing required fields: {sorted(missing)}"),
            }

        # ── Bounds, inertness and structural caps ─────────────────────────────
        try:
            bounded = _bound_payload(payload)
        except PayloadError as exc:
            logger.warning("PreProcessNode: payload rejected — %s", exc)
            emit_trace_event(
                "pre_process_validation_failed",
                {"reason": "field_out_of_bounds", "detail": str(exc)},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: {exc}"],
                # The runner surfaces `formatted_output or result` as `output`. A reason left only in
                # error_log reaches no one: the terminal result carries just `status`, and get_output()
                # does not copy error_log out of the graph -- the caller sees a blank spinner.
                "formatted_output": "Request could not be completed. " + (f"PreProcessNode: {exc}"),
            }

        contract_id = bounded["contract_id"]
        normalised_json = json.dumps(bounded, ensure_ascii=False)

        logger.info(
            "PreProcessNode: validated contract_id=%s channel=%s payload_keys=%d",
            contract_id,
            channel,
            len(bounded),
        )
        emit_trace_event(
            "pre_process_validated",
            {
                "contract_id": contract_id,
                "channel": channel,
                "payload_keys": sorted(bounded.keys()),
            },
            state,
        )

        return {
            # `validated_input` is the framework-visible channel and IS rewritten
            # by the S-2 PII filter downstream. `validated_contract` carries the
            # same content on a field the filter does not scan — it is what
            # ContractReviewGraphNode.extract_input() hands to the inner graph,
            # and it is why the report names the caller's parties instead of
            # certifying "[MASKED]" as a contract fact.
            "validated_input": normalised_json,
            "validated_contract": normalised_json,
            "enriched_context": to_json(
                {
                    "source": "FinancialContractReviewAgent",
                    "channel": str(input_context.get("channel", channel)),
                    "contract_id": contract_id,
                }
            ),
            "status": AgentStatus.SUCCESS.value,
        }

    # ── Payload acquisition ──────────────────────────────────────────────────
    def _read_payload(
        self, user_input: Any, input_context: Dict[str, Any]
    ) -> Tuple[Optional[Dict[str, Any]], str, Optional[Dict[str, Any]]]:
        """Return (payload, channel, failure).

        Prefers `input_context["contract"]`; falls back to parsing `user_input`
        as JSON. `failure` is None on success, otherwise a dict carrying the
        audit-event payload and the caller-facing message.
        """
        candidate = input_context.get(_CONTRACT_KEY)
        if isinstance(candidate, dict):
            return candidate, "input_context", None
        if candidate is not None:
            return (
                None,
                "input_context",
                {
                    "event": {"reason": "context_contract_not_object"},
                    "message": "PreProcessNode: input_context.contract must be a JSON object",
                },
            )

        if not user_input or not isinstance(user_input, str) or not user_input.strip():
            logger.warning("PreProcessNode: no contract on either input channel")
            return (
                None,
                "user_input",
                {
                    "event": {"reason": "empty_input"},
                    "message": "PreProcessNode: user_input is empty or missing",
                },
            )

        encoded = user_input.encode("utf-8")
        if len(encoded) > LIMITS["payload_bytes"]:
            return (
                None,
                "user_input",
                {
                    "event": {"reason": "payload_too_large", "limit": LIMITS["payload_bytes"]},
                    "message": (
                        f"PreProcessNode: user_input exceeds the " f"{LIMITS['payload_bytes']}-byte payload limit"
                    ),
                },
            )

        try:
            parsed = json.loads(user_input.strip())
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("PreProcessNode: JSON parse failed — %s", exc)
            return (
                None,
                "user_input",
                {
                    "event": {"reason": "json_parse_error"},
                    "message": f"PreProcessNode: invalid JSON — {exc}",
                },
            )

        if not isinstance(parsed, dict):
            return (
                None,
                "user_input",
                {
                    "event": {"reason": "payload_not_object"},
                    "message": "PreProcessNode: JSON root must be an object",
                },
            )
        return parsed, "user_input", None
