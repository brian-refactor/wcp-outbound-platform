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
    total_sent: int
    total_opened: int
    total_clicked: int
    total_replied: int
    total_bounced: int
    total_spam: int
    total_opted_out: int
    total_completed: int
    hubspot_deals: int
    hubspot_pending: int
    high_intent_upgrades: int
    open_rate: float
    click_rate: float
    reply_rate: float
    bounce_rate: float
    spam_rate: float
    unsubscribe_rate: float


class SequenceTypeRow(BaseModel):
    campaign_name: str
    enrolled: int
    opened: int
    clicked: int
    replied: int
    standard_count: int
    high_intent_count: int
    reply_rate: float


class CampaignFunnelRow(BaseModel):
    label: str
    enrolled: int
    sent: int
    opened: int
    clicked: int
    replied: int
    reply_rate: float


class DomainSendRow(BaseModel):
    domain: str
    total_sent: int
    last_sent_at: Optional[datetime]


class SequenceRow(BaseModel):
    campaign_name: str
    smartlead_campaign_id: str
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
def overview_stats(db: Session = Depends(get_db), campaign_id: Optional[str] = None):
    # Build reusable filters when scoped to a single campaign
    if campaign_id:
        enr_ids_q = db.query(SequenceEnrollment.id).filter(
            SequenceEnrollment.smartlead_campaign_id == campaign_id
        )
        ev_f = EmailEvent.enrollment_id.in_(enr_ids_q)
        enr_f = SequenceEnrollment.smartlead_campaign_id == campaign_id
        total_prospects = (
            db.query(func.count(func.distinct(SequenceEnrollment.prospect_id)))
            .filter(enr_f)
            .scalar() or 0
        )
    else:
        ev_f = None
        enr_f = None
        total_prospects = db.query(func.count(Prospect.id)).scalar() or 0

    def eq(q, *extra):
        return q.filter(*[f for f in extra if f is not None])

    active_enrollments = (
        eq(db.query(func.count(SequenceEnrollment.id)), enr_f,
           SequenceEnrollment.status == "active")
        .scalar() or 0
    )
    total_sent = (
        eq(db.query(func.count(EmailEvent.id)), ev_f,
           EmailEvent.event_type == "sent")
        .scalar() or 0
    )
    total_opened = (
        eq(db.query(func.count(EmailEvent.id)), ev_f,
           EmailEvent.event_type == "open")
        .scalar() or 0
    )
    # Bot-click filter: clicks within 20s of the open are security scanners, not humans
    if campaign_id:
        total_clicked = db.execute(text("""
            SELECT COUNT(DISTINCT ee.prospect_id)
            FROM email_events ee
            WHERE ee.event_type = 'click'
              AND ee.enrollment_id IN (
                  SELECT id FROM sequence_enrollments WHERE smartlead_campaign_id = :cid
              )
              AND EXISTS (
                  SELECT 1 FROM email_events oe
                  WHERE oe.enrollment_id = ee.enrollment_id
                    AND oe.event_type = 'open'
                    AND EXTRACT(EPOCH FROM (ee.occurred_at - oe.occurred_at)) >= 20
              )
        """), {"cid": campaign_id}).scalar() or 0
    else:
        total_clicked = db.execute(text("""
            SELECT COUNT(DISTINCT ee.prospect_id)
            FROM email_events ee
            WHERE ee.event_type = 'click'
              AND EXISTS (
                  SELECT 1 FROM email_events oe
                  WHERE oe.enrollment_id = ee.enrollment_id
                    AND oe.event_type = 'open'
                    AND EXTRACT(EPOCH FROM (ee.occurred_at - oe.occurred_at)) >= 20
              )
        """)).scalar() or 0
    total_replied = (
        eq(db.query(func.count(func.distinct(EmailEvent.prospect_id))), ev_f,
           EmailEvent.event_type == "reply")
        .scalar() or 0
    )
    total_bounced = (
        eq(db.query(func.count(SequenceEnrollment.id)), enr_f,
           SequenceEnrollment.status == "bounced")
        .scalar() or 0
    )
    total_spam = (
        eq(db.query(func.count(func.distinct(EmailEvent.prospect_id))), ev_f,
           EmailEvent.event_type == "spam")
        .scalar() or 0
    )
    total_opted_out = (
        eq(db.query(func.count(SequenceEnrollment.id)), enr_f,
           SequenceEnrollment.status == "opted_out")
        .scalar() or 0
    )
    total_completed = (
        eq(db.query(func.count(SequenceEnrollment.id)), enr_f,
           SequenceEnrollment.status == "completed")
        .scalar() or 0
    )
    hubspot_deals = (
        eq(db.query(func.count(func.distinct(EmailEvent.prospect_id))), ev_f,
           EmailEvent.event_type == "reply",
           EmailEvent.hubspot_synced_at.is_not(None))
        .scalar() or 0
    )
    hubspot_pending = (
        eq(db.query(func.count(EmailEvent.id)), ev_f,
           EmailEvent.hubspot_synced_at.is_(None),
           EmailEvent.prospect_id.is_not(None))
        .scalar() or 0
    )
    high_intent_upgrades = (
        eq(db.query(func.count(SequenceEnrollment.id)), enr_f,
           SequenceEnrollment.high_intent_switched_at.is_not(None))
        .scalar() or 0
    )

    def rate(n: int) -> float:
        return round(n / total_sent * 100, 1) if total_sent > 0 else 0.0

    return OverviewStats(
        total_prospects=total_prospects,
        active_enrollments=active_enrollments,
        total_sent=total_sent,
        total_opened=total_opened,
        total_clicked=total_clicked,
        total_replied=total_replied,
        total_bounced=total_bounced,
        total_spam=total_spam,
        total_opted_out=total_opted_out,
        total_completed=total_completed,
        hubspot_deals=hubspot_deals,
        hubspot_pending=hubspot_pending,
        high_intent_upgrades=high_intent_upgrades,
        open_rate=rate(total_opened),
        click_rate=rate(total_clicked),
        reply_rate=rate(total_replied),
        bounce_rate=rate(total_bounced),
        spam_rate=rate(total_spam),
        unsubscribe_rate=rate(total_opted_out),
    )


