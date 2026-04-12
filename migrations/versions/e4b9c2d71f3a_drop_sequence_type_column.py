"""drop_sequence_type_column

Sequence type (RE_DEAL, RE_FUND, PE_DEAL, PE_FUND) is no longer used.
Campaigns are now the primary way to categorise enrollments.

Revision ID: e4b9c2d71f3a
Revises: c7a3f1d82e9b
Create Date: 2026-04-11

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'e4b9c2d71f3a'
down_revision: Union[str, None] = 'c7a3f1d82e9b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column('sequence_enrollments', 'sequence_type')


def downgrade() -> None:
    op.add_column(
        'sequence_enrollments',
        sa.Column('sequence_type', sa.String(length=20), nullable=True),
    )
