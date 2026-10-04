"""Application configuration. All secrets come from environment variables."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=("../.env", ".env"), env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"  # development | test | production
    app_name: str = "Duka Commerce Platform"
    log_level: str = "INFO"
    public_base_url: str = "http://localhost:8000"
    cors_origins: str = "http://localhost:3000"

    database_url: str = "postgresql+psycopg://commerce:commerce@localhost:5432/commerce"

    # Auth / crypto
    jwt_secret: str = "change-me-in-env"
    jwt_expire_minutes: int = 60 * 12
    # Fernet key used to encrypt per-tenant credentials (WhatsApp access tokens) at rest.
    encryption_key: str = ""

    # LLM
    llm_provider: str = "rules"  # rules | openai_compat
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 30.0
    llm_temperature: float = 0.2
    agent_max_history_messages: int = 8
    agent_max_tool_iterations: int = 5
    agent_summary_trigger_messages: int = 24

    # Embeddings (dimension is fixed by migration 0001: 384)
    embedding_provider: str = "hash"  # hash | openai_compat
    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 384

    # WhatsApp Cloud API
    whatsapp_verify_token: str = ""
    whatsapp_app_secret: str = ""
    whatsapp_api_version: str = "v21.0"
    whatsapp_graph_base_url: str = "https://graph.facebook.com"
    whatsapp_force_dev: bool = False  # route every outbound message through the dev adapter
    whatsapp_timeout_seconds: float = 10.0

    # Payments
    payment_webhook_secret: str = ""  # HMAC secret for the mock provider callback
    momo_base_url: str = "https://sandbox.momodeveloper.mtn.com"
    momo_target_environment: str = "sandbox"
    momo_subscription_key: str = ""
    momo_api_user: str = ""
    momo_api_key: str = ""
    momo_callback_host: str = ""
    momo_currency_override: str = ""  # sandbox only accepts EUR

    enable_dev_tools: bool = True
    rate_limit_per_minute: int = 30
    # Self-service business registration. Always closed in production: pilot tenants are created
    # with `python -m app.cli create-business` on the server.
    allow_public_registration: bool = True

    @property
    def registration_open(self) -> bool:
        return self.allow_public_registration and not self.is_production

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
