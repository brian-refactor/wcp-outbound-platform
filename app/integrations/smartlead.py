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
    """
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
