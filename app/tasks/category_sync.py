"""
Smartlead lead category sync — runs every 15 minutes via Celery beat.

Fetches the AI lead category for every enrolled prospect from Smartlead
and writes it back to sequence_enrollments.smartlead_category.
"""

import logging

from app.worker import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.category_sync.sync_lead_categories")
def sync_lead_categories():
    from app.database import SessionLocal
    from app.integrations.smartlead import get_campaign_lead_categories
    from app.models.prospect import Prospect
    from app.models.sequence_enrollment import SequenceEnrollment
    from sqlalchemy import select

    db = SessionLocal()
    updated = 0
    try:
        # Get all unique campaign IDs currently in use
        campaign_ids = [
            row[0]
            for row in db.execute(
                select(SequenceEnrollment.smartlead_campaign_id).distinct()
            ).all()
        ]

        if not campaign_ids:
            logger.info("Category sync: no enrollments found")
            return {"updated": 0}

        # Build prospect email → enrollment map
        enrollments = db.execute(select(SequenceEnrollment)).scalars().all()
        prospect_ids = {e.prospect_id for e in enrollments}
        prospects = db.execute(
            select(Prospect).where(Prospect.id.in_(prospect_ids))
        ).scalars().all()
        email_to_enrollment: dict[str, SequenceEnrollment] = {}
        prospect_map = {p.id: p for p in prospects}
        for e in enrollments:
            p = prospect_map.get(e.prospect_id)
            if p:
                email_to_enrollment[p.email.lower().strip()] = e

        # Fetch categories from Smartlead for each campaign and update DB
        for campaign_id in campaign_ids:
            try:
                categories = get_campaign_lead_categories(int(campaign_id))
                for email, category_name in categories.items():
                    enrollment = email_to_enrollment.get(email)
                    if enrollment and enrollment.smartlead_category != category_name:
                        enrollment.smartlead_category = category_name
                        updated += 1
            except Exception as e:
                logger.error("Category fetch failed for campaign %s: %s", campaign_id, e)

        db.commit()
        logger.info("Category sync complete: %d enrollments updated", updated)
        return {"updated": updated}

    except Exception as e:
        db.rollback()
        logger.error("Category sync task failed: %s", e)
        raise
    finally:
        db.close()
