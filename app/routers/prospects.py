import csv
import io
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_api_key
from app.integrations import smartlead
from app.models.email_event import EmailEvent
from app.models.prospect import Prospect
from app.models.sequence_enrollment import SequenceEnrollment
from app.schemas.prospect import (
    EnrollmentOut,
    ImportResult,
    ProspectActivityOut,
    ProspectCreate,
    ProspectOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/prospects",
    tags=["prospects"],
    dependencies=[Depends(require_api_key)],
)


@router.get("/", response_model=list[ProspectOut])
def list_prospects(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    return db.query(Prospect).offset(skip).limit(limit).all()


@router.get("/{prospect_id}/activity", response_model=ProspectActivityOut)
def get_prospect_activity(prospect_id: str, db: Session = Depends(get_db)):
    """Full enrollment and email event history for a prospect."""
    prospect = db.query(Prospect).filter(Prospect.id == prospect_id).first()
    if not prospect:
        raise HTTPException(status_code=404, detail="Prospect not found")

    enrollments = (
        db.query(SequenceEnrollment)
        .filter(SequenceEnrollment.prospect_id == prospect.id)
        .order_by(SequenceEnrollment.enrolled_at.desc())
        .all()
    )

    # Fetch all events for these enrollments in one query
    events_by_enrollment: dict = {}
    if enrollments:
        enrollment_ids = [e.id for e in enrollments]
        all_events = (
            db.query(EmailEvent)
            .filter(EmailEvent.enrollment_id.in_(enrollment_ids))
            .order_by(EmailEvent.occurred_at.asc())
            .all()
        )
        for evt in all_events:
            events_by_enrollment.setdefault(evt.enrollment_id, []).append(evt)

    result = ProspectActivityOut.model_validate(prospect)
    result.enrollments = []
    for enrollment in enrollments:
        enr_out = EnrollmentOut.model_validate(enrollment)
        enr_out.events = events_by_enrollment.get(enrollment.id, [])
        result.enrollments.append(enr_out)

    return result


@router.get("/{prospect_id}", response_model=ProspectOut)
def get_prospect(prospect_id: str, db: Session = Depends(get_db)):
    prospect = db.query(Prospect).filter(Prospect.id == prospect_id).first()
    if not prospect:
        raise HTTPException(status_code=404, detail="Prospect not found")
    return prospect


@router.post("/", response_model=ProspectOut, status_code=201)
def create_prospect(data: ProspectCreate, db: Session = Depends(get_db)):
    prospect = Prospect(**data.model_dump())
    db.add(prospect)
    try:
        db.commit()
        db.refresh(prospect)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Email already exists")
    return prospect


@router.post("/import/csv", response_model=ImportResult)
async def import_csv(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """
    Import prospects from a CSV file.

    Expected columns (email is required, all others optional):
    email, first_name, last_name, company, title, linkedin_url,
    phone, asset_class_preference, geography, source
    """
    if not file.filename or not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="File must be a .csv")

    max_size = 10 * 1024 * 1024  # 10MB
    content = await file.read(max_size + 1)
    if len(content) > max_size:
        raise HTTPException(status_code=400, detail="File too large — 10MB maximum")

    text = content.decode("utf-8-sig")  # handles BOM from Excel exports
    reader = csv.DictReader(io.StringIO(text))

    imported = 0
    skipped = 0
    errors = []

    for row_num, row in enumerate(reader, start=2):  # row 1 is header
        email = (row.get("email") or "").strip().lower()
        if not email:
            errors.append(f"Row {row_num}: missing email — skipped")
            skipped += 1
            continue

        asset_class = (row.get("asset_class_preference") or "").strip() or None
        if asset_class and asset_class not in ("PE", "RE", "both"):
            errors.append(
                f"Row {row_num}: invalid asset_class_preference '{asset_class}' — set to null"
            )
            asset_class = None

        wealth_tier = (row.get("wealth_tier") or "").strip() or None
        if wealth_tier and wealth_tier not in ("mass_affluent", "HNWI", "UHNWI", "institutional"):
            errors.append(f"Row {row_num}: invalid wealth_tier '{wealth_tier}' — set to null")
            wealth_tier = None

        investor_type = (row.get("investor_type") or "").strip() or None
        if investor_type and investor_type not in ("individual", "family_office", "RIA", "broker_dealer", "endowment", "pension", "other"):
            errors.append(f"Row {row_num}: invalid investor_type '{investor_type}' — set to null")
            investor_type = None

        values = dict(
            id=uuid.uuid4(),
            email=email,
            first_name=(row.get("first_name") or "").strip() or None,
            last_name=(row.get("last_name") or "").strip() or None,
            company=(row.get("company") or "").strip() or None,
            title=(row.get("title") or "").strip() or None,
            linkedin_url=(row.get("linkedin_url") or "").strip() or None,
            phone=(row.get("phone") or "").strip() or None,
            asset_class_preference=asset_class,
            geography=(row.get("geography") or "").strip() or None,
            wealth_tier=wealth_tier,
            investor_type=investor_type,
            source=(row.get("source") or "").strip() or "apollo",
        )
        # ON CONFLICT DO NOTHING is atomic — no savepoint / transaction state issues
        # Use RETURNING id to detect dupes (rowcount is unreliable with psycopg3)
        stmt = (
            pg_insert(Prospect)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["email"])
            .returning(Prospect.id)
        )
        result = db.execute(stmt)
        if result.fetchone() is None:
            errors.append(f"Row {row_num}: {email} already exists — skipped")
            skipped += 1
        else:
            imported += 1

    db.commit()
    return ImportResult(imported=imported, skipped=skipped, errors=errors)


