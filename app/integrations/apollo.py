"""
Apollo.io contact enrichment.

Uses the People Match API to look up a person by name + company and return
their email, LinkedIn URL, title, and phone.
"""
import logging
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

APOLLO_BASE_URL = "https://api.apollo.io/v1"


def enrich_person(
    first_name: str,
    last_name: str,
    organization_name: str,
) -> Optional[dict]:
    """
    Look up a person by name + company via Apollo People Match.

    Returns a dict with any of: email, linkedin_url, title, phone,
    city, state, company. Returns None if not found or API unavailable.
    """
    if not settings.apollo_api_key:
        return None

    payload = {
        "api_key": settings.apollo_api_key,
        "first_name": first_name,
        "last_name": last_name,
        "organization_name": organization_name,
        "reveal_personal_emails": False,
    }

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                f"{APOLLO_BASE_URL}/people/match",
                json=payload,
                headers={"Content-Type": "application/json", "Cache-Control": "no-cache"},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning("Apollo enrich failed for %s %s: %s", first_name, last_name, e)
        return None

    person = data.get("person")
    if not person:
        return None

    phone = None
    phones = person.get("phone_numbers") or []
    if phones:
        phone = phones[0].get("sanitized_number") or phones[0].get("raw_number")

    employment = person.get("employment_history") or []
    current_company = None
    for job in employment:
        if job.get("current"):
            current_company = job.get("organization_name")
            break

    return {
        "email": person.get("email") or None,
        "linkedin_url": person.get("linkedin_url") or None,
        "title": person.get("title") or None,
        "phone": phone,
        "city": person.get("city") or None,
        "state": person.get("state") or None,
        "company": current_company or organization_name,
    }