@router.get("/sequences/by-type", response_model=list[SequenceTypeRow])
def sequences_by_type(db: Session = Depends(get_db), campaign_id: Optional[str] = None):
    """Sequence funnel grouped by type only — tracks merged. Used for overview chart."""
    where = "WHERE se.smartlead_campaign_id = :campaign_id" if campaign_id else ""
    params = {"campaign_id": campaign_id} if campaign_id else {}
    rows = db.execute(text(f"""
        SELECT
            COALESCE(se.campaign_name, se.smartlead_campaign_id) AS campaign_name,
            COUNT(DISTINCT se.id)                                                             AS enrolled,
            COUNT(CASE WHEN ee.event_type = 'open'         THEN ee.id END) AS opened,
            COUNT(DISTINCT CASE WHEN ee.event_type = 'click'
                AND EXISTS (SELECT 1 FROM email_events oe WHERE oe.enrollment_id = ee.enrollment_id
                    AND oe.event_type = 'open'
                    AND EXTRACT(EPOCH FROM (ee.occurred_at - oe.occurred_at)) >= 20)
                THEN ee.prospect_id END)                                                      AS clicked,
            COUNT(DISTINCT CASE WHEN ee.event_type = 'reply'        THEN ee.prospect_id END) AS replied,
            COUNT(DISTINCT CASE WHEN se.track = 'standard'          THEN se.id END)          AS standard_count,
            COUNT(DISTINCT CASE WHEN se.track = 'high_intent'       THEN se.id END)          AS high_intent_count
        FROM sequence_enrollments se
        LEFT JOIN email_events ee ON ee.enrollment_id = se.id
        {where}
        GROUP BY COALESCE(se.campaign_name, se.smartlead_campaign_id)
        ORDER BY enrolled DESC
    """), params).mappings().all()

    result = []
    for row in rows:
        enrolled = row["enrolled"] or 0
        replied = row["replied"] or 0
        result.append(SequenceTypeRow(
            campaign_name=row["campaign_name"],
            enrolled=enrolled,
            opened=row["opened"] or 0,
            clicked=row["clicked"] or 0,
            replied=replied,
            standard_count=row["standard_count"] or 0,
            high_intent_count=row["high_intent_count"] or 0,
            reply_rate=round(replied / enrolled * 100, 1) if enrolled > 0 else 0.0,
        ))
    return result


