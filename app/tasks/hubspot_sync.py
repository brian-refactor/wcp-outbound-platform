"""
HubSpot sync — runs every 5 minutes via Celery beat.

Rules:
  - sent / open / bounce / unsubscribe / complete  → mark synced, no HubSpot call
  - click   → upsert contact + create note on the contact
  - reply   → upsert contact + create note + create Deal named
               "WCP Automated Outbound - {prospect name}"

Batches up to 100 unsynced events per run. Events are marked
hubspot_synced_at immediately after processing so partial batch
failures don't re-process already-synced events.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.worker import celery_app

logger = logging.getLogger(__name__)

BATCH_SIZE = 100
SYNC_EVENT_TYPES = {"click", "reply"}  # only these trigger HubSpot API calls


@celery_app.task(name="app.tasks.hubspot_sync.sync_to_hubspot")
def sync_to_hubspot():
    from app.database import SessionLocal
    from app.integrations.hubspot import (
        build_activity_summary,
        create_deal,
        create_note,
        upsert_contacts,
    )
    from app.models.email_event import EmailEvent
    from app.models.prospect import Prospect
    from app.models.sequence_enrollment import SequenceEnrollment

    db = SessionLocal()
    synced = 0
    try:
        # Fetch unsynced events for known prospects
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

        # Only upsert contacts for events that will hit the HubSpot API
        actionable_events = [e for e in events if e.event_type in SYNC_EVENT_TYPES]

        email_to_hubspot_id: dict[str, str] = {}
        if actionable_events:
            prospect_ids = {e.prospect_id for e in actionable_events}
            prospects = (
                db.execute(select(Prospect).where(Prospect.id.in_(prospect_ids)))
                .scalars()
                .all()
            )
            prospect_map = {p.id: p for p in prospects}

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
        else:
            prospect_map = {}

        # Process each event
        for event in events:
            try:
                if event.event_type not in SYNC_EVENT_TYPES:
                    # Mark as synced without making any API calls
                    event.hubspot_synced_at = datetime.now(timezone.utc)
                    db.flush()
                    synced += 1
                    continue

                prospect = prospect_map.get(event.prospect_id)
                if not prospect:
                    logger.warning("Event %s has no matching prospect — skipping", event.id)
                    continue

                hubspot_id = email_to_hubspot_id.get(prospect.email)
                if not hubspot_id:
                    logger.warning("No HubSpot contact ID returned for %s", prospect.email)
                    continue

                # Build note body for click or reply
                note_body = _build_note_body(event)
                create_note(
                    hubspot_contact_id=hubspot_id,
                    note_body=note_body,
                    occurred_at=event.occurred_at,
                )

                # Reply → also create a Deal
                if event.event_type == "reply":
                    prospect_name = (
                        " ".join(filter(None, [prospect.first_name, prospect.last_name]))
                        or prospect.email
                    )
                    deal_name = f"WCP Automated Outbound - {prospect_name}"

                    # Build full activity history for deal description
                    enrollment = None
                    all_events = []
                    if event.enrollment_id:
                        enrollment = db.get(SequenceEnrollment, event.enrollment_id)
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

                    description = build_activity_summary(
                        prospect_name=prospect_name,
                        prospect_email=prospect.email,
                        company=prospect.company,
                        campaign_name=enrollment.campaign_name if enrollment else None,
                        track=enrollment.track if enrollment else "unknown",
                        events=event_dicts,
                    )

                    create_deal(
                        hubspot_contact_id=hubspot_id,
                        deal_name=deal_name,
                        description=description,
                    )
                    logger.info(
                        "Created HubSpot deal '%s' for %s", deal_name, prospect.email
                    )

                event.hubspot_synced_at = datetime.now(timezone.utc)
                db.flush()
                synced += 1

            except Exception as e:
                logger.error("Failed to sync event %s to HubSpot: %s", event.id, e)

        db.commit()
        logger.info("HubSpot sync complete: %d events processed", synced)
        return {"synced": synced}

    except Exception as e:
        logger.error("HubSpot sync task failed: %s", e)
        db.rollback()
        raise
    finally:
        db.close()


def _build_note_body(event) -> str:
    """Format a single email event as a HubSpot note body."""
    parts = [f"Email event: {event.event_type.upper()}"]
    if event.email_subject:
        parts.append(f'Subject: "{event.email_subject}"')
    if event.domain_used:
        parts.append(f"Sent from: {event.domain_used}")
    if event.clicked_url:
        parts.append(f"Clicked URL: {event.clicked_url}")
    if event.occurred_at:
        parts.append(f"Time: {event.occurred_at.strftime('%Y-%m-%d %H:%M UTC')}")
    return " | ".join(parts)
