from __future__ import annotations

from fastapi.testclient import TestClient

from conftest import TEST_ADMIN_API_KEY

AUTH = {"X-Admin-Api-Key": TEST_ADMIN_API_KEY}

CREDENTIALS_BODY = {
    "org_url": "https://geneco.crm.dynamics.com",
    "tenant_id": "77dee05b-8ff2-4aee-81a7-461ed9ab3456",
    "kv_vault_url": "https://geneco-kv.vault.azure.net",
    "kv_client_id": "77fa3ab9-8247-48e4-8ad5-5fc959ef7f24",
    "kv_client_secret": "super-secret-value",
}


def test_upsert_requires_admin_key(client: TestClient) -> None:
    resp = client.post("/geneco/credentials", json=CREDENTIALS_BODY)
    assert resp.status_code == 401


def test_upsert_rejects_wrong_admin_key(client: TestClient) -> None:
    resp = client.post(
        "/geneco/credentials", headers={"X-Admin-Api-Key": "wrong-key"}, json=CREDENTIALS_BODY
    )
    assert resp.status_code == 401


def test_upsert_then_get_never_exposes_the_real_secret(client: TestClient) -> None:
    resp = client.post(
        "/geneco/credentials", headers={"X-Admin-Api-Key": TEST_ADMIN_API_KEY}, json=CREDENTIALS_BODY
    )
    assert resp.status_code == 200
    assert resp.json()["kv_client_secret"] == "•••"
    assert "super-secret-value" not in resp.text

    get_resp = client.get("/geneco/credentials", headers={"X-Admin-Api-Key": TEST_ADMIN_API_KEY})
    assert get_resp.status_code == 200
    assert get_resp.json()["kv_client_secret"] == "•••"
    assert "super-secret-value" not in get_resp.text


def test_get_missing_tenant_is_404(client: TestClient) -> None:
    resp = client.get("/unknown-tenant/credentials", headers={"X-Admin-Api-Key": TEST_ADMIN_API_KEY})
    assert resp.status_code == 404


def test_delete_then_get_is_404(client: TestClient) -> None:
    client.post("/geneco/credentials", headers={"X-Admin-Api-Key": TEST_ADMIN_API_KEY}, json=CREDENTIALS_BODY)
    del_resp = client.delete("/geneco/credentials", headers={"X-Admin-Api-Key": TEST_ADMIN_API_KEY})
    assert del_resp.status_code == 204
    get_resp = client.get("/geneco/credentials", headers={"X-Admin-Api-Key": TEST_ADMIN_API_KEY})
    assert get_resp.status_code == 404


def test_delete_missing_tenant_is_404(client: TestClient) -> None:
    resp = client.delete("/unknown-tenant/credentials", headers={"X-Admin-Api-Key": TEST_ADMIN_API_KEY})
    assert resp.status_code == 404


def test_a_rejected_body_never_echoes_the_secret_it_carried(client: TestClient) -> None:
    """FastAPI's default 422 echoes the submitted input verbatim — which on
    this route means echoing the posted secret back, and into any log that
    captures response bodies."""
    resp = client.post(
        "/geneco/credentials", headers=AUTH, json={**CREDENTIALS_BODY, "org_url": "not-a-url"}
    )
    assert resp.status_code == 422
    assert CREDENTIALS_BODY["kv_client_secret"] not in resp.text
    # The field path still reaches the portal, so the error stays actionable.
    assert "org_url" in resp.text
