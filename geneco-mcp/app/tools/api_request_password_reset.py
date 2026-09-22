"""``request_password_reset`` — self-service-portal password reset.

Triggers a password-reset email against the same custom cxchat API
``check_fee_waiver_status`` uses (this service owns its own stored
credentials for it — see ``store/custom_api_credentials.py``). Keyed on
the account holder's registered email address, not an account number —
confirmed empirically against SIT that the vendor API rejects an account
number the same way it rejects an unregistered email.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from geneco_mcp.clients.custom_api_client import ConnectorError
from geneco_mcp.store.custom_api_credentials import CustomApiCredentialStore
from geneco_mcp.store.ssp_resolver import SspCredentialResolver
from geneco_mcp.tools._common import PASSWORD_RESET_NOT_CONFIGURED_ERROR, UNAVAILABLE_ERROR
from geneco_mcp.tools.registry import ToolContext, register


class RequestPasswordResetInput(BaseModel):
    email: str = Field(description="The customer's registered account email address.")


@register(
    "request_password_reset",
    kind="ssp",
    description=(
        "Send a self-service-portal password-reset email to a customer's registered email address."
    ),
    input_model=RequestPasswordResetInput,
)
async def handle(
    ctx: ToolContext, tenant_slug: str, args: RequestPasswordResetInput
) -> dict[str, Any]:
    store = CustomApiCredentialStore(ctx.session, encryption_key=ctx.encryption_key)
    resolver = SspCredentialResolver(store, ctx.ssp_cache)
    try:
        client = await resolver.get_client(tenant_slug)
    except ConnectorError:
        # The tenant is configured but its vault couldn't be read.
        return {"error": UNAVAILABLE_ERROR}
    if client is None:
        return {"error": PASSWORD_RESET_NOT_CONFIGURED_ERROR}
    try:
        result = await client.request_password_reset(args.email)
    except ConnectorError:
        return {"error": UNAVAILABLE_ERROR}
    return {
        "email": args.email,
        "success": result["success"],
        "message": result["message"],
    }
