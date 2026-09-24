from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
import respx
from httpx import Response

from geneco_mcp.clients.d365_connector import ConnectorError
from geneco_mcp.config import Settings
from geneco_mcp.db import build_engine, build_session_factory
from geneco_mcp.store.credentials import CredentialStore, D365CredentialsInput
from geneco_mcp.store.crm_resolver import CrmCredentialCache, CrmCredentialResolver

ORG_URL = "https://geneco.crm.dynamics.com"
VAULT_URL = "https://geneco-kv.vault.azure.net"

CREDENTIALS = D365CredentialsInput(
    org_url=ORG_URL,
    tenant_id="t1",
    kv_vault_url=VAULT_URL,
    kv_client_id="kv1",
    kv_client_secret="kv-secret",
    kv_secret_name_client_id="Crm-Client-Id",
    kv_secret_name_client_secret="Crm-Client-Secret",
)

DIRECT_CREDENTIALS = D365CredentialsInput(
    auth_mode="direct",
    org_url=ORG_URL,
    tenant_id="t1",
    client_id="direct-id",
    client_secret="direct-secret",
)


@pytest.fixture
async def store(settings: Settings) -> CredentialStore:
    engine = build_engine(settings.database_url)
    session_factory = build_session_factory(engine)
    async with session_factory() as session:
        yield CredentialStore(session, encryption_key=settings.credential_encryption_key)
    await engine.dispose()


def _stub_token():
    return patch(
        "geneco_mcp.clients.keyvault_client.KeyVaultClient._acquire_token",
        return_value="fake-kv-token",
    )


def _mock_vault() -> tuple[respx.Route, respx.Route]:
    id_route = respx.get(f"{VAULT_URL}/secrets/Crm-Client-Id").mock(
        return_value=Response(200, json={"value": "crm-id"})
    )
    secret_route = respx.get(f"{VAULT_URL}/secrets/Crm-Client-Secret").mock(
        return_value=Response(200, json={"value": "crm-secret"})
    )
    return id_route, secret_route


async def test_unregistered_tenant_is_none_not_an_error(store: CredentialStore) -> None:
    resolver = CrmCredentialResolver(store, CrmCredentialCache())
    assert await resolver.get_connector("unknown-tenant") is None


@respx.mock
async def test_fetches_crm_credentials_from_vault(store: CredentialStore) -> None:
    await store.upsert("geneco", CREDENTIALS)
    _mock_vault()
    resolver = CrmCredentialResolver(store, CrmCredentialCache())

    with _stub_token():
        connector = await resolver.get_connector("geneco")

    assert connector is not None
    assert connector.client_id == "crm-id"
    assert connector.client_secret == "crm-secret"
    assert connector.org_url == ORG_URL


@respx.mock
async def test_second_call_is_served_from_cache(store: CredentialStore) -> None:
    await store.upsert("geneco", CREDENTIALS)
    id_route, _ = _mock_vault()
    resolver = CrmCredentialResolver(store, CrmCredentialCache())

    with _stub_token():
        await resolver.get_connector("geneco")
        await resolver.get_connector("geneco")

    assert id_route.call_count == 1


@respx.mock
async def test_expired_cache_refetches(store: CredentialStore) -> None:
    await store.upsert("geneco", CREDENTIALS)
    id_route, _ = _mock_vault()
    # ttl=0 expires the entry the moment it's written.
    resolver = CrmCredentialResolver(store, CrmCredentialCache(ttl_seconds=0))

    with _stub_token():
        await resolver.get_connector("geneco")
        await resolver.get_connector("geneco")

    assert id_route.call_count == 2


@respx.mock
async def test_invalidate_forces_refetch(store: CredentialStore) -> None:
    await store.upsert("geneco", CREDENTIALS)
    id_route, _ = _mock_vault()
    cache = CrmCredentialCache()
    resolver = CrmCredentialResolver(store, cache)

    with _stub_token():
        await resolver.get_connector("geneco")
        cache.invalidate("geneco")
        await resolver.get_connector("geneco")

    assert id_route.call_count == 2


