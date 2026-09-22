# duta-mcp-host

A hosting service for MCP (Model Context Protocol) servers. Each sub-app
mounted here exposes a set of tools over the MCP `tools/list`/`tools/call`
JSON-RPC protocol, so any MCP-capable chatbot can discover and call them
without bespoke integration code.

The first sub-app, **`geneco-mcp`**, is mounted **twice** — once per
backend it fronts, because a tenant's credentials for the two arrive at
different times and each connector should be registerable on its own:

| Mount | Backend | Tools |
|---|---|---|
| `/geneco-crm-mcp` | D365/Dataverse | account inquiry, create case, case inquiry |
| `/geneco-ssp-mcp` | Geneco's self-service portal (custom cxchat API) | fee waiver, re-contract eligibility, fee reversal, password reset |

Both are consumed by duta-ilmu's orchestrator (its MCP client already exists — see
`services/orchestrator/src/orchestrator/tools/mcp.py` in the `duta-ilmu`
repo). `geneco-mcp` owns its own D365 connector, custom API client, and
credential storage for both: each tenant's Entra/Dataverse credentials and
the custom API's Basic Auth credentials are registered once via
credential-intake endpoints (encrypted at rest), and every tool call
resolves that tenant's stored credentials and talks to the relevant
backend directly — no dependency on duta-ilmu's integration-hub.

```
https://<host>/geneco-crm-mcp/{tenant_slug}
https://<host>/geneco-ssp-mcp/{tenant_slug}
```

Each advertises only its own tools, and only once that tenant's
credentials for its backend are registered — before that, `tools/list`
returns an empty list.

Each mounted service prefixes its environment variables with its module
name (e.g. `GENECO_MCP_DATABASE_URL`) so co-hosted services don't clash on
config in the shared gateway process.

## Project Structure

```
duta-mcp-host/
├── gateway.py              # FastAPI gateway — mounts all MCP-hosting sub-apps
├── Dockerfile              # Single container for deployment
├── docker-compose.yml      # Local orchestration (gateway + Postgres)
├── Makefile                # Dev commands
├── shared/                 # duta_mcp_shared — structlog + per-request context
└── geneco-mcp/                # Two MCP servers: D365 tools (kind="crm") + SSP API tools (kind="ssp")
    ├── alembic/              # Migrations for the credential store
    ├── app/
    │   ├── main.py           # create_app(kind=...) factory — one app per backend
    │   ├── config.py         # Settings (env-prefixed GENECO_MCP_)
    │   ├── clients/          # D365Connector (MSAL OAuth2) + CustomApiClient (Basic Auth)
    │   ├── store/            # Postgres-backed tenant credential store (encrypted at rest), one table per connector kind
    │   ├── security/         # stored-secret encryption (Fernet)
    │   ├── tools/            # Tool definitions + handlers (one file per tool, @register)
    │   └── protocol/         # MCP JSON-RPC (router.py) + credential intake (admin_router.py)
    ├── tests/
    └── pyproject.toml
```

## How It Works

The root `gateway.py` mounts each MCP-hosting sub-app at its own path:

```python
app.mount("/geneco-crm-mcp", create_geneco_mcp(kind="crm"))
app.mount("/geneco-ssp-mcp", create_geneco_mcp(kind="ssp"))
```

A request to `/geneco-crm-mcp/geneco` is received by the gateway, which
strips the `/geneco-crm-mcp` prefix and forwards `/geneco` (the MCP
endpoint for tenant `geneco`) to that sub-app. The two share a codebase
and a database; `kind` picks which tools each serves and which
credential-intake body it accepts.

Each sub-app uses `app/` as its source directory. The `package-dir`
mapping in its `pyproject.toml` makes it importable under a unique Python
name (e.g. `geneco_mcp`), avoiding import collisions in the shared gateway
process.

## Quick Start

