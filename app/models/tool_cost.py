from sqlalchemy import Numeric, String
from sqlalchemy.orm import Mapped, mapped_column
from typing import Optional

from app.database import Base


class ToolCost(Base):
    __tablename__ = "tool_costs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="other")
    monthly_cost: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")  # active / inactive
    notes: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
