"""add inspector_edits (S5: versioned inspector evidence edits and revision choices)

Revision ID: 5a1c0e7d9b21
Revises: 8ec8d78d4240
Create Date: 2026-09-25 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5a1c0e7d9b21'
down_revision: Union[str, None] = '8ec8d78d4240'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('inspector_edits',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('process_id', sa.String(length=64), nullable=False),
    sa.Column('object_id', sa.String(length=64), nullable=True),
    sa.Column('evidence_group_id', sa.Integer(), nullable=True),
    sa.Column('entity_type', sa.String(length=32), nullable=False),
    sa.Column('entity_key', sa.String(length=160), nullable=False),
    sa.Column('action', sa.String(length=32), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('source_fragment_id', sa.Integer(), nullable=True),
    sa.Column('source_document_version_id', sa.Integer(), nullable=True),
    sa.Column('previous_edit_id', sa.Integer(), nullable=True),
    sa.Column('previous_value', sa.JSON(), nullable=True),
    sa.Column('new_value', sa.JSON(), nullable=True),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['process_id'], ['inspection_processes.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['evidence_group_id'], ['evidence_groups.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['source_fragment_id'], ['evidence_fragments.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['source_document_version_id'], ['document_versions.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['previous_edit_id'], ['inspector_edits.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('process_id', 'entity_key', 'version', name='uq_inspector_edit_entity_version')
    )
    for column in ('id', 'organization_id', 'project_id', 'process_id', 'object_id', 'evidence_group_id', 'entity_type',
                   'entity_key', 'action', 'source_fragment_id', 'source_document_version_id', 'previous_edit_id', 'user_id'):
        op.create_index(op.f(f'ix_inspector_edits_{column}'), 'inspector_edits', [column], unique=False)


def downgrade() -> None:
    for column in ('user_id', 'previous_edit_id', 'source_document_version_id', 'source_fragment_id', 'action',
                   'entity_key', 'entity_type', 'evidence_group_id', 'object_id', 'process_id', 'project_id',
                   'organization_id', 'id'):
        op.drop_index(op.f(f'ix_inspector_edits_{column}'), table_name='inspector_edits')
    op.drop_table('inspector_edits')
