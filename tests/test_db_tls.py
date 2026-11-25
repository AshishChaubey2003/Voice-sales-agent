"""Production database TLS: refuse configurations that do not force TLS.

Only APP_ENV=production is enforced, so local development (plain Docker Postgres)
keeps working. Errors name fields and reasons, never the DATABASE_URL or secrets.
"""

import pytest
from pydantic import ValidationError

from api.config import Settings, describe_settings_error
from api.db import build_engine_kwargs

PASSWORD = "very-secret-password"
HOSTED_URL = f"postgresql+asyncpg://app_user:{PASSWORD}@db.example.com/voice"
LLM = {"groq_api_key": "k", "groq_model": "m", "redis_enabled": False}
POSTGRES_PARTS = {
    "postgres_user": "app_user",
    "postgres_password": PASSWORD,
    "postgres_db": "voice",
    "postgres_host": "db.example.com",
}


def build(**overrides) -> Settings:
    # Explicit app_env so a developer's environment cannot switch these tests to production.
    values = {**LLM, **POSTGRES_PARTS, "app_env": "development", **overrides}
    return Settings(_env_file=None, **values)


def production(**overrides) -> Settings:
    return build(**{**overrides, "app_env": "production"})


def assert_refused_without_leaking(error: ValidationError) -> str:
    # describe_settings_error is what the app prints; pydantic's own str() echoes inputs.
    message = describe_settings_error(error)
    assert "TLS is required" in message or "ssl parameter" in message
    assert PASSWORD not in message
    assert "db.example.com" not in message
    return message


# ------------------------------------------------ production: accepted TLS


@pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full"])
def test_production_accepts_db_ssl_with_a_forced_tls_mode(mode):
    settings = production(**POSTGRES_PARTS, db_ssl=mode)
    assert settings.db_ssl == mode


def test_production_accepts_tls_requested_in_the_database_url():
    settings = production(database_url=f"{HOSTED_URL}?ssl=require")
    assert settings.database_url.endswith("?ssl=require")


def test_production_accepts_matching_url_and_db_ssl_modes():
    production(database_url=f"{HOSTED_URL}?ssl=verify-full", db_ssl="verify-full")


def test_tls_settings_reach_asyncpg_connect_args(monkeypatch):
    settings = production(database_url=HOSTED_URL, db_ssl="verify-full")
    monkeypatch.setattr("api.db.get_settings", lambda: settings)
    assert build_engine_kwargs()["connect_args"] == {"ssl": "verify-full"}


# ------------------------------------------------ production: refused TLS


def test_production_refuses_a_database_url_without_tls():
    with pytest.raises(ValidationError) as error:
        production(database_url=HOSTED_URL)
    assert_refused_without_leaking(error.value)


def test_production_refuses_local_parts_without_db_ssl():
    with pytest.raises(ValidationError) as error:
        production(**POSTGRES_PARTS)
    assert "DB_SSL" in str(error.value)
    assert_refused_without_leaking(error.value)


@pytest.mark.parametrize("mode", ["disable", "allow", "prefer"])
def test_production_refuses_disabled_or_opportunistic_db_ssl(mode):
    with pytest.raises(ValidationError) as error:
        production(**POSTGRES_PARTS, db_ssl=mode)
    assert "disable, allow and prefer are refused" in str(error.value)


@pytest.mark.parametrize("mode", ["disable", "allow", "prefer"])
def test_production_refuses_weak_mode_in_the_database_url(mode):
    with pytest.raises(ValidationError) as error:
        production(database_url=f"{HOSTED_URL}?ssl={mode}")
    assert_refused_without_leaking(error.value)


def test_production_refuses_a_strong_db_ssl_overridden_by_a_weak_url_mode():
    with pytest.raises(ValidationError) as error:
        production(database_url=f"{HOSTED_URL}?ssl=disable", db_ssl="require")
    assert_refused_without_leaking(error.value)


def test_unknown_ssl_mode_in_the_database_url_is_refused_everywhere():
    with pytest.raises(ValidationError) as error:
        build(database_url=f"{HOSTED_URL}?ssl=yes-please")
    message = describe_settings_error(error.value)
    assert "ssl parameter" in message
    assert "yes-please" not in message


def test_malformed_database_url_error_does_not_echo_the_url():
    with pytest.raises(ValidationError) as error:
        production(database_url=f"postgresql+asyncpg://app_user:{PASSWORD}@db.example.com:notaport/voice?ssl=require")
    assert PASSWORD not in describe_settings_error(error.value)


# ------------------------------------------------ development is unaffected


def test_development_allows_a_database_without_tls():
    settings = build(**POSTGRES_PARTS)
    assert settings.db_ssl is None


def test_development_allows_disabled_db_ssl_for_local_docker():
    assert build(**POSTGRES_PARTS, db_ssl="disable").db_ssl == "disable"


def test_blank_db_ssl_is_treated_as_unset():
    # A blank DB_SSL= line in .env must not be reported as an unknown mode.
    assert build(**POSTGRES_PARTS, db_ssl="").db_ssl is None
    assert build(**POSTGRES_PARTS, db_ssl="   ").db_ssl is None


def test_blank_db_ssl_in_production_is_still_refused():
    with pytest.raises(ValidationError) as error:
        production(**POSTGRES_PARTS, db_ssl="")
    assert_refused_without_leaking(error.value)


# ------------------------------------------------ voice token settings


def test_voice_token_secret_is_optional_but_blank_counts_as_unset():
    assert build().voice_token_secret is None
    assert build(voice_token_secret="").voice_token_secret is None


def test_voice_token_secret_must_be_at_least_32_characters_and_never_echoed():
    with pytest.raises(ValidationError) as error:
        build(voice_token_secret="short-secret-value")
    message = describe_settings_error(error.value)
    assert "VOICE_TOKEN_SECRET" in message
    assert "short-secret-value" not in message


def test_voice_token_secret_strength_is_checked_in_development_too():
    with pytest.raises(ValidationError):
        build(app_env="development", voice_token_secret="x" * 10)


def test_voice_token_ttl_is_bounded():
    assert build(voice_token_ttl_secs=300).voice_token_ttl_secs == 300
    with pytest.raises(ValidationError):
        build(voice_token_ttl_secs=5)
    with pytest.raises(ValidationError):
        build(voice_token_ttl_secs=86400)
