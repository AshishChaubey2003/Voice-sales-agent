"""Regression tests for the production startup guard (finding C).

These tests exercise ``check_startup_config()`` in ``voice/bot.py`` directly.
They do NOT exercise the ``__main__`` entry point, which calls
``pipecat.runner.run.main()`` and cannot be invoked in a unit-test context.

The per-call production check inside ``run_bot`` delegates to
``production_config_errors`` from ``voice/ice.py``, which is already thoroughly
covered in ``tests/test_ice.py``.  The rows here add a regression layer for the
startup wrapper that translates those errors into a ``SystemExit``.
"""

import pytest

from api.config import Settings
from voice.bot import check_startup_config

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_ICE = "stun:stun.l.google.com:19302"
VALID_ORIGINS = "https://demo.example.com"


def _settings(**overrides) -> Settings:
    """Build a minimal Settings object without reading .env."""
    defaults = dict(
        _env_file=None,
        postgres_user="u",
        postgres_password="p",
        postgres_db="d",
        groq_api_key="dummy-not-a-real-key",
        groq_model="dummy-model",
        redis_password="p",
        # Production now refuses database connections without TLS (policy). These
        # tests exercise the ICE/origins guard, so they supply a forced TLS mode.
        db_ssl="verify-full",
        # Production also requires a voice token secret (fail closed). Test value only.
        voice_token_secret="t" * 32,
    )
    defaults.update(overrides)
    return Settings(**defaults)


# ---------------------------------------------------------------------------
# Development: check_startup_config() is always a no-op
# ---------------------------------------------------------------------------


def test_startup_passes_in_development_no_ice_no_origins(monkeypatch):
    """In development, the guard does nothing even when both env vars are absent."""
    monkeypatch.delenv("PIPECAT_ICE_SERVERS", raising=False)
    monkeypatch.delenv("PIPECAT_ALLOWED_ORIGINS", raising=False)
    # Must not raise.
    check_startup_config(settings=_settings(app_env="development"))


def test_startup_passes_in_development_with_valid_config(monkeypatch):
    """Valid production config in development mode is also fine (sanity check)."""
    monkeypatch.setenv("PIPECAT_ICE_SERVERS", VALID_ICE)
    monkeypatch.setenv("PIPECAT_ALLOWED_ORIGINS", VALID_ORIGINS)
    check_startup_config(settings=_settings(app_env="development"))


def test_development_without_ice_or_origins_warns_but_does_not_raise(monkeypatch):
    """DEPLOY-05: an operator who deploys with APP_ENV left at its default must not get
    total silence about a config that would block a real production start - development
    stays a no-op (no SystemExit) but now logs a loud warning naming the exact gap."""
    import voice.bot as bot_module

    monkeypatch.delenv("PIPECAT_ICE_SERVERS", raising=False)
    monkeypatch.delenv("PIPECAT_ALLOWED_ORIGINS", raising=False)
    warnings = []
    monkeypatch.setattr(bot_module.logger, "warning", lambda msg: warnings.append(msg))

    check_startup_config(settings=_settings(app_env="development"))  # must not raise

    joined = " ".join(warnings)
    assert "APP_ENV='development'" in joined
    assert "would block startup in production" in joined
    assert "STUN" in joined
    assert "PIPECAT_ALLOWED_ORIGINS" in joined


def test_development_with_valid_config_warns_about_nothing(monkeypatch):
    """No false alarms: a development process whose config already satisfies the
    production rules must not be warned about anything."""
    import voice.bot as bot_module

    monkeypatch.setenv("PIPECAT_ICE_SERVERS", VALID_ICE)
    monkeypatch.setenv("PIPECAT_ALLOWED_ORIGINS", VALID_ORIGINS)
    warnings = []
    monkeypatch.setattr(bot_module.logger, "warning", lambda msg: warnings.append(msg))

    check_startup_config(settings=_settings(app_env="development"))

    assert warnings == []


# ---------------------------------------------------------------------------
# Production: valid configuration — guard does not raise
# ---------------------------------------------------------------------------


def test_startup_passes_in_production_with_stun_and_origins(monkeypatch):
    """STUN + origins: production startup proceeds normally."""
    monkeypatch.setenv("PIPECAT_ICE_SERVERS", VALID_ICE)
    monkeypatch.setenv("PIPECAT_ALLOWED_ORIGINS", VALID_ORIGINS)
    check_startup_config(settings=_settings(app_env="production"))


def test_startup_passes_in_production_with_turn(monkeypatch):
    """A TURN server (which also provides STUN) satisfies the ICE requirement."""
    monkeypatch.setenv("PIPECAT_ICE_SERVERS", "turn:turn.example.com:3478")
    monkeypatch.setenv("PIPECAT_ALLOWED_ORIGINS", VALID_ORIGINS)
    check_startup_config(settings=_settings(app_env="production"))


# ---------------------------------------------------------------------------
# Production: missing or invalid configuration — guard raises SystemExit
# ---------------------------------------------------------------------------


def test_startup_fails_in_production_without_ice(monkeypatch):
    """Production startup exits when PIPECAT_ICE_SERVERS is absent."""
    monkeypatch.delenv("PIPECAT_ICE_SERVERS", raising=False)
    monkeypatch.setenv("PIPECAT_ALLOWED_ORIGINS", VALID_ORIGINS)
    with pytest.raises(SystemExit):
        check_startup_config(settings=_settings(app_env="production"))


def test_startup_fails_in_production_without_origins(monkeypatch):
    """Production startup exits when PIPECAT_ALLOWED_ORIGINS is absent."""
    monkeypatch.setenv("PIPECAT_ICE_SERVERS", VALID_ICE)
    monkeypatch.delenv("PIPECAT_ALLOWED_ORIGINS", raising=False)
    with pytest.raises(SystemExit):
        check_startup_config(settings=_settings(app_env="production"))


def test_startup_fails_in_production_without_both(monkeypatch):
    """Production startup exits when both ICE and origins are absent."""
    monkeypatch.delenv("PIPECAT_ICE_SERVERS", raising=False)
    monkeypatch.delenv("PIPECAT_ALLOWED_ORIGINS", raising=False)
    with pytest.raises(SystemExit):
        check_startup_config(settings=_settings(app_env="production"))


def test_startup_fails_in_production_with_malformed_ice(monkeypatch):
    """Malformed ICE JSON is caught at startup before any call is handled."""
    monkeypatch.setenv("PIPECAT_ICE_SERVERS", "[not json")
    monkeypatch.setenv("PIPECAT_ALLOWED_ORIGINS", VALID_ORIGINS)
    with pytest.raises(SystemExit):
        check_startup_config(settings=_settings(app_env="production"))


def test_startup_exit_message_does_not_contain_credentials(monkeypatch):
    """The SystemExit message never echoes ICE credentials or env-var values."""
    monkeypatch.setenv(
        "PIPECAT_ICE_SERVERS",
        '[{"urls":"turn:t:3478","username":"secret-user","credential":"secret-pass"},'
        '"bad_key":"oops"]',
    )
    monkeypatch.delenv("PIPECAT_ALLOWED_ORIGINS", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        check_startup_config(settings=_settings(app_env="production"))
    msg = str(exc_info.value)
    assert "secret-user" not in msg
    assert "secret-pass" not in msg
