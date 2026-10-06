"""AgentCore Platform v1.0"""

# FIN-C2-077 — DomainWorkflowGraph (inner BaseGraph)
#
# This is the INNER graph for the Cat 2 two-layer nested architecture.
# It encapsulates the financial-contract review & obligation-tracking pipeline:
#
#   START
#     → input_validate           (InputValidateNode)
#     → parse_contract_data      (ParseContractDataNode)
#     → generate_review_sections (GenerateReviewSectionsNode)
#     → compliance_check         (ComplianceCheckNode)
#     → output_format            (OutputFormatNode)
#     → END
#
# Called by ContractReviewGraphNode.get_subgraph() (graph.py).
# get_output() shapes the sub_result dict consumed by merge_output() there.
#
# Rules enforced:
#   ✅ Inherits BaseGraph (fully custom topology — no forced backbone)
#   ✅ Implements all 7 BaseGraph ABC methods
#   ✅ register_nodes() does NOT call super() (abstract in BaseGraph)
#   ✅ Does NOT register initialize / finalize (outer backbone concerns)
#   ✅ All inner nodes declare required_trust_level = TrustLevel.ANONYMOUS
#   ✅ get_output() designed together with ContractReviewGraphNode.merge_output()
#   ✅ All inner node ctors are empty-parens (no constructor args — CoE no-arg rule)
#   ❌ No Level-0 platform SDK imports
#   ❌ Not placed under src/subagents/

from typing import Any, Dict

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph import context_bridge
from src.nodes.compliance_check_node import ComplianceCheckNode
from src.nodes.generate_review_sections_node import GenerateReviewSectionsNode
from src.nodes.input_validate_node import InputValidateNode
from src.nodes.output_format_node import OutputFormatNode
from src.nodes.parse_contract_data_node import ParseContractDataNode
from src.schemas.state import State


class DomainWorkflowGraph(BaseGraph):
    """Inner domain workflow graph for FIN-C2-077.

    Inherits BaseGraph directly for a fully custom node topology.
    Called by ContractReviewGraphNode.get_subgraph() in graph.py.

    Pipeline (linear):
        START
          → input_validate            (InputValidateNode)
          → parse_contract_data       (ParseContractDataNode)
          → generate_review_sections  (GenerateReviewSectionsNode)
          → compliance_check          (ComplianceCheckNode)
          → output_format             (OutputFormatNode)
          → END

    All nodes are FunctionNode subclasses with ANONYMOUS trust_level.
    initialize / finalize are outer backbone concerns — not registered here.
    """

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "fin_c2_077_contract_review_workflow"

    @property
    def state_schema(self) -> type:
        return State

    # ── Config validation ─────────────────────────────────────────────────────

    def _validate_config(self) -> None:
        """No mandatory config for v1 rule-based inner graph."""
        pass

    # ── Initial state ─────────────────────────────────────────────────────────

    def _extra_initial_state(self) -> Dict[str, Any]:
        """Seed the inner graph's `input_context` — the caller-data bridge.

        `GraphNode.execute()` calls `subgraph.invoke(user_input, session_id=,
        ctx=)` and does NOT forward `input_context`, so without this hook the
        inner graph starts with an empty context and the domain nodes would fall
        back to the string channel — the one the framework's S-2 filter rewrites
        (party names arrive as "[MASKED]").

        Two things are seeded:
          * `contract` — the validated contract published by the outer
            PreProcessNode through the context bridge. Consume-once: the bridge
            pops, so a value can never be picked up by a later invocation.
          * `llm` — the declared runtime settings the outer graph forwarded via
            `DomainWorkflowGraph(config=...)`. Nodes cannot read graph config
            directly: `BaseNode.__call__` invokes `execute(state)` with no
            config argument, so an `execute(state, config=None)` parameter is
            never supplied and every declared value read that way is dead.
        """
        contract = context_bridge.take_contract()
        configurable = (self.config or {}).get("configurable", {}) or {}
        seeded: Dict[str, Any] = {"llm": dict(configurable)}
        if contract is not None:
            seeded["contract"] = contract
        return {"input_context": seeded}

    # ── Node registration ─────────────────────────────────────────────────────

    def register_nodes(self) -> None:
        """Register all 5 domain nodes.

        No super() call — BaseGraph.register_nodes() is abstract.
        Do NOT register initialize or finalize; those are outer backbone
        concerns handled by AgentBaseGraph in graph.py.
        Every key registered here is referenced in add_edges().
        All nodes are instantiated with empty-parens (no ctor args) —
        CoE no-arg ctor rule: SDK v1 FunctionNode subclasses take no arguments.
        """
        self._nodes["input_validate"] = InputValidateNode()
        self._nodes["parse_contract_data"] = ParseContractDataNode()
        self._nodes["generate_review_sections"] = GenerateReviewSectionsNode()
        self._nodes["compliance_check"] = ComplianceCheckNode()
        self._nodes["output_format"] = OutputFormatNode()

    # ── Edge wiring ───────────────────────────────────────────────────────────

    def add_edges(self) -> None:
        """Wire the linear contract-review generation topology.

        Linear flow:
            input_validate → parse_contract_data → generate_review_sections
            → compliance_check → output_format → END.

        No conditional branching — all paths through the review pipeline are
        linear in v1.  route() satisfies the ABC but is not used at runtime.
        """
        self._sg.add_edge(START, "input_validate")
        self._sg.add_edge("input_validate", "parse_contract_data")
        self._sg.add_edge("parse_contract_data", "generate_review_sections")
        self._sg.add_edge("generate_review_sections", "compliance_check")
        self._sg.add_edge("compliance_check", "output_format")
        self._sg.add_edge("output_format", END)

    # ── Routing ───────────────────────────────────────────────────────────────

    def route(self, state: AgentState) -> str:
        """Conditional routing — required by BaseGraph ABC.

        Linear topology; add_conditional_edges() is not used, so this method
        is never called at runtime.  Returns END on error so an unexpected
        invocation does not re-enter a processing node.
        """
        if state.get("status") == AgentStatus.ERROR.value:
            return END
        return "output_format"

    # ── Output shape ──────────────────────────────────────────────────────────

    def get_output(self, state: AgentState) -> Dict[str, Any]:
        """Shape the output dict returned to the outer graph as sub_result.

        This dict is received by ContractReviewGraphNode.merge_output()
        in graph.py as the `sub_result` argument.  Both methods are designed
        together to guarantee field-name consistency:

            Inner get_output() emits:   "contract_review", "review_sections",
                                        "compliance_flags", "escalation_required", "status"
            Outer merge_output() reads: sub_result.get(...) for each key above.
        """
        return {
            "contract_review": state.get("contract_review"),
            "review_sections": state.get("review_sections"),
            "compliance_flags": state.get("compliance_flags"),
            "escalation_required": state.get("escalation_required", False),
            "status": state.get("status"),
            "node_history": state.get("node_history", []),
            "correlation_id": state.get("correlation_id"),
        }
