"""
Email validation tasks.

All bulk validation uses bouncer.validate_all() — 20 emails per Bouncer
request with a 1s delay between batches — so Bouncer has enough time to
verify each address and doesn't return 'unknown' due to internal timeouts.

validate_emails            — scheduled every 30 min, picks up NULL-status prospects
revalidate_unknown_emails  — on-demand, picks up 'unknown'-status prospects
validate_selected_emails   — on-demand, validates a specific list of prospect IDs
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
    from app.integrations.bouncer import validate_all
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
        results = validate_all(emails)

        now = datetime.now(timezone.utc)
        for prospect in prospects:
            status = results.get(prospect.email.lower())
            prospect.email_validation_status = status if status else "unknown"
            prospect.email_validated_at = now

        db.commit()
        logger.info("Email validation complete: %d prospects validated", len(prospects))
        return {"validated": len(prospects)}

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
    from app.integrations.bouncer import validate_all
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
        emails = [p.email for p in prospects]
        results = validate_all(emails)

        now = datetime.now(timezone.utc)
        for prospect in prospects:
            status = results.get(prospect.email.lower())
            prospect.email_validation_status = status if status else "unknown"
            prospect.email_validated_at = now

        db.commit()
        logger.info("Revalidation complete: %d prospects processed", len(prospects))
        return {"revalidated": len(prospects)}

    except Exception as e:
        db.rollback()
        logger.error("Revalidation task failed: %s", e)
        raise
    finally:
        db.close()


@celery_app.task(name="app.tasks.email_validation.validate_selected_emails")
def validate_selected_emails(prospect_ids: list[str]):
    from app.config import settings
    from app.database import SessionLocal
    from app.integrations.bouncer import validate_all
    from app.models.prospect import Prospect

    if not settings.bouncer_api_key:
        logger.info("Validation skipped: BOUNCER_API_KEY not set")
        return {"validated": 0}

    db = SessionLocal()
    try:
        prospects = (
            db.execute(
                select(Prospect).where(Prospect.id.in_(prospect_ids))
            )
            .scalars()
            .all()
        )

        if not prospects:
            return {"validated": 0}

        emails = [p.email for p in prospects]
        results = validate_all(emails)

        now = datetime.now(timezone.utc)
        for prospect in prospects:
            status = results.get(prospect.email.lower())
            prospect.email_validation_status = status if status else "unknown"
            prospect.email_validated_at = now

        db.commit()
        logger.info("Selected validation complete: %d prospects", len(prospects))
        return {"validated": len(prospects)}

    except Exception as e:
        db.rollback()
        logger.error("Selected email validation failed: %s", e)
        raise
    finally:
        db.close()
