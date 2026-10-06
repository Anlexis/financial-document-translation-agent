"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for the agent.
# Entry points are adapters only — no business logic here.
# For platform-level routing, AgentGateway calls agent.invoke() directly.
#
# Class-name contract (all three must match exactly):
#   graph.py defines:  class FinC2008Agent(AgentBaseGraph)
#   agent.yaml class:  "src.graph.graph.FinC2008Agent"
#   This file imports: FinC2008Agent

import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory
from src.graph.graph import FinC2008Agent

# Serialized size cap for the caller-metadata channel — enforced at the
# adapter so oversized payloads never reach the graph.
_MAX_INPUT_CONTEXT_BYTES = 256 * 1024


def _load_runtime_config() -> dict[str, Any]:
    """Load config/config.yaml — the runtime parameters (max_retry, timeout_s).

    The platform registry loads this file itself and passes it to the graph
    constructor; the standalone server must do the same, or the declared
    values silently never reach the graph.
    """
    config_path = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
    if not config_path.exists():
        return {}
    with open(config_path, encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh)
    return loaded if isinstance(loaded, dict) else {}


app = FastAPI(title="FIN-C2-008 FinancialDocumentTranslationAgent")

agent = FinC2008Agent(config=_load_runtime_config())
agent.compile()
agent.provision_secrets(secrets_factory(namespace="fin", agent_name="FinancialDocumentTranslationAgent"))


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""
    # Language pair — accepted both as top-level convenience fields and via
    # the structured input_context channel (input_context wins on overlap).
    source_language: str = "auto"
    target_language: str = "en"
    input_context: dict[str, Any] = Field(default_factory=dict)


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> dict[str, Any]:
    # The adapter enforces only the size cap on caller metadata; every
    # consumed field is validated field-by-field in PreProcessNode.
    merged_context: dict[str, Any] = {
        "source_language": req.source_language,
        "target_language": req.target_language,
    }
    merged_context.update(req.input_context)
    if len(json.dumps(merged_context, default=str).encode("utf-8")) > _MAX_INPUT_CONTEXT_BYTES:
        raise HTTPException(status_code=413, detail="input_context too large")

    with bound_secrets(agent._secrets_provider):
        # Trust promotion: callers that present a valid INVOKE_AUTH_TOKEN
        # Bearer credential are VERIFIED_EXTERNAL — the minimum trust required
        # by PreProcessNode. Unauthenticated requests remain ANONYMOUS.
        # When a higher-trust middleware (e.g. AgentGateway) already set
        # request.state.trust_level it takes precedence over the ANONYMOUS
        # default.
        _env_token = os.environ.get("INVOKE_AUTH_TOKEN", "")
        _auth_header = request.headers.get("Authorization", "")
        if _env_token and _auth_header == f"Bearer {_env_token}":
            trust = TrustLevel.VERIFIED_EXTERNAL
        else:
            trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)

        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        result = agent.invoke(
            req.input,
            ctx=ctx,
            input_context=merged_context,
        )
        return dict(result)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "agent": "FIN-C2-008"}
