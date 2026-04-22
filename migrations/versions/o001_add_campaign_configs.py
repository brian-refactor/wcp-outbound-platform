"""add campaign_configs table

Revision ID: o001_add_campaign_configs
Revises: n001_add_tool_costs
Create Date: 2026-04-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'o001_add_campaign_configs'
down_revision: Union[str, None] = 'n001_add_tool_costs'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "campaign_configs",
        sa.Column("id",                    sa.UUID(),                   nullable=False),
        sa.Column("smartlead_campaign_id", sa.String(50),               nullable=False),
        sa.Column("campaign_name",         sa.String(255),              nullable=True),
        sa.Column("hubspot_trigger_event", sa.String(10),               nullable=False, server_default="reply"),
        sa.Column("hubspot_pipeline_id",   sa.String(50),               nullable=True),
        sa.Column("hubspot_stage_id",      sa.String(50),               nullable=True),
        sa.Column("created_at",            sa.DateTime(timezone=True),  nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at",            sa.DateTime(timezone=True),  nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("smartlead_campaign_id", name="uq_campaign_configs_campaign_id"),
    )
    op.create_index("idx_campaign_configs_campaign_id", "campaign_configs", ["smartlead_campaign_id"])


def downgrade() -> None:
    op.drop_index("idx_campaign_configs_campaign_id", table_name="campaign_configs")
    op.drop_table("campaign_configs")
