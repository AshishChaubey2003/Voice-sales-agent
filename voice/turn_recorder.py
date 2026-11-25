from collections.abc import Awaitable, Callable

SaveMessage = Callable[[str, str, dict], Awaitable[None]]


def _service_of(processor_name: str) -> str | None:
    name = (processor_name or "").upper()
    if "STT" in name:
        return "stt"
    if "LLM" in name:
        return "llm"
    if "TTS" in name:
        return "tts"
    return None


def _to_ms(seconds) -> int | None:
    """Seconds to whole milliseconds. Missing, zero, negative or non-numeric values are not measurements."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return None
    if not seconds or seconds <= 0 or seconds != seconds:
        return None
    return round(seconds * 1000)


class VoiceTurnRecorder:
    """Collects per-turn latency metrics and saves finished turns.

    One recorder serves one call, and a call handles one turn at a time, so values collected for a
    turn are cleared when that turn is saved and cannot leak into the next. Two calls use two
    recorders. Every ``record_*`` method is best effort: a malformed metric is dropped and never
    raises into the pipeline. A metric that was never reported stays absent (not zero).

    Stored keys (milliseconds unless stated). See evals/latency_telemetry.py for what each one measures.
      visitor message: stt_ttfb_ms, stt_processing_ms
      assistant message: rag_ms, rag_search_ms, rag_rewrite_ms, llm_ttfb_ms, llm_ttfat_ms,
        llm_processing_ms, tts_ttfb_ms, tts_ttfa_ms, tts_processing_ms, text_aggregation_ms,
        voice_to_voice_ms, first_speech_ms, latency_breakdown, breakdown_from, trace_run_id
    """

    def __init__(self, save_message: SaveMessage, config: dict | None = None) -> None:
        self._save_message = save_message
        self._config = dict(config or {})
        self._stt_ms: int | None = None
        self._stt_processing_ms: int | None = None
        self._clear_assistant_metrics()

    def _clear_assistant_metrics(self) -> None:
        self._llm_ms: int | None = None
        self._llm_ttfat_ms: int | None = None
        self._llm_processing_ms: int | None = None
        self._tts_ms: int | None = None
        self._tts_ttfa_ms: int | None = None
        self._tts_processing_ms: int | None = None
        self._voice_to_voice_ms: int | None = None
        self._first_speech_ms: int | None = None
        self._rag_ms: int | None = None
        self._rag_search_ms: int | None = None
        self._rag_rewrite_ms: int | None = None
        self._rag_error: bool | None = None
        self._rag_sources: list[str] | None = None
        self._rag_rewritten_query: str | None = None
        self._text_aggregation_ms: int | None = None
        self._breakdown: list[dict] | None = None
        self._breakdown_from: str | None = None
        self._trace_run_id: str | None = None

    def record_service_ttfb(self, processor_name: str, seconds: float | None) -> None:
        if not seconds or seconds <= 0:
            return
        ms = round(seconds * 1000)
        name = processor_name.upper()
        if "STT" in name:
            self._stt_ms = ms
        elif "LLM" in name and self._llm_ms is None:
            self._llm_ms = ms
        elif "TTS" in name and self._tts_ms is None:
            self._tts_ms = ms

    def record_service_metric(self, kind: str, processor_name: str, seconds: float | None) -> None:
        """Pipecat's processing time, time to first answer token (LLM) and time to first audible audio (TTS).

        ``processing`` is added up over the turn: a turn can call the LLM twice (tool call, then answer) and
        the TTS once per sentence. ``ttfat`` and ``ttfa`` keep the first value, which is how quickly the
        service began responding.
        """
        try:
            ms = _to_ms(seconds)
            service = _service_of(processor_name)
            if ms is None or service is None:
                return
            if kind == "processing":
                if service == "stt":
                    self._stt_processing_ms = (self._stt_processing_ms or 0) + ms
                elif service == "llm":
                    self._llm_processing_ms = (self._llm_processing_ms or 0) + ms
                else:
                    self._tts_processing_ms = (self._tts_processing_ms or 0) + ms
            elif kind == "ttfat" and service == "llm" and self._llm_ttfat_ms is None:
                self._llm_ttfat_ms = ms
            elif kind == "ttfa" and service == "tts" and self._tts_ttfa_ms is None:
                self._tts_ttfa_ms = ms
        except Exception:
            return

    def record_voice_to_voice(self, seconds: float | None) -> None:
        if seconds and seconds > 0:
            self._voice_to_voice_ms = round(seconds * 1000)

    def record_first_speech(self, seconds: float | None) -> None:
        if seconds and seconds > 0:
            self._first_speech_ms = round(seconds * 1000)

    def record_retrieval(
        self, elapsed_ms: int, sources: list[str], rewritten_query: str | None
    ) -> None:
        self._rag_ms = elapsed_ms
        self._rag_sources = list(sources)
        self._rag_rewritten_query = rewritten_query

    def record_retrieval_detail(
        self,
        *,
        search_ms: int | None = None,
        rewrite_ms: int | None = None,
        error: bool = False,
        trace_run_id: str | None = None,
    ) -> None:
        """Where the retrieval step's time went, whether a search failed, and the LangSmith run id if one exists."""
        try:
            self._rag_search_ms = search_ms if isinstance(search_ms, int) and search_ms >= 0 else None
            self._rag_rewrite_ms = rewrite_ms if isinstance(rewrite_ms, int) and rewrite_ms >= 0 else None
            self._rag_error = True if error else None
            self._trace_run_id = str(trace_run_id) if trace_run_id else None
        except Exception:
            return

    def record_breakdown(self, breakdown) -> None:
        """Pipecat's per-turn latency breakdown (the named parts of user-stopped to bot-started)."""
        try:
            parts = []
            for part in getattr(breakdown, "contributions", None) or []:
                ms = _to_ms(part.duration_secs)
                if ms is not None and isinstance(part.key, str):
                    parts.append({"key": part.key, "ms": ms})
            if not parts:
                return
            self._breakdown = parts
            measured_from = getattr(breakdown, "measured_from", None)
            self._breakdown_from = str(getattr(measured_from, "value", measured_from) or "") or None
            aggregation = getattr(breakdown, "text_aggregation", None)
            self._text_aggregation_ms = _to_ms(getattr(aggregation, "duration_secs", None))
        except Exception:
            return

    async def user_turn_finished(self, text: str | None) -> None:
        metrics = {"stt_ttfb_ms": self._stt_ms, "stt_processing_ms": self._stt_processing_ms}
        self._stt_ms = None
        self._stt_processing_ms = None
        text = (text or "").strip()
        if text:
            await self._save_message("user", text, self._build_latency(metrics))

    async def assistant_turn_finished(self, text: str | None, interrupted: bool) -> None:
        metrics = {
            "llm_ttfb_ms": self._llm_ms,
            "llm_ttfat_ms": self._llm_ttfat_ms,
            "llm_processing_ms": self._llm_processing_ms,
            "tts_ttfb_ms": self._tts_ms,
            "tts_ttfa_ms": self._tts_ttfa_ms,
            "tts_processing_ms": self._tts_processing_ms,
            "voice_to_voice_ms": self._voice_to_voice_ms,
            "first_speech_ms": self._first_speech_ms,
            "rag_ms": self._rag_ms,
            "rag_search_ms": self._rag_search_ms,
            "rag_rewrite_ms": self._rag_rewrite_ms,
            "rag_error": self._rag_error,
            "rag_sources": self._rag_sources,
            "rag_rewritten_query": self._rag_rewritten_query,
            "text_aggregation_ms": self._text_aggregation_ms,
            "latency_breakdown": self._breakdown,
            "breakdown_from": self._breakdown_from,
            "trace_run_id": self._trace_run_id,
        }
        self._clear_assistant_metrics()
        text = (text or "").strip()
        if text:
            await self._save_message(
                "assistant",
                text,
                self._build_latency(metrics, interrupted=bool(interrupted)),
            )

    def _build_latency(self, metrics: dict, **extra) -> dict:
        data = {key: value for key, value in metrics.items() if value is not None}
        data.update(extra)
        data.update(self._config)
        return data
