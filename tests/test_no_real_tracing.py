import os

import pytest


def test_suite_runs_with_tracing_forced_off():
    # tests/conftest.py sets this before any application module reads settings.
    assert os.environ.get("LANGSMITH_TRACING") == "false"
    from api.config import Settings

    try:
        settings = Settings()
    except Exception:  # a bare machine without the database variables cannot build Settings
        pytest.skip("Settings need the project's full environment")
    assert settings.langsmith_tracing is False
