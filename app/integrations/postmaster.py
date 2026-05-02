"""
Google Postmaster Tools API client.

Pulls domain reputation, spam rates, and authentication pass rates for
sending domains. Requires:
  GOOGLE_ANALYTICS_CREDENTIALS_JSON — service account JSON (shared with GA4)
  GOOGLE_POSTMASTER_DOMAINS         — comma-separated list of sending domains

The service account must be added as a Viewer in Postmaster Tools:
  https://postmaster.google.com → Settings → Users → Add service account email

The Gmail Postmaster Tools API must also be enabled in the Google Cloud project
that owns the service account.
"""

import json
import logging

from app.config import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://gmailpostmastertools.googleapis.com/v1"

_REPUTATION_ORDER = ["HIGH", "MEDIUM", "LOW", "BAD"]


def get_domain_stats() -> list[dict]:
    """
    Returns one dict per configured domain with the most recent day's stats.

    Each dict has keys:
      domain, reputation, spam_rate, spf_pass_rate, dkim_pass_rate,
      dmarc_pass_rate, delivery_errors, date

    Returns [] if not configured or the API call fails.
    """
    if not settings.google_postmaster_domains or not settings.google_analytics_credentials_json:
        return []

    try:
        from google.oauth2 import service_account
        import google.auth.transport.requests

        credentials = service_account.Credentials.from_service_account_info(
            json.loads(settings.google_analytics_credentials_json),
            scopes=["https://www.googleapis.com/auth/postmaster.readonly"],
        )
        session = google.auth.transport.requests.AuthorizedSession(credentials)

        domains = [d.strip() for d in settings.google_postmaster_domains.split(",") if d.strip()]
        results = []

        for domain in domains:
            try:
                resp = session.get(
                    f"{BASE_URL}/domains/{domain}/trafficStats",
                    params={"pageSize": 7},  # last 7 days; most recent is first
                )
                if resp.status_code == 404:
                    # Domain not yet registered in Postmaster Tools
                    results.append({"domain": domain, "error": "not_registered"})
                    continue
                resp.raise_for_status()
                data = resp.json()
                traffic_stats = data.get("trafficStats", [])
                if not traffic_stats:
                    results.append({"domain": domain, "error": "no_data"})
                    continue

                stat = traffic_stats[0]  # most recent day
                date_parts = stat.get("name", "").split("/")[-1]  # e.g. "2026/04/29"
                date_str = date_parts.replace("/", "-") if date_parts else "unknown"

                results.append({
                    "domain": domain,
                    "reputation": stat.get("domainReputation", "REPUTATION_CATEGORY_UNSPECIFIED"),
                    "spam_rate": stat.get("userReportedSpamRatio") or stat.get("spamRate") or 0.0,
                    "spf_pass_rate": stat.get("spfSuccessRatio", None),
                    "dkim_pass_rate": stat.get("dkimSuccessRatio", None),
                    "dmarc_pass_rate": stat.get("dmarcSuccessRatio", None),
                    "delivery_errors": stat.get("deliveryErrors", []),
                    "date": date_str,
                    "error": None,
                })

            except Exception as domain_err:
                logger.warning("Postmaster Tools: failed to fetch stats for %s: %s", domain, domain_err)
                results.append({"domain": domain, "error": str(domain_err)})

        logger.info("Postmaster Tools: fetched stats for %d domains", len(results))
        return results

    except Exception as e:
        logger.warning("Postmaster Tools: client setup failed: %s", e)
        return []
