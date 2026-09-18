"""Stage-1 credential intake: ``POST``/``GET``/``DELETE
/{tenant_slug}/credentials`` (D365) and ``.../custom-api-credentials``
(the custom fee-waiver/re-contract API), plus
``POST /{tenant_slug}/credentials/verify``, which proves the registered
D365 credentials actually work *from this process* rather than from
whatever service is asking.

This is a fundamentally more sensitive surface than the MCP ``tools/call``
endpoint (``router.py``) — it accepts real secrets (D365 ``client_secret``,
custom-API ``password``) — so unlike ``tools/call`` (intentionally
unauthenticated, matching duta-ilmu's platform-wide MCP convention), every
route here requires ``X-Admin-Api-Key``, mirroring integration-hub's
``require_internal_key`` pattern
(``services/integration-hub/src/integration_hub/api.py``).
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, HttpUrl, model_validator

from geneco_mcp.clients.d365_connector import ConnectorError
from geneco_mcp.clients.keyvault_client import KeyVaultError, describe as describe_vault_error
from geneco_mcp.config import Settings
from geneco_mcp.store.credentials import (
    AuthMode,
    CredentialStore,
    D365CredentialsInput,
    ResolvedKvCredentials,
)
from geneco_mcp.store.crm_resolver import CrmCredentialResolver
from geneco_mcp.store.custom_api_credentials import (
    CustomApiCredentialsInput,
    CustomApiCredentialStore,
)
from geneco_mcp.store.tables import DIRECT_AUTH_MODE, KEY_VAULT_AUTH_MODE

log = structlog.get_logger()

router = APIRouter()

#: Upper bound on the whole check, so a hanging backend can't pin an admin
#: request open. Comfortably under the httpx timeouts of the hops it wraps
#: (15s vault — ``key_vault`` mode only — and 20s Dataverse), and under
#: what a portal "Test connection" button would wait for.
VERIFY_TIMEOUT_SECONDS = 8.0


#: Which fields belong to which ``auth_mode``. Used both ways round: the
#: mode's own fields are required, and the *other* mode's are rejected —
#: so a body can never carry two credential pairs, nor one labelled as the
#: wrong kind of app registration.
_MODE_FIELDS: dict[str, tuple[str, ...]] = {
    DIRECT_AUTH_MODE: ("client_id", "client_secret"),
    KEY_VAULT_AUTH_MODE: ("kv_vault_url", "kv_client_id", "kv_client_secret"),
}


class D365CredentialsIn(BaseModel):
    """A tenant's D365 setup in one of two shapes, chosen by ``auth_mode``
    — mirroring the toggle duta-ilmu's tenant portal offers.

    ``key_vault`` (the default): the credentials given are the *vault
    reader's*, and the D365 app registration is read from that vault at
    call time (``Crm-Client-Id``/``Crm-Client-Secret``), never stored here.

    ``direct``: the credentials given *are* the D365 app registration, for
    a tenant with no vault. That secret is then stored here (encrypted),
    which the vault indirection exists to avoid — so prefer ``key_vault``
    whenever a tenant can offer one.

    One model rather than a discriminated union because ``auth_mode`` has
    to be *optional*: every caller registering before direct mode existed
    omits it and means ``key_vault``, and a tagged union cannot default its
    own tag.
    """

    auth_mode: AuthMode = Field(
        default=KEY_VAULT_AUTH_MODE,
        description=(
            "How the D365 credentials are supplied: 'key_vault' (default) to register a "
            "vault-reader app the D365 app registration is fetched with, or 'direct' to "
            "register the D365 app registration itself."
        ),
    )
    org_url: HttpUrl = Field(description="D365 org URL, e.g. https://yourorg.crm.dynamics.com")
    tenant_id: str = Field(
        description="Entra (Azure AD) tenant GUID — used for the D365 token in both modes, "
        "and for the Key Vault token in key_vault mode."
    )
    kv_vault_url: HttpUrl | None = Field(
        default=None,
        description="key_vault mode only. Key Vault URL, e.g. https://your-vault.vault.azure.net",
    )
    kv_client_id: str | None = Field(
        default=None,
        description="key_vault mode only. Client/application ID of the Entra app registration "
        "that can read the vault.",
    )
    kv_client_secret: str | None = Field(
        default=None, description="key_vault mode only. That app registration's client secret."
    )
    client_id: str | None = Field(
        default=None,
        description="direct mode only. Client/application ID of the D365 app registration "
        "itself — the one provisioned as an Application User in the org.",
    )
    client_secret: str | None = Field(
        default=None, description="direct mode only. That app registration's client secret."
    )
    field_map: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Optional. Overrides the D365 incident columns create_support_case writes to, "
            'as {"title": "<column>", "description": "<column>"}. Defaults to subject/description.'
        ),
    )

    @model_validator(mode="after")
    def _check_mode_fields(self) -> D365CredentialsIn:
        required = _MODE_FIELDS[self.auth_mode]
        missing = [name for name in required if getattr(self, name) is None]
        if missing:
            raise ValueError(
                f"auth_mode '{self.auth_mode}' requires {', '.join(required)} — "
                f"missing: {', '.join(missing)}"
            )
        other = next(mode for mode in _MODE_FIELDS if mode != self.auth_mode)
        unexpected = [name for name in _MODE_FIELDS[other] if getattr(self, name) is not None]
        if unexpected:
            raise ValueError(
                f"auth_mode '{self.auth_mode}' does not accept {', '.join(unexpected)} — "
                f"those belong to auth_mode '{other}'"
            )
        return self


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """422 bodies with the offending *value* stripped out.

    FastAPI's default validation response echoes the submitted input back
    (``{"input": {...}}``), which on these routes means echoing a
    ``kv_client_secret``/``client_secret``/``password`` verbatim to whoever
    posted it — and into any access log that captures response bodies. The
    field path and message are what a portal actually renders, so only
    those are kept.

    Registered app-wide in ``main.py`` rather than per-route (FastAPI
    handlers are app-level), which is harmless: the credential-intake
    routes here are the only ones in this service that use FastAPI body
    validation at all — ``protocol/router.py`` parses its own JSON-RPC
    envelope.
    """
    errors = [
        {key: value for key, value in error.items() if key not in ("input", "ctx")}
        for error in exc.errors()
    ]
    # Literal 422: starlette renamed its constant and deprecated the old
    # spelling, and this file supports both sides of that rename.
    return JSONResponse(status_code=422, content={"detail": errors})


def _require_admin_key(settings: Settings, x_admin_api_key: str | None) -> None:
    if x_admin_api_key is None or not secrets.compare_digest(x_admin_api_key, settings.admin_api_key):
        log.warning("admin.api_key_rejected", header_present=x_admin_api_key is not None)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid admin api key")


@router.post("/{tenant_slug}/credentials", status_code=status.HTTP_200_OK)
async def upsert_credentials(
    tenant_slug: str,
    body: D365CredentialsIn,
    request: Request,
    x_admin_api_key: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    _require_admin_key(settings, x_admin_api_key)
    async with request.app.state.session_factory() as session:
        store = CredentialStore(session, encryption_key=settings.credential_encryption_key)
        await store.upsert(
            tenant_slug,
            D365CredentialsInput(
                auth_mode=body.auth_mode,
                org_url=str(body.org_url),
                tenant_id=body.tenant_id,
                kv_vault_url=str(body.kv_vault_url) if body.kv_vault_url else None,
                kv_client_id=body.kv_client_id,
                kv_client_secret=body.kv_client_secret,
                client_id=body.client_id,
                client_secret=body.client_secret,
                field_map=body.field_map,
            ),
        )
        redacted = await store.get_redacted(tenant_slug)
    # Rotated vault credentials must take effect now, not at TTL expiry.
    request.app.state.crm_cache.invalidate(tenant_slug)
    assert redacted is not None
    return redacted


@router.get("/{tenant_slug}/credentials")
async def get_credentials(
    tenant_slug: str,
    request: Request,
    x_admin_api_key: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    _require_admin_key(settings, x_admin_api_key)
    async with request.app.state.session_factory() as session:
        store = CredentialStore(session, encryption_key=settings.credential_encryption_key)
        redacted = await store.get_redacted(tenant_slug)
    if redacted is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no credentials registered")
    return redacted


@router.delete("/{tenant_slug}/credentials", status_code=status.HTTP_204_NO_CONTENT)
async def delete_credentials(
    tenant_slug: str,
    request: Request,
    x_admin_api_key: Annotated[str | None, Header()] = None,
) -> None:
    settings: Settings = request.app.state.settings
    _require_admin_key(settings, x_admin_api_key)
    async with request.app.state.session_factory() as session:
        store = CredentialStore(session, encryption_key=settings.credential_encryption_key)
        deleted = await store.delete(tenant_slug)
    request.app.state.crm_cache.invalidate(tenant_slug)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no credentials registered")


class CredentialVerifyResult(BaseModel):
    """What ``POST /{tenant_slug}/credentials/verify`` answers with.

    ``detail`` is rendered verbatim in a tenant-facing portal, so it names
    secrets, app registrations and org URLs — never a secret *value*.
    """

    ok: bool = Field(
        description="True only if every hop this tenant's auth_mode requires succeeded — the "
        "vault read and the D365 call in key_vault mode, the D365 call alone in direct mode."
    )
    detail: str = Field(description="Human-readable outcome, safe to show an admin.")
    latency_ms: int = Field(description="Wall-clock time the whole check took.")


def _d365_failure_detail(exc: ConnectorError, org_url: str, auth_mode: str) -> str:
    """A D365-side failure in the vocabulary a tenant admin can act on.

    The 401/403 wording differs by mode: in ``key_vault`` mode the app
    registration D365 rejected is the one named by the vault's
    ``Crm-Client-Id`` secret, while in ``direct`` mode it is the
    ``client_id`` registered here — naming the vault secret to a direct
    tenant would send them looking for something that isn't in play.
    """
    if exc.status_code in (401, 403):
        if auth_mode == DIRECT_AUTH_MODE:
            return (
                f"D365 rejected the registered credentials ({exc.status_code}) — check the "
                "registered client_id app registration is provisioned as an Application User "
                "in this org, and that its client_secret is current."
            )
        return (
            f"D365 rejected the credentials read from Key Vault ({exc.status_code}) — check the "
            "Crm-Client-Id app registration is provisioned as an Application User in this org."
        )
    if exc.status_code is not None:
        return f"D365 returned {exc.status_code} for WhoAmI against {org_url}."
    # No status code: token acquisition (bad tenant_id, bad client secret)
    # or the org being unreachable. The underlying message carries the
    # actionable part (e.g. an AADSTS code) and holds no secret values.
    return f"Could not complete the D365 check against {org_url}: {exc}"


def _success_detail(org_url: str, auth_mode: str) -> str:
    """A green result should name the hops it actually made — claiming Key
    Vault answered for a direct tenant would be a lie about what was
    proven."""
    if auth_mode == DIRECT_AUTH_MODE:
        return (
            "D365 answered — the registered app registration got a token and WhoAmI "
            f"succeeded against {org_url}."
        )
    return (
        "Key Vault and D365 both answered — the vault returned the D365 app registration "
        f"and WhoAmI succeeded against {org_url}."
    )


async def _run_verification(
    resolver: CrmCredentialResolver, tenant_slug: str, auth_mode: str
) -> tuple[bool, str]:
    """The check itself: a D365 ``WhoAmI``, preceded by the vault hop in
    ``key_vault`` mode. Every failure is an answer — this returns
    ``(ok, detail)`` and never raises for a credential or network
    problem."""
    try:
        connector = await resolver.get_connector(tenant_slug)
    except ConnectorError as exc:
        # The resolver translates KeyVaultError at its boundary; the original
        # is kept as __cause__, and carries the status code that tells an
        # admin whether this is an access policy, a missing secret or worse.
        cause = exc.__cause__
        if isinstance(cause, KeyVaultError):
            return False, describe_vault_error(cause)
        return False, f"Could not read this tenant's D365 credentials: {exc}"
    if connector is None:
        # Raced with a DELETE between the registration check and here.
        return False, "No credentials are registered for this tenant."
    try:
        await connector.who_am_i()
    except ConnectorError as exc:
        return False, _d365_failure_detail(exc, connector.org_url, auth_mode)
    return True, _success_detail(connector.org_url, auth_mode)


@router.post("/{tenant_slug}/credentials/verify", status_code=status.HTTP_200_OK)
async def verify_credentials(
    tenant_slug: str,
    request: Request,
    x_admin_api_key: Annotated[str | None, Header()] = None,
) -> CredentialVerifyResult:
    """Prove the *registered* credentials work, from this process.

    A failing check is an answer, not a server error: everything but a bad
    admin key (401) and an unregistered tenant (404) comes back 200 with
    ``ok=false``.
    """
    settings: Settings = request.app.state.settings
    _require_admin_key(settings, x_admin_api_key)
    cache = request.app.state.crm_cache
    # A verify served from the credential cache proves nothing about the
    # vault *now* — force the vault hop, exactly as upsert/delete do.
    cache.invalidate(tenant_slug)

    started = time.monotonic()
    async with request.app.state.session_factory() as session:
        store = CredentialStore(session, encryption_key=settings.credential_encryption_key)
        credentials = await store.get_credentials(tenant_slug)
        if credentials is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="no credentials registered"
            )
        # Which mode this tenant is in decides how the outcome is phrased —
        # the check itself is the resolver's business, not this route's.
        auth_mode = (
            KEY_VAULT_AUTH_MODE
            if isinstance(credentials, ResolvedKvCredentials)
            else DIRECT_AUTH_MODE
        )
        resolver = CrmCredentialResolver(store, cache)
        try:
            ok, detail = await asyncio.wait_for(
                _run_verification(resolver, tenant_slug, auth_mode), VERIFY_TIMEOUT_SECONDS
            )
        except TimeoutError:
            waited_on = "Key Vault or D365" if auth_mode == KEY_VAULT_AUTH_MODE else "D365"
            ok, detail = False, (
                f"Timed out after {VERIFY_TIMEOUT_SECONDS:.0f}s waiting for {waited_on}."
            )
        except Exception as exc:  # last net: a verify must never 5xx
            # Only the type — an arbitrary exception's message is not
            # guaranteed free of anything sensitive.
            ok, detail = False, f"Verification failed unexpectedly ({type(exc).__name__})."
            log.exception("credentials.verify_unexpected_error", tenant_slug=tenant_slug)

    latency_ms = int((time.monotonic() - started) * 1000)
    log.info("credentials.verified", tenant_slug=tenant_slug, ok=ok, latency_ms=latency_ms)
    return CredentialVerifyResult(ok=ok, detail=detail, latency_ms=latency_ms)


class CustomApiCredentialsIn(BaseModel):
    base_url: HttpUrl = Field(description="Custom API base URL, e.g. https://api.geneco.example")
    username: str = Field(description="Basic auth username for the custom API.")
    password: str = Field(description="Basic auth password for the custom API.")


@router.post("/{tenant_slug}/custom-api-credentials", status_code=status.HTTP_200_OK)
async def upsert_custom_api_credentials(
    tenant_slug: str,
    body: CustomApiCredentialsIn,
    request: Request,
    x_admin_api_key: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    _require_admin_key(settings, x_admin_api_key)
    async with request.app.state.session_factory() as session:
        store = CustomApiCredentialStore(session, encryption_key=settings.credential_encryption_key)
        await store.upsert(
            tenant_slug,
            CustomApiCredentialsInput(
                base_url=str(body.base_url),
                username=body.username,
                password=body.password,
            ),
        )
        redacted = await store.get_redacted(tenant_slug)
    assert redacted is not None
    return redacted


@router.get("/{tenant_slug}/custom-api-credentials")
async def get_custom_api_credentials(
    tenant_slug: str,
    request: Request,
    x_admin_api_key: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    _require_admin_key(settings, x_admin_api_key)
    async with request.app.state.session_factory() as session:
        store = CustomApiCredentialStore(session, encryption_key=settings.credential_encryption_key)
        redacted = await store.get_redacted(tenant_slug)
    if redacted is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no credentials registered")
    return redacted


@router.delete("/{tenant_slug}/custom-api-credentials", status_code=status.HTTP_204_NO_CONTENT)
async def delete_custom_api_credentials(
    tenant_slug: str,
    request: Request,
    x_admin_api_key: Annotated[str | None, Header()] = None,
) -> None:
    settings: Settings = request.app.state.settings
    _require_admin_key(settings, x_admin_api_key)
    async with request.app.state.session_factory() as session:
        store = CustomApiCredentialStore(session, encryption_key=settings.credential_encryption_key)
        deleted = await store.delete(tenant_slug)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no credentials registered")
