"""vault secret names become per-tenant, and mandatory

Until now ``clients/keyvault_client.py`` held four hardcoded names —
``Crm-Client-Id``/``Crm-Client-Secret`` and ``Ssp-User-Id``/
``Ssp-Password`` — so a tenant whose vault spelled them differently could
not be onboarded at all. These columns move that fact onto the credential
row, where it belongs: it is a property of the tenant's vault, not of this
service.

The names are REQUIRED in ``key_vault`` mode rather than defaulted. A
default would be indistinguishable from a deliberate choice at the moment
it failed, and it would fail on a live customer-facing tool call rather
than at registration — so intake rejects a vault-mode body without them,
and ``_has_usable_pair`` treats a row lacking them as unregistered.

That makes the back-fill below load-bearing rather than cosmetic: existing
``key_vault`` rows carry NULL, which after this migration means "not
usable". They are set to the names their vaults are known to use — the
values those rows were resolving with all along, just implicitly. Only
``key_vault`` rows are touched; a ``direct`` row has no vault to name and
keeps NULL.

The columns are 127 characters because that is Azure's own limit on a
secret name, and they are stored plain rather than encrypted: a name is
not a secret, and the read paths echo it so an admin can confirm what is
being looked up.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-24
"""

import sqlalchemy as sa

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "d365_credentials",
        sa.Column("kv_secret_name_client_id", sa.String(length=127), nullable=True),
    )
    op.add_column(
        "d365_credentials",
        sa.Column("kv_secret_name_client_secret", sa.String(length=127), nullable=True),
    )
    op.add_column(
        "custom_api_credentials",
        sa.Column("kv_secret_name_username", sa.String(length=127), nullable=True),
    )
    op.add_column(
        "custom_api_credentials",
        sa.Column("kv_secret_name_password", sa.String(length=127), nullable=True),
    )

    # Back-fill what the hardcoded constants used to supply. Without this
    # every existing key_vault tenant would stop resolving the moment the
    # fallback was removed.
    op.execute(
        """
        UPDATE d365_credentials
           SET kv_secret_name_client_id = 'Crm-Client-Id',
               kv_secret_name_client_secret = 'Crm-Client-Secret'
         WHERE auth_mode = 'key_vault'
        """
    )
    op.execute(
        """
        UPDATE custom_api_credentials
           SET kv_secret_name_username = 'Ssp-User-Id',
               kv_secret_name_password = 'Ssp-Password'
         WHERE auth_mode = 'key_vault'
        """
    )


def downgrade() -> None:
    # Safe to drop outright, unlike 0005's: the old code supplied these
    # four names itself, so a row loses nothing it cannot re-derive. A
    # tenant that had registered NON-default names silently reverts to the
    # defaults on downgrade — which is the old behaviour, and the reason
    # this migration exists.
    op.drop_column("custom_api_credentials", "kv_secret_name_password")
    op.drop_column("custom_api_credentials", "kv_secret_name_username")
    op.drop_column("d365_credentials", "kv_secret_name_client_secret")
    op.drop_column("d365_credentials", "kv_secret_name_client_id")
