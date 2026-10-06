"""Application configuration. All secrets come from environment variables."""
from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=("../.env", ".env"), env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"  # development | test | production
    app_name: str = "Duka Commerce Platform"
    log_level: str = "INFO"
    public_base_url: str = "http://localhost:8000"
    cors_origins: str = "http://localhost:3000"

    database_url: str = "postgresql+psycopg://commerce:commerce@localhost:5432/commerce"

    @field_validator("database_url")
    @classmethod
    def _psycopg_driver(cls, v: str) -> str:
        # Hosted Postgres (e.g. Render) hands out postgresql:// or postgres:// URLs; SQLAlchemy would then look for
        # psycopg2, which is not installed. The app uses psycopg 3.
        for prefix in ("postgresql://", "postgres://"):
            if v.startswith(prefix):
                return "postgresql+psycopg://" + v[len(prefix):]
        return v

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
    llm_timeout_seconds: float = 20.0      # per HTTP attempt (also capped by the turn budget)
    llm_max_attempts: int = 3              # bounded retries on 408/409/429/5xx/network errors
    llm_max_tokens: int = 500
    llm_temperature: float = 0.2
    agent_turn_timeout_seconds: float = 45.0  # whole turn (all LLM calls + tools); then the fallback is sent
    agent_max_tool_calls: int = 8
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

    # Durable inbound processing (webhook_events) and outbound delivery (outbox) workers.
    background_workers: int = 2           # worker threads per process; 0 disables (tests drain explicitly)
    worker_poll_seconds: float = 2.0
    webhook_lease_seconds: int = 300      # a crashed worker's event is reclaimed after this
    webhook_max_attempts: int = 5
    outbox_max_attempts: int = 5
    outbox_sending_timeout_seconds: int = 120
    webhook_event_retention_days: int = 30  # processed inbound payloads (PII) are purged after this

    # Operations: bearer token for /metrics and /readyz?details=1 (required to see them in production).
    ops_token: str = ""

    enable_dev_tools: bool = True
    rate_limit_per_minute: int = 30
    # Self-service business registration. Always closed in production: pilot tenants are created
    # with `python -m app.cli create-business` on the server.
    allow_public_registration: bool = True

    @property
    def registration_open(self) -> bool:
        return self.allow_public_registration and not self.is_production

    @model_validator(mode="after")
    def _reject_comment_values(self) -> "Settings":
        # docker compose's env_file turns `KEY=   # comment` into the value "# comment". For a secret such as
        # WHATSAPP_APP_SECRET that would silently become a publicly known string, so refuse to start.
        bad = [k.upper() for k, v in self.__dict__.items() if isinstance(v, str) and v.lstrip().startswith("#")]
        if bad:
            raise ValueError(f"{', '.join(bad)}: value starts with '#'. Put .env comments on their own line.")
        return self

    def production_problems(self) -> list[str]:
        """Configuration that must never reach production. main.py refuses to start if any is found."""
        problems = []
        if len(self.jwt_secret) < 32 or self.jwt_secret.startswith("change-me"):
            problems.append("JWT_SECRET must be a random string of at least 32 characters")
        if not self.encryption_key:
            problems.append("ENCRYPTION_KEY must be set (Fernet key for WhatsApp tokens)")
        else:
            try:
                from cryptography.fernet import Fernet
                Fernet(self.encryption_key.encode())
            except (ValueError, TypeError):
                problems.append("ENCRYPTION_KEY is not a valid Fernet key")
        if not self.public_base_url.startswith("https://"):
            problems.append("PUBLIC_BASE_URL must be the https:// address of this deployment")
        if self.llm_provider != "openai_compat" or not self.llm_api_key:
            problems.append("LLM_PROVIDER=openai_compat with LLM_API_KEY is required (the rules engine is not AI)")
        if not self.whatsapp_app_secret:
            problems.append("WHATSAPP_APP_SECRET must be set (webhook signature verification)")
        if not self.whatsapp_verify_token or self.whatsapp_verify_token.startswith("choose-"):
            problems.append("WHATSAPP_VERIFY_TOKEN must be a random string")
        if ":commerce@" in self.database_url:
            problems.append("DATABASE_URL uses the default development password")
        return problems

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
