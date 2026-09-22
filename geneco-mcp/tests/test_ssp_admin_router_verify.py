"""``POST /{tenant_slug}/credentials/verify`` on the SSP server.

Same rule as the CRM verify: a credential or network failure is an ANSWER
(200 + ``ok=false``), never a 5xx. Only a bad admin key (401) and an
unregistered tenant (404) are non-200.

What differs is what a green result can claim. The SSP API has no
ping/WhoAmI endpoint, so the check is a real ``feewaiver`` lookup — and
without a real account number to look up, a non-auth error is
indistinguishable from "no such account". These tests pin that
distinction: a 401/403 is always attributable to the credentials, and a
probe-only 4xx must never come back green.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

import httpx
import respx
from fastapi.testclient import TestClient
from httpx import Response

from conftest import TEST_ADMIN_API_KEY
from geneco_mcp.protocol.admin_router import PROBE_ACCOUNT_NUMBER

SSP_URL = "https://ssp.geneco.example"
KV_URL = "https://geneco-kv.vault.azure.net"
FEE_WAIVER_URL = f"{SSP_URL}/api/cxchat/v2/feewaiver"
REAL_ACCOUNT = "GC8030359D"

AUTH = {"X-Admin-Api-Key": TEST_ADMIN_API_KEY}


def _mock_vault() -> None:
    respx.get(f"{KV_URL}/secrets/Ssp-User-Id").mock(
        return_value=Response(200, json={"value": "vault-ssp-user"})
    )
    respx.get(f"{KV_URL}/secrets/Ssp-Password").mock(
        return_value=Response(200, json={"value": "vault-ssp-password"})
    )


@contextmanager
def _stub_token():
    with patch(
        "geneco_mcp.clients.keyvault_client.KeyVaultClient._acquire_token",
        return_value="fake-kv-token",
    ):
        yield


def _seed_direct(ssp_client: TestClient, tenant_slug: str = "geneco") -> None:
    resp = ssp_client.post(
        f"/{tenant_slug}/credentials",
        headers=AUTH,
        json={"base_url": SSP_URL, "username": "svc", "password": "s3cret"},
    )
    assert resp.status_code == 200


def _seed_key_vault(ssp_client: TestClient, tenant_slug: str = "geneco") -> None:
    resp = ssp_client.post(
        f"/{tenant_slug}/credentials",
        headers=AUTH,
        json={
            "auth_mode": "key_vault",
            "base_url": SSP_URL,
            "tenant_id": "t1",
            "kv_vault_url": KV_URL,
            "kv_client_id": "kv1",
            "kv_client_secret": "kv-secret",
        },
    )
    assert resp.status_code == 200


# --- the guarantees that hold in every mode ---


def test_requires_an_admin_key(ssp_client: TestClient) -> None:
    assert ssp_client.post("/geneco/credentials/verify").status_code == 401


def test_rejects_a_wrong_admin_key(ssp_client: TestClient) -> None:
    resp = ssp_client.post(
        "/geneco/credentials/verify", headers={"X-Admin-Api-Key": "wrong-key"}
    )
    assert resp.status_code == 401


def test_unregistered_tenant_is_404(ssp_client: TestClient) -> None:
    resp = ssp_client.post("/unknown-tenant/credentials/verify", headers=AUTH)
    assert resp.status_code == 404


# --- direct mode ---


@respx.mock
def test_probe_lookup_that_succeeds_reports_ok(ssp_client: TestClient) -> None:
    _seed_direct(ssp_client)
    route = respx.post(FEE_WAIVER_URL).mock(
        return_value=Response(200, json={"FeeWaiverLines": [], "Remarks": None})
    )
    resp = ssp_client.post("/geneco/credentials/verify", headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert isinstance(body["latency_ms"], int)
    # The probe account number is what went on the wire, not a real one.
    assert PROBE_ACCOUNT_NUMBER.encode() in route.calls.last.request.content


@respx.mock
def test_a_real_account_number_is_used_when_given(ssp_client: TestClient) -> None:
    _seed_direct(ssp_client)
    route = respx.post(FEE_WAIVER_URL).mock(
        return_value=Response(200, json={"FeeWaiverLines": [], "Remarks": None})
    )
    resp = ssp_client.post(
        "/geneco/credentials/verify", headers=AUTH, json={"account_number": REAL_ACCOUNT}
    )

    assert resp.json()["ok"] is True
    assert REAL_ACCOUNT.encode() in route.calls.last.request.content


@respx.mock
def test_rejected_credentials_are_reported_as_such(ssp_client: TestClient) -> None:
    """A 401 is the one failure attributable to the credentials with
    certainty — it must name the registered pair in direct mode."""
    _seed_direct(ssp_client)
    respx.post(FEE_WAIVER_URL).mock(return_value=Response(401, text="Unauthorized"))
    resp = ssp_client.post("/geneco/credentials/verify", headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "rejected" in body["detail"]
    assert "username and password" in body["detail"]


@respx.mock
def test_403_is_also_a_credential_rejection(ssp_client: TestClient) -> None:
    _seed_direct(ssp_client)
    respx.post(FEE_WAIVER_URL).mock(return_value=Response(403, text="Forbidden"))
    body = ssp_client.post("/geneco/credentials/verify", headers=AUTH).json()
    assert body["ok"] is False
    assert "rejected" in body["detail"]


@respx.mock
def test_a_probe_4xx_is_inconclusive_not_a_false_green(ssp_client: TestClient) -> None:
    """The core honesty guarantee: with a probe account number, a non-auth
    4xx could be "no such account" rather than bad credentials. It must be
    reported as unconfirmed — never ok=true, and never as a credential
    failure the admin would go rotate a secret over."""
    _seed_direct(ssp_client)
    respx.post(FEE_WAIVER_URL).mock(return_value=Response(400, text="Bad account"))
    body = ssp_client.post("/geneco/credentials/verify", headers=AUTH).json()

    assert body["ok"] is False
    assert "cannot confirm" in body["detail"]
    assert "real account_number" in body["detail"]
    # Must not send an admin rotating a secret that was fine.
    assert "username and password" not in body["detail"]
    assert "Ssp-User-Id" not in body["detail"]


@respx.mock
def test_a_4xx_with_a_real_account_number_is_not_excused_as_a_probe(
    ssp_client: TestClient,
) -> None:
    """The inconclusive wording is only honest when a probe was used — with
    a real account number the same status is simply the API's answer."""
    _seed_direct(ssp_client)
    respx.post(FEE_WAIVER_URL).mock(return_value=Response(400, text="Bad account"))
    body = ssp_client.post(
        "/geneco/credentials/verify", headers=AUTH, json={"account_number": REAL_ACCOUNT}
    ).json()

    assert body["ok"] is False
    assert "returned 400" in body["detail"]
    assert "cannot confirm" not in body["detail"]


