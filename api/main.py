import logging
from contextlib import asynccontextmanager

import redis.asyncio as redis
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from api.chat import router as chat_router
from api.config import get_settings
from api.db import build_engine, build_session_factory, database_ok
from api.eval_routes import page_router as eval_page_router
from api.eval_routes import router as eval_router
from api.handoff import conversation_router as handoff_conversation_router
from api.handoff import page_router as sales_page_router
from api.handoff import router as sales_router
from api.llm import GroqLLM
from api.mcp_server import configure_session_factory
from api.mcp_server import mcp as mcp_server
from api.voice_tokens import router as voice_tokens_router

logger = logging.getLogger(__name__)

# Built at import time: streamable_http_app() is what creates the session manager,
# and the lifespan below needs it. It serves at "/" so the single "/mcp" prefix
# comes from the mount (the default path would expose /mcp/mcp).
mcp_asgi_app = mcp_server.streamable_http_app(
    streamable_http_path="/", stateless_http=True
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Raises SettingsError with field names (no values) when .env is incomplete.
    settings = get_settings()

    app.state.engine = build_engine()
    app.state.session_factory = build_session_factory(app.state.engine)
    # The MCP tools open their own sessions; give them the app's pool so a
    # stdio/HTTP MCP client does not build a second engine per call.
    configure_session_factory(app.state.session_factory)

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
        # Starlette does not run a mounted app's lifespan, so the MCP session
        # manager is started here. Without it every /mcp request fails with
        # "Task group is not initialized".
        async with mcp_server.session_manager.run():
            yield
    finally:
        await app.state.llm.close()
        if app.state.redis is not None:
            await app.state.redis.aclose()
        await app.state.engine.dispose()


app = FastAPI(title="Voice Sales Agent API", lifespan=lifespan)
app.include_router(chat_router)
app.include_router(eval_router)
app.include_router(eval_page_router)
app.include_router(voice_tokens_router)
app.include_router(sales_router)
app.include_router(handoff_conversation_router)
app.include_router(sales_page_router)

# Model Context Protocol server (Streamable HTTP). Stateless: every request carries
# its own organization API key, so there is no cross-request MCP session state.
app.mount("/mcp", mcp_asgi_app)


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
