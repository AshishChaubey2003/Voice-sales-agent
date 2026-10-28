import asyncio
import os
from datetime import UTC, datetime

from loguru import logger
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import TTSSpeakFrame
from pipecat.observers.user_bot_latency_observer import UserBotLatencyObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import (
    PipelineParams,
    PipelineWorker,
    ProcessorUnusablePolicy,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.groq.llm import GroqLLMService
from pipecat.services.groq.stt import GroqSTTService
from pipecat.services.groq.tts import GroqTTSService
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.workers.runner import WorkerRunner
from sqlalchemy import select, update

from api.config import get_settings
from api.db import build_engine, build_session_factory
from api.embeddings import get_embedding_model
from api.knowledge import GROUNDING_RULES
from api.leads import LEAD_CAPTURE_RULES
from api.llm import GroqLLM
from api.models import Agent, Conversation, Message
from voice.ice import (
    ICE_ENV_VAR,
    describe_ice_env,
    production_config_errors,
)

ALLOWED_ORIGINS_ENV_VAR = "PIPECAT_ALLOWED_ORIGINS"
from voice.lead_tool import (
    VOICE_LEAD_RULES,
    PendingLeadSaves,
    build_save_lead_handler,
    build_save_lead_schema,
)
from voice.limits import CallLimiter, run_with_call_guards, try_admit
from voice.metrics_observer import ServiceTTFBObserver
from voice.rag import KnowledgeInjector
from voice.turn_recorder import VoiceTurnRecorder
from voice.voice_auth import install_voice_auth, production_token_errors

# One runner process serves every call, so the limiter is shared across calls.
_call_limiter: CallLimiter | None = None


def get_call_limiter(settings) -> CallLimiter:
    global _call_limiter
    if _call_limiter is None:
        _call_limiter = CallLimiter(settings.voice_max_concurrent_calls)
    return _call_limiter

def check_startup_config(settings=None) -> None:
    """Fail fast if the process is misconfigured for a public deployment.

    Called once from ``__main__`` before the Pipecat runner starts accepting
    connections.  A no-op in development (the default ``APP_ENV``); only raises
    ``SystemExit`` when ``APP_ENV=production`` and ``production_config_errors``
    returns at least one blocking message.

    ``settings`` may be injected for testing; production code omits it and the
    cached live Settings object is used.  These tests cover this function
    directly — the ``__main__`` entry point itself is not under test because it
    invokes the full Pipecat runner.
    """
    if settings is None:
        settings = get_settings()
    ice = describe_ice_env(os.getenv(ICE_ENV_VAR))
    errors = production_config_errors(
        app_env=settings.app_env,
        ice=ice,
        allowed_origins=os.getenv(ALLOWED_ORIGINS_ENV_VAR),
    )
    token_secret = settings.voice_token_secret
    token_secret_value = token_secret.get_secret_value() if token_secret else None
    errors = errors + production_token_errors(settings.app_env, token_secret_value)
    if errors:
        for msg in errors:
            logger.error(f"Startup blocked: {msg}")
        raise SystemExit(
            "Refusing to start in production with blocking configuration errors. "
            "Fix the issues above and restart the process."
        )
    elif settings.app_env != "production":
        # DEPLOY-05: these same checks stay a no-op outside production by design, so local
        # development keeps working with no ICE/CORS setup at all. But that means an
        # operator who deploys this process publicly and simply forgets to set
        # APP_ENV=production gets no signal whatsoever today. Surface what WOULD have
        # blocked startup as a loud warning instead - this never raises and never changes
        # development behaviour, it only makes the silent gap visible in the startup log.
        would_block = production_config_errors(
            app_env="production", ice=ice, allowed_origins=os.getenv(ALLOWED_ORIGINS_ENV_VAR)
        )
        for msg in would_block:
            logger.warning(
                f"APP_ENV={settings.app_env!r}: this configuration would block startup "
                f"in production - {msg} If this process is reachable from the public "
                f"internet, set APP_ENV=production."
            )


VOICE_RULES = """Voice conversation rules (these override any earlier rule about language or format):
- Your replies are spoken aloud. Never use emojis, markdown, bullet points, or lists.
- Keep every reply to one or two short sentences.
- Always reply in English, because the voice can only speak English right now.
- Never read out links, codes, or long numbers."""

transport_params = {
    "webrtc": lambda: TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
    ),
}


