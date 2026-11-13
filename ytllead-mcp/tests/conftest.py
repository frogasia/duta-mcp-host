from __future__ import annotations

import time
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient

from ytllead_mcp.config import Settings
from ytllead_mcp.main import create_app

PATH = "/mcp"
SECRET = "test-identity-signing-secret-0123456789abcdef"


def fake_user(user_id: int) -> dict[str, Any]:
    """Invented, trimmed LEAD user in the real response's shape."""
    return {
        "id": user_id,
        "email": f"user{user_id}@example.test",
        "user_profile": {"name": "Test Person", "table_no": "12", "company": {"name": "Ex Co"}},
        "events": [{"id": 1, "title": "Example Conference", "start_at": "2026-11-23T16:00:00Z"}],
        "workshop_teams": [],
    }


def make_client(**overrides: Any) -> TestClient:
    values = {
        "lead_api_token": "test-token",
        "identity_signing_secret": SECRET,
    }
    values.update(overrides)
    return TestClient(create_app(Settings(_env_file=None, **values)))  # ignore any local .env


@pytest.fixture
def client() -> TestClient:
    return make_client()


def token(
    sub: Any, *, secret: str = SECRET, iat: float | None = None, ttl: int = 60, **extra
) -> str:
    now = time.time() if iat is None else iat
    claims = {"sub": sub, "iat": int(now), "exp": int(now) + ttl, **extra}
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, secret, algorithm="HS256")


def identity(user_id: Any, **token_kwargs) -> dict[str, str]:
    """Headers the platform sends for a signed-in user: the signed token."""
    return {"X-Verified-User-Token": token(str(user_id), **token_kwargs)}


def rpc(client: TestClient, method: str, params: Any = None, *, path=PATH, headers=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    resp = client.post(path, json=body, headers=headers or {})
    assert resp.status_code == 200
    return resp.json()


def call(client: TestClient, headers=None, *, arguments=None) -> dict[str, Any]:
    """tools/call → {"isError", "text"} (Markdown on success, the message on error)."""
    result = rpc(
        client,
        "tools/call",
        {"name": "get_my_lead_profile", "arguments": arguments or {}},
        headers=headers,
    )["result"]
    return {"isError": result["isError"], "text": result["content"][0]["text"]}
