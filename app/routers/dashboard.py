"""
Dashboard UI — server-rendered HTML pages using Jinja2 + HTMX.
All pages are read-only; no API key required (internal ops tool).
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.email_event import EmailEvent
from app.models.prospect import Prospect
from app.models.sequence_enrollment import SequenceEnrollment
from app.integrations import smartlead
from app.routers.stats import (
    overview_stats,
    recent_events,
    sends_by_domain,
    sequence_stats,
    sync_stats,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dashboard", tags=["dashboard"])
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["enumerate"] = enumerate

PAGE_SIZE = 25


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def dashboard_overview(request: Request, db: Session = Depends(get_db)):
    stats = overview_stats(db=db)
    seq = sequence_stats(db=db)
    events = recent_events(limit=20, db=db)
    return templates.TemplateResponse(
        "dashboard/overview.html",
        {
            "request": request,
            "stats": stats,
            "sequences": seq,
            "events": events,
            "active_page": "overview",
        },
    )


# ---------------------------------------------------------------------------
# Activity feed fragment (HTMX polling target)
# ---------------------------------------------------------------------------

@router.get("/fragments/activity", response_class=HTMLResponse)
def activity_fragment(request: Request, db: Session = Depends(get_db)):
    events = recent_events(limit=20, db=db)
    return templates.TemplateResponse(
        "dashboard/fragments/activity_feed.html",
        {"request": request, "events": events},
    )


# ---------------------------------------------------------------------------
# Prospects list
# ---------------------------------------------------------------------------

@router.get("/prospects", response_class=HTMLResponse)
def dashboard_prospects(
    request: Request,
    search: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    db: Session = Depends(get_db),
):
    offset = (page - 1) * PAGE_SIZE

    base_query = """
        SELECT
            p.id::text,
            p.first_name,
            p.last_name,
            p.email,
            p.company,
            p.title,
            p.created_at,
            (SELECT se.status FROM sequence_enrollments se
             WHERE se.prospect_id = p.id ORDER BY se.enrolled_at DESC LIMIT 1)          AS latest_status,
            (SELECT se.sequence_type FROM sequence_enrollments se
             WHERE se.prospect_id = p.id ORDER BY se.enrolled_at DESC LIMIT 1)          AS latest_sequence_type,
            (SELECT se.track FROM sequence_enrollments se
             WHERE se.prospect_id = p.id ORDER BY se.enrolled_at DESC LIMIT 1)          AS latest_track,
            (SELECT COUNT(*) FROM sequence_enrollments se
             WHERE se.prospect_id = p.id)                                                AS enrollment_count,
            (SELECT COUNT(*) FROM email_events ee
             WHERE ee.prospect_id = p.id)                                                AS event_count,
            (SELECT ee.event_type FROM email_events ee
             WHERE ee.prospect_id = p.id ORDER BY ee.occurred_at DESC LIMIT 1)          AS last_event_type,
            (SELECT ee.occurred_at FROM email_events ee
             WHERE ee.prospect_id = p.id ORDER BY ee.occurred_at DESC LIMIT 1)          AS last_event_at
        FROM prospects p
        WHERE 1=1
    """

    params: dict = {}

    if search:
        base_query += """
            AND (
                p.email     ILIKE '%' || :search || '%' OR
                p.first_name ILIKE '%' || :search || '%' OR
                p.last_name  ILIKE '%' || :search || '%' OR
                p.company    ILIKE '%' || :search || '%'
            )
        """
        params["search"] = search

    if status:
        base_query += """
            AND EXISTS (
                SELECT 1 FROM sequence_enrollments se2
                WHERE se2.prospect_id = p.id
                  AND se2.status = :status
                ORDER BY se2.enrolled_at DESC
                LIMIT 1
            )
        """
        params["status"] = status

    count_sql = f"SELECT COUNT(*) FROM ({base_query}) AS sub"
    total = db.execute(text(count_sql), params).scalar() or 0

    data_query = base_query + " ORDER BY p.created_at DESC LIMIT :limit OFFSET :offset"
    params["limit"] = PAGE_SIZE
    params["offset"] = offset
    rows = db.execute(text(data_query), params).mappings().all()

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    return templates.TemplateResponse(
        "dashboard/prospects.html",
        {
            "request": request,
            "prospects": rows,
            "search": search or "",
            "status_filter": status or "",
            "page": page,
            "total": total,
            "total_pages": total_pages,
            "active_page": "prospects",
        },
    )


# ---------------------------------------------------------------------------
# Prospect detail
# ---------------------------------------------------------------------------

@router.get("/prospects/{prospect_id}", response_class=HTMLResponse)
def dashboard_prospect_detail(
    prospect_id: str, request: Request, db: Session = Depends(get_db)
):
    prospect = db.query(Prospect).filter(Prospect.id == prospect_id).first()
    if not prospect:
        return HTMLResponse("<h1>Prospect not found</h1>", status_code=404)

    enrollments = (
        db.query(SequenceEnrollment)
        .filter(SequenceEnrollment.prospect_id == prospect.id)
        .order_by(SequenceEnrollment.enrolled_at.desc())
        .all()
    )

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
            events_by_enrollment.setdefault(str(evt.enrollment_id), []).append(evt)

    return templates.TemplateResponse(
        "dashboard/prospect_detail.html",
        {
            "request": request,
            "prospect": prospect,
            "enrollments": enrollments,
            "events_by_enrollment": events_by_enrollment,
            "active_page": "prospects",
        },
    )


# ---------------------------------------------------------------------------
# Sequence performance
# ---------------------------------------------------------------------------

@router.get("/sequences", response_class=HTMLResponse)
def dashboard_sequences(request: Request, db: Session = Depends(get_db)):
    seq = sequence_stats(db=db)

    # Top companies by reply count
    top_companies = db.execute(text("""
        SELECT
            p.company,
            COUNT(DISTINCT se.id)                                                   AS enrolled,
            COUNT(DISTINCT CASE WHEN ee.event_type = 'reply' THEN p.id END)        AS replied
        FROM prospects p
        JOIN sequence_enrollments se ON se.prospect_id = p.id
        LEFT JOIN email_events ee ON ee.prospect_id = p.id AND ee.event_type = 'reply'
        WHERE p.company IS NOT NULL AND p.company != ''
        GROUP BY p.company
        ORDER BY replied DESC, enrolled DESC
        LIMIT 15
    """)).mappings().all()

    return templates.TemplateResponse(
        "dashboard/sequences.html",
        {
            "request": request,
            "sequences": seq,
            "top_companies": top_companies,
            "active_page": "sequences",
        },
    )


# ---------------------------------------------------------------------------
# Mailboxes
# ---------------------------------------------------------------------------

@router.get("/mailboxes", response_class=HTMLResponse)
def dashboard_mailboxes(request: Request, db: Session = Depends(get_db)):
    # Live mailbox list from Smartlead API
    mailboxes = []
    smartlead_error = None
    try:
        mailboxes = smartlead.list_email_accounts()
    except Exception as e:
        smartlead_error = str(e)
        logger.warning("Could not fetch Smartlead email accounts: %s", e)

    # Local sends breakdown by sending domain
    domain_sends = sends_by_domain(db=db)

    # Build a quick lookup: domain -> send count from our DB
    domain_send_map = {row.domain: row.total_sent for row in domain_sends}

    return templates.TemplateResponse(
        "dashboard/mailboxes.html",
        {
            "request": request,
            "mailboxes": mailboxes,
            "smartlead_error": smartlead_error,
            "domain_sends": domain_sends,
            "domain_send_map": domain_send_map,
            "active_page": "mailboxes",
        },
    )


# ---------------------------------------------------------------------------
# Sync health
# ---------------------------------------------------------------------------

@router.get("/sync", response_class=HTMLResponse)
def dashboard_sync(request: Request, db: Session = Depends(get_db)):
    sync = sync_stats(db=db)

    recent_synced = db.execute(text("""
        SELECT
            p.email,
            NULLIF(TRIM(COALESCE(p.first_name,'') || ' ' || COALESCE(p.last_name,'')), '') AS prospect_name,
            ee.event_type,
            ee.occurred_at,
            ee.hubspot_synced_at
        FROM email_events ee
        LEFT JOIN prospects p ON p.id = ee.prospect_id
        WHERE ee.hubspot_synced_at IS NOT NULL
        ORDER BY ee.hubspot_synced_at DESC
        LIMIT 50
    """)).mappings().all()

    return templates.TemplateResponse(
        "dashboard/sync.html",
        {
            "request": request,
            "sync": sync,
            "recent_synced": recent_synced,
            "active_page": "sync",
        },
    )
