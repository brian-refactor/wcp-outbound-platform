"""add email validation fields to prospects

Revision ID: f3c8b2a91d4e
Revises: a1c4e9f82b3d
Create Date: 2026-04-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f3c8b2a91d4e'
down_revision: Union[str, None] = 'a1c4e9f82b3d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('prospects', sa.Column(
        'email_validation_status',
        sa.String(length=20),
        nullable=True,
    ))
    op.add_column('prospects', sa.Column(
        'email_validated_at',
        sa.DateTime(timezone=True),
        nullable=True,
    ))


def downgrade() -> None:
    op.drop_column('prospects', 'email_validated_at')
    op.drop_column('prospects', 'email_validation_status')
