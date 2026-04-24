"""
Email validation task — runs every 30 minutes via Celery beat.

Picks up prospects with no email_validation_status, validates in batches
via Bouncer, and writes the result back to the DB.

Skips gracefully if BOUNCER_API_KEY is not configured.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.worker import celery_app

logger = logging.getLogger(__name__)

BATCH_SIZE = 200


@celery_app.task(name="app.tasks.email_validation.validate_emails")
def validate_emails():
    from app.config import settings
    from app.database import SessionLocal
    from app.integrations.bouncer import validate_batch
    from app.models.prospect import Prospect

    if not settings.bouncer_api_key:
        logger.info("Email validation skipped: BOUNCER_API_KEY not set")
        return {"validated": 0}

    db = SessionLocal()
    try:
        prospects = (
            db.execute(
                select(Prospect)
                .where(Prospect.email_validation_status.is_(None))
                .order_by(Prospect.created_at)
                .limit(BATCH_SIZE)
            )
            .scalars()
            .all()
        )

        if not prospects:
            logger.info("Email validation: no unvalidated prospects")
            return {"validated": 0}

        emails = [p.email for p in prospects]
        results = validate_batch(emails)

        now = datetime.now(timezone.utc)
        validated = 0
        for prospect in prospects:
            status = results.get(prospect.email.lower())
            if status:
                prospect.email_validation_status = status
                prospect.email_validated_at = now
                validated += 1
            else:
                prospect.email_validation_status = "unknown"
                prospect.email_validated_at = now
                validated += 1

        db.commit()
        logger.info("Email validation complete: %d prospects validated", validated)
        return {"validated": validated}

    except Exception as e:
        db.rollback()
        logger.error("Email validation task failed: %s", e)
        raise
    finally:
        db.close()
