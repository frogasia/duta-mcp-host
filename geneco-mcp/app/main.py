"""geneco-mcp: two MCP servers over one codebase, one per backend this
service talks to.

``geneco-crm-mcp``
    D365-backed tools — account inquiry, create case, case inquiry.
``geneco-ssp-mcp``
    Self-service-portal-backed tools — fee waiver, re-contract eligibility,
    fee reversal, password reset.

They are separate MCP endpoints rather than one, because their credentials
arrive at different times: a tenant is onboarded to one backend weeks
before the other, and the portal registers each connector only once its
credentials exist. Splitting at the URL is what lets those two be enabled
independently — see ``gateway.py`` for the mounts.

One app factory serves both. ``kind`` picks which tools the app exposes
(``tools/list`` *and* ``tools/call``, so an endpoint can never run a tool
it never advertised) and which credential-intake routes it mounts. The
code below them is shared and already split by backend: its own D365
connector and custom API client, each with its own encrypted credential
storage (``store/``, ``security/``, ``clients/``) — this service talks to
D365 and the SSP API directly, with no dependency on duta-ilmu's
integration-hub.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from geneco_mcp.config import Settings
from geneco_mcp.db import build_engine, build_session_factory
from geneco_mcp.protocol.admin_router import crm_router, ssp_router, validation_error_handler
from geneco_mcp.protocol.router import router
from geneco_mcp.store.crm_resolver import CrmCredentialCache
from geneco_mcp.store.ssp_resolver import SspCredentialCache
from geneco_mcp.tools import tools_for_kind
from geneco_mcp.tools.registry import ToolKind

#: The MCP ``serverInfo.name`` and OpenAPI title each kind introduces
#: itself with. Two endpoints reporting the same name during ``initialize``
#: would be indistinguishable to a client holding both registrations.
SERVER_NAMES: dict[ToolKind, str] = {
    "crm": "geneco-crm-mcp",
    "ssp": "geneco-ssp-mcp",
}


def create_app(settings: Settings | None = None, *, kind: ToolKind) -> FastAPI:
    cfg = settings if settings is not None else Settings()
    application = FastAPI(title=SERVER_NAMES[kind], docs_url="/api/docs")
    application.state.settings = cfg
    application.state.tool_kind = kind
    application.state.server_name = SERVER_NAMES[kind]
    # Filtered once at startup: the registry is populated at import and
    # never changes, so each app holds its own view of it rather than
    # re-filtering per request.
    application.state.tools = tools_for_kind(kind)
    application.state.tools_by_name = {spec.name: spec for spec in application.state.tools}
    engine = build_engine(cfg.database_url)
    application.state.engine = engine
    application.state.session_factory = build_session_factory(engine)
    application.state.crm_cache = CrmCredentialCache()
    application.state.ssp_cache = SspCredentialCache()
    # Keeps posted secrets out of 422 bodies — see the handler's docstring.
    application.add_exception_handler(RequestValidationError, validation_error_handler)
    # Only this kind's credential routes: both are mounted at
    # ``/{tenant_slug}/credentials``, so an app carrying both would be a
    # route collision, not a menu.
    application.include_router(crm_router if kind == "crm" else ssp_router)
    application.include_router(router)

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return application


def crm_app() -> FastAPI:
    """Import-string factory for ``geneco-crm-mcp`` (``run.py``, uvicorn
    ``--reload``). The gateway calls ``create_app`` directly instead."""
    return create_app(kind="crm")


def ssp_app() -> FastAPI:
    """Import-string factory for ``geneco-ssp-mcp`` — see ``crm_app``."""
    return create_app(kind="ssp")
