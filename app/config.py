from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/wcp_outbound"
    environment: str = "development"

    # Smartlead
    smartlead_api_key: str = ""
    smartlead_webhook_secret: str = ""  # shared secret to verify incoming webhooks

    # Redis (Celery broker + result backend)
    redis_url: str = "redis://localhost:6379/0"

    # HubSpot
    hubspot_access_token: str = ""  # private app token
    hubspot_deal_pipeline_id: str = "890766156"      # Outbound - Cold Leads pipeline
    hubspot_deal_stage_id: str = "1341410439"       # New Lead to Contact stage

    # Bouncer
    bouncer_api_key: str = ""  # from usebouncer.com account

    # Anthropic (Claude API — personalized email intros)
    anthropic_api_key: str = ""

    # Apollo.io (contact enrichment)
    apollo_api_key: str = ""

    # Hunter.io (email finder — fallback when Apollo returns no email)
    hunter_api_key: str = ""

    # Google Analytics 4 (optional — enables GA-verified sessions column on Sequences page)
    google_analytics_property_id: str = ""   # numeric ID from GA4 Admin → Property Settings
    google_analytics_credentials_json: str = ""  # full service account JSON key content

    # Google Postmaster Tools (optional — enables /dashboard/deliverability page)
    # Comma-separated list of sending domains to monitor, e.g. "willowcreekinvest.com,wcpinvestors.com"
    # Reuses google_analytics_credentials_json service account (different scope)
    google_postmaster_domains: str = ""

    # API authentication
    api_key: str = ""  # X-API-Key header value; empty string disables auth in dev

    # Dashboard login
    dashboard_username: str = "admin"
    dashboard_password: str = ""        # required in production; empty disables auth in dev
    session_secret: str = "dev-secret-change-in-production"  # signs session cookie

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
