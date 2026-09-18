# geneco-mcp

An MCP server hosting seven tools for duta-ilmu's orchestrator to call:

| Tool | What it does |
|---|---|
| `verify_account` | Account inquiry — confirms an `account_id`/`mobile_number` pair, returns the account holder's name and balance. |
| `create_support_case` | Create case — re-verifies the account, then creates a D365 incident. |
| `get_support_cases` | Case inquiry — re-verifies the account, then lists its recent D365 incidents. |
| `check_fee_waiver_status` | Fee waiver inquiry — looks up an account's fee waiver contract lines (ETF, APDF, LPC, PF) against the custom cxchat API. |
| `check_recontract_eligibility` | Re-contract eligibility check — looks up whether an account is eligible to re-contract, against the custom cxchat API. |
| `reverse_fee` | Fee reversal — reverses the applicable fees for an account, against the custom cxchat API. |
| `request_password_reset` | Self-service-portal password reset — sends a password-reset email for a registered account email address, against the custom cxchat API. |

This service owns its own connectors and credential storage — D365/Dataverse
(`app/clients/d365_connector.py`) and the custom fee-waiver API
(`app/clients/custom_api_client.py`), each with its own table under
`app/store/` — no dependency on duta-ilmu's integration-hub. Each tenant's
credentials are registered once per connector kind (stage 1, below) and
encrypted at rest (`app/security/crypto.py`); every `tools/call` (stage 2)
resolves the tenant's stored credentials for whichever connector that tool
needs and talks to the backend directly.

For D365 the stored credential is the tenant's **Key Vault** app
registration, not its D365 one: the real D365 app registration
(`Crm-Client-Id`/`Crm-Client-Secret`) is fetched from that vault on the
tenant's first call and cached in memory for 15 minutes
(`app/store/crm_resolver.py`), so the D365 secret is never at rest here.
The fetch is lazy rather than at startup — a tenant registered after boot
works without a restart, a vault outage degrades only the tenant being
called, and a rotated secret is picked up within the TTL (immediately, if
the credentials are re-registered). The custom API stores Basic Auth
credentials directly, since it has no vault.

## Base URL — read this first

Every path below is relative to a base that depends on how the service is
running, and getting this wrong is the most common way to waste an
afternoon (you get a `404 {"detail":"Not Found"}`, not a helpful error):

