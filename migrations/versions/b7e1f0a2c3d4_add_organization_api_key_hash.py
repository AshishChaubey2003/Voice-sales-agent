"""add organization api_key_hash

Revision ID: b7e1f0a2c3d4
Revises: 25ae4fc65be4
Create Date: 2026-10-26 00:00:00.000000

Adds a single nullable column holding the SHA-256 hex digest of each
organization's API key, plus a unique constraint so a digest maps to at most
one organization. Nullable so existing rows upgrade cleanly; an organization
with no hash simply cannot authenticate until a key is minted for it. The raw
key is never stored.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7e1f0a2c3d4'
down_revision: Union[str, Sequence[str], None] = '25ae4fc65be4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'organizations',
        sa.Column('api_key_hash', sa.String(length=64), nullable=True),
    )
    op.create_unique_constraint(
        op.f('uq_organizations_api_key_hash'),
        'organizations',
        ['api_key_hash'],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        op.f('uq_organizations_api_key_hash'),
        'organizations',
        type_='unique',
    )
    op.drop_column('organizations', 'api_key_hash')
