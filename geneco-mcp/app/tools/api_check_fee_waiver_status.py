"""``check_fee_waiver_status`` — fee waiver inquiry.

Looks up an account's fee waiver contract lines (ETF, APDF, LPC, PF)
against the custom cxchat API directly (this service owns its own stored
credentials for it — see ``store/custom_api_credentials.py``). Unlike the
D365 tools, this account is not re-verified first: the custom API has no
documented shape for that, and account_number here is a direct lookup key
into the fee-waiver endpoint, not something to cross-check against a
mobile number.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from geneco_mcp.clients.custom_api_client import ConnectorError
from geneco_mcp.store.custom_api_credentials import CustomApiCredentialStore
from geneco_mcp.store.ssp_resolver import SspCredentialResolver
from geneco_mcp.tools._common import CUSTOM_API_NOT_CONFIGURED_ERROR, UNAVAILABLE_ERROR
from geneco_mcp.tools.registry import ToolContext, register


class CheckFeeWaiverStatusInput(BaseModel):
    account_number: str = Field(description="The customer's account number.")


@register(
    "check_fee_waiver_status",
    kind="ssp",
    description=(
        "Check a customer's fee waiver contract lines (ETF, APDF, LPC, PF) by account number."
    ),
    input_model=CheckFeeWaiverStatusInput,
)
async def handle(
    ctx: ToolContext, tenant_slug: str, args: CheckFeeWaiverStatusInput
) -> dict[str, Any]:
    store = CustomApiCredentialStore(ctx.session, encryption_key=ctx.encryption_key)
    resolver = SspCredentialResolver(store, ctx.ssp_cache)
    try:
        client = await resolver.get_client(tenant_slug)
    except ConnectorError:
        # The tenant is configured but its vault couldn't be read.
        return {"error": UNAVAILABLE_ERROR}
    if client is None:
        return {"error": CUSTOM_API_NOT_CONFIGURED_ERROR}
    try:
        result = await client.get_fee_waiver_status(args.account_number)
    except ConnectorError:
        return {"error": UNAVAILABLE_ERROR}
    return {
        "account_number": args.account_number,
        "fee_waiver_lines": result["fee_waiver_lines"],
        "remarks": result["remarks"],
    }
