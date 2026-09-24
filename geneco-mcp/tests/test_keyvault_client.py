from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
import respx
from httpx import Response

from geneco_mcp.clients.keyvault_client import KeyVaultClient, KeyVaultError

VAULT_URL = "https://geneco-kv.vault.azure.net"


@pytest.fixture
def vault_client() -> KeyVaultClient:
    return KeyVaultClient(
        vault_url=VAULT_URL, tenant_id="t1", client_id="kv1", client_secret="kvs1"
    )


def _stub_token(client: KeyVaultClient):
    """msal's own token handshake isn't this project's wire shape — stub it
    so these tests exercise our request/response handling instead."""
    return patch.object(client, "_acquire_token", return_value="fake-kv-token")


@respx.mock
async def test_get_secret_returns_value(vault_client: KeyVaultClient) -> None:
    route = respx.get(f"{VAULT_URL}/secrets/Crm-Client-Id").mock(
        return_value=Response(200, json={"value": "crm-id", "id": "..."})
    )
    with _stub_token(vault_client):
        assert await vault_client.get_secret("Crm-Client-Id") == "crm-id"
    assert route.calls.last.request.url.params["api-version"] == "7.4"


@respx.mock
async def test_get_crm_credentials_reads_the_names_it_is_given(
    vault_client: KeyVaultClient,
) -> None:
    respx.get(f"{VAULT_URL}/secrets/Crm-Client-Id").mock(
        return_value=Response(200, json={"value": "crm-id"})
    )
    respx.get(f"{VAULT_URL}/secrets/Crm-Client-Secret").mock(
        return_value=Response(200, json={"value": "crm-secret"})
    )
    with _stub_token(vault_client):
        assert await vault_client.get_crm_credentials(
            client_id_secret="Crm-Client-Id", client_secret_secret="Crm-Client-Secret"
        ) == ("crm-id", "crm-secret")


async def test_secret_names_have_no_defaults(vault_client: KeyVaultClient) -> None:
    """This module must not know any tenant's naming. If a default ever
    creeps back onto these signatures, a caller that forgot to pass the
    tenant's own names would silently read someone else's."""
    with pytest.raises(TypeError):
        await vault_client.get_crm_credentials()
    with pytest.raises(TypeError):
        await vault_client.get_ssp_credentials()


@respx.mock
async def test_network_failure_raises_keyvault_error(vault_client: KeyVaultClient) -> None:
    respx.get(f"{VAULT_URL}/secrets/Crm-Client-Id").mock(side_effect=httpx.ConnectError("boom"))
    with _stub_token(vault_client), pytest.raises(KeyVaultError):
        await vault_client.get_secret("Crm-Client-Id")


@respx.mock
async def test_403_raises_keyvault_error_not_uncaught(vault_client: KeyVaultClient) -> None:
    """The access-policy-denied case — must surface as a handled error."""
    respx.get(f"{VAULT_URL}/secrets/Crm-Client-Id").mock(
        return_value=Response(403, text="Forbidden")
    )
    with _stub_token(vault_client), pytest.raises(KeyVaultError) as exc:
        await vault_client.get_secret("Crm-Client-Id")
    assert exc.value.retryable is False


@respx.mock
async def test_malformed_body_raises_keyvault_error(vault_client: KeyVaultClient) -> None:
    respx.get(f"{VAULT_URL}/secrets/Crm-Client-Id").mock(
        return_value=Response(200, text="not json")
    )
    with _stub_token(vault_client), pytest.raises(KeyVaultError):
        await vault_client.get_secret("Crm-Client-Id")


@respx.mock
async def test_missing_value_raises_keyvault_error(vault_client: KeyVaultClient) -> None:
    respx.get(f"{VAULT_URL}/secrets/Crm-Client-Id").mock(
        return_value=Response(200, json={"id": "...", "attributes": {}})
    )
    with _stub_token(vault_client), pytest.raises(KeyVaultError):
        await vault_client.get_secret("Crm-Client-Id")
