"""Resolves a ``tenant_slug`` into a ready-to-call ``D365Connector``,
taking one of two routes depending on how that tenant's credentials are
stored (``store/credentials.py``).

``key_vault`` mode fetches the tenant's real D365 app registration from
its Key Vault and caches the result in memory so the vault is hit once per
TTL rather than once per tool call. This is the layer that keeps
``Crm-Client-Secret`` out of the database: the DB holds only the tenant's
*vault* credentials, and the D365 secret exists solely in this process's
memory, for at most ``CRM_CREDENTIAL_CACHE_TTL_SECONDS``. The fetch is
lazy — on a tenant's first call, not at startup — so a tenant registered
after boot works without a restart, a vault outage degrades only the
tenant being called rather than the whole service, and a rotated secret is
picked up within the TTL.

``direct`` mode has no vault to hop through: the stored pair *is* the D365
app registration, so the connector is built straight from it. It
deliberately neither reads nor writes the cache — the cache exists to
avoid a vault round-trip, and there is no round-trip here to avoid.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from geneco_mcp.clients.d365_connector import ConnectorError, D365Connector
from geneco_mcp.clients.keyvault_client import KeyVaultClient, KeyVaultError
from geneco_mcp.store.credentials import (
    CredentialStore,
    ResolvedDirectCredentials,
    ResolvedKvCredentials,
)

#: How long a fetched D365 app registration stays usable before the vault
#: is consulted again. Long enough that vault calls are rare, short enough
#: that a rotation takes effect without a restart. Injectable rather than
#: a settings knob (see YAGNI in ``CLAUDE.md``) — tests pass their own.
CRM_CREDENTIAL_CACHE_TTL_SECONDS = 900


@dataclass(frozen=True, slots=True)
class _CachedCrmCredentials:
    client_id: str
    client_secret: str
    expires_at: float


class CrmCredentialCache:
    """Process-local TTL cache of *vault-fetched* D365 app registrations.

    Lives on ``app.state`` and is handed to handlers via ``ToolContext``
    rather than reached for as a module-level singleton. Only the
    ``key_vault`` path ever populates it.
    """

    def __init__(self, *, ttl_seconds: float = CRM_CREDENTIAL_CACHE_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[str, _CachedCrmCredentials] = {}

    def get(self, tenant_slug: str) -> tuple[str, str] | None:
        entry = self._entries.get(tenant_slug)
        if entry is None:
            return None
        if entry.expires_at <= time.monotonic():
            del self._entries[tenant_slug]
            return None
        return entry.client_id, entry.client_secret

    def set(self, tenant_slug: str, *, client_id: str, client_secret: str) -> None:
        self._entries[tenant_slug] = _CachedCrmCredentials(
            client_id=client_id,
            client_secret=client_secret,
            expires_at=time.monotonic() + self._ttl,
        )

    def invalidate(self, tenant_slug: str) -> None:
        """Drop a tenant's cached credentials — called whenever its stored
        vault credentials change, so a rotation takes effect immediately
        instead of at TTL expiry."""
        self._entries.pop(tenant_slug, None)


class CrmCredentialResolver:
    """Turns a ``tenant_slug`` into a ``D365Connector`` — going via the
    tenant's Key Vault on a cache miss in ``key_vault`` mode, and straight
    from the stored pair in ``direct`` mode."""

    def __init__(self, store: CredentialStore, cache: CrmCredentialCache) -> None:
        self._store = store
        self._cache = cache

    async def get_connector(self, tenant_slug: str) -> D365Connector | None:
        """``None`` when the tenant has no credentials registered.

        Raises ``ConnectorError`` when the tenant *is* configured but its
        vault can't be reached or read — so callers handle it exactly like
        any other backend failure, never as a raw 500. A ``direct`` tenant
        has no vault hop, so this path can only raise for it once the
        connector is actually called.
        """
        credentials = await self._store.get_credentials(tenant_slug)
        if credentials is None:
            return None
        if isinstance(credentials, ResolvedDirectCredentials):
            return D365Connector(
                org_url=credentials.org_url,
                tenant_id=credentials.tenant_id,
                client_id=credentials.client_id,
                client_secret=credentials.client_secret,
                field_map=credentials.field_map,
            )
        return await self._connector_via_vault(tenant_slug, credentials)

    async def _connector_via_vault(
        self, tenant_slug: str, kv: ResolvedKvCredentials
    ) -> D365Connector:
        cached = self._cache.get(tenant_slug)
        if cached is None:
            client = KeyVaultClient(
                vault_url=kv.vault_url,
                tenant_id=kv.tenant_id,
                client_id=kv.client_id,
                client_secret=kv.client_secret,
            )
            try:
                crm_client_id, crm_client_secret = await client.get_crm_credentials()
            except KeyVaultError as exc:
                # Translate at the boundary so tools keep catching one
                # error type — an unwrapped KeyVaultError here would reach
                # the router as an unhandled 500.
                raise ConnectorError(f"could not read D365 credentials: {exc}") from exc
            self._cache.set(
                tenant_slug, client_id=crm_client_id, client_secret=crm_client_secret
            )
            cached = (crm_client_id, crm_client_secret)

        client_id, client_secret = cached
        return D365Connector(
            org_url=kv.org_url,
            tenant_id=kv.tenant_id,
            client_id=client_id,
            client_secret=client_secret,
            field_map=kv.field_map,
        )
