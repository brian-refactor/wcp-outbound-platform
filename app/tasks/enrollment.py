"""
Bulk enrollment Celery task.

Runs the full enrollment pipeline in the background so the web request
returns immediately. Handles dedup against Smartlead + DB, intro
generation, and batched Smartlead API submission.
"""

import logging

from app.worker import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.enrollment.bulk_enroll_campaign")
def bulk_enroll_campaign(
    prospect_ids: list[str],
    campaign_id: int,
    campaign_name: str,
    include_catch_all: bool = False,
):
    from app.database import SessionLocal
    from app.integrations import smartlead
    from app.models.prospect import Prospect
    from app.models.sequence_enrollment import SequenceEnrollment
    from app.routers.dashboard import _ensure_personalized_intro, _prospect_custom_fields

    db = SessionLocal()
    try:
        prospects = db.query(Prospect).filter(Prospect.id.in_(prospect_ids)).all()
        if not prospects:
            logger.info("Bulk enroll task: no prospects found for %d ids", len(prospect_ids))
            return {"enrolled": 0}

        allowed_statuses = ("valid", "catch-all") if include_catch_all else ("valid",)

        enrollable = [p for p in prospects if p.email_validation_status in allowed_statuses]
        skipped_status = len(prospects) - len(enrollable)
        if skipped_status:
            logger.info("Bulk enroll task: skipped %d by email status", skipped_status)

        if not enrollable:
            return {"enrolled": 0, "skipped_status": skipped_status}

        # Dedup against Smartlead
        try:
            smartlead_emails = smartlead.get_all_campaign_lead_emails(campaign_id)
        except Exception as e:
            logger.warning("Could not fetch Smartlead campaign leads for dedup: %s", e)
            smartlead_emails = set()

        # Dedup against DB
        active_ids = {
            str(row.prospect_id)
            for row in db.query(SequenceEnrollment.prospect_id)
            .filter(
                SequenceEnrollment.smartlead_campaign_id == str(campaign_id),
                SequenceEnrollment.status == "active",
            )
            .all()
        }

        to_enroll = [
            p for p in enrollable
            if p.email.lower() not in smartlead_emails and str(p.id) not in active_ids
        ]
        skipped_dupe = len(enrollable) - len(to_enroll)
        if skipped_dupe:
            logger.info("Bulk enroll task: skipped %d duplicates", skipped_dupe)

        if not to_enroll:
            return {"enrolled": 0, "skipped_status": skipped_status, "skipped_dupe": skipped_dupe}

        # Generate missing intros
        for prospect in to_enroll:
            _ensure_personalized_intro(prospect, db)

        # Batch enroll
        lead_dicts = [
            {
                "email": p.email,
                "first_name": p.first_name,
                "last_name": p.last_name,
                "custom_fields": _prospect_custom_fields(p),
            }
            for p in to_enroll
        ]

        smartlead.enroll_prospects_batch(campaign_id, lead_dicts)

        for prospect in to_enroll:
            db.add(SequenceEnrollment(
                prospect_id=prospect.id,
                smartlead_campaign_id=str(campaign_id),
                campaign_name=campaign_name or None,
                status="active",
            ))
        db.commit()

        logger.info(
            "Bulk enroll task complete: %d enrolled, %d skipped (status), %d skipped (dupe)",
            len(to_enroll), skipped_status, skipped_dupe,
        )
        return {"enrolled": len(to_enroll), "skipped_status": skipped_status, "skipped_dupe": skipped_dupe}

    except Exception as e:
        db.rollback()
        logger.error("Bulk enroll task failed for campaign %s: %s", campaign_id, e)
        raise
    finally:
        db.close()
