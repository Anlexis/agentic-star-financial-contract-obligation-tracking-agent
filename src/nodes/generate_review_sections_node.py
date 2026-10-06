"""AgentCore Platform v1.0"""

# FIN-C2-077 — GenerateReviewSectionsNode
# Inner domain node 3: generate all 7 contract-review report sections.
#
# v1 implementation: deterministic template-based generation.
# config/config.yaml declares llm.system_prompt_template (prompts/contract_review.j2),
# llm.temperature and llm.max_tokens. ContractReviewGraphNode._parent_config()
# forwards them to the inner graph, which seeds them onto the inner
# `input_context` — the only channel a node can actually read them from, since
# BaseNode.__call__ calls execute(state) with no config argument.
# SDK v1 ships NO LLM client (there is no framework.services.llm_client), so in
# v1 the section text is synthesised DETERMINISTICALLY from the contract_data
# fields using fixed Python templates. `max_tokens` is enforced as the section
# render budget; the prompt template and temperature are the documented
# production-wiring point and are intentionally NOT invoked here (never faked).
#
# Sections produced:
#   1. review_summary
#   2. contract_parties
#   3. key_terms
#   4. obligations_tracking
#   5. risk_assessment
#   6. compliance_findings
#   7. recommended_actions
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

# Fallback render budget when the runtime config declares no max_tokens.
# Characters, not tokens: the budget bounds the DOCUMENT, and a token count is
# not a meaningful unit for deterministically assembled text. ~4 characters per
# token is the conventional ratio, so the declared max_tokens is scaled by 4.
_DEFAULT_RENDER_BUDGET_CHARS = 16_000
_CHARS_PER_TOKEN = 4
_TRUNCATION_MARK = "\n  [section truncated at the configured render budget]"


def _declared_llm(state: AgentState) -> Dict[str, Any]:
    """Return the declared llm settings seeded onto the inner input_context."""
    context = _without_platform_context(state.get("input_context")) or {}
    if not isinstance(context, dict):
        return {}
    declared = context.get("llm")
    return dict(declared) if isinstance(declared, dict) else {}


def _render_budget(declared_llm: Dict[str, Any]) -> int:
    """Return the per-section character budget derived from declared max_tokens."""
    raw = declared_llm.get("max_tokens")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        return _DEFAULT_RENDER_BUDGET_CHARS
    return raw * _CHARS_PER_TOKEN


def _apply_render_budget(sections: Dict[str, str], budget_chars: int) -> tuple[Dict[str, str], List[str]]:
    """Cap every section at *budget_chars*; return the sections and what was cut.

    This is what makes the declared `llm.max_tokens` an OBSERVABLE setting
    rather than a decorative one, and it is also the structural cap on the
    rendered document: without it a payload carrying the maximum permitted
    number of parties and obligations produces a report bounded only by those
    counts.
    """
    truncated: List[str] = []
    capped: Dict[str, str] = {}
    for key, text in sections.items():
        if len(text) > budget_chars:
            capped[key] = text[:budget_chars] + _TRUNCATION_MARK
            truncated.append(key)
        else:
            capped[key] = text
    return capped, truncated


def _section_review_summary(d: Dict[str, Any]) -> str:
    """Generate the Review Summary section."""
    contract_id = d.get("contract_id", "N/A")
    contract_type = d.get("contract_type", "unspecified")
    risk_tier = str(d.get("risk_tier", "unknown")).upper()
    value = int(d.get("value_amount", 0))
    currency = d.get("currency", "JPY")
    lines = [
        f"Contract ID:   {contract_id}",
        f"Contract Type: {contract_type}",
        f"Risk Tier:     {risk_tier}",
        f"Contract Value: {value:,} {currency}",
        f"Parties:       {len(d.get('parties', []))}",
        f"Obligations:   {len(d.get('obligations', []))}",
    ]
    return "\n".join(lines)


def _section_contract_parties(d: Dict[str, Any]) -> str:
    """Generate the Contract Parties section."""
    parties: List[Dict[str, Any]] = d.get("parties", [])
    if not parties:
        return "No parties listed."
    lines = [f"  - {p.get('name', 'Unknown')} ({p.get('role', 'party')})" for p in parties]
    return "\n".join(lines)


def _section_key_terms(d: Dict[str, Any]) -> str:
    """Generate the Key Terms section."""
    effective = d.get("effective_date") or "N/A"
    expiry = d.get("expiry_date") or "Open-ended"
    term = int(d.get("term_months", 0))
    law = d.get("governing_law") or "Not specified"
    value = int(d.get("value_amount", 0))
    currency = d.get("currency", "JPY")
    lines = [
        f"  Effective Date:  {effective}",
        f"  Expiry Date:     {expiry}",
        f"  Term:            {term} months",
        f"  Governing Law:   {law}",
        f"  Contract Value:  {value:,} {currency}",
    ]
    return "\n".join(lines)


def _section_obligations_tracking(d: Dict[str, Any]) -> str:
    """Generate the Obligations Tracking section."""
    obligations: List[Dict[str, Any]] = d.get("obligations", [])
    if not obligations:
        return "No tracked obligations recorded."
    lines = []
    for i, ob in enumerate(obligations, 1):
        desc = str(ob.get("description", "(no description)"))
        party = str(ob.get("responsible_party", "unspecified"))
        due = str(ob.get("due_date", "no due date"))
        otype = str(ob.get("type", "general"))
        lines.append(f"  {i}. [{otype}] {desc}")
        lines.append(f"       Responsible: {party} | Due: {due}")
    return "\n".join(lines)


