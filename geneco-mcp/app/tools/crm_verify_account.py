"""``verify_account`` — account inquiry.

Confirms an account_id/mobile_number pair against the tenant's D365 org
directly (this service owns its own D365 connector + stored credentials —
see ``store/credentials.py``) and returns the account holder's name and
balance if they match. Re-verifies against D365 on every call — no
session/conversation state is available to cache a prior verification
against (an MCP ``tools/call`` carries no conversation identity), mirroring
duta-ilmu's own ``d365_tools.verify_account``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from geneco_mcp.clients.d365_connector import ConnectorError
from geneco_mcp.store.credentials import CredentialStore
from geneco_mcp.store.crm_resolver import CrmCredentialResolver
from geneco_mcp.tools._common import NOT_CONFIGURED_ERROR, UNAVAILABLE_ERROR
from geneco_mcp.tools.registry import ToolContext, register


class VerifyAccountInput(BaseModel):
    account_id: str = Field(description="The customer's account number.")
    mobile_number: str = Field(description="The mobile number on file for the account.")


@register(
    "verify_account",
    kind="crm",
    description=(
        "Verify a customer's account by account number and mobile number, "
        "returning the account holder's name and balance if they match."
    ),
    input_model=VerifyAccountInput,
)
async def handle(ctx: ToolContext, tenant_slug: str, args: VerifyAccountInput) -> dict[str, Any]:
    store = CredentialStore(ctx.session, encryption_key=ctx.encryption_key)
    resolver = CrmCredentialResolver(store, ctx.crm_cache)
    try:
        connector = await resolver.get_connector(tenant_slug)
    except ConnectorError:
        # The tenant is configured but its vault couldn't be read.
        return {"error": UNAVAILABLE_ERROR}
    if connector is None:
        return {"error": NOT_CONFIGURED_ERROR}
    try:
        result = await connector.get_account_by_filter(args.account_id, args.mobile_number)
    except ConnectorError:
        return {"error": UNAVAILABLE_ERROR}
    if result is None:
        return {
            "verified": False,
            "message": "Account number and mobile number do not match. Please check and try again.",
        }
    return {
        "verified": True,
        "account_id": args.account_id,
        "name": result["name"],
        "account_balance": result["account_balance"],
    }