@respx.mock
async def test_vault_failure_raises_connector_error_not_keyvault_error(
    store: CredentialStore,
) -> None:
    """The resolver translates at the boundary so tool handlers keep
    catching a single error type — an unwrapped KeyVaultError here would
    reach the router as a raw 500."""
    await store.upsert("geneco", CREDENTIALS)
    respx.get(f"{VAULT_URL}/secrets/Crm-Client-Id").mock(side_effect=httpx.ConnectError("boom"))
    resolver = CrmCredentialResolver(store, CrmCredentialCache())

    with _stub_token(), pytest.raises(ConnectorError):
        await resolver.get_connector("geneco")


@respx.mock
async def test_failed_fetch_is_not_cached(store: CredentialStore) -> None:
    await store.upsert("geneco", CREDENTIALS)
    failing = respx.get(f"{VAULT_URL}/secrets/Crm-Client-Id").mock(
        side_effect=httpx.ConnectError("boom")
    )
    cache = CrmCredentialCache()
    resolver = CrmCredentialResolver(store, cache)

    with _stub_token():
        with pytest.raises(ConnectorError):
            await resolver.get_connector("geneco")
        assert cache.get("geneco") is None
        with pytest.raises(ConnectorError):
            await resolver.get_connector("geneco")

    assert failing.call_count == 2


async def test_cache_is_per_tenant() -> None:
    cache = CrmCredentialCache()
    cache.set("a", client_id="id-a", client_secret="secret-a")
    cache.set("b", client_id="id-b", client_secret="secret-b")

    assert cache.get("a") == ("id-a", "secret-a")
    assert cache.get("b") == ("id-b", "secret-b")

    cache.invalidate("a")
    assert cache.get("a") is None
    assert cache.get("b") == ("id-b", "secret-b")


@respx.mock
async def test_direct_mode_builds_a_connector_without_touching_the_vault(
    store: CredentialStore,
) -> None:
    """No vault route is mocked here on purpose: respx fails any unmocked
    request, so this passing *is* the assertion that no vault call was
    made."""
    await store.upsert("geneco", DIRECT_CREDENTIALS)
    resolver = CrmCredentialResolver(store, CrmCredentialCache())

    connector = await resolver.get_connector("geneco")

    assert connector is not None
    assert connector.client_id == "direct-id"
    assert connector.client_secret == "direct-secret"
    assert connector.org_url == ORG_URL


async def test_direct_mode_never_writes_the_cache(store: CredentialStore) -> None:
    """The cache exists to avoid a vault round-trip; direct mode has none to
    avoid, and a D365 secret has no reason to sit in it."""
    await store.upsert("geneco", DIRECT_CREDENTIALS)
    cache = CrmCredentialCache()
    resolver = CrmCredentialResolver(store, cache)

    await resolver.get_connector("geneco")

    assert cache.get("geneco") is None


async def test_direct_mode_ignores_a_cache_entry_left_by_key_vault_mode(
    store: CredentialStore,
) -> None:
    """A tenant that switched from vault to direct may still have a cached
    vault-fetched pair under its slug until the TTL expires — the direct
    path must use the stored credentials, not that."""
    await store.upsert("geneco", DIRECT_CREDENTIALS)
    cache = CrmCredentialCache()
    cache.set("geneco", client_id="stale-vault-id", client_secret="stale-vault-secret")
    resolver = CrmCredentialResolver(store, cache)

    connector = await resolver.get_connector("geneco")

    assert connector is not None
    assert connector.client_id == "direct-id"
    assert connector.client_secret == "direct-secret"


@respx.mock
async def test_switching_to_direct_mode_stops_using_the_vault(store: CredentialStore) -> None:
    """End to end through the store: a re-registered tenant resolves the new
    way, with the old vault credentials gone from the row."""
    await store.upsert("geneco", CREDENTIALS)
    id_route, _ = _mock_vault()
    resolver = CrmCredentialResolver(store, CrmCredentialCache())
    with _stub_token():
        await resolver.get_connector("geneco")
    assert id_route.call_count == 1

    await store.upsert("geneco", DIRECT_CREDENTIALS)
    connector = await resolver.get_connector("geneco")

    assert connector is not None
    assert connector.client_id == "direct-id"
    assert id_route.call_count == 1
