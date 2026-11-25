import uuid
from functools import lru_cache

from pydantic import SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import make_url

TTS_PROVIDERS = ("groq", "deepgram")

# Values asyncpg accepts for its `ssl` argument.
DB_SSL_MODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
# Production accepts only modes that force TLS. disable, allow and prefer can fall
# back to plaintext, so they are refused.
TLS_REQUIRED_MODES = ("require", "verify-ca", "verify-full")

VOICE_TOKEN_MIN_SECRET_LENGTH = 32


def _database_url_ssl_mode(url: str) -> str | None:
    """Returns the ssl= query value of a DATABASE_URL, or None when absent.

    Never echoes the URL: parse errors name only the field.
    """
    try:
        query = make_url(url).query
    except Exception:
        raise ValueError("DATABASE_URL could not be parsed") from None
    value = query.get("ssl")
    if value is None:
        return None
    if isinstance(value, (tuple, list)):
        value = value[-1] if value else ""
    mode = str(value).strip().lower()
    if mode not in DB_SSL_MODES:
        raise ValueError(
            f"DATABASE_URL ssl parameter must be one of {', '.join(DB_SSL_MODES)}"
        )
    return mode


class SettingsError(RuntimeError):
    """Configuration is missing or invalid. The message never contains secret values."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"

    # Database
    # A full DATABASE_URL wins over the POSTGRES_* parts and is how hosted
    # Postgres (for example Neon) is reached.
    database_url: str | None = None
    postgres_user: str | None = None
    postgres_password: SecretStr | None = None
    postgres_db: str | None = None
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5433

    # Connection handling. asyncpg takes `ssl`, not `sslmode`.
    db_ssl: str | None = None
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_pool_recycle_seconds: int = 1800
    # Set to 0 behind a transaction pooler, which cannot hold prepared statements.
    db_prepared_statement_cache_size: int | None = None

    # Redis (only the /readyz check uses it today)
    redis_enabled: bool = True
    redis_password: SecretStr | None = None
    redis_host: str = "127.0.0.1"
    redis_port: int = 6380

    # LLM
    groq_api_key: SecretStr
    groq_model: str
    llm_reasoning_effort: str | None = "low"
    llm_timeout_seconds: float = 20.0
    llm_max_output_tokens: int = 600
    chat_history_limit: int = 10
    # "langgraph" (default) routes each text-chat turn through api/chat_graph.py; "legacy" keeps the
    # original straight-line code path. Voice is unaffected by either.
    chat_orchestrator: str = "langgraph"

    # Voice
    voice_agent_id: uuid.UUID | None = None
    voice_greeting: str = "Hi there! How can I help you today?"
    voice_reasoning_effort: str | None = None
    tts_provider: str = "groq"
    deepgram_api_key: SecretStr | None = None
    deepgram_tts_voice: str = "aura-2-helena-en"

    # Voice call guards for a public demo (protect paid Groq/Deepgram/TURN quota).
    # None or 0 means no cap.
    voice_max_call_seconds: int | None = None
    voice_max_concurrent_calls: int = 1
    # VOICE-01: how long a disconnect waits for an in-flight save_lead commit to finish
    # before cancelling the pipeline anyway. Bounded on purpose - a stuck save must not
    # block shutdown forever.
    lead_save_disconnect_grace_secs: float = 3.0
    # Private voice access. Visitors need a short-lived token minted by
    # POST /v1/voice/tokens (org API key required). The secret is server-only.
    voice_token_secret: SecretStr | None = None
    voice_token_ttl_secs: int = 300

    # Knowledge base (RAG)
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_cache_dir: str = ".models"
    rag_top_k: int = 4
    rag_max_distance: float | None = 0.30

    # Evaluation and optional LangSmith tracing. Tracing stays off unless both
    # LANGSMITH_TRACING=true and LANGSMITH_API_KEY are set (see api/tracing.py).
    eval_results_dir: str = "evals/results"
    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "voice-sales-agent"
    langsmith_endpoint: str | None = None
    langsmith_trace_content: bool = False
    # Optional LLM judge (evaluation only; never used on the live request path).
    gemini_api_key: SecretStr | None = None
    gemini_judge_model: str | None = None
    # Read timeout for Gemini judge calls only (seconds). Unset = 60. Independent of LLM_TIMEOUT_SECONDS.
    gemini_judge_timeout_seconds: float | None = None
    # Human handoff email notification (off by default; see api/notifications.py). Not telephony.
    handoff_email_enabled: bool = False
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str | None = None
    smtp_use_starttls: bool = True
    # Opt-in online monitoring (counts and timings only; see api/monitoring.py).
    online_monitoring: bool = False
    alert_webhook_url: str | None = None
    alert_file: str | None = None

    @field_validator("gemini_judge_timeout_seconds")
    @classmethod
    def _sane_judge_timeout(cls, value: float | None) -> float | None:
        if value is not None and not (0 < value <= 600):
            raise ValueError("must be greater than 0 and at most 600 seconds")
        return value

    @field_validator("tts_provider")
    @classmethod
    def _known_tts_provider(cls, value: str) -> str:
        provider = value.strip().lower()
        if provider not in TTS_PROVIDERS:
            raise ValueError(f"must be one of {', '.join(TTS_PROVIDERS)}")
        return provider

    @field_validator("voice_token_secret", mode="before")
    @classmethod
    def _blank_voice_token_secret_is_unset(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("voice_token_secret")
    @classmethod
    def _voice_token_secret_strength(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) < VOICE_TOKEN_MIN_SECRET_LENGTH:
            raise ValueError(
                f"must be at least {VOICE_TOKEN_MIN_SECRET_LENGTH} characters"
            )
        return value

    @field_validator("voice_token_ttl_secs")
    @classmethod
    def _voice_token_ttl_bounds(cls, value: int) -> int:
        if not 30 <= value <= 3600:
            raise ValueError("must be between 30 and 3600 seconds")
        return value

    @field_validator("db_ssl", mode="before")
    @classmethod
    def _blank_db_ssl_is_unset(cls, value):
        # A blank DB_SSL= line in .env means unset. Without this, it would fail as an
        # unknown mode, and production would report the wrong problem.
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("db_ssl")
    @classmethod
    def _known_db_ssl_mode(cls, value: str | None) -> str | None:
        if value is None:
            return None
        mode = value.strip().lower()
        if mode not in DB_SSL_MODES:
            raise ValueError(f"must be one of {', '.join(DB_SSL_MODES)}")
        return mode

    @field_validator("database_url")
    @classmethod
    def _async_driver_and_no_sslmode(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        url = value.strip()
        if not url.startswith("postgresql+asyncpg://"):
            raise ValueError(
                "must start with postgresql+asyncpg:// "
                "(a plain postgresql:// URL uses a blocking driver)"
            )
        if "sslmode=" in url:
            raise ValueError(
                "must not contain sslmode; asyncpg does not accept it. "
                "Use ssl=require in the URL or set DB_SSL instead"
            )
        return url

    @model_validator(mode="after")
    def _check_combinations(self) -> "Settings":
        if not self.database_url:
            missing = [
                name
                for name in ("postgres_user", "postgres_password", "postgres_db")
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(
                    "set DATABASE_URL, or all of "
                    + ", ".join(name.upper() for name in missing)
                )

        if self.tts_provider == "deepgram" and self.deepgram_api_key is None:
            raise ValueError("DEEPGRAM_API_KEY is required when TTS_PROVIDER=deepgram")

        if self.redis_enabled and self.redis_password is None:
            raise ValueError(
                "REDIS_PASSWORD is required when REDIS_ENABLED=true "
                "(set REDIS_ENABLED=false to run without Redis)"
            )

        # Production database TLS. Checked for DB_SSL and the DATABASE_URL ssl=
        # parameter together, so a weak value in either place is refused.
        url_mode = _database_url_ssl_mode(self.database_url) if self.database_url else None
        if self.app_env == "production":
            modes = {mode for mode in (self.db_ssl, url_mode) if mode}
            if not modes:
                raise ValueError(
                    "TLS is required in production: set DB_SSL to require, verify-ca "
                    "or verify-full, or add ssl=require to DATABASE_URL"
                )
            if modes - set(TLS_REQUIRED_MODES):
                raise ValueError(
                    "TLS is required in production: DB_SSL and the DATABASE_URL ssl "
                    "parameter must be require, verify-ca or verify-full "
                    "(disable, allow and prefer are refused)"
                )

        return self


def describe_settings_error(error: ValidationError) -> str:
    """Turns a pydantic error into field names and reasons, never values."""
    problems = []
    for item in error.errors():
        location = ".".join(str(part) for part in item["loc"])
        name = location.upper() if location else "configuration"
        problems.append(f"  - {name}: {item['msg']}")
    return "Invalid configuration. Fix these in .env or the environment:\n" + "\n".join(
        problems
    )


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as error:
        # `from None` keeps pydantic's raw error, which echoes input values,
        # out of the traceback.
        raise SettingsError(describe_settings_error(error)) from None
