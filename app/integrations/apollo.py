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

PER_PAGE = 25


def search_people(
    keywords: str = "",
    titles: list[str] | None = None,
    locations: list[str] | None = None,
    page: int = 1,
) -> tuple[list[dict], int]:
    """
    Search Apollo's people database by keyword, title, and location.

    Returns (results, total_entries).
    Each result dict has: first_name, last_name, name, title, company,
    city, state, linkedin_url, email (may be None).
    """
    if not settings.apollo_api_key:
        return [], 0

    payload: dict = {
        "page": page,
        "per_page": PER_PAGE,
    }
    if keywords:
        payload["q_keywords"] = keywords
    if titles:
        payload["person_titles"] = titles
    if locations:
        payload["person_locations"] = locations

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                f"{APOLLO_BASE_URL}/mixed_people/search",
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Cache-Control": "no-cache",
                    "X-Api-Key": settings.apollo_api_key,
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning("Apollo people search failed: %s", e)
        return [], 0

    people = data.get("people") or []
    total = (data.get("pagination") or {}).get("total_entries", len(people))

    results = []
    for p in people:
        phone = None
        phones = p.get("phone_numbers") or []
        if phones:
            phone = phones[0].get("sanitized_number") or phones[0].get("raw_number")

        org = p.get("organization") or {}
        results.append({
            "first_name": p.get("first_name") or "",
            "last_name": p.get("last_name") or "",
            "name": p.get("name") or "",
            "title": p.get("title") or "",
            "company": org.get("name") or p.get("employment_history", [{}])[0].get("organization_name", ""),
            "city": p.get("city") or "",
            "state": p.get("state") or "",
            "linkedin_url": p.get("linkedin_url") or "",
            "email": p.get("email") or None,
            "phone": phone,
        })

    return results, total


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
                headers={
                    "Content-Type": "application/json",
                    "Cache-Control": "no-cache",
                    "X-Api-Key": settings.apollo_api_key,
                },
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
