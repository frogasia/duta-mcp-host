# CLAUDE.md

Project-specific guidance for Claude Code working in this repo. Adapted from
`chatbot-poc-projects/CLAUDE.md` for an MCP-hosting service rather than a
chat application — the layering names differ, the principles don't.

## Repo layout

```
duta-mcp-host/
├── gateway.py              # Mounts geneco-mcp (and future MCP hosts) behind one uvicorn
├── shared/                 # duta_mcp_shared — structlog + per-request context
└── geneco-mcp/                # MCP server for account inquiry / create case / case inquiry
    │                          + Geneco's custom cxchat API tools (fee waiver, re-contract
    │                          eligibility, fee reversal, password reset)
    ├── alembic/              # Migrations for the credential store
    └── app/
        ├── protocol/        # ← MCP wire boundary: tools/call (router.py) + credential
        │                      intake (admin_router.py). tenant_slug from the URL.
        ├── tools/           # Tool definitions + handlers (LLM-facing contract, one file
        │                      each — crm_* prefix for D365 tools, api_* for custom API tools)
        ├── clients/         # D365Connector (MSAL OAuth2) + CustomApiClient (Basic Auth)
        │                      + KeyVaultClient (reads each tenant's D365 creds at call time)
        ├── store/           # Postgres-backed tenant credential store, one table per connector
        │                      kind (D365 rows carry auth_mode: key_vault or direct), +
        │                      crm_resolver.py's in-memory TTL cache of vault-fetched creds
        └── security/        # Encryption for stored client secrets/password (crypto.py)
```

`geneco-mcp` owns its own D365 connector, custom API client, and encrypted
credential storage for both — it talks to D365 and the custom cxchat API
directly and has no dependency on duta-ilmu's integration-hub.

## Coding principles

### SOLID — applied concretely in this repo

- **Single responsibility.** `D365Connector` only speaks Dataverse/MSAL.
  `CredentialStore` only resolves a `tenant_slug` to a connector (DB +
  decryption). `protocol/router.py` only speaks JSON-RPC. Tool handlers
  only adapt MCP arguments to a store/connector call.
- **Open–closed.** Adding a tool = new file under `tools/` + `@register`;
  no edits to `protocol/router.py`'s dispatcher. Adding a D365 operation =
  new method on `D365Connector`; no edits to `_request`.
- **Liskov.** Prefer composition over inheritance — there is no inheritance
  hierarchy here worth mentioning, and that's the point; don't introduce
  one for "semantic tagging".
- **Interface segregation.** `D365Connector` exposes one method per D365
  operation (`get_account_by_filter`, `create_case`,
  `list_incidents_for_customer`), not a generic `call(endpoint, body)` —
  callers state intent, not wire shape.
- **Dependency inversion.** `CredentialStore` is constructed from a DB
  session + the encryption key and passed into tool handlers as an
  argument, never reached for as a module-level singleton — this is what
  makes `respx`-mocked tests possible without monkeypatching internals.

### Type hygiene

- **No `Any`** at typed boundaries. MCP arguments are validated into a
  Pydantic input model (one per tool) before a handler ever sees them; the
  credential-intake body is validated into `D365CredentialsIn`.
- **Prefer Pydantic models over bare dicts at boundaries** — every tool's
  input is a `BaseModel`; JSON-RPC envelopes are built via the typed
  helpers in `protocol/models.py`, not hand-assembled dicts scattered
  across the router.
- D365/Dataverse's own response dicts are passed through as-is once
  shaped into the tool's result (they're already `dict[str, Any]` per an
  external contract this project doesn't own) — don't re-wrap them in a
  redundant model just to satisfy a type checker.

### YAGNI

- This service hosts exactly the tools duta-ilmu's orchestrator actually
  calls today. Don't add placeholder tools, config knobs, or a "generic"
  MCP-server framework in anticipation of a second consumer that hasn't
  arrived.
