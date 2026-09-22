from __future__ import annotations

from fastapi.testclient import TestClient

from conftest import TEST_ADMIN_API_KEY

CREDENTIALS_BODY = {
    "base_url": "https://custom-api.geneco.example",
    "username": "cxchat-svc",
    "password": "super-secret-value",
}


def test_upsert_requires_admin_key(ssp_client: TestClient) -> None:
    resp = ssp_client.post("/geneco/credentials", json=CREDENTIALS_BODY)
    assert resp.status_code == 401


def test_upsert_rejects_wrong_admin_key(ssp_client: TestClient) -> None:
    resp = ssp_client.post(
        "/geneco/credentials",
        headers={"X-Admin-Api-Key": "wrong-key"},
        json=CREDENTIALS_BODY,
    )
    assert resp.status_code == 401


def test_upsert_then_get_never_exposes_the_real_secret(ssp_client: TestClient) -> None:
    resp = ssp_client.post(
        "/geneco/credentials",
        headers={"X-Admin-Api-Key": TEST_ADMIN_API_KEY},
        json=CREDENTIALS_BODY,
    )
    assert resp.status_code == 200
    assert resp.json()["password"] == "•••"
    assert "super-secret-value" not in resp.text

    get_resp = ssp_client.get(
        "/geneco/credentials", headers={"X-Admin-Api-Key": TEST_ADMIN_API_KEY}
    )
    assert get_resp.status_code == 200
    assert get_resp.json()["password"] == "•••"
    assert "super-secret-value" not in get_resp.text


def test_get_missing_tenant_is_404(ssp_client: TestClient) -> None:
    resp = ssp_client.get(
        "/unknown-tenant/credentials", headers={"X-Admin-Api-Key": TEST_ADMIN_API_KEY}
    )
    assert resp.status_code == 404


def test_delete_then_get_is_404(ssp_client: TestClient) -> None:
    ssp_client.post(
        "/geneco/credentials",
        headers={"X-Admin-Api-Key": TEST_ADMIN_API_KEY},
        json=CREDENTIALS_BODY,
    )
    del_resp = ssp_client.delete(
        "/geneco/credentials", headers={"X-Admin-Api-Key": TEST_ADMIN_API_KEY}
    )
    assert del_resp.status_code == 204
    get_resp = ssp_client.get(
        "/geneco/credentials", headers={"X-Admin-Api-Key": TEST_ADMIN_API_KEY}
    )
    assert get_resp.status_code == 404


def test_delete_missing_tenant_is_404(ssp_client: TestClient) -> None:
    resp = ssp_client.delete(
        "/unknown-tenant/credentials", headers={"X-Admin-Api-Key": TEST_ADMIN_API_KEY}
    )
    assert resp.status_code == 404


def test_crm_and_ssp_credentials_are_independent(
    ssp_client: TestClient, crm_client: TestClient
) -> None:
    """Same path, same tenant, two backends — and revoking one leaves the
    other registered. The two endpoints only look alike; they are separate
    rows in separate tables, reached through separate MCP servers."""
    d365_body = {
        "org_url": "https://geneco.crm.dynamics.com",
        "tenant_id": "77dee05b-8ff2-4aee-81a7-461ed9ab3456",
        "kv_vault_url": "https://geneco-kv.vault.azure.net",
        "kv_client_id": "77fa3ab9-8247-48e4-8ad5-5fc959ef7f24",
        "kv_client_secret": "d365-secret",
    }
    crm_client.post(
        "/geneco/credentials", headers={"X-Admin-Api-Key": TEST_ADMIN_API_KEY}, json=d365_body
    )
    ssp_client.post(
        "/geneco/credentials",
        headers={"X-Admin-Api-Key": TEST_ADMIN_API_KEY},
        json=CREDENTIALS_BODY,
    )

    del_resp = ssp_client.delete(
        "/geneco/credentials", headers={"X-Admin-Api-Key": TEST_ADMIN_API_KEY}
    )
    assert del_resp.status_code == 204

    d365_get = crm_client.get(
        "/geneco/credentials", headers={"X-Admin-Api-Key": TEST_ADMIN_API_KEY}
    )
    assert d365_get.status_code == 200


def test_crm_endpoint_rejects_an_ssp_credentials_body(crm_client: TestClient) -> None:
    """The shared path carries different bodies per server — posting one to
    the other is a 422, not a half-written row."""
    resp = crm_client.post(
        "/geneco/credentials",
        headers={"X-Admin-Api-Key": TEST_ADMIN_API_KEY},
        json=CREDENTIALS_BODY,
    )
    assert resp.status_code == 422


def test_both_servers_expose_a_verify_route(
    ssp_client: TestClient, crm_client: TestClient
) -> None:
    """Both answer 404 for an unregistered tenant rather than 405 — i.e.
    the route exists on each. What each verify can *prove* differs; see
    ``test_ssp_admin_router_verify.py``."""
    auth = {"X-Admin-Api-Key": TEST_ADMIN_API_KEY}
    assert ssp_client.post("/nobody/credentials/verify", headers=auth).status_code == 404
    assert crm_client.post("/nobody/credentials/verify", headers=auth).status_code == 404


# — key_vault mode —

KV_BODY = {
    "auth_mode": "key_vault",
    "base_url": "https://custom-api.geneco.example",
    "tenant_id": "00000000-0000-0000-0000-000000000000",
    "kv_vault_url": "https://geneco-kv.vault.azure.net",
    "kv_client_id": "kv-app-id",
    "kv_client_secret": "kv-super-secret-value",
}
AUTH = {"X-Admin-Api-Key": TEST_ADMIN_API_KEY}


