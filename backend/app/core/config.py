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
    # Safety limits for every app connection, in milliseconds (0 = off; migrations use their own connection).
    # One inbound message is one transaction that stays open through the AI turn, so a lock wait must outlast a
    # turn holding the lock, and an idle transaction is only ended well after the turn budget (see
    # _consistent_time_limits).
    db_statement_timeout_ms: int = 60_000
    db_lock_timeout_ms: int = 50_000
    db_idle_in_transaction_timeout_ms: int = 120_000

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
    llm_model: str = "gpt-4o-mini"         # the platform default, always allowed
    # Further models a business may choose for its assistant (comma-separated). Anything else is rejected when the
    # assistant settings are saved; a stored choice the platform no longer allows falls back to LLM_MODEL.
    llm_allowed_models: str = ""
    llm_timeout_seconds: float = 20.0      # per HTTP attempt (also capped by the turn budget)
    llm_max_attempts: int = 3              # bounded retries on 408/409/429/5xx/network errors
    llm_max_tokens: int = 500
    llm_temperature: float = 0.2
    agent_turn_timeout_seconds: float = 45.0  # whole turn (all LLM calls + tools); then the fallback is sent
    agent_max_tool_calls: int = 8
    agent_max_history_messages: int = 8
    agent_max_tool_iterations: int = 5
    agent_summary_trigger_messages: int = 24
    # Runaway Conversation Guard (docs/P1_RUNAWAY_GUARD.md, app/services/ai_guard.py). Every real model call and every
    # provider HTTP attempt is reserved in ai_usage_counters before it is made: per inbound message (the webhook event,
    # across retries), per customer and per tenant, in fixed UTC hour/day buckets. Mode per scope: off = not counted;
    # observe = counted, and reservations past a limit are logged (ai_guard.decision) but never refused; enforce =
    # reservations past a limit are refused (the customer gets the facts already found or a safe reply, the
    # conversation is flagged, the owner alerted once per window).
    # The message budget enforces by default (its limit is derived from the turn limits below, so it never limits a
    # first processing attempt, only retries); customer and tenant limits are chosen from observe-mode data first.
    ai_guard_message_mode: str = "enforce"
    ai_guard_customer_mode: str = "observe"
    ai_guard_tenant_mode: str = "observe"
    # Per inbound message, across retries. 0 = what one processing attempt may use: 1 summary call +
    # AGENT_MAX_TOOL_ITERATIONS calls, each with up to LLM_MAX_ATTEMPTS HTTP attempts.
    ai_guard_message_calls: int = 0
    ai_guard_message_attempts: int = 0
    # Per customer and per tenant, per UTC hour / UTC day. 0 = no limit (counted only): choose values from what
    # observe mode records, never by guess.
    ai_guard_customer_calls_per_hour: int = 0
    ai_guard_customer_calls_per_day: int = 0
    ai_guard_customer_attempts_per_hour: int = 0
    ai_guard_customer_attempts_per_day: int = 0
    ai_guard_tenant_calls_per_hour: int = 0
    ai_guard_tenant_calls_per_day: int = 0
    ai_guard_tenant_attempts_per_hour: int = 0
    ai_guard_tenant_attempts_per_day: int = 0

    # Usage metering: path of the operator's price list (JSON, format in app/services/pricing.py). Duka ships no
    # prices; without it every AI model call is still recorded in usage_events, unpriced.
    usage_pricing_file: str = ""

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
    # WhatsApp only delivers normal (non-template) messages within 24 h of the customer's last message. Older than
    # this, a message to the customer is not attempted and the owner is told; the margin covers processing delays.
    whatsapp_window_hours: float = 23.5

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
    # A crashed worker's event is reclaimed after this. Longer than the slowest processing (a lock wait plus a full
    # AI turn); a graceful shutdown hands unfinished events back at once instead.
    webhook_lease_seconds: int = 120
    webhook_max_attempts: int = 5
    outbox_max_attempts: int = 5
    outbox_sending_timeout_seconds: int = 120
    webhook_event_retention_days: int = 30  # processed inbound payloads (PII) are purged after this
    # Orders hold their stock until the owner acts. Remind the owner (once per order) when one has waited longer than
    # this for review, or has been accepted this long ago and is still unpaid. 0 = no reminder.
    order_review_reminder_hours: float = 2.0
    order_payment_reminder_hours: float = 24.0

    # Operations: bearer token for /metrics and /readyz?details=1 (required to see them in production).
    ops_token: str = ""

    # HTTP hardening (app/api/middleware.py), independent of any reverse proxy.
    max_request_body_bytes: int = 10 * 1024 * 1024
    # Reverse proxies in front of the API that append the address they saw to X-Forwarded-For (Render: 1,
    # deploy/ Caddy: 1, none: 0). Rate limits use the entry that many hops from the right; entries further left
    # were written by the client and are ignored.
    trusted_proxy_hops: int = 0

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

    @model_validator(mode="after")
    def _consistent_time_limits(self) -> "Settings":
        """The worker holds a conversation (and, while ordering, the business row) locked for up to a whole AI turn.
        Refuse limits that would cancel normal work: a waiter must outlast that turn, the idle transaction around
        an LLM call must not be ended, and the lease must not expire while the event is still being processed."""
        turn_ms = self.agent_turn_timeout_seconds * 1000
        lock, stmt, idle = self.db_lock_timeout_ms, self.db_statement_timeout_ms, self.db_idle_in_transaction_timeout_ms
        problems = []
        if lock and lock <= turn_ms:
            problems.append("DB_LOCK_TIMEOUT_MS must be longer than AGENT_TURN_TIMEOUT_SECONDS")
        if stmt and lock and stmt < lock:
            problems.append("DB_STATEMENT_TIMEOUT_MS must be at least DB_LOCK_TIMEOUT_MS")
        if idle and idle <= turn_ms:
            problems.append("DB_IDLE_IN_TRANSACTION_TIMEOUT_MS must be longer than AGENT_TURN_TIMEOUT_SECONDS")
        if self.webhook_lease_seconds * 1000 <= (lock or stmt) + turn_ms:
            problems.append("WEBHOOK_LEASE_SECONDS must be longer than the DB lock timeout plus the AI turn budget")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    @model_validator(mode="after")
    def _ai_guard_settings(self) -> "Settings":
        # Enforcement is added scope by scope (docs/P1_RUNAWAY_GUARD.md, B3-B5).
        allowed = {"message": {"off", "observe", "enforce"}, "customer": {"off", "observe"},
                   "tenant": {"off", "observe"}}
        for scope, modes in allowed.items():
            name = f"ai_guard_{scope}_mode"
            if getattr(self, name) not in modes:
                raise ValueError(f"{name.upper()} must be one of {sorted(modes)}")
        for name, value in self.model_dump().items():
            if name.startswith("ai_guard_") and isinstance(value, int) and value < 0:
                raise ValueError(f"{name.upper()} must be 0 (default / no limit) or a positive number")
        return self

    @property
    def ai_guard_message_limits(self) -> tuple[int, int]:
        """(model calls, HTTP attempts) one inbound message may reserve across all its processing attempts."""
        calls = self.ai_guard_message_calls or 1 + self.agent_max_tool_iterations
        return calls, self.ai_guard_message_attempts or calls * self.llm_max_attempts

    @property
    def allowed_llm_models(self) -> list[str]:
        extra = [m.strip() for m in self.llm_allowed_models.split(",") if m.strip()]
        return list(dict.fromkeys([self.llm_model, *extra]))

    def permitted_llm_model(self, requested: str | None) -> str | None:
        """A business's own model choice while the platform allows it, else None (= the default, LLM_MODEL)."""
        return requested if requested and requested in self.allowed_llm_models else None

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
