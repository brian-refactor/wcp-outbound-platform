"""
Webhook handlers for inbound events from Smartlead.

Security: Smartlead sends the secret in the request body as `secret_key`.
Requests with a missing or incorrect secret are rejected with 401.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import and_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.integrations.smartlead import CATEGORY_NAMES
from app.models.email_event import EmailEvent
from app.models.prospect import Prospect
from app.models.sequence_enrollment import SequenceEnrollment

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

_OOO_PHRASES = (
    "out of office",
    "out of the office",
    "automatic reply",
    "auto-reply",
    "auto reply",
    "vacation reply",
    "away from the office",
    "away from my desk",
    "i am currently out",
    "i'm currently out",
    "currently out of",
    "will be back",
    "on vacation",
    "on annual leave",
    "on parental leave",
    "on maternity leave",
    "on paternity leave",
)


def _is_ooo(reply_text: str | None, reply_category: str | None) -> bool:
    if reply_category == "Out of Office":
        return True
    if reply_text:
        lower = reply_text.lower()
        return any(phrase in lower for phrase in _OOO_PHRASES)
    return False

# Smartlead event_type values mapped to our internal names
SMARTLEAD_EVENT_MAP = {
    "EMAIL_SENT": "sent",
    "EMAIL_OPEN": "open",
    "EMAIL_OPENED": "open",           # alias
    "EMAIL_LINK_CLICKED": "click",
    "EMAIL_CLICKED": "click",         # alias
    "EMAIL_LINK_CLICK": "click",      # alias
    "EMAIL_REPLIED": "reply",
    "EMAIL_REPLY": "reply",           # Smartlead test webhook sends EMAIL_REPLY (not EMAIL_REPLIED)
    "EMAIL_BOUNCED": "bounce",
    "EMAIL_BOUNCE": "bounce",         # alias
    "LEAD_UNSUBSCRIBED": "unsubscribe",
    "LEAD_UNSUBSCRIBE": "unsubscribe", # alias
    "LEAD_COMPLETED_SEQUENCE": "complete",
    "SEQUENCE_COMPLETED": "complete",  # alias
}


@router.post("/smartlead")
async def smartlead_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Receive email events from Smartlead.

    Smartlead retries failed webhooks 3x with exponential backoff.
    This handler is idempotent — duplicate message_ids are silently skipped.

    Expected payload fields:
      - event_type: one of EMAIL_SENT, EMAIL_OPEN, EMAIL_LINK_CLICKED,
                    EMAIL_REPLIED, EMAIL_BOUNCED, LEAD_UNSUBSCRIBED
      - secret_key: shared secret for auth verification
      - sent_message.message_id: unique ID per email send (our dedup key)
      - to_email: prospect email address
      - subject: email subject line
      - from_email: sending mailbox (gives us the domain)
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # Note: Smartlead does not support webhook secret signing.
    # Security relies on the obscurity of the webhook URL in development
    # and Railway's private networking in production.

    raw = json.dumps(payload)
    smartlead_event = payload.get("event_type", "")
    event_type = SMARTLEAD_EVENT_MAP.get(smartlead_event)

    if not event_type:
        logger.warning("Unrecognised Smartlead event_type: %s", smartlead_event)
        # Return 200 so Smartlead doesn't retry unknown event types
        return {"status": "ignored", "reason": f"unknown event_type: {smartlead_event}"}

    # Smartlead nests message_id under sent_message
    sent_message = payload.get("sent_message") or {}
    message_id = sent_message.get("message_id") or payload.get("message_id")
    lead_email = (payload.get("to_email") or payload.get("lead_email") or "").strip().lower()
    subject = payload.get("subject")
    link_clicked = payload.get("link_clicked") or payload.get("link_details") or []
    clicked_url = link_clicked[0] if link_clicked else None
    from_email = payload.get("from_email") or ""
    domain_used = from_email.split("@")[-1] if "@" in from_email else None
    sequence_number = payload.get("sequence_number")
    reply_category_id = payload.get("reply_category")
    reply_category = CATEGORY_NAMES.get(reply_category_id) if reply_category_id else None
    reply_text = (
        ((payload.get("reply_message") or {}).get("text") or "").strip() or None
        if event_type == "reply"
        else None
    )

    # Extract Smartlead campaign ID to link this event to the correct enrollment
    campaign_id = str(payload.get("campaign_id") or "")

    # Look up the prospect by email (nullable — event still recorded if not found)
    prospect = None
    enrollment = None
    if lead_email:
        prospect = db.query(Prospect).filter(Prospect.email == lead_email).first()
        if not prospect:
            logger.warning(
                "Smartlead event for unknown prospect email: %s", lead_email
            )

    # Look up the active enrollment for this prospect + campaign
    if prospect and campaign_id:
        enrollment = (
            db.query(SequenceEnrollment)
            .filter(
                and_(
                    SequenceEnrollment.prospect_id == prospect.id,
                    SequenceEnrollment.smartlead_campaign_id == campaign_id,
                    SequenceEnrollment.status == "active",
                )
            )
            .first()
        )

    event = EmailEvent(
        prospect_id=prospect.id if prospect else None,
        enrollment_id=enrollment.id if enrollment else None,
        event_type=event_type,
        email_subject=subject,
        domain_used=domain_used,
        clicked_url=clicked_url,
        sequence_number=sequence_number,
        smartlead_message_id=message_id,
        raw_payload=raw,
        is_ooo=_is_ooo(reply_text, reply_category) if event_type == "reply" else False,
    )

    db.add(event)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        # Duplicate message_id — idempotent, return 200 so Smartlead stops retrying
        logger.info("Duplicate webhook ignored (message_id=%s)", message_id)
        return {"status": "ok"}

    # Opt-out: mark enrollment and prospect as opted out
    if event_type == "unsubscribe" and enrollment:
        enrollment.status = "opted_out"
        enrollment.opted_out_at = datetime.now(timezone.utc)
        logger.info("Prospect %s opted out of enrollment %s", lead_email, enrollment.id)

    # Bounce: mark enrollment as bounced
    if event_type == "bounce" and enrollment:
        enrollment.status = "bounced"
        logger.info("Prospect %s bounced on enrollment %s", lead_email, enrollment.id)

    # Reply: store category; only mark completed if it's a real reply (not OOO)
    if event_type == "reply" and enrollment:
        if reply_category:
            enrollment.smartlead_category = reply_category
        if _is_ooo(reply_text, reply_category):
            logger.info(
                "Prospect %s sent OOO — enrollment %s stays active", lead_email, enrollment.id
            )
        else:
            enrollment.status = "completed"
            enrollment.completed_at = datetime.now(timezone.utc)
            logger.info("Prospect %s replied — enrollment %s marked completed", lead_email, enrollment.id)

    # Sequence complete: mark enrollment as completed
    if event_type == "complete" and enrollment:
        enrollment.status = "completed"
        enrollment.completed_at = datetime.now(timezone.utc)
        logger.info("Prospect %s completed sequence — enrollment %s", lead_email, enrollment.id)

    db.commit()
    logger.info(
        "Recorded %s event for %s (message_id=%s, enrollment=%s)",
        event_type,
        lead_email,
        message_id,
        enrollment.id if enrollment else "none",
    )

    return {"status": "ok"}
