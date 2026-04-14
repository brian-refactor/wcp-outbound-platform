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
from app.integrations import smartlead, zerobounce
from app.models.email_event import EmailEvent
from app.models.prospect import Prospect
from app.models.sequence_enrollment import SequenceEnrollment
from app.routers.stats import (
    campaigns_funnel,
    overview_stats,
    recent_events,
    sends_by_domain,
    sequence_stats,
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
    from app.integrations.zerobounce import get_credits
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
    credits = zerobounce.get_credits()
    used = db.query(func.count(Prospect.id)).filter(
        Prospect.email_validated_at.is_not(None)
    ).scalar() or 0
    low = 0 <= credits < 500
    return templates.TemplateResponse(
        "dashboard/fragments/zb_credits.html",
        {"request": request, "credits": credits, "used": used, "low": low},
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
                COALESCE(se.campaign_name, se.smartlead_campaign_id) || '|' || se.status,
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
    db: Session = Depends(get_db),
):
    if prospect_ids:
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
    prospect_ids: list[str] = Form(...),
    campaign_id: str = Form(...),
    campaign_name: str = Form(""),
):
    if not campaign_id:
        return RedirectResponse(url="/dashboard/prospects?bulk_error=missing_fields", status_code=303)

    prospects = db.query(Prospect).filter(Prospect.id.in_(prospect_ids)).all()
    enrolled_count = 0
    failed = []

    for prospect in prospects:
        if prospect.email_validation_status != "valid":
            logger.warning(
                "Bulk enroll skipped %s — email status: %s",
                prospect.email, prospect.email_validation_status or "not validated",
            )
            failed.append(f"{prospect.email} (email {prospect.email_validation_status or 'not validated'})")
            continue
        already_enrolled = (
            db.query(SequenceEnrollment)
            .filter(
                SequenceEnrollment.prospect_id == prospect.id,
                SequenceEnrollment.smartlead_campaign_id == str(campaign_id),
                SequenceEnrollment.status == "active",
            )
            .first()
        )
        if already_enrolled:
            logger.info("Bulk enroll skipped %s — already active in campaign %s", prospect.email, campaign_id)
            continue
        try:
            _ensure_personalized_intro(prospect, db)
            smartlead.enroll_prospect(
                campaign_id=int(campaign_id),
                email=prospect.email,
                first_name=prospect.first_name,
                last_name=prospect.last_name,
                custom_fields=_prospect_custom_fields(prospect),
            )
            db.add(SequenceEnrollment(
                prospect_id=prospect.id,
                smartlead_campaign_id=str(campaign_id),
                campaign_name=campaign_name or None,
                status="active",
            ))
            enrolled_count += 1
        except Exception as e:
            logger.error("Bulk enroll failed for %s: %s", prospect.email, e)
            failed.append(prospect.email)

    db.commit()
    msg = f"bulk_enrolled={enrolled_count}"
    if failed:
        msg += f"&bulk_failed={len(failed)}"
    return RedirectResponse(url=f"/dashboard/prospects?{msg}", status_code=303)


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

    # Validate email immediately via ZeroBounce (single-email call)
    try:
        results = zerobounce.validate_batch([email])
        validation_status = results.get(email)
        if validation_status:
            prospect.email_validation_status = validation_status
            prospect.email_validated_at = datetime.now(timezone.utc)
            db.commit()
    except Exception as e:
        logger.warning("ZeroBounce validation failed for %s: %s", email, e)

    # Optional enrollment
    if campaign_id and campaign_id.strip():
        if prospect.email_validation_status != "valid":
            status_label = prospect.email_validation_status or "not validated"
            return render_error(
                f"Prospect added, but cannot enroll — email validated as '{status_label}'. "
                "Only valid emails can be enrolled."
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
# CSV import
# ---------------------------------------------------------------------------

@router.get("/prospects/import", response_class=HTMLResponse)
def prospect_import_form(request: Request):
    return templates.TemplateResponse(
        "dashboard/prospect_import.html",
        {"request": request, "active_page": "prospects", "result": None},
    )


@router.post("/prospects/import", response_class=HTMLResponse)
async def prospect_import_submit(
    request: Request,
    db: Session = Depends(get_db),
    file: UploadFile = File(...),
):
    error = None
    result = None

    if not file.filename or not file.filename.endswith(".csv"):
        error = "Please upload a .csv file."
    else:
        max_size = 10 * 1024 * 1024
        content = await file.read(max_size + 1)
        if len(content) > max_size:
            error = "File too large — 10 MB maximum."
        else:
            text_content = content.decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text_content))
            imported, skipped, errors = 0, 0, []

            for row_num, row in enumerate(reader, start=2):
                email = (row.get("email") or "").strip().lower()
                if not email:
                    errors.append(f"Row {row_num}: missing email — skipped")
                    skipped += 1
                    continue

                asset_class = (row.get("asset_class_preference") or "").strip() or None
                if asset_class and asset_class not in ("PE", "RE", "both"):
                    errors.append(f"Row {row_num}: invalid asset_class '{asset_class}' — set to null")
                    asset_class = None

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
                    source=(row.get("source") or "").strip() or "apollo",
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
            result = {"imported": imported, "skipped": skipped, "errors": errors}

    return templates.TemplateResponse(
        "dashboard/prospect_import.html",
        {"request": request, "active_page": "prospects", "result": result, "error": error},
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
def prospect_edit_form(prospect_id: str, request: Request, db: Session = Depends(get_db)):
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
        if prospect.email_validation_status != "valid":
            try:
                results = zerobounce.validate_batch([prospect.email])
                validation_status = results.get(prospect.email)
                if validation_status:
                    prospect.email_validation_status = validation_status
                    prospect.email_validated_at = datetime.now(timezone.utc)
                    db.commit()
                    db.refresh(prospect)
            except Exception as e:
                logger.warning("ZeroBounce validation failed for %s: %s", prospect.email, e)

        if prospect.email_validation_status != "valid":
            status_label = prospect.email_validation_status or "not validated"
            return render_error(
                f"Cannot enroll — email validated as '{status_label}'. "
                "Only valid emails can be enrolled."
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
            "seq_types": seq_types,
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
    zb_credits = zerobounce.get_credits()
    zb_used = db.query(func.count(Prospect.id)).filter(
        Prospect.email_validated_at.is_not(None)
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
        email_status = "unknown"  # ZeroBounce will validate on next batch run

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
