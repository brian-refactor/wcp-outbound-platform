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


def create_note(
    hubspot_contact_id: str,
    note_body: str,
    occurred_at: "datetime",
) -> str:
    """
    Creates a Note on a HubSpot contact and returns the note id.
    Raises: httpx.HTTPStatusError on API error
    """
    from datetime import datetime

    ts = occurred_at.strftime("%Y-%m-%dT%H:%M:%S.000Z") if hasattr(occurred_at, "strftime") else str(occurred_at)

    payload = {
        "properties": {
            "hs_note_body": note_body,
            "hs_timestamp": ts,
        },
        "associations": [
            {
                "to": {"id": hubspot_contact_id},
                "types": [
                    {
                        "associationCategory": "HUBSPOT_DEFINED",
                        "associationTypeId": 202,  # note -> contact
                    }
                ],
            }
        ],
    }

    with _client() as client:
        resp = client.post("/crm/v3/objects/notes", json=payload)
        resp.raise_for_status()

    note_id = resp.json()["id"]
    logger.info("Created HubSpot note %s for contact %s", note_id, hubspot_contact_id)
    return note_id


def create_deal(
    hubspot_contact_id: str,
    deal_name: str,
    description: str,
    pipeline_id: str | None = None,
    stage_id: str | None = None,
) -> str:
    """
    Creates a Deal in the configured pipeline/stage and associates it to the
    contact inline (no crm.associations.write scope required).

    pipeline_id / stage_id: if None, falls back to global settings values.
    Returns the HubSpot deal id.
    Raises: httpx.HTTPStatusError on API error
    """
    payload = {
        "properties": {
            "dealname": deal_name,
            "pipeline": pipeline_id or settings.hubspot_deal_pipeline_id,
            "dealstage": stage_id or settings.hubspot_deal_stage_id,
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


def get_lists() -> list[dict]:
    """
    Return all HubSpot contact lists as [{id, name, size, dynamic}].
    Uses v1 Contacts API — works with crm.objects.contacts.read scope.
    """
    results = []
    offset = 0
    while True:
        with _client() as client:
            resp = client.get("/contacts/v1/lists", params={"count": 250, "offset": offset})
            resp.raise_for_status()
        data = resp.json()
        for lst in data.get("lists", []):
            results.append({
                "id": str(lst["listId"]),
                "name": lst.get("name", ""),
                "size": lst.get("metaData", {}).get("size", 0),
                "dynamic": lst.get("dynamic", False),
            })
        if not data.get("has-more"):
            break
        offset = data.get("offset", 0)
    return sorted(results, key=lambda x: x["name"].lower())


def get_list_contacts(list_id: str) -> list[dict]:
    """
    Fetch all contacts from a HubSpot list.
    Returns list of dicts with keys: email, first_name, last_name, company, title, phone.
    Contacts with no email are skipped.
    """
    results = []
    vid_offset = None
    props = ["email", "firstname", "lastname", "company", "jobtitle", "phone"]
    while True:
        params: dict = {"count": 100}
        for p in props:
            params.setdefault("property", [])
            if isinstance(params["property"], list):
                params["property"].append(p)
        if vid_offset is not None:
            params["vidOffset"] = vid_offset
        with _client() as client:
            resp = client.get(f"/contacts/v1/lists/{list_id}/contacts/all", params=params)
            resp.raise_for_status()
        data = resp.json()
        for contact in data.get("contacts", []):
            props_data = contact.get("properties", {})
            email = (props_data.get("email", {}).get("value") or "").strip().lower()
            if not email:
                continue
            results.append({
                "email": email,
                "first_name": (props_data.get("firstname", {}).get("value") or "").strip() or None,
                "last_name": (props_data.get("lastname", {}).get("value") or "").strip() or None,
                "company": (props_data.get("company", {}).get("value") or "").strip() or None,
                "title": (props_data.get("jobtitle", {}).get("value") or "").strip() or None,
                "phone": (props_data.get("phone", {}).get("value") or "").strip() or None,
            })
        if not data.get("has-more"):
            break
        vid_offset = data.get("vid-offset")
    return results


def get_deal_pipelines() -> list[dict]:
    """
    Fetch all HubSpot deal pipelines and their stages.

    Returns:
      [{"id": "...", "label": "...", "stages": [{"id": "...", "label": "..."}, ...]}, ...]

    Raises: httpx.HTTPStatusError on API error
    """
    with _client() as client:
        resp = client.get("/crm/v3/pipelines/deals")
        resp.raise_for_status()

    pipelines = []
    for p in resp.json().get("results", []):
        stages = [
            {"id": s["id"], "label": s["label"]}
            for s in sorted(p.get("stages", []), key=lambda s: s.get("displayOrder", 0))
        ]
        pipelines.append({"id": p["id"], "label": p.get("label", p["id"]), "stages": stages})
    return sorted(pipelines, key=lambda p: p["label"].lower())


def build_activity_summary(
    prospect_name: str,
    prospect_email: str,
    company: str | None,
    campaign_name: str | None,
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
        f"Campaign: {campaign_name or 'Unknown'} | Track at reply: {track}",
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
