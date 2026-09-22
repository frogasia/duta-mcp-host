"""duta-mcp-host gateway.

Mounts independent MCP-hosting sub-apps behind a single uvicorn process,
drives their lifespans concurrently under one shared structlog pipeline,
and tags every per-request log line with the owning sub-app.

Adding a new sub-app:
    1. Import its ``create_app`` factory.
    2. Append a ``SubApp(...)`` entry to ``SUB_APPS``.
Nothing else in this module should need to change.

geneco-mcp mounts twice, once per backend it fronts, so a tenant onboarded
to D365 weeks before the self-service portal (or the other way round) has
one connector to register now and the other later. Same codebase and same
database both times — only the tool set and the credential-intake body
differ, picked by ``kind`` (see ``geneco_mcp/main.py``).
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

import structlog
from duta_mcp_shared.observability import configure_logging
from geneco_mcp.config import Settings as GenecoMcpSettings
from geneco_mcp.main import create_app as create_geneco_mcp
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

GATEWAY_SERVICE = "duta-mcp-host"


@dataclass(frozen=True)
class SubApp:
    """An MCP-hosting FastAPI app mounted under the gateway.

    ``name`` is the service tag attached to every log line emitted while
    handling a request that matches ``path_prefix``.
    """

    name: str
    path_prefix: str
    app: FastAPI


SUB_APPS: tuple[SubApp, ...] = (
    SubApp(
        name="geneco-crm-mcp",
        path_prefix="/geneco-crm-mcp",
        app=create_geneco_mcp(kind="crm"),
    ),
    SubApp(
        name="geneco-ssp-mcp",
        path_prefix="/geneco-ssp-mcp",
        app=create_geneco_mcp(kind="ssp"),
    ),
)


def _service_for_path(path: str) -> str:
    for sub in SUB_APPS:
        if path.startswith(sub.path_prefix):
            return sub.name
    return GATEWAY_SERVICE


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Configure the process-wide structured logging pipeline BEFORE any
    # sub-app lifespan runs. Sub-apps share the same pipeline.
    geneco_mcp_settings = GenecoMcpSettings()
    configure_logging(
        log_level=geneco_mcp_settings.log_level,
        log_format=geneco_mcp_settings.log_format,  # type: ignore[arg-type]
        service=GATEWAY_SERVICE,
        env=geneco_mcp_settings.env,
    )

    # Starlette does not propagate lifespan events to mounted sub-apps.
    # Drive each sub-app's lifespan explicitly; gather so any slow startup
    # overlaps with the rest.
    async with AsyncExitStack() as stack:
        await asyncio.gather(
            *(
                stack.enter_async_context(sub.app.router.lifespan_context(sub.app))
                for sub in SUB_APPS
            )
        )
        yield


app = FastAPI(title="duta-mcp-host Gateway", lifespan=lifespan)


@app.middleware("http")
async def bind_service_context(request: Request, call_next):
    """Tag per-request logs with the owning sub-app derived from ``SUB_APPS``."""
    service = _service_for_path(request.scope.get("path", "") or "")
    structlog.contextvars.bind_contextvars(service=service)
    try:
        return await call_next(request)
    finally:
        structlog.contextvars.unbind_contextvars("service")


for _sub in SUB_APPS:
    app.mount(_sub.path_prefix, _sub.app)


@app.get("/")
async def root() -> JSONResponse:
    return JSONResponse({"success": True, "mounted": [s.path_prefix for s in SUB_APPS]})


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})
