"""add campaign_name to sequence_enrollments

Revision ID: a1c4e9f82b3d
Revises: d3811ea7b37c
Create Date: 2026-04-09 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1c4e9f82b3d'
down_revision: Union[str, None] = 'd3811ea7b37c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('sequence_enrollments', sa.Column('campaign_name', sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column('sequence_enrollments', 'campaign_name')
