from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/wcp_outbound"
    environment: str = "development"

    # Smartlead
    smartlead_api_key: str = ""
    smartlead_webhook_secret: str = ""  # shared secret to verify incoming webhooks

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
