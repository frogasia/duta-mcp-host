"""ytllead-mcp: one MCP tool over the LEAD user-info API.

``POST /<any-segment>`` speaks the JSON-RPC subset duta-ilmu's MCP client
uses (``initialize``, ``notifications/initialized``, ``tools/list``,
``tools/call``). The segment is ignored so old ``/<tenant-slug>`` URLs keep
working; the bare mount root 307-redirects, so it can't be used.

The LEAD record is PII, so the user id is never a tool argument (the model
fills those from what the user types). It comes only from
``X-Verified-User-Token``, a 60s HS256 JWT the platform mints per call from
the verified sign-in. That token is the only gate; there is no API key.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import jwt
import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from ytllead_mcp.config import Settings
from ytllead_mcp.lead import LeadApiError, fetch_user_info
from ytllead_mcp.render import to_markdown

log = structlog.get_logger()

SERVER_NAME = "ytllead-mcp"
MAX_TOKEN_LIFETIME_S = 120  # platform mints 60s tokens
TOOL_NAME = "get_my_lead_profile"
TOOL = {
    "name": TOOL_NAME,
    "description": (
        "Get the signed-in user's own YTL LEAD conference profile as Markdown: "
        "personal details, attendance and registration progress, table number, "
        "their LEAD event (when and where), pre-conference tour selection, "
        "workshop team, and their personality profile with a suggested LEAD "
        "agenda. All times are Malaysia time (MYT). Takes no arguments — it always "
        "returns the record of the person signed in to this chat and cannot look "
        "up anyone else."
    ),
    "inputSchema": {"type": "object", "properties": {}},
}

NOT_AVAILABLE_ERROR = "LEAD profile lookup is not available for this workspace."
NOT_SIGNED_IN_ERROR = (
    "I can only look up your LEAD profile when you're signed in to the LEAD app. "
    "Please sign in and try again."
)
NOT_FOUND_ERROR = "I couldn't find a LEAD profile for your account."
UNAVAILABLE_ERROR = "I can't reach the LEAD system right now — please try again later."


def verified_user_id(request: Request, cfg: Settings) -> int | None:
    """The user id from ``X-Verified-User-Token``: an HS256 JWT minted by the
    platform per call, ``{"sub": "<id>", "iat", "exp"}``. Fails closed —
    no token, bad signature, expired, over-long lifetime or a malformed
    ``sub`` all mean "not signed in"."""
    token = request.headers.get("X-Verified-User-Token", "")
    try:
        claims = jwt.decode(
            token,
            cfg.identity_signing_secret,
            algorithms=["HS256"],
            leeway=30,
            options={"require": ["sub", "iat", "exp"]},
        )
    except jwt.PyJWTError:
        return None
    # A captured token must be short-lived even if the minter gets it wrong.
    iat, exp = claims["iat"], claims["exp"]
    if not (isinstance(iat, int | float) and isinstance(exp, int | float)):
        return None
    if exp - iat > MAX_TOKEN_LIFETIME_S or iat > time.time() + 30:
        return None
    sub = claims["sub"]
    if not (isinstance(sub, str) and sub.isascii() and sub.isdigit()):
        return None
    return int(sub) if int(sub) > 0 else None


async def get_my_lead_profile(settings: Settings, user_id: int | None) -> dict[str, Any]:
    if user_id is None:
        return {"error": NOT_SIGNED_IN_ERROR}
    try:
        data = await fetch_user_info(settings.lead_api_token, user_id)
    except LeadApiError as exc:
        log.warning("lead.user_info_failed", user_id=user_id, status_code=exc.status_code)
        return {"error": UNAVAILABLE_ERROR}
    if data is None:
        return {"error": NOT_FOUND_ERROR}
    if data.get("id") != user_id:  # never hand back someone else's record
        log.warning("lead.user_info_id_mismatch", user_id=user_id)
        return {"error": UNAVAILABLE_ERROR}
    return {"profile": data}


def _rpc(request_id: Any, result: dict[str, Any]) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})


def _rpc_error(request_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings if settings is not None else Settings()
    app = FastAPI(title=SERVER_NAME, docs_url="/api/docs")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/{_segment}")
    async def mcp(request: Request) -> Response:
        try:
            payload = await request.json()
        except ValueError:
            return _rpc_error(None, -32700, "parse error")
        if not isinstance(payload, dict):
            return _rpc_error(None, -32600, "invalid request")

        request_id = payload.get("id")
        method = payload.get("method")
        params = payload.get("params") or {}
        if not isinstance(params, dict):
            return _rpc_error(request_id, -32602, "invalid params")
        serves = cfg.is_configured

        if method == "initialize":
            resp = _rpc(
                request_id,
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": "0.1.0"},
                },
            )
            resp.headers["Mcp-Session-Id"] = uuid.uuid4().hex
            return resp
        if method == "notifications/initialized":
            return Response(status_code=202)
        if method == "tools/list":
            return _rpc(request_id, {"tools": [TOOL] if serves else []})
        if method == "tools/call":
            if not serves:
                result = {"error": NOT_AVAILABLE_ERROR}
            elif params.get("name") != TOOL_NAME:
                result = {"error": f"unknown tool: {params.get('name')!r}"}
            else:
                user_id = verified_user_id(request, cfg)
                result = await get_my_lead_profile(cfg, user_id)
            if "error" not in result:
                try:
                    result = {"text": to_markdown(result["profile"])}
                except Exception:  # LEAD has no schema: a shape change must not become a 500
                    log.exception("lead.render_failed", user_id=user_id)
                    result = {"error": UNAVAILABLE_ERROR}
            text = result.get("error") or result["text"]
            return _rpc(
                request_id,
                {"content": [{"type": "text", "text": text}], "isError": "error" in result},
            )

        log.info("mcp.unknown_method", method=method)
        return _rpc_error(request_id, -32601, f"method not found: {method}")

    return app
