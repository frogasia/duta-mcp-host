"""``get_support_cases`` — case inquiry.

Re-verifies account_id/mobile_number, then lists that customer's recent
D365 incidents directly (this service owns its own D365 connector + stored
credentials — see ``store/credentials.py``).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from geneco_mcp.clients.d365_connector import ConnectorError
from geneco_mcp.store.credentials import CredentialStore
from geneco_mcp.store.crm_resolver import CrmCredentialResolver
from geneco_mcp.tools._common import UNAVAILABLE_ERROR, verify_and_get_guid
from geneco_mcp.tools.registry import ToolContext, register


class GetSupportCasesInput(BaseModel):
    account_id: str = Field(description="The customer's account number.")
    mobile_number: str = Field(description="The mobile number on file for the account.")


@register(
    "get_support_cases",
    kind="crm",
    description=(
        "List a customer's support cases, after verifying their account "
        "number and mobile number match."
    ),
    input_model=GetSupportCasesInput,
)
async def handle(
    ctx: ToolContext, tenant_slug: str, args: GetSupportCasesInput
) -> dict[str, Any]:
    store = CredentialStore(ctx.session, encryption_key=ctx.encryption_key)
    resolver = CrmCredentialResolver(store, ctx.crm_cache)
    connector, account_guid, error = await verify_and_get_guid(
        resolver, tenant_slug, account_id=args.account_id, mobile_number=args.mobile_number
    )
    if error is not None:
        return error
    assert connector is not None and account_guid is not None
    try:
        cases = await connector.list_incidents_for_customer(account_guid)
    except ConnectorError:
        return {"error": UNAVAILABLE_ERROR}
    return {"cases": cases, "count": len(cases)}
