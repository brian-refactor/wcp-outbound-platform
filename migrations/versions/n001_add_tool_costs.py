"""add tool_costs table with pre-seeded tools

Revision ID: n001_add_tool_costs
Revises: m001_merge_heads
Create Date: 2026-04-14

"""
from typing import Union
import sqlalchemy as sa
from alembic import op

revision: str = 'n001_add_tool_costs'
down_revision: Union[str, None] = 'm001_merge_heads'
branch_labels = None
depends_on = None

SEED = [
    ("tc-001", "Smartlead",          "outreach",       0.00, "active", "Cold email sequencing platform"),
    ("tc-002", "HubSpot",            "crm",            0.00, "active", "CRM — contacts, notes, deals on reply"),
    ("tc-003", "ZeroBounce",         "validation",     0.00, "active", "Email validation — credits-based"),
    ("tc-004", "Anthropic (Claude)", "ai",             0.00, "active", "Personalized intro generation"),
    ("tc-005", "Apollo.io",          "enrichment",     0.00, "active", "Contact enrichment + people search"),
    ("tc-006", "Hunter.io",          "enrichment",     0.00, "active", "Email finder fallback"),
    ("tc-007", "Railway",            "hosting",        0.00, "active", "Web + worker service hosting"),
    ("tc-008", "Upstash Redis",      "infrastructure", 0.00, "active", "Celery broker — free tier"),
]


def upgrade() -> None:
    op.create_table(
        "tool_costs",
        sa.Column("id",           sa.String(36),     primary_key=True),
        sa.Column("name",         sa.String(255),    nullable=False),
        sa.Column("category",     sa.String(64),     nullable=False, server_default="other"),
        sa.Column("monthly_cost", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("status",       sa.String(16),     nullable=False, server_default="active"),
        sa.Column("notes",        sa.String(500),    nullable=True),
    )

    op.bulk_insert(
        sa.table(
            "tool_costs",
            sa.column("id",           sa.String),
            sa.column("name",         sa.String),
            sa.column("category",     sa.String),
            sa.column("monthly_cost", sa.Numeric),
            sa.column("status",       sa.String),
            sa.column("notes",        sa.String),
        ),
        [
            {"id": id_, "name": name, "category": cat,
             "monthly_cost": cost, "status": status, "notes": notes}
            for id_, name, cat, cost, status, notes in SEED
        ],
    )


def downgrade() -> None:
    op.drop_table("tool_costs")
