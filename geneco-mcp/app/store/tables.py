"""SQLAlchemy tables: one row per tenant's credentials, per connector kind.

Unlike integration-hub's ``tenant_connectors`` (which stores several
connector *kinds* per tenant in one table), this service gives each
connector kind its own table with ``tenant_slug`` as the primary key
directly — ``D365Credential`` for the D365 connector, ``CustomApiCredential``
for the custom fee-waiver/re-contract API. Every secret column
(``kv_client_secret_encrypted``, ``client_secret_encrypted``,
``password_encrypted``) is a Fernet ciphertext (see ``security/crypto.py``)
— never the plaintext value.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from sqlalchemy import JSON, Boolean, DateTime, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


#: Default ``D365Credential.auth_mode``: every caller registering before
#: direct mode existed meant this one, so it must stay the default — the
#: other way round would silently reinterpret a vault-reader app as a D365
#: app registration. The ``Literal`` that constrains it lives in
#: ``store/credentials.py``; this constant is here because the column
#: default and the migration's server default both need it.
KEY_VAULT_AUTH_MODE: Final = "key_vault"
DIRECT_AUTH_MODE: Final = "direct"


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


class D365Credential(Base):
    """A tenant's D365 setup, stored one of two ways — ``auth_mode`` says
    which, and exactly one mode's columns are populated on any given row.

    ``key_vault`` (the default, and the one to prefer): the stored
    credential is the tenant's *Key Vault* app registration. The D365 app
    registration (``Crm-Client-Id``/``Crm-Client-Secret``) lives in that
    vault and is fetched at call time — see ``clients/keyvault_client.py``
    — so it is never at rest here.

    ``direct``: the tenant has no vault, so ``client_id``/
    ``client_secret_encrypted`` *are* the D365 app registration. This does
    put a D365 secret at rest here (encrypted), which the vault indirection
    exists to avoid — it is the price of onboarding a tenant that cannot
    provide a vault.

    Per-mode presence is enforced in the application layer
    (``protocol/admin_router.py``), not by a DB constraint, so a portal
    gets a readable message rather than an IntegrityError.
    """

    __tablename__ = "d365_credentials"

    tenant_slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: ``"key_vault"`` or ``"direct"`` — see the class docstring. A plain
    #: string rather than a DB enum: the two services that speak this
    #: vocabulary (this one and duta-ilmu's portal) agree on the values,
    #: and a new mode shouldn't need a migration on a type.
    auth_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default=KEY_VAULT_AUTH_MODE, server_default=KEY_VAULT_AUTH_MODE
    )
    org_url: Mapped[str] = mapped_column(String(512), nullable=False)
    #: Entra tenant GUID — used as the authority for the Dataverse token in
    #: both modes, and for the Key Vault token in ``key_vault`` mode, on the
    #: assumption (inherited from geneco-poc's ``crm_client.py``) that a
    #: tenant's vault and CRM app registrations live in the same Entra
    #: tenant.
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: ``key_vault`` mode only — NULL on a ``direct`` row.
    kv_vault_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    kv_client_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Fernet ciphertext — see ``security/crypto.py``. Never stored plaintext.
    kv_client_secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: ``direct`` mode only — NULL on a ``key_vault`` row. This is the D365
    #: app registration itself, not a vault reader.
    client_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Fernet ciphertext — see ``security/crypto.py``. Never stored plaintext.
    client_secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    field_map: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class CustomApiCredential(Base):
    __tablename__ = "custom_api_credentials"

    tenant_slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    username: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Fernet ciphertext — see ``security/crypto.py``. Never stored plaintext.
    password_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
