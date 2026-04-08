"""
High Intent scan — runs every 15 minutes.

Criteria: prospect clicked a link >48h ago AND has no reply on that enrollment
AND is still on the standard track.

When triggered: marks enrollment as high_intent and, if a high_intent_campaign_id
is configured on the enrollment, enrolls the prospect in that Smartlead campaign.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, select

from app.worker import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.high_intent.scan_high_intent")
def scan_high_intent():
    from app.database import SessionLocal
    from app.integrations.smartlead import enroll_prospect
    from app.models.email_event import EmailEvent
    from app.models.prospect import Prospect
    from app.models.sequence_enrollment import SequenceEnrollment

    db = SessionLocal()
    switched = 0
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=48)

        # Correlated subquery: this enrollment has at least one click older than 48h
        has_old_click = (
            select(EmailEvent.id)
            .where(
                and_(
                    EmailEvent.enrollment_id == SequenceEnrollment.id,
                    EmailEvent.event_type == "click",
                    EmailEvent.occurred_at <= cutoff,
                )
            )
            .correlate(SequenceEnrollment)
            .exists()
        )

        # Correlated subquery: this enrollment has a reply
        has_reply = (
            select(EmailEvent.id)
            .where(
                and_(
                    EmailEvent.enrollment_id == SequenceEnrollment.id,
                    EmailEvent.event_type == "reply",
                )
            )
            .correlate(SequenceEnrollment)
            .exists()
        )

        candidates = (
            db.execute(
                select(SequenceEnrollment).where(
                    and_(
                        SequenceEnrollment.status == "active",
                        SequenceEnrollment.track == "standard",
                        has_old_click,
                        ~has_reply,
                    )
                )
            )
            .scalars()
            .all()
        )

        for enrollment in candidates:
            try:
                enrollment.track = "high_intent"
                enrollment.high_intent_switched_at = datetime.now(timezone.utc)
                db.flush()

                if enrollment.high_intent_campaign_id:
                    prospect = db.get(Prospect, enrollment.prospect_id)
                    if prospect:
                        enroll_prospect(
                            campaign_id=int(enrollment.high_intent_campaign_id),
                            email=prospect.email,
                            first_name=prospect.first_name,
                            last_name=prospect.last_name,
                        )
                        enrollment.smartlead_campaign_id = enrollment.high_intent_campaign_id
                        db.flush()
                        logger.info(
                            "Switched %s to high_intent campaign %s",
                            prospect.email,
                            enrollment.high_intent_campaign_id,
                        )
                    else:
                        logger.warning(
                            "Enrollment %s has no matching prospect", enrollment.id
                        )

                switched += 1
            except Exception as e:
                logger.error(
                    "Failed to switch enrollment %s to high_intent: %s", enrollment.id, e
                )

        db.commit()
        logger.info("High intent scan complete: %d switched", switched)
        return {"switched": switched}

    except Exception as e:
        logger.error("High intent scan failed: %s", e)
        db.rollback()
        raise
    finally:
        db.close()
