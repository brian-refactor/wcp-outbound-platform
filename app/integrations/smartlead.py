"""
Smartlead API client.

Handles prospect enrollment into campaigns and inbox status checks.
All calls are synchronous — called from Celery tasks or admin endpoints.
"""

import logging
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

SMARTLEAD_BASE_URL = "https://server.smartlead.ai/api/v1"


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=SMARTLEAD_BASE_URL,
        params={"api_key": settings.smartlead_api_key},
        timeout=30.0,
    )


def get_lead_by_email(email: str) -> Optional[dict]:
    """Return the Smartlead lead record for an email, or None if not found."""
    with _client() as client:
        response = client.get("/leads", params={"email": email.strip().lower()})
        if not response.is_success:
            return None
        data = response.json()
        if isinstance(data, list):
            return data[0] if data else None
        return data if data else None


def is_lead_in_campaign(email: str, campaign_id: int) -> bool:
    """
    Return True if email is already a lead in the campaign.
    Fails open (returns False) on API errors so enrollment is never incorrectly blocked.
    """
    lead = get_lead_by_email(email)
    if not lead:
        return False
    lead_id = lead.get("id")
    if not lead_id:
        return False
    try:
        with _client() as client:
            response = client.get(f"/leads/{lead_id}/all-campaign")
            if not response.is_success:
                return False
            campaigns = response.json()
            if isinstance(campaigns, list):
                return any(str(c.get("campaign_id", "")) == str(campaign_id) for c in campaigns)
            return False
    except Exception:
        return False


def enroll_prospect(
    campaign_id: int,
    email: str,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    custom_fields: Optional[dict] = None,
) -> dict:
    """
    Add a prospect (lead) to a Smartlead campaign.

    Returns the Smartlead API response dict.
    Raises httpx.HTTPStatusError on API errors.
    Raises ValueError if the lead is already enrolled in this campaign.
    """
    if is_lead_in_campaign(email, campaign_id):
        raise ValueError(f"{email} is already enrolled in campaign {campaign_id}")

    payload = {
        "lead_list": [
            {
                "email": email,
                "first_name": first_name or "",
                "last_name": last_name or "",
                **({"custom_fields": custom_fields} if custom_fields else {}),
            }
        ]
    }

    with _client() as client:
        response = client.post(f"/campaigns/{campaign_id}/leads", json=payload)
        if not response.is_success:
            logger.error(
                "Smartlead enrollment failed for %s in campaign %s: %s %s — body: %s",
                email, campaign_id, response.status_code, response.reason_phrase, response.text,
            )
        response.raise_for_status()
        result = response.json()
        logger.info(
            "Enrolled %s in Smartlead campaign %s: %s", email, campaign_id, result
        )
        return result


def get_all_campaign_lead_emails(campaign_id: int) -> set[str]:
    """
    Return the lowercase email addresses of every lead already in the campaign.
    Used by bulk enroll to filter duplicates in one paginated fetch instead of
    checking each prospect individually.
    """
    emails: set[str] = set()
    offset = 0
    with _client() as client:
        while True:
            response = client.get(
                f"/campaigns/{campaign_id}/leads",
                params={"limit": 100, "offset": offset},
            )
            if not response.is_success:
                logger.warning("Could not fetch campaign leads for dedup check: %s", response.status_code)
                break
            data = response.json()
            leads = data.get("data", [])
            for lead in leads:
                email = (lead.get("lead") or {}).get("email", "").lower().strip()
                if email:
                    emails.add(email)
            if len(leads) < 100:
                break
            offset += 100
    logger.info("Fetched %d existing emails from campaign %s for dedup", len(emails), campaign_id)
    return emails


ENROLL_BATCH_SIZE = 100


def enroll_prospects_batch(campaign_id: int, leads: list[dict]) -> list[dict]:
    """
    Enroll multiple leads in a single Smartlead API call.

    Each item in `leads` should have: email, first_name, last_name, custom_fields (optional).
    Sends in chunks of ENROLL_BATCH_SIZE. The caller must pre-filter duplicates.
    Returns the list of Smartlead response dicts (one per chunk).
    """
    if not leads:
        return []
    responses = []
    with _client() as client:
        for i in range(0, len(leads), ENROLL_BATCH_SIZE):
            chunk = leads[i:i + ENROLL_BATCH_SIZE]
            payload = {
                "lead_list": [
                    {
                        "email": lead["email"],
                        "first_name": lead.get("first_name") or "",
                        "last_name": lead.get("last_name") or "",
                        **({"custom_fields": lead["custom_fields"]} if lead.get("custom_fields") else {}),
                    }
                    for lead in chunk
                ]
            }
            response = client.post(f"/campaigns/{campaign_id}/leads", json=payload)
            if not response.is_success:
                logger.error(
                    "Smartlead batch enrollment failed for campaign %s (chunk %d): %s — %s",
                    campaign_id, i // ENROLL_BATCH_SIZE, response.status_code, response.text,
                )
            response.raise_for_status()
            result = response.json()
            logger.info("Batch enrolled %d leads in campaign %s: %s", len(chunk), campaign_id, result)
            responses.append(result)
    return responses


def get_campaign_details(campaign_id: int) -> dict:
    """Fetch campaign details — useful for verifying a campaign ID is valid."""
    with _client() as client:
        response = client.get(f"/campaigns/{campaign_id}")
        response.raise_for_status()
        return response.json()


def list_campaigns() -> list[dict]:
    """
    Fetch all campaigns from Smartlead.
    Returns a list of dicts with at minimum 'id' and 'name'.
    """
    with _client() as client:
        response = client.get("/campaigns")
        response.raise_for_status()
        return response.json()


def list_email_accounts() -> list[dict]:
    """
    Fetch all connected email accounts and their warm-up status.
    Used by the admin inbox status dashboard.
    """
    with _client() as client:
        response = client.get("/email-accounts")
        response.raise_for_status()
        return response.json()


# Smartlead's fixed AI lead category IDs → human-readable names.
# Confirmed from webhooks: 3 = Not Interested, 4 = Do Not Contact, 6 = Out of Office.
CATEGORY_NAMES: dict[int, str] = {
    1: "Interested",
    2: "Meeting Booked",
    3: "Not Interested",
    4: "Do Not Contact",
    5: "Wrong Person",
    6: "Out of Office",
    7: "Unqualified",
    8: "Follow Up",
}


def get_campaign_lead_categories(campaign_id: int) -> dict[str, str | None]:
    """
    Return {email_lower: category_name} for every lead in the campaign.
    Leads with no category set have value None.
    """
    results: dict[str, str | None] = {}
    offset = 0
    with _client() as client:
        while True:
            response = client.get(
                f"/campaigns/{campaign_id}/leads",
                params={"limit": 100, "offset": offset},
            )
            response.raise_for_status()
            data = response.json()
            leads = data.get("data", [])
            for lead in leads:
                email = (lead.get("lead") or {}).get("email", "").lower().strip()
                category_id = lead.get("lead_category_id")
                if email:
                    results[email] = CATEGORY_NAMES.get(category_id) if category_id else None
            if len(leads) < 100:
                break
            offset += 100
    logger.info("Fetched categories for %d leads in campaign %s", len(results), campaign_id)
    return results
