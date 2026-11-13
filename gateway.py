"""duta-mcp-host gateway.

Mounts independent MCP-hosting sub-apps behind a single uvicorn process,
drives their lifespans concurrently under one shared structlog pipeline,
and tags every per-request log line with the owning sub-app.

Adding a new sub-app:
    1. Import its ``create_app`` factory.
    2. Append a ``SubApp(...)`` entry to ``SUB_APPS``.
Nothing else in this module should need to change.

Only ytllead-mcp is mounted. The geneco-mcp mounts are commented out
(detached, not deleted): uncomment its imports and ``SubApp`` entries, and
the matching lines in the Dockerfile, docker-entrypoint.sh,
docker-compose.yml, Makefile, requirements.txt and CI, to bring it back.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

import structlog
from duta_mcp_shared.observability import configure_logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from ytllead_mcp.config import Settings as YtlleadMcpSettings
from ytllead_mcp.main import create_app as create_ytllead_mcp

# geneco-mcp detached:
# from geneco_mcp.main import create_app as create_geneco_mcp

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
    # geneco-mcp detached:
    # SubApp(
    #     name="geneco-crm-mcp",
    #     path_prefix="/geneco-crm-mcp",
    #     app=create_geneco_mcp(kind="crm"),
    # ),
    # SubApp(
    #     name="geneco-ssp-mcp",
    #     path_prefix="/geneco-ssp-mcp",
    #     app=create_geneco_mcp(kind="ssp"),
    # ),
    SubApp(
        name="ytllead-mcp",
        path_prefix="/ytllead-mcp",
        app=create_ytllead_mcp(),
    ),
)


def _service_for_path(path: str) -> str:
    for sub in SUB_APPS:
        if path.startswith(sub.path_prefix):
            return sub.name
    return GATEWAY_SERVICE


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Configure logging BEFORE any sub-app lifespan runs; all sub-apps share
    # it. Level/format/env come from YTLLEAD_MCP_LOG_* / YTLLEAD_MCP_ENV.
    logging_settings = YtlleadMcpSettings()
    configure_logging(
        log_level=logging_settings.log_level,
        log_format=logging_settings.log_format,  # type: ignore[arg-type]
        service=GATEWAY_SERVICE,
        env=logging_settings.env,
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
