"""Per-tenant vault secret names, end to end.

Until 0006 the four secret names were module constants, so a tenant whose
vault spelled them differently could not be onboarded. These cover the
three things that has to mean: a row can carry its own names, a row that
carries none still resolves to the defaults (every row written before the
columns existed), and a name the vault would reject is refused at intake
rather than at call time.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

from conftest import TEST_ADMIN_API_KEY
from geneco_mcp.clients.keyvault_client import KeyVaultClient, KeyVaultError, describe
from geneco_mcp.config import Settings
from geneco_mcp.db import build_engine, build_session_factory
from geneco_mcp.store.credentials import CredentialStore, D365CredentialsInput
from geneco_mcp.store.crm_resolver import CrmCredentialCache, CrmCredentialResolver
from geneco_mcp.store.custom_api_credentials import (
    CustomApiCredentialsInput,
    CustomApiCredentialStore,
)
from geneco_mcp.store.ssp_resolver import SspCredentialCache, SspCredentialResolver
from geneco_mcp.store.tables import CustomApiCredential, D365Credential

AUTH = {"X-Admin-Api-Key": TEST_ADMIN_API_KEY}
ORG_URL = "https://geneco.crm.dynamics.com"
VAULT_URL = "https://geneco-kv.vault.azure.net"
SSP_BASE_URL = "https://oemsspapi-sit.example/webapi"


def _stub_token():
    return patch(
        "geneco_mcp.clients.keyvault_client.KeyVaultClient._acquire_token",
        return_value="fake-kv-token",
    )


@pytest.fixture
async def crm_store(settings: Settings) -> CredentialStore:
    engine = build_engine(settings.database_url)
    session_factory = build_session_factory(engine)
    async with session_factory() as session:
        yield CredentialStore(session, encryption_key=settings.credential_encryption_key)
    await engine.dispose()


@pytest.fixture
async def ssp_store(settings: Settings) -> CustomApiCredentialStore:
    engine = build_engine(settings.database_url)
    session_factory = build_session_factory(engine)
    async with session_factory() as session:
        yield CustomApiCredentialStore(
            session, encryption_key=settings.credential_encryption_key
        )
    await engine.dispose()


# --- the vault client itself ------------------------------------------------


@respx.mock
async def test_crm_pair_reads_the_names_it_is_given() -> None:
    client = KeyVaultClient(
        vault_url=VAULT_URL, tenant_id="t1", client_id="kv1", client_secret="kvs1"
    )
    respx.get(f"{VAULT_URL}/secrets/Tenant-Crm-Id").mock(
        return_value=Response(200, json={"value": "crm-id"})
    )
    respx.get(f"{VAULT_URL}/secrets/Tenant-Crm-Secret").mock(
        return_value=Response(200, json={"value": "crm-secret"})
    )
    with _stub_token():
        assert await client.get_crm_credentials(
            client_id_secret="Tenant-Crm-Id", client_secret_secret="Tenant-Crm-Secret"
        ) == ("crm-id", "crm-secret")


@respx.mock
async def test_ssp_pair_reads_the_names_it_is_given() -> None:
    client = KeyVaultClient(
        vault_url=VAULT_URL, tenant_id="t1", client_id="kv1", client_secret="kvs1"
    )
    respx.get(f"{VAULT_URL}/secrets/Vendor-Api-User").mock(
        return_value=Response(200, json={"value": "ssp-user"})
    )
    respx.get(f"{VAULT_URL}/secrets/Vendor-Api-Pass").mock(
        return_value=Response(200, json={"value": "ssp-pass"})
    )
    with _stub_token():
        assert await client.get_ssp_credentials(
            username_secret="Vendor-Api-User", password_secret="Vendor-Api-Pass"
        ) == ("ssp-user", "ssp-pass")


def test_describe_explains_a_rejected_name() -> None:
    """Azure answers 400 BadParameter for a name containing an underscore.
    Intake now blocks those, but a row predating that validation can still
    carry one, so the message has to name the actual rule."""
    detail = describe(
        KeyVaultError("bad name", status_code=400, secret_name="Vendor_API_User_NP")
    )
    assert "only letters, digits and dashes" in detail
    assert "Vendor_API_User_NP" in detail


def test_describe_404_no_longer_claims_the_names_are_fixed() -> None:
    detail = describe(KeyVaultError("missing", status_code=404, secret_name="Tenant-Crm-Id"))
    assert "Tenant-Crm-Id" in detail
    assert "fixed" not in detail


# --- the stores: stored, cleared, and coalesced -----------------------------


async def test_crm_row_without_names_is_not_usable(crm_store: CredentialStore) -> None:
    """No fallback: a vault row with no names has nothing to ask the vault
    for, so it counts as unregistered rather than resolving under some
    other tenant's naming. Only a hand-edited row or one predating 0006's
    back-fill can reach this state."""
    await crm_store.upsert(
        "geneco",
        D365CredentialsInput(
            org_url=ORG_URL,
            tenant_id="t1",
            kv_vault_url=VAULT_URL,
            kv_client_id="kv1",
            kv_client_secret="kv-secret",
            kv_secret_name_client_id="Tenant-Crm-Id",
            kv_secret_name_client_secret="Tenant-Crm-Secret",
        ),
    )
    # Simulate a pre-0006 row by clearing the names behind the store's back.
    row = await crm_store._session.get(D365Credential, "geneco")
    row.kv_secret_name_client_id = None
    row.kv_secret_name_client_secret = None
    await crm_store._session.commit()

    assert await crm_store.get_credentials("geneco") is None
    # tools/list and tools/call must agree about what "registered" means.
    assert await crm_store.is_registered("geneco") is False


async def test_crm_row_keeps_the_names_it_was_given(crm_store: CredentialStore) -> None:
    await crm_store.upsert(
        "geneco",
        D365CredentialsInput(
            org_url=ORG_URL,
            tenant_id="t1",
            kv_vault_url=VAULT_URL,
            kv_client_id="kv1",
            kv_client_secret="kv-secret",
            kv_secret_name_client_id="Tenant-Crm-Id",
            kv_secret_name_client_secret="Tenant-Crm-Secret",
        ),
    )
    resolved = await crm_store.get_credentials("geneco")
    assert resolved.client_id_secret_name == "Tenant-Crm-Id"
    assert resolved.client_secret_secret_name == "Tenant-Crm-Secret"


async def test_switching_to_direct_clears_the_names(crm_store: CredentialStore) -> None:
    """The full-replace rule the other per-mode columns already follow: a
    direct row has no vault, so it must not keep names pointing into one."""
    await crm_store.upsert(
        "geneco",
        D365CredentialsInput(
            org_url=ORG_URL,
            tenant_id="t1",
            kv_vault_url=VAULT_URL,
            kv_client_id="kv1",
            kv_client_secret="kv-secret",
            kv_secret_name_client_id="Tenant-Crm-Id",
            kv_secret_name_client_secret="Tenant-Crm-Secret",
        ),
    )
    row = await crm_store.upsert(
        "geneco",
        D365CredentialsInput(
            auth_mode="direct",
            org_url=ORG_URL,
            tenant_id="t1",
            client_id="direct-id",
            client_secret="direct-secret",
        ),
    )
    assert row.kv_secret_name_client_id is None
    assert row.kv_secret_name_client_secret is None


async def test_ssp_row_without_names_is_not_usable(
    ssp_store: CustomApiCredentialStore,
) -> None:
    await ssp_store.upsert(
        "geneco",
        CustomApiCredentialsInput(
            auth_mode="key_vault",
            base_url=SSP_BASE_URL,
            tenant_id="t1",
            kv_vault_url=VAULT_URL,
            kv_client_id="kv1",
            kv_client_secret="kv-secret",
            kv_secret_name_username="Vendor-Api-User",
            kv_secret_name_password="Vendor-Api-Pass",
        ),
    )
    row = await ssp_store._session.get(CustomApiCredential, "geneco")
    row.kv_secret_name_username = None
    row.kv_secret_name_password = None
    await ssp_store._session.commit()

    assert await ssp_store.get_credentials("geneco") is None
    assert await ssp_store.is_registered("geneco") is False


async def test_ssp_row_keeps_the_names_it_was_given(
    ssp_store: CustomApiCredentialStore,
) -> None:
    await ssp_store.upsert(
        "geneco",
        CustomApiCredentialsInput(
            auth_mode="key_vault",
            base_url=SSP_BASE_URL,
            tenant_id="t1",
            kv_vault_url=VAULT_URL,
            kv_client_id="kv1",
            kv_client_secret="kv-secret",
            kv_secret_name_username="Vendor-Api-User",
            kv_secret_name_password="Vendor-Api-Pass",
        ),
    )
    resolved = await ssp_store.get_credentials("geneco")
    assert resolved.username_secret_name == "Vendor-Api-User"
    assert resolved.password_secret_name == "Vendor-Api-Pass"


# --- the resolvers actually ask the vault for those names -------------------


@respx.mock
async def test_crm_resolver_fetches_the_registered_names(crm_store: CredentialStore) -> None:
    await crm_store.upsert(
        "geneco",
        D365CredentialsInput(
            org_url=ORG_URL,
            tenant_id="t1",
            kv_vault_url=VAULT_URL,
            kv_client_id="kv1",
            kv_client_secret="kv-secret",
            kv_secret_name_client_id="Tenant-Crm-Id",
            kv_secret_name_client_secret="Tenant-Crm-Secret",
        ),
    )
    id_route = respx.get(f"{VAULT_URL}/secrets/Tenant-Crm-Id").mock(
        return_value=Response(200, json={"value": "crm-id"})
    )
    secret_route = respx.get(f"{VAULT_URL}/secrets/Tenant-Crm-Secret").mock(
        return_value=Response(200, json={"value": "crm-secret"})
    )
    default_route = respx.get(f"{VAULT_URL}/secrets/Crm-Client-Id").mock(
        return_value=Response(200, json={"value": "wrong"})
    )

    resolver = CrmCredentialResolver(crm_store, CrmCredentialCache())
    with _stub_token():
        connector = await resolver.get_connector("geneco")

    assert id_route.called and secret_route.called
    assert not default_route.called
    assert connector.client_id == "crm-id"


@respx.mock
async def test_ssp_resolver_fetches_the_registered_names(
    ssp_store: CustomApiCredentialStore,
) -> None:
    await ssp_store.upsert(
        "geneco",
        CustomApiCredentialsInput(
            auth_mode="key_vault",
            base_url=SSP_BASE_URL,
            tenant_id="t1",
            kv_vault_url=VAULT_URL,
            kv_client_id="kv1",
            kv_client_secret="kv-secret",
            kv_secret_name_username="Vendor-Api-User",
            kv_secret_name_password="Vendor-Api-Pass",
        ),
    )
    user_route = respx.get(f"{VAULT_URL}/secrets/Vendor-Api-User").mock(
        return_value=Response(200, json={"value": "ssp-user"})
    )
    pass_route = respx.get(f"{VAULT_URL}/secrets/Vendor-Api-Pass").mock(
        return_value=Response(200, json={"value": "ssp-pass"})
    )
    default_route = respx.get(f"{VAULT_URL}/secrets/Ssp-User-Id").mock(
        return_value=Response(200, json={"value": "wrong"})
    )

    resolver = SspCredentialResolver(ssp_store, SspCredentialCache())
    with _stub_token():
        client = await resolver.get_client("geneco")

    assert user_route.called and pass_route.called
    assert not default_route.called
    assert client.username == "ssp-user"


# --- intake: accepted, echoed, and refused ----------------------------------


def test_crm_intake_round_trips_the_names(crm_client: TestClient) -> None:
    body = {
        "org_url": ORG_URL,
        "tenant_id": "t1",
        "kv_vault_url": VAULT_URL,
        "kv_client_id": "kv1",
        "kv_client_secret": "super-secret-value",
        "kv_secret_name_client_id": "Tenant-Crm-Id",
        "kv_secret_name_client_secret": "Tenant-Crm-Secret",
    }
    resp = crm_client.post("/geneco/credentials", headers=AUTH, json=body)
    assert resp.status_code == 200
    assert resp.json()["kv_secret_name_client_id"] == "Tenant-Crm-Id"

    got = crm_client.get("/geneco/credentials", headers=AUTH).json()
    assert got["kv_secret_name_client_secret"] == "Tenant-Crm-Secret"
    # A name is not a secret — but the secret still must not come back.
    assert "super-secret-value" not in resp.text


def test_crm_intake_rejects_a_vault_body_with_no_names(crm_client: TestClient) -> None:
    """Mandatory, not defaulted: registering without them must fail here,
    where a portal can render the 422, rather than succeeding and failing
    later on a customer-facing tool call."""
    body = {
        "org_url": ORG_URL,
        "tenant_id": "t1",
        "kv_vault_url": VAULT_URL,
        "kv_client_id": "kv1",
        "kv_client_secret": "super-secret-value",
    }
    resp = crm_client.post("/geneco/credentials", headers=AUTH, json=body)
    assert resp.status_code == 422
    assert "kv_secret_name_client_id" in resp.text
    assert "super-secret-value" not in resp.text


def test_crm_intake_rejects_a_vault_body_with_only_one_name(crm_client: TestClient) -> None:
    body = {
        "org_url": ORG_URL,
        "tenant_id": "t1",
        "kv_vault_url": VAULT_URL,
        "kv_client_id": "kv1",
        "kv_client_secret": "super-secret-value",
        "kv_secret_name_client_id": "Tenant-Crm-Id",
    }
    resp = crm_client.post("/geneco/credentials", headers=AUTH, json=body)
    assert resp.status_code == 422
    assert "kv_secret_name_client_secret" in resp.text


def test_ssp_intake_rejects_a_vault_body_with_no_names(ssp_client: TestClient) -> None:
    body = {
        "auth_mode": "key_vault",
        "base_url": SSP_BASE_URL,
        "tenant_id": "t1",
        "kv_vault_url": VAULT_URL,
        "kv_client_id": "kv1",
        "kv_client_secret": "super-secret-value",
    }
    resp = ssp_client.post("/geneco/credentials", headers=AUTH, json=body)
    assert resp.status_code == 422
    assert "kv_secret_name_username" in resp.text


def test_ssp_intake_round_trips_the_names(ssp_client: TestClient) -> None:
    body = {
        "auth_mode": "key_vault",
        "base_url": SSP_BASE_URL,
        "tenant_id": "t1",
        "kv_vault_url": VAULT_URL,
        "kv_client_id": "kv1",
        "kv_client_secret": "super-secret-value",
        "kv_secret_name_username": "Vendor-Api-User",
        "kv_secret_name_password": "Vendor-Api-Pass",
    }
    resp = ssp_client.post("/geneco/credentials", headers=AUTH, json=body)
    assert resp.status_code == 200
    assert resp.json()["kv_secret_name_username"] == "Vendor-Api-User"
    assert resp.json()["kv_secret_name_password"] == "Vendor-Api-Pass"


def test_underscore_name_is_refused_at_intake_not_at_call_time(
    crm_client: TestClient,
) -> None:
    """The exact shape that motivated this: Azure rejects an underscore with
    a 400 the tenant would only see on a tool call. A 422 here is the whole
    point of validating the name at the boundary."""
    body = {
        "org_url": ORG_URL,
        "tenant_id": "t1",
        "kv_vault_url": VAULT_URL,
        "kv_client_id": "kv1",
        "kv_client_secret": "super-secret-value",
        "kv_secret_name_client_id": "Vendor_API_User_NP",
    }
    resp = crm_client.post("/geneco/credentials", headers=AUTH, json=body)
    assert resp.status_code == 422
    # validation_error_handler strips the echoed input, so the rejected
    # body must not come back — secret or not.
    assert "super-secret-value" not in resp.text


def test_direct_mode_rejects_a_secret_name(crm_client: TestClient) -> None:
    """A direct row has no vault to name secrets in; silently ignoring the
    field would leave an admin thinking it took effect."""
    body = {
        "auth_mode": "direct",
        "org_url": ORG_URL,
        "tenant_id": "t1",
        "client_id": "direct-id",
        "client_secret": "direct-secret-value",
        "kv_secret_name_client_id": "Tenant-Crm-Id",
    }
    resp = crm_client.post("/geneco/credentials", headers=AUTH, json=body)
    assert resp.status_code == 422


def test_ssp_direct_mode_rejects_a_secret_name(ssp_client: TestClient) -> None:
    body = {
        "auth_mode": "direct",
        "base_url": SSP_BASE_URL,
        "username": "Vendor_API_User_NP",
        "password": "direct-password-value",
        "kv_secret_name_username": "Vendor-Api-User",
    }
    resp = ssp_client.post("/geneco/credentials", headers=AUTH, json=body)
    assert resp.status_code == 422
