"""ssp credentials can come from a key vault, not only a stored pair

The mirror of 0004, for the other backend: a tenant that keeps its SSP
Basic Auth pair in a Key Vault had nowhere to put it — this table could
only hold the pair itself. ``auth_mode`` splits the row into ``direct``
(the existing shape) and ``key_vault``, where the stored credential is a
vault-reader app and the SSP user id/password are read at call time.

Non-destructive: every existing row is a stored pair, and the server
default says ``direct``, so they keep working untouched.

``tenant_id`` is nullable and SSP-specific rather than borrowed from
``d365_credentials`` — a tenant can register SSP without D365 at all,
which is the reason the two MCP servers were split.

Per-mode presence is enforced in the application layer, not as a CHECK
constraint, for the same reason as 0004: a portal gets a readable 422.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-22
"""

import sqlalchemy as sa

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "custom_api_credentials",
        sa.Column("auth_mode", sa.String(length=16), nullable=False, server_default="direct"),
    )
    op.add_column(
        "custom_api_credentials", sa.Column("tenant_id", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "custom_api_credentials", sa.Column("kv_vault_url", sa.String(length=512), nullable=True)
    )
    op.add_column(
        "custom_api_credentials", sa.Column("kv_client_id", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "custom_api_credentials", sa.Column("kv_client_secret_encrypted", sa.Text(), nullable=True)
    )
    # A key-vault row has no stored pair to fill.
    op.alter_column(
        "custom_api_credentials", "username", existing_type=sa.String(length=128), nullable=True
    )
    op.alter_column(
        "custom_api_credentials", "password_encrypted", existing_type=sa.Text(), nullable=True
    )


def downgrade() -> None:
    # Key-vault rows have nowhere to live in the old schema — the SSP pair
    # is in the vault, not derivable here, so they must be re-registered
    # rather than back-filled with placeholders that fail at call time.
    op.execute("DELETE FROM custom_api_credentials WHERE auth_mode = 'key_vault'")
    op.alter_column(
        "custom_api_credentials", "password_encrypted", existing_type=sa.Text(), nullable=False
    )
    op.alter_column(
        "custom_api_credentials", "username", existing_type=sa.String(length=128), nullable=False
    )
    op.drop_column("custom_api_credentials", "kv_client_secret_encrypted")
    op.drop_column("custom_api_credentials", "kv_client_id")
    op.drop_column("custom_api_credentials", "kv_vault_url")
    op.drop_column("custom_api_credentials", "tenant_id")
    op.drop_column("custom_api_credentials", "auth_mode")
