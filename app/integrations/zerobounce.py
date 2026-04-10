"""
ZeroBounce email validation client.

Docs: https://www.zerobounce.net/docs/email-validation-api-quickstart/
Batch endpoint: POST https://bulkapi.zerobounce.net/v2/validatebatch
  - Max 200 emails per request
  - Returns per-email status: valid | invalid | catch-all | unknown |
    spamtrap | abuse | do_not_mail | disposable

Status mapping to our internal values:
  valid        → valid
  catch-all    → catch-all   (domain accepts all — deliverability uncertain)
  unknown      → unknown
  invalid      → invalid
  spamtrap     → invalid
  abuse        → invalid
  do_not_mail  → invalid
  disposable   → invalid
"""

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

BATCH_URL = "https://bulkapi.zerobounce.net/v2/validatebatch"
MAX_BATCH = 200

# Statuses that map to our "invalid" bucket
INVALID_STATUSES = {"invalid", "spamtrap", "abuse", "do_not_mail", "disposable"}


def get_credits() -> int:
    """
    Return remaining ZeroBounce credits for the account.
    Returns -1 if the API key is not configured or the call fails.
    """
    if not settings.zerobounce_api_key:
        return -1
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.get(
                "https://api.zerobounce.net/v2/getcredits",
                params={"api_key": settings.zerobounce_api_key},
            )
            response.raise_for_status()
            data = response.json()
            return int(data.get("Credits", -1))
    except Exception as e:
        logger.warning("Could not fetch ZeroBounce credits: %s", e)
        return -1


def validate_batch(emails: list[str]) -> dict[str, str]:
    """
    Validate up to MAX_BATCH email addresses in one API call.

    Args:
        emails: list of email address strings (max 200)

    Returns:
        dict mapping email → normalised status string:
        "valid" | "invalid" | "catch-all" | "unknown"

    Raises:
        httpx.HTTPStatusError on non-2xx responses.
        ValueError if no API key is configured.
    """
    if not settings.zerobounce_api_key:
        raise ValueError("ZEROBOUNCE_API_KEY is not configured")

    if not emails:
        return {}

    payload = {
        "api_key": settings.zerobounce_api_key,
        "email_batch": [{"email_address": e} for e in emails[:MAX_BATCH]],
    }

    with httpx.Client(timeout=60.0) as client:
        response = client.post(BATCH_URL, json=payload)
        response.raise_for_status()
        data = response.json()

    results: dict[str, str] = {}
    for item in data.get("email_batch", []):
        email = (item.get("address") or "").strip().lower()
        raw_status = (item.get("status") or "unknown").lower()

        if raw_status in INVALID_STATUSES:
            status = "invalid"
        elif raw_status == "catch-all":
            status = "catch-all"
        elif raw_status == "valid":
            status = "valid"
        else:
            status = "unknown"

        if email:
            results[email] = status

    logger.info("ZeroBounce validated %d emails", len(results))
    return results