def _section_risk_assessment(d: Dict[str, Any]) -> str:
    """Generate the Risk Assessment section."""
    risk_tier = str(d.get("risk_tier", "unknown")).upper()
    value = int(d.get("value_amount", 0))
    term = int(d.get("term_months", 0))
    lines = [
        f"  Overall Risk Tier: {risk_tier}",
        f"  Basis: contract value {value:,} over a {term}-month term.",
        "  High-value or long-duration contracts warrant senior legal review.",
    ]
    return "\n".join(lines)


def _section_compliance_findings(d: Dict[str, Any]) -> str:
    """Generate the Compliance Findings section (advisory pre-check).

    The authoritative determination is produced by ComplianceCheckNode; this
    section reflects the clause pre-check computed in ParseContractDataNode.
    """
    present: List[str] = d.get("clauses", [])
    missing: List[str] = d.get("missing_mandatory_clauses", [])
    lines = [
        f"  Present Clauses: {', '.join(present) if present else 'none'}",
        f"  Missing Mandatory Clauses: {', '.join(missing) if missing else 'none'}",
        f"  Mandatory Coverage: {'COMPLETE' if not missing else 'INCOMPLETE'}",
    ]
    return "\n".join(lines)


def _section_recommended_actions(d: Dict[str, Any]) -> str:
    """Generate the Recommended Actions section."""
    missing: List[str] = d.get("missing_mandatory_clauses", [])
    risk_tier = str(d.get("risk_tier", "low"))
    actions: List[str] = []
    if missing:
        actions.append(f"Add or negotiate the missing mandatory clause(s): {', '.join(missing)}.")
    if risk_tier == "high":
        actions.append("Route to senior legal counsel for high-risk review.")
    if int(d.get("obligations_count", 0)) > 0:
        actions.append("Register tracked obligations with the obligation-monitoring calendar.")
    if not actions:
        actions.append("No blocking issues found; proceed to standard approval workflow.")
    return "\n".join(f"  {i}. {a}" for i, a in enumerate(actions, 1))


class GenerateReviewSectionsNode(FunctionNode):
    """Generate all 7 contract-review report sections.

    v1: deterministic template-based synthesis (no SDK LLM client).
    Production: the declared llm.system_prompt_template — forwarded by
    ContractReviewGraphNode._parent_config() as
    config["configurable"]["system_prompt_template"] — would drive an LLM. It is
    NOT invoked in v1; the sections are synthesised deterministically below.

    Inner node — ANONYMOUS trust (see module docstring).

    Input state keys:
        contract_data: str  — JSON-serialised enriched contract payload (ADR-005)

    Output state keys (partial dict):
        review_sections: str  — JSON-serialised section dict (ADR-005)
        status:          str
        error_log:       list[str]  (only on ERROR)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        contract_data: Dict[str, Any] = from_json(state.get("contract_data"), {})

        # The declared llm block (config/config.yaml) is seeded onto the inner
        # graph's `input_context` by DomainWorkflowGraph._extra_initial_state().
        #
        # It is NOT read from an `execute(state, config=...)` argument: the
        # framework calls `execute(state)` with one argument, so that parameter
        # was never supplied and `system_prompt_template` was always None — the
        # declared configuration looked wired and was dead on every invocation.
        #
        # SDK v1 ships NO LLM client, so this node synthesises the sections
        # DETERMINISTICALLY via the _section_* helpers below. `max_tokens` is
        # enforced as a real render budget (see _apply_render_budget); the
        # prompt template and temperature are the documented production-wiring
        # point and are recorded in the trace event, never invoked or faked.
        declared_llm = _declared_llm(state)
        system_prompt_template = declared_llm.get("system_prompt_template")
        render_budget = _render_budget(declared_llm)

        if not contract_data:
            logger.error("GenerateReviewSectionsNode: contract_data missing in state")
            emit_trace_event(
                "generate_review_sections_failed",
                {"reason": "missing_contract_data"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["GenerateReviewSectionsNode: contract_data missing in state"],
                # The runner surfaces `formatted_output or result` as `output`. A reason left only in
                # error_log reaches no one: the terminal result carries just `status`, and get_output()
                # does not copy error_log out of the graph -- the caller sees a blank spinner.
                "formatted_output": "Request could not be completed. "
                + ("GenerateReviewSectionsNode: contract_data missing in state"),
            }

        contract_id = contract_data.get("contract_id", "unknown")

        # ── Generate all sections ─────────────────────────────────────────────
        sections: Dict[str, str] = {
            "review_summary": _section_review_summary(contract_data),
            "contract_parties": _section_contract_parties(contract_data),
            "key_terms": _section_key_terms(contract_data),
            "obligations_tracking": _section_obligations_tracking(contract_data),
            "risk_assessment": _section_risk_assessment(contract_data),
            "compliance_findings": _section_compliance_findings(contract_data),
            "recommended_actions": _section_recommended_actions(contract_data),
        }
        sections, truncated = _apply_render_budget(sections, render_budget)

        logger.info(
            "GenerateReviewSectionsNode: contract_id=%s sections=%d",
            contract_id,
            len(sections),
        )
        emit_trace_event(
            "generate_review_sections_complete",
            {
                "contract_id": contract_id,
                "section_count": len(sections),
                "section_keys": sorted(sections.keys()),
                "prompt_template": system_prompt_template,
                "temperature": declared_llm.get("temperature"),
                "render_budget_chars": render_budget,
                "budget_truncated": sorted(truncated),
                "generation_mode": "deterministic_template_v1",
            },
            state,
        )

        return {
            "review_sections": to_json(sections),
            "status": AgentStatus.SUCCESS.value,
        }
