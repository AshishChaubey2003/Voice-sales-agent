"""add sales_reps and handoff_requests

Revision ID: d5e3b82a4c16
Revises: c4d2a91e7f35
Create Date: 2026-11-25 00:00:00.000000

Adds the human side of the sales workflow: reps with an Available/Busy/Offline status, and handoff
requests that connect a visitor's conversation to a rep. Every row carries organization_id and the
foreign keys are composite, so a rep or conversation of another organization can never be linked.
A partial unique index allows at most one open (pending/assigned) request per conversation.
Purely additive: no existing table or row is changed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd5e3b82a4c16'
down_revision: Union[str, Sequence[str], None] = 'c4d2a91e7f35'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'sales_reps',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('email', sa.String(length=320), nullable=False),
        sa.Column('status', sa.String(length=20), server_default='offline', nullable=False),
        sa.Column('status_changed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("status IN ('available', 'busy', 'offline')", name=op.f('ck_sales_reps_status_valid')),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_sales_reps_organization_id_organizations'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_sales_reps')),
        sa.UniqueConstraint('id', 'organization_id', name=op.f('uq_sales_reps_id')),
        sa.UniqueConstraint('organization_id', 'email', name='uq_sales_reps_org_email'),
    )
    op.create_index('ix_sales_reps_org_status', 'sales_reps', ['organization_id', 'status'], unique=False)

    op.create_table(
        'handoff_requests',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('conversation_id', sa.Uuid(), nullable=False),
        sa.Column('status', sa.String(length=20), server_default='pending', nullable=False),
        sa.Column('reason', sa.String(length=300), nullable=True),
        sa.Column('context_summary', sa.Text(), server_default='', nullable=False),
        sa.Column('assigned_rep_id', sa.Uuid(), nullable=True),
        sa.Column('assigned_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('notification_status', sa.String(length=20), server_default='not_configured', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("status IN ('pending', 'assigned', 'resolved', 'cancelled')", name=op.f('ck_handoff_requests_status_valid')),
        sa.CheckConstraint("notification_status IN ('not_configured', 'sent', 'failed')", name=op.f('ck_handoff_requests_notification_status_valid')),
        sa.ForeignKeyConstraint(['conversation_id', 'organization_id'], ['conversations.id', 'conversations.organization_id'], name=op.f('fk_handoff_requests_conversation_id_conversations'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['assigned_rep_id', 'organization_id'], ['sales_reps.id', 'sales_reps.organization_id'], name=op.f('fk_handoff_requests_assigned_rep_id_sales_reps')),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_handoff_requests_organization_id_organizations'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_handoff_requests')),
    )
    op.create_index(
        'uq_handoff_one_open_per_conversation', 'handoff_requests', ['conversation_id'], unique=True,
        postgresql_where=sa.text("status IN ('pending', 'assigned')"),
    )
    op.create_index('ix_handoff_requests_org_status', 'handoff_requests', ['organization_id', 'status', 'created_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_handoff_requests_org_status', table_name='handoff_requests')
    op.drop_index('uq_handoff_one_open_per_conversation', table_name='handoff_requests', postgresql_where=sa.text("status IN ('pending', 'assigned')"))
    op.drop_table('handoff_requests')
    op.drop_index('ix_sales_reps_org_status', table_name='sales_reps')
    op.drop_table('sales_reps')
