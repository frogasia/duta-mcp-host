from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

import httpx
import pytest
import respx
from httpx import Response

from geneco_mcp.clients.custom_api_client import ConnectorError as CustomApiConnectorError
from geneco_mcp.clients.custom_api_client import CustomApiClient
from geneco_mcp.clients.d365_connector import ConnectorError, D365Connector
from geneco_mcp.tools import all_tools, get_tool
from geneco_mcp.tools.api_check_fee_waiver_status import CheckFeeWaiverStatusInput
from geneco_mcp.tools.api_check_recontract_eligibility import CheckRecontractEligibilityInput
from geneco_mcp.tools.api_request_password_reset import RequestPasswordResetInput
from geneco_mcp.tools.api_reverse_fee import ReverseFeeInput
from geneco_mcp.tools.crm_verify_account import VerifyAccountInput

ORG_URL = "https://geneco.crm.dynamics.com"
CUSTOM_API_URL = "https://custom-api.geneco.example"


@pytest.fixture
def connector() -> D365Connector:
    return D365Connector(org_url=ORG_URL, tenant_id="t1", client_id="c1", client_secret="s1")


@contextmanager
def stub_token(connector: D365Connector):
    """MSAL's own token-acquisition handshake (authority discovery + the
    token endpoint) isn't what this module owns — stub it so these tests
    exercise our own request/response handling against Dataverse, not
    msal's internals or a real network call."""
    with patch.object(connector, "_acquire_token", return_value="fake-token"):
        yield


def test_all_tools_registered() -> None:
    assert {t.name for t in all_tools()} == {
        "verify_account",
        "create_support_case",
        "get_support_cases",
        "check_fee_waiver_status",
        "check_recontract_eligibility",
        "reverse_fee",
        "request_password_reset",
    }


def test_verify_account_registered_with_its_input_model() -> None:
    spec = get_tool("verify_account")
    assert spec is not None
    assert spec.input_model is VerifyAccountInput


def test_check_fee_waiver_status_registered_with_its_input_model() -> None:
    spec = get_tool("check_fee_waiver_status")
    assert spec is not None
    assert spec.input_model is CheckFeeWaiverStatusInput


def test_check_recontract_eligibility_registered_with_its_input_model() -> None:
    spec = get_tool("check_recontract_eligibility")
    assert spec is not None
    assert spec.input_model is CheckRecontractEligibilityInput


def test_reverse_fee_registered_with_its_input_model() -> None:
    spec = get_tool("reverse_fee")
    assert spec is not None
    assert spec.input_model is ReverseFeeInput


def test_request_password_reset_registered_with_its_input_model() -> None:
    spec = get_tool("request_password_reset")
    assert spec is not None
    assert spec.input_model is RequestPasswordResetInput


@respx.mock
async def test_get_account_by_filter_found(connector: D365Connector) -> None:
    respx.get(f"{ORG_URL}/api/data/v9.2/accounts").mock(
        return_value=Response(
            200,
            json={"value": [{"accountid": "guid-1", "name": "Jane", "oem_accountbalance": 5.0}]},
        )
    )
    with stub_token(connector):
        result = await connector.get_account_by_filter("A1", "0123456789")
    assert result == {"account_guid": "guid-1", "name": "Jane", "account_balance": 5.0}


@respx.mock
async def test_get_account_by_filter_no_match_returns_none(connector: D365Connector) -> None:
    respx.get(f"{ORG_URL}/api/data/v9.2/accounts").mock(return_value=Response(200, json={"value": []}))
    with stub_token(connector):
        result = await connector.get_account_by_filter("A1", "0123456789")
    assert result is None


@respx.mock
async def test_get_account_by_filter_network_failure_raises_connector_error(
    connector: D365Connector,
) -> None:
    respx.get(f"{ORG_URL}/api/data/v9.2/accounts").mock(side_effect=httpx.ConnectError("boom"))
    with stub_token(connector):
        with pytest.raises(ConnectorError):
            await connector.get_account_by_filter("A1", "0123456789")


@respx.mock
async def test_get_account_by_filter_non_200_raises_connector_error_not_uncaught(
    connector: D365Connector,
) -> None:
    """The exact gap this port fixes vs. integration-hub's version: a bad
    HTTP status must become ConnectorError, never bubble up raw."""
    respx.get(f"{ORG_URL}/api/data/v9.2/accounts").mock(return_value=Response(500, text="boom"))
    with stub_token(connector):
        with pytest.raises(ConnectorError):
            await connector.get_account_by_filter("A1", "0123456789")


