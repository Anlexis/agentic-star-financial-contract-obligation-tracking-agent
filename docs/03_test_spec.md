# Test Specification — FIN-C2-077 Financial Contract Review & Obligation Tracking Agent

## 1. Test Strategy

- **Agent:** FIN-C2-077 — Financial Contract Review & Obligation Tracking Agent
  (Cat 2, DocGeneration pattern, two-layer nested graph: outer `AgentBaseGraph`
  backbone + inner `DomainWorkflowGraph` `BaseGraph`).
- **Coverage target:** ≥ 90% of `src/nodes/` + `src/graph/` branches.
- **Test types:** Unit (per node + graph wiring) · Proof-of-Boundary (framework
  security/serialization contracts) · Backbone invoke (full `Graph().invoke()`).
- **Framework provisioning:** `framework` (agenticstar-agentcore) is installed by
  CI from the package registry as the published wheel. Tests import the real
  modules; there are no stub nodes.
- **S-4 audit:** `emit_trace_event` is patched at the node module level in unit
  tests to avoid audit-backend calls, never via a `sys.modules` stub (which would
  break the real `shared` package the framework loads at import time).

### Test file map

| File | Scope |
|------|-------|
| `tests/unit/test_nodes.py` | All 7 domain/backbone nodes + outer & inner graph wiring |
| `tests/unit/test_validation.py` | Caller-field bounds: non-finite matrix, control characters, identifier form, list caps |
| `tests/unit/test_injection_screen.py` | Control-token class, directives, markup splicing, key/value walk, false-positive probes |
| `tests/unit/test_adapter_context.py` | Adapter `input_context` credential screen + the detector-parity property |
| `tests/unit/test_framework_compliance_tc06_tc07.py` | TC-06/TC-07 framework-compliance checks (central scaffold file) |
| `tests/integration/test_invoke_e2e.py` | End-to-end behaviour through the REAL ASGI `POST /invoke` |
| `tests/proof_of_boundary/test_pb_invoke_order.py` | PB-6 per-node + backbone invoke order (VERIFIED_EXTERNAL) + S-1 gate + payload alignment + masking |
| `tests/proof_of_boundary/test_import_isolation.py` | PB-4 Level-0 import isolation (AST scan) |
| `tests/proof_of_boundary/test_state_safety.py` | PB-2/PB-5 State msgpack/credential safety (AST scan) |
| `tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py` | PB-7 HITL interrupt-propagation (skip stub — no cross-boundary HITL) |

### Canonical valid request (PB-6 `_VALID_REQUEST`)

The escalation-required financial contract used by the backbone invoke test and by
`deploy/invoke_payload.json` (the two MUST stay identical — asserted by
`test_invoke_payload_matches_pb6`). **The contract travels on `input_context`, not in `input`:**
the framework's PII mask rewrites the string channel, and asserting the unmasked path in the
suite while the sign-off invoke exercised the masked one would prove nothing about deployment.

```json
{
  "input": "",
  "session_id": "stg-signoff-fin-c2-077-001",
  "input_context": {
    "contract": {
      "contract_id": "FIN-CTR-20260712-001",
      "contract_type": "Master Services Agreement",
      "parties": [
        {"name": "Sumitomo Mitsui Banking Corporation", "role": "client"},
        {"name": "FPT Software Japan K.K.", "role": "vendor"}
      ],
      "effective_date": "2026-04-01",
      "expiry_date": "2029-04-01",
      "governing_law": "Japan",
      "contract_value": {"amount": 82000000, "currency": "JPY"},
      "clauses": ["termination", "liability", "confidentiality", "governing law", "payment terms", "dispute resolution", "indemnity"],
      "obligations": [
        {"description": "Deliver quarterly SLA compliance report", "responsible_party": "FPT Software Japan K.K.", "due_date": "2026-06-30", "type": "reporting"},
        {"description": "Complete annual SOC 2 Type II audit", "responsible_party": "FPT Software Japan K.K.", "due_date": "2026-09-30", "type": "audit"}
      ]
    }
  }
}
```

Escalation: contract value (82,000,000 JPY) ≥ escalation threshold (10,000,000)
**AND** ≥ high-value threshold (50,000,000) ⇒ `risk_tier="high"` ⇒
`escalation_required=True`. All six mandatory clauses are covered after
normalisation (termination, liability, confidentiality, governing_law,
payment_terms, dispute_resolution).

## 2. Framework Compliance Tests (Mandatory)

