"""add smartlead_category to sequence_enrollments

Revision ID: p001_add_smartlead_category
Revises: o001_add_campaign_configs
Create Date: 2026-04-24
"""
from alembic import op
import sqlalchemy as sa

revision = "p001_add_smartlead_category"
down_revision = "o001_add_campaign_configs"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "sequence_enrollments",
        sa.Column("smartlead_category", sa.String(50), nullable=True),
    )


def downgrade():
    op.drop_column("sequence_enrollments", "smartlead_category")
