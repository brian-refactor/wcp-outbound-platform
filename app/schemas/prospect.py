import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, field_validator


class ProspectCreate(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: str
    company: Optional[str] = None
    title: Optional[str] = None
    linkedin_url: Optional[str] = None
    phone: Optional[str] = None
    asset_class_preference: Optional[str] = None
    net_worth_estimate: Optional[str] = None
    geography: Optional[str] = None
    source: Optional[str] = "manual"

    @field_validator("asset_class_preference")
    @classmethod
    def validate_asset_class(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("PE", "RE", "both"):
            raise ValueError("asset_class_preference must be PE, RE, or both")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        return v.strip().lower()


class ProspectOut(BaseModel):
    id: uuid.UUID
    first_name: Optional[str]
    last_name: Optional[str]
    email: str
    company: Optional[str]
    title: Optional[str]
    asset_class_preference: Optional[str]
    geography: Optional[str]
    source: Optional[str]
    verified_email: bool
    accredited_status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ImportResult(BaseModel):
    imported: int
    skipped: int
    errors: list[str]
