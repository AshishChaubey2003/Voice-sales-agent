import pytest
from pydantic import ValidationError

from api.config import Settings, SettingsError, describe_settings_error, get_settings
from api.db import build_engine_kwargs, get_database_url

LOCAL_DB = {
    "postgres_user": "voice_app",
    "postgres_password": "secret-password",
    "postgres_db": "voice_sales",
}
LLM = {"groq_api_key": "secret-key", "groq_model": "openai/gpt-oss-20b"}


def build(**overrides) -> Settings:
    """Settings from explicit values only, so a developer's .env cannot change the result."""
    values = {**LOCAL_DB, **LLM, "redis_password": "secret-password", **overrides}
    return Settings(_env_file=None, **values)


@pytest.fixture
def settings_from_env(monkeypatch):
    """Builds settings from environment variables and restores the cache afterwards."""

    def apply(**env):
        get_settings.cache_clear()
        # Ignore the developer's own .env so the test result cannot depend on it.
        monkeypatch.setitem(Settings.model_config, "env_file", None)
        for key, value in {**LOCAL_DB, **LLM, "redis_password": "p", **env}.items():
            monkeypatch.setenv(key.upper(), str(value))
        return get_settings()

    yield apply
    get_settings.cache_clear()


def test_valid_local_settings_are_accepted():
    settings = build()
    assert settings.tts_provider == "groq"
    assert settings.redis_enabled is True


def test_database_parts_or_database_url_is_required():
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, redis_password="p", **LLM)
    assert "DATABASE_URL" in str(error.value)


def test_database_url_must_use_the_async_driver():
    with pytest.raises(ValidationError) as error:
        build(database_url="postgresql://user:pw@host.example.com/db")
    assert "postgresql+asyncpg" in str(error.value)


def test_database_url_rejects_sslmode_because_asyncpg_rejects_it():
    with pytest.raises(ValidationError) as error:
        build(database_url="postgresql+asyncpg://u:p@host.example.com/db?sslmode=require")
    assert "sslmode" in str(error.value)


def test_unknown_db_ssl_mode_is_rejected():
    with pytest.raises(ValidationError):
        build(db_ssl="yes-please")


def test_unknown_tts_provider_is_rejected():
    with pytest.raises(ValidationError) as error:
        build(tts_provider="elevenlabs")
    assert "groq" in str(error.value)


def test_deepgram_provider_requires_its_api_key():
    with pytest.raises(ValidationError) as error:
        build(tts_provider="deepgram")
    assert "DEEPGRAM_API_KEY" in str(error.value)

    assert build(tts_provider="deepgram", deepgram_api_key="k").tts_provider == "deepgram"


def test_redis_password_is_required_only_when_redis_is_enabled():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **LOCAL_DB, **LLM)

    settings = Settings(_env_file=None, redis_enabled=False, **LOCAL_DB, **LLM)
    assert settings.redis_enabled is False


def test_settings_error_names_fields_without_leaking_values():
    with pytest.raises(ValidationError) as error:
        build(tts_provider="elevenlabs")

    message = describe_settings_error(error.value)
    assert "TTS_PROVIDER" in message
    assert "elevenlabs" not in message
    assert "secret-key" not in message
    assert "secret-password" not in message


def test_get_settings_raises_a_clear_error_for_bad_configuration(settings_from_env):
    with pytest.raises(SettingsError) as error:
        settings_from_env(tts_provider="elevenlabs")
    assert "TTS_PROVIDER" in str(error.value)
    assert "elevenlabs" not in str(error.value)


def test_database_url_overrides_the_postgres_parts(settings_from_env):
    settings_from_env(
        database_url="postgresql+asyncpg://neon_user:pw@ep-demo.neon.tech/neondb"
    )
    url = get_database_url()
    assert url.host == "ep-demo.neon.tech"
    assert url.database == "neondb"
    assert url.drivername == "postgresql+asyncpg"


def test_postgres_parts_are_used_when_no_database_url(settings_from_env):
    settings_from_env(postgres_host="127.0.0.1", postgres_port="5433")
    url = get_database_url()
    assert url.host == "127.0.0.1"
    assert url.port == 5433
    assert url.drivername == "postgresql+asyncpg"


def test_tls_and_pooler_options_reach_the_engine(settings_from_env):
    settings_from_env(
        database_url="postgresql+asyncpg://u:p@ep-demo-pooler.neon.tech/neondb",
        db_ssl="require",
        db_prepared_statement_cache_size="0",
        db_pool_size="2",
        db_max_overflow="0",
    )
    kwargs = build_engine_kwargs()
    assert kwargs["connect_args"] == {"ssl": "require"}
    assert kwargs["prepared_statement_cache_size"] == 0
    assert kwargs["pool_size"] == 2
    assert kwargs["max_overflow"] == 0
    assert kwargs["pool_pre_ping"] is True


def test_local_engine_has_no_tls_or_pooler_options(settings_from_env):
    settings_from_env()
    kwargs = build_engine_kwargs()
    assert "connect_args" not in kwargs
    assert "prepared_statement_cache_size" not in kwargs