| Running as | Base URL |
|---|---|
| Standalone (`make run`, this README's quickstart) | `http://localhost:8000` |
| Behind the gateway (`Dockerfile`/`docker-compose`, i.e. **every real deployment**) | `http://<host>/geneco-mcp` |

The gateway mounts this app under `/geneco-mcp` (see `gateway.py`'s
`SUB_APPS`), so a tenant's MCP endpoint in production is
`https://<host>/geneco-mcp/{tenant_slug}`. Examples below use the
standalone form; prepend `/geneco-mcp` if you're talking to the gateway.

`{tenant_slug}` is a free-form identifier **you choose** when registering
a tenant — it is not validated against anything and never leaves this
service. Use the same value in stage 1 and stage 2.

## API reference

The service serves its own interactive OpenAPI docs, which are always
current with the code (schemas and field descriptions come straight from
the Pydantic models):

```
<base>/api/docs        # Swagger UI, with try-it-out
<base>/openapi.json    # machine-readable schema
```

e.g. `http://localhost:8123/geneco-mcp/api/docs` behind the gateway. Use
that as the authoritative endpoint/field reference; the rest of this
README covers the things a schema can't express — prerequisites, the
error convention, and how the pieces fit together.

## Two-stage flow

**Stage 1 — one-time credential intake** (authenticated, `X-Admin-Api-Key`),
one pair of endpoints per connector kind:

```
POST   /{tenant_slug}/credentials   # register/replace a tenant's D365 credentials
GET    /{tenant_slug}/credentials   # confirm what's registered (kv_client_secret always masked)
DELETE /{tenant_slug}/credentials   # revoke

POST   /{tenant_slug}/credentials/verify   # prove the registered credentials actually work

POST   /{tenant_slug}/custom-api-credentials   # register/replace the custom API's Basic Auth creds
GET    /{tenant_slug}/custom-api-credentials   # confirm what's registered (password always masked)
DELETE /{tenant_slug}/custom-api-credentials   # revoke
```

### Before you register a D365 tenant

Registering succeeds even if these aren't done — and then *every* D365
tool call fails at runtime. Set them up first:

1. **Two secrets must already exist in that tenant's Key Vault**, named
   exactly (names are hardcoded in `app/clients/keyvault_client.py`):
   - `Crm-Client-Id` — the Dataverse app registration's client ID
   - `Crm-Client-Secret` — its client secret
2. **The app registration you pass as `kv_client_id` needs `Get` on
   secrets** in that vault (RBAC role `Key Vault Secrets User`, or a
   `Get` access policy). It does **not** need any Dataverse access — it
   only reads the vault.
3. **The Dataverse app registration** (the one whose ID is in
   `Crm-Client-Id`) must be provisioned as an Application User in the
   D365 org, or Dataverse returns 403 even with a valid token.

```bash
curl -s -X POST localhost:8000/geneco/credentials \
  -H 'Content-Type: application/json' \
  -H 'X-Admin-Api-Key: dev-admin-key' \
  -d '{
    "org_url": "https://geneco.crm.dynamics.com",
    "tenant_id": "<entra-tenant-guid>",
    "kv_vault_url": "https://<your-vault>.vault.azure.net",
    "kv_client_id": "<vault-reader-app-client-id>",
    "kv_client_secret": "<vault-reader-app-client-secret>"
  }'
```

| Field | What it is |
|---|---|
| `org_url` | The Dataverse org, e.g. `https://yourorg.crm.dynamics.com` |
| `tenant_id` | Entra tenant GUID — used as the authority for **both** the Key Vault token and the Dataverse token. This assumes the vault and CRM app registrations live in the same Entra tenant; if yours don't, this needs a second column |
| `kv_vault_url` | The tenant's Key Vault, e.g. `https://your-vault.vault.azure.net` |
| `kv_client_id` / `kv_client_secret` | The app registration that can **read that vault**. Not the D365 one — that gets fetched from the vault at call time and is never stored here |
| `field_map` | Optional `{}`. Overrides the D365 incident column names used by `create_support_case` |

The custom API has no vault — its Basic Auth credentials are stored
directly:

```bash
curl -s -X POST localhost:8000/geneco/custom-api-credentials \
  -H 'Content-Type: application/json' \
  -H 'X-Admin-Api-Key: dev-admin-key' \
  -d '{
    "base_url": "https://<custom-api-host>",
    "username": "<basic-auth-username>",
    "password": "<basic-auth-password>"
  }'
```

Both `POST`s return the stored record with the secret masked, and are
idempotent — re-posting replaces the previous values (and drops the
cached D365 credentials for that tenant, so a rotation takes effect
immediately):

```json
{
  "tenant_slug": "geneco",
  "org_url": "https://geneco.crm.dynamics.com/",
  "tenant_id": "...",
  "kv_vault_url": "https://your-vault.vault.azure.net/",
  "kv_client_id": "...",
  "kv_client_secret": "•••",
  "field_map": {},
  "enabled": true
}
```

`GET` returns the same shape (`404` if nothing is registered); `DELETE`
returns `204`, or `404` if there was nothing to delete.

### Verifying a registration

Registering only proves the values were *stored*. `verify` proves this
service can actually use them right now: it does the same two hops a tool
call does — Key Vault token -> read `Crm-Client-Id`/`Crm-Client-Secret` ->
D365 token -> `WhoAmI` — from inside this process, so a green result
accounts for this service's own egress, identity and firewall rules.

```bash
curl -s -X POST localhost:8000/geneco/credentials/verify \
  -H 'X-Admin-Api-Key: dev-admin-key'
```

```json
{
  "ok": true,
  "detail": "Key Vault and D365 both answered — the vault returned the D365 app registration and WhoAmI succeeded against https://geneco.crm.dynamics.com.",
  "latency_ms": 412
}
```

There is no request body — it verifies what is already registered for
that slug. A *failing* check is an answer, not an error: the response is
still `200`, with `ok: false` and a `detail` naming the actionable cause
(vault access policy, missing secret, D365 rejecting the app
registration, unreachable host, timeout). The only non-`200`s are `401`
(bad admin key) and `404` (nothing registered for that slug). `detail` is
safe to show a tenant admin verbatim — it names secrets and app
registrations, never a secret value.

Two things worth knowing: the check deliberately bypasses the cached D365
app registration (a verify served from cache would prove nothing about
the vault *now*), so the next tool call for that tenant pays one extra
vault round-trip; and the whole check is bounded at 8 seconds.

**Stage 2 — the MCP protocol face**, matching exactly what duta-ilmu's
orchestrator MCP client speaks
(`services/orchestrator/src/orchestrator/tools/mcp.py`): JSON-RPC
`initialize` / `notifications/initialized` / `tools/list` / `tools/call`.

```
POST /{tenant_slug}
```

`tenant_slug` is a URL path parameter, not hardcoded — any tenant with
credentials registered via stage 1 can point its MCP config at
`.../geneco-mcp/{their_slug}`.

### Errors: check `isError`, not the HTTP status

**A failed tool call still returns HTTP 200.** A client that branches on
the status code will treat every failure as a success. Failures are
carried inside the JSON-RPC result instead:

```json
{
  "jsonrpc": "2.0", "id": 1,
  "result": {
    "content": [{"type": "text", "text": "{\"error\": \"...\"}"}],
    "structuredContent": {"error": "I can't check that right now — please contact support."},
    "isError": true
  }
}
```

This is deliberate. The `error` string is written to be relayed verbatim
to a customer by the calling model, so an unconfigured tenant, an
unreachable Key Vault, and a D365 outage all surface as a sentence a
chatbot can say — never a 5xx, and never fabricated data. Branch on
`result.isError`, and read `result.structuredContent`.

There are three other response shapes on this endpoint, all verified:

| Case | Response |
|---|---|
| Unknown JSON-RPC method | **HTTP 200**, but a top-level `error` object (`{"code": -32601, ...}`) instead of `result` — so check for `error` before reading `result` |
| `notifications/initialized` | **HTTP 202**, empty body (it's a notification, not a request) |
| Unknown path / bad tenant slug on an admin route | **HTTP 404** |

Note the first two: a tool failure and a protocol failure are *different
shapes*, and neither is a non-200.

A successful call puts the tool's own result in `structuredContent`:

```json
{
  "result": {
    "content": [{"type": "text", "text": "{\"verified\": true, ...}"}],
    "structuredContent": {"verified": true, "account_id": "...", "name": "...", "account_balance": 75.79},
    "isError": false
  }
}
```

No transport-level authentication on this endpoint (the orchestrator's MCP
client sends no auth headers to tenant MCP servers today — a
platform-wide convention, not introduced here). This is a deliberate,
confirmed tradeoff, not an oversight — see the project's "deferred
security follow-up" note: once real D365 credentials live here, this
endpoint directly triggers live D365/custom-API calls using stored
secrets, so this is worth revisiting alongside network-level access
control. The data-level gate is unchanged regardless: every D365 tool
requires a correct `account_id` + `mobile_number` match before returning
anything. The four custom-API tools (`check_fee_waiver_status`,
`check_recontract_eligibility`, `reverse_fee`, `request_password_reset`)
are the exception — the custom API has no documented shape for that
cross-check, so `account_number` (or `email`, for password reset) is a
direct lookup key there, not something re-verified first.

## Provisioning a tenant end to end

Against a gateway deployment (drop `/geneco-mcp` if running standalone):

```bash
HOST=http://localhost:8123/geneco-mcp
SLUG=acme                       # you choose this
ADMIN_KEY=dev-admin-key         # GENECO_MCP_ADMIN_API_KEY

# 1. D365, via the tenant's Key Vault (prerequisites above must be done first)
curl -s -X POST "$HOST/$SLUG/credentials" \
  -H "X-Admin-Api-Key: $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"org_url":"https://acme.crm.dynamics.com","tenant_id":"<entra-guid>",
       "kv_vault_url":"https://acme-kv.vault.azure.net",
       "kv_client_id":"<vault-reader-id>","kv_client_secret":"<vault-reader-secret>"}'

# 2. The custom cxchat API
curl -s -X POST "$HOST/$SLUG/custom-api-credentials" \
  -H "X-Admin-Api-Key: $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"base_url":"https://<cxchat-host>/webapi","username":"<user>","password":"<pass>"}'

# 3. Prove the D365 chain works (vault -> D365), no account number needed
curl -s -X POST "$HOST/$SLUG/credentials/verify" -H "X-Admin-Api-Key: $ADMIN_KEY"

# 4. Confirm the tools are visible
curl -s -X POST "$HOST/$SLUG" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

# 5. End to end, with a real account
curl -s -X POST "$HOST/$SLUG" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"verify_account",
       "arguments":{"account_id":"<acct>","mobile_number":"<mobile>"}}}'
```

Steps 1–2 only prove the credentials were *stored*. Step 3 is what proves
the D365 ones *work*, and its `detail` says what to fix if they don't —
do that before blaming a tool call. Step 5 is the end-to-end test: if it
returns `isError: true` with "not available for this workspace", nothing
is registered for that slug; if it says "I can't check that right now",
the credentials are registered but the vault or D365 rejected them (step
3 will say which). Nothing verifies the custom API's credentials yet —
only D365 has a `verify`.

## Configuration

All env vars are prefixed `GENECO_MCP_` — see [.env.example](./.env.example).

| Var | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://genecomcp:genecomcp-dev@localhost:55441/geneco_mcp` | Where tenant credentials are stored (D365 and custom API, one table each). |
| `CREDENTIAL_ENCRYPTION_KEY` | *(empty)* | Fernet key encrypting each stored `kv_client_secret` (D365's vault credential) and `password` (custom API). Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. A bare env var is a stopgap — swap for a real KMS/Key Vault-issued key before production. |
| `ADMIN_API_KEY` | *(empty)* | Sent as `X-Admin-Api-Key` — required on `/{tenant_slug}/credentials` and `/{tenant_slug}/custom-api-credentials`. |
| `LOG_LEVEL` | `INFO` | |
| `LOG_FORMAT` | `json` | `json` or `human`. |
| `ENV` | `local` | |

## Local quickstart

```bash
cp .env.example .env   # fill in DATABASE_URL / CREDENTIAL_ENCRYPTION_KEY / ADMIN_API_KEY
make dev                # installs [dev] extras
docker compose -f ../docker-compose.yml up -d db   # or point DATABASE_URL at your own Postgres
make migrate            # alembic upgrade head
make run                # serves on :8000
make test
```

```bash
curl -s -X POST localhost:8000/geneco \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

### SIT test data

A known-real account in the `orca-sit` D365/Dataverse org, used for manual
smoke-testing all seven tools end-to-end against the real SIT backends
(both D365 and the custom cxchat API) — not fixture data, and not
guaranteed stable if the SIT org's data changes:

| Field | Value |
|---|---|
| Account number | `GC8030359D` |
| Mobile number | `87654321` |
| Registered email | `serayageneco+test-renewdtoc@gmail.com` |

The custom cxchat API's own base URL/Basic Auth credentials for SIT are
**not** in this file — they're in `.env`'s gitignored reference block
(`CUSTOM_API_BASE_URL`/`CUSTOM_API_USERNAME`/`CUSTOM_API_PASSWORD`, not
read directly by `Settings`); fetch them from there and register them via
stage 1 before testing the custom-API tools locally.

## Adding a tool

1. New file under `app/tools/<name>.py`: a Pydantic input model + an
   `@register("<name>", description=..., input_model=...)` handler that
   takes a `ToolContext` (`app/tools/registry.py`) as its first argument —
   `ctx.session` and `ctx.encryption_key` are all a handler gets; it
   builds whichever credential store it needs from those
   (`CredentialStore` for D365, `CustomApiCredentialStore` for the custom
   API, or a new store class for a new connector kind entirely).
2. Import it in `app/tools/__init__.py`.
3. If it needs a new D365 operation, add a method to `D365Connector` in
   `app/clients/d365_connector.py` (same for `CustomApiClient` and the
   custom API); wrap any new outbound `httpx`/`msal` call and response
   parse the same way `_request`/`_acquire_token` already do, so a real
   failure always raises `ConnectorError` rather than an unhandled
   exception (never a raw 500 to the caller).
4. A genuinely new connector/credential kind (not D365 or the custom API)
   needs its own table in `app/store/tables.py` + Alembic migration, a
   store class mirroring `store/custom_api_credentials.py`, and its own
   `POST`/`GET`/`DELETE /{tenant_slug}/<kind>-credentials` routes in
   `app/protocol/admin_router.py` reusing `_require_admin_key`.

`app/protocol/router.py` never needs to change — `tools/list` and
`tools/call` both read from the registry, and handlers are handed a
store-agnostic `ToolContext` rather than a concrete store.
