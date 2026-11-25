"""add turn_evaluations

Revision ID: c4d2a91e7f35
Revises: b7e1f0a2c3d4
Create Date: 2026-10-28 00:00:00.000000

Adds one new table; no existing table or row is changed or deleted.

One row per (assistant message, evaluator version): the unique constraint is what makes re-running the
evaluator idempotent. Deleting a message or conversation deletes its evaluations (ON DELETE CASCADE),
matching how leads and messages already behave. A turn with no row is "pending".
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c4d2a91e7f35'
down_revision: Union[str, Sequence[str], None] = 'b7e1f0a2c3d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('turn_evaluations',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.Column('conversation_id', sa.Uuid(), nullable=False),
    sa.Column('message_id', sa.Uuid(), nullable=False),
    sa.Column('user_message_id', sa.Uuid(), nullable=True),
    sa.Column('evaluator_version', sa.String(length=40), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('correctness', sa.String(length=20), nullable=True),
    sa.Column('correctness_provisional', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('reference_quality', sa.String(length=20), server_default='none', nullable=False),
    sa.Column('reference_case_id', sa.String(length=100), nullable=True),
    sa.Column('dataset_sha256', sa.String(length=64), nullable=True),
    sa.Column('ungraded_reason', sa.Text(), nullable=True),
    sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('evaluated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("correctness IS NULL OR correctness IN ('correct', 'partial', 'incorrect')", name=op.f('ck_turn_evaluations_correctness_valid')),
    sa.CheckConstraint("status IN ('passed', 'failed', 'ungraded', 'error', 'skipped')", name=op.f('ck_turn_evaluations_status_valid')),
    sa.ForeignKeyConstraint(['conversation_id', 'organization_id'], ['conversations.id', 'conversations.organization_id'], name=op.f('fk_turn_evaluations_conversation_id_conversations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['message_id'], ['messages.id'], name=op.f('fk_turn_evaluations_message_id_messages'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_turn_evaluations_organization_id_organizations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_message_id'], ['messages.id'], name=op.f('fk_turn_evaluations_user_message_id_messages'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_turn_evaluations')),
    sa.UniqueConstraint('message_id', 'evaluator_version', name='uq_turn_evaluations_message_version')
    )
    op.create_index('ix_turn_evaluations_conversation', 'turn_evaluations', ['conversation_id'], unique=False)
    op.create_index('ix_turn_evaluations_org_created', 'turn_evaluations', ['organization_id', 'created_at'], unique=False)
    op.create_index('ix_turn_evaluations_org_status', 'turn_evaluations', ['organization_id', 'status'], unique=False)


def downgrade() -> None:
    """Downgrade schema. Drops only the evaluations table; conversations and messages are untouched."""
    op.drop_index('ix_turn_evaluations_org_status', table_name='turn_evaluations')
    op.drop_index('ix_turn_evaluations_org_created', table_name='turn_evaluations')
    op.drop_index('ix_turn_evaluations_conversation', table_name='turn_evaluations')
    op.drop_table('turn_evaluations')
