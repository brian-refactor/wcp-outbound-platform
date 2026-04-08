"""
HubSpot sync — runs every 5 minutes.

For every unsynced EmailEvent:
  - Upserts the contact in HubSpot (keeps the record current)
  - If the event is a reply: creates a Deal in the Prospect Outreach stage
    with the full email activity history embedded in the description

Batches up to 100 events per run. Partial failures are logged and retried
on the next run (events remain hubspot_synced_at IS NULL).
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.worker import celery_app

logger = logging.getLogger(__name__)

BATCH_SIZE = 100


@celery_app.task(name="app.tasks.hubspot_sync.sync_to_hubspot")
def sync_to_hubspot():
    from app.database import SessionLocal
    from app.integrations.hubspot import build_activity_summary, create_deal, upsert_contacts
    from app.models.email_event import EmailEvent
    from app.models.prospect import Prospect
    from app.models.sequence_enrollment import SequenceEnrollment

    db = SessionLocal()
    synced = 0
    try:
        events = (
            db.execute(
                select(EmailEvent)
                .where(EmailEvent.hubspot_synced_at.is_(None))
                .where(EmailEvent.prospect_id.is_not(None))
                .order_by(EmailEvent.occurred_at)
                .limit(BATCH_SIZE)
            )
            .scalars()
            .all()
        )

        if not events:
            logger.info("HubSpot sync: no unsynced events")
            return {"synced": 0}

        # Resolve prospects in one query
        prospect_ids = {e.prospect_id for e in events}
        prospects = (
            db.execute(select(Prospect).where(Prospect.id.in_(prospect_ids)))
            .scalars()
            .all()
        )
        prospect_map = {p.id: p for p in prospects}

        # Batch upsert all contacts to HubSpot
        contact_inputs = [
            {
                "email": p.email,
                "first_name": p.first_name,
                "last_name": p.last_name,
                "company": p.company,
                "title": p.title,
                "phone": p.phone,
            }
            for p in prospect_map.values()
        ]

        try:
            email_to_hubspot_id = upsert_contacts(contact_inputs)
        except Exception as e:
            logger.error("HubSpot contact upsert failed — skipping batch: %s", e)
            return {"synced": 0}

        # Process each event
        for event in events:
            try:
                prospect = prospect_map.get(event.prospect_id)
                if not prospect:
                    logger.warning("Event %s has no matching prospect — skipping", event.id)
                    continue

                hubspot_id = email_to_hubspot_id.get(prospect.email)
                if not hubspot_id:
                    logger.warning("No HubSpot ID for %s", prospect.email)
                    continue

                # Reply events → create a Deal with full activity history
                if event.event_type == "reply" and event.enrollment_id:
                    _create_reply_deal(
                        db=db,
                        event=event,
                        prospect=prospect,
                        hubspot_contact_id=hubspot_id,
                        create_deal=create_deal,
                        build_activity_summary=build_activity_summary,
                        EmailEvent=EmailEvent,
                        SequenceEnrollment=SequenceEnrollment,
                    )

                event.hubspot_synced_at = datetime.now(timezone.utc)
                db.flush()
                synced += 1

            except Exception as e:
                logger.error("Failed to sync event %s to HubSpot: %s", event.id, e)

        db.commit()
        logger.info("HubSpot sync complete: %d events synced", synced)
        return {"synced": synced}

    except Exception as e:
        logger.error("HubSpot sync task failed: %s", e)
        db.rollback()
        raise
    finally:
        db.close()


def _create_reply_deal(
    db,
    event,
    prospect,
    hubspot_contact_id: str,
    create_deal,
    build_activity_summary,
    EmailEvent,
    SequenceEnrollment,
):
    """Fetch the full enrollment history and create a HubSpot deal."""

    # Load the enrollment for sequence context
    enrollment = db.get(SequenceEnrollment, event.enrollment_id)

    # Fetch ALL email events for this enrollment (not just the current batch)
    all_events = (
        db.execute(
            select(EmailEvent)
            .where(EmailEvent.enrollment_id == event.enrollment_id)
            .order_by(EmailEvent.occurred_at)
        )
        .scalars()
        .all()
    )

    event_dicts = [
        {
            "event_type": e.event_type,
            "occurred_at": e.occurred_at,
            "email_subject": e.email_subject,
            "domain_used": e.domain_used,
            "clicked_url": e.clicked_url,
        }
        for e in all_events
    ]

    prospect_name = " ".join(filter(None, [prospect.first_name, prospect.last_name])) or prospect.email
    sequence_type = enrollment.sequence_type if enrollment else "unknown"
    track = enrollment.track if enrollment else "unknown"

    description = build_activity_summary(
        prospect_name=prospect_name,
        prospect_email=prospect.email,
        company=prospect.company,
        sequence_type=sequence_type,
        track=track,
        events=event_dicts,
    )

    deal_name = f"{prospect_name} — Outbound Reply"

    create_deal(
        hubspot_contact_id=hubspot_contact_id,
        deal_name=deal_name,
        description=description,
    )

    logger.info("Created HubSpot deal for %s (reply on enrollment %s)", prospect.email, event.enrollment_id)