@respx.mock
async def test_who_am_i_returns_dataverse_identity(connector: D365Connector) -> None:
    """The credential-verify probe: cheapest proof a token works, touching
    no business data."""
    route = respx.get(f"{ORG_URL}/api/data/v9.2/WhoAmI").mock(
        return_value=Response(200, json={"UserId": "u-1", "OrganizationId": "o-1"})
    )
    with stub_token(connector):
        assert (await connector.who_am_i())["OrganizationId"] == "o-1"
    assert route.called


@respx.mock
async def test_who_am_i_non_200_raises_connector_error_carrying_the_status(
    connector: D365Connector,
) -> None:
    respx.get(f"{ORG_URL}/api/data/v9.2/WhoAmI").mock(return_value=Response(403, text="Forbidden"))
    with stub_token(connector):
        with pytest.raises(ConnectorError) as exc:
            await connector.who_am_i()
    assert exc.value.status_code == 403


@respx.mock
async def test_who_am_i_network_failure_raises_connector_error(connector: D365Connector) -> None:
    respx.get(f"{ORG_URL}/api/data/v9.2/WhoAmI").mock(side_effect=httpx.ConnectError("boom"))
    with stub_token(connector):
        with pytest.raises(ConnectorError) as exc:
            await connector.who_am_i()
    assert exc.value.status_code is None


@respx.mock
async def test_create_case_returns_guid(connector: D365Connector) -> None:
    respx.post(f"{ORG_URL}/api/data/v9.2/incidents").mock(
        return_value=Response(
            204, headers={"OData-EntityId": f"{ORG_URL}/api/data/v9.2/incidents(guid-2)"}
        )
    )
    with stub_token(connector):
        guid = await connector.create_case(
            title="Billing issue", description="Overcharged last month", account_guid="guid-1"
        )
    assert guid == "guid-2"


@respx.mock
async def test_create_case_missing_entity_id_raises_connector_error(connector: D365Connector) -> None:
    respx.post(f"{ORG_URL}/api/data/v9.2/incidents").mock(return_value=Response(204))
    with stub_token(connector):
        with pytest.raises(ConnectorError):
            await connector.create_case(title="x", description="y", account_guid="guid-1")


@respx.mock
async def test_list_incidents_for_customer(connector: D365Connector) -> None:
    respx.get(f"{ORG_URL}/api/data/v9.2/incidents").mock(
        return_value=Response(
            200,
            json={
                "value": [
                    {
                        "ticketnumber": "T1",
                        "title": "Issue",
                        "statecode": 0,
                        "createdon": "2026-01-01",
                        "description": "d",
                    }
                ]
            },
        )
    )
    with stub_token(connector):
        cases = await connector.list_incidents_for_customer("guid-1")
    assert cases == [
        {
            "ticket_number": "T1",
            "title": "Issue",
            "status": "Active",
            "created_on": "2026-01-01",
            "description": "d",
        }
    ]


@pytest.fixture
def custom_api_client() -> CustomApiClient:
    return CustomApiClient(base_url=CUSTOM_API_URL, username="cxchat-svc", password="s1")


@respx.mock
async def test_get_fee_waiver_status_returns_lines(custom_api_client: CustomApiClient) -> None:
    respx.post(f"{CUSTOM_API_URL}/api/cxchat/v2/feewaiver").mock(
        return_value=Response(
            200,
            json={
                "FeeWaiverLines": [{"FeeType": "ETF", "Amount": 50.0, "ContractStatus": "Active"}],
                "Remarks": "ok",
            },
        )
    )
    result = await custom_api_client.get_fee_waiver_status("GC8063979W")
    assert result == {
        "fee_waiver_lines": [{"FeeType": "ETF", "Amount": 50.0, "ContractStatus": "Active"}],
        "remarks": "ok",
    }


@respx.mock
async def test_get_fee_waiver_status_no_lines_is_empty_list_not_error(
    custom_api_client: CustomApiClient,
) -> None:
    respx.post(f"{CUSTOM_API_URL}/api/cxchat/v2/feewaiver").mock(
        return_value=Response(200, json={"FeeWaiverLines": [], "Remarks": None})
    )
    result = await custom_api_client.get_fee_waiver_status("GC8063979W")
    assert result == {"fee_waiver_lines": [], "remarks": None}


@respx.mock
async def test_get_fee_waiver_status_network_failure_raises_connector_error(
    custom_api_client: CustomApiClient,
) -> None:
    respx.post(f"{CUSTOM_API_URL}/api/cxchat/v2/feewaiver").mock(
        side_effect=httpx.ConnectError("boom")
    )
    with pytest.raises(CustomApiConnectorError):
        await custom_api_client.get_fee_waiver_status("GC8063979W")


