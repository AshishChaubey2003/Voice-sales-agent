"""Opt-in online monitoring: cheap deterministic signals and alerts, off the request path.

Off unless ONLINE_MONITORING=true. A conversation turn submits a TurnEvent (counts,
timings, statuses, error TYPE only; never message text, names or emails). A background
worker keeps a rolling window, applies alert rules and sends alerts through a sink.

Properties (all covered by tests/test_monitoring.py):
* submit() never blocks or raises: a full queue drops the event and counts the drop.
* a turn id is processed at most once (duplicate submissions are ignored).
* the worker runs in an empty contextvars context with tracing disabled, so it can never
  nest under, or add traces to, the request that started it, and an evaluator hook
  cannot trace itself recursively.
* sink failures and timeouts are counted, never raised; alerts have per-rule cooldowns.
* state is in-process: it resets on restart and covers one worker process only.

What is NOT here: LLM-judge scoring of live traffic. `judge_hook` is an extension point
only; wiring a judge to production text needs explicit approval and a privacy review.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)

MIN_SAMPLES_DEFAULT = 20


@dataclass(frozen=True)
class TurnEvent:
    turn_id: str
    session_id: str
    channel: str
    ok: bool
    organization_id: str | None = None
    error_type: str | None = None
    llm_ms: int | None = None
    rag_ms: int | None = None
    n_sources: int | None = None
    lead_status: str | None = None
    reply_chars: int | None = None
    secret_kinds: tuple[str, ...] = ()


@dataclass(frozen=True)
class AlertRule:
    name: str
    metric: str  # error_rate | p95_llm_ms | empty_retrieval_rate | lead_rejected_rate | secret_in_reply_count
    threshold: float
    severity: str = "warning"
    min_samples: int = MIN_SAMPLES_DEFAULT  # 0 means "fire on the first occurrence"
    cooldown_seconds: float = 900.0


# PROVISIONAL starting points: not calibrated on real traffic.
DEFAULT_RULES = (
    AlertRule("elevated_error_rate", "error_rate", 0.05, "critical"),
    AlertRule("latency_regression_p95", "p95_llm_ms", 8000, "warning"),
    AlertRule("secret_in_reply", "secret_in_reply_count", 1, "critical", min_samples=0),
)


@dataclass
class Alert:
    rule: str
    severity: str
    metric: str
    value: float
    threshold: float
    samples: int
    at: float
    simulated: bool = True  # only a sink that really delivers sets this to False
    delivered: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class AlertSink(Protocol):
    async def send(self, alert: Alert) -> Alert: ...


class LogAlertSink:
    """Local/test sink: logs a SIMULATED alert. Nothing leaves the process."""

    async def send(self, alert: Alert) -> Alert:
        logger.warning("SIMULATED ALERT (not delivered) %s: %s=%.4g threshold=%.4g n=%d",
                       alert.rule, alert.metric, alert.value, alert.threshold, alert.samples)
        return alert


class FileAlertSink:
    """Appends alerts as JSON lines. Still simulated: a file is not a delivered notification."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)

    async def send(self, alert: Alert) -> Alert:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(alert.to_dict()) + "\n")
        return alert


