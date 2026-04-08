"""
HubSpot integration — private app token auth.

Contacts are upserted by email (batch, up to 100 per call).
Email events are logged as Notes associated to the contact.
"""

import logging
from datetime import datetime

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.hubapi.com"


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

    Returns: dict mapping email (lowercased) -> HubSpot contact id string
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
    event_type: str,
    email_subject: str | None,
    domain_used: str | None,
    clicked_url: str | None,
    occurred_at: datetime,
) -> str:
    """
    Creates a HubSpot note and associates it to the contact.

    Returns the HubSpot note id.
    Raises: httpx.HTTPStatusError on API error
    """
    parts = [f"Email event: {event_type}"]
    if email_subject:
        parts.append(f"Subject: {email_subject}")
    if domain_used:
        parts.append(f"Domain: {domain_used}")
    if clicked_url:
        parts.append(f"URL: {clicked_url}")

    body = " | ".join(parts)
    timestamp = occurred_at.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    with _client() as client:
        resp = client.post(
            "/crm/v3/objects/notes",
            json={"properties": {"hs_note_body": body, "hs_timestamp": timestamp}},
        )
        resp.raise_for_status()
        note_id = resp.json()["id"]

        # Associate note to contact
        assoc = client.put(
            f"/crm/v4/objects/notes/{note_id}/associations/contacts/{hubspot_contact_id}/default"
        )
        assoc.raise_for_status()

    return note_id
