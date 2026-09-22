"""``check_recontract_eligibility`` — re-contract eligibility inquiry.

Checks whether an account is eligible to re-contract against the same
custom cxchat API ``check_fee_waiver_status`` uses (this service owns its
own stored credentials for it — see ``store/custom_api_credentials.py``).
Like fee waiver, this is a direct lookup keyed on account_number — the
custom API has no documented shape for a mobile-number cross-check, so
there's no re-verify step first.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from geneco_mcp.clients.custom_api_client import ConnectorError
from geneco_mcp.store.custom_api_credentials import CustomApiCredentialStore
from geneco_mcp.store.ssp_resolver import SspCredentialResolver
from geneco_mcp.tools._common import RECONTRACT_ELIGIBILITY_NOT_CONFIGURED_ERROR, UNAVAILABLE_ERROR
from geneco_mcp.tools.registry import ToolContext, register


class CheckRecontractEligibilityInput(BaseModel):
    account_number: str = Field(description="The customer's account number.")


@register(
    "check_recontract_eligibility",
    kind="ssp",
    description="Check whether a customer's account is eligible to re-contract, by account number.",
    input_model=CheckRecontractEligibilityInput,
)
async def handle(
    ctx: ToolContext, tenant_slug: str, args: CheckRecontractEligibilityInput
) -> dict[str, Any]:
    store = CustomApiCredentialStore(ctx.session, encryption_key=ctx.encryption_key)
    resolver = SspCredentialResolver(store, ctx.ssp_cache)
    try:
        client = await resolver.get_client(tenant_slug)
    except ConnectorError:
        # The tenant is configured but its vault couldn't be read.
        return {"error": UNAVAILABLE_ERROR}
    if client is None:
        return {"error": RECONTRACT_ELIGIBILITY_NOT_CONFIGURED_ERROR}
    try:
        result = await client.check_recontract_eligibility(args.account_number)
    except ConnectorError:
        return {"error": UNAVAILABLE_ERROR}
    return {
        "account_number": args.account_number,
        "result": result["result"],
        "remarks": result["remarks"],
    }
