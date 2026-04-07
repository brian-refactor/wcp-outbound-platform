import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, field_validator


class ProspectCreate(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    email: str
    company: str | None = None
    title: str | None = None
    linkedin_url: str | None = None
    phone: str | None = None
    asset_class_preference: str | None = None
    net_worth_estimate: str | None = None
    geography: str | None = None
    source: str | None = "manual"

    @field_validator("asset_class_preference")
    @classmethod
    def validate_asset_class(cls, v: str | None) -> str | None:
        if v is not None and v not in ("PE", "RE", "both"):
            raise ValueError("asset_class_preference must be PE, RE, or both")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        return v.strip().lower()


class ProspectOut(BaseModel):
    id: uuid.UUID
    first_name: str | None
    last_name: str | None
    email: str
    company: str | None
    title: str | None
    asset_class_preference: str | None
    geography: str | None
    source: str | None
    verified_email: bool
    accredited_status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ImportResult(BaseModel):
    imported: int
    skipped: int
    errors: list[str]