def campaigns_funnel(db: Session, campaign_id: Optional[str] = None) -> list[CampaignFunnelRow]:
    """Funnel grouped by campaign name — used for the overview chart."""
    where = "WHERE se.smartlead_campaign_id = :campaign_id" if campaign_id else ""
    params = {"campaign_id": campaign_id} if campaign_id else {}
    rows = db.execute(text(f"""
        SELECT
            COALESCE(se.campaign_name, se.smartlead_campaign_id)              AS label,
            COUNT(DISTINCT se.id)                                              AS enrolled,
            COUNT(DISTINCT CASE WHEN ee.event_type = 'sent'  THEN ee.prospect_id END) AS sent,
            COUNT(CASE WHEN ee.event_type = 'open'  THEN ee.id END) AS opened,
            COUNT(DISTINCT CASE WHEN ee.event_type = 'click'
                AND EXISTS (SELECT 1 FROM email_events oe WHERE oe.enrollment_id = ee.enrollment_id
                    AND oe.event_type = 'open'
                    AND EXTRACT(EPOCH FROM (ee.occurred_at - oe.occurred_at)) >= 20)
                THEN ee.prospect_id END)                                                AS clicked,
            COUNT(DISTINCT CASE WHEN ee.event_type = 'reply' THEN ee.prospect_id END) AS replied
        FROM sequence_enrollments se
        LEFT JOIN email_events ee ON ee.enrollment_id = se.id
        {where}
        GROUP BY COALESCE(se.campaign_name, se.smartlead_campaign_id)
        ORDER BY enrolled DESC
    """), params).mappings().all()

    result = []
    for row in rows:
        enrolled = row["enrolled"] or 0
        replied = row["replied"] or 0
        result.append(CampaignFunnelRow(
            label=row["label"],
            enrolled=enrolled,
            sent=row["sent"] or 0,
            opened=row["opened"] or 0,
            clicked=row["clicked"] or 0,
            replied=replied,
            reply_rate=round(replied / enrolled * 100, 1) if enrolled > 0 else 0.0,
        ))
    return result