@respx.mock
def test_backend_500_is_an_answer_not_a_5xx(ssp_client: TestClient) -> None:
    _seed_direct(ssp_client)
    respx.post(FEE_WAIVER_URL).mock(return_value=Response(500, text="boom"))
    resp = ssp_client.post("/geneco/credentials/verify", headers=AUTH)

    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert "500" in resp.json()["detail"]


@respx.mock
def test_unreachable_backend_is_an_answer_not_a_5xx(ssp_client: TestClient) -> None:
    _seed_direct(ssp_client)
    respx.post(FEE_WAIVER_URL).mock(side_effect=httpx.ConnectError("boom"))
    resp = ssp_client.post("/geneco/credentials/verify", headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "Could not reach" in body["detail"]


@respx.mock
def test_verify_never_reverses_a_fee_or_emails_a_customer(ssp_client: TestClient) -> None:
    """The probe must stay read-only. Hitting feereversal would move a
    customer's money and forgotpasswordv2 would email them — neither is an
    acceptable side effect of an admin pressing "test connection"."""
    _seed_direct(ssp_client)
    respx.post(FEE_WAIVER_URL).mock(
        return_value=Response(200, json={"FeeWaiverLines": [], "Remarks": None})
    )
    reversal = respx.post(f"{SSP_URL}/api/cxchat/v2/feereversal").mock(
        return_value=Response(200, json={})
    )
    password = respx.post(f"{SSP_URL}/api/forgotpasswordv2").mock(
        return_value=Response(200, json={})
    )

    assert ssp_client.post("/geneco/credentials/verify", headers=AUTH).json()["ok"] is True
    assert not reversal.called
    assert not password.called


# --- key_vault mode ---


@respx.mock
def test_key_vault_mode_verifies_through_the_vault(ssp_client: TestClient) -> None:
    _seed_key_vault(ssp_client)
    _mock_vault()
    respx.post(FEE_WAIVER_URL).mock(
        return_value=Response(200, json={"FeeWaiverLines": [], "Remarks": None})
    )
    with _stub_token():
        body = ssp_client.post("/geneco/credentials/verify", headers=AUTH).json()

    assert body["ok"] is True
    assert "Key Vault" in body["detail"]


@respx.mock
def test_key_vault_mode_names_the_vault_secrets_on_rejection(ssp_client: TestClient) -> None:
    """In vault mode the rejected pair came from the vault, not from
    anything registered here — pointing an admin at the registered fields
    would send them to the wrong place."""
    _seed_key_vault(ssp_client)
    _mock_vault()
    respx.post(FEE_WAIVER_URL).mock(return_value=Response(401, text="Unauthorized"))
    with _stub_token():
        body = ssp_client.post("/geneco/credentials/verify", headers=AUTH).json()

    assert body["ok"] is False
    assert "Ssp-User-Id" in body["detail"]
    assert "registered username" not in body["detail"]


@respx.mock
def test_an_unreadable_vault_is_an_answer_not_a_5xx(ssp_client: TestClient) -> None:
    _seed_key_vault(ssp_client)
    respx.get(f"{KV_URL}/secrets/Ssp-User-Id").mock(return_value=Response(403, text="Forbidden"))
    with _stub_token():
        resp = ssp_client.post("/geneco/credentials/verify", headers=AUTH)

    assert resp.status_code == 200
    assert resp.json()["ok"] is False


@respx.mock
def test_verify_forces_the_vault_hop_rather_than_serving_a_cached_pair(
    ssp_client: TestClient,
) -> None:
    """A verify answered from cache would prove nothing about the vault
    *now* — the whole point of the check."""
    _seed_key_vault(ssp_client)
    _mock_vault()
    respx.post(FEE_WAIVER_URL).mock(
        return_value=Response(200, json={"FeeWaiverLines": [], "Remarks": None})
    )
    with _stub_token():
        ssp_client.post("/geneco/credentials/verify", headers=AUTH)
        second = respx.get(f"{KV_URL}/secrets/Ssp-User-Id").mock(
            return_value=Response(200, json={"value": "vault-ssp-user"})
        )
        ssp_client.post("/geneco/credentials/verify", headers=AUTH)

    assert second.called
