"""The LEAD API call. Every failure raises ``LeadApiError`` so the tool can
answer with a relayable error, never a 500. Bodies are PII: never logged."""

from __future__ import annotations

from typing import Any

import httpx

USER_INFO_URL = "https://www.ytl-lead.com/api/v1/app/users/info"


class LeadApiError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


async def fetch_user_info(token: str, user_id: int) -> dict[str, Any] | None:
    """LEAD's ``data`` object, or ``None`` if there's no such user.

    LEAD answers an unknown id with 404 {"success": false, "message": "User not found."};
    ``success: false`` and ``data: null`` also count as "not found"."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                USER_INFO_URL,
                params={"id": user_id},
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise LeadApiError(type(exc).__name__) from exc
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise LeadApiError("HTTP error", status_code=resp.status_code)
    try:
        body = resp.json()
    except ValueError as exc:
        raise LeadApiError("non-JSON body", status_code=resp.status_code) from exc
    if not isinstance(body, dict):
        raise LeadApiError("unexpected body", status_code=resp.status_code)
    data = body.get("data")
    if data is None or body.get("success") is False:
        return None
    if not isinstance(data, dict):
        raise LeadApiError("unexpected body", status_code=resp.status_code)
    return data
