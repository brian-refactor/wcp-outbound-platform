"""
Dashboard UI — server-rendered HTML pages using Jinja2 + HTMX.
All pages are read-only; no API key required (internal ops tool).
"""

import csv
import io
import json as _json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import settings
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.integrations import apollo as apollo_client
from app.integrations import edgar as edgar_client
from app.integrations import hunter as hunter_client
from app.integrations import smartlead, bouncer
from app.models.email_event import EmailEvent
from app.models.prospect import Prospect
from app.models.sequence_enrollment import SequenceEnrollment
from app.models.tool_cost import ToolCost
from app.routers.stats import (
    campaigns_funnel,
    overview_stats,
    recent_events,
    sends_by_domain,
    sequence_stats,
    sequence_email_stats,
    sequences_by_type,
    sync_stats,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dashboard", tags=["dashboard"])
auth_router = APIRouter(tags=["auth"])
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["enumerate"] = enumerate


def _to_et(dt, fmt="%b %d, %H:%M"):
    """Convert a UTC datetime to US/Eastern and format it."""
    from zoneinfo import ZoneInfo
    if dt is None:
        return ""
    eastern = ZoneInfo("America/New_York")
    if dt.tzinfo is None:
        from datetime import timezone
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(eastern).strftime(fmt)


templates.env.filters["to_et"] = _to_et
templates.env.filters["fromjson"] = _json.loads


# ---------------------------------------------------------------------------
# Login / Logout
# ---------------------------------------------------------------------------

@auth_router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get("authenticated"):
        return RedirectResponse(url="/dashboard", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@auth_router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    if (
        username == settings.dashboard_username
        and password == settings.dashboard_password
    ):
        request.session["authenticated"] = True
        return RedirectResponse(url="/dashboard", status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "Invalid username or password."},
        status_code=401,
    )


@auth_router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)

