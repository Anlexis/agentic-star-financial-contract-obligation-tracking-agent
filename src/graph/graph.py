"""AgentCore Platform v1.0"""

# FIN-C2-077 — Outer graph (AgentBaseGraph; Cat 2 two-layer nested architecture)
#
# Architecture (Cat 2):
#
#   Outer backbone (fixed — identical to Cat 1, do NOT override add_edges()):
#     START → initialize → pre_process → main → {route} → post_process → finalize → END
#                                             ↓ (RETRY, max 3)
#                                          pre_process
#
#   `main` slot is a GraphNode subclass (ContractReviewGraphNode) that delegates
#   the full domain workflow to DomainWorkflowGraph (inner BaseGraph).
#
#   Domain complexity is fully encapsulated inside the inner graph. The outer
#   backbone is never modified.
#
# Directory layout:
#   src/graph/graph.py                 ← outer graph (this file)
#   src/graph/domain_workflow_graph.py ← inner graph (multi-step topology)
#
# Rules enforced:
#   ✅ FinancialContractReviewAgent inherits AgentBaseGraph (L1 Base)
#   ✅ super().register_nodes() called first (fills initialize + finalize)
#   ✅ ContractReviewGraphNode assigned to self._nodes["main"]
#   ✅ PreProcessNode (VERIFIED_EXTERNAL) in pre_process slot (S-1 gate)
#   ✅ PostProcessNode (ANONYMOUS) in post_process slot (S-3 output gate)
#   ✅ S-3 _security_gate_output implemented on the agent class (§5-6), and
#      delegating to the ONE module-level gate so the two cannot drift
#   ✅ _parent_config() forwards the runtime config declared in config/config.yaml
#   ✅ extract_input() bridges the validated contract to the inner graph — the
#      framework forwards no input_context across the GraphNode boundary, and
#      the string it does forward is rewritten by the S-2 PII filter
#   ✅ get_output() surfaces the domain result on the outer invoke
#   ✅ merge_output() returns only changed keys
#   ✅ class name matches config/agent.yaml class: field exactly
#   ❌ add_edges() NOT overridden on the outer graph
#   ❌ No Level-0 platform SDK imports

from typing import Any, ClassVar, Dict, Optional

from framework.errors import ConfigError
from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.trust_level import TrustLevel
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph import context_bridge
from src.nodes.post_process_node import PostProcessNode, evaluate_output_gate
from src.nodes.pre_process_node import PreProcessNode
from src.runtime_config import load_runtime_config
from src.schemas.state import State, from_json


