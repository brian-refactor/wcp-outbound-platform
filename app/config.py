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
    hubspot_deal_pipeline_id: str = "default"       # WCP Deals pipeline
    hubspot_deal_stage_id: str = "1114790192"       # Prospect Outreach stage

    # ZeroBounce
    zerobounce_api_key: str = ""  # from zerobounce.net account

    # Anthropic (Claude API — personalized email intros)
    anthropic_api_key: str = ""

    # Apollo.io (contact enrichment)
    apollo_api_key: str = ""

    # Hunter.io (email finder — fallback when Apollo returns no email)
    hunter_api_key: str = ""

    # API authentication
    api_key: str = ""  # X-API-Key header value; empty string disables auth in dev

    # Dashboard login
    dashboard_username: str = "admin"
    dashboard_password: str = ""        # required in production; empty disables auth in dev
    session_secret: str = "dev-secret-change-in-production"  # signs session cookie

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
