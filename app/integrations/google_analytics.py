"""
Google Analytics 4 Data API client.

Pulls session counts broken down by utm_campaign for email traffic
(utm_source=outbound, utm_medium=email). Campaign names follow the convention
sl{smartlead_campaign_id} (e.g. sl3229383), so stripping the sl prefix gives
a campaign ID that matches sequence_enrollments.smartlead_campaign_id.

Requires two env vars:
  GOOGLE_ANALYTICS_PROPERTY_ID   — numeric GA4 property ID
  GOOGLE_ANALYTICS_CREDENTIALS_JSON — full service account JSON key
"""

import json
import logging

from app.config import settings

logger = logging.getLogger(__name__)


def get_email_sessions_by_campaign() -> dict[str, int]:
    """
    Return {campaign_id: session_count} for GA4 sessions where
    utm_source=outbound and utm_medium=email, grouped by utm_campaign.

    Returns {} if credentials are not configured or the API call fails.
    The caller should treat a missing campaign ID as 0 / not yet tracked.
    """
    if not settings.google_analytics_property_id or not settings.google_analytics_credentials_json:
        return {}

    try:
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        from google.analytics.data_v1beta.types import (
            DateRange,
            Dimension,
            DimensionFilter,
            Filter,
            FilterExpression,
            FilterExpressionList,
            Metric,
            RunReportRequest,
        )
        from google.oauth2 import service_account

        credentials = service_account.Credentials.from_service_account_info(
            json.loads(settings.google_analytics_credentials_json),
            scopes=["https://www.googleapis.com/auth/analytics.readonly"],
        )
        client = BetaAnalyticsDataClient(credentials=credentials)

        request = RunReportRequest(
            property=f"properties/{settings.google_analytics_property_id}",
            dimensions=[Dimension(name="sessionCampaignName")],
            metrics=[Metric(name="sessions")],
            date_ranges=[DateRange(start_date="2024-01-01", end_date="today")],
            dimension_filter=FilterExpression(
                and_group=FilterExpressionList(
                    expressions=[
                        FilterExpression(
                            filter=Filter(
                                field_name="sessionSource",
                                string_filter=Filter.StringFilter(
                                    value="outbound",
                                    match_type=Filter.StringFilter.MatchType.EXACT,
                                    case_sensitive=False,
                                ),
                            )
                        ),
                        FilterExpression(
                            filter=Filter(
                                field_name="sessionMedium",
                                string_filter=Filter.StringFilter(
                                    value="email",
                                    match_type=Filter.StringFilter.MatchType.EXACT,
                                    case_sensitive=False,
                                ),
                            )
                        ),
                    ]
                )
            ),
        )

        response = client.run_report(request)

        results: dict[str, int] = {}
        for row in response.rows:
            campaign_name = row.dimension_values[0].value  # e.g. "sl3229383"
            sessions = int(row.metric_values[0].value)
            # Strip the sl prefix to get the raw Smartlead campaign ID
            if campaign_name.startswith("sl"):
                campaign_id = campaign_name[2:]
                results[campaign_id] = results.get(campaign_id, 0) + sessions

        logger.info("GA4: fetched session counts for %d campaigns", len(results))
        return results

    except Exception as e:
        logger.warning("GA4 session fetch failed — hiding GA column: %s", e)
        return {}
