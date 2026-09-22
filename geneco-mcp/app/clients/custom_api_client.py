"""Custom API connector — Basic Auth against geneco's SSP cxchat backend
(fee waiver, re-contract eligibility, fee reversal — see
``docs/SSP_Authentication_API_Specs_V3.pdf``).

Ported from geneco-poc's ``backend/app/services/custom_api_client.py``
(only ``get_fee_waiver_status`` was ever implemented there; re-contract and
fee reversal never got written there — this module adds both, matching the
SSP spec), rewritten async and wrapped for the same honest-error boundary
as ``clients/d365_connector.py``: every outbound ``httpx`` call and
response parse raises ``ConnectorError`` on failure rather than leaking an
unhandled exception or silently returning ``None`` (the geneco-poc original
swallowed both "not configured" and "call failed" into the same ``None``
return — this version tells them apart, since only one of those is an
actual error).
"""

from __future__ import annotations

from typing import Any

import httpx

FEE_WAIVER_PATH = "/api/cxchat/v2/feewaiver"
RECONTRACT_ELIGIBILITY_PATH = "/api/cxchat/v2/checkrecontracteligibility"
FEE_REVERSAL_PATH = "/api/cxchat/v2/feereversal"
FORGOT_PASSWORD_PATH = "/api/forgotpasswordv2"