PAGE_SIZE = 25


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def dashboard_overview(
    request: Request,
    db: Session = Depends(get_db),
    campaign_id: Optional[str] = Query(None),
):
    from app.integrations.bouncer import get_credits
    from app.models.prospect import Prospect as ProspectModel
    campaigns = []
    try:
        campaigns = smartlead.list_campaigns()
    except Exception:
        pass
    stats = overview_stats(db=db, campaign_id=campaign_id)
    funnel = campaigns_funnel(db=db, campaign_id=campaign_id)
    events = recent_events(limit=20, db=db, campaign_id=campaign_id)
    zb_credits = get_credits()
    zb_used = db.query(func.count(ProspectModel.id)).filter(
        ProspectModel.email_validated_at.is_not(None)
    ).scalar() or 0
    return templates.TemplateResponse(
        "dashboard/overview.html",
        {
            "request": request,
            "stats": stats,
            "funnel": funnel,
            "events": events,
            "zb_credits": zb_credits,
            "zb_used": zb_used,
            "active_page": "overview",
            "campaigns": campaigns,
            "selected_campaign_id": campaign_id,
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


@router.get("/fragments/zb-credits", response_class=HTMLResponse)
def zb_credits_fragment(request: Request, db: Session = Depends(get_db)):
    credits = bouncer.get_credits()
    used = db.query(func.count(Prospect.id)).filter(
        Prospect.email_validated_at.is_not(None)
    ).scalar() or 0
    low = 0 <= credits < 500
    return templates.TemplateResponse(
        "dashboard/fragments/zb_credits.html",
        {"request": request, "credits": credits, "used": used, "low": low},
    )


@router.get("/fragments/revalidate-status", response_class=HTMLResponse)
def revalidate_status_fragment(request: Request, db: Session = Depends(get_db)):
    from datetime import datetime, timezone
    count = db.execute(
        text("SELECT COUNT(*) FROM prospects WHERE email_validation_status = 'unknown'")
    ).scalar() or 0
    checked = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    dismiss = (
        '<button type="button" onclick="this.closest(\'[data-flash]\').remove()"'
        ' class="ml-auto text-gray-400 hover:text-gray-600 shrink-0">'
        '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">'
        '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>'
        "</svg></button>"
    )

    if count == 0:
        return HTMLResponse(
            '<div id="revalidate-status" data-flash class="rounded-xl border border-green-200 bg-green-50 px-5 py-3 mb-5 flex items-center gap-3">'
            '<svg class="w-4 h-4 text-green-500 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">'
            '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/>'
            "</svg>"
            '<span class="text-sm font-medium text-green-800">Revalidation complete — all unknown emails have been processed.</span>'
            + dismiss +
            "</div>"
        )

    return HTMLResponse(
        '<div id="revalidate-status" data-flash'
        ' hx-get="/dashboard/fragments/revalidate-status"'
        ' hx-trigger="every 10s"'
        ' hx-swap="outerHTML"'
        ' class="rounded-xl border border-blue-200 bg-blue-50 px-5 py-3 mb-5 flex items-center gap-3">'
        '<svg class="w-4 h-4 text-blue-500 shrink-0 animate-spin" fill="none" viewBox="0 0 24 24">'
        '<circle class="opacity-20" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="3"/>'
        '<path class="opacity-80" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"/>'
        "</svg>"
        f'<span class="text-sm font-medium text-blue-800">Revalidation in progress — <strong>{count}</strong> unknown emails remaining. '
        f'<span class="font-normal text-blue-600">Last checked {checked}</span></span>'
        + dismiss +
        "</div>"
    )


@router.get("/fragments/zb-alert", response_class=HTMLResponse)
def zb_alert_fragment(request: Request):
    credits = bouncer.get_credits()
    if credits < 0 or credits >= 500:
        return HTMLResponse("")
    return templates.TemplateResponse(
        "dashboard/fragments/zb_alert.html",
        {"request": request, "credits": credits},
    )


# ---------------------------------------------------------------------------
# Prospects list
# ---------------------------------------------------------------------------

@router.get("/prospects", response_class=HTMLResponse)
def dashboard_prospects(
    request: Request,
    search: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    investor_type: Optional[str] = Query(None),
    wealth_tier: Optional[str] = Query(None),
    enrolled: Optional[str] = Query(None),  # "yes" | "no"
    email_validation: Optional[str] = Query(None),  # valid | invalid | catch-all | unknown | none
    campaign_id: Optional[str] = Query(None),
    intro: Optional[str] = Query(None),  # "has" | "missing"
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
            (SELECT se.track FROM sequence_enrollments se
             WHERE se.prospect_id = p.id ORDER BY se.enrolled_at DESC LIMIT 1)          AS latest_track,
            (SELECT string_agg(
                COALESCE(se.campaign_name, se.smartlead_campaign_id) || '|' || se.status || '|' || COALESCE(se.smartlead_category, ''),
                ',' ORDER BY se.enrolled_at DESC
             ) FROM sequence_enrollments se
             WHERE se.prospect_id = p.id)                                                AS enrollments_summary,
            (SELECT COUNT(*) FROM sequence_enrollments se
             WHERE se.prospect_id = p.id)                                                AS enrollment_count,
            (SELECT COUNT(*) FROM email_events ee
             WHERE ee.prospect_id = p.id)                                                AS event_count,
            (SELECT ee.event_type FROM email_events ee
             WHERE ee.prospect_id = p.id ORDER BY ee.occurred_at DESC LIMIT 1)          AS last_event_type,
            (SELECT ee.occurred_at FROM email_events ee
             WHERE ee.prospect_id = p.id ORDER BY ee.occurred_at DESC LIMIT 1)          AS last_event_at,
            p.email_validation_status,
            p.investor_type,
            p.wealth_tier,
            (p.personalized_intro IS NOT NULL)                                              AS has_intro,
            -- HubSpot status: deal > contact > pending > none
            CASE
                WHEN EXISTS (SELECT 1 FROM email_events ee
                             WHERE ee.prospect_id = p.id
                               AND ee.event_type = 'reply'
                               AND ee.hubspot_synced_at IS NOT NULL)      THEN 'deal'
                WHEN EXISTS (SELECT 1 FROM email_events ee
                             WHERE ee.prospect_id = p.id
                               AND ee.hubspot_synced_at IS NOT NULL)      THEN 'contact'
                WHEN EXISTS (SELECT 1 FROM email_events ee
                             WHERE ee.prospect_id = p.id)                 THEN 'pending'
                ELSE NULL
            END                                                                          AS hubspot_status
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
            )
        """
        params["status"] = status

    if investor_type:
        base_query += " AND p.investor_type = :investor_type"
        params["investor_type"] = investor_type

    if wealth_tier:
        base_query += " AND p.wealth_tier = :wealth_tier"
        params["wealth_tier"] = wealth_tier

    if enrolled == "yes":
        base_query += " AND EXISTS (SELECT 1 FROM sequence_enrollments se2 WHERE se2.prospect_id = p.id)"
    elif enrolled == "no":
        base_query += " AND NOT EXISTS (SELECT 1 FROM sequence_enrollments se2 WHERE se2.prospect_id = p.id)"

    if email_validation == "none":
        base_query += " AND p.email_validation_status IS NULL"
    elif email_validation:
        base_query += " AND p.email_validation_status = :email_validation"
        params["email_validation"] = email_validation

    if campaign_id:
        base_query += """
            AND EXISTS (
                SELECT 1 FROM sequence_enrollments se2
                WHERE se2.prospect_id = p.id
                  AND se2.smartlead_campaign_id = :campaign_id
            )
        """
        params["campaign_id"] = campaign_id

    if intro == "has":
        base_query += " AND p.personalized_intro IS NOT NULL"
    elif intro == "missing":
        base_query += " AND p.personalized_intro IS NULL"

    count_sql = f"SELECT COUNT(*) FROM ({base_query}) AS sub"
    total = db.execute(text(count_sql), params).scalar() or 0

    data_query = base_query + " ORDER BY p.created_at DESC LIMIT :limit OFFSET :offset"
    params["limit"] = PAGE_SIZE
    params["offset"] = offset
    rows = db.execute(text(data_query), params).mappings().all()

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    campaigns = []
    try:
        campaigns = smartlead.list_campaigns()
    except Exception:
        pass

    intro_missing_count = db.execute(
        text("SELECT COUNT(*) FROM prospects WHERE personalized_intro IS NULL")
    ).scalar() or 0

    validation_counts = dict(
        db.execute(
            text("""
                SELECT
                  COALESCE(email_validation_status, 'none') AS status,
                  COUNT(*) AS cnt
                FROM prospects
                GROUP BY COALESCE(email_validation_status, 'none')
            """)
        ).fetchall()
    )
    unknown_email_count = validation_counts.get("unknown", 0)
    total_prospects_count = sum(validation_counts.values())

    return templates.TemplateResponse(
        "dashboard/prospects.html",
        {
            "request": request,
            "prospects": rows,
            "search": search or "",
            "status_filter": status or "",
            "investor_type_filter": investor_type or "",
            "wealth_tier_filter": wealth_tier or "",
            "enrolled_filter": enrolled or "",
            "intro_filter": intro or "",
            "page": page,
            "total": total,
            "total_pages": total_pages,
            "campaigns": campaigns,
            "selected_campaign_id": campaign_id or "",
            "intro_missing_count": intro_missing_count,
            "unknown_email_count": unknown_email_count,
            "validation_counts": validation_counts,
            "total_prospects_count": total_prospects_count,
            "active_page": "prospects",
        },
    )


# ---------------------------------------------------------------------------
# Batch generate personalized intros
# ---------------------------------------------------------------------------

@router.post("/prospects/batch-generate-intro", response_class=HTMLResponse)
def batch_generate_intro(
    request: Request,
    prospect_ids: Optional[list[str]] = Form(None),
    select_all: str = Form("0"),
    db: Session = Depends(get_db),
):
    if select_all == "1":
        prospects = db.query(Prospect).limit(100).all()
    elif prospect_ids:
        prospects = db.query(Prospect).filter(Prospect.id.in_(prospect_ids)).all()
    else:
        prospects = db.query(Prospect).filter(Prospect.personalized_intro.is_(None)).limit(100).all()

    generated = 0
    for prospect in prospects:
        had_intro = bool(prospect.personalized_intro)
        _ensure_personalized_intro(prospect, db)
        if not had_intro:
            generated += 1

    db.commit()
    return RedirectResponse(
        f"/dashboard/prospects?batch_intro={generated}",
        status_code=303,
    )


# ---------------------------------------------------------------------------
# Bulk enroll
# ---------------------------------------------------------------------------

@router.post("/prospects/bulk-enroll", response_class=HTMLResponse)
def prospect_bulk_enroll(
    request: Request,
    db: Session = Depends(get_db),
    prospect_ids: Optional[list[str]] = Form(None),
    select_all: str = Form("0"),
    campaign_id: str = Form(...),
    campaign_name: str = Form(""),
    include_catch_all: str = Form("0"),
):
    if not campaign_id:
        return RedirectResponse(url="/dashboard/prospects?bulk_error=missing_fields", status_code=303)

    if select_all == "1":
        prospects = db.query(Prospect).all()
    elif prospect_ids:
        prospects = db.query(Prospect).filter(Prospect.id.in_(prospect_ids)).all()
    else:
        return RedirectResponse(url="/dashboard/prospects?bulk_error=missing_fields", status_code=303)

    failed = []
    allowed_statuses = ("valid", "catch-all") if include_catch_all == "1" else ("valid",)

    # Filter: invalid email status
    enrollable = []
    for prospect in prospects:
        if prospect.email_validation_status not in allowed_statuses:
            logger.warning(
                "Bulk enroll skipped %s — email status: %s",
                prospect.email, prospect.email_validation_status or "not validated",
            )
            failed.append(f"{prospect.email} (email {prospect.email_validation_status or 'not validated'})")
        else:
            enrollable.append(prospect)

    if not enrollable:
        db.commit()
        msg = f"bulk_enrolled=0&bulk_failed={len(failed)}"
        return RedirectResponse(url=f"/dashboard/prospects?{msg}", status_code=303)

    # One paginated fetch of already-enrolled emails from Smartlead (replaces per-prospect API checks)
    try:
        smartlead_emails = smartlead.get_all_campaign_lead_emails(int(campaign_id))
    except Exception as e:
        logger.warning("Could not fetch existing campaign leads from Smartlead: %s", e)
        smartlead_emails = set()

    # One DB query for prospects already active in this campaign
    active_prospect_ids = {
        str(row.prospect_id)
        for row in db.query(SequenceEnrollment.prospect_id)
        .filter(
            SequenceEnrollment.smartlead_campaign_id == str(campaign_id),
            SequenceEnrollment.status == "active",
        )
        .all()
    }

    # Filter duplicates
    to_enroll = []
    for prospect in enrollable:
        if prospect.email.lower() in smartlead_emails:
            logger.info("Bulk enroll skipped %s — already in Smartlead campaign %s", prospect.email, campaign_id)
            continue
        if str(prospect.id) in active_prospect_ids:
            logger.info("Bulk enroll skipped %s — already active in DB for campaign %s", prospect.email, campaign_id)
            continue
        to_enroll.append(prospect)

    if not to_enroll:
        msg = f"bulk_enrolled=0"
        if failed:
            msg += f"&bulk_failed={len(failed)}"
        return RedirectResponse(url=f"/dashboard/prospects?{msg}", status_code=303)

    # Generate missing intros (sequential — Claude needs individual context)
    for prospect in to_enroll:
        _ensure_personalized_intro(prospect, db)

    # Build lead dicts and batch-enroll in one Smartlead call
    lead_dicts = [
        {
            "email": p.email,
            "first_name": p.first_name,
            "last_name": p.last_name,
            "custom_fields": _prospect_custom_fields(p),
        }
        for p in to_enroll
    ]

    enrolled_count = 0
    try:
        smartlead.enroll_prospects_batch(int(campaign_id), lead_dicts)
        # Bulk-insert enrollment records
        for prospect in to_enroll:
            db.add(SequenceEnrollment(
                prospect_id=prospect.id,
                smartlead_campaign_id=str(campaign_id),
                campaign_name=campaign_name or None,
                status="active",
            ))
        enrolled_count = len(to_enroll)
    except Exception as e:
        logger.error("Bulk enroll batch failed for campaign %s: %s", campaign_id, e)
        failed.extend(p.email for p in to_enroll)

    db.commit()
    msg = f"bulk_enrolled={enrolled_count}"
    if failed:
        msg += f"&bulk_failed={len(failed)}"
    return RedirectResponse(url=f"/dashboard/prospects?{msg}", status_code=303)


# ---------------------------------------------------------------------------
# Bulk validate emails
# ---------------------------------------------------------------------------

@router.post("/prospects/bulk-validate-emails", response_class=HTMLResponse)
def bulk_validate_emails(
    request: Request,
    prospect_ids: Optional[list[str]] = Form(None),
    select_all: str = Form("0"),
    db: Session = Depends(get_db),
):
    from app.worker import celery_app as _celery

    if select_all == "1":
        ids = [str(p.id) for p in db.query(Prospect.id).all()]
    elif prospect_ids:
        ids = list(prospect_ids)
    else:
        return RedirectResponse(url="/dashboard/prospects", status_code=303)

    _celery.send_task(
        "app.tasks.email_validation.validate_selected_emails",
        kwargs={"prospect_ids": ids},
    )
    return RedirectResponse(
        url=f"/dashboard/prospects?validate_queued={len(ids)}",
        status_code=303,
    )


# ---------------------------------------------------------------------------
# Revalidate all unknown emails
# ---------------------------------------------------------------------------

@router.post("/prospects/revalidate-unknown", response_class=HTMLResponse)
def revalidate_unknown_emails(request: Request):
    from app.worker import celery_app
    celery_app.send_task("app.tasks.email_validation.revalidate_unknown_emails")
    return RedirectResponse(url="/dashboard/prospects?revalidate_started=1", status_code=303)


# ---------------------------------------------------------------------------
# Add single prospect
# ---------------------------------------------------------------------------



def _prospect_custom_fields(prospect: Prospect) -> dict:
    """Build Smartlead custom field payload from a prospect record.
    Keys match the variable names used in Smartlead email templates."""
    return {k: v for k, v in {
        "company":                prospect.company,
        "title":                  prospect.title,
        "geography":              prospect.geography,
        "asset_class_preference": prospect.asset_class_preference,
        "wealth_tier":            prospect.wealth_tier,
        "investor_type":          prospect.investor_type,
        "linkedin_url":           prospect.linkedin_url,
        "phone":                  prospect.phone,
        "personalized_intro":     prospect.personalized_intro,
    }.items() if v}


def _personalized_intro_fallback(prospect: Prospect) -> str:
    """Generic opener used when Claude generation is unavailable or fails."""
    if prospect.investor_type in ("family_office", "RIA", "broker_dealer"):
        opener = "I wanted to reach out directly given your work in the wealth management space."
    elif prospect.asset_class_preference == "RE":
        opener = "I wanted to reach out given your interest in real estate as an asset class."
    elif prospect.asset_class_preference == "PE":
        opener = "I wanted to reach out given your interest in private equity."
    elif prospect.geography:
        opener = f"I wanted to reach out to a fellow investor in the {prospect.geography} market."
    else:
        opener = "I wanted to reach out directly about a private markets opportunity."
    return opener


def _ensure_personalized_intro(prospect: Prospect, db: Session) -> None:
    """Generate and save a personalized intro if the prospect doesn't have one."""
    if prospect.personalized_intro:
        return
    if settings.anthropic_api_key:
        try:
            from app.integrations.claude_ai import generate_personalized_intro
            prospect.personalized_intro = generate_personalized_intro(prospect)
            db.flush()
            return
        except Exception as e:
            logger.warning("Could not generate personalized intro for %s: %s", prospect.email, e)
    # Fall back to a rule-based opener so the Smartlead variable is never blank
    prospect.personalized_intro = _personalized_intro_fallback(prospect)
    db.flush()


@router.get("/prospects/new", response_class=HTMLResponse)
def prospect_new_form(request: Request):
    campaigns = []
    campaigns_error = None
    try:
        campaigns = smartlead.list_campaigns()
    except Exception as e:
        campaigns_error = str(e)
        logger.warning("Could not fetch Smartlead campaigns: %s", e)

    return templates.TemplateResponse(
        "dashboard/prospect_new.html",
        {
            "request": request,
            "active_page": "prospects",

            "campaigns": campaigns,
            "campaigns_error": campaigns_error,
            "error": None,
            "form": {},
        },
    )


@router.post("/prospects/new", response_class=HTMLResponse)
def prospect_new_submit(
    request: Request,
    db: Session = Depends(get_db),
    first_name: Optional[str] = Form(None),
    last_name: Optional[str] = Form(None),
    email: str = Form(...),
    company: Optional[str] = Form(None),
    title: Optional[str] = Form(None),
    phone: Optional[str] = Form(None),
    linkedin_url: Optional[str] = Form(None),
    asset_class_preference: Optional[str] = Form(None),
    geography: Optional[str] = Form(None),
    wealth_tier: Optional[str] = Form(None),
    investor_type: Optional[str] = Form(None),
    source: Optional[str] = Form("manual"),
    campaign_id: Optional[str] = Form(None),
    campaign_name: Optional[str] = Form(None),
    high_intent_campaign_id: Optional[str] = Form(None),
):
    form_data = {
        "first_name": first_name, "last_name": last_name, "email": email,
        "company": company, "title": title, "phone": phone,
        "linkedin_url": linkedin_url, "asset_class_preference": asset_class_preference,
        "geography": geography, "wealth_tier": wealth_tier,
        "investor_type": investor_type, "source": source or "manual",
        "campaign_id": campaign_id, "campaign_name": campaign_name,
        "high_intent_campaign_id": high_intent_campaign_id,
    }

    campaigns = []
    try:
        campaigns = smartlead.list_campaigns()
    except Exception:
        pass

    def render_error(msg):
        return templates.TemplateResponse(
            "dashboard/prospect_new.html",
            {
                "request": request,
                "active_page": "prospects",
    
                "campaigns": campaigns,
                "campaigns_error": None,
                "error": msg,
                "form": form_data,
            },
            status_code=422,
        )

    email = (email or "").strip().lower()
    if not email:
        return render_error("Email is required.")

    asset_class = (asset_class_preference or "").strip() or None
    if asset_class and asset_class not in ("PE", "RE", "both"):
        asset_class = None

    wt = (wealth_tier or "").strip() or None
    it = (investor_type or "").strip() or None

    prospect = Prospect(
        first_name=(first_name or "").strip() or None,
        last_name=(last_name or "").strip() or None,
        email=email,
        company=(company or "").strip() or None,
        title=(title or "").strip() or None,
        phone=(phone or "").strip() or None,
        linkedin_url=(linkedin_url or "").strip() or None,
        asset_class_preference=asset_class,
        geography=(geography or "").strip() or None,
        wealth_tier=wt,
        investor_type=it,
        source=(source or "manual").strip(),
    )
    db.add(prospect)
    try:
        db.commit()
        db.refresh(prospect)
    except IntegrityError:
        db.rollback()
        return render_error(f"A prospect with email {email} already exists.")

    # Validate email immediately via Bouncer (single-email call)
    try:
        results = bouncer.validate_batch([email])
        validation_status = results.get(email)
        if validation_status:
            prospect.email_validation_status = validation_status
            prospect.email_validated_at = datetime.now(timezone.utc)
            db.commit()
    except Exception as e:
        logger.warning("Bouncer validation failed for %s: %s", email, e)

    # Optional enrollment
    if campaign_id and campaign_id.strip():
        if prospect.email_validation_status not in ("valid", "catch-all"):
            status_label = prospect.email_validation_status or "not validated"
            return render_error(
                f"Prospect added, but cannot enroll — email validated as '{status_label}'. "
                "Only valid and catch-all emails can be enrolled."
            )
        try:
            _ensure_personalized_intro(prospect, db)
            smartlead.enroll_prospect(
                campaign_id=int(campaign_id),
                email=prospect.email,
                first_name=prospect.first_name,
                last_name=prospect.last_name,
                custom_fields=_prospect_custom_fields(prospect),
            )
            enrollment = SequenceEnrollment(
                prospect_id=prospect.id,
                smartlead_campaign_id=str(campaign_id),
                campaign_name=(campaign_name or "").strip() or None,
                high_intent_campaign_id=str(high_intent_campaign_id) if high_intent_campaign_id else None,
            )
            db.add(enrollment)
            db.commit()
        except Exception as e:
            logger.error("Enrollment failed for %s: %s", email, e)
            return render_error(f"Prospect created but enrollment failed: {e}")

    return RedirectResponse(
        url=f"/dashboard/prospects/{prospect.id}?created=1",
        status_code=303,
    )


# ---------------------------------------------------------------------------
# HubSpot list import
# ---------------------------------------------------------------------------

@router.get("/prospects/import/hubspot", response_class=HTMLResponse)
def hubspot_import_form(request: Request):
    from app.integrations import hubspot as hubspot_client
    lists = []
    error = None
    try:
        lists = hubspot_client.get_lists()
    except Exception as exc:
        error = f"Could not fetch HubSpot lists: {exc}"
    return templates.TemplateResponse(
        "dashboard/prospect_import.html",
        {"request": request, "active_page": "prospects", "hubspot_lists": lists, "error": error},
    )


@router.post("/prospects/import/hubspot", response_class=HTMLResponse)
async def hubspot_import_preview(
    request: Request,
    db: Session = Depends(get_db),
):
    import base64
    import json as _json2
    from app.integrations import hubspot as hubspot_client

    form = await request.form()
    list_id   = (form.get("list_id") or "").strip()
    list_name = (form.get("list_name") or "").strip()

    if not list_id:
        return RedirectResponse(url="/dashboard/prospects/import/hubspot", status_code=303)

    try:
        contacts = hubspot_client.get_list_contacts(list_id)
    except Exception as exc:
        return templates.TemplateResponse(
            "dashboard/prospect_import.html",
            {"request": request, "active_page": "prospects",
             "error": f"Failed to fetch list contacts: {exc}", "hubspot_lists": []},
        )

    if not contacts:
        return templates.TemplateResponse(
            "dashboard/prospect_import.html",
            {"request": request, "active_page": "prospects",
             "error": "No contacts with email addresses found in this list.",
             "hubspot_lists": []},
        )

    # Dedup against existing prospects
    emails = [c["email"] for c in contacts]
    existing_emails = {
        row[0]
        for row in db.query(Prospect.email).filter(Prospect.email.in_(emails)).all()
    }
    new_contacts = [c for c in contacts if c["email"] not in existing_emails]
    duplicate_count = len(contacts) - len(new_contacts)

    validated: dict[str, str] = {}
    if new_contacts and settings.bouncer_api_key:
        new_emails = [c["email"] for c in new_contacts]
        try:
            from app.integrations.bouncer import validate_all
            validated = validate_all(new_emails)
        except Exception as exc:
            logger.warning("Bouncer validation failed during HubSpot import: %s", exc)

    valid_count = catchall_count = unknown_count = invalid_count = 0
    for c in new_contacts:
        status = validated.get(c["email"], "unknown")
        c["email_validation_status"] = status
        if status == "valid":
            valid_count += 1
        elif status == "catch-all":
            catchall_count += 1
        elif status == "invalid":
            invalid_count += 1
        else:
            unknown_count += 1

    contacts_b64 = base64.b64encode(_json2.dumps(new_contacts).encode()).decode()

    return templates.TemplateResponse(
        "dashboard/prospect_import.html",
        {
            "request": request,
            "active_page": "prospects",
            "hubspot_preview": {
                "list_id": list_id,
                "list_name": list_name,
                "total_fetched": len(contacts),
                "duplicate_count": duplicate_count,
                "new_count": len(new_contacts),
                "valid_count": valid_count,
                "catchall_count": catchall_count,
                "unknown_count": unknown_count,
                "invalid_count": invalid_count,
                "contacts_b64": contacts_b64,
                "sample": new_contacts[:8],
            },
        },
    )


@router.post("/prospects/import/hubspot/confirm", response_class=HTMLResponse)
async def hubspot_import_confirm(
    request: Request,
    db: Session = Depends(get_db),
):
    import base64
    import json as _json2

    form = await request.form()
    contacts_b64    = (form.get("contacts_b64") or "").strip()
    include_catchall = form.get("include_catchall") == "1"
    include_unknown  = form.get("include_unknown") == "1"

    if not contacts_b64:
        return RedirectResponse(url="/dashboard/prospects/import/hubspot", status_code=303)

    contacts = _json2.loads(base64.b64decode(contacts_b64).decode())
    now_utc = datetime.now(timezone.utc)

    imported = skipped = 0
    for c in contacts:
        status = c.get("email_validation_status", "unknown")
        if status == "invalid":
            skipped += 1
            continue
        if status == "catch-all" and not include_catchall:
            skipped += 1
            continue
        if status == "unknown" and not include_unknown:
            skipped += 1
            continue

        values = dict(
            id=uuid.uuid4(),
            email=c["email"],
            first_name=c.get("first_name"),
            last_name=c.get("last_name"),
            company=c.get("company"),
            title=c.get("title"),
            phone=c.get("phone"),
            source="hubspot",
            email_validation_status=status,
            email_validated_at=now_utc,
        )
        stmt = (
            pg_insert(Prospect)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["email"])
            .returning(Prospect.id)
        )
        if db.execute(stmt).fetchone() is None:
            skipped += 1
        else:
            imported += 1

    db.commit()
    return templates.TemplateResponse(
        "dashboard/prospect_import.html",
        {
            "request": request,
            "active_page": "prospects",
            "result": {"imported": imported, "skipped": skipped, "errors": []},
        },
    )


# ---------------------------------------------------------------------------
# CSV import
# ---------------------------------------------------------------------------

# DB fields exposed in the column mapper, in display order.
# Each entry: (db_field, label, required, hint)
CSV_IMPORT_FIELDS = [
    ("email",                  "Email",               True,  ""),
    ("first_name",             "First Name",          False, ""),
    ("last_name",              "Last Name",           False, ""),
    ("company",                "Company",             False, ""),
    ("title",                  "Job Title",           False, ""),
    ("phone",                  "Phone",               False, ""),
    ("linkedin_url",           "LinkedIn URL",        False, ""),
    ("geography",              "Geography",           False, "e.g. Southeast US"),
    ("asset_class_preference", "Asset Class",         False, "PE, RE, or both"),
    ("wealth_tier",            "Wealth Tier",         False, "mass_affluent / HNWI / UHNWI / institutional"),
    ("investor_type",          "Investor Type",       False, "individual / family_office / RIA / broker_dealer / endowment / pension / other"),
    ("net_worth_estimate",     "Net Worth Estimate",  False, ""),
    ("source",                 "Source",              False, "Defaults to 'manual' if blank"),
]

# Common column name variants for auto-suggest
_FIELD_ALIASES: dict[str, list[str]] = {
    "email":                  ["email", "email_address", "e_mail", "mail"],
    "first_name":             ["first_name", "first", "firstname", "fname", "given_name"],
    "last_name":              ["last_name", "last", "lastname", "lname", "surname", "family_name"],
    "company":                ["company", "company_name", "organization", "org", "employer", "firm"],
    "title":                  ["title", "job_title", "position", "role"],
    "phone":                  ["phone", "phone_number", "mobile", "cell", "telephone", "tel"],
    "linkedin_url":           ["linkedin_url", "linkedin", "linkedin_profile", "li_url"],
    "geography":              ["geography", "location", "region", "city", "state", "area"],
    "asset_class_preference": ["asset_class_preference", "asset_class", "asset", "preference"],
    "wealth_tier":            ["wealth_tier", "tier", "wealth"],
    "investor_type":          ["investor_type", "investor", "type", "category"],
    "net_worth_estimate":     ["net_worth_estimate", "net_worth", "networth"],
    "source":                 ["source", "lead_source"],
}


def _normalize(s: str) -> str:
    return s.lower().strip().replace(" ", "_").replace("-", "_")


def _auto_suggest(csv_columns: list[str]) -> dict[str, str]:
    """Return {db_field: matching_csv_column} for confident auto-matches."""
    norm_map = {_normalize(col): col for col in csv_columns}
    suggestions: dict[str, str] = {}
    for field, aliases in _FIELD_ALIASES.items():
        for alias in aliases:
            if _normalize(alias) in norm_map:
                suggestions[field] = norm_map[_normalize(alias)]
                break
    return suggestions


@router.get("/prospects/import", response_class=HTMLResponse)
def prospect_import_form(request: Request):
    return templates.TemplateResponse(
        "dashboard/prospect_import.html",
        {"request": request, "active_page": "prospects", "db_fields": CSV_IMPORT_FIELDS},
    )


@router.post("/prospects/import", response_class=HTMLResponse)
async def prospect_import_upload(
    request: Request,
    file: UploadFile = File(...),
):
    import base64

    def _import_error(msg: str):
        return templates.TemplateResponse(
            "dashboard/prospect_import.html",
            {"request": request, "active_page": "prospects",
             "error": msg, "db_fields": CSV_IMPORT_FIELDS},
        )

    if not file.filename or not file.filename.endswith(".csv"):
        return _import_error("Please upload a .csv file.")

    max_size = 10 * 1024 * 1024
    content = await file.read(max_size + 1)
    if len(content) > max_size:
        return _import_error("File too large — 10 MB maximum.")

    text_content = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text_content))
    csv_columns = list(reader.fieldnames or [])

    if not csv_columns:
        return _import_error("CSV has no column headers.")

    # Read preview rows and count total
    preview_rows: list[dict] = []
    row_count = 0
    for row in reader:
        row_count += 1
        if len(preview_rows) < 3:
            preview_rows.append(dict(row))

    # Build per-column sample values for hints in the mapper
    col_samples: dict[str, list[str]] = {col: [] for col in csv_columns}
    for row in preview_rows:
        for col in csv_columns:
            val = (row.get(col) or "").strip()
            if val and val not in col_samples[col]:
                col_samples[col].append(val)

    return templates.TemplateResponse(
        "dashboard/prospect_import.html",
        {
            "request": request,
            "active_page": "prospects",
            "mapper": {
                "csv_columns": csv_columns,
                "preview_rows": preview_rows,
                "col_samples": col_samples,
                "suggestions": _auto_suggest(csv_columns),
                "csv_b64": base64.b64encode(content).decode("ascii"),
                "filename": file.filename,
                "row_count": row_count,
            },
            "db_fields": CSV_IMPORT_FIELDS,
        },
    )