VALID_SEQUENCE_TYPES = {"RE_DEAL", "RE_FUND", "PE_DEAL", "PE_FUND"}


class EnrollRequest(BaseModel):
    campaign_id: int
    sequence_type: str  # RE_DEAL | RE_FUND | PE_DEAL | PE_FUND
    high_intent_campaign_id: Optional[int] = None  # Smartlead campaign to switch to on High Intent
    custom_fields: Optional[dict] = None


@router.post("/{prospect_id}/enroll")
def enroll_prospect(
    prospect_id: str,
    body: EnrollRequest,
    db: Session = Depends(get_db),
):
    """Enroll a prospect in a Smartlead campaign and start sequence tracking."""
    if body.sequence_type not in VALID_SEQUENCE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid sequence_type. Must be one of: {', '.join(sorted(VALID_SEQUENCE_TYPES))}",
        )

    prospect = db.query(Prospect).filter(Prospect.id == prospect_id).first()
    if not prospect:
        raise HTTPException(status_code=404, detail="Prospect not found")

    # Merge prospect record fields into any caller-supplied custom fields.
    # Prospect data takes precedence so Smartlead templates always have
    # accurate values regardless of what the caller passes.
    auto_fields = {k: v for k, v in {
        "company":                prospect.company,
        "title":                  prospect.title,
        "geography":              prospect.geography,
        "asset_class_preference": prospect.asset_class_preference,
        "wealth_tier":            prospect.wealth_tier,
        "investor_type":          prospect.investor_type,
        "linkedin_url":           prospect.linkedin_url,
        "phone":                  prospect.phone,
    }.items() if v}
    merged_custom_fields = {**(body.custom_fields or {}), **auto_fields}

    try:
        result = smartlead.enroll_prospect(
            campaign_id=body.campaign_id,
            email=prospect.email,
            first_name=prospect.first_name,
            last_name=prospect.last_name,
            custom_fields=merged_custom_fields,
        )
    except Exception as e:
        logger.error("Smartlead enrollment failed for %s: %s", prospect.email, e)
        raise HTTPException(status_code=502, detail=f"Smartlead error: {str(e)}")

    enrollment = SequenceEnrollment(
        prospect_id=prospect.id,
        smartlead_campaign_id=str(body.campaign_id),
        high_intent_campaign_id=str(body.high_intent_campaign_id) if body.high_intent_campaign_id else None,
        sequence_type=body.sequence_type,
    )
    db.add(enrollment)
    db.commit()
    db.refresh(enrollment)

    return {
        "status": "enrolled",
        "enrollment_id": str(enrollment.id),
        "smartlead_response": result,
    }
