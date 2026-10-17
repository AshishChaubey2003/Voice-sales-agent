import uuid
from functools import lru_cache

from pydantic import SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

TTS_PROVIDERS = ("groq", "deepgram")

# Values asyncpg accepts for its `ssl` argument.
DB_SSL_MODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")


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

    # Knowledge base (RAG)
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_cache_dir: str = ".models"
    rag_top_k: int = 4
    rag_max_distance: float | None = 0.30

    @field_validator("tts_provider")
    @classmethod
    def _known_tts_provider(cls, value: str) -> str:
        provider = value.strip().lower()
        if provider not in TTS_PROVIDERS:
            raise ValueError(f"must be one of {', '.join(TTS_PROVIDERS)}")
        return provider

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
