from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import httpx
import respx
from fastapi.testclient import TestClient
from httpx import Response

from conftest import TEST_ADMIN_API_KEY

ORG_URL = "https://geneco.crm.dynamics.com"
KV_URL = "https://geneco-kv.vault.azure.net"


def _mock_vault() -> None:
    """Stub the vault hop that now sits in front of every D365 call."""
    respx.get(f"{KV_URL}/secrets/Crm-Client-Id").mock(
        return_value=Response(200, json={"value": "crm-crm_client-id"})
    )
    respx.get(f"{KV_URL}/secrets/Crm-Client-Secret").mock(
        return_value=Response(200, json={"value": "crm-crm_client-secret"})
    )


@contextmanager
def _stub_tokens():
    """Both MSAL handshakes (vault + Dataverse) are msal's machinery, not
    this project's wire shape — stub them rather than mocking over HTTP."""
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


def _rpc(
    mcp: TestClient,
    tenant_slug: str,
    method: str,
    params: dict[str, Any] | None = None,
    request_id: int | None = 1,
) -> httpx.Response:
    body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        body["id"] = request_id
    if params is not None:
        body["params"] = params
    return mcp.post(f"/{tenant_slug}", json=body)


def _seed_credentials(mcp: TestClient, tenant_slug: str) -> None:
    """Register D365 credentials — always against the CRM server."""
    resp = mcp.post(
        f"/{tenant_slug}/credentials",
        headers={"X-Admin-Api-Key": TEST_ADMIN_API_KEY},
        json={
            "org_url": ORG_URL,
            "tenant_id": "t1",
            "kv_vault_url": KV_URL,
            "kv_client_id": "kv1",
            "kv_client_secret": "kvs1",
            "kv_secret_name_client_id": "Crm-Client-Id",
            "kv_secret_name_client_secret": "Crm-Client-Secret",
        },
    )
    assert resp.status_code == 200


