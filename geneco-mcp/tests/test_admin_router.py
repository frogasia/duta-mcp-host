from __future__ import annotations

import sqlalchemy as sa
from fastapi.testclient import TestClient

from conftest import TEST_ADMIN_API_KEY
from geneco_mcp.config import Settings

AUTH = {"X-Admin-Api-Key": TEST_ADMIN_API_KEY}

CREDENTIALS_BODY = {
    "org_url": "https://geneco.crm.dynamics.com",
    "tenant_id": "77dee05b-8ff2-4aee-81a7-461ed9ab3456",
    "kv_vault_url": "https://geneco-kv.vault.azure.net",
    "kv_client_id": "77fa3ab9-8247-48e4-8ad5-5fc959ef7f24",
    "kv_client_secret": "super-secret-value",
    "kv_secret_name_client_id": "Crm-Client-Id",
    "kv_secret_name_client_secret": "Crm-Client-Secret",
}

DIRECT_BODY = {
    "auth_mode": "direct",
    "org_url": "https://geneco.crm.dynamics.com",
    "tenant_id": "77dee05b-8ff2-4aee-81a7-461ed9ab3456",
    "client_id": "3f1c7b02-9d41-4f8e-9a77-1c2b3d4e5f60",
    "client_secret": "direct-secret-value",
}


def _row(settings: Settings, tenant_slug: str = "geneco") -> sa.Row:
    """Read the stored row directly — the point of the mode-switch tests is
    what is left in the *columns*, which no API response shows."""
    engine = sa.create_engine(settings.database_url.replace("+aiosqlite", ""))
    with engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT * FROM d365_credentials WHERE tenant_slug = :slug"),
            {"slug": tenant_slug},
        ).mappings().one()
    engine.dispose()
    return row


def test_upsert_requires_admin_key(crm_client: TestClient) -> None:
    resp = crm_client.post("/geneco/credentials", json=CREDENTIALS_BODY)
    assert resp.status_code == 401


def test_upsert_rejects_wrong_admin_key(crm_client: TestClient) -> None:
    resp = crm_client.post(
        "/geneco/credentials", headers={"X-Admin-Api-Key": "wrong-key"}, json=CREDENTIALS_BODY
    )
    assert resp.status_code == 401


def test_upsert_then_get_never_exposes_the_real_secret(crm_client: TestClient) -> None:
    resp = crm_client.post(
        "/geneco/credentials", headers={"X-Admin-Api-Key": TEST_ADMIN_API_KEY}, json=CREDENTIALS_BODY
    )
    assert resp.status_code == 200
    assert resp.json()["kv_client_secret"] == "•••"
    assert "super-secret-value" not in resp.text

    get_resp = crm_client.get("/geneco/credentials", headers={"X-Admin-Api-Key": TEST_ADMIN_API_KEY})
    assert get_resp.status_code == 200
    assert get_resp.json()["kv_client_secret"] == "•••"
    assert "super-secret-value" not in get_resp.text


def test_get_missing_tenant_is_404(crm_client: TestClient) -> None:
    resp = crm_client.get("/unknown-tenant/credentials", headers={"X-Admin-Api-Key": TEST_ADMIN_API_KEY})
    assert resp.status_code == 404


def test_delete_then_get_is_404(crm_client: TestClient) -> None:
    crm_client.post("/geneco/credentials", headers={"X-Admin-Api-Key": TEST_ADMIN_API_KEY}, json=CREDENTIALS_BODY)
    del_resp = crm_client.delete("/geneco/credentials", headers={"X-Admin-Api-Key": TEST_ADMIN_API_KEY})
    assert del_resp.status_code == 204
    get_resp = crm_client.get("/geneco/credentials", headers={"X-Admin-Api-Key": TEST_ADMIN_API_KEY})
    assert get_resp.status_code == 404


def test_delete_missing_tenant_is_404(crm_client: TestClient) -> None:
    resp = crm_client.delete("/unknown-tenant/credentials", headers={"X-Admin-Api-Key": TEST_ADMIN_API_KEY})
    assert resp.status_code == 404


def test_key_vault_is_the_default_auth_mode(crm_client: TestClient) -> None:
    """A body with no auth_mode is a pre-direct-mode caller registering a
    vault reader — reinterpreting it as a D365 app registration would be
    silently wrong."""
    resp = crm_client.post("/geneco/credentials", headers=AUTH, json=CREDENTIALS_BODY)
    assert resp.status_code == 200
    assert resp.json()["auth_mode"] == "key_vault"


def test_register_direct_mode_then_get_masks_the_secret(crm_client: TestClient) -> None:
    resp = crm_client.post("/geneco/credentials", headers=AUTH, json=DIRECT_BODY)
    assert resp.status_code == 200
    body = resp.json()
    assert body["auth_mode"] == "direct"
    assert body["client_id"] == DIRECT_BODY["client_id"]
    assert body["client_secret"] == "•••"
    assert "direct-secret-value" not in resp.text

    get_resp = crm_client.get("/geneco/credentials", headers=AUTH)
    assert get_resp.status_code == 200
    assert get_resp.json()["client_secret"] == "•••"
    assert "direct-secret-value" not in get_resp.text


