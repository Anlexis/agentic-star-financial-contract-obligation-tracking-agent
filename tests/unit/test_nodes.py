# FIN-C2-077 — Unit Tests: domain nodes + graph wiring
#
# Real, non-stub unit tests. They import the REAL modules merged to develop in
# Wave-1 and assert real behaviour (contract-review report content, mandatory-
# clause + value/risk escalation thresholds, S-1 trust levels, the S-3 output
# gate, and the Cat 2 two-layer nested graph composition).
#
# S-4 audit events are patched at the node MODULE level (not via a sys.modules
# stub, which would break the real `shared` package the framework loads at import
# time). Patch pattern per node:
#     monkeypatch.setattr("src.nodes.<mod>.emit_trace_event", lambda *a, **k: None)

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from src.schemas.state import from_json, to_json


# ── Shared fixtures / helpers ─────────────────────────────────────────────────


def _contract_payload(**overrides) -> dict:
    """A complete, valid raw contract payload (as a caller would POST)."""
    payload = {
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
    payload.update(overrides)
    return payload


VALID_PAYLOAD = json.dumps(_contract_payload())

# Canonical normalised clause set (post InputValidateNode aliasing) — covers all
# six mandatory clauses plus one extra (indemnification).
_NORMALISED_CLAUSES = [
    "termination",
    "liability",
    "confidentiality",
    "governing_law",
    "payment_terms",
    "dispute_resolution",
    "indemnification",
]


def _contract_data(**overrides) -> dict:
    """The normalised + enriched contract_data dict shape produced by
    InputValidateNode (+ ParseContractDataNode enrichment) — i.e. the input the
    downstream inner nodes (generate / compliance / output) consume."""
    data = {
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
        "value_amount": 82000000,
        "currency": "JPY",
        "clauses": list(_NORMALISED_CLAUSES),
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
        "term_months": 36,
        "risk_tier": "high",
        "obligations_count": 2,
        "missing_mandatory_clauses": [],
        "mandatory_clauses_ok": True,
    }
    data.update(overrides)
    return data


# ── PreProcessNode (outer pre_process, S-1 VERIFIED_EXTERNAL) ──────────────────


class TestPreProcessNode:
    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        monkeypatch.setattr("src.nodes.pre_process_node.emit_trace_event", lambda *a, **k: None)

    def setup_method(self):
        from src.nodes.pre_process_node import PreProcessNode

        self.node = PreProcessNode()

    def test_valid_payload_returns_success(self):
        result = self.node.execute({"user_input": VALID_PAYLOAD, "input_context": {}})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["validated_input"] is not None
        assert json.loads(result["validated_input"])["contract_id"] == "FIN-CTR-20260712-001"

    def test_enriched_context_carries_contract_id(self):
        result = self.node.execute({"user_input": VALID_PAYLOAD, "input_context": {"channel": "legal"}})
        ctx = from_json(result["enriched_context"])
        assert ctx["contract_id"] == "FIN-CTR-20260712-001"
        assert ctx["channel"] == "legal"

    def test_empty_input_returns_error(self):
        result = self.node.execute({"user_input": "", "input_context": {}})
        assert result["status"] == AgentStatus.ERROR.value
        assert any("empty" in e for e in result["error_log"])

    def test_invalid_json_returns_error(self):
        result = self.node.execute({"user_input": "{not valid json}", "input_context": {}})
        assert result["status"] == AgentStatus.ERROR.value
        assert any("JSON" in e or "json" in e for e in result["error_log"])

    def test_non_object_json_returns_error(self):
        result = self.node.execute({"user_input": "[1, 2, 3]", "input_context": {}})
        assert result["status"] == AgentStatus.ERROR.value
        assert any("object" in e for e in result["error_log"])

    def test_missing_required_field_returns_error(self):
        payload = _contract_payload()
        del payload["clauses"]  # required field
        result = self.node.execute({"user_input": json.dumps(payload), "input_context": {}})
        assert result["status"] == AgentStatus.ERROR.value
        assert any("clauses" in e for e in result["error_log"])

    def test_trust_level_is_verified_external(self):
        assert self.node.required_trust_level == TrustLevel.VERIFIED_EXTERNAL

    def test_execute_signature_is_state_first(self):
        import inspect
        from src.nodes.pre_process_node import PreProcessNode

        params = list(inspect.signature(PreProcessNode.execute).parameters.keys())
        assert params[0] == "self" and params[1] == "state"
        assert "_invoke_impl" not in PreProcessNode.__dict__


# ── InputValidateNode (inner domain node 1, ANONYMOUS) ─────────────────────────


class TestInputValidateNode:
    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        monkeypatch.setattr("src.nodes.input_validate_node.emit_trace_event", lambda *a, **k: None)

    def setup_method(self):
        from src.nodes.input_validate_node import InputValidateNode

        self.node = InputValidateNode()

    def test_valid_input_builds_contract_data(self):
        result = self.node.execute({"validated_input": VALID_PAYLOAD})
        assert result["status"] == AgentStatus.SUCCESS.value
        data = from_json(result["contract_data"])
        assert data["contract_id"] == "FIN-CTR-20260712-001"
        assert data["value_amount"] == 82000000
        assert data["currency"] == "JPY"

    def test_clause_aliases_are_normalised(self):
        payload = _contract_payload(clauses=["governing law", "nda", "arbitration", "liability cap"])
        result = self.node.execute({"validated_input": json.dumps(payload)})
        data = from_json(result["contract_data"])
        # governing law -> governing_law, nda -> confidentiality,
        # arbitration -> dispute_resolution, liability cap -> liability
        assert data["clauses"] == ["governing_law", "confidentiality", "dispute_resolution", "liability"]

    def test_parties_normalised_to_name_role(self):
        payload = _contract_payload(parties=["Acme Bank Ltd", {"name": "FPT Software Japan K.K.", "role": "vendor"}])
        result = self.node.execute({"validated_input": json.dumps(payload)})
        data = from_json(result["contract_data"])
        assert data["parties"] == [
            {"name": "Acme Bank Ltd", "role": "party"},
            {"name": "FPT Software Japan K.K.", "role": "vendor"},
        ]

    def test_falls_back_to_user_input(self):
        result = self.node.execute({"user_input": VALID_PAYLOAD})
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_empty_contract_id_returns_error(self):
        payload = _contract_payload(contract_id="")
        result = self.node.execute({"validated_input": json.dumps(payload)})
        assert result["status"] == AgentStatus.ERROR.value
        assert any("contract_id" in e for e in result["error_log"])

    def test_empty_parties_returns_error(self):
        payload = _contract_payload(parties=[])
        result = self.node.execute({"validated_input": json.dumps(payload)})
        assert result["status"] == AgentStatus.ERROR.value
        assert any("parties" in e for e in result["error_log"])

    def test_empty_clauses_returns_error(self):
        payload = _contract_payload(clauses=[])
        result = self.node.execute({"validated_input": json.dumps(payload)})
        assert result["status"] == AgentStatus.ERROR.value
        assert any("clauses" in e for e in result["error_log"])

    def test_scalar_contract_value_is_extracted(self):
        payload = _contract_payload(contract_value=5000000)
        result = self.node.execute({"validated_input": json.dumps(payload)})
        data = from_json(result["contract_data"])
        assert data["value_amount"] == 5000000
        assert data["currency"] == "JPY"

    def test_trust_level_anonymous(self):
        assert self.node.required_trust_level == TrustLevel.ANONYMOUS


# ── ParseContractDataNode (inner domain node 2, ANONYMOUS) ─────────────────────


class TestParseContractDataNode:
    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        monkeypatch.setattr("src.nodes.parse_contract_data_node.emit_trace_event", lambda *a, **k: None)

    def setup_method(self):
        from src.nodes.parse_contract_data_node import ParseContractDataNode

        self.node = ParseContractDataNode()

    def _run(self, **overrides):
        state = {"contract_data": to_json(_contract_data(**overrides))}
        return self.node.execute(state)

    def test_high_risk_by_value(self):
        data = from_json(
            self._run(
                value_amount=60000000,
                effective_date="2026-04-01",
                expiry_date="2026-07-01",
            )["contract_data"]
        )
        assert data["risk_tier"] == "high"

    def test_high_risk_by_term(self):
        data = from_json(
            self._run(
                value_amount=1000000,
                effective_date="2026-01-01",
                expiry_date="2029-06-01",
            )["contract_data"]
        )
        assert data["risk_tier"] == "high"  # 41-month term >= 36

    def test_medium_risk_by_value(self):
        data = from_json(
            self._run(
                value_amount=12000000,
                effective_date="2026-04-01",
                expiry_date="2026-07-01",
            )["contract_data"]
        )
        assert data["risk_tier"] == "medium"

    def test_low_risk(self):
        data = from_json(
            self._run(
                value_amount=1000000,
                effective_date=None,
                expiry_date=None,
            )["contract_data"]
        )
        assert data["risk_tier"] == "low"

    def test_term_months_computed(self):
        data = from_json(self._run(effective_date="2026-04-01", expiry_date="2029-04-01")["contract_data"])
        assert data["term_months"] == 36

    def test_mandatory_clauses_ok_when_all_present(self):
        data = from_json(self._run()["contract_data"])
        assert data["mandatory_clauses_ok"] is True
        assert data["missing_mandatory_clauses"] == []

    def test_missing_mandatory_clauses_detected(self):
        data = from_json(self._run(clauses=["termination"])["contract_data"])
        assert data["mandatory_clauses_ok"] is False
        assert "liability" in data["missing_mandatory_clauses"]

    def test_missing_contract_data_returns_error(self):
        result = self.node.execute({})
        assert result["status"] == AgentStatus.ERROR.value

    def test_trust_level_anonymous(self):
        assert self.node.required_trust_level == TrustLevel.ANONYMOUS


# ── GenerateReviewSectionsNode (inner domain node 3, ANONYMOUS) ────────────────


class TestGenerateReviewSectionsNode:
    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        monkeypatch.setattr("src.nodes.generate_review_sections_node.emit_trace_event", lambda *a, **k: None)

    def setup_method(self):
        from src.nodes.generate_review_sections_node import GenerateReviewSectionsNode

        self.node = GenerateReviewSectionsNode()

    def test_generates_all_seven_sections(self):
        state = {"contract_data": to_json(_contract_data())}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        sections = from_json(result["review_sections"])
        assert set(sections.keys()) == {
            "review_summary",
            "contract_parties",
            "key_terms",
            "obligations_tracking",
            "risk_assessment",
            "compliance_findings",
            "recommended_actions",
        }

    def test_summary_reflects_contract_fields(self):
        state = {"contract_data": to_json(_contract_data())}
        sections = from_json(self.node.execute(state)["review_sections"])
        assert "FIN-CTR-20260712-001" in sections["review_summary"]
        assert "Sumitomo Mitsui Banking Corporation" in sections["contract_parties"]

    def test_recommended_actions_flag_missing_clause(self):
        data = _contract_data(missing_mandatory_clauses=["payment_terms"], mandatory_clauses_ok=False)
        sections = from_json(self.node.execute({"contract_data": to_json(data)})["review_sections"])
        assert "payment_terms" in sections["recommended_actions"]

    def test_reads_declared_llm_settings_from_seeded_context(self):
        # The declared llm block is seeded onto the inner graph's input_context
        # by DomainWorkflowGraph._extra_initial_state(). It is NOT read from an
        # `execute(state, config=...)` argument: BaseNode.__call__ calls
        # execute(state) with one argument, so such a parameter is never
        # supplied and every value read through it is dead.
        state = {
            "contract_data": to_json(_contract_data()),
            "input_context": {"llm": {"system_prompt_template": "prompts/contract_review.j2"}},
        }
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert set(from_json(result["review_sections"]).keys()) == {
            "review_summary",
            "contract_parties",
            "key_terms",
            "obligations_tracking",
            "risk_assessment",
            "compliance_findings",
            "recommended_actions",
        }

    def test_declared_max_tokens_is_an_observable_render_budget(self):
        """A declared value must CHANGE behaviour, or it is decorative."""
        data = to_json(_contract_data())
        generous = self.node.execute(
            {
                "contract_data": data,
                "input_context": {"llm": {"max_tokens": 4000}},
            }
        )
        tiny = self.node.execute(
            {
                "contract_data": data,
                "input_context": {"llm": {"max_tokens": 10}},  # 10 * 4 = 40 chars
            }
        )
        generous_sections = from_json(generous["review_sections"])
        tiny_sections = from_json(tiny["review_sections"])
        assert len(generous_sections["review_summary"]) > 40
        assert tiny_sections["review_summary"].startswith(generous_sections["review_summary"][:40])
        assert "[section truncated" in tiny_sections["review_summary"]

    def test_missing_contract_data_returns_error(self):
        result = self.node.execute({})
        assert result["status"] == AgentStatus.ERROR.value

    def test_trust_level_anonymous(self):
        assert self.node.required_trust_level == TrustLevel.ANONYMOUS


# ── ComplianceCheckNode (inner domain node 4, ANONYMOUS) ───────────────────────


class TestComplianceCheckNode:
    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        monkeypatch.setattr("src.nodes.compliance_check_node.emit_trace_event", lambda *a, **k: None)

    def setup_method(self):
        from src.nodes.compliance_check_node import ComplianceCheckNode

        self.node = ComplianceCheckNode()

    def test_escalation_by_missing_clause(self):
        state = {
            "contract_data": to_json(
                _contract_data(
                    clauses=["termination"],
                    value_amount=1000000,
                    risk_tier="low",
                )
            )
        }
        result = self.node.execute(state)
        assert result["escalation_required"] is True
        flags = from_json(result["compliance_flags"])
        assert flags["mandatory_coverage_complete"] is False
        assert "liability" in flags["missing_clauses"]

    def test_escalation_by_value_threshold(self):
        state = {"contract_data": to_json(_contract_data(value_amount=15000000, risk_tier="low"))}
        result = self.node.execute(state)
        assert result["escalation_required"] is True
        flags = from_json(result["compliance_flags"])
        assert flags["value_threshold_met"] is True
        assert flags["mandatory_coverage_complete"] is True

    def test_escalation_by_high_risk(self):
        state = {"contract_data": to_json(_contract_data(value_amount=1000000, risk_tier="high"))}
        result = self.node.execute(state)
        assert result["escalation_required"] is True

    def test_no_escalation_when_clean(self):
        state = {"contract_data": to_json(_contract_data(value_amount=5000000, risk_tier="low"))}
        result = self.node.execute(state)
        assert result["escalation_required"] is False
        flags = from_json(result["compliance_flags"])
        assert flags["mandatory_coverage_complete"] is True
        assert flags["value_threshold_met"] is False

    def test_compliance_flags_carry_thresholds(self):
        state = {"contract_data": to_json(_contract_data())}
        flags = from_json(self.node.execute(state)["compliance_flags"])
        assert flags["value_threshold"] == 10000000
        assert "Financial Contract Review Policy" in flags["policy_basis"]
        assert flags["mandatory_clauses"] == sorted(flags["mandatory_clauses"])

    def test_missing_contract_data_returns_error(self):
        result = self.node.execute({})
        assert result["status"] == AgentStatus.ERROR.value

    def test_trust_level_anonymous(self):
        assert self.node.required_trust_level == TrustLevel.ANONYMOUS


# ── OutputFormatNode (inner domain node 5, ANONYMOUS) ──────────────────────────


class TestOutputFormatNode:
    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        monkeypatch.setattr("src.nodes.output_format_node.emit_trace_event", lambda *a, **k: None)

    def setup_method(self):
        from src.nodes.output_format_node import OutputFormatNode

        self.node = OutputFormatNode()

    def _state(self, escalation_required=True):
        sections = {
            "review_summary": "Contract ID:   FIN-CTR-20260712-001",
            "contract_parties": "  - Sumitomo Mitsui Banking Corporation (client)",
            "key_terms": "  Term:            36 months",
            "obligations_tracking": "  1. [reporting] Deliver quarterly SLA compliance report",
            "risk_assessment": "  Overall Risk Tier: HIGH",
            "compliance_findings": "  Mandatory Coverage: COMPLETE",
            "recommended_actions": "  1. Route to senior legal counsel for high-risk review.",
        }
        compliance = {
            "escalation_required": escalation_required,
            "reason": "Contract value (82,000,000) >= escalation threshold (10,000,000); Risk tier is HIGH",
            "policy_basis": "Financial Contract Review Policy — mandatory-clause + value/risk escalation",
            "missing_clauses": [],
        }
        return {
            "review_sections": to_json(sections),
            "compliance_flags": to_json(compliance),
            "contract_data": to_json(_contract_data()),
        }

    def test_assembles_full_report(self):
        result = self.node.execute(self._state())
        assert result["status"] == AgentStatus.SUCCESS.value
        report = result["contract_review"]
        assert result["result"] == report
        assert "FINANCIAL CONTRACT REVIEW & OBLIGATION TRACKING REPORT" in report
        for header in (
            "1. Review Summary",
            "2. Contract Parties",
            "3. Key Terms",
            "4. Obligations Tracking",
            "5. Risk Assessment",
            "6. Compliance Findings",
            "7. Recommended Actions",
            "COMPLIANCE DETERMINATION",
        ):
            assert header in report, f"missing section header: {header}"

    def test_escalation_required_note_present(self):
        report = self.node.execute(self._state(escalation_required=True))["contract_review"]
        assert "Escalation Required:   YES" in report

    def test_no_escalation_note(self):
        report = self.node.execute(self._state(escalation_required=False))["contract_review"]
        assert "Escalation Required:   NO" in report

    def test_missing_sections_returns_error(self):
        result = self.node.execute({"contract_data": to_json(_contract_data())})
        assert result["status"] == AgentStatus.ERROR.value

    def test_trust_level_anonymous(self):
        assert self.node.required_trust_level == TrustLevel.ANONYMOUS


# ── PostProcessNode (outer post_process, S-3 output gate, ANONYMOUS) ───────────


class TestPostProcessNode:
    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)

    def setup_method(self):
        from src.nodes.post_process_node import PostProcessNode

        self.node = PostProcessNode()

    def test_clean_report_passes_gate(self):
        report = "FINANCIAL CONTRACT REVIEW & OBLIGATION TRACKING REPORT\nContract ID: FIN-CTR-1\nAll clear."
        result = self.node.execute({"contract_review": report, "escalation_required": True})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["formatted_output"] == report
        assert result["result"] == report

    def test_empty_report_uses_fallback(self):
        result = self.node.execute({"contract_review": "", "escalation_required": False})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "No review content generated" in result["formatted_output"]

    def test_s3_gate_withholds_credential_leak(self):
        leaky = "FINANCIAL CONTRACT REVIEW\ntoken=sk-abcdefghij0123456789ABCDEF"
        result = self.node.execute({"contract_review": leaky, "escalation_required": True})
        assert result["status"] == AgentStatus.ERROR.value
        # Behaviour, not wording: nothing of the leaky report is released, and
        # the replacement is TRUTHY (a falsy formatted_output re-opens
        # get_output()'s `formatted_output or result` fallback).
        assert result["formatted_output"]
        assert "sk-abcdefghij0123456789ABCDEF" not in result["formatted_output"]
        assert "sk-abcdefghij0123456789ABCDEF" not in result["result"]
        assert result["formatted_output"] == result["result"]
        assert any("S-3" in e for e in result["error_log"])
        # The leaked value must not travel in the error reason either.
        assert not any("sk-abcdefghij" in e for e in result["error_log"])

    def test_s3_violation_clears_every_output_bearing_field(self):
        """Raising is not containment: get_output() falls back to state['result']."""
        leaky = "FINANCIAL CONTRACT REVIEW\ntoken=sk-abcdefghij0123456789ABCDEF"
        result = self.node.execute(
            {
                "contract_review": leaky,
                "review_sections": to_json({"review_summary": leaky}),
                "compliance_flags": to_json({"escalation_required": True}),
                "escalation_required": True,
            }
        )
        for field in ("contract_review", "review_sections", "compliance_flags"):
            assert field in result, f"{field} must be explicitly cleared, not merely absent"
            assert result[field] is None

    def test_output_gate_is_the_union_of_local_and_framework_detectors(self):
        """Wider is safe; narrower is a bypass.

        The local patterns catch credential ASSIGNMENTS, which the framework's
        format-based detector does not match at all. The framework detector
        catches AKIA / sk_live_ / database URIs, which the local patterns did
        not. A value the framework catches and this gate misses makes the
        framework's own @final S-3 gate raise inside post_process, and the
        wrapper then discards this node's clearing — a detector gap is a
        containment bypass, so the gate must be the union of both.
        """
        from framework.security.credential_detector import detect_credentials_in_value
        from src.nodes.post_process_node import evaluate_output_gate

        local_only = ["password = supersecret123", "api_key: abcdefgh12345678"]
        framework_only = [
            "AKIAIOSFODNN7EXAMPLE",
            "sk_live_" + "51H8xyzABCDEFGHIJKLMNOP",
            "postgresql://svc:dummypassword@db.internal:5432/prod",
        ]
        for value in local_only:
            assert not detect_credentials_in_value(value), (
                f"{value!r} is expected to be framework-invisible; if the framework "
                "now catches it, keep the local pattern anyway"
            )
            assert evaluate_output_gate(value) is not None
        for value in framework_only:
            assert detect_credentials_in_value(value)
            assert evaluate_output_gate(value) is not None
        for value in ["sk-abcdefghij0123456789ABCDEF", "Bearer abcdefgh12345678"]:
            assert evaluate_output_gate(value) is not None
        assert evaluate_output_gate("A perfectly clean contract review.") is None
        assert evaluate_output_gate("") is None

    def test_gate_does_not_fire_on_legitimate_contract_language(self):
        """Fail-closed screens must not refuse real work."""
        from src.nodes.post_process_node import evaluate_output_gate

        for clean in [
            "  Present Clauses: termination, liability, confidentiality",
            "  Determination Basis: Contract value (82,000,000) >= threshold (10,000,000)",
            "The parties shall exchange a token of good faith on signature.",
            "Governing Law:   Japan",
            "Password protection of the data room is the vendor's obligation.",
        ]:
            assert evaluate_output_gate(clean) is None, clean

    def test_trust_level_anonymous(self):
        assert self.node.required_trust_level == TrustLevel.ANONYMOUS


