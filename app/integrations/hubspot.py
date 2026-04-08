"""
HubSpot integration — private app token auth.

Contacts are upserted by email (batch, up to 100 per call).
When a prospect replies, a Deal is created in the configured pipeline/stage
with the full outbound email history embedded in the description.
"""

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.hubapi.com"

# Deal-to-contact association type (HubSpot built-in, no extra scope needed)
_DEAL_TO_CONTACT_ASSOC_TYPE = 3


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=_BASE_URL,
        headers={"Authorization": f"Bearer {settings.hubspot_access_token}"},
        timeout=30,
    )


def upsert_contacts(prospects: list[dict]) -> dict[str, str]:
    """
    Batch upsert up to 100 contacts by email.

    prospects: list of dicts with keys email, first_name, last_name,
               company, title, phone (all optional except email)

    Returns: dict mapping email -> HubSpot contact id string
    Raises: httpx.HTTPStatusError on API error
    """
    inputs = [
        {
            "idProperty": "email",
            "id": p["email"],
            "properties": {
                "email": p["email"],
                "firstname": p.get("first_name") or "",
                "lastname": p.get("last_name") or "",
                "company": p.get("company") or "",
                "jobtitle": p.get("title") or "",
                "phone": p.get("phone") or "",
            },
        }
        for p in prospects
    ]

    with _client() as client:
        resp = client.post(
            "/crm/v3/objects/contacts/batch/upsert",
            json={"inputs": inputs},
        )
        resp.raise_for_status()

    results = resp.json().get("results", [])
    return {
        r["properties"]["email"]: r["id"]
        for r in results
        if r.get("properties", {}).get("email")
    }


def create_deal(
    hubspot_contact_id: str,
    deal_name: str,
    description: str,
) -> str:
    """
    Creates a Deal in the configured pipeline/stage and associates it to the
    contact inline (no crm.associations.write scope required).

    Returns the HubSpot deal id.
    Raises: httpx.HTTPStatusError on API error
    """
    payload = {
        "properties": {
            "dealname": deal_name,
            "pipeline": settings.hubspot_deal_pipeline_id,
            "dealstage": settings.hubspot_deal_stage_id,
            "description": description,
        },
        "associations": [
            {
                "to": {"id": hubspot_contact_id},
                "types": [
                    {
                        "associationCategory": "HUBSPOT_DEFINED",
                        "associationTypeId": _DEAL_TO_CONTACT_ASSOC_TYPE,
                    }
                ],
            }
        ],
    }

    with _client() as client:
        resp = client.post("/crm/v3/objects/deals", json=payload)
        resp.raise_for_status()

    deal_id = resp.json()["id"]
    logger.info("Created HubSpot deal %s (%s) for contact %s", deal_id, deal_name, hubspot_contact_id)
    return deal_id


def build_activity_summary(
    prospect_name: str,
    prospect_email: str,
    company: str | None,
    sequence_type: str,
    track: str,
    events: list[dict],
) -> str:
    """
    Formats the full outbound email history into a deal description string.

    events: list of dicts with keys event_type, occurred_at, email_subject,
            domain_used, clicked_url (all from EmailEvent rows)
    """
    lines = [
        f"Outbound sequence reply — {prospect_name} <{prospect_email}>",
        f"Company: {company or 'Unknown'}",
        f"Sequence: {sequence_type} | Track at reply: {track}",
        "",
        "── Email Activity History ──",
    ]

    EVENT_LABELS = {
        "sent": "📤 Sent",
        "open": "👁  Opened",
        "click": "🔗 Clicked",
        "reply": "💬 Replied",
        "bounce": "⚠️  Bounced",
        "unsubscribe": "🚫 Unsubscribed",
        "complete": "✅ Sequence Complete",
    }

    for ev in sorted(events, key=lambda e: e["occurred_at"]):
        occurred = ev["occurred_at"]
        # Format datetime regardless of tzinfo
        ts = occurred.strftime("%Y-%m-%d %H:%M UTC") if hasattr(occurred, "strftime") else str(occurred)
        label = EVENT_LABELS.get(ev["event_type"], ev["event_type"].upper())
        parts = [f"{ts}  {label}"]
        if ev.get("email_subject"):
            parts.append(f'"{ev["email_subject"]}"')
        if ev.get("domain_used"):
            parts.append(f"via {ev['domain_used']}")
        if ev.get("clicked_url"):
            parts.append(f"→ {ev['clicked_url']}")
        lines.append("  " + " | ".join(parts))

    # Summary stats
    counts = {}
    for ev in events:
        counts[ev["event_type"]] = counts.get(ev["event_type"], 0) + 1

    lines += [
        "",
        "── Stats ──",
        f"  Sent: {counts.get('sent', 0)} | "
        f"Opens: {counts.get('open', 0)} | "
        f"Clicks: {counts.get('click', 0)}",
    ]

    return "\n".join(lines)
