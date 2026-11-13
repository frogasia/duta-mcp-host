# ytllead-mcp

An MCP server that exposes one YTL LEAD conference app API as one tool.
The gateway mounts it at `/ytllead-mcp`:

```
https://<host>/ytllead-mcp/mcp
```

## Tool

| Tool | Arguments | LEAD call |
|---|---|---|
| `get_my_lead_profile` | none | `GET /api/v1/app/users/info?id=<verified user id>` |

The tool returns the record as **Markdown**, not JSON (`app/render.py`).
Sections: personal details, attendance and registration, LEAD event, pre-conference tour,
workshop team, and personality profile with its suggested agenda.

- **No schema.** LEAD publishes none. The renderer was checked against every
  record in ids 1–550 (475 users, Nov 2026). Known fields get named sections.
  Any other field, including ones never seen populated (`workshop_teams`,
  `speakers`), is rendered by a generic fallback, so new data shows up instead
  of being dropped. If a field arrives in an unexpected shape, the call answers
  "can't reach LEAD" rather than failing.
- **Left out on purpose:** database plumbing (ids, pivots, image paths,
  colours), `login_status`, the event `type` (LEAD labels BUILD workshops
  "Seminar") and the `is_active`/`is_ready` admin flags. The placeholder tour
  title "I am not interested." renders as "Not interested in a pre-conference tour".
- **Ambiguity is stated, not guessed:** not attending, a step never completed
  (its entry may be a system default), whether a tour is confirmed, which of
  several venues applies, and the tour meeting point (not in the record).
- **Times:** `…Z` timestamps are UTC and shown in Malaysia time (MYT, UTC+8).
  Zone-less `is_*_completed` stamps are already MYT. Tour start/end times are
  local. A tour's `date` is local midnight stored as UTC, so
  `2026-11-23T16:00Z` renders as 24 Nov.
- **Errors** come back as the plain message text, with `isError: true`.

## Identity: why the tool takes no arguments

The record contains PII: email, phone number, date of birth and the
personality profile. The tool must only return the record of the person
signed in to the chat. The model writes the tool arguments from whatever
the user types, so the user id is **never** an argument. It comes from a
request header that the platform sets:

| Header | Value |
|---|---|
| `X-Verified-User-Token` | An HS256 JWT the platform mints for each call: `{"sub": "<LEAD user id>", "iat", "exp"}`, lifetime 60s. It is signed with `YTLLEAD_MCP_IDENTITY_SIGNING_SECRET`, and that secret never travels on the wire. The platform sends it only for customers signed in through the verified widget sign-in (the host-signed `external_user_id`, channel prefix stripped). |

The check fails closed. The call answers "please sign in", and LEAD is never
called, if any of these is true: there's no token,
the signature is bad, the token has expired (30s clock-skew leeway), its
lifetime is over 120s, the algorithm isn't HS256, or `sub` isn't a bare
positive integer. The server also checks that the returned record's `id`
matches the token's `sub`. The old `X-Verified-User-Id` header is ignored.

A captured request exposes one user for at most 90 seconds (60s lifetime plus 30s clock-skew leeway). Whoever holds the
**signing secret** can mint a token for any user, so give it only to the LEAD
tenant.

## Configuration

All settings are environment variables with the `YTLLEAD_MCP_` prefix.
See [.env.example](./.env.example). There is no database.

| Variable | Purpose |
|---|---|
| `LEAD_API_TOKEN` | Bearer token for the LEAD API. It is provisioned outside this service and is never logged. |
| `IDENTITY_SIGNING_SECRET` | HS256 secret for `X-Verified-User-Token`. |
| `LOG_LEVEL` / `LOG_FORMAT` / `ENV` | Same meaning as in the rest of the repo. |

`tools/list` returns `[]` until both the LEAD token and the signing secret are set. There is
no API key: the signed token is the only gate.

Older `/ytllead-mcp/<tenant-slug>` URLs still work, because any single
path segment is accepted.

## Errors

Every failure comes back as HTTP 200 with the relayable message as the text
and `isError: true`. A failure is never a 5xx and never fabricated data.
LEAD answers an unknown id with `404 {"success":false,"message":"User not found."}`,
which becomes "profile not found". `success: false` and `data: null` are treated the same way.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e ../shared -e ".[dev]"
.venv/bin/pytest                                              # respx, no network
.venv/bin/uvicorn ytllead_mcp.main:create_app --factory --port 8002   # standalone: POST /mcp
```

`app/lead.py` makes the LEAD call, `app/render.py` turns the record into
Markdown, and `app/main.py` holds the MCP endpoint, the identity check and the tool.

Test fixtures are invented. Do not copy real LEAD responses into this repo.
