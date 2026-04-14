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
    employee_ranges: list[str] | None = None,
    revenue_ranges: list[str] | None = None,
    industries: list[str] | None = None,
    has_email: bool = False,
    page: int = 1,
) -> tuple[list[dict], int]:
    """
    Search Apollo's people database.

    Returns (results, total_entries).
    employee_ranges: list of "min,max" strings e.g. ["1,10", "11,50"]
    industries: list of keyword tags e.g. ["financial services", "real estate"]
    has_email: if True, only return contacts Apollo has emails for
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
    if employee_ranges:
        payload["organization_num_employees_ranges"] = employee_ranges
    if revenue_ranges:
        payload["organization_revenue_ranges"] = revenue_ranges
    if industries:
        payload["q_organization_keyword_tags"] = industries
    if has_email:
        payload["contact_email_status"] = ["verified", "likely to engage"]

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                f"{APOLLO_BASE_URL}/mixed_people/api_search",
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

    if "error" in data:
        logger.warning("Apollo people search error response: %s", data["error"])
        raise RuntimeError(data["error"])

    people = data.get("people") or []
    total = data.get("total_entries", len(people))

    results = []
    for p in people:
        org = p.get("organization") or {}
        first_name = p.get("first_name") or ""
        last_name_obfuscated = p.get("last_name_obfuscated") or ""
        results.append({
            "apollo_id": p.get("id") or "",
            "first_name": first_name,
            "last_name": last_name_obfuscated,
            "name": f"{first_name} {last_name_obfuscated}".strip(),
            "title": p.get("title") or "",
            "company": org.get("name") or "",
            "has_email": p.get("has_email") or False,
            "city": "",
            "state": "",
            "linkedin_url": "",
            "email": None,
            "phone": None,
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