| TC-ID | Test | Expected Result | Where |
|-------|------|----------------|-------|
| TC-01 | State contract: flat `TypedDict`, domain fields `NotRequired`, JSON-string (ADR-005) for dict/list fields, no Pydantic/dataclass | AST scan: 0 violations | `test_state_safety.py` |
| TC-02 | Invalid/empty/non-JSON input rejected at PreProcessNode | `status=error`, error_log populated | `TestPreProcessNode` |
| TC-03 | No JWT/credential in State | CI `gate-credential-scan`: 0 violations | CI + `test_state_safety.py` |
| TC-04 | `execute(self, state)` contract — no `_invoke_impl` | Signature `(self, state)`, `_invoke_impl` absent | `test_execute_signature_is_state_first` |
| TC-05 | S-4: `emit_trace_event()` called inside each node `execute()` | ≥1 domain event per node (positional form) | verified by CoE preflight S-4/#3 |
| TC-08 | S-1: `required_trust_level` enforced in `__call__` before `execute()` | ANONYMOUS caller → refused; VERIFIED_EXTERNAL → admitted | `TestS1TrustGate` |
| TC-08a | Outer `PreProcessNode` = VERIFIED_EXTERNAL; inner nodes + post_process = ANONYMOUS | trust levels asserted per node | `test_trust_level_*` |
| TC-11 | S-3 output gate on post_process | credential pattern → redacted + `status=error`; clean → pass | `TestPostProcessNode` |

## 3. Proof-of-Boundary Tests (Mandatory)

| PB-ID | Boundary | Test | Expected Result | Where |
|-------|----------|------|----------------|-------|
| PB-2 | State serialization | AST scan of `src/schemas/state.py` | primitives / JSON-string only; no Pydantic/dataclass | `test_state_safety.py` |
| PB-4 | Import isolation | AST scan of `src/` | 0 Level-0 (`agenticstar` / platform) imports | `test_import_isolation.py` |
| PB-5 | Checkpoint safety | no credential-named fields / prohibited types in State | inspection pass | `test_state_safety.py` |
| PB-6 | Invoke execution order (per node) | `__call__`: S-4 node_start → S-1 gate → S-2 input gate → `execute()` → S-3 output gate → S-4 node_complete | order verified for every `src/nodes/` class | `TestInvokeOrder` |
| PB-6b | Backbone invoke order | full `Graph().invoke(_VALID_PAYLOAD, ctx=VERIFIED_EXTERNAL)` | `status=success`; node_history = `[Initialize, PreProcess, ContractReviewGraphNode, PostProcess, Finalize]` | `TestBackboneInvokeOrder` |
| PB-6c | Real external caller | `InvocationContext(caller_trust_level=VERIFIED_EXTERNAL)` — **never** `for_internal()` | inner ANONYMOUS nodes accept the passthrough trust; SUCCESS end-to-end | `TestBackboneInvokeOrder` |
| PB-6d | Payload alignment | `deploy/invoke_payload.json == _VALID_REQUEST` (body AND channel) | Stage-5 deploy-stg invoke exercises the PB-6 request | `test_invoke_payload_matches_pb6` |
| PB-6e | Masking does not reach the report | full invoke on the context channel | no `[MASKED]`; the caller's party names and contract type appear verbatim | `test_masking_does_not_reach_the_report` |
| PB-6f | Legacy string channel | `agent.invoke(json_string, ctx=VERIFIED_EXTERNAL)` | still SUCCESS — the `input` channel remains supported | `test_string_channel_still_accepted` |
| PB-7 | HITL interrupt propagation | skip stub — `propagate_hitl=False`, no cross-boundary interrupt() checkpoint | skipped with reason (real assertion when HITL wired) | `test_pb7_hitl_interrupt_propagation.py` |

## 3-bis. End-to-end tests through the real ASGI entry point

`tests/integration/test_invoke_e2e.py` drives `src/api/server.py` through its ASGI interface at
the declared VERIFIED_EXTERNAL trust level, with `INVOKE_AUTH_TOKEN` set and a Bearer header —
the same credential `stg_invoke_evidence.py` presents for a non-INTERNAL entry contract.

| E2E-ID | Test | Expected |
|--------|------|----------|
| E2E-01 | Missing / wrong Bearer token | 401 with the generic body |
| E2E-02 | Contract on `input_context` | 200; report contains the contract id and the caller's value |
| E2E-03 | Masking check | no `[MASKED]` anywhere in the report |
| E2E-04 | Determination moves with input | 82,000,000 JPY / 36-month → escalation YES; 500,000 JPY / 6-month → escalation NO |
| E2E-05 | Missing mandatory clause | escalation YES, missing clause named |
| E2E-06 | Non-finite matrix on `contract_value.amount` | `NaN`, `±Infinity`, `"NaN"`, `"Infinity"`, `true`, `-1`, `1e18`, non-numeric → `status=error`, nothing released |
| E2E-07 | Valid amount on the same field | 200, figure rendered |
| E2E-08 | Control characters in a party name | refused; no report emitted |
| E2E-09 | Genuine report structure | exactly one `COMPLIANCE DETERMINATION`, exactly one `Escalation Required:` |
| E2E-10 | Injection payloads (`<\|im_start\|>`, `[INST]`, `<<SYS>>`, directive) | refused, nothing released |
| E2E-11 | Ordinary language containing the same words | 200, text rendered unchanged |
| E2E-12 | Credential-shaped `input_context` field | 400 naming the field, value never echoed |
| E2E-13 | Oversized `input_context` | 400 at the adapter |
| E2E-14 | Error-envelope containment | no report text, no traceback, no source paths; `contract_review` / `review_sections` / `compliance_flags` all null |