class ContractReviewGraphNode(GraphNode):
    """GraphNode subclass assigned to the `main` slot of FinancialContractReviewAgent.

    Wraps DomainWorkflowGraph (inner Cat 2 BaseGraph).
    Called by AgentBaseGraph backbone after pre_process and before post_process.

    Contracts:
      get_subgraph()  — instantiate and return DomainWorkflowGraph
      extract_input() — pull validated_input (S-1 output) from outer state
      merge_output()  — map sub_result fields into outer state delta (changed keys only)
      error_strategy  — "propagate": re-raise inner errors as SubgraphError (fail-fast)
    """

    # S-1 declared on the wrapper too: the CI gate only AST-scans FunctionNode
    # subclasses, so a GraphNode main slot passes the pipeline without one and is
    # flagged at review. Same level the nodes in this repo already declare.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False

    # Runtime parameters handed down by the outer graph in register_nodes().
    # Assigned as an ATTRIBUTE, never a constructor argument — SDK v1 node
    # classes are instantiated with empty parens by the graph builder.
    runtime_config: Dict[str, Any] = {}

    def get_subgraph(self) -> Any:
        """Instantiate and return the inner domain workflow graph.

        DomainWorkflowGraph is imported lazily (inside the method) to avoid
        circular-import risk at module load time and to match the Cat 2 pattern.
        """
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        return DomainWorkflowGraph(config=self._parent_config())

    def extract_input(self, state: AgentState) -> str:
        """Return the string input passed into inner_graph.invoke().

        ALSO publishes the validated contract to the inner graph through the
        context bridge. `GraphNode.execute()` forwards only this one string and
        the InvocationContext — never `input_context` — and the string it
        forwards lands on the inner `user_input`, which the framework's S-2
        filter rewrites. So the structured contract travels out-of-band.

        The set has to happen HERE, not in PreProcessNode: LangGraph gives each
        node its own copied context, so a ContextVar set in an earlier node is
        invisible by the time the inner graph builds its state. This method and
        the inner `invoke()` share one call stack.
        """
        raw_contract = state.get("validated_contract")
        contract = from_json(raw_contract if isinstance(raw_contract, str) else None, None)
        context_bridge.set_contract(contract if isinstance(contract, dict) else None)
        return str(state.get("validated_input") or state.get("user_input") or "")

    def merge_output(self, state: AgentState, sub_result: Dict[str, Any]) -> Dict[str, Any]:
        """Map inner graph sub_result back into the outer state delta.

        sub_result is the dict returned by DomainWorkflowGraph.get_output().
        Returns ONLY changed keys — never the full state.

        Key coupling (designed together with DomainWorkflowGraph.get_output()):
          Inner get_output() emits  → "contract_review", "review_sections",
                                       "compliance_flags", "escalation_required", "status"
          This merge_output() reads → sub_result.get(...) for each of these keys.

        PostProcessNode (outer post_process) reads contract_review + escalation_required
        from state to apply the S-3 gate and set formatted_output.
        """
        return {
            "contract_review": sub_result.get("contract_review"),
            "review_sections": sub_result.get("review_sections"),
            "compliance_flags": sub_result.get("compliance_flags"),
            "escalation_required": sub_result.get("escalation_required", False),
            "status": sub_result.get("status"),
        }

    def _parent_config(self) -> Dict[str, Any]:
        """Forward the declared runtime config to the inner graph.

        The declared values now live in ``config/config.yaml`` — the manifest
        migration moved the ``config:`` block out of ``config/agent.yaml``, and
        the previous reader still asked the manifest for an ``agent.config``
        block that no longer exists. It would have returned ``{}`` on every
        invocation: manifest valid, suite green, and every declared value dead.

        Preference order: the config the outer graph was CONSTRUCTED with (the
        platform passes ``config/config.yaml`` to ``Graph(config=...)``), then
        the file itself, so the standalone entry point behaves identically.

        Only keys that are actually declared (non-None) are forwarded; absent
        keys fall back to node defaults.

        v1 note: SDK v1 ships no LLM client, so GenerateReviewSectionsNode's
        synthesis is deterministic. ``max_tokens`` is enforced as the section
        render budget (a real, observable effect); ``system_prompt_template``
        and ``temperature`` are the documented production-wiring contract and
        are recorded in the audit event rather than faked.
        """
        cfg = self.runtime_config or load_runtime_config()
        llm = cfg.get("llm", {}) or {}
        declared = {
            "system_prompt_template": llm.get("system_prompt_template"),
            "temperature": llm.get("temperature"),
            "max_tokens": llm.get("max_tokens"),
        }
        return {"configurable": {k: v for k, v in declared.items() if v is not None}}


