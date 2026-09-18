"""Direct D365 (Dataverse Web API) connector — OAuth2 client-credentials via
msal against the tenant's Entra app registration.

Ported from duta-ilmu's integration-hub
(``services/integration-hub/src/integration_hub/connectors/d365.py``), with
one deliberate fix: every outbound call (MSAL token acquisition, the
Dataverse HTTP request, response parsing) is wrapped so a real failure
always raises ``ConnectorError`` instead of leaking an unhandled exception —
that gap in integration-hub's version is exactly the bug that motivated
this repo owning its own connector (a 500 instead of an honest
``{"error": ...}``). This is now the one place that boundary is enforced,
mirroring the "honest errors" rule this repo already applied to
integration-hub calls.

Only the three operations geneco-mcp's tools actually use are ported
(``get_account_by_filter``, ``create_case``, ``list_incidents_for_customer``)
— no ``add_comment``/``sync_status``, since nothing here calls them (YAGNI).
The one addition is ``who_am_i``, the credential-verify endpoint's
connectivity probe (``protocol/admin_router.py``).
"""

from __future__ import annotations

from typing import Any

import httpx

API_PATH = "/api/data/v9.2"

DEFAULT_INCIDENT_MAP = {"subject": "title", "description": "description"}

ODATA_HEADERS = {
    "OData-MaxVersion": "4.0",
    "OData-Version": "4.0",
    "Accept": "application/json",
    "Content-Type": "application/json; charset=utf-8",
}