@router.post("/prospects/import/confirm", response_class=HTMLResponse)
async def prospect_import_confirm(
    request: Request,
    db: Session = Depends(get_db),
):
    import base64

    form = await request.form()
    csv_b64 = (form.get("csv_b64") or "")
    if not csv_b64:
        return RedirectResponse(url="/dashboard/prospects/import", status_code=303)

    # Build field→column mapping from form
    mapping: dict[str, Optional[str]] = {}
    for field, _, _, _ in CSV_IMPORT_FIELDS:
        val = (form.get(f"map_{field}") or "").strip()
        mapping[field] = val if val else None

    if not mapping.get("email"):
        content_bytes = base64.b64decode(csv_b64)
        text_content = content_bytes.decode("utf-8-sig")
        reader_tmp = csv.DictReader(io.StringIO(text_content))
        csv_columns = list(reader_tmp.fieldnames or [])
        preview_rows: list[dict] = []
        row_count = 0
        for row in reader_tmp:
            row_count += 1
            if len(preview_rows) < 3:
                preview_rows.append(dict(row))
        col_samples: dict[str, list[str]] = {col: [] for col in csv_columns}
        for row in preview_rows:
            for col in csv_columns:
                val = (row.get(col) or "").strip()
                if val and val not in col_samples[col]:
                    col_samples[col].append(val)
        current_mapping = {f: mapping.get(f) or "" for f, *_ in CSV_IMPORT_FIELDS}
        return templates.TemplateResponse(
            "dashboard/prospect_import.html",
            {
                "request": request,
                "active_page": "prospects",
                "error": "You must map the Email column before importing.",
                "mapper": {
                    "csv_columns": csv_columns,
                    "col_samples": col_samples,
                    "suggestions": current_mapping,
                    "csv_b64": csv_b64,
                    "filename": "",
                    "row_count": row_count,
                },
                "db_fields": CSV_IMPORT_FIELDS,
            },
        )

    content_bytes = base64.b64decode(csv_b64)
    text_content = content_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text_content))

    VALID_ASSET_CLASSES = {"PE", "RE", "both"}
    VALID_WEALTH_TIERS = {"mass_affluent", "HNWI", "UHNWI", "institutional"}
    VALID_INVESTOR_TYPES = {"individual", "family_office", "RIA", "broker_dealer", "endowment", "pension", "other"}

    imported, skipped, errors = 0, 0, []

    for row_num, row in enumerate(reader, start=2):
        email_col = mapping["email"]
        email = (row.get(email_col) or "").strip().lower()
        if not email:
            errors.append(f"Row {row_num}: missing email — skipped")
            skipped += 1
            continue

        def _get(field: str) -> Optional[str]:
            col = mapping.get(field)
            if not col:
                return None
            return (row.get(col) or "").strip() or None

        asset_class = _get("asset_class_preference")
        if asset_class and asset_class not in VALID_ASSET_CLASSES:
            errors.append(f"Row {row_num}: invalid asset_class '{asset_class}' — set to null")
            asset_class = None

        wealth_tier = _get("wealth_tier")
        if wealth_tier and wealth_tier not in VALID_WEALTH_TIERS:
            errors.append(f"Row {row_num}: invalid wealth_tier '{wealth_tier}' — set to null")
            wealth_tier = None

        investor_type = _get("investor_type")
        if investor_type and investor_type not in VALID_INVESTOR_TYPES:
            errors.append(f"Row {row_num}: invalid investor_type '{investor_type}' — set to null")
            investor_type = None

        values = dict(
            id=uuid.uuid4(),
            email=email,
            first_name=_get("first_name"),
            last_name=_get("last_name"),
            company=_get("company"),
            title=_get("title"),
            linkedin_url=_get("linkedin_url"),
            phone=_get("phone"),
            asset_class_preference=asset_class,
            geography=_get("geography"),
            net_worth_estimate=_get("net_worth_estimate"),
            wealth_tier=wealth_tier,
            investor_type=investor_type,
            source=_get("source") or "manual",
        )
        stmt = (
            pg_insert(Prospect)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["email"])
            .returning(Prospect.id)
        )
        row_result = db.execute(stmt)
        if row_result.fetchone() is None:
            errors.append(f"Row {row_num}: {email} already exists — skipped")
            skipped += 1
        else:
            imported += 1

    db.commit()
    return templates.TemplateResponse(
        "dashboard/prospect_import.html",
        {
            "request": request,
            "active_page": "prospects",
            "result": {"imported": imported, "skipped": skipped, "errors": errors},
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

    # HubSpot status for this prospect
    has_deal = db.execute(text(
        "SELECT 1 FROM email_events WHERE prospect_id = :pid AND event_type = 'reply' AND hubspot_synced_at IS NOT NULL LIMIT 1"
    ), {"pid": str(prospect.id)}).fetchone()
    has_contact = db.execute(text(
        "SELECT 1 FROM email_events WHERE prospect_id = :pid AND hubspot_synced_at IS NOT NULL LIMIT 1"
    ), {"pid": str(prospect.id)}).fetchone()
    hubspot_status = "deal" if has_deal else ("contact" if has_contact else None)

    return templates.TemplateResponse(
        "dashboard/prospect_detail.html",
        {
            "request": request,
            "prospect": prospect,
            "enrollments": enrollments,
            "events_by_enrollment": events_by_enrollment,
            "hubspot_status": hubspot_status,
            "active_page": "prospects",
        },
    )


# ---------------------------------------------------------------------------
# Bulk delete prospects
# ---------------------------------------------------------------------------

@router.post("/prospects/bulk-delete", response_class=HTMLResponse)
def bulk_delete_prospects(
    request: Request,
    prospect_ids: Optional[list[str]] = Form(None),
    select_all: str = Form("0"),
    search: Optional[str] = Form(None),
    status: Optional[str] = Form(None),
    investor_type: Optional[str] = Form(None),
    wealth_tier: Optional[str] = Form(None),
    enrolled: Optional[str] = Form(None),
    email_validation: Optional[str] = Form(None),
    campaign_id: Optional[str] = Form(None),
    intro: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    BULK_DELETE_LIMIT = 1000

    if select_all == "1":
        has_filter = any([search, status, investor_type, wealth_tier, enrolled, email_validation, campaign_id, intro])
        if not has_filter:
            return RedirectResponse(url="/dashboard/prospects?delete_error=nofilter", status_code=303)

        q = db.query(Prospect)
        if search:
            q = q.filter(
                Prospect.email.ilike(f"%{search}%")
                | Prospect.first_name.ilike(f"%{search}%")
                | Prospect.last_name.ilike(f"%{search}%")
                | Prospect.company.ilike(f"%{search}%")
            )
        if status:
            sub = db.query(SequenceEnrollment.prospect_id).filter(
                SequenceEnrollment.status == status
            ).subquery()
            q = q.filter(Prospect.id.in_(sub))
        if investor_type:
            q = q.filter(Prospect.investor_type == investor_type)
        if wealth_tier:
            q = q.filter(Prospect.wealth_tier == wealth_tier)
        if enrolled == "yes":
            sub = db.query(SequenceEnrollment.prospect_id).subquery()
            q = q.filter(Prospect.id.in_(sub))
        elif enrolled == "no":
            sub = db.query(SequenceEnrollment.prospect_id).subquery()
            q = q.filter(Prospect.id.notin_(sub))
        if email_validation == "none":
            q = q.filter(Prospect.email_validation_status.is_(None))
        elif email_validation:
            q = q.filter(Prospect.email_validation_status == email_validation)
        if campaign_id:
            sub = db.query(SequenceEnrollment.prospect_id).filter(
                SequenceEnrollment.smartlead_campaign_id == campaign_id
            ).subquery()
            q = q.filter(Prospect.id.in_(sub))
        if intro == "has":
            q = q.filter(Prospect.personalized_intro.isnot(None))
        elif intro == "missing":
            q = q.filter(Prospect.personalized_intro.is_(None))
        prospects = q.limit(BULK_DELETE_LIMIT + 1).all()
        if len(prospects) > BULK_DELETE_LIMIT:
            count = db.query(Prospect).count()
            return RedirectResponse(
                url=f"/dashboard/prospects?delete_error=toomany&count={len(prospects)}",
                status_code=303,
            )
    elif prospect_ids:
        if len(prospect_ids) > BULK_DELETE_LIMIT:
            return RedirectResponse(
                url=f"/dashboard/prospects?delete_error=toomany&count={len(prospect_ids)}",
                status_code=303,
            )
        prospects = db.query(Prospect).filter(Prospect.id.in_(prospect_ids)).all()
    else:
        return RedirectResponse(url="/dashboard/prospects", status_code=303)

    deleted = 0
    for prospect in prospects:
        db.query(EmailEvent).filter(EmailEvent.prospect_id == prospect.id).delete()
        db.query(SequenceEnrollment).filter(SequenceEnrollment.prospect_id == prospect.id).delete()
        db.delete(prospect)
        deleted += 1

    db.commit()
    return RedirectResponse(
        url=f"/dashboard/prospects?bulk_deleted={deleted}",
        status_code=303,
    )


# ---------------------------------------------------------------------------
# Delete prospect
# ---------------------------------------------------------------------------

@router.post("/prospects/{prospect_id}/delete", response_class=HTMLResponse)
def prospect_delete(prospect_id: str, db: Session = Depends(get_db)):
    prospect = db.query(Prospect).filter(Prospect.id == prospect_id).first()
    if not prospect:
        return HTMLResponse("<h1>Prospect not found</h1>", status_code=404)
    db.query(EmailEvent).filter(EmailEvent.prospect_id == prospect.id).delete()
    db.query(SequenceEnrollment).filter(SequenceEnrollment.prospect_id == prospect.id).delete()
    db.delete(prospect)
    db.commit()
    return RedirectResponse(url="/dashboard/prospects", status_code=303)


# ---------------------------------------------------------------------------
# Generate personalized intro
# ---------------------------------------------------------------------------

@router.post("/prospects/{prospect_id}/generate-intro", response_class=HTMLResponse)
def generate_intro(prospect_id: str, request: Request, db: Session = Depends(get_db)):
    prospect = db.query(Prospect).filter(Prospect.id == prospect_id).first()
    if not prospect:
        return HTMLResponse("<p class='text-red-500 text-xs'>Prospect not found.</p>", status_code=404)
    if not settings.anthropic_api_key:
        return HTMLResponse("<p class='text-red-500 text-xs'>ANTHROPIC_API_KEY not configured.</p>", status_code=400)
    try:
        from app.integrations.claude_ai import generate_personalized_intro
        prospect.personalized_intro = generate_personalized_intro(prospect)
        db.commit()
    except Exception as e:
        logger.error("generate_intro failed for %s: %s", prospect_id, e)
        return HTMLResponse(f"<p class='text-red-500 text-xs'>Generation failed: {e}</p>", status_code=500)

    return HTMLResponse(f"""
<div id="intro-block">
  <p id="intro-text" class="text-sm text-gray-700 leading-relaxed">{prospect.personalized_intro}</p>
  <div class="mt-2 flex items-center gap-3">
    <button hx-post="/dashboard/prospects/{prospect_id}/generate-intro"
            hx-target="#intro-block" hx-swap="outerHTML"
            class="text-xs text-indigo-600 hover:text-indigo-800 font-medium">
      Regenerate
    </button>
  </div>
</div>
""")


# Edit prospect
# ---------------------------------------------------------------------------

@router.get("/prospects/{prospect_id}/edit", response_class=HTMLResponse)
def prospect_edit_form(prospect_id: str, request: Request, db: Session = Depends(get_db), enrich_msg: Optional[str] = None):
    prospect = db.query(Prospect).filter(Prospect.id == prospect_id).first()
    if not prospect:
        return HTMLResponse("<h1>Prospect not found</h1>", status_code=404)
    campaigns = []
    campaigns_error = None
    try:
        campaigns = smartlead.list_campaigns()
    except Exception as e:
        campaigns_error = str(e)
        logger.warning("Could not fetch Smartlead campaigns: %s", e)
    return templates.TemplateResponse(
        "dashboard/prospect_edit.html",
        {
            "request": request,
            "prospect": prospect,
            "active_page": "prospects",
            "error": None,
            "enrich_msg": enrich_msg,
            "campaigns": campaigns,
            "campaigns_error": campaigns_error,

        },
    )


@router.post("/prospects/{prospect_id}/edit", response_class=HTMLResponse)
def prospect_edit_submit(
    prospect_id: str,
    request: Request,
    db: Session = Depends(get_db),
    first_name: Optional[str] = Form(None),
    last_name: Optional[str] = Form(None),
    email: str = Form(...),
    company: Optional[str] = Form(None),
    title: Optional[str] = Form(None),
    phone: Optional[str] = Form(None),
    linkedin_url: Optional[str] = Form(None),
    geography: Optional[str] = Form(None),
    asset_class_preference: Optional[str] = Form(None),
    wealth_tier: Optional[str] = Form(None),
    investor_type: Optional[str] = Form(None),
    net_worth_estimate: Optional[str] = Form(None),
    source: Optional[str] = Form(None),
    accredited_status: Optional[str] = Form(None),
    campaign_id: Optional[str] = Form(None),
    campaign_name: Optional[str] = Form(None),
    high_intent_campaign_id: Optional[str] = Form(None),
):
    prospect = db.query(Prospect).filter(Prospect.id == prospect_id).first()
    if not prospect:
        return HTMLResponse("<h1>Prospect not found</h1>", status_code=404)

    campaigns = []
    try:
        campaigns = smartlead.list_campaigns()
    except Exception:
        pass

    def render_error(msg):
        return templates.TemplateResponse(
            "dashboard/prospect_edit.html",
            {
                "request": request,
                "prospect": prospect,
                "active_page": "prospects",
                "error": msg,
                "campaigns": campaigns,
                "campaigns_error": None,
    
            },
            status_code=422,
        )

    # Check email uniqueness if changed
    email = email.strip().lower()
    if email != prospect.email:
        existing = db.query(Prospect).filter(Prospect.email == email).first()
        if existing:
            return render_error(f"Email {email} already belongs to another prospect.")

    prospect.first_name = first_name.strip() or None if first_name else None
    prospect.last_name = last_name.strip() or None if last_name else None
    prospect.email = email
    prospect.company = company.strip() or None if company else None
    prospect.title = title.strip() or None if title else None
    prospect.phone = phone.strip() or None if phone else None
    prospect.linkedin_url = linkedin_url.strip() or None if linkedin_url else None
    prospect.geography = geography.strip() or None if geography else None
    prospect.asset_class_preference = asset_class_preference or None
    prospect.wealth_tier = wealth_tier or None
    prospect.investor_type = investor_type or None
    prospect.net_worth_estimate = net_worth_estimate.strip() or None if net_worth_estimate else None
    prospect.source = source or None
    prospect.accredited_status = accredited_status or "unverified"

    db.commit()
    db.refresh(prospect)

    # Optional enrollment
    if campaign_id and campaign_id.strip():
        # Check for an existing active enrollment in this campaign
        existing_enrollment = (
            db.query(SequenceEnrollment)
            .filter(
                SequenceEnrollment.prospect_id == prospect.id,
                SequenceEnrollment.smartlead_campaign_id == str(campaign_id),
                SequenceEnrollment.status == "active",
            )
            .first()
        )
        if existing_enrollment:
            return render_error("Prospect is already actively enrolled in that campaign.")

        # Validate email if not already validated
        if prospect.email_validation_status not in ("valid", "catch-all"):
            try:
                results = bouncer.validate_batch([prospect.email])
                validation_status = results.get(prospect.email)
                if validation_status:
                    prospect.email_validation_status = validation_status
                    prospect.email_validated_at = datetime.now(timezone.utc)
                    db.commit()
                    db.refresh(prospect)
            except Exception as e:
                logger.warning("Bouncer validation failed for %s: %s", prospect.email, e)

        if prospect.email_validation_status not in ("valid", "catch-all"):
            status_label = prospect.email_validation_status or "not validated"
            return render_error(
                f"Cannot enroll — email validated as '{status_label}'. "
                "Only valid and catch-all emails can be enrolled."
            )

        try:
            _ensure_personalized_intro(prospect, db)
            smartlead.enroll_prospect(
                campaign_id=int(campaign_id),
                email=prospect.email,
                first_name=prospect.first_name,
                last_name=prospect.last_name,
                custom_fields=_prospect_custom_fields(prospect),
            )
            enrollment = SequenceEnrollment(
                prospect_id=prospect.id,
                smartlead_campaign_id=str(campaign_id),
                campaign_name=(campaign_name or "").strip() or None,
                high_intent_campaign_id=str(high_intent_campaign_id) if high_intent_campaign_id else None,
            )
            db.add(enrollment)
            db.commit()
        except Exception as e:
            logger.error("Enrollment failed for %s: %s", prospect.email, e)
            return render_error(f"Changes saved but enrollment failed: {e}")

    return RedirectResponse(url=f"/dashboard/prospects/{prospect_id}", status_code=303)


# ---------------------------------------------------------------------------
# Sequence performance
# ---------------------------------------------------------------------------

@router.get("/sequences", response_class=HTMLResponse)
def dashboard_sequences(request: Request, db: Session = Depends(get_db)):
    seq = sequence_stats(db=db)
    seq_types = sequences_by_type(db=db)

    return templates.TemplateResponse(
        "dashboard/sequences.html",
        {
            "request": request,
            "sequences": seq,
            "seq_types": seq_types,
            "active_page": "sequences",
        },
    )


@router.get("/sequences/{campaign_id}", response_class=HTMLResponse)
def dashboard_sequence_detail(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    from app.models.campaign_config import CampaignConfig

    email_steps = sequence_email_stats(db=db, campaign_id=campaign_id)
    stats = overview_stats(db=db, campaign_id=campaign_id)
    campaign_name = email_steps[0].campaign_name if email_steps else campaign_id
    hs_cfg = db.query(CampaignConfig).filter(
        CampaignConfig.smartlead_campaign_id == campaign_id
    ).first()

    return templates.TemplateResponse(
        "dashboard/sequence_detail.html",
        {
            "request": request,
            "campaign_id": campaign_id,
            "campaign_name": campaign_name,
            "email_steps": email_steps,
            "stats": stats,
            "hs_cfg": hs_cfg,
            "active_page": "sequences",
        },
    )


@router.get("/sequences/{campaign_id}/config", response_class=HTMLResponse)
def campaign_config_get(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    from app.integrations.hubspot import get_deal_pipelines
    from app.models.campaign_config import CampaignConfig, TRIGGER_CHOICES

    cfg = db.query(CampaignConfig).filter(
        CampaignConfig.smartlead_campaign_id == campaign_id
    ).first()

    email_steps = sequence_email_stats(db=db, campaign_id=campaign_id)
    campaign_name = email_steps[0].campaign_name if email_steps else campaign_id

    pipelines = []
    pipelines_error = None
    try:
        pipelines = get_deal_pipelines()
    except Exception as e:
        pipelines_error = str(e)
        logger.warning("Could not fetch HubSpot pipelines: %s", e)

    return templates.TemplateResponse(
        "dashboard/campaign_config.html",
        {
            "request": request,
            "campaign_id": campaign_id,
            "campaign_name": campaign_name,
            "cfg": cfg,
            "pipelines": pipelines,
            "pipelines_error": pipelines_error,
            "trigger_choices": TRIGGER_CHOICES,
            "active_page": "sequences",
        },
    )


@router.post("/sequences/{campaign_id}/config")
def campaign_config_post(
    campaign_id: str,
    db: Session = Depends(get_db),
    hubspot_trigger_event: str = Form("reply"),
    hubspot_pipeline_id: str = Form(""),
    hubspot_stage_id: str = Form(""),
):
    from app.models.campaign_config import CampaignConfig, TRIGGER_CHOICES

    if hubspot_trigger_event not in TRIGGER_CHOICES:
        hubspot_trigger_event = "reply"

    cfg = db.query(CampaignConfig).filter(
        CampaignConfig.smartlead_campaign_id == campaign_id
    ).first()

    if cfg is None:
        cfg = CampaignConfig(smartlead_campaign_id=campaign_id)
        db.add(cfg)

    cfg.hubspot_trigger_event = hubspot_trigger_event
    cfg.hubspot_pipeline_id = hubspot_pipeline_id.strip() or None
    cfg.hubspot_stage_id = hubspot_stage_id.strip() or None

    email_steps = sequence_email_stats(db=db, campaign_id=campaign_id)
    if email_steps:
        cfg.campaign_name = email_steps[0].campaign_name

    db.commit()
    return RedirectResponse(url=f"/dashboard/sequences/{campaign_id}", status_code=303)


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

    # Sent today from our webhook event log — use ET midnight so "today" matches the user's timezone
    from zoneinfo import ZoneInfo
    eastern = ZoneInfo("America/New_York")
    today_start_et = datetime.now(eastern).replace(hour=0, minute=0, second=0, microsecond=0)
    sent_today_db = (
        db.query(func.count(EmailEvent.id))
        .filter(EmailEvent.event_type == "sent", EmailEvent.occurred_at >= today_start_et)
        .scalar()
    ) or 0

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
            "sent_today_db": sent_today_db,
            "active_page": "mailboxes",
        },
    )


# ---------------------------------------------------------------------------
# Sync health
# ---------------------------------------------------------------------------

@router.get("/sync", response_class=HTMLResponse)
def dashboard_sync(request: Request, db: Session = Depends(get_db)):
    from datetime import timedelta
    sync = sync_stats(db=db)
    zb_credits = bouncer.get_credits()
    zb_used = db.query(func.count(Prospect.id)).filter(
        Prospect.email_validated_at.is_not(None)
    ).scalar() or 0

    # Actual HubSpot API writes (click + reply only) — distinct from "processed"
    now_utc = datetime.now(timezone.utc)
    hs_writes_1h = db.query(func.count(EmailEvent.id)).filter(
        EmailEvent.event_type.in_(["click", "reply"]),
        EmailEvent.hubspot_synced_at >= now_utc - timedelta(hours=1),
    ).scalar() or 0
    hs_writes_24h = db.query(func.count(EmailEvent.id)).filter(
        EmailEvent.event_type.in_(["click", "reply"]),
        EmailEvent.hubspot_synced_at >= now_utc - timedelta(hours=24),
    ).scalar() or 0

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
            "hs_writes_1h": hs_writes_1h,
            "hs_writes_24h": hs_writes_24h,
            "active_page": "sync",
            "zb_credits": zb_credits,
            "zb_used": zb_used,
        },
    )


# ---------------------------------------------------------------------------
# EDGAR Form D lead finder
# ---------------------------------------------------------------------------

from app.models.saved_search import SavedSearch


@router.get("/edgar", response_class=HTMLResponse)
def edgar_search(
    request: Request,
    db: Session = Depends(get_db),
    keywords: str = Query(""),
    state: str = Query(""),
    start_date: str = Query(""),
    end_date: str = Query(""),
    offset: int = Query(0),
):
    searched = any([keywords, state, start_date, end_date])

    # If no params in URL, restore last search from session
    if not searched and request.session.get("edgar_last_search"):
        last = request.session["edgar_last_search"]
        qs = "&".join(f"{k}={v}" for k, v in last.items() if v)
        if qs:
            return RedirectResponse(url=f"/dashboard/edgar?{qs}", status_code=302)

    rows = []
    total = 0
    error = None

    if searched:
        # Persist search params in session
        request.session["edgar_last_search"] = {
            "keywords": keywords, "state": state,
            "start_date": start_date, "end_date": end_date,
        }
        try:
            filings, total = edgar_client.search_form_d(
                keywords=keywords,
                state=state,
                start_date=start_date,
                end_date=end_date,
                offset=offset,
                size=15,
            )
            rows = edgar_client.enrich_filings(filings)
        except Exception as e:
            logger.error("EDGAR search error: %s", e)
            error = "Failed to reach EDGAR — try again in a moment."

    saved_searches = db.query(SavedSearch).order_by(SavedSearch.created_at.desc()).all()

    return templates.TemplateResponse(
        "dashboard/edgar.html",
        {
            "request": request,
            "active_page": "edgar",
            "rows": rows,
            "total": total,
            "offset": offset,
            "page_size": 15,
            "keywords": keywords,
            "state": state,
            "start_date": start_date,
            "end_date": end_date,
            "us_states": edgar_client.US_STATES,
            "searched": searched,
            "error": error,
            "saved_searches": saved_searches,
        },
    )


@router.post("/edgar/save-search", response_class=HTMLResponse)
def edgar_save_search(
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(""),
    keywords: str = Form(""),
    state: str = Form(""),
    start_date: str = Form(""),
    end_date: str = Form(""),
):
    if name.strip():
        params = _json.dumps({
            "keywords": keywords, "state": state,
            "start_date": start_date, "end_date": end_date,
        })
        db.add(SavedSearch(name=name.strip(), params=params))
        db.commit()
    qs = "&".join(f"{k}={v}" for k, v in [
        ("keywords", keywords), ("state", state),
        ("start_date", start_date), ("end_date", end_date),
    ] if v)
    return RedirectResponse(url=f"/dashboard/edgar?{qs}", status_code=303)


@router.post("/edgar/saved-searches/{search_id}/delete", response_class=HTMLResponse)
def edgar_delete_saved_search(
    search_id: str,
    db: Session = Depends(get_db),
):
    db.query(SavedSearch).filter(SavedSearch.id == search_id).delete()
    db.commit()
    return RedirectResponse(url="/dashboard/edgar", status_code=303)


@router.post("/prospects/{prospect_id}/enrich", response_class=HTMLResponse)
def prospect_enrich(
    prospect_id: str,
    db: Session = Depends(get_db),
    source: str = Form("apollo"),
):
    prospect = db.query(Prospect).filter(Prospect.id == prospect_id).first()
    if not prospect:
        return HTMLResponse("<h1>Prospect not found</h1>", status_code=404)

    first_name = prospect.first_name or ""
    last_name = prospect.last_name or ""
    company = prospect.company or ""

    enriched: dict = {}
    status_msg = "Could not enrich — name and company are required."

    if first_name and last_name and company:
        if source in ("apollo", "both"):
            apollo_result = apollo_client.enrich_person(first_name, last_name, company)
            if apollo_result:
                enriched = apollo_result
                status_msg = "Apollo enrichment complete."
            else:
                status_msg = "Apollo found no results."

        if source in ("hunter", "both") or (source == "apollo" and not enriched.get("email")):
            hunter_result = hunter_client.find_email(first_name, last_name, company)
            if hunter_result and hunter_result.get("email"):
                enriched["email"] = hunter_result["email"]
                status_msg = f"Hunter.io found email (confidence {hunter_result.get('confidence', '?')}%)."
                if source == "apollo":
                    status_msg = "Apollo found no email — Hunter.io fallback used."
            elif source == "hunter":
                status_msg = "Hunter.io found no email."

    # Apply enriched fields only where prospect field is currently blank
    updated_fields = []
    field_map = {
        "email": "email",
        "title": "title",
        "phone": "phone",
        "linkedin_url": "linkedin_url",
        "company": "company",
    }
    for apollo_key, prospect_attr in field_map.items():
        if enriched.get(apollo_key) and not getattr(prospect, prospect_attr):
            setattr(prospect, prospect_attr, enriched[apollo_key])
            updated_fields.append(prospect_attr)

    if updated_fields:
        db.commit()
        status_msg += f" Updated: {', '.join(updated_fields)}."

    return RedirectResponse(
        url=f"/dashboard/prospects/{prospect_id}/edit?enrich_msg={status_msg}",
        status_code=303,
    )


@router.post("/edgar/add-prospect", response_class=HTMLResponse)
def edgar_add_prospect(
    request: Request,
    db: Session = Depends(get_db),
    full_name: str = Form(""),
    title: str = Form(""),
    company: str = Form(""),
    state: str = Form(""),
    biz_location: str = Form(""),
    return_url: str = Form("/dashboard/edgar"),
):
    name_parts = full_name.strip().rsplit(" ", 1)
    first_name = name_parts[0] if len(name_parts) >= 1 else ""
    last_name = name_parts[1] if len(name_parts) == 2 else ""
    geography = biz_location or state or None

    # If already a prospect, skip enrichment and go straight to their page
    existing = db.query(Prospect).filter(
        func.lower(Prospect.first_name) == first_name.lower(),
        func.lower(Prospect.last_name) == last_name.lower(),
        func.lower(Prospect.company) == company.lower(),
    ).first() if first_name and last_name and company else None

    if existing:
        return RedirectResponse(url=f"/dashboard/prospects/{existing.id}?from_edgar=1", status_code=303)

    # Enrich via Apollo then Hunter — but do NOT save yet
    enriched: dict = {}
    email_source = None

    if first_name and last_name and company:
        apollo_result = apollo_client.enrich_person(first_name, last_name, company)
        if apollo_result:
            enriched = apollo_result
            if enriched.get("email"):
                email_source = "Apollo"

    if not email_source and first_name and last_name and company:
        hunter_result = hunter_client.find_email(first_name, last_name, company)
        if hunter_result:
            enriched["email"] = hunter_result["email"]
            email_source = f"Hunter.io (confidence {hunter_result.get('confidence', '?')}%)"

    return templates.TemplateResponse(
        "dashboard/edgar_preview.html",
        {
            "request": request,
            "active_page": "edgar",
            "confirm_url": "/dashboard/edgar/confirm-prospect",
            "first_name": first_name,
            "last_name": last_name,
            "email": enriched.get("email") or "",
            "title": enriched.get("title") or title,
            "company": enriched.get("company") or company,
            "phone": enriched.get("phone") or "",
            "linkedin_url": enriched.get("linkedin_url") or "",
            "geography": geography,
            "email_source": email_source,
            "return_url": return_url,
        },
    )


@router.post("/edgar/confirm-prospect", response_class=HTMLResponse)
def edgar_confirm_prospect(
    db: Session = Depends(get_db),
    first_name: str = Form(""),
    last_name: str = Form(""),
    email: str = Form(""),
    title: str = Form(""),
    company: str = Form(""),
    phone: str = Form(""),
    linkedin_url: str = Form(""),
    geography: str = Form(""),
    return_url: str = Form("/dashboard/edgar"),
):
    if not email or not email.strip():
        email = f"unknown_{uuid.uuid4().hex[:8]}@edgar.placeholder"
        email_status = "unknown"
    else:
        email_status = "unknown"  # Bouncer will validate on next batch run

    prospect = Prospect(
        id=str(uuid.uuid4()),
        email=email.strip(),
        first_name=first_name.strip() or None,
        last_name=last_name.strip() or None,
        company=company.strip() or None,
        title=title.strip() or None,
        phone=phone.strip() or None,
        linkedin_url=linkedin_url.strip() or None,
        geography=geography.strip() or None,
        source="apollo",
        email_validation_status=email_status,
    )
    db.add(prospect)
    db.commit()
    return RedirectResponse(url=f"/dashboard/prospects/{prospect.id}?from_edgar=1", status_code=303)


# ---------------------------------------------------------------------------
# Apollo Lead Finder
# ---------------------------------------------------------------------------

EXECUTIVE_TITLES = [
    "CEO", "Chief Executive Officer",
    "President",
    "Founder", "Co-Founder",
    "Owner",
    "Managing Partner",
    "Managing Director",
    "Chairman",
    "General Partner",
    "Principal",
    "Chief Financial Officer", "CFO",
    "Chief Investment Officer", "CIO",
    "Chief Operating Officer", "COO",
]


COMPANY_SIZE_OPTIONS = [
    ("1,10",    "Boutique (1–10)"),
    ("11,50",   "Small (11–50)"),
    ("51,200",  "Mid-size (51–200)"),
    ("201,1000","Large (201–1,000)"),
]

REVENUE_OPTIONS = [
    ("1000000,10000000",     "$1M–$10M"),
    ("10000000,50000000",    "$10M–$50M"),
    ("50000000,100000000",   "$50M–$100M"),
    ("100000000,500000000",  "$100M–$500M"),
    ("500000000,",           "$500M+"),
]

INDUSTRY_OPTIONS = [
    "Financial Services",
    "Investment Management",
    "Real Estate",
    "Private Equity",
    "Wealth Management",
    "Family Office",
    "Venture Capital",
    "Banking",
]


@router.get("/leads", response_class=HTMLResponse)
def leads_search(
    request: Request,
    keywords: str = Query(""),
    title: str = Query(""),       # from preset buttons
    title_text: str = Query(""),  # from the free-text input
    location: str = Query(""),
    executives: str = Query(""),
    size: list[str] = Query([]),
    revenue: list[str] = Query([]),
    industry: list[str] = Query([]),
    has_email: str = Query(""),
    page: int = Query(1),
    searched: str = Query(""),
):
    # Preset button value takes priority over the free-text input
    effective_title = title if title else title_text

    results = []
    total = 0
    error = None
    is_searched = bool(searched)
    is_executives = bool(executives)
    is_has_email = bool(has_email)

    if is_searched:
        if is_executives:
            titles = EXECUTIVE_TITLES
        elif effective_title.strip():
            titles = [effective_title.strip()]
        else:
            titles = []
        locations = [location.strip()] if location.strip() else []
        try:
            results, total = apollo_client.search_people(
                keywords=keywords.strip(),
                titles=titles or None,
                locations=locations or None,
                employee_ranges=size or None,
                revenue_ranges=revenue or None,
                industries=industry or None,
                has_email=is_has_email,
                page=page,
            )
        except Exception as e:
            logger.error("Apollo people search error: %s", e)
            error = f"Apollo search failed: {e}"

    return templates.TemplateResponse(
        "dashboard/leads.html",
        {
            "request": request,
            "active_page": "leads",
            "results": results,
            "total": total,
            "per_page": apollo_client.PER_PAGE,
            "page": page,
            "keywords": keywords,
            "title": effective_title,
            "location": location,
            "executives": is_executives,
            "selected_sizes": size,
            "selected_revenues": revenue,
            "selected_industries": industry,
            "revenue_options": REVENUE_OPTIONS,
            "has_email": is_has_email,
            "company_size_options": COMPANY_SIZE_OPTIONS,
            "industry_options": INDUSTRY_OPTIONS,
            "searched": is_searched,
            "error": error,
        },
    )


@router.get("/leads/add-prospect", response_class=HTMLResponse)
def leads_add_prospect_get(
    request: Request,
    db: Session = Depends(get_db),
    apollo_id: str = Query(""),
    first_name: str = Query(""),
    title: str = Query(""),
    company: str = Query(""),
    return_url: str = Query("/dashboard/leads"),
):
    return _leads_enrich_and_preview(request, db, apollo_id, first_name, title, company, return_url)


def _leads_enrich_and_preview(request, db, apollo_id, first_name, title, company, return_url):
    """Shared logic for single-contact enrich + preview (used by GET and POST handlers)."""
    enriched: dict = {}
    email_source = None

    if first_name and company:
        apollo_result = apollo_client.enrich_person(first_name, "", company)
        if apollo_result:
            enriched = apollo_result
            if enriched.get("email"):
                email_source = "Apollo"

    if not email_source and first_name and company:
        hunter_result = hunter_client.find_email(first_name, "", company)
        if hunter_result:
            enriched["email"] = hunter_result["email"]
            email_source = f"Hunter.io (confidence {hunter_result.get('confidence', '?')}%)"

    resolved_first = enriched.get("first_name") or first_name
    resolved_last = enriched.get("last_name") or ""

    # If already a prospect, go straight to their page
    existing = db.query(Prospect).filter(
        func.lower(Prospect.first_name) == resolved_first.lower(),
        func.lower(Prospect.last_name) == resolved_last.lower(),
        func.lower(Prospect.company) == company.lower(),
    ).first() if resolved_first and resolved_last and company else None

    if existing:
        return RedirectResponse(url=f"/dashboard/prospects/{existing.id}", status_code=303)

    geography = ", ".join(filter(None, [enriched.get("city"), enriched.get("state")])) or None

    return templates.TemplateResponse(
        "dashboard/edgar_preview.html",
        {
            "request": request,
            "active_page": "leads",
            "confirm_url": "/dashboard/leads/confirm-prospect",
            "first_name": resolved_first,
            "last_name": resolved_last,
            "email": enriched.get("email") or "",
            "title": enriched.get("title") or title,
            "company": enriched.get("company") or company,
            "phone": enriched.get("phone") or "",
            "linkedin_url": enriched.get("linkedin_url") or "",
            "geography": geography,
            "email_source": email_source,
            "return_url": return_url,
        },
    )


@router.post("/leads/add-prospect", response_class=HTMLResponse)
def leads_add_prospect(
    request: Request,
    db: Session = Depends(get_db),
    apollo_id: str = Form(""),
    first_name: str = Form(""),
    title: str = Form(""),
    company: str = Form(""),
    return_url: str = Form("/dashboard/leads"),
):
    return _leads_enrich_and_preview(request, db, apollo_id, first_name, title, company, return_url)


@router.post("/leads/confirm-prospect", response_class=HTMLResponse)
def leads_confirm_prospect(
    db: Session = Depends(get_db),
    first_name: str = Form(""),
    last_name: str = Form(""),
    email: str = Form(""),
    title: str = Form(""),
    company: str = Form(""),
    phone: str = Form(""),
    linkedin_url: str = Form(""),
    geography: str = Form(""),
    return_url: str = Form("/dashboard/leads"),
):
    from datetime import datetime, timezone
    if not email or not email.strip():
        email = f"unknown_{uuid.uuid4().hex[:8]}@apollo.placeholder"

    email = email.strip()
    prospect = Prospect(
        id=str(uuid.uuid4()),
        email=email,
        first_name=first_name.strip() or None,
        last_name=last_name.strip() or None,
        company=company.strip() or None,
        title=title.strip() or None,
        phone=phone.strip() or None,
        linkedin_url=linkedin_url.strip() or None,
        geography=geography.strip() or None,
        source="apollo",
        email_validation_status="unknown",
    )
    db.add(prospect)
    db.commit()

    # Validate immediately if it's a real email (not a placeholder)
    if "@apollo.placeholder" not in email:
        try:
            results = bouncer.validate_batch([email])
            status = results.get(email)
            if status:
                prospect.email_validation_status = status
                prospect.email_validated_at = datetime.now(timezone.utc)
                db.commit()
        except Exception as e:
            logger.warning("Bouncer validation failed for %s: %s", email, e)

    return RedirectResponse(url=f"/dashboard/prospects/{prospect.id}", status_code=303)


@router.post("/leads/batch-enrich", response_class=HTMLResponse)
def leads_batch_enrich(
    request: Request,
    db: Session = Depends(get_db),
    selected: list[str] = Form([]),
    return_url: str = Form("/dashboard/leads"),
):
    if not selected:
        return RedirectResponse(url=return_url, status_code=303)

    enriched_rows = []
    for item in selected:
        parts = item.split("|", 2)
        if len(parts) < 3:
            continue
        apollo_id, first_name, company = parts[0], parts[1], parts[2]

        enriched: dict = {}
        email_source = None

        if first_name and company:
            apollo_result = apollo_client.enrich_person(first_name, "", company)
            if apollo_result:
                enriched = apollo_result
                if enriched.get("email"):
                    email_source = "Apollo"

        if not email_source and first_name and company:
            hunter_result = hunter_client.find_email(first_name, "", company)
            if hunter_result:
                enriched["email"] = hunter_result["email"]
                email_source = f"Hunter ({hunter_result.get('confidence', '?')}%)"

        resolved_first = enriched.get("first_name") or first_name
        resolved_last = enriched.get("last_name") or ""
        resolved_email = enriched.get("email") or ""
        geography = ", ".join(filter(None, [enriched.get("city"), enriched.get("state")])) or ""

        # Check if already saved as a prospect
        existing_id = None
        if resolved_email:
            ex = db.query(Prospect).filter(func.lower(Prospect.email) == resolved_email.lower()).first()
            if ex:
                existing_id = str(ex.id)

        enriched_rows.append({
            "first_name": resolved_first,
            "last_name": resolved_last,
            "email": resolved_email,
            "email_source": email_source,
            "title": enriched.get("title") or "",
            "company": enriched.get("company") or company,
            "phone": enriched.get("phone") or "",
            "linkedin_url": enriched.get("linkedin_url") or "",
            "geography": geography,
            "existing_id": existing_id,
        })

    return templates.TemplateResponse(
        "dashboard/leads_batch_preview.html",
        {
            "request": request,
            "active_page": "leads",
            "rows": enriched_rows,
            "return_url": return_url,
        },
    )


@router.post("/leads/batch-confirm", response_class=HTMLResponse)
def leads_batch_confirm(
    db: Session = Depends(get_db),
    include: list[str] = Form([]),
    first_name: list[str] = Form([]),
    last_name: list[str] = Form([]),
    email: list[str] = Form([]),
    title: list[str] = Form([]),
    company: list[str] = Form([]),
    phone: list[str] = Form([]),
    linkedin_url: list[str] = Form([]),
    geography: list[str] = Form([]),
    return_url: str = Form("/dashboard/leads"),
):
    from datetime import datetime, timezone

    # Build the list of prospects to save first
    to_save = []
    for idx_str in include:
        try:
            idx = int(idx_str)
        except ValueError:
            continue
        if idx >= len(first_name):
            continue

        em = email[idx].strip() if idx < len(email) else ""
        if not em:
            em = f"unknown_{uuid.uuid4().hex[:8]}@apollo.placeholder"

        if db.query(Prospect).filter(func.lower(Prospect.email) == em.lower()).first():
            continue

        to_save.append({
            "email": em,
            "first_name": (first_name[idx].strip() or None) if idx < len(first_name) else None,
            "last_name": (last_name[idx].strip() or None) if idx < len(last_name) else None,
            "company": (company[idx].strip() or None) if idx < len(company) else None,
            "title": (title[idx].strip() or None) if idx < len(title) else None,
            "phone": (phone[idx].strip() or None) if idx < len(phone) else None,
            "linkedin_url": (linkedin_url[idx].strip() or None) if idx < len(linkedin_url) else None,
            "geography": (geography[idx].strip() or None) if idx < len(geography) else None,
        })

    real_emails = [r["email"] for r in to_save if "@apollo.placeholder" not in r["email"]]
    zb_results: dict = {}
    if real_emails:
        try:
            zb_results = bouncer.validate_all(real_emails)
        except Exception as e:
            logger.warning("Bouncer batch validation failed: %s", e)

    now = datetime.now(timezone.utc)
    saved = 0
    for row in to_save:
        em = row["email"]
        zb_status = zb_results.get(em)
        prospect = Prospect(
            id=str(uuid.uuid4()),
            email=em,
            first_name=row["first_name"],
            last_name=row["last_name"],
            company=row["company"],
            title=row["title"],
            phone=row["phone"],
            linkedin_url=row["linkedin_url"],
            geography=row["geography"],
            source="apollo",
            email_validation_status=zb_status or "unknown",
            email_validated_at=now if zb_status else None,
        )
        db.add(prospect)
        saved += 1

    db.commit()
    return RedirectResponse(url=f"/dashboard/prospects?added={saved}", status_code=303)


# ---------------------------------------------------------------------------
# Spend Tracker
# ---------------------------------------------------------------------------

SPEND_CATEGORIES = ["outreach", "crm", "enrichment", "ai", "validation", "hosting", "infrastructure", "other"]


@router.get("/spend", response_class=HTMLResponse)
def spend_page(
    request: Request,
    db: Session = Depends(get_db),
    editing: str = Query(""),
):
    tools = db.query(ToolCost).order_by(ToolCost.category, ToolCost.name).all()
    active = [t for t in tools if t.status == "active"]
    inactive = [t for t in tools if t.status != "active"]
    total_active = float(sum(t.monthly_cost for t in active))

    zb_credits = bouncer.get_credits()
    zb_used = db.query(func.count(Prospect.id)).filter(
        Prospect.email_validated_at.is_not(None)
    ).scalar() or 0

    # Current-month window
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    emails_sent_month = (
        db.query(func.count(EmailEvent.id))
        .filter(
            EmailEvent.event_type == "sent",
            EmailEvent.occurred_at >= month_start,
        )
        .scalar()
    ) or 0

    # Distinct prospects who replied this month → each becomes a HubSpot deal
    hs_leads_month = (
        db.query(func.count(func.distinct(EmailEvent.prospect_id)))
        .filter(
            EmailEvent.event_type == "reply",
            EmailEvent.prospect_id.isnot(None),
            EmailEvent.occurred_at >= month_start,
        )
        .scalar()
    ) or 0

    cost_per_email = (total_active / emails_sent_month) if emails_sent_month else None
    cost_per_lead = (total_active / hs_leads_month) if hs_leads_month else None

    return templates.TemplateResponse(
        "dashboard/spend.html",
        {
            "request": request,
            "active_page": "spend",
            "tools": tools,
            "total_active": total_active,
            "total_inactive": float(sum(t.monthly_cost for t in inactive)),
            "active_count": len(active),
            "inactive_count": len(inactive),
            "editing": editing,
            "categories": SPEND_CATEGORIES,
            "emails_sent_month": emails_sent_month,
            "hs_leads_month": hs_leads_month,
            "cost_per_email": cost_per_email,
            "cost_per_lead": cost_per_lead,
            "zb_credits": zb_credits,
            "zb_used": zb_used,
        },
    )


@router.post("/spend/add", response_class=HTMLResponse)
def spend_add(
    db: Session = Depends(get_db),
    name: str = Form(...),
    category: str = Form("other"),
    monthly_cost: float = Form(0.0),
    notes: str = Form(""),
):
    tool = ToolCost(
        id=str(uuid.uuid4()),
        name=name.strip(),
        category=category,
        monthly_cost=monthly_cost,
        status="active",
        notes=notes.strip() or None,
    )
    db.add(tool)
    db.commit()
    return RedirectResponse(url="/dashboard/spend", status_code=303)


@router.post("/spend/{tool_id}/update", response_class=HTMLResponse)
def spend_update(
    tool_id: str,
    db: Session = Depends(get_db),
    name: str = Form(...),
    category: str = Form("other"),
    monthly_cost: float = Form(0.0),
    status: str = Form("active"),
    notes: str = Form(""),
):
    tool = db.query(ToolCost).filter(ToolCost.id == tool_id).first()
    if tool:
        tool.name = name.strip()
        tool.category = category
        tool.monthly_cost = monthly_cost
        tool.status = status
        tool.notes = notes.strip() or None
        db.commit()
    return RedirectResponse(url="/dashboard/spend", status_code=303)


@router.post("/spend/{tool_id}/delete", response_class=HTMLResponse)
def spend_delete(
    tool_id: str,
    db: Session = Depends(get_db),
):
    db.query(ToolCost).filter(ToolCost.id == tool_id).delete()
    db.commit()
    return RedirectResponse(url="/dashboard/spend", status_code=303)