```bash
# Install all dependencies into a local venv
make install

# Configure the DB + credential encryption key + admin API key
cp geneco-mcp/.env.example geneco-mcp/.env
# generate a real encryption key:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Start Postgres and run migrations
docker compose up -d db
cd geneco-mcp && make migrate && cd ..

# Run the gateway locally
make run

# Stage 1: register a tenant's Key Vault credentials, which the service uses
# to fetch that tenant's D365 app registration at call time (X-Admin-Api-Key).
# A tenant with no vault registers the D365 app registration itself instead —
# add "auth_mode":"direct" with client_id/client_secret; see geneco-mcp/README.md
curl -s -X POST localhost:8000/geneco-crm-mcp/geneco/credentials \
  -H 'Content-Type: application/json' \
  -H 'X-Admin-Api-Key: dev-admin-key' \
  -d '{"org_url":"https://geneco.crm.dynamics.com","tenant_id":"...","kv_vault_url":"https://your-vault.vault.azure.net","kv_client_id":"...","kv_client_secret":"..."}'

# ...and the self-service-portal API, on the other mount (same path)
curl -s -X POST localhost:8000/geneco-ssp-mcp/geneco/credentials \
  -H 'Content-Type: application/json' \
  -H 'X-Admin-Api-Key: dev-admin-key' \
  -d '{"base_url":"https://<ssp-host>","username":"...","password":"..."}'

# Stage 2: drive the MCP handshake by hand
curl -s -X POST localhost:8000/geneco-crm-mcp/geneco \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}'

# Lists the three D365 tools — or [] if stage 1 was skipped for this slug
curl -s -X POST localhost:8000/geneco-crm-mcp/geneco \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'

# Docker (gateway + Postgres)
make docker-up
curl http://localhost/geneco-crm-mcp/geneco/health
```

Each sub-app also works standalone for development:

```bash
cd geneco-mcp
make install
make run           # geneco-crm-mcp on :8000, routes at POST /{tenant_slug}
make run-ssp       # geneco-ssp-mcp on :8001
make test
```

Note the path difference: standalone each server runs unmounted, so a
tenant's endpoint is `/{tenant_slug}`; behind the gateway it's
`/geneco-crm-mcp/{tenant_slug}` or `/geneco-ssp-mcp/{tenant_slug}`.
`geneco-mcp/README.md`'s examples use the standalone form — prepend the
right mount when talking to a gateway deployment (which is what the
Dockerfile runs). See that README for credential setup, the request and
response shapes, and the error convention.

## Adding a New Tool to `geneco-mcp`

1. Create `geneco-mcp/app/tools/<tool_name>.py` with a Pydantic input model
   and an `@register("<tool_name>", kind="crm"|"ssp", description=...,
   input_model=...)` handler (see `crm_verify_account.py` for a D365
   tool's shape, or `api_check_fee_waiver_status.py` for an SSP tool's).
   `kind` decides which of the two servers serves it.
2. Import it in `geneco-mcp/app/tools/__init__.py` (triggers registration).
3. If it needs a new D365 operation, add a method to `D365Connector` in
   `app/clients/d365_connector.py` (or a new `CustomApiClient` method in
   `app/clients/custom_api_client.py` for the custom cxchat API) — wrap
   any new outbound call the same way `_request`/`_acquire_token` already
   do, so a real failure always raises `ConnectorError` rather than an
   unhandled exception.

Nothing in `protocol/router.py` needs to change — `tools/list` and
`tools/call` both read from the registry.

## Adding a New MCP-Hosting Sub-App

Follow the same pattern as `chatbot-poc-projects`:

1. Scaffold a new project (`cp -r geneco-mcp <new-mcp>`), set its
   `pyproject.toml` `name` + `package-dir` mapping, update internal
   imports.
2. Add a `create_app()` factory in `<new-mcp>/app/main.py`.
3. Import and mount it in `gateway.py`'s `SUB_APPS`.
4. Extend the root `Makefile`'s `install` target and the `Dockerfile`'s
   `COPY`/`pip install` lines.
5. Add its `.env` to `docker-compose.yml`'s `env_file` list.

## Deployment

The gateway runs as a single container:

- **Container**: Runs `uvicorn gateway:app` on port 8000.
- Path routing is handled by the gateway, not the reverse proxy in front
  of it.

## Contract

Each MCP-hosting sub-app must:

1. Have a `pyproject.toml` with a unique `name` and `package-dir` mapping.
2. Expose a `create_app()` factory in `app/main.py` that returns a FastAPI
   instance.
3. Implement `POST /{tenant_slug}` speaking JSON-RPC 2.0
   (`initialize` / `notifications/initialized` / `tools/list` /
   `tools/call`) — the subset the consuming MCP client actually uses.
4. Serve HTTP on port 8000 when running standalone.
