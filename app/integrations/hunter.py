"""
Hunter.io email finder — used as a fallback when Apollo People Match returns no email.

Uses the Email Finder endpoint: given a first name, last name, and company name,
Hunter attempts to find a verified work email address.
"""
import logging
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

HUNTER_BASE_URL = "https://api.hunter.io/v2"


def find_email(first_name: str, last_name: str, company: str) -> Optional[dict]:
    """
    Look up a work email via Hunter.io Email Finder.

    Returns a dict with email and confidence score, or None if not found.
    """
    if not settings.hunter_api_key:
        return None

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(
                f"{HUNTER_BASE_URL}/email-finder",
                params={
                    "first_name": first_name,
                    "last_name": last_name,
                    "company": company,
                    "api_key": settings.hunter_api_key,
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning("Hunter.io lookup failed for %s %s: %s", first_name, last_name, e)
        return None

    result = (data.get("data") or {})
    email = result.get("email")
    if not email:
        return None

    return {
        "email": email,
        "confidence": result.get("score", 0),
        "sources": len(result.get("sources") or []),
    }