class FinancialContractReviewAgent(AgentBaseGraph):
    """Outer graph for FIN-C2-077 (Cat 2 — DocGenerationAgent).

    Inherits AgentBaseGraph directly (L1 Base). Domain logic is fully
    encapsulated in ContractReviewGraphNode (main slot), which delegates to
    DomainWorkflowGraph (inner BaseGraph).

    Backbone (fixed — identical to Cat 1):
        START → initialize → pre_process → main → post_process → finalize → END

    register_nodes() and get_output() are the overrides:
      - super().register_nodes() fills: initialize, finalize (framework defaults)
      - pre_process: PreProcessNode    (VERIFIED_EXTERNAL — S-1 trust gate)
      - main:        ContractReviewGraphNode (delegates to DomainWorkflowGraph)
      - post_process: PostProcessNode  (ANONYMOUS — S-3 output gate)
      - get_output(): surfaces the structured domain result (SR #12)

    add_edges() is NOT overridden — backbone wiring belongs to the framework.

    Class name MUST match config/agent.yaml `class:` field exactly.
    server.py imports this as `Graph` via the alias below.
    """

    @property
    def name(self) -> str:
        """Agent identifier registered with AgentRegistry."""
        return "FinancialContractReviewAgent"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        """Fill all 5 backbone slots.

        super().register_nodes() MUST be called first — it injects the
        framework's default InitializeNode (sets schema_version, session_id,
        trust_level) and FinalizeNode (builds response_metadata, total_time_ms).
        """
        super().register_nodes()  # fills: initialize, finalize

        main_node = ContractReviewGraphNode()
        # Hand the graph's own runtime config down to the main slot so the
        # declared llm block reaches the inner graph on BOTH entry paths (the
        # platform constructs Graph(config=...); the standalone server loads
        # config/config.yaml and does the same). Attribute assignment, not a
        # constructor argument — SDK v1 node classes take no ctor args.
        main_node.runtime_config = dict(self.config or {})

        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = main_node
        self._nodes["post_process"] = PostProcessNode()

    def _validate_config(self) -> None:
        """Validate the backbone config, then this template's own invariant.

        `security.s3_gate_enabled` is a declared platform-contract flag. It has
        exactly one correct value for this template (§5-6 makes the S-3 output
        gate mandatory), so it is asserted here rather than being consulted as a
        switch — a config file that says the gate is off is a deployment error,
        not an instruction to run without an output gate.
        """
        super()._validate_config()
        security = self.config.get("security")
        if isinstance(security, dict) and security.get("s3_gate_enabled") is False:
            raise ConfigError(
                f"[{self.__class__.__name__}] 'security.s3_gate_enabled' must not be "
                "false — the S-3 output gate is mandatory for this template."
            )

    def get_output(self, state: AgentState) -> Dict[str, Any]:
        """Surface the domain contract-review result on the outer invoke() return.

        SR #12: AgentBaseGraph.get_output() returns only the minimal
        ``{output, status, trace_id, correlation_id, node_history}`` envelope. On
        the compiled outer-graph success path that dropped the structured domain
        result the inner DomainWorkflowGraph produces and
        ContractReviewGraphNode.merge_output() merges into outer state
        (contract_review, review_sections, compliance_flags, escalation_required)
        — they were absent from the dict returned by ``agent.invoke()`` even on a
        successful review. This override extends the base envelope so a successful
        invocation actually returns the structured domain result (notably the
        machine-readable ``escalation_required`` signal and structured
        ``compliance_flags``, which a caller would otherwise have to parse out of
        the report text).

        S-3 invariant preserved (fail-closed):
          * ``formatted_output`` / ``result`` / ``contract_review`` are the
            POST-gate, caller-facing values produced by PostProcessNode — the S-3
            output gate has already blocked credential patterns (status -> ERROR +
            sanitised stub). We surface those, NEVER the pre-gate raw
            ``state["contract_review"]`` written by the inner OutputFormatNode, so
            the output gate cannot be bypassed.
          * The structured fields (review_sections / compliance_flags /
            escalation_required) are surfaced ONLY when the gate passed
            (status == SUCCESS). On any non-success outcome — including an S-3
            credential block — they are withheld (None).
        """
        # {output, status, trace_id, correlation_id, node_history}
        output: Dict[str, Any] = super().get_output(state)
        succeeded = state.get("status") == AgentStatus.SUCCESS.value
        gated_report = state.get("formatted_output") or state.get("result")

        # Caller-facing, already S-3-gated values (safe on both paths).
        output["formatted_output"] = state.get("formatted_output")
        output["result"] = state.get("result")
        output["contract_review"] = gated_report if succeeded else None

        # Structured domain result — surfaced only on the gated success path.
        output["review_sections"] = state.get("review_sections") if succeeded else None
        output["compliance_flags"] = state.get("compliance_flags") if succeeded else None
        output["escalation_required"] = state.get("escalation_required") if succeeded else None
        return output

    # ── S-3 output gate (agent-class implementation; the framework rules §5-6) ────────────
    def _security_gate_output(self, content: str) -> Optional[str]:
        """S-3 domain output gate for FIN-C2-077.

        Delegates to the single module-level implementation in
        `post_process_node.evaluate_output_gate`, so the agent-level contract
        point (§5-6) and the runtime post_process slot can never drift apart.
        This used to be a SECOND, independently maintained pattern list; a fix
        applied to one of the two would silently leave the other narrower.

        Implemented on the AGENT CLASS (an AgentBaseGraph subclass, not a node)
        so it is never auto-wrapped by the real SDK's node gate-hook mechanism.
        """
        return evaluate_output_gate(content)

    # add_edges() is NOT overridden — backbone wiring belongs to the framework.


# Alias for backward compat (server.py imports Graph).
# Class name FinancialContractReviewAgent matches config/agent.yaml class: field.
Graph = FinancialContractReviewAgent
