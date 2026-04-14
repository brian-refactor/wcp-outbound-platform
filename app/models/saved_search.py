import uuid

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SavedSearch(Base):
    __tablename__ = "saved_searches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    params: Mapped[str] = mapped_column(String(1024), nullable=False)  # JSON-encoded search params
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
