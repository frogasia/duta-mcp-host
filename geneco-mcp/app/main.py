"""geneco-mcp: an MCP server hosting Geneco's D365-backed tools (account
inquiry / create case / case inquiry) and custom-API-backed tools (fee
waiver, re-contract eligibility, fee reversal, password reset) for the
duta-ilmu orchestrator's MCP client to consume.

This service owns the MCP *protocol* surface (``protocol/router.py``) and
its own D365 connector + custom API client, each with its own encrypted
credential storage (``store/``, ``security/``, ``clients/``) — it talks to
D365 and the custom API directly, with no dependency on duta-ilmu's
integration-hub.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from geneco_mcp.config import Settings
from geneco_mcp.db import build_engine, build_session_factory
from geneco_mcp.protocol.admin_router import router as admin_router
from geneco_mcp.protocol.admin_router import validation_error_handler
from geneco_mcp.protocol.router import router
from geneco_mcp.store.crm_resolver import CrmCredentialCache


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings if settings is not None else Settings()
    application = FastAPI(title="geneco-mcp", docs_url="/api/docs")
    application.state.settings = cfg
    engine = build_engine(cfg.database_url)
    application.state.engine = engine
    application.state.session_factory = build_session_factory(engine)
    application.state.crm_cache = CrmCredentialCache()
    # Keeps posted secrets out of 422 bodies — see the handler's docstring.
    application.add_exception_handler(RequestValidationError, validation_error_handler)
    application.include_router(admin_router)
    application.include_router(router)

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return application


app = create_app()
