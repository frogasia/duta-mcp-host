"""Resolves a ``tenant_slug`` into a ready-to-call ``CustomApiClient``,
taking one of two routes depending on how that tenant's SSP credentials
are stored (``store/custom_api_credentials.py``).

The mirror of ``store/crm_resolver.py`` for the other backend, and for the
same reason: ``key_vault`` mode fetches the tenant's real SSP Basic Auth
pair (``Ssp-User-Id``/``Ssp-Password``) from its Key Vault and caches it
in memory, so the vault is hit once per TTL rather than once per tool
call, and the SSP password exists only in this process's memory. The fetch
is lazy, so a tenant registered after boot works without a restart and a
vault outage degrades only the tenant being called.

``direct`` mode has no vault to hop through: the stored pair *is* the SSP
credential, so the client is built straight from it, and the cache is
neither read nor written.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from geneco_mcp.clients.custom_api_client import ConnectorError, CustomApiClient
from geneco_mcp.clients.keyvault_client import KeyVaultClient, KeyVaultError
from geneco_mcp.store.custom_api_credentials import (
    CustomApiCredentialStore,
    ResolvedDirectCustomApiCredentials,
    ResolvedKvCustomApiCredentials,
)

#: How long a fetched SSP pair stays usable before the vault is consulted
#: again. Same value and same reasoning as the CRM cache's TTL.
SSP_CREDENTIAL_CACHE_TTL_SECONDS = 900


@dataclass(frozen=True, slots=True)
class _CachedSspCredentials:
    username: str
    password: str
    expires_at: float


class SspCredentialCache:
    """Process-local TTL cache of *vault-fetched* SSP Basic Auth pairs.

    Lives on ``app.state`` and is handed to handlers via ``ToolContext``
    rather than reached for as a module-level singleton. Only the
    ``key_vault`` path ever populates it.
    """

    def __init__(self, *, ttl_seconds: float = SSP_CREDENTIAL_CACHE_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[str, _CachedSspCredentials] = {}

    def get(self, tenant_slug: str) -> tuple[str, str] | None:
        entry = self._entries.get(tenant_slug)
        if entry is None:
            return None
        if entry.expires_at <= time.monotonic():
            del self._entries[tenant_slug]
            return None
        return entry.username, entry.password

    def set(self, tenant_slug: str, *, username: str, password: str) -> None:
        self._entries[tenant_slug] = _CachedSspCredentials(
            username=username,
            password=password,
            expires_at=time.monotonic() + self._ttl,
        )

    def invalidate(self, tenant_slug: str) -> None:
        """Drop a tenant's cached pair — called whenever its stored
        credentials change, so a rotation takes effect immediately instead
        of at TTL expiry."""
        self._entries.pop(tenant_slug, None)


class SspCredentialResolver:
    """Turns a ``tenant_slug`` into a ``CustomApiClient`` — going via the
    tenant's Key Vault on a cache miss in ``key_vault`` mode, and straight
    from the stored pair in ``direct`` mode."""

    def __init__(self, store: CustomApiCredentialStore, cache: SspCredentialCache) -> None:
        self._store = store
        self._cache = cache

    async def get_client(self, tenant_slug: str) -> CustomApiClient | None:
        """``None`` when the tenant has no SSP credentials registered.

        Raises ``ConnectorError`` when the tenant *is* configured but its
        vault can't be reached or read — so the tools handle it exactly
        like any other backend failure, never as a raw 500.
        """
        credentials = await self._store.get_credentials(tenant_slug)
        if credentials is None:
            return None
        if isinstance(credentials, ResolvedDirectCustomApiCredentials):
            return CustomApiClient(
                base_url=credentials.base_url,
                username=credentials.username,
                password=credentials.password,
            )
        return await self._client_via_vault(tenant_slug, credentials)

    async def _client_via_vault(
        self, tenant_slug: str, kv: ResolvedKvCustomApiCredentials
    ) -> CustomApiClient:
        cached = self._cache.get(tenant_slug)
        if cached is None:
            client = KeyVaultClient(
                vault_url=kv.vault_url,
                tenant_id=kv.tenant_id,
                client_id=kv.client_id,
                client_secret=kv.client_secret,
            )
            try:
                ssp_username, ssp_password = await client.get_ssp_credentials()
            except KeyVaultError as exc:
                # Translate at the boundary so tools keep catching one
                # error type — an unwrapped KeyVaultError here would reach
                # the router as an unhandled 500.
                raise ConnectorError(f"could not read SSP credentials: {exc}") from exc
            self._cache.set(tenant_slug, username=ssp_username, password=ssp_password)
            cached = (ssp_username, ssp_password)

        username, password = cached
        return CustomApiClient(base_url=kv.base_url, username=username, password=password)
