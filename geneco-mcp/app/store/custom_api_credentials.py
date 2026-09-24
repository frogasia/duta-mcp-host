"""Tenant custom-API (SSP) credential store: the one place that resolves a
``tenant_slug`` into the credentials the cxchat API client needs — and the
one place ``password``/``kv_client_secret`` ever gets decrypted.

Mirrors ``store/credentials.py``'s ``CredentialStore`` for the D365
connector, including its two ``auth_mode`` shapes. ``direct`` holds the
Basic Auth pair here (encrypted); ``key_vault`` holds only a vault-reader
app, and the SSP pair is fetched at call time by ``store/ssp_resolver.py``.

As there, the two modes resolve to two *types* rather than one class with
nullable halves, so a caller cannot read a field that doesn't apply to the
mode it is in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from geneco_mcp.security.crypto import decrypt_secret, encrypt_secret
from geneco_mcp.store.tables import DIRECT_AUTH_MODE, CustomApiCredential

#: Masked placeholder shown in place of the real secret on every read path.
MASK = "•••"

#: Spelled the same as ``store/credentials.py``'s ``AuthMode`` so both
#: credential kinds say the same thing the same way.
AuthMode = Literal["direct", "key_vault"]


@dataclass(slots=True, kw_only=True)
class CustomApiCredentialsInput:
    """Plain data holder for the fields a stage-1 intake call provides —
    kept separate from the DB row so callers never need to import the ORM
    model just to construct one.

    Both credential shapes are optional *here* because only one applies per
    ``auth_mode``; which one is required is enforced at the intake boundary
    (``protocol/admin_router.py``), where a missing field can still become
    a 422 a portal can render.
    """

    base_url: str
    auth_mode: AuthMode = DIRECT_AUTH_MODE
    #: ``direct`` mode — the SSP Basic Auth pair itself.
    username: str | None = None
    password: str | None = None
    #: ``key_vault`` mode — the vault, and the app that may read it.
    tenant_id: str | None = None
    kv_vault_url: str | None = None
    kv_client_id: str | None = None
    kv_client_secret: str | None = None
    #: What this tenant's vault calls the two secrets holding the SSP
    #: Basic Auth pair. Required in ``key_vault`` mode (enforced at the
    #: intake boundary); optional here only for ``direct`` rows.
    kv_secret_name_username: str | None = None
    kv_secret_name_password: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedDirectCustomApiCredentials:
    """A decrypted SSP Basic Auth pair, ready to build a client from with
    no vault hop in between."""

    base_url: str
    username: str
    password: str


@dataclass(frozen=True, slots=True)
class ResolvedKvCustomApiCredentials:
    """Decrypted vault-reader credentials plus the SSP base URL they
    unlock. ``client_id``/``client_secret`` here are the *vault reader's*
    — the SSP pair is what reading the vault produces.

    The two ``*_secret_name`` fields are never ``None`` — a row that does
    not carry them is not usable in this mode at all."""

    base_url: str
    tenant_id: str
    vault_url: str
    client_id: str
    client_secret: str
    username_secret_name: str
    password_secret_name: str


def _has_usable_pair(row: CustomApiCredential) -> bool:
    """Whether the row actually carries the credentials its ``auth_mode``
    calls for. Both ``get_credentials`` and ``is_registered`` answer
    through this, so "registered" can never mean something different to
    the tool list than it does to the tool call."""
    if row.auth_mode == DIRECT_AUTH_MODE:
        return row.username is not None and row.password_encrypted is not None
    return (
        row.tenant_id is not None
        and row.kv_vault_url is not None
        and row.kv_client_id is not None
        and row.kv_client_secret_encrypted is not None
        # Without the names there is nothing to ask the vault for — see
        # the same guard in ``store/credentials.py``.
        and row.kv_secret_name_username is not None
        and row.kv_secret_name_password is not None
    )


class CustomApiCredentialStore:
    def __init__(self, session: AsyncSession, *, encryption_key: str) -> None:
        self._session = session
        self._encryption_key = encryption_key

    async def upsert(
        self, tenant_slug: str, config: CustomApiCredentialsInput
    ) -> CustomApiCredential:
        row = await self._session.get(CustomApiCredential, tenant_slug)
        if row is None:
            row = CustomApiCredential(tenant_slug=tenant_slug)
            self._session.add(row)
        row.auth_mode = config.auth_mode
        row.base_url = config.base_url
        # A full replace, so the other mode's columns are cleared rather
        # than left behind — a stale pair would otherwise outlive the
        # switch and resolve after a later edit flipped the mode back.
        if config.auth_mode == DIRECT_AUTH_MODE:
            assert config.password is not None
            row.username = config.username
            row.password_encrypted = encrypt_secret(config.password, key=self._encryption_key)
            row.tenant_id = None
            row.kv_vault_url = None
            row.kv_client_id = None
            row.kv_client_secret_encrypted = None
            row.kv_secret_name_username = None
            row.kv_secret_name_password = None
        else:
            assert config.kv_client_secret is not None
            row.tenant_id = config.tenant_id
            row.kv_vault_url = config.kv_vault_url
            row.kv_client_id = config.kv_client_id
            row.kv_client_secret_encrypted = encrypt_secret(
                config.kv_client_secret, key=self._encryption_key
            )
            row.kv_secret_name_username = config.kv_secret_name_username
            row.kv_secret_name_password = config.kv_secret_name_password
            row.username = None
            row.password_encrypted = None
        row.enabled = True
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def delete(self, tenant_slug: str) -> bool:
        result = await self._session.execute(
            delete(CustomApiCredential).where(CustomApiCredential.tenant_slug == tenant_slug)
        )
        await self._session.commit()
        return result.rowcount > 0

    async def get_redacted(self, tenant_slug: str) -> dict[str, Any] | None:
        """The stored record with its secret masked, carrying only the
        fields that apply to its ``auth_mode`` — the other mode's columns
        are NULL by construction, so echoing them would only suggest there
        is something there."""
        row = await self._session.get(CustomApiCredential, tenant_slug)
        if row is None:
            return None
        redacted: dict[str, Any] = {
            "tenant_slug": row.tenant_slug,
            "auth_mode": row.auth_mode,
            "base_url": row.base_url,
        }
        if row.auth_mode == DIRECT_AUTH_MODE:
            redacted["username"] = row.username
            redacted["password"] = MASK
        else:
            redacted["tenant_id"] = row.tenant_id
            redacted["kv_vault_url"] = row.kv_vault_url
            redacted["kv_client_id"] = row.kv_client_id
            redacted["kv_client_secret"] = MASK
            redacted["kv_secret_name_username"] = row.kv_secret_name_username
            redacted["kv_secret_name_password"] = row.kv_secret_name_password
        redacted["enabled"] = row.enabled
        return redacted

    async def is_registered(self, tenant_slug: str) -> bool:
        """Whether a tool call for this tenant would find credentials —
        without decrypting anything.

        Mirrors ``CredentialStore.is_registered``: ``tools/list`` asks
        whether the SSP tools should be advertised at all, which is a
        question about the row's presence, not about whether the SSP API
        would accept them.
        """
        row = await self._session.scalar(
            select(CustomApiCredential).where(
                CustomApiCredential.tenant_slug == tenant_slug,
                CustomApiCredential.enabled.is_(True),
            )
        )
        return row is not None and _has_usable_pair(row)

    async def get_credentials(
        self, tenant_slug: str
    ) -> ResolvedDirectCustomApiCredentials | ResolvedKvCustomApiCredentials | None:
        """``None`` when the tenant has nothing registered (or is disabled).

        The returned type says which mode the row is in — the resolver
        branches on it rather than re-reading ``auth_mode``.
        """
        row = await self._session.scalar(
            select(CustomApiCredential).where(
                CustomApiCredential.tenant_slug == tenant_slug,
                CustomApiCredential.enabled.is_(True),
            )
        )
        if row is None or not _has_usable_pair(row):
            # Treat a row missing its credentials as unregistered rather
            # than building a client that would fail confusingly on the
            # first outbound call.
            return None
        if row.auth_mode == DIRECT_AUTH_MODE:
            assert row.username is not None and row.password_encrypted is not None
            return ResolvedDirectCustomApiCredentials(
                base_url=row.base_url,
                username=row.username,
                password=decrypt_secret(row.password_encrypted, key=self._encryption_key),
            )
        assert row.tenant_id is not None and row.kv_vault_url is not None
        assert row.kv_client_id is not None and row.kv_client_secret_encrypted is not None
        assert row.kv_secret_name_username is not None
        assert row.kv_secret_name_password is not None
        return ResolvedKvCustomApiCredentials(
            base_url=row.base_url,
            tenant_id=row.tenant_id,
            vault_url=row.kv_vault_url,
            client_id=row.kv_client_id,
            client_secret=decrypt_secret(
                row.kv_client_secret_encrypted, key=self._encryption_key
            ),
            username_secret_name=row.kv_secret_name_username,
            password_secret_name=row.kv_secret_name_password,
        )
