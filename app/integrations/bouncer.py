"""
Bouncer email validation client.

Docs: https://docs.usebouncer.com/api-reference/batch-sync/batch-sync
Endpoint: POST https://api.usebouncer.com/v1.1/email/verify/batch/sync
  - Synchronous — blocks until results are ready
  - Max 10,000 emails per request
  - Auth: x-api-key header

Status mapping to our internal values:
  deliverable   → valid
  undeliverable → invalid
  risky         → invalid  (low quality / catch-all — blocked from enrollment)
  unknown       → unknown
"""

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

BATCH_URL = "https://api.usebouncer.com/v1.1/email/verify/batch/sync"
CREDITS_URL = "https://api.usebouncer.com/v1.1/credits"
MAX_BATCH = 10_000


def get_credits() -> int:
    if not settings.bouncer_api_key:
        return -1
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.get(
                CREDITS_URL,
                headers={"x-api-key": settings.bouncer_api_key},
            )
            response.raise_for_status()
            data = response.json()
            return int(data.get("credits", -1))
    except Exception as e:
        logger.warning("Could not fetch Bouncer credits: %s", e)
        return -1


def validate_batch(emails: list[str]) -> dict[str, str]:
    """
    Validate up to MAX_BATCH email addresses in one synchronous API call.

    Returns:
        dict mapping email → normalised status: "valid" | "invalid" | "unknown"
    """
    if not settings.bouncer_api_key:
        raise ValueError("BOUNCER_API_KEY is not configured")

    if not emails:
        return {}

    with httpx.Client(timeout=120.0) as client:
        response = client.post(
            BATCH_URL,
            headers={"x-api-key": settings.bouncer_api_key, "Content-Type": "application/json"},
            json=emails[:MAX_BATCH],
        )
        response.raise_for_status()
        data = response.json()

    results: dict[str, str] = {}
    for item in data:
        email = (item.get("email") or "").strip().lower()
        raw_status = (item.get("status") or "unknown").lower()

        if raw_status == "deliverable":
            status = "valid"
        elif raw_status in ("undeliverable", "risky"):
            status = "invalid"
        else:
            status = "unknown"

        if email:
            results[email] = status

    logger.info("Bouncer validated %d emails", len(results))
    return results
