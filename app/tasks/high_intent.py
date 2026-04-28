"""
High Intent scan — runs every 15 minutes.

Criteria: prospect clicked a link >48h ago AND has no reply on that enrollment
AND is still on the standard track.

When triggered: marks enrollment as high_intent and, if a high_intent_campaign_id
is configured on the enrollment, enrolls the prospect in that Smartlead campaign.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, select, text

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

        # Real click: happened >= 15s after an open on the same enrollment.
        # Clicks within 15s of the open are corporate security scanners, not humans.
        candidate_ids = [
            row[0]
            for row in db.execute(text("""
                SELECT se.id
                FROM sequence_enrollments se
                WHERE se.status = 'active'
                  AND se.track = 'standard'
                  AND NOT EXISTS (
                      SELECT 1 FROM email_events
                      WHERE enrollment_id = se.id AND event_type = 'reply'
                  )
                  AND EXISTS (
                      SELECT 1
                      FROM email_events click
                      JOIN email_events open_ ON open_.enrollment_id = click.enrollment_id
                          AND open_.event_type = 'open'
                      WHERE click.enrollment_id = se.id
                        AND click.event_type = 'click'
                        AND click.occurred_at <= :cutoff
                        AND EXTRACT(EPOCH FROM (click.occurred_at - open_.occurred_at)) >= 15
                  )
            """), {"cutoff": cutoff}).fetchall()
        ]

        candidates = (
            db.execute(
                select(SequenceEnrollment).where(SequenceEnrollment.id.in_(candidate_ids))
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
