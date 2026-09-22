"""``create_support_case`` — create case.

Re-verifies account_id/mobile_number, then creates a D365 incident for
that account directly (this service owns its own D365 connector + stored
credentials — see ``store/credentials.py``). No mock fallback: an
unconfigured/unreachable D365 org returns an honest error the model can
relay, never a fabricated case id.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from geneco_mcp.clients.d365_connector import ConnectorError
from geneco_mcp.store.credentials import CredentialStore
from geneco_mcp.store.crm_resolver import CrmCredentialResolver
from geneco_mcp.tools._common import UNAVAILABLE_ERROR, verify_and_get_guid
from geneco_mcp.tools.registry import ToolContext, register


class CreateSupportCaseInput(BaseModel):
    account_id: str = Field(description="The customer's account number.")
    mobile_number: str = Field(description="The mobile number on file for the account.")
    title: str = Field(description="Short summary of the issue, used as the case title.")
    description: str = Field(description="Full description of the customer's issue.")


@register(
    "create_support_case",
    kind="crm",
    description=(
        "Create a support case for a customer, after verifying their account "
        "number and mobile number match."
    ),
    input_model=CreateSupportCaseInput,
)
async def handle(
    ctx: ToolContext, tenant_slug: str, args: CreateSupportCaseInput
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
        case_guid = await connector.create_case(
            title=args.title, description=args.description, account_guid=account_guid
        )
    except ConnectorError:
        return {"error": UNAVAILABLE_ERROR}
    return {
        "case_id": case_guid,
        "account_id": args.account_id,
        "title": args.title,
        "description": args.description,
        "status": "Open",
    }
