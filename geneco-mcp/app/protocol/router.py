"""MCP JSON-RPC endpoint: ``POST /{tenant_slug}``.

Serves one *kind* of tool — whichever the app mounting it was built for
(``main.py``). The same module backs both ``geneco-crm-mcp`` and
``geneco-ssp-mcp``; everything kind-specific is read off ``app.state``
rather than the global registry, so neither endpoint can list or run the
other's tools.

Speaks exactly the subset duta-ilmu's orchestrator MCP client uses
(``services/orchestrator/src/orchestrator/tools/mcp.py`` in the duta-ilmu
repo): ``initialize`` -> ``notifications/initialized`` -> ``tools/list`` /
``tools/call``, JSON-RPC 2.0 over a single POST — that client only POSTs
and never opens a separate SSE stream, so no bidirectional Streamable-HTTP
support is needed here.

That client sends no auth headers when calling a tenant's MCP server (this
is a platform-wide convention in duta-ilmu, not something introduced
here — see ``control_plane/aggregation.py``: "Our MCP registrations never
carry auth headers"), so this endpoint is intentionally unauthenticated at
the transport level. The data-level gate is unchanged: every tool still
requires a correct ``account_id`` + ``mobile_number`` match before
returning anything, via integration-hub.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker

from geneco_mcp.config import Settings
from geneco_mcp.protocol.models import JsonRpcId, rpc_error, rpc_result
from geneco_mcp.store.credentials import CredentialStore
from geneco_mcp.store.crm_resolver import CrmCredentialCache
from geneco_mcp.store.ssp_resolver import SspCredentialCache
from geneco_mcp.store.custom_api_credentials import CustomApiCredentialStore
from geneco_mcp.tools.registry import ToolContext, ToolKind, ToolSpec

log = structlog.get_logger()

PROTOCOL_VERSION = "2025-03-26"
SERVER_VERSION = "0.1.0"
SESSION_HEADER = "Mcp-Session-Id"

router = APIRouter()


def _tool_defs(specs: tuple[ToolSpec, ...]) -> list[dict[str, Any]]:
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "inputSchema": spec.input_model.model_json_schema(),
        }
        for spec in specs
    ]


async def _is_configured(
    kind: ToolKind,
    tenant_slug: str,
    *,
    settings: Settings,
    session_factory: async_sessionmaker,
) -> bool:
    """Whether this tenant has registered the credential kind these tools
    need. A row's presence only — no vault hop, no outbound call."""
    async with session_factory() as session:
        if kind == "crm":
            return await CredentialStore(
                session, encryption_key=settings.credential_encryption_key
            ).is_registered(tenant_slug)
        return await CustomApiCredentialStore(
            session, encryption_key=settings.credential_encryption_key
        ).is_registered(tenant_slug)


async def _handle_tools_call(
    params: dict[str, Any],
    *,
    tenant_slug: str,
    settings: Settings,
    session_factory: async_sessionmaker,
    crm_cache: CrmCredentialCache,
    ssp_cache: SspCredentialCache,
    tools_by_name: dict[str, ToolSpec],
) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    # This app's tools only — never the global registry. An endpoint that
    # would run a tool it never advertised is a hole in the split, not a
    # convenience.
    spec = tools_by_name.get(name) if isinstance(name, str) else None
    if spec is None:
        return {"content": [{"type": "text", "text": f"unknown tool: {name!r}"}], "isError": True}
    try:
        parsed_args = spec.input_model.model_validate(arguments)
    except ValidationError as exc:
        return {
            "content": [{"type": "text", "text": f"invalid arguments for {name}: {exc}"}],
            "isError": True,
        }
    async with session_factory() as session:
        ctx = ToolContext(
            session=session,
            encryption_key=settings.credential_encryption_key,
            crm_cache=crm_cache,
            ssp_cache=ssp_cache,
        )
        result = await spec.handler(ctx, tenant_slug, parsed_args)
    return {
        "content": [{"type": "text", "text": json.dumps(result)}],
        "structuredContent": result,
        "isError": bool(result.get("error")),
    }


@router.post("/{tenant_slug}")
async def mcp_endpoint(tenant_slug: str, request: Request) -> Response:
    settings: Settings = request.app.state.settings
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse(rpc_error(None, -32700, "parse error"))

    if not isinstance(payload, dict):
        return JSONResponse(rpc_error(None, -32600, "invalid request"))

    request_id: JsonRpcId = payload.get("id")
    method = payload.get("method")
    params = payload.get("params") or {}

    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": request.app.state.server_name, "version": SERVER_VERSION},
        }
        return JSONResponse(
            rpc_result(request_id, result), headers={SESSION_HEADER: uuid.uuid4().hex}
        )

    if method == "notifications/initialized":
        # Notification: no id, no response body expected.
        return Response(status_code=202)

    if method == "tools/list":
        # Advertised only once the credentials behind them exist: a tenant
        # mid-onboarding would otherwise be offered tools whose every call
        # answers "not available for this workspace right now". An empty
        # list is the honest answer, and the list grows by itself the
        # moment stage-1 intake lands.
        configured = await _is_configured(
            request.app.state.tool_kind,
            tenant_slug,
            settings=settings,
            session_factory=request.app.state.session_factory,
        )
        tools = _tool_defs(request.app.state.tools) if configured else []
        return JSONResponse(rpc_result(request_id, {"tools": tools}))

    if method == "tools/call":
        result = await _handle_tools_call(
            params,
            tenant_slug=tenant_slug,
            settings=settings,
            session_factory=request.app.state.session_factory,
            crm_cache=request.app.state.crm_cache,
            ssp_cache=request.app.state.ssp_cache,
            tools_by_name=request.app.state.tools_by_name,
        )
        return JSONResponse(rpc_result(request_id, result))

    log.info("mcp.unknown_method", method=method, tenant_slug=tenant_slug)
    return JSONResponse(rpc_error(request_id, -32601, f"method not found: {method}"))
