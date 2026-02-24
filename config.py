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

    # LLM
    gemini_api_key: str
    llm_provider: str = "gemini"          # "gemini" | "claude" | "openai"
    gemini_model: str = "gemini-2.5-flash"  # override via GEMINI_MODEL in .env

    # App
    log_level: str = "INFO"
    environment: str = "development"

    @property
    def pubsub_topic_path(self) -> str:
        return f"projects/{self.gcp_project_id}/topics/{self.pubsub_topic}"

    @property
    def pubsub_subscription_path(self) -> str:
        return f"projects/{self.gcp_project_id}/subscriptions/{self.pubsub_subscription}"


settings = Settings()
