"""
Bouncer email validation client.

Docs: https://docs.usebouncer.com/api-reference/batch-sync/batch-sync
Endpoint: POST https://api.usebouncer.com/v1.1/email/verify/batch/sync
  - Synchronous — blocks until results are ready
  - Max 10,000 emails per request
  - Auth: x-api-key header

Status mapping to our internal values (uses status + toxicity score):
  deliverable + toxicity 0–5  → valid
  deliverable + toxicity > 5  → catch-all  (deliverable but risky sender reputation)
  risky                       → catch-all  (catch-all domain, may or may not deliver)
  undeliverable               → invalid
  unknown                     → unknown

Usage:
  - Single email or tiny list  → validate_batch(emails)
  - Any bulk operation          → validate_all(emails)
    validate_all splits into SMALL_BATCH_SIZE chunks with a 1s delay between
    requests so Bouncer has enough time to verify each address accurately.
    Sending large batches in one request causes Bouncer to return 'unknown'
    for addresses it can't check within its internal timeout.
"""

import logging
import time

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

BATCH_URL = "https://api.usebouncer.com/v1.1/email/verify/batch/sync"
CREDITS_URL = "https://api.usebouncer.com/v1.1/credits"
MAX_BATCH = 10_000
SMALL_BATCH_SIZE = 20


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
        toxicity = int(item.get("toxicity") or 0)

        if raw_status == "deliverable":
            status = "valid" if toxicity <= 5 else "catch-all"
        elif raw_status == "risky":
            status = "catch-all"
        elif raw_status == "undeliverable":
            status = "invalid"
        else:
            status = "unknown"

        if email:
            results[email] = status

    logger.info("Bouncer validated %d emails", len(results))
    return results


def validate_all(emails: list[str]) -> dict[str, str]:
    """
    Validate any number of emails using small batches with a delay between
    requests. Use this for all bulk operations.
    """
    if not emails:
        return {}
    results: dict[str, str] = {}
    for i in range(0, len(emails), SMALL_BATCH_SIZE):
        batch = emails[i:i + SMALL_BATCH_SIZE]
        results.update(validate_batch(batch))
        if i + SMALL_BATCH_SIZE < len(emails):
            time.sleep(1)
    return results
