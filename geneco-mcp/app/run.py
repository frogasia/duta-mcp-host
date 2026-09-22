"""Standalone dev entrypoint: ``python -m geneco_mcp.run [crm|ssp]``.

Runs ONE of the two MCP servers, on its own port, without the gateway —
``crm`` (D365 tools) on 8000, ``ssp`` (self-service-portal tools) on 8001.
Unmounted, so paths have no ``/geneco-crm-mcp`` prefix here: the endpoint
is just ``POST /{tenant_slug}``. Run ``uvicorn gateway:app`` instead to get
both behind their real prefixes.

Configures the shared structlog pipeline before uvicorn starts so
``uvicorn.access``/``uvicorn.error`` route through it from the first
request (mirrors chatbot-poc-projects/sc-chatbot's ``run.py``).
"""

from __future__ import annotations

import sys

import uvicorn
from duta_mcp_shared.observability import configure_logging

from geneco_mcp.config import Settings
from geneco_mcp.main import SERVER_NAMES

#: Distinct ports so both can run side by side during local development.
PORTS = {"crm": 8000, "ssp": 8001}

if __name__ == "__main__":
    kind = sys.argv[1] if len(sys.argv) > 1 else "crm"
    if kind not in SERVER_NAMES:
        raise SystemExit(f"usage: python -m geneco_mcp.run [{'|'.join(SERVER_NAMES)}]")
    settings = Settings()
    configure_logging(
        log_level=settings.log_level,
        log_format=settings.log_format,  # type: ignore[arg-type]
        service=SERVER_NAMES[kind],
        env=settings.env,
    )
    uvicorn.run(
        f"geneco_mcp.main:{kind}_app",
        factory=True,
        host="0.0.0.0",
        port=PORTS[kind],
        log_config=None,
        reload=settings.env == "local",
    )
