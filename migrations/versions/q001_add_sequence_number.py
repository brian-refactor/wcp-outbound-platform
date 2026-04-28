"""add sequence_number to email_events and backfill clicked_url

Revision ID: q001_add_sequence_number
Revises: p001_add_smartlead_category
Create Date: 2026-04-28
"""
from alembic import op
import sqlalchemy as sa

revision = "q001_add_sequence_number"
down_revision = "p001_add_smartlead_category"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "email_events",
        sa.Column("sequence_number", sa.Integer(), nullable=True),
    )

    # Backfill sequence_number for all events from stored raw_payload
    op.execute("""
        UPDATE email_events
        SET sequence_number = (raw_payload::json ->> 'sequence_number')::int
        WHERE raw_payload IS NOT NULL
          AND raw_payload::json ->> 'sequence_number' IS NOT NULL
    """)

    # Backfill clicked_url for click events — webhook was reading wrong field name
    # Smartlead sends link_clicked (array), handler was reading clicked_link (missing)
    op.execute("""
        UPDATE email_events
        SET clicked_url = (raw_payload::json -> 'link_clicked' ->> 0)
        WHERE event_type = 'click'
          AND clicked_url IS NULL
          AND raw_payload IS NOT NULL
          AND raw_payload::json -> 'link_clicked' ->> 0 IS NOT NULL
    """)


def downgrade():
    op.drop_column("email_events", "sequence_number")
