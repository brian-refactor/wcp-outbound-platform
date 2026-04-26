"""
Email validation tasks.

validate_emails — runs every 30 minutes via Celery beat. Picks up prospects
with no email_validation_status (NULL) and validates in batches via Bouncer.

revalidate_unknown_emails — triggered on-demand from the dashboard. Picks up
all prospects with email_validation_status = 'unknown' and re-verifies them
using small batches so Bouncer has enough time per address.
"""

import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select

from app.worker import celery_app

logger = logging.getLogger(__name__)

BATCH_SIZE = 200
REVALIDATE_BATCH_SIZE = 20  # Small batches give Bouncer time to verify each address


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

        now = datetime.now(timezone.utc)
        validated = 0

        for i in range(0, len(prospects), REVALIDATE_BATCH_SIZE):
            batch = prospects[i:i + REVALIDATE_BATCH_SIZE]
            emails = [p.email for p in batch]
            results = validate_batch(emails)
            for prospect in batch:
                status = results.get(prospect.email.lower())
                prospect.email_validation_status = status if status else "unknown"
                prospect.email_validated_at = now
                validated += 1
            db.commit()
            if i + REVALIDATE_BATCH_SIZE < len(prospects):
                time.sleep(1)

        logger.info("Email validation complete: %d prospects validated", validated)
        return {"validated": validated}

    except Exception as e:
        db.rollback()
        logger.error("Email validation task failed: %s", e)
        raise
    finally:
        db.close()


@celery_app.task(name="app.tasks.email_validation.revalidate_unknown_emails")
def revalidate_unknown_emails():
    from app.config import settings
    from app.database import SessionLocal
    from app.integrations.bouncer import validate_batch
    from app.models.prospect import Prospect

    if not settings.bouncer_api_key:
        logger.info("Revalidation skipped: BOUNCER_API_KEY not set")
        return {"revalidated": 0}

    db = SessionLocal()
    try:
        prospects = (
            db.execute(
                select(Prospect)
                .where(Prospect.email_validation_status == "unknown")
                .order_by(Prospect.created_at)
            )
            .scalars()
            .all()
        )

        if not prospects:
            logger.info("Revalidation: no unknown prospects to process")
            return {"revalidated": 0}

        logger.info("Revalidation starting: %d unknown prospects", len(prospects))
        now = datetime.now(timezone.utc)
        revalidated = 0

        for i in range(0, len(prospects), REVALIDATE_BATCH_SIZE):
            batch = prospects[i:i + REVALIDATE_BATCH_SIZE]
            emails = [p.email for p in batch]
            results = validate_batch(emails)
            for prospect in batch:
                status = results.get(prospect.email.lower())
                prospect.email_validation_status = status if status else "unknown"
                prospect.email_validated_at = now
                revalidated += 1
            db.commit()
            if i + REVALIDATE_BATCH_SIZE < len(prospects):
                time.sleep(1)

        logger.info("Revalidation complete: %d prospects processed", revalidated)
        return {"revalidated": revalidated}

    except Exception as e:
        db.rollback()
        logger.error("Revalidation task failed: %s", e)
        raise
    finally:
        db.close()
