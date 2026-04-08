from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/wcp_outbound"
    environment: str = "development"

    # Smartlead
    smartlead_api_key: str = ""
    smartlead_webhook_secret: str = ""  # shared secret to verify incoming webhooks

    # Redis (Celery broker + result backend)
    redis_url: str = "redis://localhost:6379/0"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
