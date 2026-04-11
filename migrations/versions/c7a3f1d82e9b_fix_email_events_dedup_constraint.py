"""fix_email_events_dedup_constraint

Replace the single-column unique constraint on smartlead_message_id with a
composite (smartlead_message_id, event_type) constraint so that sent, open,
click, and reply events for the same email message can all be stored, while
still deduplicating Smartlead's webhook retries.

Revision ID: c7a3f1d82e9b
Revises: f3c8b2a91d4e
Create Date: 2026-04-11

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c7a3f1d82e9b'
down_revision: Union[str, None] = 'f3c8b2a91d4e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        'email_events_smartlead_message_id_key',
        'email_events',
        type_='unique',
    )
    op.create_unique_constraint(
        'uq_email_events_message_event',
        'email_events',
        ['smartlead_message_id', 'event_type'],
    )


def downgrade() -> None:
    op.drop_constraint(
        'uq_email_events_message_event',
        'email_events',
        type_='unique',
    )
    op.create_unique_constraint(
        'email_events_smartlead_message_id_key',
        'email_events',
        ['smartlead_message_id'],
    )