def test_get_echoes_only_the_active_modes_fields(crm_client: TestClient) -> None:
    crm_client.post("/geneco/credentials", headers=AUTH, json=DIRECT_BODY)
    direct = crm_client.get("/geneco/credentials", headers=AUTH).json()
    assert "kv_client_id" not in direct
    assert "kv_client_secret" not in direct

    crm_client.post("/geneco/credentials", headers=AUTH, json=CREDENTIALS_BODY)
    kv = crm_client.get("/geneco/credentials", headers=AUTH).json()
    assert kv["auth_mode"] == "key_vault"
    assert "client_id" not in kv
    assert "client_secret" not in kv


def test_switching_key_vault_to_direct_clears_the_vault_columns(
    crm_client: TestClient, settings: Settings
) -> None:
    crm_client.post("/geneco/credentials", headers=AUTH, json=CREDENTIALS_BODY)
    assert crm_client.post("/geneco/credentials", headers=AUTH, json=DIRECT_BODY).status_code == 200

    row = _row(settings)
    assert row["auth_mode"] == "direct"
    assert row["client_id"] == DIRECT_BODY["client_id"]
    assert row["client_secret_encrypted"] is not None
    # Stale vault credentials a later code path could pick up.
    assert row["kv_vault_url"] is None
    assert row["kv_client_id"] is None
    assert row["kv_client_secret_encrypted"] is None


def test_switching_direct_to_key_vault_clears_the_direct_columns(
    crm_client: TestClient, settings: Settings
) -> None:
    crm_client.post("/geneco/credentials", headers=AUTH, json=DIRECT_BODY)
    assert crm_client.post("/geneco/credentials", headers=AUTH, json=CREDENTIALS_BODY).status_code == 200

    row = _row(settings)
    assert row["auth_mode"] == "key_vault"
    assert row["kv_client_id"] == CREDENTIALS_BODY["kv_client_id"]
    assert row["kv_client_secret_encrypted"] is not None
    # A D365 secret left at rest after the tenant moved to a vault.
    assert row["client_id"] is None
    assert row["client_secret_encrypted"] is None


def test_neither_secret_is_ever_stored_in_plaintext(
    crm_client: TestClient, settings: Settings
) -> None:
    crm_client.post("/geneco/credentials", headers=AUTH, json=DIRECT_BODY)
    assert DIRECT_BODY["client_secret"] not in _row(settings)["client_secret_encrypted"]
    crm_client.post("/geneco/credentials", headers=AUTH, json=CREDENTIALS_BODY)
    assert CREDENTIALS_BODY["kv_client_secret"] not in _row(settings)["kv_client_secret_encrypted"]


def test_direct_mode_missing_its_own_fields_is_rejected(crm_client: TestClient) -> None:
    body = {k: v for k, v in DIRECT_BODY.items() if k != "client_secret"}
    resp = crm_client.post("/geneco/credentials", headers=AUTH, json=body)
    assert resp.status_code == 422
    assert "client_secret" in resp.text


def test_key_vault_mode_missing_its_own_fields_is_rejected(crm_client: TestClient) -> None:
    body = {k: v for k, v in CREDENTIALS_BODY.items() if k != "kv_vault_url"}
    resp = crm_client.post("/geneco/credentials", headers=AUTH, json=body)
    assert resp.status_code == 422
    assert "kv_vault_url" in resp.text


def test_a_body_carrying_the_other_modes_fields_is_rejected(crm_client: TestClient) -> None:
    """Both pairs at once would leave it ambiguous which app registration
    the caller meant — and would post a secret that has nowhere to go."""
    resp = crm_client.post(
        "/geneco/credentials", headers=AUTH, json={**DIRECT_BODY, **{
            "kv_vault_url": CREDENTIALS_BODY["kv_vault_url"],
            "kv_client_id": CREDENTIALS_BODY["kv_client_id"],
            "kv_client_secret": CREDENTIALS_BODY["kv_client_secret"],
            "kv_secret_name_client_id": "Crm-Client-Id",
            "kv_secret_name_client_secret": "Crm-Client-Secret",
        }}
    )
    assert resp.status_code == 422
    assert CREDENTIALS_BODY["kv_client_secret"] not in resp.text


def test_an_unknown_auth_mode_is_rejected(crm_client: TestClient) -> None:
    resp = crm_client.post(
        "/geneco/credentials", headers=AUTH, json={**DIRECT_BODY, "auth_mode": "sideways"}
    )
    assert resp.status_code == 422


def test_a_rejected_body_never_echoes_the_secret_it_carried(crm_client: TestClient) -> None:
    """FastAPI's default 422 echoes the submitted input verbatim — which on
    this route means echoing the posted secret back, and into any log that
    captures response bodies."""
    resp = crm_client.post(
        "/geneco/credentials", headers=AUTH, json={**CREDENTIALS_BODY, "org_url": "not-a-url"}
    )
    assert resp.status_code == 422
    assert CREDENTIALS_BODY["kv_client_secret"] not in resp.text
    # The field path still reaches the portal, so the error stays actionable.
    assert "org_url" in resp.text


def test_a_rejected_direct_body_never_echoes_its_secret(crm_client: TestClient) -> None:
    resp = crm_client.post(
        "/geneco/credentials", headers=AUTH, json={**DIRECT_BODY, "org_url": "not-a-url"}
    )
    assert resp.status_code == 422
    assert DIRECT_BODY["client_secret"] not in resp.text
