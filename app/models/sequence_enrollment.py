import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SequenceEnrollment(Base):
    __tablename__ = "sequence_enrollments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    prospect_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    # Smartlead campaign IDs (strings — Smartlead uses integer IDs but we store as str for flexibility)
    smartlead_campaign_id: Mapped[str] = mapped_column(String(50), nullable=False)
    high_intent_campaign_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # RE_DEAL | RE_FUND | PE_DEAL | PE_FUND
    sequence_type: Mapped[str] = mapped_column(String(20), nullable=False)

    # standard | high_intent
    track: Mapped[str] = mapped_column(String(20), nullable=False, default="standard")

    # active | completed | opted_out | bounced
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")

    enrolled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    high_intent_switched_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    opted_out_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # Fast lookup of all enrollments for a prospect
        Index("idx_enrollments_prospect", "prospect_id"),
        # Fast lookup of active standard-track enrollments for High Intent scan
        Index(
            "idx_enrollments_active_standard",
            "status",
            "track",
            postgresql_where=(status == "active"),
        ),
    )
