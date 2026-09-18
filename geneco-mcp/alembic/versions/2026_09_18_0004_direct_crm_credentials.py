"""d365 credentials can be stored directly, not only via key vault

A tenant with no Key Vault could not be onboarded at all: the only shape
this table could hold was a vault-reader app registration. ``auth_mode``
splits the row into two shapes — ``key_vault`` (the existing one) and
``direct``, where ``client_id``/``client_secret_encrypted`` are the D365
app registration itself.

Non-destructive, unlike 0003: existing rows are all ``key_vault`` and the
server default says so, so they keep working untouched.

Per-mode presence (a ``direct`` row needs ``client_id``, a ``key_vault``
row needs the three ``kv_*`` columns) is deliberately NOT a CHECK
constraint — the application layer enforces it so a portal gets a readable
message instead of an IntegrityError.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-18
"""

import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "d365_credentials",
        sa.Column("auth_mode", sa.String(length=16), nullable=False, server_default="key_vault"),
    )
    op.add_column("d365_credentials", sa.Column("client_id", sa.String(length=64), nullable=True))
    op.add_column(
        "d365_credentials", sa.Column("client_secret_encrypted", sa.Text(), nullable=True)
    )
    # A direct-mode row has no vault columns to fill.
    op.alter_column("d365_credentials", "kv_vault_url", existing_type=sa.String(length=512), nullable=True)
    op.alter_column("d365_credentials", "kv_client_id", existing_type=sa.String(length=64), nullable=True)
    op.alter_column(
        "d365_credentials", "kv_client_secret_encrypted", existing_type=sa.Text(), nullable=True
    )


def downgrade() -> None:
    # Direct-mode rows have nowhere to live in the old schema — their
    # credentials are not derivable as vault ones, so they must be
    # re-registered rather than back-filled with placeholders that would
    # fail at call time. Key-vault rows survive.
    op.execute("DELETE FROM d365_credentials WHERE auth_mode = 'direct'")
    op.alter_column(
        "d365_credentials", "kv_client_secret_encrypted", existing_type=sa.Text(), nullable=False
    )
    op.alter_column("d365_credentials", "kv_client_id", existing_type=sa.String(length=64), nullable=False)
    op.alter_column("d365_credentials", "kv_vault_url", existing_type=sa.String(length=512), nullable=False)
    op.drop_column("d365_credentials", "client_secret_encrypted")
    op.drop_column("d365_credentials", "client_id")
    op.drop_column("d365_credentials", "auth_mode")