def test_direct_is_the_default_auth_mode(ssp_client: TestClient) -> None:
    """A body with no auth_mode is a pre-vault-mode caller registering a
    stored Basic Auth pair, and must keep meaning that."""
    resp = ssp_client.post("/geneco/credentials", headers=AUTH, json=CREDENTIALS_BODY)
    assert resp.status_code == 200
    assert resp.json()["auth_mode"] == "direct"


def test_key_vault_mode_masks_the_vault_secret(ssp_client: TestClient) -> None:
    resp = ssp_client.post("/geneco/credentials", headers=AUTH, json=KV_BODY)
    assert resp.status_code == 200
    body = resp.json()
    assert body["auth_mode"] == "key_vault"
    assert body["kv_client_secret"] == "•••"
    assert "kv-super-secret-value" not in resp.text
    # The other mode's columns are NULL, so echoing them would only
    # suggest there is a stored pair behind them.
    assert "username" not in body
    assert "password" not in body


def test_key_vault_mode_requires_a_tenant_id(ssp_client: TestClient) -> None:
    """The Entra tenant GUID is the vault token's authority — without it
    the vault hop cannot even be attempted."""
    resp = ssp_client.post(
        "/geneco/credentials",
        headers=AUTH,
        json={k: v for k, v in KV_BODY.items() if k != "tenant_id"},
    )
    assert resp.status_code == 422


def test_direct_mode_rejects_vault_fields(ssp_client: TestClient) -> None:
    resp = ssp_client.post(
        "/geneco/credentials",
        headers=AUTH,
        json={**CREDENTIALS_BODY, "kv_vault_url": "https://geneco-kv.vault.azure.net"},
    )
    assert resp.status_code == 422


def test_key_vault_mode_rejects_a_stored_pair(ssp_client: TestClient) -> None:
    resp = ssp_client.post(
        "/geneco/credentials", headers=AUTH, json={**KV_BODY, "username": "cxchat-svc"}
    )
    assert resp.status_code == 422


def test_an_unknown_auth_mode_is_rejected(ssp_client: TestClient) -> None:
    resp = ssp_client.post(
        "/geneco/credentials", headers=AUTH, json={**CREDENTIALS_BODY, "auth_mode": "sideways"}
    )
    assert resp.status_code == 422


def test_a_422_never_echoes_the_posted_vault_secret(ssp_client: TestClient) -> None:
    resp = ssp_client.post(
        "/geneco/credentials", headers=AUTH, json={**KV_BODY, "username": "cxchat-svc"}
    )
    assert resp.status_code == 422
    assert "kv-super-secret-value" not in resp.text


def test_switching_to_key_vault_clears_the_stored_pair(ssp_client: TestClient) -> None:
    """A full replace — a leftover pair would outlive the switch and
    resolve again if the mode were flipped back."""
    ssp_client.post("/geneco/credentials", headers=AUTH, json=CREDENTIALS_BODY)
    ssp_client.post("/geneco/credentials", headers=AUTH, json=KV_BODY)

    resp = ssp_client.get("/geneco/credentials", headers=AUTH)

    assert resp.status_code == 200
    assert resp.json()["auth_mode"] == "key_vault"
    assert "super-secret-value" not in resp.text


# — the SSP API signals auth failure in the body, not the status —


def test_a_400_saying_unauthorized_is_read_as_a_rejected_credential():
    """Observed against the SIT API: a wrong password, a wrong username and
    no auth header all answer 400 with {"errordescription":"Unauthorized
    request"}. Status alone cannot tell those from an unknown account, so the
    body is what identifies a credential failure."""
    from geneco_mcp.clients.custom_api_client import ConnectorError
    from geneco_mcp.protocol.admin_router import _is_ssp_auth_rejection, _ssp_failure_detail

    rejected = ConnectorError(
        "custom API request failed 400",
        status_code=400,
        body='[{"result":"FAIL","responsecode":"GEN400","errordescription":"Unauthorized request"}]',
    )
    assert _is_ssp_auth_rejection(rejected) is True
    ok, detail = _ssp_failure_detail(rejected, probed=True, auth_mode="direct")
    assert ok is False
    assert "rejected the registered credentials" in detail


def test_a_400_that_is_not_an_auth_failure_stays_inconclusive():
    """Guessing here would send an admin rotating a secret that was fine."""
    from geneco_mcp.clients.custom_api_client import ConnectorError
    from geneco_mcp.protocol.admin_router import _is_ssp_auth_rejection, _ssp_failure_detail

    other = ConnectorError(
        "custom API request failed 400",
        status_code=400,
        body='[{"result":"FAIL","responsecode":"GEN400","errordescription":"Invalid account"}]',
    )
    assert _is_ssp_auth_rejection(other) is False
    ok, detail = _ssp_failure_detail(other, probed=True, auth_mode="direct")
    assert ok is False
    assert "cannot confirm" in detail


def test_key_vault_mode_names_the_vault_secrets_not_the_registered_fields():
    from geneco_mcp.clients.custom_api_client import ConnectorError
    from geneco_mcp.protocol.admin_router import _ssp_failure_detail

    rejected = ConnectorError("x", status_code=400, body='{"errordescription":"Unauthorized request"}')
    _, detail = _ssp_failure_detail(rejected, probed=True, auth_mode="key_vault")
    assert "Ssp-User-Id/Ssp-Password" in detail
