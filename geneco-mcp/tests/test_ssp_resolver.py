from __future__ import annotations

from unittest.mock import patch

import pytest
import respx
from httpx import Response

from geneco_mcp.clients.custom_api_client import ConnectorError
from geneco_mcp.config import Settings
from geneco_mcp.db import build_engine, build_session_factory
from geneco_mcp.store.custom_api_credentials import (
    CustomApiCredentialsInput,
    CustomApiCredentialStore,
)
from geneco_mcp.store.ssp_resolver import SspCredentialCache, SspCredentialResolver

BASE_URL = "https://ssp.geneco.example"
VAULT_URL = "https://geneco-kv.vault.azure.net"

DIRECT_CREDENTIALS = CustomApiCredentialsInput(
    base_url=BASE_URL,
    username="direct-user",
    password="direct-pass",
)

KV_CREDENTIALS = CustomApiCredentialsInput(
    auth_mode="key_vault",
    base_url=BASE_URL,
    tenant_id="t1",
    kv_vault_url=VAULT_URL,
    kv_client_id="kv1",
    kv_client_secret="kv-secret",
    kv_secret_name_username="Ssp-User-Id",
    kv_secret_name_password="Ssp-Password",
)


@pytest.fixture
async def store(settings: Settings) -> CustomApiCredentialStore:
    engine = build_engine(settings.database_url)
    session_factory = build_session_factory(engine)
    async with session_factory() as session:
        yield CustomApiCredentialStore(
            session, encryption_key=settings.credential_encryption_key
        )
    await engine.dispose()


def _stub_token():
    return patch(
        "geneco_mcp.clients.keyvault_client.KeyVaultClient._acquire_token",
        return_value="fake-kv-token",
    )


def _mock_vault() -> tuple[respx.Route, respx.Route]:
    user_route = respx.get(f"{VAULT_URL}/secrets/Ssp-User-Id").mock(
        return_value=Response(200, json={"value": "vault-user"})
    )
    password_route = respx.get(f"{VAULT_URL}/secrets/Ssp-Password").mock(
        return_value=Response(200, json={"value": "vault-pass"})
    )
    return user_route, password_route


async def test_unregistered_tenant_is_none_not_an_error(
    store: CustomApiCredentialStore,
) -> None:
    resolver = SspCredentialResolver(store, SspCredentialCache())
    assert await resolver.get_client("unknown-tenant") is None


@respx.mock
async def test_fetches_ssp_credentials_from_vault(store: CustomApiCredentialStore) -> None:
    await store.upsert("geneco", KV_CREDENTIALS)
    _mock_vault()
    resolver = SspCredentialResolver(store, SspCredentialCache())

    with _stub_token():
        client = await resolver.get_client("geneco")

    assert client is not None
    assert client.username == "vault-user"
    assert client.password == "vault-pass"
    assert client.base_url == BASE_URL


@respx.mock
async def test_second_call_is_served_from_cache(store: CustomApiCredentialStore) -> None:
    await store.upsert("geneco", KV_CREDENTIALS)
    user_route, _ = _mock_vault()
    resolver = SspCredentialResolver(store, SspCredentialCache())

    with _stub_token():
        await resolver.get_client("geneco")
        await resolver.get_client("geneco")

    assert user_route.call_count == 1


@respx.mock
async def test_expired_cache_refetches(store: CustomApiCredentialStore) -> None:
    await store.upsert("geneco", KV_CREDENTIALS)
    user_route, _ = _mock_vault()
    # ttl=0 expires the entry the moment it's written.
    resolver = SspCredentialResolver(store, SspCredentialCache(ttl_seconds=0))

    with _stub_token():
        await resolver.get_client("geneco")
        await resolver.get_client("geneco")

    assert user_route.call_count == 2


@respx.mock
async def test_invalidate_forces_refetch(store: CustomApiCredentialStore) -> None:
    await store.upsert("geneco", KV_CREDENTIALS)
    user_route, _ = _mock_vault()
    cache = SspCredentialCache()
    resolver = SspCredentialResolver(store, cache)

    with _stub_token():
        await resolver.get_client("geneco")
        cache.invalidate("geneco")
        await resolver.get_client("geneco")

    assert user_route.call_count == 2


@respx.mock
async def test_vault_failure_raises_connector_error_not_keyvault_error(
    store: CustomApiCredentialStore,
) -> None:
    """The resolver translates at the boundary so tool handlers keep
    catching a single error type — an unwrapped KeyVaultError here would
    reach the router as a raw 500."""
    await store.upsert("geneco", KV_CREDENTIALS)
    respx.get(f"{VAULT_URL}/secrets/Ssp-User-Id").mock(return_value=Response(403, json={}))
    resolver = SspCredentialResolver(store, SspCredentialCache())

    with _stub_token(), pytest.raises(ConnectorError):
        await resolver.get_client("geneco")


@respx.mock
async def test_failed_fetch_is_not_cached(store: CustomApiCredentialStore) -> None:
    await store.upsert("geneco", KV_CREDENTIALS)
    failing = respx.get(f"{VAULT_URL}/secrets/Ssp-User-Id").mock(
        return_value=Response(500, json={})
    )
    resolver = SspCredentialResolver(store, SspCredentialCache())

    with _stub_token():
        for _ in range(2):
            with pytest.raises(ConnectorError):
                await resolver.get_client("geneco")

    assert failing.call_count == 2


@respx.mock
async def test_direct_mode_builds_a_client_without_touching_the_vault(
    store: CustomApiCredentialStore,
) -> None:
    await store.upsert("geneco", DIRECT_CREDENTIALS)
    user_route, _ = _mock_vault()
    resolver = SspCredentialResolver(store, SspCredentialCache())

    client = await resolver.get_client("geneco")

    assert client is not None
    assert (client.username, client.password) == ("direct-user", "direct-pass")
    assert user_route.call_count == 0


@respx.mock
async def test_direct_mode_ignores_a_cache_entry_left_by_key_vault_mode(
    store: CustomApiCredentialStore,
) -> None:
    """The cache exists to save a vault round-trip; a direct row has none
    to save, and must never serve the previous mode's pair."""
    cache = SspCredentialCache()
    cache.set("geneco", username="stale-vault-user", password="stale-vault-pass")
    await store.upsert("geneco", DIRECT_CREDENTIALS)
    resolver = SspCredentialResolver(store, cache)

    client = await resolver.get_client("geneco")

    assert client is not None
    assert client.username == "direct-user"


async def test_switching_modes_clears_the_other_shape(
    store: CustomApiCredentialStore,
) -> None:
    """A full replace: a vault row must not leave a stored pair behind
    that a later switch back to direct would silently resolve."""
    await store.upsert("geneco", DIRECT_CREDENTIALS)
    await store.upsert("geneco", KV_CREDENTIALS)

    redacted = await store.get_redacted("geneco")

    assert redacted is not None
    assert redacted["auth_mode"] == "key_vault"
    assert "username" not in redacted
    assert "password" not in redacted
