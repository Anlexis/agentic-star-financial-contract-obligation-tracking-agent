# PB-6: Invoke Execution Order Verification
# Verifies BaseNode.__call__() enforces: S-1 trust gate -> S-4 node_start ->
# S-2 _security_gate_input() -> execute() -> S-3 _security_gate_output() ->
# S-4 node_complete, for every concrete node under src/nodes/.
#
# Also verifies the full backbone invoke order for the outer
# FinancialContractReviewAgent (Cat 2 two-layer nested graph):
#   InitializeNode -> PreProcessNode (pre_process) -> ContractReviewGraphNode (main)
#   -> PostProcessNode (post_process) -> FinalizeNode
#
# PB-6 invoke uses VERIFIED_EXTERNAL caller trust (the real external path) — NEVER
# for_internal(). A VERIFIED_EXTERNAL InvocationContext exercises the same code path a
# real STG caller uses: it clears the outer PreProcessNode S-1 gate
# (required_trust_level = VERIFIED_EXTERNAL) AND passes through the inner ANONYMOUS
# domain nodes. for_internal() (INTERNAL) would not represent a real external caller,
# so it is deliberately not used.

import importlib
import inspect
import json
import pkgutil
from pathlib import Path

import pytest

# ── Template-specific constants ───────────────────────────────────────────────

# Class name of the node in the `main` backbone slot.
_MAIN_SLOT_NODE = "ContractReviewGraphNode"

# A SUCCESS-yielding financial-contract payload for the backbone invoke test.
# Escalation-required: contract value (82,000,000 JPY) >= escalation threshold
# (10,000,000) AND >= high-value threshold (50,000,000) => risk tier "high".
# All PreProcessNode required fields present (contract_id, parties, clauses) and
# all six mandatory clauses covered (termination, liability, confidentiality,
# governing_law, payment_terms, dispute_resolution) after normalisation.
#
# THE CONTRACT IS READ FROM `input_context`, NEVER FROM `user_input`. The
# framework's S-2 filter masks personal-data shapes in `user_input` before any
# template code runs: read from there, this contract's parties come back as
# "FPT [MASKED] K.K." and its type as "[MASKED]", and the agent would certify
# the redaction sentinel as contract fact. test_masking_does_not_reach_the_report
# below pins that. Marketplace chat can send nothing but a string, so the
# adapter hook (src/services/chat_context.py, wired in cli.py) lifts a pasted
# JSON record into `input_context` before the first node runs — which is why the
# published payload may carry the record in `input` as well without reopening
# the masked path.
#
# CONTRACT: deploy/invoke_payload.json MUST carry the identical body — the
# Stage-5 deploy-stg evidence invoke and the PB-6 test must exercise the same
# request. test_invoke_payload_matches_pb6 asserts it so the two cannot drift.
_VALID_CONTRACT = {
    "contract_id": "FIN-CTR-20260712-001",
    "contract_type": "Master Services Agreement",
    "parties": [
        {"name": "Sumitomo Mitsui Banking Corporation", "role": "client"},
        {"name": "FPT Software Japan K.K.", "role": "vendor"},
    ],
    "effective_date": "2026-04-01",
    "expiry_date": "2029-04-01",
    "governing_law": "Japan",
    "contract_value": {"amount": 82000000, "currency": "JPY"},
    "clauses": [
        "termination",
        "liability",
        "confidentiality",
        "governing law",
        "payment terms",
        "dispute resolution",
        "indemnity",
    ],
    "obligations": [
        {
            "description": "Deliver quarterly SLA compliance report",
            "responsible_party": "FPT Software Japan K.K.",
            "due_date": "2026-06-30",
            "type": "reporting",
        },
        {
            "description": "Complete annual SOC 2 Type II audit",
            "responsible_party": "FPT Software Japan K.K.",
            "due_date": "2026-09-30",
            "type": "audit",
        },
    ],
}

