# Migration: `geneco-mcp` splits into two MCP servers

**For:** duta-ilmu (orchestrator + tenant portal)
**Status:** breaking — the old `/geneco-mcp` mount is gone, not deprecated
**Why:** the two backends are onboarded at different times. A tenant's D365
credentials can land weeks before its self-service-portal (SSP) ones, or the
other way round. Two endpoints let the portal register and enable each
connector the moment its own credentials exist, instead of holding one
registration back until both halves are ready.

---

## 1. What changed

One mount became two. Same service, same database, same deployment.

| Before | After |
|---|---|
| `POST /geneco-mcp/{slug}` — all 7 tools | `POST /geneco-crm-mcp/{slug}` — 3 D365 tools<br>`POST /geneco-ssp-mcp/{slug}` — 4 SSP tools |
| `POST /geneco-mcp/{slug}/credentials` | `POST /geneco-crm-mcp/{slug}/credentials` |
| `POST /geneco-mcp/{slug}/custom-api-credentials` | `POST /geneco-ssp-mcp/{slug}/credentials` |
| `POST /geneco-mcp/{slug}/credentials/verify` | `POST /geneco-crm-mcp/{slug}/credentials/verify` |
| `serverInfo.name: "geneco-mcp"` | `"geneco-crm-mcp"` / `"geneco-ssp-mcp"` |

Both servers spell the resource `credentials` — the mount prefix already says
which backend is meant. The **bodies differ** (D365 fields vs SSP ones), and
posting one to the other is a `422`. Each takes a `direct` and a `key_vault`
shape, chosen by `auth_mode`.

### Tool names are unchanged

No renames. `verify_account`, `create_support_case`, `get_support_cases`,
`check_fee_waiver_status`, `check_recontract_eligibility`, `reverse_fee`,
`request_password_reset` — all exactly as before, just served by two
endpoints instead of one. **Nothing in your tool registry needs renaming.**

### `tools/list` is now credential-gated

Each server advertises its tools only once that tenant has registered the
credentials behind them. Before that it returns `{"tools": []}`.

```
no credentials    → crm: []            ssp: []
SSP creds added   → crm: []            ssp: [4 SSP tools]
CRM creds added   → crm: [3 D365 tools] ssp: [4 SSP tools]
```

A server will also never *run* a tool it did not advertise — calling an SSP
tool on the CRM endpoint is an `unknown tool` error even when SSP credentials
exist.

---

## 2. Orchestrator changes

*(Done in duta-ilmu, 2026-09-22. Recorded here because what these turned into
differs from what was asked for.)*

**2.1 — Register two MCP URLs per tenant, not one.** ✅ `tenants.config["mcp"]`
is now `{"servers": {"crm": {...}, "ssp": {...}}}`; the old flat
`{server_url, enabled}` reads as a lone `crm` entry, so no data migration.
Each discovered tool row records its server in `config["server"]`, and the
toolproxy routes a call to the server that advertised it. Shape and helpers
live in `support/tools/servers.py`.

**2.2 — `tools/list` caching.** ✅ Not a connection cache — duta-ilmu
**persists** the list as `Tool` rows, refreshed only by an explicit
`POST /tenants/{slug}/mcp/discover`. (The path named here,
`services/orchestrator/src/orchestrator/tools/mcp.py`, does not exist; the
client is `packages/py-duta-domain/src/duta_domain/support/tools/mcp.py` and
holds no cache of its own.) Discovery is now scoped per server, so refreshing
one backend cannot disturb the other's rows.

**2.3 — Empty tool list.** ✅ Handled, but NOT by ignoring it. Since
`tools/list` is credential-gated, an empty answer means that backend can serve
nothing, so its tool rows are **removed** rather than left listed as if they
worked. Only an empty *answer* clears; an unreachable server raises a 502 and
removes nothing, so a blip is never mistaken for a revocation.

Deliberate cost, chosen by the product owner: the per-tool on/off choices live
on those rows, so clearing forgets them and every tool returns switched ON when
the credentials are fixed — including ones a tenant had switched off.

**2.4 — No auth header changes.** `tools/call` stays unauthenticated on both
servers, per the platform-wide convention. Only the credential-intake routes
require `X-Admin-Api-Key`, and one key still covers both.

---

## 3. Portal changes

**3.1 — New `ConnectorKind`.** In
`apps/web/src/surfaces/portal/lib/api.ts`:

```ts
export type ConnectorKind = "d365" | "generic_rest" | "webhook" | "ssp";
```

plus matching support in integration-hub: an `SspConfig` model, `KIND_MODELS`,
and `CONNECTOR_KINDS` in both `integrations/tables.py` and
`tenancy/schemas.py`. `kind` is validated in Python only, so this needs no
duta-ilmu migration.

**3.2 — New tab** in
`apps/web/src/app/(portal)/portal/(portal)/integrations/page.tsx`, labelled
**"Self-Service Portal"**.

A clone of the D365 tab, including its Key Vault toggle — the SSP server
takes both modes as of 0005. No endpoint field: unlike Generic REST, the
paths are fixed in the service, and the tab says so, otherwise it reads as a
duplicate of Generic REST.

```
POST /geneco-ssp-mcp/{tenant_slug}/credentials
Content-Type: application/json
X-Admin-Api-Key: <admin key>

direct:     {"auth_mode": "direct", "base_url": "https://<ssp-host>",
             "username": "...", "password": "..."}
key_vault:  {"auth_mode": "key_vault", "base_url": "https://<ssp-host>",
             "tenant_id": "...", "kv_vault_url": "...",
             "kv_client_id": "...", "kv_client_secret": "..."}
```

