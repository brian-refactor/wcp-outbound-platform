"""merge personalized_intro and saved_searches heads

Revision ID: m001_merge_heads
Revises: 4284f4b07285, a7f2c3d84e1b
Create Date: 2026-04-14

"""
from typing import Union

# revision identifiers, used by Alembic.
revision: str = 'm001_merge_heads'
down_revision: Union[str, tuple] = ('4284f4b07285', 'a7f2c3d84e1b')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
