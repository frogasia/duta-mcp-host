from __future__ import annotations

import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from geneco_mcp.config import Settings
from geneco_mcp.main import create_app
from geneco_mcp.store.tables import Base

TEST_ADMIN_API_KEY = "test-admin-key"
TEST_ENCRYPTION_KEY = Fernet.generate_key().decode()


@pytest.fixture
def settings(tmp_path) -> Settings:
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    # DDL setup uses a plain sync engine (no event loop involved) so the
    # async engine the app actually uses never has a connection reused
    # across a different event loop than TestClient's own.
    sync_engine = sa.create_engine(db_url.replace("+aiosqlite", ""))
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()
    return Settings(
        database_url=db_url,
        credential_encryption_key=TEST_ENCRYPTION_KEY,
        admin_api_key=TEST_ADMIN_API_KEY,
    )


# The two MCP servers, over one database. Built from the same ``settings``
# fixture on purpose: a test can seed through one client and read through
# the other, which is what registering each backend's credentials
# separately actually looks like.
@pytest.fixture
def crm_app(settings: Settings) -> FastAPI:
    return create_app(settings, kind="crm")


@pytest.fixture
def ssp_app(settings: Settings) -> FastAPI:
    return create_app(settings, kind="ssp")


@pytest.fixture
def crm_client(crm_app: FastAPI) -> TestClient:
    return TestClient(crm_app)


@pytest.fixture
def ssp_client(ssp_app: FastAPI) -> TestClient:
    return TestClient(ssp_app)
