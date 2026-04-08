import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, field_validator

VALID_WEALTH_TIERS = ("mass_affluent", "HNWI", "UHNWI", "institutional")
VALID_INVESTOR_TYPES = ("individual", "family_office", "RIA", "broker_dealer", "endowment", "pension", "other")


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
    wealth_tier: Optional[str] = None
    investor_type: Optional[str] = None
    source: Optional[str] = "manual"

    @field_validator("asset_class_preference")
    @classmethod
    def validate_asset_class(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("PE", "RE", "both"):
            raise ValueError("asset_class_preference must be PE, RE, or both")
        return v

    @field_validator("wealth_tier")
    @classmethod
    def validate_wealth_tier(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in VALID_WEALTH_TIERS:
            raise ValueError(f"wealth_tier must be one of: {', '.join(VALID_WEALTH_TIERS)}")
        return v

    @field_validator("investor_type")
    @classmethod
    def validate_investor_type(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in VALID_INVESTOR_TYPES:
            raise ValueError(f"investor_type must be one of: {', '.join(VALID_INVESTOR_TYPES)}")
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
    wealth_tier: Optional[str]
    investor_type: Optional[str]
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


class EmailEventOut(BaseModel):
    id: uuid.UUID
    event_type: str
    email_subject: Optional[str]
    domain_used: Optional[str]
    clicked_url: Optional[str]
    occurred_at: datetime
    hubspot_synced_at: Optional[datetime]

    model_config = {"from_attributes": True}


class EnrollmentOut(BaseModel):
    id: uuid.UUID
    smartlead_campaign_id: str
    sequence_type: str
    track: str
    status: str
    enrolled_at: datetime
    high_intent_switched_at: Optional[datetime]
    opted_out_at: Optional[datetime]
    completed_at: Optional[datetime]
    events: list[EmailEventOut] = []

    model_config = {"from_attributes": True}


class ProspectActivityOut(BaseModel):
    id: uuid.UUID
    email: str
    first_name: Optional[str]
    last_name: Optional[str]
    company: Optional[str]
    title: Optional[str]
    asset_class_preference: Optional[str]
    wealth_tier: Optional[str]
    investor_type: Optional[str]
    geography: Optional[str]
    source: Optional[str]
    verified_email: bool
    accredited_status: str
    created_at: datetime
    enrollments: list[EnrollmentOut] = []

    model_config = {"from_attributes": True}
