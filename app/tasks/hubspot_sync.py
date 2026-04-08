"""
HubSpot sync — runs every 5 minutes.

Finds EmailEvent rows where hubspot_synced_at IS NULL (oldest first),
upserts the associated contacts to HubSpot, creates a note per event,
and marks hubspot_synced_at on each event as it succeeds.

Batches up to 100 events per run (matches HubSpot's contact upsert limit).
Partial failures are logged and retried on the next run.
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
    from app.integrations.hubspot import create_note, upsert_contacts
    from app.models.email_event import EmailEvent
    from app.models.prospect import Prospect

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

        # Resolve all prospects in one query
        prospect_ids = {e.prospect_id for e in events}
        prospects = (
            db.execute(select(Prospect).where(Prospect.id.in_(prospect_ids)))
            .scalars()
            .all()
        )
        prospect_map = {p.id: p for p in prospects}

        # Batch upsert contacts to HubSpot
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

        # Create a note per event and mark synced
        for event in events:
            try:
                prospect = prospect_map.get(event.prospect_id)
                if not prospect:
                    logger.warning("Event %s has no matching prospect — skipping", event.id)
                    continue

                hubspot_id = email_to_hubspot_id.get(prospect.email)
                if not hubspot_id:
                    logger.warning(
                        "No HubSpot ID for %s — contact may have been skipped", prospect.email
                    )
                    continue

                create_note(
                    hubspot_contact_id=hubspot_id,
                    event_type=event.event_type,
                    email_subject=event.email_subject,
                    domain_used=event.domain_used,
                    clicked_url=event.clicked_url,
                    occurred_at=event.occurred_at,
                )

                event.hubspot_synced_at = datetime.now(timezone.utc)
                db.flush()  # flush per event so partial progress survives a crash
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
