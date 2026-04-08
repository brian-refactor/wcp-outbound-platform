"""
Stats API — read-only aggregations over prospects, enrollments, and email events.
Feeds both the dashboard UI and available as raw JSON.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.email_event import EmailEvent
from app.models.prospect import Prospect
from app.models.sequence_enrollment import SequenceEnrollment

router = APIRouter(prefix="/stats", tags=["stats"])


class OverviewStats(BaseModel):
    total_prospects: int
    active_enrollments: int
    total_replied: int
    total_bounced: int
    total_opted_out: int
    total_completed: int
    hubspot_pending: int


class SequenceRow(BaseModel):
    sequence_type: str
    track: str
    enrolled: int
    opened: int
    clicked: int
    replied: int
    bounced: int
    opted_out: int
    reply_rate: float


class SyncStats(BaseModel):
    pending: int
    synced_last_1h: int
    synced_last_24h: int
    last_synced_at: Optional[datetime]
    oldest_pending_at: Optional[datetime]


class RecentEvent(BaseModel):
    id: str
    prospect_email: str
    prospect_name: Optional[str]
    company: Optional[str]
    event_type: str
    email_subject: Optional[str]
    domain_used: Optional[str]
    occurred_at: datetime
    hubspot_synced_at: Optional[datetime]


@router.get("/overview", response_model=OverviewStats)
def overview_stats(db: Session = Depends(get_db)):
    total_prospects = db.query(func.count(Prospect.id)).scalar() or 0
    active_enrollments = (
        db.query(func.count(SequenceEnrollment.id))
        .filter(SequenceEnrollment.status == "active")
        .scalar() or 0
    )
    total_replied = (
        db.query(func.count(func.distinct(EmailEvent.prospect_id)))
        .filter(EmailEvent.event_type == "reply")
        .scalar() or 0
    )
    total_bounced = (
        db.query(func.count(SequenceEnrollment.id))
        .filter(SequenceEnrollment.status == "bounced")
        .scalar() or 0
    )
    total_opted_out = (
        db.query(func.count(SequenceEnrollment.id))
        .filter(SequenceEnrollment.status == "opted_out")
        .scalar() or 0
    )
    total_completed = (
        db.query(func.count(SequenceEnrollment.id))
        .filter(SequenceEnrollment.status == "completed")
        .scalar() or 0
    )
    hubspot_pending = (
        db.query(func.count(EmailEvent.id))
        .filter(
            EmailEvent.hubspot_synced_at.is_(None),
            EmailEvent.prospect_id.is_not(None),
        )
        .scalar() or 0
    )

    return OverviewStats(
        total_prospects=total_prospects,
        active_enrollments=active_enrollments,
        total_replied=total_replied,
        total_bounced=total_bounced,
        total_opted_out=total_opted_out,
        total_completed=total_completed,
        hubspot_pending=hubspot_pending,
    )


@router.get("/sequences", response_model=list[SequenceRow])
def sequence_stats(db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT
            se.sequence_type,
            se.track,
            COUNT(DISTINCT se.id)                                                          AS enrolled,
            COUNT(DISTINCT CASE WHEN ee.event_type = 'open'  THEN ee.prospect_id END)     AS opened,
            COUNT(DISTINCT CASE WHEN ee.event_type = 'click' THEN ee.prospect_id END)     AS clicked,
            COUNT(DISTINCT CASE WHEN ee.event_type = 'reply' THEN ee.prospect_id END)     AS replied,
            COUNT(DISTINCT CASE WHEN se.status = 'bounced'   THEN se.id END)              AS bounced,
            COUNT(DISTINCT CASE WHEN se.status = 'opted_out' THEN se.id END)              AS opted_out
        FROM sequence_enrollments se
        LEFT JOIN email_events ee ON ee.enrollment_id = se.id
        GROUP BY se.sequence_type, se.track
        ORDER BY se.sequence_type, se.track
    """)).mappings().all()

    result = []
    for row in rows:
        enrolled = row["enrolled"] or 0
        replied = row["replied"] or 0
        result.append(SequenceRow(
            sequence_type=row["sequence_type"],
            track=row["track"],
            enrolled=enrolled,
            opened=row["opened"] or 0,
            clicked=row["clicked"] or 0,
            replied=replied,
            bounced=row["bounced"] or 0,
            opted_out=row["opted_out"] or 0,
            reply_rate=round(replied / enrolled * 100, 1) if enrolled > 0 else 0.0,
        ))
    return result


@router.get("/sync", response_model=SyncStats)
def sync_stats(db: Session = Depends(get_db)):
    row = db.execute(text("""
        SELECT
            COUNT(*) FILTER (WHERE hubspot_synced_at IS NULL AND prospect_id IS NOT NULL)  AS pending,
            COUNT(*) FILTER (WHERE hubspot_synced_at >= NOW() - INTERVAL '1 hour')         AS synced_last_1h,
            COUNT(*) FILTER (WHERE hubspot_synced_at >= NOW() - INTERVAL '24 hours')       AS synced_last_24h,
            MAX(hubspot_synced_at)                                                          AS last_synced_at,
            MIN(occurred_at) FILTER (WHERE hubspot_synced_at IS NULL
                                     AND   prospect_id IS NOT NULL)                        AS oldest_pending_at
        FROM email_events
    """)).mappings().one()

    return SyncStats(
        pending=row["pending"] or 0,
        synced_last_1h=row["synced_last_1h"] or 0,
        synced_last_24h=row["synced_last_24h"] or 0,
        last_synced_at=row["last_synced_at"],
        oldest_pending_at=row["oldest_pending_at"],
    )


@router.get("/events/recent", response_model=list[RecentEvent])
def recent_events(limit: int = 50, db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT
            ee.id::text,
            COALESCE(p.email, 'unknown')                                         AS prospect_email,
            NULLIF(TRIM(COALESCE(p.first_name,'') || ' ' || COALESCE(p.last_name,'')), '') AS prospect_name,
            p.company,
            ee.event_type,
            ee.email_subject,
            ee.domain_used,
            ee.occurred_at,
            ee.hubspot_synced_at
        FROM email_events ee
        LEFT JOIN prospects p ON p.id = ee.prospect_id
        ORDER BY ee.occurred_at DESC
        LIMIT :limit
    """), {"limit": min(limit, 200)}).mappings().all()

    return [
        RecentEvent(
            id=row["id"],
            prospect_email=row["prospect_email"],
            prospect_name=row["prospect_name"],
            company=row["company"],
            event_type=row["event_type"],
            email_subject=row["email_subject"],
            domain_used=row["domain_used"],
            occurred_at=row["occurred_at"],
            hubspot_synced_at=row["hubspot_synced_at"],
        )
        for row in rows
    ]
