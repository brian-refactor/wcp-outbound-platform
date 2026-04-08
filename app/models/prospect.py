import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Prospect(Base):
    __tablename__ = "prospects"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    first_name: Mapped[Optional[str]] = mapped_column(String(255))
    last_name: Mapped[Optional[str]] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    company: Mapped[Optional[str]] = mapped_column(String(255))
    title: Mapped[Optional[str]] = mapped_column(String(255))
    linkedin_url: Mapped[Optional[str]] = mapped_column(Text)
    phone: Mapped[Optional[str]] = mapped_column(String(50))

    # Targeting
    asset_class_preference: Mapped[Optional[str]] = mapped_column(String(10))  # PE | RE | both
    net_worth_estimate: Mapped[Optional[str]] = mapped_column(String(20))  # bucketed; not from Apollo
    geography: Mapped[Optional[str]] = mapped_column(String(100))

    # Investor classification
    wealth_tier: Mapped[Optional[str]] = mapped_column(String(20))   # mass_affluent | HNWI | UHNWI | institutional
    investor_type: Mapped[Optional[str]] = mapped_column(String(20)) # individual | family_office | RIA | broker_dealer | endowment | pension | other

    # Source
    source: Mapped[Optional[str]] = mapped_column(String(50))  # apollo | manual

    # Status
    verified_email: Mapped[bool] = mapped_column(Boolean, default=False)
    accredited_status: Mapped[str] = mapped_column(
        String(20), default="unverified"
    )  # unverified | pending | verified | failed

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