def test_initialize_returns_protocol_info_and_session_header(crm_client: TestClient) -> None:
    resp = _rpc(
        crm_client,
        "geneco",
        "initialize",
        {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "x", "version": "0"}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == 1
    assert body["result"]["protocolVersion"] == "2025-03-26"
    assert "mcp-session-id" in {k.lower() for k in resp.headers}


def test_notifications_initialized_returns_202_no_body(crm_client: TestClient) -> None:
    resp = crm_client.post("/geneco", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert resp.status_code == 202
    assert resp.content == b""


def test_initialize_reports_the_crm_server_name(crm_client: TestClient) -> None:
    """Two endpoints a client may hold registrations for at once must not
    introduce themselves identically."""
    resp = _rpc(crm_client, "geneco", "initialize", {})
    assert resp.json()["result"]["serverInfo"]["name"] == "geneco-crm-mcp"


def test_tools_list_is_empty_before_credentials_are_registered(crm_client: TestClient) -> None:
    """A tenant mid-onboarding is offered nothing rather than tools whose
    every call answers "not available for this workspace right now"."""
    resp = _rpc(crm_client, "no-credentials-tenant", "tools/list", {})
    assert resp.json()["result"]["tools"] == []


def test_tools_list_returns_the_crm_tools_once_registered(crm_client: TestClient) -> None:
    _seed_credentials(crm_client, "geneco")
    resp = _rpc(crm_client, "geneco", "tools/list", {})
    tools = {t["name"] for t in resp.json()["result"]["tools"]}
    assert tools == {"verify_account", "create_support_case", "get_support_cases"}
    for tool in resp.json()["result"]["tools"]:
        assert tool["inputSchema"]["type"] == "object"


def test_tools_list_goes_empty_again_when_credentials_are_revoked(
    crm_client: TestClient,
) -> None:
    _seed_credentials(crm_client, "geneco")
    crm_client.delete("/geneco/credentials", headers={"X-Admin-Api-Key": TEST_ADMIN_API_KEY})
    resp = _rpc(crm_client, "geneco", "tools/list", {})
    assert resp.json()["result"]["tools"] == []


def test_crm_tools_list_ignores_ssp_credentials(
    crm_client: TestClient, ssp_client: TestClient
) -> None:
    """Registering one backend must not light up the other's tools — the
    whole point of onboarding them separately."""
    _seed_custom_api_credentials(ssp_client, "geneco")
    assert _rpc(crm_client, "geneco", "tools/list", {}).json()["result"]["tools"] == []


def test_crm_server_will_not_run_an_ssp_tool(
    crm_client: TestClient, ssp_client: TestClient
) -> None:
    """An endpoint must never execute a tool it never advertised, even with
    that tool's credentials registered on the other server."""
    _seed_credentials(crm_client, "geneco")
    _seed_custom_api_credentials(ssp_client, "geneco")
    resp = _rpc(
        crm_client,
        "geneco",
        "tools/call",
        {"name": "check_fee_waiver_status", "arguments": {"account_number": "GC8063979W"}},
    )
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["isError"] is True
    assert "unknown tool" in result["content"][0]["text"]


def test_tools_call_verify_account_no_credentials_is_honest_error(crm_client: TestClient) -> None:
    """No connector registered for this tenant at all — never a 500, never
    a fabricated result."""
    resp = _rpc(
        crm_client,
        "no-credentials-tenant",
        "tools/call",
        {"name": "verify_account", "arguments": {"account_id": "ACC1", "mobile_number": "0123456789"}},
    )
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["isError"] is True
    assert "not available" in result["structuredContent"]["error"]


@respx.mock
def test_tools_call_verify_account_happy_path(crm_client: TestClient) -> None:
    _seed_credentials(crm_client, "geneco")
    _mock_vault()
    respx.get(f"{ORG_URL}/api/data/v9.2/accounts").mock(
        return_value=Response(
            200,
            json={"value": [{"accountid": "guid-1", "name": "Jane", "oem_accountbalance": 10.0}]},
        )
    )
    with _stub_tokens():
        resp = _rpc(
            crm_client,
            "geneco",
            "tools/call",
            {"name": "verify_account", "arguments": {"account_id": "ACC1", "mobile_number": "0123456789"}},
        )
    result = resp.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["verified"] is True


@respx.mock
def test_tools_call_d365_failure_is_honest_error_not_5xx(crm_client: TestClient) -> None:
    """Regression test for the exact bug found in integration-hub: a D365
    connectivity failure must come back as a 200 + honest error, never an
    unhandled 500."""
    _seed_credentials(crm_client, "geneco")
    _mock_vault()
    respx.get(f"{ORG_URL}/api/data/v9.2/accounts").mock(side_effect=httpx.ConnectError("boom"))
    with _stub_tokens():
        resp = _rpc(
            crm_client,
            "geneco",
            "tools/call",
            {"name": "verify_account", "arguments": {"account_id": "ACC1", "mobile_number": "0123456789"}},
        )
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["isError"] is True
    assert "error" in result["structuredContent"]


@respx.mock
def test_tools_call_vault_failure_is_honest_error_not_5xx(crm_client: TestClient) -> None:
    """A tenant IS configured but its Key Vault can't be read — the D365
    credentials can't be resolved at all. Must still be a 200 + relayable
    error, never an unhandled 500 leaking out of the resolver."""
    _seed_credentials(crm_client, "geneco")
    respx.get(f"{KV_URL}/secrets/Crm-Client-Id").mock(side_effect=httpx.ConnectError("boom"))
    with _stub_tokens():
        resp = _rpc(
            crm_client,
            "geneco",
            "tools/call",
            {"name": "verify_account", "arguments": {"account_id": "ACC1", "mobile_number": "0123456789"}},
        )
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["isError"] is True
    assert "error" in result["structuredContent"]


@respx.mock
def test_tools_call_vault_failure_on_case_tools_is_honest_error_not_5xx(
    crm_client: TestClient,
) -> None:
    """Same guarantee via the shared re-verify path in ``_common.py``."""
    _seed_credentials(crm_client, "geneco")
    respx.get(f"{KV_URL}/secrets/Crm-Client-Id").mock(return_value=Response(403, text="Forbidden"))
    with _stub_tokens():
        resp = _rpc(
            crm_client,
            "geneco",
            "tools/call",
            {
                "name": "get_support_cases",
                "arguments": {"account_id": "ACC1", "mobile_number": "0123456789"},
            },
        )
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["isError"] is True
    assert "error" in result["structuredContent"]


def test_tools_call_unknown_tool_is_honest_error(crm_client: TestClient) -> None:
    resp = _rpc(crm_client, "geneco", "tools/call", {"name": "not_a_tool", "arguments": {}})
    result = resp.json()["result"]
    assert result["isError"] is True


def test_tools_call_invalid_arguments_is_honest_error(crm_client: TestClient) -> None:
    resp = _rpc(crm_client, "geneco", "tools/call", {"name": "verify_account", "arguments": {}})
    result = resp.json()["result"]
    assert result["isError"] is True


def test_unknown_method_returns_jsonrpc_error(crm_client: TestClient) -> None:
    resp = _rpc(crm_client, "geneco", "not/a/method")
    body = resp.json()
    assert body["error"]["code"] == -32601


CUSTOM_API_URL = "https://custom-api.geneco.example"


def _seed_custom_api_credentials(mcp: TestClient, tenant_slug: str) -> None:
    """Register SSP credentials — always against the SSP server, whose
    ``credentials`` path is its own, not the CRM server's."""
    resp = mcp.post(
        f"/{tenant_slug}/credentials",
        headers={"X-Admin-Api-Key": TEST_ADMIN_API_KEY},
        json={"base_url": CUSTOM_API_URL, "username": "cxchat-svc", "password": "s1"},
    )
    assert resp.status_code == 200


def test_tools_call_check_fee_waiver_status_no_credentials_is_honest_error(
    ssp_client: TestClient,
) -> None:
    resp = _rpc(
        ssp_client,
        "no-credentials-tenant",
        "tools/call",
        {"name": "check_fee_waiver_status", "arguments": {"account_number": "GC8063979W"}},
    )
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["isError"] is True
    assert "not available" in result["structuredContent"]["error"]


@respx.mock
def test_tools_call_check_fee_waiver_status_happy_path(ssp_client: TestClient) -> None:
    _seed_custom_api_credentials(ssp_client, "geneco")
    respx.post(f"{CUSTOM_API_URL}/api/cxchat/v2/feewaiver").mock(
        return_value=Response(
            200,
            json={
                "FeeWaiverLines": [{"FeeType": "ETF", "Amount": 50.0, "ContractStatus": "Active"}],
                "Remarks": "ok",
            },
        )
    )
    resp = _rpc(
        ssp_client,
        "geneco",
        "tools/call",
        {"name": "check_fee_waiver_status", "arguments": {"account_number": "GC8063979W"}},
    )
    result = resp.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["fee_waiver_lines"] == [
        {"FeeType": "ETF", "Amount": 50.0, "ContractStatus": "Active"}
    ]
    assert result["structuredContent"]["remarks"] == "ok"


@respx.mock
def test_tools_call_check_fee_waiver_status_failure_is_honest_error_not_5xx(
    ssp_client: TestClient,
) -> None:
    _seed_custom_api_credentials(ssp_client, "geneco")
    respx.post(f"{CUSTOM_API_URL}/api/cxchat/v2/feewaiver").mock(
        side_effect=httpx.ConnectError("boom")
    )
    resp = _rpc(
        ssp_client,
        "geneco",
        "tools/call",
        {"name": "check_fee_waiver_status", "arguments": {"account_number": "GC8063979W"}},
    )
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["isError"] is True
    assert "error" in result["structuredContent"]


def test_tools_call_check_recontract_eligibility_no_credentials_is_honest_error(
    ssp_client: TestClient,
) -> None:
    resp = _rpc(
        ssp_client,
        "no-credentials-tenant",
        "tools/call",
        {"name": "check_recontract_eligibility", "arguments": {"account_number": "GC8063979W"}},
    )
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["isError"] is True
    assert "not available" in result["structuredContent"]["error"]


@respx.mock
def test_tools_call_check_recontract_eligibility_happy_path(ssp_client: TestClient) -> None:
    _seed_custom_api_credentials(ssp_client, "geneco")
    respx.post(f"{CUSTOM_API_URL}/api/cxchat/v2/checkrecontracteligibility").mock(
        return_value=Response(
            200,
            json={"Result": "Not Eligible", "Remarks": "Account doesn't have any Active Contract"},
        )
    )
    resp = _rpc(
        ssp_client,
        "geneco",
        "tools/call",
        {"name": "check_recontract_eligibility", "arguments": {"account_number": "GC8063979W"}},
    )
    result = resp.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["result"] == "Not Eligible"
    assert result["structuredContent"]["remarks"] == "Account doesn't have any Active Contract"


@respx.mock
def test_tools_call_check_recontract_eligibility_failure_is_honest_error_not_5xx(
    ssp_client: TestClient,
) -> None:
    _seed_custom_api_credentials(ssp_client, "geneco")
    respx.post(f"{CUSTOM_API_URL}/api/cxchat/v2/checkrecontracteligibility").mock(
        side_effect=httpx.ConnectError("boom")
    )
    resp = _rpc(
        ssp_client,
        "geneco",
        "tools/call",
        {"name": "check_recontract_eligibility", "arguments": {"account_number": "GC8063979W"}},
    )
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["isError"] is True
    assert "error" in result["structuredContent"]


def test_tools_call_reverse_fee_no_credentials_is_honest_error(ssp_client: TestClient) -> None:
    resp = _rpc(
        ssp_client,
        "no-credentials-tenant",
        "tools/call",
        {"name": "reverse_fee", "arguments": {"account_number": "GC8063979W"}},
    )
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["isError"] is True
    assert "not available" in result["structuredContent"]["error"]


@respx.mock
def test_tools_call_reverse_fee_happy_path(ssp_client: TestClient) -> None:
    _seed_custom_api_credentials(ssp_client, "geneco")
    respx.post(f"{CUSTOM_API_URL}/api/cxchat/v2/feereversal").mock(
        return_value=Response(
            200,
            json={"Result": "Success", "Remarks": "Fee reversals were processed successfully."},
        )
    )
    resp = _rpc(
        ssp_client,
        "geneco",
        "tools/call",
        {"name": "reverse_fee", "arguments": {"account_number": "GC8063979W"}},
    )
    result = resp.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["result"] == "Success"
    assert result["structuredContent"]["remarks"] == "Fee reversals were processed successfully."


@respx.mock
def test_tools_call_reverse_fee_failure_is_honest_error_not_5xx(ssp_client: TestClient) -> None:
    _seed_custom_api_credentials(ssp_client, "geneco")
    respx.post(f"{CUSTOM_API_URL}/api/cxchat/v2/feereversal").mock(
        side_effect=httpx.ConnectError("boom")
    )
    resp = _rpc(
        ssp_client,
        "geneco",
        "tools/call",
        {"name": "reverse_fee", "arguments": {"account_number": "GC8063979W"}},
    )
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["isError"] is True
    assert "error" in result["structuredContent"]


def test_tools_call_request_password_reset_no_credentials_is_honest_error(
    ssp_client: TestClient,
) -> None:
    resp = _rpc(
        ssp_client,
        "no-credentials-tenant",
        "tools/call",
        {"name": "request_password_reset", "arguments": {"email": "customer@example.com"}},
    )
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["isError"] is True
    assert "not available" in result["structuredContent"]["error"]


@respx.mock
def test_tools_call_request_password_reset_happy_path(ssp_client: TestClient) -> None:
    _seed_custom_api_credentials(ssp_client, "geneco")
    respx.post(f"{CUSTOM_API_URL}/api/forgotpasswordv2").mock(
        return_value=Response(
            200, json={"SuccessCode": "200", "Message": "Password Reset Instructions Sent"}
        )
    )
    resp = _rpc(
        ssp_client,
        "geneco",
        "tools/call",
        {"name": "request_password_reset", "arguments": {"email": "customer@example.com"}},
    )
    result = resp.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["success"] is True
    assert result["structuredContent"]["message"] == "Password Reset Instructions Sent"


@respx.mock
def test_tools_call_request_password_reset_declined_is_not_an_error(ssp_client: TestClient) -> None:
    """The vendor's own decline (e.g. unregistered email) is a normal tool
    result, not a tool error — only a real connector failure sets
    isError."""
    _seed_custom_api_credentials(ssp_client, "geneco")
    respx.post(f"{CUSTOM_API_URL}/api/forgotpasswordv2").mock(
        return_value=Response(
            502,
            json={
                "ErrorCode": "502",
                "Error": "Bad Gateway",
                "Stack": "This email address is not registered with Geneco.",
            },
        )
    )
    resp = _rpc(
        ssp_client,
        "geneco",
        "tools/call",
        {"name": "request_password_reset", "arguments": {"email": "unregistered@example.com"}},
    )
    result = resp.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["success"] is False
    assert result["structuredContent"]["message"] == "This email address is not registered with Geneco."


@respx.mock
def test_tools_call_request_password_reset_failure_is_honest_error_not_5xx(
    ssp_client: TestClient,
) -> None:
    _seed_custom_api_credentials(ssp_client, "geneco")
    respx.post(f"{CUSTOM_API_URL}/api/forgotpasswordv2").mock(side_effect=httpx.ConnectError("boom"))
    resp = _rpc(
        ssp_client,
        "geneco",
        "tools/call",
        {"name": "request_password_reset", "arguments": {"email": "customer@example.com"}},
    )
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["isError"] is True
    assert "error" in result["structuredContent"]


def test_ssp_initialize_reports_the_ssp_server_name(ssp_client: TestClient) -> None:
    resp = _rpc(ssp_client, "geneco", "initialize", {})
    assert resp.json()["result"]["serverInfo"]["name"] == "geneco-ssp-mcp"


def test_ssp_tools_list_is_empty_before_credentials_are_registered(
    ssp_client: TestClient,
) -> None:
    resp = _rpc(ssp_client, "no-credentials-tenant", "tools/list", {})
    assert resp.json()["result"]["tools"] == []


def test_ssp_tools_list_returns_the_ssp_tools_once_registered(ssp_client: TestClient) -> None:
    _seed_custom_api_credentials(ssp_client, "geneco")
    resp = _rpc(ssp_client, "geneco", "tools/list", {})
    tools = {t["name"] for t in resp.json()["result"]["tools"]}
    assert tools == {
        "check_fee_waiver_status",
        "check_recontract_eligibility",
        "reverse_fee",
        "request_password_reset",
    }


def test_ssp_tools_appear_independently_of_the_crm_stage(
    ssp_client: TestClient, crm_client: TestClient
) -> None:
    """The staged case this split exists for: SSP credentials arrive first,
    and its tools work while CRM is still unregistered."""
    _seed_custom_api_credentials(ssp_client, "geneco")
    assert len(_rpc(ssp_client, "geneco", "tools/list", {}).json()["result"]["tools"]) == 4
    assert _rpc(crm_client, "geneco", "tools/list", {}).json()["result"]["tools"] == []

    _seed_credentials(crm_client, "geneco")
    assert len(_rpc(ssp_client, "geneco", "tools/list", {}).json()["result"]["tools"]) == 4
    assert len(_rpc(crm_client, "geneco", "tools/list", {}).json()["result"]["tools"]) == 3


def test_ssp_server_will_not_run_a_crm_tool(
    ssp_client: TestClient, crm_client: TestClient
) -> None:
    _seed_credentials(crm_client, "geneco")
    resp = _rpc(
        ssp_client,
        "geneco",
        "tools/call",
        {"name": "verify_account", "arguments": {"account_id": "ACC1", "mobile_number": "0123456789"}},
    )
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["isError"] is True
    assert "unknown tool" in result["content"][0]["text"]