@respx.mock
async def test_get_fee_waiver_status_non_200_raises_connector_error_not_uncaught(
    custom_api_client: CustomApiClient,
) -> None:
    respx.post(f"{CUSTOM_API_URL}/api/cxchat/v2/feewaiver").mock(return_value=Response(500, text="boom"))
    with pytest.raises(CustomApiConnectorError):
        await custom_api_client.get_fee_waiver_status("GC8063979W")


@respx.mock
async def test_check_recontract_eligibility_returns_result(
    custom_api_client: CustomApiClient,
) -> None:
    respx.post(f"{CUSTOM_API_URL}/api/cxchat/v2/checkrecontracteligibility").mock(
        return_value=Response(
            200,
            json={"Result": "Not Eligible", "Remarks": "Account doesn't have any Active Contract"},
        )
    )
    result = await custom_api_client.check_recontract_eligibility("GC8063979W")
    assert result == {
        "result": "Not Eligible",
        "remarks": "Account doesn't have any Active Contract",
    }


@respx.mock
async def test_check_recontract_eligibility_network_failure_raises_connector_error(
    custom_api_client: CustomApiClient,
) -> None:
    respx.post(f"{CUSTOM_API_URL}/api/cxchat/v2/checkrecontracteligibility").mock(
        side_effect=httpx.ConnectError("boom")
    )
    with pytest.raises(CustomApiConnectorError):
        await custom_api_client.check_recontract_eligibility("GC8063979W")


@respx.mock
async def test_check_recontract_eligibility_non_200_raises_connector_error_not_uncaught(
    custom_api_client: CustomApiClient,
) -> None:
    respx.post(f"{CUSTOM_API_URL}/api/cxchat/v2/checkrecontracteligibility").mock(
        return_value=Response(500, text="boom")
    )
    with pytest.raises(CustomApiConnectorError):
        await custom_api_client.check_recontract_eligibility("GC8063979W")


@respx.mock
async def test_reverse_fee_returns_result(custom_api_client: CustomApiClient) -> None:
    respx.post(f"{CUSTOM_API_URL}/api/cxchat/v2/feereversal").mock(
        return_value=Response(
            200,
            json={"Result": "Success", "Remarks": "Fee reversals were processed successfully."},
        )
    )
    result = await custom_api_client.reverse_fee("GC8063979W")
    assert result == {
        "result": "Success",
        "remarks": "Fee reversals were processed successfully.",
    }


@respx.mock
async def test_reverse_fee_network_failure_raises_connector_error(
    custom_api_client: CustomApiClient,
) -> None:
    respx.post(f"{CUSTOM_API_URL}/api/cxchat/v2/feereversal").mock(
        side_effect=httpx.ConnectError("boom")
    )
    with pytest.raises(CustomApiConnectorError):
        await custom_api_client.reverse_fee("GC8063979W")


@respx.mock
async def test_reverse_fee_non_200_raises_connector_error_not_uncaught(
    custom_api_client: CustomApiClient,
) -> None:
    respx.post(f"{CUSTOM_API_URL}/api/cxchat/v2/feereversal").mock(
        return_value=Response(500, text="boom")
    )
    with pytest.raises(CustomApiConnectorError):
        await custom_api_client.reverse_fee("GC8063979W")


@respx.mock
async def test_request_password_reset_success(custom_api_client: CustomApiClient) -> None:
    respx.post(f"{CUSTOM_API_URL}/api/forgotpasswordv2").mock(
        return_value=Response(
            200, json={"SuccessCode": "200", "Message": "Password Reset Instructions Sent"}
        )
    )
    result = await custom_api_client.request_password_reset("customer@example.com")
    assert result == {"success": True, "message": "Password Reset Instructions Sent"}


@respx.mock
async def test_request_password_reset_declined_with_relayable_message_is_not_an_error(
    custom_api_client: CustomApiClient,
) -> None:
    """A 502-shaped decline that carries a real ``Stack`` message (e.g.
    "email not registered") is a legitimate business outcome, not a
    connector failure — confirmed empirically against SIT, see the
    client method's docstring."""
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
    result = await custom_api_client.request_password_reset("unregistered@example.com")
    assert result == {
        "success": False,
        "message": "This email address is not registered with Geneco.",
    }


@respx.mock
async def test_request_password_reset_network_failure_raises_connector_error(
    custom_api_client: CustomApiClient,
) -> None:
    respx.post(f"{CUSTOM_API_URL}/api/forgotpasswordv2").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(CustomApiConnectorError):
        await custom_api_client.request_password_reset("customer@example.com")


@respx.mock
async def test_request_password_reset_non_json_body_raises_connector_error(
    custom_api_client: CustomApiClient,
) -> None:
    respx.post(f"{CUSTOM_API_URL}/api/forgotpasswordv2").mock(return_value=Response(502, text="boom"))
    with pytest.raises(CustomApiConnectorError):
        await custom_api_client.request_password_reset("customer@example.com")
