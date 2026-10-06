"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for the agent.
# Entry points are adapters only — no business logic here.
# For platform-level routing, AgentGateway calls agent.invoke() directly.

import json
import os
import re
import secrets
from typing import Any, Dict
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from framework.security.credential_detector import detect_credentials_in_value
from shared.secrets import factory as secrets_factory
from src.graph.graph import Graph
from src.runtime_config import load_runtime_config

app = FastAPI(title="Agent")

# Construct the graph WITH its declared runtime parameters. `Graph()` with no
# config left every value in config/config.yaml dead on the standalone path
# (max_retry, the llm block) while the manifest still advertised them; the
# platform passes the same file to Graph(config=...), so both paths now agree.
agent = Graph(config=load_runtime_config())
agent.compile()
# Replace namespace/agent_name to match the agent's manifest values.
agent.provision_secrets(secrets_factory(namespace="fin-c2-077", agent_name="FinancialContractReviewAgent"))

# Adapter-level cap on the context channel. The platform adapter caps
# input_context at roughly this size; enforcing it here turns a truncation
# further down the stack into a clear 400 at the boundary.
_MAX_CONTEXT_BYTES = 262_144

# A context field NAME is caller data too. Echo it back only when it is inert.
_SAFE_FIELD_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class InvokeRequest(BaseModel):
    input: str = ""
    session_id: str = ""
    # Caller data travels here, not in `input`. The framework's S-2 filter masks
    # personal-data shapes in `input` before any template code runs, so a
    # contract submitted as a JSON string comes back with its parties rendered
    # as "[MASKED]" — the review would then certify the redaction sentinel as
    # contract fact. `input_context` is not rewritten.
    input_context: Dict[str, Any] = Field(default_factory=dict)


def _describe_field(name: object, index: int) -> str:
    """Name a context field in an error without echoing caller data.

    A field NAME can itself be hostile or credential-shaped, so it is quoted
    back only when it matches an inert pattern and trips no credential pattern
    of its own; otherwise the position is reported instead.
    """
    if isinstance(name, str) and _SAFE_FIELD_NAME.match(name) and not detect_credentials_in_value(name):
        return f"input_context.{name}"
    return f"input_context field #{index}"


def screen_input_context(context: Dict[str, Any]) -> None:
    """Refuse a credential-shaped `input_context` at the adapter, before invoke().

    `InitializeNode._setup()` returns `input_context` verbatim into its result,
    and the framework's @final S-3 gate scans every value of every result — so a
    credential-shaped string anywhere in the context makes the FIRST node return
    status=error with a traceback in error_log, before any template code runs.
    The request cannot succeed either way; refusing here converts an opaque
    node-1 failure into an actionable 400 that names the field.

    Fields are iterated one at a time rather than scanned as a whole dict. That
    is exactly equivalent — `detect_credentials_in_value(dict)` is defined as the
    union over its values — and the equivalence is what lets the error name the
    field without widening or narrowing the block set. It is pinned as a property
    test in tests/unit/test_adapter_context.py.

    400, not 422: pydantic owns 422 and answers there with a list of error
    objects, so reusing it would make client handling ambiguous.
    """
    encoded = len(json.dumps(context, default=str).encode("utf-8"))
    if encoded > _MAX_CONTEXT_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"input_context exceeds the {_MAX_CONTEXT_BYTES}-byte limit.",
        )
    for index, (name, value) in enumerate(context.items()):
        if detect_credentials_in_value(value):
            raise HTTPException(
                status_code=400,
                detail=(f"{_describe_field(name, index)} contains a credential-shaped " "value. Remove it and retry."),
            )


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> Dict[str, Any]:
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    # Standalone/STG caller auth ((internal issue reference removed), the staging runbook §5): when
    # INVOKE_AUTH_TOKEN is set on the server environment, callers that no upstream
    # middleware vouched for (still ANONYMOUS) must present it as a Bearer token
    # and run at VERIFIED_EXTERNAL. Middleware-established trust is never demoted.
    # This adapter is the entry-point auth boundary (standalone equivalent of
    # platform AuthMiddleware) — a deployment-level caller credential, not an
    # agent secret, so ctx.secrets does not apply (no InvocationContext exists
    # before auth); see the framework rules §3 "Entry-point exception".
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        # Compare bytes: compare_digest raises TypeError on non-ASCII str input
        # (headers decode as latin-1), which would 500 instead of the generic 401.
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            # Generic body on purpose — do not leak whether the token was absent,
            # malformed, or wrong.
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL

    screen_input_context(req.input_context)

    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        envelope: Dict[str, Any] = agent.invoke(req.input, ctx=ctx, input_context=dict(req.input_context))
        return envelope


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok", "agent": "FinancialContractReviewAgent"}