`auth_mode` defaults to `direct` (where D365 defaults to `key_vault`) —
every caller that registered before vault mode existed posted a stored pair
and meant it. The other mode's fields are rejected with a `422`, so exactly
one set goes on any call.

Vault mode needs a **`tenant_id`** the original spec did not list: it is the
authority the Key Vault token is acquired against, and SSP cannot borrow
D365's, since a tenant may have no D365 connector at all. In that mode the
vault holds the SSP pair under the fixed names `Ssp-User-Id` / `Ssp-Password`.

Response echoes the record with the live mode's secret masked as `•••`, and
omits the other mode's columns entirely.

**3.3 — The D365 base URL moves** — `/geneco-mcp/...` →
`/geneco-crm-mcp/...`. Not a portal change: the tabs hold no URLs. All three
D365 routes are built from one `DUTA_MCP_BASE_URL` env var, and SSP needed a
second, `DUTA_MCP_SSP_BASE_URL`. Both must also exist on the **tenancy**
Settings, not only the integrations one — the portal's tool sync reads them
there, and a missing field reads exactly like an unconfigured backend and
skips the sync silently.

**3.4 — SSP now has a `verify` too**, at the same path
(`POST /geneco-ssp-mcp/{slug}/credentials/verify`), with the same
`{ok, detail, latency_ms}` shape. So a "Test connection" button on the SSP tab
works — with one difference worth wiring for.

The SSP API has no ping/WhoAmI endpoint, so the check is a real read-only
`feewaiver` lookup. It takes an optional `{"account_number": "..."}` body —
**which duta-ilmu does not send, and does not need to.**

Measured against the SIT API on 2026-09-22, the endpoint separates the two
cases on its own:

| | HTTP | body |
|---|---|---|
| any accepted credential, real or unknown account | `2xx` | — |
| wrong password / wrong username / no auth header | `400` | `{"responsecode":"GEN400","errordescription":"Unauthorized request"}` |

So the placeholder probe already tells good credentials from bad in both
directions, and a real account number only answers a different question
(whether that account resolves). A "test account number" field was built on
the SSP tab and then removed once this was measured.

⚠️ **This API never uses 401/403** — every auth failure is a `400` with the
reason in the body only. The original wording of this section assumed
otherwise, and so did `_ssp_failure_detail`, which therefore reported rejected
credentials as "could not confirm". Fixed by reading the body: `ConnectorError`
now carries it, and `_is_ssp_auth_rejection` matches on it.

That match is a **substring check for "unauthorized"** — the best signal this
API offers, but a heuristic, and now a destructive one: a definite rejection
clears that backend's tools. If the API ever uses that word in a non-auth
message, it would clear a working tenant's tools. Known and accepted.

**3.5 — Consider gating the tabs.** Now five, one of which (MCP) is still a
mock. Not a blocker for this migration, but the page is due an overhaul.

The SSP tab carries its own **tool table**, listing only the tools its own
backend advertised — as does the D365 tab. Both render one shared
`McpToolPanel`; the two backends are registered, tested and toggled
independently.

---

## 4. Cutover

The old `/geneco-mcp` mount is **removed**, not aliased — the two servers
can't both live under it, because both now serve `/{slug}/credentials`.

1. Run duta-mcp-host's migration **0005** (below), then deploy it
2. Set `DUTA_MCP_BASE_URL` to the `/geneco-crm-mcp` mount and add
   `DUTA_MCP_SSP_BASE_URL` for `/geneco-ssp-mcp`, then restart duta-ilmu
3. Repoint each tenant's stored MCP registration (see the warning below)
4. Ship the new tab whenever it's ready — it's additive

**There IS a schema change, contrary to earlier drafts of this section.**
Migration `0005` adds `auth_mode`, `tenant_id` and the three `kv_*` columns to
`custom_api_credentials`, and makes `username`/`password_encrypted` nullable
so a vault-mode row can leave them empty. It is non-destructive going forward:
existing rows default to `auth_mode='direct'` and keep working untouched.
`d365_credentials` is unchanged.

**And duta-ilmu holds state that must change too.** Each tenant's
`config["mcp"]` is reshaped from `{server_url, enabled}` to
`{"servers": {...}}`, and tool rows gain a `server` key. Both are read
back-compatibly, so the reshape happens on the next save per tenant rather
than as a migration — but until a tenant is saved, its registration still
points at the removed `/geneco-mcp` mount and **every tool call 404s**.

⚠️ Repointing a tenant clears that server's tool rows (the URL changed, so the
rows described a different server) and, with them, its per-tool on/off
choices. Expect every tool to come back switched ON after the re-discovery.

**Rollback** is a redeploy of the previous duta-mcp-host revision, reverting
the URLs, and `alembic downgrade 0004` — which DELETES any `key_vault` SSP row,
since the old schema has nowhere to put one. Those tenants must re-register.
Rollback is therefore not free, unlike what this section previously claimed.

---

## 5. Verifying it works

```bash
CRM=https://<host>/geneco-crm-mcp
SSP=https://<host>/geneco-ssp-mcp
SLUG=<tenant>

# Each should report its own name
curl -s -X POST "$CRM/$SLUG" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' | jq .result.serverInfo

# Each lists only its own tools — [] if that backend isn't registered yet
curl -s -X POST "$CRM/$SLUG" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | jq '.result.tools[].name'
curl -s -X POST "$SSP/$SLUG" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | jq '.result.tools[].name'
```

An empty list means that backend's credentials aren't registered for that
slug — not that the service is broken.

---

## Questions

Full detail in [README.md](./README.md) — base URLs, the two D365 `auth_mode`
shapes, the error convention (`isError`, never the HTTP status), and
provisioning a tenant end to end.