# The string channel: still accepted by PreProcessNode as a fallback, used by the
# tests that exercise that path explicitly, and — since the Marketplace adapter
# hook lifts it into `input_context` — the only channel a chat user has.
_VALID_PAYLOAD = json.dumps(_VALID_CONTRACT)

# The request body the standalone adapter and the STG evidence invoke both send.
# It carries the contract on BOTH channels on purpose. `input_context` is the
# preferred one and _read_payload takes it first, so the STG invoke still runs
# unmasked; `input` carries the same record so that a chat user, who can send
# nothing but a string, reaches the same review through the adapter hook.
_VALID_REQUEST = {"input": _VALID_PAYLOAD, "input_context": {"contract": _VALID_CONTRACT}}

# ─────────────────────────────────────────────────────────────────────────────


def _discover_node_classes() -> list[type]:
    """Import every module under src/nodes/ and collect concrete BaseNode subclasses."""
    from framework.nodes.base_node import BaseNode

    try:
        pkg = importlib.import_module("src.nodes")
    except ImportError:
        return []

    discovered = []
    for _, modname, _ in pkgutil.walk_packages(pkg.__path__, prefix="src.nodes."):
        module = importlib.import_module(modname)
        for attr in vars(module).values():
            if (
                isinstance(attr, type)
                and issubclass(attr, BaseNode)
                and attr is not BaseNode
                and attr.__module__ == modname
                and not inspect.isabstract(attr)
            ):
                discovered.append(attr)
    return discovered


def _patch_domain_emit(monkeypatch):
    """Patch emit_trace_event in every domain node module (avoids audit-backend calls)."""
    for mod_suffix in (
        "pre_process_node",
        "input_validate_node",
        "parse_contract_data_node",
        "generate_review_sections_node",
        "compliance_check_node",
        "output_format_node",
        "post_process_node",
    ):
        try:
            monkeypatch.setattr(
                f"src.nodes.{mod_suffix}.emit_trace_event",
                lambda *a, **k: None,
            )
        except AttributeError:
            pass  # module not yet imported / no emit symbol; fine


class TestInvokeOrder:
    """PB-6: __call__ must run S-1 -> node_start -> S-2 -> execute() -> S-3 -> node_complete."""

    def test_call_order_for_every_node(self, monkeypatch):
        node_classes = _discover_node_classes()
        if not node_classes:
            pytest.skip("no concrete BaseNode subclasses found under src/nodes/")

        import framework.nodes.base_node as base_node_module

        failures: list[str] = []
        for node_cls in node_classes:
            order: list[str] = []
            monkeypatch.setattr(
                base_node_module,
                "emit_trace_event",
                lambda event_type, _payload, _state, _o=order: _o.append(f"event:{event_type}"),
            )

            for method_name, label in (
                ("_security_gate_input", "security_gate_input"),
                ("execute", "execute"),
                ("_security_gate_output", "security_gate_output"),
            ):
                original = getattr(node_cls, method_name)

                def spy(self, arg, _o=order, _label=label, _orig=original):
                    _o.append(_label)
                    return _orig(self, arg)

                monkeypatch.setattr(node_cls, method_name, spy)

            instance = node_cls()
            # caller trust == the node's required level so the S-1 gate always passes here;
            # the gate-denial branch is asserted separately in TestS1TrustGate.
            state = {
                "caller_trust_level": node_cls.required_trust_level.value,
                "correlation_id": "pb6-invoke-order-test",
            }
            instance(state)

            expected = [
                "event:node_start",
                "security_gate_input",
                "execute",
                "security_gate_output",
                "event:node_complete",
            ]
            if order != expected:
                failures.append(
                    f"{node_cls.__name__}: invoke order violation.\n" f"expected: {expected}\nactual:   {order}"
                )

        assert not failures, "\n\n".join(failures)


