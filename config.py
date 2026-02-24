from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # GCP
    gcp_project_id: str = "aperture-prod-20260221"
    firestore_database: str = "aperture-db"
    pubsub_topic: str = "aperture-gmail-push"
    pubsub_subscription: str = "aperture-gmail-push-sub"

    # Telegram
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_webhook_secret: str = ""  # set via TELEGRAM_WEBHOOK_SECRET in .env

    # LLM
    gemini_api_key: str
    llm_provider: str = "gemini"          # "gemini" | "claude" | "openai"
    gemini_model: str = "gemini-2.5-flash"  # override via GEMINI_MODEL in .env

    # Cloud Run (set after first deployment)
    cloud_run_url: str = ""  # e.g. https://aperture-xxxx-uc.a.run.app

    # Internal endpoint security
    internal_secret: str = ""  # set via INTERNAL_SECRET in .env

    # App
    timezone: str = "America/New_York"  # for digest schedule display
    log_level: str = "INFO"
    environment: str = "development"

    @property
    def pubsub_topic_path(self) -> str:
        return f"projects/{self.gcp_project_id}/topics/{self.pubsub_topic}"

    @property
    def pubsub_subscription_path(self) -> str:
        return f"projects/{self.gcp_project_id}/subscriptions/{self.pubsub_subscription}"


settings = Settings()