class ConnectorError(Exception):
    """``status_code`` is the SSP API's own HTTP status when there was one,
    and ``None`` for a network-level failure or an unparseable body.
    ``body`` is its response text, truncated.

    Both are carried structurally because ``credentials/verify`` has to tell
    "these credentials were rejected" from "the backend is having a bad day",
    and only the former is the admin's to fix. The status alone cannot say:
    this API answers EVERY auth failure with 400 and puts the reason in the
    body (``{"responsecode":"GEN400","errordescription":"Unauthorized
    request"}``), so the body is the only place that signal exists."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        status_code: int | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
        self.body = body


class CustomApiClient:
    """One instance per call, built from a tenant's stored credentials —
    stateless across calls, no cached HTTP client/session."""

    def __init__(self, *, base_url: str, username: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json"}

    def _auth(self) -> tuple[str, str]:
        return (self.username, self.password)

    async def _request(self, method: str, path: str, *, json_body: dict[str, Any]) -> httpx.Response:
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.request(
                    method, url, json=json_body, headers=self._headers(), auth=self._auth()
                )
        except httpx.HTTPError as exc:
            raise ConnectorError(f"custom API request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ConnectorError(
                f"custom API request failed {resp.status_code}: {resp.text[:500]}",
                status_code=resp.status_code,
                body=resp.text[:500],
            )
        return resp

    @staticmethod
    def _json_value(resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except ValueError as exc:
            raise ConnectorError("malformed custom API response (not JSON)") from exc

    async def verify_access(self, account_number: str) -> None:
        """Prove these credentials are accepted by the SSP API. Returns on
        success, raises ``ConnectorError`` otherwise.

        Probes ``feewaiver`` because it is the only *read-only* endpoint of
        the four with a plain status-code contract. Deliberately NOT
        ``feereversal`` (it mutates a customer's account) and NOT
        ``forgotpasswordv2`` (it emails a real customer, and answers a
        business decline with HTTP 502 — unreadable as an auth signal).

        What a 2xx proves is that Basic Auth was accepted and the endpoint
        ran; the caller decides what a non-auth failure means, since that
        depends on whether it passed a real account number or a probe.
        """
        await self._request(
            "POST", FEE_WAIVER_PATH, json_body={"AccountNumber": account_number}
        )

    async def get_fee_waiver_status(self, account_number: str) -> dict[str, Any]:
        """Fee waiver contract lines (ETF, APDF, LPC, PF) for an account.

        Endpoint per the API Detail tab: ``POST /api/cxchat/v2/feewaiver``,
        Basic auth. Sample request: ``{"AccountNumber": "GC8063979W"}``.
        """
        resp = await self._request(
            "POST", FEE_WAIVER_PATH, json_body={"AccountNumber": account_number}
        )
        data = self._json_value(resp)
        return {
            "fee_waiver_lines": data.get("FeeWaiverLines", []),
            "remarks": data.get("Remarks"),
        }

    async def check_recontract_eligibility(self, account_number: str) -> dict[str, Any]:
        """Whether an account is eligible to re-contract.

        Endpoint per the API Detail tab: ``POST
        /api/cxchat/v2/checkrecontracteligibility``, Basic auth. Sample
        request: ``{"AccountNumber": "GC8063979W"}``, sample response:
        ``{"Result": "Not Eligible", "Remarks": "..."}``.
        """
        resp = await self._request(
            "POST", RECONTRACT_ELIGIBILITY_PATH, json_body={"AccountNumber": account_number}
        )
        data = self._json_value(resp)
        return {
            "result": data.get("Result"),
            "remarks": data.get("Remarks"),
        }

    async def request_password_reset(self, user_id: str, *, device_type: str = "W") -> dict[str, Any]:
        """Trigger a self-service-portal password-reset email.

        Endpoint per the Chatbot Integration Inventory: ``POST
        /api/forgotpasswordv2``, Basic auth. Sample request:
        ``{"UserId": "<registered email>", "DeviceType": "W"}`` — despite
        the field name, ``UserId`` must be the account holder's registered
        email address, not an account number (confirmed empirically
        against SIT: an account number is rejected the same way an
        unregistered email is). ``DeviceType`` only needs to be a single
        character — confirmed against SIT that its content is irrelevant
        (``"1"``, ``"2"``, ``"w"``, ``"M"`` all succeed) but its *length*
        isn't (``""``, ``"10"``, ``"WW"`` all fail the same way an
        unregistered email does); no real enum of values is documented
        anywhere this project has access to. Defaults to ``"W"`` (Web) and
        isn't exposed as a tool argument — every caller of this MCP server
        is a chat/web surface, so the default covers the one case that
        exists today (see YAGNI in ``CLAUDE.md``).

        Unlike the other three cxchat endpoints, this one does NOT use a
        plain HTTP-status success/failure split: confirmed against SIT
        that a real, customer-relayable decline (e.g. "this email isn't
        registered") comes back as **HTTP 502** with
        ``{"ErrorCode": "502", "Error": "Bad Gateway", "Stack":
        "<the actual customer-facing message>"}`` — the same envelope an
        actual backend outage would produce, except a real outage's
        ``Stack`` is the generic "An error has occurred. Please try again
        later." rather than a specific message. Since both look identical
        at the JSON-shape level, and the specific case carries real
        content worth relaying (not fabricating), this method treats any
        response carrying a ``Stack`` value as a relayable decline rather
        than a connector failure — only a non-JSON body or a network-level
        failure still raises ``ConnectorError``.
        """
        url = f"{self.base_url}{FORGOT_PASSWORD_PATH}"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    url,
                    json={"UserId": user_id, "DeviceType": device_type},
                    headers=self._headers(),
                    auth=self._auth(),
                )
        except httpx.HTTPError as exc:
            raise ConnectorError(f"custom API request failed: {exc}") from exc
        data = self._json_value(resp)
        if resp.status_code == 200 and "SuccessCode" in data:
            return {"success": True, "message": data.get("Message")}
        if "Stack" in data:
            return {"success": False, "message": data.get("Stack")}
        raise ConnectorError(f"custom API request failed {resp.status_code}: {resp.text[:500]}")

    async def reverse_fee(self, account_number: str) -> dict[str, Any]:
        """Reverse the applicable fees for an account.

        Endpoint per the API Detail tab: ``POST /api/cxchat/v2/feereversal``,
        Basic auth. Sample request: ``{"AccountNumber": "GC8063979W"}``,
        sample response: ``{"Result": "Success", "Remarks": "..."}``.
        """
        resp = await self._request(
            "POST", FEE_REVERSAL_PATH, json_body={"AccountNumber": account_number}
        )
        data = self._json_value(resp)
        return {
            "result": data.get("Result"),
            "remarks": data.get("Remarks"),
        }