# ── Graph wiring: outer AgentBaseGraph + inner BaseGraph (Cat 2 nested) ─────────


class TestOuterGraphComposition:
    def test_registers_five_backbone_slots(self):
        from src.graph.graph import (
            ContractReviewGraphNode,
            FinancialContractReviewAgent,
        )
        from src.nodes.post_process_node import PostProcessNode
        from src.nodes.pre_process_node import PreProcessNode

        agent = FinancialContractReviewAgent()
        agent.compile()
        assert set(agent._nodes.keys()) == {
            "initialize",
            "pre_process",
            "main",
            "post_process",
            "finalize",
        }
        assert isinstance(agent._nodes["pre_process"], PreProcessNode)
        assert isinstance(agent._nodes["main"], ContractReviewGraphNode)
        assert isinstance(agent._nodes["post_process"], PostProcessNode)

    def test_name_and_state_schema(self):
        from src.schemas.state import State
        from src.graph.graph import FinancialContractReviewAgent

        agent = FinancialContractReviewAgent()
        assert agent.name == "FinancialContractReviewAgent"
        assert agent.state_schema is State

    def test_graph_alias_matches_real_class(self):
        from src.graph.graph import Graph, FinancialContractReviewAgent

        assert Graph is FinancialContractReviewAgent

    def test_main_slot_graphnode_contracts(self):
        from src.graph.graph import ContractReviewGraphNode

        node = ContractReviewGraphNode()
        assert node.error_strategy == "propagate"
        assert node.propagate_hitl is False
        # extract_input prefers validated_input, falls back to user_input
        assert node.extract_input({"validated_input": "V", "user_input": "U"}) == "V"
        assert node.extract_input({"user_input": "U"}) == "U"

    def test_merge_output_maps_subresult_keys(self):
        from src.graph.graph import ContractReviewGraphNode

        node = ContractReviewGraphNode()
        sub_result = {
            "contract_review": "REPORT",
            "review_sections": "{}",
            "compliance_flags": "{}",
            "escalation_required": True,
            "status": AgentStatus.SUCCESS.value,
            "node_history": ["x"],  # not forwarded by merge_output
        }
        delta = node.merge_output({}, sub_result)
        assert delta["contract_review"] == "REPORT"
        assert delta["escalation_required"] is True
        assert delta["status"] == AgentStatus.SUCCESS.value
        assert set(delta.keys()) == {
            "contract_review",
            "review_sections",
            "compliance_flags",
            "escalation_required",
            "status",
        }

    def test_parent_config_forwards_the_declared_llm_block(self):
        """_parent_config() reads config/config.yaml, not the retired manifest block.

        The manifest migration moved the `config:` block out of
        `config/agent.yaml`, and the previous reader still asked the manifest for
        an `agent.config` block that no longer exists — it would have returned
        `{}` on every invocation while the manifest, the suite and CI all stayed
        green.
        """
        from src.graph.graph import ContractReviewGraphNode

        cfgurable = ContractReviewGraphNode()._parent_config().get("configurable", {})
        assert cfgurable.get("system_prompt_template") == "prompts/contract_review.j2"
        assert cfgurable.get("temperature") == 0.0
        assert cfgurable.get("max_tokens") == 4000

    def test_parent_config_prefers_the_config_the_graph_was_constructed_with(self):
        """The platform passes config/config.yaml to Graph(config=...)."""
        from src.graph.graph import FinancialContractReviewAgent

        agent = FinancialContractReviewAgent(config={"llm": {"max_tokens": 123}})
        agent.compile()
        forwarded = agent._nodes["main"]._parent_config()["configurable"]
        assert forwarded["max_tokens"] == 123

    def test_s3_gate_enabled_false_fails_compile(self):
        """A config that turns the mandatory output gate off is a deployment error."""
        from framework.errors import ConfigError
        from src.graph.graph import FinancialContractReviewAgent

        agent = FinancialContractReviewAgent(config={"security": {"s3_gate_enabled": False}})
        with pytest.raises(ConfigError):
            agent.compile()


