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
from voice.ice import ICE_ENV_VAR, describe_ice_env
from voice.lead_tool import (
    VOICE_LEAD_RULES,
    build_save_lead_handler,
    build_save_lead_schema,
)
from voice.limits import CallLimiter, call_duration_watchdog
from voice.metrics_observer import ServiceTTFBObserver
from voice.rag import KnowledgeInjector
from voice.turn_recorder import VoiceTurnRecorder

# One runner process serves every call, so the limiter is shared across calls.
_call_limiter: CallLimiter | None = None


def get_call_limiter(settings) -> CallLimiter:
    global _call_limiter
    if _call_limiter is None:
        _call_limiter = CallLimiter(settings.voice_max_concurrent_calls)
    return _call_limiter

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

    engine = build_engine()
    session_factory = build_session_factory(engine)
    try:
        await run_session(transport, runner_args, settings, session_factory)
    finally:
        try:
            await engine.dispose()
        except Exception:
            logger.warning("Database engine cleanup skipped during shutdown")


async def run_session(transport, runner_args, settings, session_factory):
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

    llm.register_function(
        "save_lead",
        build_save_lead_handler(
            session_factory=session_factory,
            organization_id=org_id,
            conversation_id=conversation_id,
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
    knowledge_injector = KnowledgeInjector(
        session_factory=session_factory,
        organization_id=org_id,
        settings=settings,
        rewriter=rewriter,
        on_retrieval=recorder.record_retrieval,
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
        await runner.cancel()

    async def cleanup() -> None:
        try:
            await rewriter.close()
        except Exception:
            logger.warning("Query rewriter cleanup skipped during shutdown")
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

    # Bound concurrent calls so a public URL cannot run many pipelines at once.
    limiter = get_call_limiter(settings)
    if not limiter.try_acquire():
        logger.warning(
            f"Refusing call: {limiter.active}/{limiter.max_concurrent} "
            "concurrent calls already in progress"
        )
        await runner.cancel()
        await cleanup()
        return

    # Stop a call that runs past the configured limit (for example a tab left open).
    watchdog = asyncio.create_task(
        call_duration_watchdog(settings.voice_max_call_seconds, runner.cancel)
    )
    try:
        await runner.run()
    finally:
        watchdog.cancel()
        limiter.release()
        await cleanup()


async def bot(runner_args: RunnerArguments):
    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()