- No Streamable-HTTP SSE duplex support — the one MCP client this project
  talks to (duta-ilmu's orchestrator) only POSTs and never opens a
  separate stream. Every *tool call* is still stateless per call (no
  conversation/session caching — see `tools/_common.py`'s re-verify
  pattern); the credential store is the one deliberate exception to
  "no persistence," since stage-1 credential intake has to outlive a
  single request. Build the framework this client actually needs, not the
  full spec.
- Only the D365 connector operations the three tools actually use are
  implemented (`get_account_by_filter`, `create_case`,
  `list_incidents_for_customer`) — no `add_comment`/`sync_status`/
  `test_connection` scaffolding for operations nothing calls yet.

### Async-all-the-way

- FastAPI endpoints, tool handlers, `D365Connector`, and `CredentialStore`
  methods are all `async def`. Use `httpx.AsyncClient` for Dataverse calls
  and `AsyncSession`/`async_sessionmaker` for the DB — never a sync HTTP
  or DB call inside an `async def` route.

### Honest errors, never fabricated data

This is a hard constraint (duta-ilmu's own decision for these tools, not
just a style preference): a D365/network failure or an unconfigured tenant
must come back as `{"error": "<message the model can relay to the
customer>"}`, never a mocked/fabricated result. `D365Connector._request`
(and `_acquire_token`) is the one place that boundary is enforced — every
outbound MSAL/httpx call and response parse is wrapped so a real failure
always raises `ConnectorError`, never an unhandled exception. Don't add a
second error-shaping path elsewhere, and don't let a new connector method
skip this wrapping — an unwrapped exception here means a customer-facing
tool call turns into a raw 500 instead of a relayable error (this is the
exact bug that motivated this service owning its own connector instead of
proxying to a system that had this same gap).

### Credential security

- **In `key_vault` mode the D365 secret is never at rest here** — and that
  mode is the default and the one to prefer. `d365_credentials` stores the
  tenant's *Key Vault* app registration; the real D365 one
  (`Crm-Client-Id`/`Crm-Client-Secret`, names hardcoded in
  `clients/keyvault_client.py`) is fetched on first use and lives only in
  the in-memory TTL cache in `store/crm_resolver.py`. Don't add a code path
  that writes a fetched `Crm-Client-Secret` to the DB or a log — that would
  undo the whole point of the indirection.
- `auth_mode: "direct"` is the deliberate exception, for tenants with no
  vault to read from: `client_id`/`client_secret_encrypted` then hold the
  D365 app registration itself. A row is in exactly one mode and `upsert`
  clears the other mode's columns, so a tenant that moves to a vault
  leaves no D365 secret behind. Don't add a precedence rule between the
  two — there is only ever one pair to find.
- The two secret columns (`kv_client_secret_encrypted`,
  `client_secret_encrypted`) are the only fields encrypted at rest
  (`security/crypto.py`, Fernet) — `org_url`/`tenant_id`/`auth_mode`/
  `kv_vault_url`/`kv_client_id`/`client_id` are stored plain, matching the
  same secret/non-secret split used elsewhere in this system.
- Every read path (`GET`/`POST /{tenant_slug}/credentials`) returns a
  masked placeholder in place of whichever secret the row holds, never the
  real value — see `store/credentials.py`'s `MASK`. Validation failures go
  through `admin_router.validation_error_handler`, which strips the
  echoed input, so a rejected body can't bounce a posted secret back.
- Per-mode field presence is enforced in the Pydantic intake model, not by
  a DB constraint — a portal needs a readable 422, not an IntegrityError.
- A vault failure must surface as a relayable error, never a 500: the
  resolver translates `KeyVaultError` into `ConnectorError` at its
  boundary so tool handlers keep catching one error type. Note this makes
  `get_connector()` raise as well as return `None` — keep both call sites
  (`tools/_common.py`, `tools/crm_verify_account.py`) handling each case.
- `tools/call` is intentionally unauthenticated (matches duta-ilmu's
  platform-wide MCP convention — the orchestrator's MCP client never sends
  auth headers). The credential-intake endpoints
  (`POST`/`GET`/`DELETE /{tenant_slug}/credentials`) are a fundamentally
  more sensitive surface and require `X-Admin-Api-Key` — don't relax that,
  and don't add a new sensitive route without the same guard.

## Test expectations

- **`pytest` + `respx`** for HTTP mocks against `D365Connector`'s Dataverse
  calls — no live network calls in tests. MSAL's own token-acquisition
  handshake is stubbed via `unittest.mock.patch` on `_acquire_token`
  rather than mocked over HTTP — that's msal's internal machinery, not
  something this project owns the request shape of.
- **`asyncio_mode = "auto"`** (set in each sub-app's `pyproject.toml`) — no
  `@pytest.mark.asyncio` needed on async test functions.
- Tests use a per-test SQLite file DB (`tests/conftest.py`), with tables
  created via a plain **sync** SQLAlchemy engine (not the app's async one)
  so DDL setup never risks reusing a pooled `aiosqlite` connection across
  a different event loop than the one `TestClient` drives requests on.
- Cover the full JSON-RPC sequence (`initialize` → `tools/list` →
  `tools/call`) at least once per tool, plus the honest-error path for both
  "no credentials registered for this tenant" and "D365 call failed" —
  the latter must assert **HTTP 200**, never a 5xx.

## PR hygiene

- One PR = one concern.
- Commit messages: `<type>(<scope>): <subject>` — e.g. `feat(geneco-mcp):
  add create_support_case tool`.
- Never skip pre-commit hooks or force-push shared branches.
