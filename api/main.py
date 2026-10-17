import logging
from contextlib import asynccontextmanager

import redis.asyncio as redis
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from api.chat import router as chat_router
from api.config import get_settings
from api.db import build_engine, build_session_factory, database_ok
from api.llm import GroqLLM

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Raises SettingsError with field names (no values) when .env is incomplete.
    settings = get_settings()

    app.state.engine = build_engine()
    app.state.session_factory = build_session_factory(app.state.engine)

    app.state.redis = None
    if settings.redis_enabled:
        app.state.redis = redis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            password=settings.redis_password.get_secret_value(),
        )

    app.state.llm = GroqLLM(
        api_key=settings.groq_api_key.get_secret_value(),
        model=settings.groq_model,
        timeout_seconds=settings.llm_timeout_seconds,
        max_output_tokens=settings.llm_max_output_tokens,
        reasoning_effort=settings.llm_reasoning_effort,
    )

    try:
        yield
    finally:
        await app.state.llm.close()
        if app.state.redis is not None:
            await app.state.redis.aclose()
        await app.state.engine.dispose()


app = FastAPI(title="Voice Sales Agent API", lifespan=lifespan)
app.include_router(chat_router)


@app.get("/healthz")
async def healthz():
    """Liveness only: the process is up. Never touches a dependency."""
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    """Readiness: the dependencies this service cannot serve traffic without."""
    checks = {}

    try:
        await database_ok(app.state.engine)
        checks["postgres"] = "ok"
    except Exception:
        # Logged in full; the response says only which dependency failed.
        logger.exception("Postgres readiness check failed")
        checks["postgres"] = "error"

    if app.state.redis is not None:
        try:
            await app.state.redis.ping()
            checks["redis"] = "ok"
        except Exception:
            logger.exception("Redis readiness check failed")
            checks["redis"] = "error"

    ready = all(value == "ok" for value in checks.values())
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ready" if ready else "not_ready", "checks": checks},
    )
