"""
Webhook handlers for inbound events from Smartlead.

Security: every request must include the shared secret in the
X-Smartlead-Secret header. Requests without it are rejected with 401.
"""

import hashlib
import hmac
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.email_event import EmailEvent
from app.models.prospect import Prospect

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# Smartlead event_type values mapped to our internal names
SMARTLEAD_EVENT_MAP = {
    "EMAIL_SENT": "sent",
    "EMAIL_OPEN": "open",
    "EMAIL_LINK_CLICKED": "click",
    "EMAIL_REPLIED": "reply",
    "EMAIL_BOUNCED": "bounce",
    "LEAD_UNSUBSCRIBED": "unsubscribe",
}


def _verify_secret(x_smartlead_secret: Optional[str] = Header(default=None)) -> None:
    """Reject requests that don't carry the correct shared secret."""
    if not settings.smartlead_webhook_secret:
        # Secret not configured — skip verification in development only
        if settings.environment == "production":
            raise HTTPException(status_code=500, detail="Webhook secret not configured")
        return

    if x_smartlead_secret != settings.smartlead_webhook_secret:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")


@router.post("/smartlead", dependencies=[Depends(_verify_secret)])
async def smartlead_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Receive email events from Smartlead.

    Smartlead retries failed webhooks 3x with exponential backoff.
    This handler is idempotent — duplicate message_ids are silently skipped.

    Expected payload fields (Smartlead sends snake_case):
      - event_type: one of EMAIL_SENT, EMAIL_OPEN, EMAIL_LINK_CLICKED,
                    EMAIL_REPLIED, EMAIL_BOUNCED, LEAD_UNSUBSCRIBED
      - message_id: unique ID per email send (our dedup key)
      - lead_email: prospect email address
      - subject: email subject line
      - clicked_link: URL clicked (click events only)
      - from_email: sending mailbox (gives us the domain)
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    raw = json.dumps(payload)
    smartlead_event = payload.get("event_type", "")
    event_type = SMARTLEAD_EVENT_MAP.get(smartlead_event)

    if not event_type:
        logger.warning("Unrecognised Smartlead event_type: %s", smartlead_event)
        # Return 200 so Smartlead doesn't retry unknown event types
        return {"status": "ignored", "reason": f"unknown event_type: {smartlead_event}"}

    message_id = payload.get("message_id") or payload.get("id")
    lead_email = (payload.get("lead_email") or "").strip().lower()
    subject = payload.get("subject")
    clicked_url = payload.get("clicked_link")
    from_email = payload.get("from_email") or ""
    domain_used = from_email.split("@")[-1] if "@" in from_email else None

    # Look up the prospect by email (nullable — event still recorded if not found)
    prospect = None
    if lead_email:
        prospect = db.query(Prospect).filter(Prospect.email == lead_email).first()
        if not prospect:
            logger.warning(
                "Smartlead event for unknown prospect email: %s", lead_email
            )

    event = EmailEvent(
        prospect_id=prospect.id if prospect else None,
        event_type=event_type,
        email_subject=subject,
        domain_used=domain_used,
        clicked_url=clicked_url,
        smartlead_message_id=message_id,
        raw_payload=raw,
    )

    db.add(event)
    try:
        db.commit()
        logger.info(
            "Recorded %s event for %s (message_id=%s)", event_type, lead_email, message_id
        )
    except IntegrityError:
        db.rollback()
        # Duplicate message_id — idempotent, return 200 so Smartlead stops retrying
        logger.info("Duplicate webhook ignored (message_id=%s)", message_id)

    return {"status": "ok"}