def build_voice_prompt(agent_prompt: str) -> str:
    # Lead capture rules go last: the model follows the end of a long prompt more reliably.
    return (
        f"{agent_prompt}\n\n{GROUNDING_RULES}\n\n{VOICE_RULES}\n\n"
        f"{LEAD_CAPTURE_RULES}\n\n{VOICE_LEAD_RULES}"
    )


def build_llm_settings(model: str, system_prompt: str, reasoning_effort: str | None):
    """Returns (settings, effort_label). Sends reasoning_effort through Pipecat's `extra` params."""
    if reasoning_effort:
        settings = GroqLLMService.Settings(
            model=model,
            system_instruction=system_prompt,
            extra={"reasoning_effort": reasoning_effort},
        )
        return settings, reasoning_effort

    settings = GroqLLMService.Settings(model=model, system_instruction=system_prompt)
    return settings, "default"


def build_tts(settings):
    """Creates the TTS service chosen by TTS_PROVIDER in .env."""
    provider = settings.tts_provider.lower()

    if provider == "deepgram":
        if settings.deepgram_api_key is None:
            raise RuntimeError("DEEPGRAM_API_KEY is missing in .env")
        from pipecat.services.deepgram.tts import DeepgramTTSService

        return DeepgramTTSService(
            api_key=settings.deepgram_api_key.get_secret_value(),
            settings=DeepgramTTSService.Settings(voice=settings.deepgram_tts_voice),
        )

    if provider == "groq":
        return GroqTTSService(
            api_key=settings.groq_api_key.get_secret_value(),
            settings=GroqTTSService.Settings(voice="autumn"),
        )

    raise RuntimeError(f"Unknown TTS_PROVIDER: {settings.tts_provider}")


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments):
    settings = get_settings()
    if settings.voice_agent_id is None:
        raise RuntimeError("VOICE_AGENT_ID is missing in .env")

    # Say at startup whether a remote visitor can actually connect. Without ICE
    # servers the transport gathers host candidates only, which works on
    # localhost and silently fails everywhere else.
    ice = describe_ice_env(os.getenv(ICE_ENV_VAR))
    if ice.ok_for_public_call:
        logger.info(ice.summary())
    else:
        logger.warning(ice.summary())

    # In production, refuse to start when the public call path is misconfigured,
    # rather than coming up "healthy" and silently rejecting every visitor.
    # Development stays permissive (the warning above is enough).
    blocking = production_config_errors(
        app_env=settings.app_env,
        ice=ice,
        allowed_origins=os.getenv(ALLOWED_ORIGINS_ENV_VAR),
    )
    if blocking:
        raise RuntimeError(
            "Per-call production check failed (defense-in-depth): "
            + " ".join(blocking)
        )

    engine = build_engine()
    session_factory = build_session_factory(engine)
    try:
        await run_session(transport, runner_args, settings, session_factory)
    finally:
        try:
            await engine.dispose()
        except Exception:
            logger.warning("Database engine cleanup skipped during shutdown")


async def mark_conversation_ended(session_factory, conversation_id) -> None:
    """Mark a conversation row as ended. Best-effort — logs on failure."""
    try:
        async with session_factory() as db:
            await db.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .values(status="ended", ended_at=datetime.now(UTC))
            )
            await db.commit()
        logger.info(f"Voice conversation ended: {conversation_id}")
    except Exception:
        logger.warning(f"Could not mark conversation {conversation_id} as ended")


