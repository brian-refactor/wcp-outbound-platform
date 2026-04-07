import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Prospect(Base):
    __tablename__ = "prospects"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    first_name: Mapped[str | None] = mapped_column(String(255))
    last_name: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    company: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(String(255))
    linkedin_url: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(String(50))

    # Targeting
    asset_class_preference: Mapped[str | None] = mapped_column(String(10))  # PE | RE | both
    net_worth_estimate: Mapped[str | None] = mapped_column(String(20))  # bucketed; not from Apollo
    geography: Mapped[str | None] = mapped_column(String(100))

    # Source
    source: Mapped[str | None] = mapped_column(String(50))  # apollo | manual

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
