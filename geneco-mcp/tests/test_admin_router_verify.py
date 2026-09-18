"""``POST /{tenant_slug}/credentials/verify`` — the endpoint that answers
"can *this* service use the registered credentials right now?".

The rule under test throughout: a credential or network failure is an
ANSWER (200 + ``ok=false``), never a 5xx. Only a bad admin key (401) and
an unregistered tenant (404) are non-200.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from unittest.mock import patch

import httpx
import respx
from fastapi.testclient import TestClient
from httpx import Response

from conftest import TEST_ADMIN_API_KEY

ORG_URL = "https://geneco.crm.dynamics.com"
KV_URL = "https://geneco-kv.vault.azure.net"
WHOAMI_URL = f"{ORG_URL}/api/data/v9.2/WhoAmI"
VAULT_SECRET = "kv-secret-value"

AUTH = {"X-Admin-Api-Key": TEST_ADMIN_API_KEY}


def _mock_vault() -> respx.Route:
    id_route = respx.get(f"{KV_URL}/secrets/Crm-Client-Id").mock(
        return_value=Response(200, json={"value": "crm-client-id"})
    )
    respx.get(f"{KV_URL}/secrets/Crm-Client-Secret").mock(
        return_value=Response(200, json={"value": "crm-client-secret"})
    )
    return id_route


def _mock_whoami(response: Response | None = None) -> respx.Route:
    return respx.get(WHOAMI_URL).mock(
        return_value=response
        if response is not None
        else Response(200, json={"UserId": "u-1", "OrganizationId": "o-1"})
    )


@contextmanager
def _stub_tokens():
    """Both MSAL handshakes are msal's machinery, not this project's wire
    shape — stub them rather than mocking over HTTP."""
    with (
        patch(
            "geneco_mcp.clients.keyvault_client.KeyVaultClient._acquire_token",
            return_value="fake-kv-token",
        ),
        patch(
            "geneco_mcp.clients.d365_connector.D365Connector._acquire_token",
            return_value="fake-token",
        ),
    ):
        yield


def _seed_credentials(client: TestClient, tenant_slug: str = "geneco") -> None:
    resp = client.post(
        f"/{tenant_slug}/credentials",
        headers=AUTH,
        json={
            "org_url": ORG_URL,
            "tenant_id": "t1",
            "kv_vault_url": KV_URL,
            "kv_client_id": "kv1",
            "kv_client_secret": VAULT_SECRET,
        },
    )
    assert resp.status_code == 200


@respx.mock
def test_happy_path_reports_ok_with_the_org_that_answered(client: TestClient) -> None:
    _seed_credentials(client)
    _mock_vault()
    _mock_whoami()

    with _stub_tokens():
        resp = client.post("/geneco/credentials/verify", headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert ORG_URL in body["detail"]
    assert isinstance(body["latency_ms"], int)


def test_verify_requires_admin_key(client: TestClient) -> None:
    _seed_credentials(client)
    assert client.post("/geneco/credentials/verify").status_code == 401


def test_verify_rejects_wrong_admin_key(client: TestClient) -> None:
    _seed_credentials(client)
    resp = client.post("/geneco/credentials/verify", headers={"X-Admin-Api-Key": "wrong-key"})
    assert resp.status_code == 401


def test_unregistered_tenant_is_404(client: TestClient) -> None:
    resp = client.post("/unknown-tenant/credentials/verify", headers=AUTH)
    assert resp.status_code == 404


@respx.mock
def test_vault_unreachable_is_200_not_5xx(client: TestClient) -> None:
    _seed_credentials(client)
    respx.get(f"{KV_URL}/secrets/Crm-Client-Id").mock(side_effect=httpx.ConnectError("boom"))

    with _stub_tokens():
        resp = client.post("/geneco/credentials/verify", headers=AUTH)

    assert resp.status_code == 200
    assert resp.json()["ok"] is False


@respx.mock
def test_vault_403_explains_the_access_policy(client: TestClient) -> None:
    _seed_credentials(client)
    respx.get(f"{KV_URL}/secrets/Crm-Client-Id").mock(return_value=Response(403, text="Forbidden"))

    with _stub_tokens():
        resp = client.post("/geneco/credentials/verify", headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "denied access" in body["detail"]
    assert "Crm-Client-Id" in body["detail"]


@respx.mock
def test_missing_vault_secret_says_so(client: TestClient) -> None:
    _seed_credentials(client)
    respx.get(f"{KV_URL}/secrets/Crm-Client-Id").mock(return_value=Response(404, text="NotFound"))

    with _stub_tokens():
        resp = client.post("/geneco/credentials/verify", headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "no secret" in body["detail"]


@respx.mock
def test_d365_rejecting_the_token_is_200_with_an_application_user_hint(
    client: TestClient,
) -> None:
    _seed_credentials(client)
    _mock_vault()
    _mock_whoami(Response(403, text="Forbidden"))

    with _stub_tokens():
        resp = client.post("/geneco/credentials/verify", headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "Application User" in body["detail"]


@respx.mock
def test_d365_unreachable_is_200_not_5xx(client: TestClient) -> None:
    _seed_credentials(client)
    _mock_vault()
    respx.get(WHOAMI_URL).mock(side_effect=httpx.ConnectError("boom"))

    with _stub_tokens():
        resp = client.post("/geneco/credentials/verify", headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert ORG_URL in body["detail"]


@respx.mock
def test_verify_never_exposes_a_secret_value(client: TestClient) -> None:
    """Neither the stored vault secret nor the vault-fetched D365 one may
    appear anywhere in the response."""
    _seed_credentials(client)
    _mock_vault()
    _mock_whoami()

    with _stub_tokens():
        resp = client.post("/geneco/credentials/verify", headers=AUTH)

    assert resp.status_code == 200
    assert VAULT_SECRET not in resp.text
    assert "crm-client-secret" not in resp.text


@respx.mock
def test_verify_never_exposes_a_secret_value_on_failure(client: TestClient) -> None:
    _seed_credentials(client)
    # A vault that echoes something secret-shaped back in its error body.
    respx.get(f"{KV_URL}/secrets/Crm-Client-Id").mock(
        return_value=Response(500, text=f"upstream said {VAULT_SECRET}")
    )

    with _stub_tokens():
        resp = client.post("/geneco/credentials/verify", headers=AUTH)

    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert VAULT_SECRET not in resp.text


@respx.mock
def test_verify_bypasses_the_credential_cache(client: TestClient) -> None:
    """A verify served from the cached D365 app registration would prove
    nothing about the vault *now*, so every verify re-reads it."""
    _seed_credentials(client)
    id_route = _mock_vault()
    _mock_whoami()

    with _stub_tokens():
        client.post("/geneco/credentials/verify", headers=AUTH)
        client.post("/geneco/credentials/verify", headers=AUTH)

    assert id_route.call_count == 2


def test_a_hanging_backend_times_out_as_ok_false(client: TestClient) -> None:
    _seed_credentials(client)

    async def _hang(*_args, **_kwargs):
        await asyncio.sleep(5)

    with (
        patch("geneco_mcp.protocol.admin_router.VERIFY_TIMEOUT_SECONDS", 0.05),
        patch("geneco_mcp.store.crm_resolver.CrmCredentialResolver.get_connector", _hang),
    ):
        resp = client.post("/geneco/credentials/verify", headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "Timed out" in body["detail"]


def test_an_unexpected_error_is_still_200(client: TestClient) -> None:
    """The last net: even a bug in the check itself must not 5xx an admin's
    'Test connection' click."""
    _seed_credentials(client)

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("unexpected")

    with patch("geneco_mcp.store.crm_resolver.CrmCredentialResolver.get_connector", _boom):
        resp = client.post("/geneco/credentials/verify", headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "RuntimeError" in body["detail"]

