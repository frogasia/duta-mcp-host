"""Tool handlers public surface.

Importing this module registers every tool via its ``@register`` decorator
(see ``registry.py``). Downstream callers just do
``from geneco_mcp.tools import all_tools, get_tool``.
"""

from __future__ import annotations

from geneco_mcp.tools import api_check_fee_waiver_status as _api_check_fee_waiver_status  # noqa: F401
from geneco_mcp.tools import (  # noqa: F401
    api_check_recontract_eligibility as _api_check_recontract_eligibility,
)
from geneco_mcp.tools import (  # noqa: F401
    api_request_password_reset as _api_request_password_reset,
)
from geneco_mcp.tools import api_reverse_fee as _api_reverse_fee  # noqa: F401
from geneco_mcp.tools import crm_create_support_case as _crm_create_support_case  # noqa: F401
from geneco_mcp.tools import crm_get_support_cases as _crm_get_support_cases  # noqa: F401
from geneco_mcp.tools import crm_verify_account as _crm_verify_account  # noqa: F401
from geneco_mcp.tools.registry import ToolSpec, all_tools, get_tool, tools_for_kind

__all__ = ["ToolSpec", "all_tools", "get_tool", "tools_for_kind"]