class _SessionState:
    """Mutable state shared between run_session and _run_admitted_session."""
    __slots__ = ("conversation_id", "rewriter", "slot_handed_over")

    def __init__(self):
        self.conversation_id = None
        self.rewriter = None
        self.slot_handed_over = False


async def run_session(transport, runner_args, settings, session_factory):
    """Admit the call, then delegate to _run_admitted_session.

    The admission check (concurrency limiter) happens BEFORE any database work
    or persistence, so a refused call creates no conversation row and touches no
    external state.  If setup fails after the row is created, the finally block
    marks the row "ended" and releases the call slot.
    """
    limiter = get_call_limiter(settings)
    if not try_admit(limiter):
        return  # refused — nothing persisted, nothing to clean up

    state = _SessionState()
    try:
        await _run_admitted_session(
            transport, runner_args, settings, session_factory, limiter, state,
        )
    finally:
        if not state.slot_handed_over:
            limiter.release()
            if state.conversation_id is not None:
                await mark_conversation_ended(session_factory, state.conversation_id)
            if state.rewriter is not None:
                try:
                    await state.rewriter.close()
                except Exception:
                    logger.warning("Query rewriter cleanup skipped during shutdown")


async def _run_admitted_session(transport, runner_args, settings, session_factory, limiter, state):
    """The body of a call that has already been admitted by the limiter."""
    async with session_factory() as session:
        agent = await session.scalar(
            select(Agent).where(
                Agent.id == settings.voice_agent_id, Agent.status == "active"
            )
        )
        if agent is None:
            raise RuntimeError("Voice agent not found. Check VOICE_AGENT_ID in .env")

        conversation = Conversation(
            organization_id=agent.organization_id,
            agent_id=agent.id,
            channel="voice",
        )
        session.add(conversation)
        await session.commit()
        org_id = agent.organization_id
        conversation_id = conversation.id
        state.conversation_id = conversation_id
        agent_prompt = agent.system_prompt

    logger.info(f"Voice conversation started: {conversation_id}")

    # Load the embedding model before the visitor asks anything
    await asyncio.to_thread(get_embedding_model)

    async def save_message(role: str, content: str, latency: dict) -> None:
        try:
            async with session_factory() as db:
                db.add(
                    Message(
                        organization_id=org_id,
                        conversation_id=conversation_id,
                        role=role,
                        content=content,
                        latency_ms=latency,
                    )
                )
                await db.commit()
        except Exception:
            logger.exception("Could not save voice message")

    groq_key = settings.groq_api_key.get_secret_value()
    llm_settings, effort_label = build_llm_settings(
        settings.groq_model,
        build_voice_prompt(agent_prompt),
        settings.voice_reasoning_effort,
    )
    logger.info(f"Voice LLM reasoning_effort: {effort_label}")

    recorder = VoiceTurnRecorder(
        save_message,
        config={
            "reasoning_effort": effort_label,
            "greeting": "fixed",
            "tts_provider": settings.tts_provider,
            "rag": "on",
        },
    )

    stt = GroqSTTService(api_key=groq_key)
    llm = GroqLLMService(api_key=groq_key, settings=llm_settings)
    tts = build_tts(settings)
    logger.info(f"Voice TTS provider: {settings.tts_provider}")

    # VOICE-01: tracked so on_client_disconnected below can wait for an in-flight
    # save_lead commit instead of cancelling it mid-write.
    pending_lead_saves = PendingLeadSaves()
    llm.register_function(
        "save_lead",
        build_save_lead_handler(
            session_factory=session_factory,
            organization_id=org_id,
            conversation_id=conversation_id,
            pending=pending_lead_saves,
        ),
    )
    logger.info("Registered tools: save_lead")

    # Short timeout: in a voice call a slow rewrite is worse than no rewrite
    rewriter = GroqLLM(
        api_key=groq_key,
        model=settings.groq_model,
        timeout_seconds=4.0,
        max_output_tokens=200,
        reasoning_effort="low",
    )
    state.rewriter = rewriter
    knowledge_injector = KnowledgeInjector(
        session_factory=session_factory,
        organization_id=org_id,
        settings=settings,
        rewriter=rewriter,
        on_retrieval=recorder.record_retrieval,
        conversation_id=conversation_id,
    )

    context = LLMContext(tools=ToolsSchema(standard_tools=[build_save_lead_schema()]))
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    @user_aggregator.event_handler("on_user_turn_stopped")
    async def on_user_turn_stopped(aggregator, strategy, message):
        await recorder.user_turn_finished(message.content)

    @assistant_aggregator.event_handler("on_assistant_turn_stopped")
    async def on_assistant_turn_stopped(aggregator, message):
        await recorder.assistant_turn_finished(message.content, message.interrupted)

    ttfb_observer = ServiceTTFBObserver(recorder.record_service_ttfb)
    latency_observer = UserBotLatencyObserver()

    @latency_observer.event_handler("on_latency_measured")
    async def on_latency_measured(observer, latency):
        recorder.record_voice_to_voice(latency)

    @latency_observer.event_handler("on_first_bot_speech_latency")
    async def on_first_bot_speech_latency(observer, latency):
        recorder.record_first_speech(latency)

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            knowledge_injector,
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        observers=[ttfb_observer, latency_observer],
        idle_timeout_secs=runner_args.pipeline_idle_timeout_secs,
        processor_unusable_policy=ProcessorUnusablePolicy.END,
    )

    runner = WorkerRunner(handle_sigint=runner_args.handle_sigint)
    await runner.add_workers(worker)

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Visitor connected")
        await worker.queue_frames([TTSSpeakFrame(settings.voice_greeting)])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Visitor disconnected")
        # VOICE-01: give an in-flight save_lead commit a brief grace period instead of
        # cancelling it mid-write - a visitor who just said "yes" should not silently lose
        # their lead because they closed the tab at the exact wrong moment. Bounded so a
        # genuinely stuck save can never block shutdown forever.
        idle = await pending_lead_saves.wait_idle(settings.lead_save_disconnect_grace_secs)
        if not idle:
            logger.warning(
                "Visitor disconnected while a save_lead commit was still in flight after "
                f"waiting {settings.lead_save_disconnect_grace_secs}s; cancelling anyway."
            )
        await runner.cancel()

    async def cleanup() -> None:
        try:
            await rewriter.close()
        except Exception:
            logger.warning("Query rewriter cleanup skipped during shutdown")
        await mark_conversation_ended(session_factory, conversation_id)

    # Hand the slot to run_with_call_guards — from here the guards own the
    # limiter release and the cleanup.  The outer finally must not release again.
    state.slot_handed_over = True

    # Run under the concurrency limiter and duration watchdog (see voice/limits.py).
    # The guard is process-local: it bounds concurrent calls within this runner
    # process, not across replicas. See DEPLOYMENT.md section 6.6.
    await run_with_call_guards(
        runner=runner,
        limiter=limiter,
        max_call_seconds=settings.voice_max_call_seconds,
        cleanup=cleanup,
        admitted=True,
    )


async def bot(runner_args: RunnerArguments):
    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import app, main

    check_startup_config()
    # Installed before main() serves anything, so the token check wraps every
    # route the runner registers (/start, /api/offer, /sessions/..., webhooks, ws).
    _voice_secret = get_settings().voice_token_secret
    install_voice_auth(app, _voice_secret.get_secret_value() if _voice_secret else None)
    # Browser client source and the local demo page. Not served in production: customers
    # vendor voice-client.js into their own frontend. Contains no secrets.
    if get_settings().app_env != "production":
        from pathlib import Path

        from fastapi.staticfiles import StaticFiles

        app.mount(
            "/voice-client",
            StaticFiles(directory=Path(__file__).parent / "static"),
            name="voice-client",
        )
    main()