class WebhookAlertSink:
    """Posts the alert JSON to a URL. delivered=True only after an HTTP 2xx response."""

    def __init__(self, url: str, *, timeout: float = 5.0, transport: httpx.AsyncBaseTransport | None = None):
        self._url = url
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)

    async def send(self, alert: Alert) -> Alert:
        response = await self._client.post(self._url, json=alert.to_dict())
        response.raise_for_status()
        alert.simulated, alert.delivered = False, True
        return alert


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct / 100
    low, high = int(rank), min(int(rank) + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


class OnlineMonitor:
    def __init__(
        self,
        sink: AlertSink | None = None,
        rules: tuple[AlertRule, ...] = DEFAULT_RULES,
        *,
        window: int = 200,
        queue_size: int = 1000,
        clock=time.monotonic,
        sink_timeout: float = 5.0,
        judge_hook=None,
        seen_limit: int = 5000,
    ) -> None:
        self._sink = sink or LogAlertSink()
        self._rules = rules
        self._events: deque[TurnEvent] = deque(maxlen=window)
        self._queue: asyncio.Queue[TurnEvent] | None = None
        self._queue_size = queue_size
        self._clock = clock
        self._sink_timeout = sink_timeout
        self._judge_hook = judge_hook
        self._seen: deque[str] = deque(maxlen=seen_limit)
        self._seen_set: set[str] = set()
        self._last_fired: dict[str, float] = {}
        self._worker: asyncio.Task | None = None
        self.counters = {"submitted": 0, "processed": 0, "duplicates": 0, "dropped": 0,
                         "alerts_fired": 0, "sink_failures": 0, "hook_failures": 0}
        self.recent_alerts: deque[dict] = deque(maxlen=20)

    # -- producer side (request path) ---------------------------------------

    def submit(self, event: TurnEvent) -> bool:
        """Queues an event. Never blocks and never raises into the caller."""
        try:
            self.counters["submitted"] += 1
            if self._queue is None:
                self._queue = asyncio.Queue(maxsize=self._queue_size)
            self._ensure_worker()
            self._queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            self.counters["dropped"] += 1
        except Exception:
            self.counters["dropped"] += 1
            logger.warning("Could not queue a monitoring event")
        return False

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            # Empty context: the worker must not inherit the request's trace.
            self._worker = asyncio.get_running_loop().create_task(
                self._run(), context=contextvars.Context()
            )

    # -- consumer side -------------------------------------------------------

    async def _run(self) -> None:
        from api.tracing import untraced

        with untraced():
            while True:
                event = await self._queue.get()
                try:
                    await self.process(event)
                except Exception:
                    logger.warning("Monitoring failed to process an event")

    async def drain(self) -> None:
        """Waits until every queued event is processed (used by tests and shutdown)."""
        if self._queue is None:
            return
        for _ in range(1000):
            if self._queue.empty() and self.counters["processed"] + self.counters["duplicates"] >= (
                self.counters["submitted"] - self.counters["dropped"]
            ):
                return
            await asyncio.sleep(0.001)

    async def process(self, event: TurnEvent) -> list[Alert]:
        if event.turn_id in self._seen_set:
            self.counters["duplicates"] += 1
            return []
        if len(self._seen) == self._seen.maxlen:
            self._seen_set.discard(self._seen[0])
        self._seen.append(event.turn_id)
        self._seen_set.add(event.turn_id)
        self._events.append(event)
        self.counters["processed"] += 1
        if self._judge_hook is not None:
            try:
                await asyncio.wait_for(self._judge_hook(event), timeout=self._sink_timeout)
            except Exception:
                self.counters["hook_failures"] += 1
        return await self._evaluate_rules()

    def metrics(self, organization_id: str | None = None) -> dict[str, Any]:
        events = [e for e in self._events if organization_id is None or e.organization_id == organization_id]
        n = len(events)
        llm = [e.llm_ms for e in events if e.llm_ms is not None]
        retrieved = [e for e in events if e.n_sources is not None]
        leads = [e for e in events if e.lead_status in ("saved", "rejected")]
        return {
            "samples": n,
            "error_rate": (sum(1 for e in events if not e.ok) / n) if n else None,
            "p95_llm_ms": _percentile(llm, 95) if len(llm) >= 20 else None,
            "empty_retrieval_rate": (sum(1 for e in retrieved if e.n_sources == 0) / len(retrieved)) if retrieved else None,
            "lead_rejected_rate": (sum(1 for e in leads if e.lead_status == "rejected") / len(leads)) if leads else None,
            "secret_in_reply_count": sum(1 for e in events if e.secret_kinds),
            "llm_samples": len(llm),
        }

    async def _evaluate_rules(self) -> list[Alert]:
        metrics, fired = self.metrics(), []
        now = self._clock()
        for rule in self._rules:
            value = metrics.get(rule.metric)
            if value is None or metrics["samples"] < rule.min_samples or value < rule.threshold:
                continue
            last = self._last_fired.get(rule.name)
            if last is not None and now - last < rule.cooldown_seconds:
                continue
            self._last_fired[rule.name] = now
            alert = Alert(rule.name, rule.severity, rule.metric, float(value), rule.threshold,
                          metrics["samples"], time.time())
            try:
                alert = await asyncio.wait_for(self._sink.send(alert), timeout=self._sink_timeout)
            except Exception:
                self.counters["sink_failures"] += 1
                logger.warning("Alert sink failed for rule %s", rule.name)
            self.counters["alerts_fired"] += 1
            self.recent_alerts.append(alert.to_dict())
            fired.append(alert)
        return fired

    def org_snapshot(self, organization_id: str) -> dict:
        """What one tenant may see: its own rolling metrics, never other tenants' traffic or alerts."""
        return {
            "scope": "your organization's turns in this process only; resets on restart",
            "metrics": self.metrics(organization_id),
            "rules": [asdict(r) for r in self._rules],
            "sink": type(self._sink).__name__,
            "alerts": "operator-level; see server logs or the configured alert destination",
        }

    def snapshot(self) -> dict:
        return {
            "scope": "this process only; resets on restart",
            "metrics": self.metrics(),
            "counters": dict(self.counters),
            "rules": [asdict(r) for r in self._rules],
            "recent_alerts": list(self.recent_alerts),
            "sink": type(self._sink).__name__,
        }


_monitor: OnlineMonitor | None = None


def get_monitor() -> OnlineMonitor | None:
    """The process monitor, or None when ONLINE_MONITORING is off."""
    global _monitor
    if _monitor is not None:
        return _monitor
    try:
        from api.config import get_settings

        settings = get_settings()
        if not settings.online_monitoring:
            return None
        if settings.alert_webhook_url:
            sink: AlertSink = WebhookAlertSink(settings.alert_webhook_url)
        elif settings.alert_file:
            sink = FileAlertSink(settings.alert_file)
        else:
            sink = LogAlertSink()
        _monitor = OnlineMonitor(sink)
    except Exception:
        logger.warning("Online monitoring could not be configured; it stays off")
        return None
    return _monitor


def _reset_for_tests() -> None:
    global _monitor
    _monitor = None