## 4. Business Logic Tests

| BL-ID | Test | Input | Expected Result | Where |
|-------|------|-------|----------------|-------|
| BL-01 | Happy-path report generation | `_VALID_PAYLOAD` | 7-section review report; `FINANCIAL CONTRACT REVIEW & OBLIGATION TRACKING REPORT` + contract_id present | `test_backbone_invoke_succeeds_and_returns_output`, `TestInnerDomainGraph` |
| BL-02 | Clause-name normalisation | `["governing law","nda","arbitration","liability cap"]` | `["governing_law","confidentiality","dispute_resolution","liability"]` (canonicalised + deduped) | `test_clause_aliases_are_normalised` |
| BL-03 | Party normalisation | mixed string + dict parties | `[{name, role}]` with default role `party` | `test_parties_normalised_to_name_role` |
| BL-04 | Risk-tier classification | value/term matrix | high / medium / low per value (50M/10M) + term (36/12 mo) thresholds | `TestParseContractDataNode` |
| BL-05 | Mandatory-clause coverage | full vs partial clause set | `mandatory_clauses_ok` + `missing_mandatory_clauses` correct | `TestParseContractDataNode`, `TestComplianceCheckNode` |
| BL-06 | Escalation — missing clause | clauses = `["termination"]` | `escalation_required=True`, coverage incomplete | `test_escalation_by_missing_clause` |
| BL-07 | Escalation — value threshold | value = 15,000,000, risk low | `escalation_required=True` (value_threshold_met) | `test_escalation_by_value_threshold` |
| BL-08 | Escalation — high risk tier | risk_tier = high | `escalation_required=True` | `test_escalation_by_high_risk` |
| BL-09 | No escalation (clean) | all clauses, value 5,000,000, risk low | `escalation_required=False` | `test_no_escalation_when_clean` |
| BL-10 | Report section assembly | 7 rendered sections + compliance flags | all 7 headers + `COMPLIANCE DETERMINATION`; Escalation YES/NO reflects flag | `TestOutputFormatNode` |
| BL-11 | Graph key coupling | inner `get_output` ↔ outer `merge_output` | 5 coupled keys mapped; `merge_output` returns changed keys only | `TestOuterGraphComposition`, `TestInnerDomainGraph` |

### Negative / boundary cases

| Case | Node | Expected |
|------|------|----------|
| empty `user_input` | PreProcessNode | `status=error`, "empty" |
| invalid JSON | PreProcessNode | `status=error`, "invalid JSON" |
| JSON root not an object | PreProcessNode | `status=error`, "object" |
| missing `clauses` | PreProcessNode | `status=error`, "clauses" |
| empty `contract_id` | InputValidateNode | `status=error`, "contract_id" |
| empty `parties` | InputValidateNode | `status=error`, "parties" |
| empty `clauses` | InputValidateNode | `status=error`, "clauses" |
| missing `contract_data` | Parse / Generate / Compliance | `status=error` |
| missing `review_sections` | OutputFormatNode | `status=error` |
| empty `contract_review` | PostProcessNode | fallback message, `status=success` |
| credential leak in output | PostProcessNode | report withheld, `status=error`, every output-bearing field cleared (S-3) |
| `contract_value.amount` = NaN / ±Infinity / bool / out of range | InputValidateNode | `status=error`, field named, value never echoed |
| control character in any rendered string | PreProcessNode | `status=error`, field named |
| >50 parties / >50 clauses / >200 obligations | PreProcessNode | `status=error`, field named |
| injection signature in any caller field or key | PreProcessNode `_extra_security_gate_input` | `status=error`, path + signature class named |
| credential-shaped `input_context` field | adapter | HTTP 400, field named, value never echoed |

## 5. Test Execution Summary

- Execution: `pytest tests/` against the installed framework wheel.
- Gates: `gate-dep-pinning`, `gate-stub-check`, `gate-cat-consistency`,
  `gate-audit-trace-check`, `gate-manifest-schema`, `gate-oss-license`,
  `gate-forbidden-strings`, `gate-credential-scan`, `gate-trust-level-check`,
  import-isolation, composition, invoke-chain, scaffold-integrity — all PASS.
- Coverage: node + graph modules exercised on both success and error paths;
  the adapter and the full pipeline exercised through the real ASGI entry point.
- PB-7 ships as a skip stub by design (no cross-boundary HITL propagation).
