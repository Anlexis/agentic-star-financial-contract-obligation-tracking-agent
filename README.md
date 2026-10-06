# Financial Contract & Obligation Tracking Agent

AI agent for reviewing financial contracts and tracking obligations, built with Agentic Star.

> **Category**: Cat 2 (domain-specific document-generation pipeline)
> **Industry**: Finance
> **Template ID**: FIN-C2-077

## Overview

Reviews a financial contract and produces a structured review-and-obligation-tracking report
for the legal or compliance analyst who submitted it.

You send the contract as structured data — parties, dates, clauses, obligations and value.
The agent normalises it (clause names arrive in a dozen spellings; "arbitration", "disputes"
and "dispute resolution" are one clause), classifies the contract's risk tier from its value
and term, checks it against a mandatory-clause policy, and decides whether it must be routed
to senior legal review. The output is a seven-section plain-text report: summary, parties,
key terms, tracked obligations, risk assessment, compliance findings and recommended actions,
followed by a compliance determination that states the escalation decision and the reason for
it. The escalation flag is also returned as a machine-readable field so a workflow can act on
it without parsing the report.

The policy that ships is a worked example — six mandatory clauses, a value threshold and a
risk tier — and it lives in the domain nodes, where you are meant to replace it with your own.

Every caller field is bounded before it is used: numbers must be finite and in range (a `NaN`
contract value is refused, not silently read as zero), strings cannot carry line breaks into a
report whose structure is line breaks, lists are capped, and the assembled report is withheld
if a credential-shaped string appears anywhere in it.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the SDK version does not match, the agent fails during
graph compile / start-up preflight rather than starting in a partially working state. This is
intentional — a half-running agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and operational documentation
```

See `docs/02_design.md` for the architecture and `docs/03_test_spec.md` for the test
specification — those are the two documents that ship with this repository.

## Customising

1. `config/agent.yaml` is the manifest (identity, entry class, trust level, declared
   secrets); `config/config.yaml` holds the runtime parameters the graph is constructed
   with. Adjust the second for your environment.
2. Replace the mandatory-clause set, the value threshold and the risk-tier bands in
   `src/nodes/compliance_check_node.py` and `src/nodes/parse_contract_data_node.py`
   with your own policy, and the clause aliases in `src/nodes/input_validate_node.py`
   with the vocabulary your contracts actually use.
3. Adjust the report sections in `src/nodes/generate_review_sections_node.py` and the
   document assembly in `src/nodes/output_format_node.py`.
4. Review the caller-data bounds in `src/validation.py` — list caps, string lengths and
   the contract-value range are deliberately conservative.
5. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
