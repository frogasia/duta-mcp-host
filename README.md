# duta-mcp-host

A hosting service for MCP (Model Context Protocol) servers. Each sub-app
mounted here exposes a set of tools over the MCP `tools/list`/`tools/call`
JSON-RPC protocol, so any MCP-capable chatbot can discover and call them
without bespoke integration code.

The first sub-app, **`geneco-mcp`**, hosts Geneco's D365-backed tools
(account inquiry / create case / case inquiry) and custom-API-backed tools
(fee waiver, re-contract eligibility, fee reversal, password reset) that
duta-ilmu's orchestrator consumes (its MCP client already exists — see
`services/orchestrator/src/orchestrator/tools/mcp.py` in the `duta-ilmu`
repo). `geneco-mcp` owns its own D365 connector, custom API client, and
credential storage for both: each tenant's Entra/Dataverse credentials and
the custom API's Basic Auth credentials are registered once via
credential-intake endpoints (encrypted at rest), and every tool call
resolves that tenant's stored credentials and talks to the relevant
backend directly — no dependency on duta-ilmu's integration-hub.

```
https://<host>/geneco-mcp/{tenant_slug}
```

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
└── geneco-mcp/                # MCP server: D365 tools (crm_*) + custom cxchat API tools (api_*)
    ├── alembic/              # Migrations for the credential store
    ├── app/
    │   ├── main.py           # create_app() factory
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
app.mount("/geneco-mcp", create_geneco_mcp())
```

A request to `/geneco-mcp/geneco` is received by the gateway, which strips
the `/geneco-mcp` prefix and forwards `/geneco` (the MCP endpoint for tenant
`geneco`) to the sub-app.

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
curl -s -X POST localhost:8000/geneco-mcp/geneco/credentials \
  -H 'Content-Type: application/json' \
  -H 'X-Admin-Api-Key: dev-admin-key' \
  -d '{"org_url":"https://geneco.crm.dynamics.com","tenant_id":"...","kv_vault_url":"https://your-vault.vault.azure.net","kv_client_id":"...","kv_client_secret":"..."}'

# Stage 2: drive the MCP handshake by hand
curl -s -X POST localhost:8000/geneco-mcp/geneco \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}'

curl -s -X POST localhost:8000/geneco-mcp/geneco \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'

# Docker (gateway + Postgres)
make docker-up
curl http://localhost/geneco-mcp/geneco/health
```

Each sub-app also works standalone for development:

```bash
cd geneco-mcp
make install
make run           # serves on :8000, routes at POST /{tenant_slug}
make test
```

Note the path difference: standalone, a tenant's endpoint is
`/{tenant_slug}`; behind the gateway it's `/geneco-mcp/{tenant_slug}`.
`geneco-mcp/README.md`'s examples use the standalone form — prepend
`/geneco-mcp` when talking to a gateway deployment (which is what the
Dockerfile runs). See that README for credential setup, the request and
response shapes, and the error convention.

## Adding a New Tool to `geneco-mcp`

1. Create `geneco-mcp/app/tools/<tool_name>.py` with a Pydantic input model
   and an `@register("<tool_name>", description=..., input_model=...)`
   handler (see `crm_verify_account.py` for a D365 tool's shape, or
   `api_check_fee_waiver_status.py` for a custom-API tool's).
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
