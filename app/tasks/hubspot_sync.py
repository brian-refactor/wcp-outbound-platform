"""
HubSpot sync — runs every 15 minutes via Celery beat.

Per-campaign rules (from CampaignConfig):
  trigger="reply"  → upsert contact + note on click/reply + deal on reply  (default)
  trigger="click"  → upsert contact + note on click/reply + deal on click
  trigger="open"   → upsert contact + note on open/click/reply + deal on open
  trigger="none"   → upsert contact + note on click/reply, no deal ever

Campaigns with no CampaignConfig row fall back to trigger="reply" + global
pipeline/stage IDs — identical to the previous hardcoded behavior.

Batches up to 100 unsynced events per run.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.worker import celery_app

logger = logging.getLogger(__name__)

BATCH_SIZE = 100
_NOTE_EVENT_TYPES = {"click", "reply"}

_OOO_PHRASES = (
    "out of office",
    "out of the office",
    "automatic reply",
    "auto-reply",
    "auto reply",
    "vacation reply",
    "away from the office",
    "away from my desk",
    "i am currently out",
    "i'm currently out",
    "currently out of",
    "will be back",
    "on vacation",
    "on annual leave",
    "on parental leave",
    "on maternity leave",
    "on paternity leave",
)


def _is_ooo(reply_text: str | None, reply_category: str | None) -> bool:
    if reply_category == "Out of Office":
        return True
    if reply_text:
        lower = reply_text.lower()
        return any(phrase in lower for phrase in _OOO_PHRASES)
    return False


@celery_app.task(name="app.tasks.hubspot_sync.sync_to_hubspot")
def sync_to_hubspot():
    from app.database import SessionLocal
    from app.integrations.hubspot import (
        build_activity_summary,
        create_deal,
        create_note,
        upsert_contacts,
    )
    from app.models.campaign_config import CampaignConfig
    from app.models.email_event import EmailEvent
    from app.models.prospect import Prospect
    from app.models.sequence_enrollment import SequenceEnrollment

    db = SessionLocal()
    synced = 0
    try:
        # ── Load all campaign configs once (zero queries per-event) ─────────
        campaign_configs: dict[str, CampaignConfig] = {
            c.smartlead_campaign_id: c
            for c in db.execute(select(CampaignConfig)).scalars().all()
        }
        open_trigger_campaign_ids: set[str] = {
            cid for cid, cfg in campaign_configs.items()
            if cfg.hubspot_trigger_event == "open"
        }

        # ── Fetch unsynced events ────────────────────────────────────────────
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

        # ── Batch-fetch enrollment→campaign mapping for open events ──────────
        # Only needed if any campaign is configured with trigger="open"
        open_enrollment_campaign_map: dict[str, str] = {}
        if open_trigger_campaign_ids:
            open_enrollment_ids = {
                str(e.enrollment_id)
                for e in events
                if e.event_type == "open" and e.enrollment_id
            }
            if open_enrollment_ids:
                enrollments = (
                    db.execute(
                        select(SequenceEnrollment)
                        .where(SequenceEnrollment.id.in_(open_enrollment_ids))
                    )
                    .scalars()
                    .all()
                )
                open_enrollment_campaign_map = {
                    str(e.id): e.smartlead_campaign_id for e in enrollments
                }

        def _is_actionable(event: EmailEvent) -> bool:
            if event.event_type in _NOTE_EVENT_TYPES:
                return True
            if event.event_type == "open" and event.enrollment_id:
                campaign_id = open_enrollment_campaign_map.get(str(event.enrollment_id))
                return campaign_id in open_trigger_campaign_ids
            return False

        actionable_events = [e for e in events if _is_actionable(e)]

        # ── Contact upsert (batch, only for actionable events) ───────────────
        email_to_hubspot_id: dict[str, str] = {}
        prospect_map: dict = {}
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

        # ── Process each event ───────────────────────────────────────────────
        for event in events:
            try:
                if not _is_actionable(event):
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

                # Look up per-campaign config; fall back to defaults if none
                campaign_id_str = None
                enrollment = None
                if event.enrollment_id:
                    if event.event_type == "open":
                        campaign_id_str = open_enrollment_campaign_map.get(str(event.enrollment_id))
                    else:
                        enrollment = db.get(SequenceEnrollment, event.enrollment_id)
                        if enrollment:
                            campaign_id_str = enrollment.smartlead_campaign_id

                cfg = campaign_configs.get(campaign_id_str) if campaign_id_str else None
                trigger = cfg.hubspot_trigger_event if cfg else "reply"
                pipeline_id = cfg.hubspot_pipeline_id if cfg else None
                stage_id = cfg.hubspot_stage_id if cfg else None

                # Note — created for click/reply always; open only when trigger="open"
                note_body = _build_note_body(event)
                create_note(
                    hubspot_contact_id=hubspot_id,
                    note_body=note_body,
                    occurred_at=event.occurred_at,
                )

                # Deal — only when this event matches the configured trigger
                if trigger != "none" and event.event_type == trigger:
                    prospect_name = (
                        " ".join(filter(None, [prospect.first_name, prospect.last_name]))
                        or prospect.email
                    )
                    deal_name = f"WCP Automated Outbound - {prospect_name}"

                    if enrollment is None and event.enrollment_id:
                        enrollment = db.get(SequenceEnrollment, event.enrollment_id)

                    all_events = []
                    if event.enrollment_id:
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

                    # Extract reply text and sequence number from raw webhook payload
                    import json as _json
                    _payload = _json.loads(event.raw_payload) if event.raw_payload else {}
                    reply_text = (_payload.get("reply_message") or {}).get("text", "").strip() or None
                    sequence_number = _payload.get("sequence_number")
                    if event.is_ooo:
                        logger.info(
                            "Skipping HubSpot deal for OOO reply from %s", prospect.email
                        )
                        event.hubspot_synced_at = datetime.now(timezone.utc)
                        db.flush()
                        synced += 1
                        continue

                    description = build_activity_summary(
                        prospect_name=prospect_name,
                        prospect_email=prospect.email,
                        company=prospect.company,
                        campaign_name=enrollment.campaign_name if enrollment else None,
                        track=enrollment.track if enrollment else "unknown",
                        events=event_dicts,
                        reply_text=reply_text,
                        sequence_number=sequence_number,
                    )

                    create_deal(
                        hubspot_contact_id=hubspot_id,
                        deal_name=deal_name,
                        description=description,
                        pipeline_id=pipeline_id,
                        stage_id=stage_id,
                    )
                    logger.info(
                        "Created HubSpot deal '%s' for %s (trigger=%s)",
                        deal_name, prospect.email, trigger,
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
