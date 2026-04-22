import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

TRIGGER_CHOICES = ("none", "open", "click", "reply")


class CampaignConfig(Base):
    __tablename__ = "campaign_configs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    smartlead_campaign_id: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    campaign_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    hubspot_trigger_event: Mapped[str] = mapped_column(String(10), nullable=False, default="reply")
    hubspot_pipeline_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    hubspot_stage_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (Index("idx_campaign_configs_campaign_id", "smartlead_campaign_id"),)
