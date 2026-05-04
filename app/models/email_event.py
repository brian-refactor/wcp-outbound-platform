import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class EmailEvent(Base):
    __tablename__ = "email_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    prospect_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    enrollment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    event_type: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # sent | open | click | reply | bounce | unsubscribe

    email_subject: Mapped[Optional[str]] = mapped_column(Text)
    domain_used: Mapped[Optional[str]] = mapped_column(String(255))
    clicked_url: Mapped[Optional[str]] = mapped_column(Text)
    sequence_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Smartlead dedup key — prevents duplicate inserts from webhook retries.
    # Unique per (message_id, event_type) so sent/open/click/reply for the same
    # email are all stored, but retries of the exact same event are dropped.
    smartlead_message_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )

    # NULL until synced to HubSpot — used by the 5-min batch sync task
    hubspot_synced_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # True when the reply is an Out of Office auto-reply — excluded from reply counts
    is_ooo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    # Raw payload from Smartlead for debugging
    raw_payload: Mapped[Optional[str]] = mapped_column(Text)

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "smartlead_message_id", "event_type",
            name="uq_email_events_message_event",
        ),
        # Fast lookup of all events for a given enrollment
        Index("idx_email_events_enrollment", "enrollment_id"),
        # Fast lookup of unsynced events for HubSpot batch sync
        Index(
            "idx_email_events_hubspot_sync",
            "occurred_at",
            postgresql_where=(hubspot_synced_at is None),
        ),
    )