class TestInnerDomainGraph:
    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        for mod in (
            "input_validate_node",
            "parse_contract_data_node",
            "generate_review_sections_node",
            "compliance_check_node",
            "output_format_node",
        ):
            monkeypatch.setattr(f"src.nodes.{mod}.emit_trace_event", lambda *a, **k: None)

    def test_registers_five_domain_nodes(self):
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        g = DomainWorkflowGraph()
        g.register_nodes()
        assert set(g._nodes.keys()) == {
            "input_validate",
            "parse_contract_data",
            "generate_review_sections",
            "compliance_check",
            "output_format",
        }

    def test_name_and_state_schema(self):
        from src.schemas.state import State
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        g = DomainWorkflowGraph()
        assert g.name == "fin_c2_077_contract_review_workflow"
        assert g.state_schema is State

    def test_inner_graph_invoke_produces_report(self):
        """Standalone inner-graph invoke (ANONYMOUS caller) runs the linear pipeline
        and shapes the get_output() dict consumed by the outer merge_output()."""
        from framework.schemas.invocation_context import InvocationContext, TrustLevel
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        g = DomainWorkflowGraph()
        g.compile()
        ctx = InvocationContext(caller_trust_level=TrustLevel.ANONYMOUS)
        result = g.invoke(VALID_PAYLOAD, ctx=ctx)
        # The inner graph get_output IS the terminal output, so its keys surface directly.
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["contract_review"] is not None
        assert result["escalation_required"] is True
        assert "FINANCIAL CONTRACT REVIEW & OBLIGATION TRACKING REPORT" in result["contract_review"]


