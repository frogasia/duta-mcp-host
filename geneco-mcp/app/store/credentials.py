"""Tenant D365 credential store: the one place that resolves a
``tenant_slug`` into the decrypted credentials a ``D365Connector`` needs,
and the one place either stored secret ever gets decrypted.

A tenant's D365 setup is stored one of two ways, discriminated by
``auth_mode`` (see ``store/tables.py``):

``key_vault``
    The stored credential is the tenant's *vault reader* app registration;
    the real D365 app registration is fetched from that vault at call time.
``direct``
    The stored credential *is* the D365 app registration, for a tenant with
    no vault to read from.

Pure persistence either way: it does not call Key Vault or build a
connector. The vault hop and its cache live in ``store/crm_resolver.py``,
which is what tool handlers actually use.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from geneco_mcp.security.crypto import decrypt_secret, encrypt_secret
from geneco_mcp.store.tables import DIRECT_AUTH_MODE, KEY_VAULT_AUTH_MODE, D365Credential

#: Masked placeholder shown in place of the real secret on every read path.
MASK = "•••"

#: The two ways a tenant's D365 credentials can be stored. Mirrors the
#: toggle duta-ilmu's tenant portal offers, so both services describe the
#: same thing the same way.
AuthMode = Literal["direct", "key_vault"]


@dataclass(slots=True, kw_only=True)
class D365CredentialsInput:
    """Plain data holder for the fields a stage-1 intake call provides —
    kept separate from the DB row so callers never need to import the ORM
    model just to construct one.

    Both credential pairs are optional *here* because only one applies per
    ``auth_mode``; which one is required is enforced at the intake boundary
    (``protocol/admin_router.py``), where a missing field can still become a
    422 a portal can render.
    """

    org_url: str
    tenant_id: str
    auth_mode: AuthMode = KEY_VAULT_AUTH_MODE
    #: ``key_vault`` mode.
    kv_vault_url: str | None = None
    kv_client_id: str | None = None
    kv_client_secret: str | None = None
    #: ``direct`` mode — the D365 app registration itself.
    client_id: str | None = None
    client_secret: str | None = None
    field_map: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ResolvedKvCredentials:
    """Decrypted vault credentials plus the D365 config they unlock.

    ``client_id``/``client_secret`` here are the *vault reader's* — the
    D365 pair is what reading the vault produces.
    """

    org_url: str
    tenant_id: str
    vault_url: str
    client_id: str
    client_secret: str
    field_map: dict[str, str]


@dataclass(frozen=True, slots=True)
class ResolvedDirectCredentials:
    """A decrypted D365 app registration, ready to build a connector from
    with no vault hop in between.

    A separate type from ``ResolvedKvCredentials`` rather than one class
    with nullable halves, so the resolver cannot read a field that doesn't
    apply to the mode it's in — the two ``client_id``s mean different
    things and must not be confusable.
    """

    org_url: str
    tenant_id: str
    client_id: str
    client_secret: str
    field_map: dict[str, str]


class CredentialStore:
    def __init__(self, session: AsyncSession, *, encryption_key: str) -> None:
        self._session = session
        self._encryption_key = encryption_key

    async def upsert(self, tenant_slug: str, config: D365CredentialsInput) -> D365Credential:
        """Full replace, never a merge — and writing one mode CLEARS the
        other's columns, so a tenant switching from vault to direct (or
        back) leaves no stale credentials behind for a later code path to
        pick up."""
        row = await self._session.get(D365Credential, tenant_slug)
        if row is None:
            row = D365Credential(tenant_slug=tenant_slug)
            self._session.add(row)
        row.auth_mode = config.auth_mode
        row.org_url = str(config.org_url)
        row.tenant_id = config.tenant_id
        if config.auth_mode == DIRECT_AUTH_MODE:
            assert config.client_id is not None and config.client_secret is not None
            row.client_id = config.client_id
            row.client_secret_encrypted = encrypt_secret(
                config.client_secret, key=self._encryption_key
            )
            row.kv_vault_url = None
            row.kv_client_id = None
            row.kv_client_secret_encrypted = None
        else:
            assert (
                config.kv_vault_url is not None
                and config.kv_client_id is not None
                and config.kv_client_secret is not None
            )
            row.kv_vault_url = str(config.kv_vault_url)
            row.kv_client_id = config.kv_client_id
            row.kv_client_secret_encrypted = encrypt_secret(
                config.kv_client_secret, key=self._encryption_key
            )
            row.client_id = None
            row.client_secret_encrypted = None
        row.field_map = config.field_map
        row.enabled = True
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def delete(self, tenant_slug: str) -> bool:
        result = await self._session.execute(
            delete(D365Credential).where(D365Credential.tenant_slug == tenant_slug)
        )
        await self._session.commit()
        return result.rowcount > 0

    async def get_redacted(self, tenant_slug: str) -> dict[str, Any] | None:
        """The stored record with its secret masked, carrying only the
        fields that apply to its ``auth_mode`` — the other mode's columns
        are NULL by construction, so echoing them would only suggest there
        is something there."""
        row = await self._session.get(D365Credential, tenant_slug)
        if row is None:
            return None
        redacted: dict[str, Any] = {
            "tenant_slug": row.tenant_slug,
            "auth_mode": row.auth_mode,
            "org_url": row.org_url,
            "tenant_id": row.tenant_id,
        }
        if row.auth_mode == DIRECT_AUTH_MODE:
            redacted["client_id"] = row.client_id
            redacted["client_secret"] = MASK
        else:
            redacted["kv_vault_url"] = row.kv_vault_url
            redacted["kv_client_id"] = row.kv_client_id
            redacted["kv_client_secret"] = MASK
        redacted["field_map"] = row.field_map
        redacted["enabled"] = row.enabled
        return redacted

    async def get_credentials(
        self, tenant_slug: str
    ) -> ResolvedKvCredentials | ResolvedDirectCredentials | None:
        """``None`` when the tenant has nothing registered (or is disabled).

        The returned type says which mode the row is in — the resolver
        branches on it rather than re-reading ``auth_mode``.
        """
        row = await self._session.scalar(
            select(D365Credential).where(
                D365Credential.tenant_slug == tenant_slug, D365Credential.enabled.is_(True)
            )
        )
        if row is None:
            return None
        if row.auth_mode == DIRECT_AUTH_MODE:
            # A direct row without its pair can only come from a hand-edited
            # DB — treat it as unregistered rather than building a connector
            # that would fail confusingly at token acquisition.
            if row.client_id is None or row.client_secret_encrypted is None:
                return None
            return ResolvedDirectCredentials(
                org_url=row.org_url,
                tenant_id=row.tenant_id,
                client_id=row.client_id,
                client_secret=decrypt_secret(
                    row.client_secret_encrypted, key=self._encryption_key
                ),
                field_map=row.field_map,
            )
        if (
            row.kv_vault_url is None
            or row.kv_client_id is None
            or row.kv_client_secret_encrypted is None
        ):
            return None
        return ResolvedKvCredentials(
            org_url=row.org_url,
            tenant_id=row.tenant_id,
            vault_url=row.kv_vault_url,
            client_id=row.kv_client_id,
            client_secret=decrypt_secret(row.kv_client_secret_encrypted, key=self._encryption_key),
            field_map=row.field_map,
        )