@router.get("/sequences", response_model=list[SequenceRow])
def sequence_stats(db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT
            COALESCE(se.campaign_name, se.smartlead_campaign_id) AS campaign_name,
            se.smartlead_campaign_id,
            se.track,
            COUNT(DISTINCT se.id)                                                          AS enrolled,
            COUNT(CASE WHEN ee.event_type = 'open'  THEN ee.id END)                       AS opened,
            COUNT(DISTINCT CASE WHEN ee.event_type = 'click'
                AND EXISTS (SELECT 1 FROM email_events oe WHERE oe.enrollment_id = ee.enrollment_id
                    AND oe.event_type = 'open'
                    AND EXTRACT(EPOCH FROM (ee.occurred_at - oe.occurred_at)) >= 20)
                THEN ee.prospect_id END)                                                    AS clicked,
            COUNT(DISTINCT CASE WHEN ee.event_type = 'reply' THEN ee.prospect_id END)     AS replied,
            COUNT(DISTINCT CASE WHEN se.status = 'bounced'   THEN se.id END)              AS bounced,
            COUNT(DISTINCT CASE WHEN se.status = 'opted_out' THEN se.id END)              AS opted_out
        FROM sequence_enrollments se
        LEFT JOIN email_events ee ON ee.enrollment_id = se.id
        GROUP BY COALESCE(se.campaign_name, se.smartlead_campaign_id), se.smartlead_campaign_id, se.track
        ORDER BY enrolled DESC, se.track
    """)).mappings().all()

    result = []
    for row in rows:
        enrolled = row["enrolled"] or 0
        replied = row["replied"] or 0
        result.append(SequenceRow(
            campaign_name=row["campaign_name"],
            smartlead_campaign_id=row["smartlead_campaign_id"],
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


class EmailStepRow(BaseModel):
    campaign_name: str
    email_subject: str
    sent: int
    opened: int
    clicked: int
    replied: int
    open_rate: float
    reply_rate: float


def sequence_email_stats(db: Session, campaign_id: Optional[str] = None) -> list[EmailStepRow]:
    """Per-email breakdown within each campaign, ordered by sequence step (sent desc)."""
    where = "AND se.smartlead_campaign_id = :campaign_id" if campaign_id else ""
    params = {"campaign_id": campaign_id} if campaign_id else {}
    rows = db.execute(text(f"""
        SELECT
            COALESCE(se.campaign_name, se.smartlead_campaign_id) AS campaign_name,
            ee.email_subject,
            COUNT(CASE WHEN ee.event_type = 'sent'  THEN ee.id END)                   AS sent,
            COUNT(CASE WHEN ee.event_type = 'open'  THEN ee.id END)                   AS opened,
            COUNT(DISTINCT CASE WHEN ee.event_type = 'click'
                AND EXISTS (SELECT 1 FROM email_events oe WHERE oe.enrollment_id = ee.enrollment_id
                    AND oe.event_type = 'open'
                    AND EXTRACT(EPOCH FROM (ee.occurred_at - oe.occurred_at)) >= 20)
                THEN ee.prospect_id END)                                               AS clicked,
            COUNT(DISTINCT CASE WHEN ee.event_type = 'reply' THEN ee.prospect_id END) AS replied
        FROM email_events ee
        JOIN sequence_enrollments se ON se.id = ee.enrollment_id
        WHERE ee.email_subject IS NOT NULL {where}
        GROUP BY COALESCE(se.campaign_name, se.smartlead_campaign_id), ee.email_subject
        ORDER BY campaign_name, sent DESC
    """), params).mappings().all()

    result = []
    for row in rows:
        sent = row["sent"] or 0
        opened = row["opened"] or 0
        replied = row["replied"] or 0
        result.append(EmailStepRow(
            campaign_name=row["campaign_name"],
            email_subject=row["email_subject"],
            sent=sent,
            opened=opened,
            clicked=row["clicked"] or 0,
            replied=replied,
            open_rate=round(opened / sent * 100, 1) if sent > 0 else 0.0,
            reply_rate=round(replied / sent * 100, 1) if sent > 0 else 0.0,
        ))
    return result


@router.get("/sends/by-domain", response_model=list[DomainSendRow])
def sends_by_domain(db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT
            COALESCE(domain_used, 'unknown')  AS domain,
            COUNT(*)                           AS total_sent,
            MAX(occurred_at)                   AS last_sent_at
        FROM email_events
        WHERE event_type = 'sent'
        GROUP BY domain_used
        ORDER BY total_sent DESC
    """)).mappings().all()

    return [
        DomainSendRow(
            domain=row["domain"],
            total_sent=row["total_sent"],
            last_sent_at=row["last_sent_at"],
        )
        for row in rows
    ]


@router.get("/events/recent", response_model=list[RecentEvent])
def recent_events(limit: int = 50, db: Session = Depends(get_db), campaign_id: Optional[str] = None):
    where = "WHERE se.smartlead_campaign_id = :campaign_id" if campaign_id else ""
    join  = "JOIN sequence_enrollments se ON se.id = ee.enrollment_id" if campaign_id else ""
    params = {"limit": min(limit, 200)}
    if campaign_id:
        params["campaign_id"] = campaign_id
    rows = db.execute(text(f"""
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
        {join}
        {where}
        ORDER BY ee.occurred_at DESC
        LIMIT :limit
    """), params).mappings().all()

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