# ── SR #12 regression: outer get_output() surfaces the structured domain result ─
# Before the fix, FinancialContractReviewAgent had no get_output() override, so a
# compiled outer-graph agent.invoke() returned only the base {output, status,
# trace_id, correlation_id, node_history} envelope and DROPPED the domain keys
# added by ContractReviewGraphNode.merge_output() (contract_review,
# review_sections, compliance_flags, escalation_required). get_output() now
# surfaces them fail-closed (SUCCESS only; POST-S-3-gate values).


class TestOuterGetOutput:
    @pytest.fixture(autouse=True)
    def patch_emit(self, monkeypatch):
        for mod in (
            "pre_process_node",
            "input_validate_node",
            "parse_contract_data_node",
            "generate_review_sections_node",
            "compliance_check_node",
            "output_format_node",
            "post_process_node",
        ):
            monkeypatch.setattr(f"src.nodes.{mod}.emit_trace_event", lambda *a, **k: None)

    def _invoke(self):
        from framework.schemas.invocation_context import InvocationContext, TrustLevel
        from src.graph.graph import Graph

        agent = Graph()
        agent.compile()
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        return agent.invoke(VALID_PAYLOAD, ctx=ctx)

    def test_get_output_override_is_defined(self):
        from src.graph.graph import FinancialContractReviewAgent

        assert "get_output" in FinancialContractReviewAgent.__dict__

    def test_domain_result_surfaced_on_success(self):
        result = self._invoke()
        assert result["status"] == AgentStatus.SUCCESS.value
        # Structured domain keys (dropped by the base envelope) are now surfaced.
        assert result["contract_review"] is not None
        assert "FINANCIAL CONTRACT REVIEW & OBLIGATION TRACKING REPORT" in result["contract_review"]
        assert result["review_sections"] is not None
        assert result["compliance_flags"] is not None
        assert result["escalation_required"] is True
        # Caller-facing, POST-S-3-gate output preserved.
        assert result["formatted_output"] is not None
        # Base envelope preserved (regression guard).
        assert result["output"] is not None
        assert "FINANCIAL CONTRACT REVIEW & OBLIGATION TRACKING REPORT" in result["output"]