class ConnectorError(Exception):
    """``status_code`` is set only when Dataverse actually answered with an
    HTTP error — carried structurally (like ``KeyVaultError``'s) so callers
    that explain *why* a call failed can branch on it rather than parsing
    message text. ``None`` means the failure was before/around the response
    (token acquisition, network, malformed body)."""

    def __init__(
        self, message: str, *, retryable: bool = True, status_code: int | None = None
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


def _odata_escape(value: str) -> str:
    """Escape a value for use inside an OData ``$filter`` string literal
    (single quotes double per the OData ABNF) — filter values here come
    from chat-supplied account/mobile numbers, not trusted input."""
    return value.replace("'", "''")


class D365Connector:
    """One instance per call, built from a tenant's stored credentials —
    stateless across calls, no cached HTTP client/session."""

    def __init__(
        self,
        *,
        org_url: str,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        field_map: dict[str, str] | None = None,
    ) -> None:
        self.org_url = str(org_url).rstrip("/")
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.field_map = field_map or {}
        self._msal_app: Any = None

    @property
    def _authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}"

    @property
    def _scope(self) -> str:
        return f"{self.org_url}/.default"

    @property
    def _api_base(self) -> str:
        return f"{self.org_url}{API_PATH}"

    def _get_msal_app(self) -> Any:
        if self._msal_app is None:
            import msal

            self._msal_app = msal.ConfidentialClientApplication(
                client_id=self.client_id,
                client_credential=self.client_secret,
                authority=self._authority,
            )
        return self._msal_app

    def _acquire_token(self) -> str:
        try:
            result = self._get_msal_app().acquire_token_for_client(scopes=[self._scope])
        except Exception as exc:  # msal/network failure — never let this leak as a 500
            raise ConnectorError(f"token acquisition failed: {exc}") from exc
        if "access_token" not in result:
            raise ConnectorError(
                f"token acquisition failed: {result.get('error_description', result)}",
                retryable=False,
            )
        return result["access_token"]

    def _headers(self) -> dict[str, str]:
        return {**ODATA_HEADERS, "Authorization": f"Bearer {self._acquire_token()}"}

    async def _request(
        self, method: str, url: str, *, json_body: dict[str, Any] | None = None
    ) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.request(method, url, json=json_body, headers=self._headers())
        except httpx.HTTPError as exc:
            raise ConnectorError(f"D365 request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ConnectorError(
                f"D365 request failed {resp.status_code}: {resp.text[:500]}",
                status_code=resp.status_code,
            )
        return resp

    @staticmethod
    def _json_value(resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except ValueError as exc:
            raise ConnectorError("malformed D365 response (not JSON)") from exc

    def _incident_body(self, *, title: str, description: str, account_guid: str) -> dict[str, Any]:
        field_map = {**DEFAULT_INCIDENT_MAP, **self.field_map}
        src = {"subject": title, "description": description}
        body = {col: src[field] for field, col in field_map.items() if src.get(field)}
        body["customerid_account@odata.bind"] = f"/accounts({account_guid})"
        return body

    async def who_am_i(self) -> dict[str, Any]:
        """Cheapest proof this tenant's credentials actually work: acquire a
        token and call Dataverse's ``WhoAmI``, which touches no business data
        and needs no arguments. Returns Dataverse's own response
        (``UserId``/``BusinessUnitId``/``OrganizationId``).

        Used by the credential-verify endpoint; it lives here rather than in
        the router so the honest-error boundary in ``_request`` covers it like
        every other operation.
        """
        resp = await self._request("GET", f"{self._api_base}/WhoAmI")
        value = self._json_value(resp)
        if not isinstance(value, dict):
            raise ConnectorError("unexpected D365 WhoAmI response shape", retryable=False)
        return value

    async def create_case(self, *, title: str, description: str, account_guid: str) -> str:
        """Create a D365 incident bound to ``account_guid``, return its GUID."""
        resp = await self._request(
            "POST",
            f"{self._api_base}/incidents",
            json_body=self._incident_body(
                title=title, description=description, account_guid=account_guid
            ),
        )
        # Dataverse returns the created entity's URL in OData-EntityId, e.g.
        # https://org.crm.dynamics.com/api/data/v9.2/incidents(<guid>)
        entity_id = resp.headers.get("OData-EntityId", "")
        guid = entity_id.rsplit("(", 1)[-1].rstrip(")") if "(" in entity_id else ""
        if not guid:
            raise ConnectorError("missing OData-EntityId in D365 response", retryable=False)
        return guid

    async def get_account_by_filter(
        self, account_id: str, mobile_number: str
    ) -> dict[str, Any] | None:
        """Query ``accounts`` by account number + mobile. Returns ``None``
        if no account matches both fields (not an error — "no match" is a
        valid outcome)."""
        cleaned_mobile = mobile_number.replace(" ", "").replace("-", "")
        url = (
            f"{self._api_base}/accounts"
            "?$select=accountid,name,accountnumber,oem_accountbalance"
            f"&$filter=accountnumber eq '{_odata_escape(account_id)}'"
            f" and telephone1 eq '{_odata_escape(cleaned_mobile)}'"
        )
        resp = await self._request("GET", url)
        records = self._json_value(resp).get("value", [])
        if not records:
            return None
        record = records[0]
        try:
            return {
                "account_guid": record["accountid"],
                "name": record.get("name", ""),
                "account_balance": record.get("oem_accountbalance", 0),
            }
        except KeyError as exc:
            raise ConnectorError(f"unexpected D365 response shape: missing {exc}") from exc

    async def list_incidents_for_customer(self, account_guid: str) -> list[dict[str, Any]]:
        """Recent open/resolved incidents for a customer account. Empty list
        means "no cases found", not an error."""
        url = (
            f"{self._api_base}/incidents"
            "?$select=title,ticketnumber,statecode,statuscode,createdon,description"
            f"&$filter=_customerid_value eq '{_odata_escape(account_guid)}'"
            "&$orderby=createdon desc&$top=10"
        )
        resp = await self._request("GET", url)
        status_map = {0: "Active", 1: "Resolved", 2: "Cancelled"}
        cases = []
        for record in self._json_value(resp).get("value", []):
            cases.append(
                {
                    "ticket_number": record.get("ticketnumber", ""),
                    "title": record.get("title", ""),
                    "status": status_map.get(record.get("statecode"), "Unknown"),
                    "created_on": record.get("createdon", ""),
                    "description": (record.get("description") or "")[:200],
                }
            )
        return cases
