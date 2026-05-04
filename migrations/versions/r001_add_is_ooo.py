"""add is_ooo to email_events and backfill from raw_payload

Revision ID: r001_add_is_ooo
Revises: q001_add_sequence_number
Create Date: 2026-05-04
"""
from alembic import op
import sqlalchemy as sa

revision = "r001_add_is_ooo"
down_revision = "q001_add_sequence_number"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "email_events",
        sa.Column("is_ooo", sa.Boolean(), nullable=False, server_default="false"),
    )
    # Backfill: Smartlead category 6 = "Out of Office"
    op.execute("""
        UPDATE email_events
        SET is_ooo = TRUE
        WHERE event_type = 'reply'
          AND raw_payload IS NOT NULL
          AND (raw_payload::jsonb->>'reply_category')::int = 6
    """)


def downgrade():
    op.drop_column("email_events", "is_ooo")