class TestS1TrustGate:
    """PB-6 S-1: the trust gate in BaseNode.__call__ runs BEFORE execute() and denies
    a caller whose trust is below the node's required_trust_level."""

    def test_pre_process_denies_anonymous_caller(self, monkeypatch):
        """PreProcessNode (required VERIFIED_EXTERNAL) must refuse an ANONYMOUS caller."""
        _patch_domain_emit(monkeypatch)
        from framework.schemas.agent_status import AgentStatus
        from framework.schemas.trust_level import TrustLevel
        from src.nodes.pre_process_node import PreProcessNode

        node = PreProcessNode()
        result = node(
            {
                "caller_trust_level": TrustLevel.ANONYMOUS.value,
                "user_input": _VALID_PAYLOAD,
                "correlation_id": "pb6-s1-denial",
            }
        )
        assert result["status"] == AgentStatus.ERROR.value
        assert any(
            "trust gate" in e.lower() for e in result.get("error_log", [])
        ), f"expected an S-1 trust-gate denial, got error_log={result.get('error_log')}"

    def test_pre_process_admits_verified_external_caller(self, monkeypatch):
        """The same node admits a VERIFIED_EXTERNAL caller and runs execute() to SUCCESS."""
        _patch_domain_emit(monkeypatch)
        from framework.schemas.agent_status import AgentStatus
        from framework.schemas.trust_level import TrustLevel
        from src.nodes.pre_process_node import PreProcessNode

        node = PreProcessNode()
        result = node(
            {
                "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
                "user_input": _VALID_PAYLOAD,
                "input_context": {},
                "correlation_id": "pb6-s1-admit",
            }
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["validated_input"] is not None


class TestBackboneInvokeOrder:
    """PB-6 backbone: a full Graph().invoke() runs the 5-node backbone in order.

    Backbone order: InitializeNode -> PreProcessNode (pre_process) ->
                    ContractReviewGraphNode (main) ->
                    PostProcessNode (post_process) -> FinalizeNode

    Uses VERIFIED_EXTERNAL caller trust — the real external path.
    InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL) is mandatory;
    NEVER use for_internal(), which would not represent a real external caller.
    """

    def _invoke(self, monkeypatch, contract=None):
        _patch_domain_emit(monkeypatch)
        from framework.schemas.invocation_context import InvocationContext, TrustLevel
        from src.graph.graph import Graph
        from src.runtime_config import load_runtime_config

        agent = Graph(config=load_runtime_config())
        agent.compile()
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        return agent.invoke("", ctx=ctx, input_context={"contract": contract or _VALID_CONTRACT})

    def test_backbone_invoke_succeeds_and_returns_output(self, monkeypatch):
        from framework.schemas.agent_status import AgentStatus

        result = self._invoke(monkeypatch)
        assert result.get("status") == AgentStatus.SUCCESS.value, (
            f"Expected status={AgentStatus.SUCCESS.value!r}, got: {result.get('status')!r}\n"
            f"error_log: {result.get('error_log')}"
        )
        assert result.get("output") is not None, "output must be set after a successful invoke"
        # The assembled contract review must be present in the surfaced output.
        assert "FINANCIAL CONTRACT REVIEW & OBLIGATION TRACKING REPORT" in result["output"]
        assert "FIN-CTR-20260712-001" in result["output"]

    def test_backbone_node_history_matches_expected_order(self, monkeypatch):
        result = self._invoke(monkeypatch)
        history = result.get("node_history", [])
        assert history == [
            "InitializeNode",
            "PreProcessNode",
            "ContractReviewGraphNode",
            "PostProcessNode",
            "FinalizeNode",
        ], f"unexpected backbone node_history: {history}"

    def test_main_slot_is_contract_review_graph_node(self):
        """The `main` backbone slot must be ContractReviewGraphNode (a GraphNode — Cat 2)."""
        from framework.nodes.graph_node import GraphNode
        from src.graph.graph import ContractReviewGraphNode, FinancialContractReviewAgent

        agent = FinancialContractReviewAgent()
        agent.compile()
        main_node = agent._nodes.get("main")
        assert main_node is not None, "main slot must be registered"
        assert isinstance(
            main_node, ContractReviewGraphNode
        ), f"main slot must be ContractReviewGraphNode, got {type(main_node).__name__}"
        assert isinstance(main_node, GraphNode), "main slot node must subclass GraphNode (Cat 2 contract)"
        assert main_node.__class__.__name__ == _MAIN_SLOT_NODE

    def test_invoke_payload_matches_pb6(self):
        """deploy/invoke_payload.json MUST carry the PB-6 request (Stage-5 alignment).

        The deploy-stg evidence invoke (stg_invoke_evidence.py POSTs
        invoke_payload.json as the request body) must exercise the same request
        PB-6 asserts yields SUCCESS — including the CHANNEL. `input_context`
        carries the contract and _read_payload prefers it, so STG runs unmasked;
        `input` carries the identical record because it is the only channel
        Marketplace chat has, and the adapter hook lifts it into `input_context`
        before any node runs. Both must stay in step with the record here.
        """
        repo_root = Path(__file__).resolve().parents[2]
        payload_file = repo_root / "deploy" / "invoke_payload.json"
        assert payload_file.exists(), "deploy/invoke_payload.json is required for deploy-stg"
        body = json.loads(payload_file.read_text())
        assert body.get("input") == _VALID_REQUEST["input"]
        assert (
            body.get("input_context") == _VALID_REQUEST["input_context"]
        ), "deploy/invoke_payload.json['input_context'] must equal the PB-6 request"
        contract = body["input_context"]["contract"]
        for required in ("contract_id", "parties", "clauses"):
            assert required in contract, f"invoke_payload contract missing field: {required}"

    def test_masking_does_not_reach_the_report(self, monkeypatch):
        """A masked value must never be reported as an extracted contract fact.

        The platform's S-2 filter masks personal-data shapes in `user_input`
        BEFORE template code runs. On the string channel this repo's own
        sign-off payload came back with `Contract Type: [MASKED]` and a party
        rendered as `FPT [MASKED] K.K.`. The context channel is not rewritten,
        so the report must carry the caller's own text.
        """
        result = self._invoke(monkeypatch)
        report = result["output"]
        assert "[MASKED]" not in report
        assert "Sumitomo Mitsui Banking Corporation" in report
        assert "FPT Software Japan K.K." in report
        assert "Master Services Agreement" in report

    def test_declared_config_reaches_the_inner_graph_end_to_end(self, monkeypatch):
        """A declared runtime value must change behaviour through a full invoke.

        The path is long and every hop used to be broken somewhere: the graph is
        constructed with config/config.yaml, the main slot receives it,
        `_parent_config()` forwards the llm block, the inner graph seeds it onto
        `input_context`, and the section node reads it there. Asserting it at the
        node level would prove none of that — the previous reader asked the
        manifest for an `agent.config` block the migration had removed, and the
        node read a config argument the framework never supplies.
        """
        from framework.schemas.invocation_context import InvocationContext, TrustLevel
        from src.graph.graph import Graph

        _patch_domain_emit(monkeypatch)

        def invoke_with(max_tokens):
            agent = Graph(config={"llm": {"max_tokens": max_tokens}})
            agent.compile()
            ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
            return agent.invoke("", ctx=ctx, input_context={"contract": _VALID_CONTRACT})

        generous = invoke_with(4000)["output"]
        tiny = invoke_with(10)["output"]  # 10 tokens x 4 = 40 characters per section
        assert "[section truncated" not in generous
        assert "[section truncated" in tiny
        assert len(tiny) < len(generous)

    def test_string_channel_still_accepted(self, monkeypatch):
        """The legacy `input` string channel remains a supported entry path."""
        from framework.schemas.agent_status import AgentStatus
        from framework.schemas.invocation_context import InvocationContext, TrustLevel
        from src.graph.graph import Graph
        from src.runtime_config import load_runtime_config

        _patch_domain_emit(monkeypatch)
        agent = Graph(config=load_runtime_config())
        agent.compile()
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        result = agent.invoke(_VALID_PAYLOAD, ctx=ctx)
        assert result.get("status") == AgentStatus.SUCCESS.value
        assert "FIN-CTR-20260712-001" in result["output"]
