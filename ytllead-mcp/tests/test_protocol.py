from __future__ import annotations

import time

import httpx
import jwt
import pytest
import respx
from conftest import PATH, SECRET, call, fake_user, identity, make_client, rpc, token

from ytllead_mcp.lead import USER_INFO_URL
from ytllead_mcp.main import NOT_FOUND_ERROR, NOT_SIGNED_IN_ERROR, UNAVAILABLE_ERROR


def ok(user_id: int) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "data": fake_user(user_id)})


@respx.mock
def test_full_sequence_returns_signed_in_users_profile(client) -> None:
    route = respx.get(USER_INFO_URL).mock(return_value=ok(86))

    init = client.post(PATH, json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init.json()["result"]["serverInfo"]["name"] == "ytllead-mcp"
    assert init.headers["Mcp-Session-Id"]
    notified = client.post(PATH, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert notified.status_code == 202
    tools = rpc(client, "tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == ["get_my_lead_profile"]

    result = call(client, identity(86))
    assert result["isError"] is False
    assert "- LEAD user ID: 86" in result["text"]
    sent = route.calls.last.request
    assert sent.url.params["id"] == "86"
    assert sent.headers["Authorization"] == "Bearer test-token"


@respx.mock
def test_id_in_arguments_is_ignored(client) -> None:
    route = respx.get(USER_INFO_URL).mock(return_value=ok(86))
    assert "- LEAD user ID: 86" in call(client, identity(86), arguments={"id": 87})["text"]
    assert route.calls.last.request.url.params["id"] == "86"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Verified-User-Id": "86"},  # old header: no longer trusted
        {
            "X-Internal-Api-Key": "anything"
        },  # anonymous visitor: no token (a stray key header changes nothing)
        {"X-Verified-User-Token": "ké".encode("latin-1")},  # non-ASCII junk token
        identity(86, secret="some-other-secret-0123456789abcdef"),
        identity(86, iat=time.time() - 200),  # expired beyond the 30s leeway
        identity(86, ttl=3600),  # lifetime longer than allowed
        identity(86, iat=None, ttl=60, exp=None),  # no exp claim
        {"X-Verified-User-Token": token(86)},  # non-string sub
        {"X-Verified-User-Token": "not.a.jwt"},
        {
            "X-Verified-User-Token": jwt.encode(
                {"sub": "86", "iat": int(time.time()), "exp": int(time.time()) + 60},
                None,
                algorithm="none",
            ),
        },
        identity("abc"),
        identity("0"),
        identity("-5"),
        identity("86.0"),
        identity(""),
    ],
)
@respx.mock
def test_no_verified_identity_never_calls_lead(client, headers) -> None:
    route = respx.get(USER_INFO_URL)
    assert call(client, headers) == {"isError": True, "text": NOT_SIGNED_IN_ERROR}
    assert not route.called


@respx.mock
def test_any_single_path_segment_is_served(client) -> None:
    """Older /<tenant-slug> URLs keep working; the segment is ignored."""
    assert rpc(client, "tools/list", path="/some-old-slug")["result"]["tools"]


@pytest.mark.parametrize("missing", ["lead_api_token", "identity_signing_secret"])
@respx.mock
def test_unconfigured_server_advertises_nothing(missing) -> None:
    route = respx.get(USER_INFO_URL)
    client = make_client(**{missing: ""})
    assert rpc(client, "tools/list")["result"]["tools"] == []
    assert call(client, identity(86))["isError"] is True
    assert not route.called


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(401),
        httpx.Response(200, text="<html>"),
        httpx.Response(200, json=["x"]),
        httpx.Response(200, json={"data": "x"}),
        ok(87),
    ],  # last one: LEAD returned someone else's record
)
@respx.mock
def test_lead_failure_is_honest_error_with_http_200(client, response) -> None:
    respx.get(USER_INFO_URL).mock(return_value=response)
    assert call(client, identity(86)) == {"isError": True, "text": UNAVAILABLE_ERROR}


@respx.mock
def test_network_failure_is_honest_error(client) -> None:
    respx.get(USER_INFO_URL).mock(side_effect=httpx.ConnectTimeout("t"))
    assert call(client, identity(86))["text"] == UNAVAILABLE_ERROR


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404),
        httpx.Response(200, json={"success": False}),
        httpx.Response(200, json={"success": True, "data": None}),
    ],
)
@respx.mock
def test_unknown_user_is_not_found(client, response) -> None:
    respx.get(USER_INFO_URL).mock(return_value=response)
    assert call(client, identity(86))["text"] == NOT_FOUND_ERROR


def test_unknown_tool_and_method(client) -> None:
    body = rpc(client, "tools/call", {"name": "nope"}, headers=identity(86))
    assert body["result"]["isError"] is True
    assert rpc(client, "resources/list")["error"]["code"] == -32601


@respx.mock
def test_token_within_leeway_is_accepted(client) -> None:
    """Expired 20s ago: inside the 30s clock-skew leeway."""
    respx.get(USER_INFO_URL).mock(return_value=ok(86))
    assert call(client, identity(86, iat=time.time() - 80))["isError"] is False


def test_non_object_params_is_a_jsonrpc_error_not_a_500(client) -> None:
    resp = client.post(
        PATH, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": ["x"]}
    )
    assert resp.status_code == 200 and resp.json()["error"]["code"] == -32602


@respx.mock
def test_render_failure_is_honest_error_not_a_500(client) -> None:
    """LEAD has no schema: a field in an unexpected shape must not crash the call."""
    weird = {
        **fake_user(86),
        "user_personality": {"personality": {"name": "X", "lead_agenda": ["a"]}},
    }
    respx.get(USER_INFO_URL).mock(
        return_value=httpx.Response(200, json={"success": True, "data": weird})
    )
    assert call(client, identity(86)) == {"isError": True, "text": UNAVAILABLE_ERROR}


def test_string_exp_is_rejected_not_a_500(client) -> None:
    now = int(time.time())
    bad = jwt.encode({"sub": "86", "iat": now, "exp": str(now + 60)}, SECRET, algorithm="HS256")
    assert call(client, {"X-Verified-User-Token": bad}) == {
        "isError": True,
        "text": NOT_SIGNED_IN_ERROR,
    }


def test_future_iat_is_rejected(client) -> None:
    later = time.time() + 3600
    assert call(client, identity(86, iat=later)) == {"isError": True, "text": NOT_SIGNED_IN_ERROR}
