import asyncio
import json
import uuid

import httpx
import pytest

from api import monitoring as m


def ev(i=None, **kw):
    base = dict(turn_id=i or str(uuid.uuid4()), session_id="s", channel="chat", ok=True, organization_id="org-a",
                llm_ms=100, rag_ms=10, n_sources=2)
    base.update(kw)
    return m.TurnEvent(**base)


class Sink:
    def __init__(self, fail=False, hang=False):
        self.alerts, self.fail, self.hang = [], fail, hang

    async def send(self, alert):
        if self.hang:
            await asyncio.sleep(10)
        if self.fail:
            raise RuntimeError("down")
        self.alerts.append(alert)
        return alert


def run(coro):
    return asyncio.run(coro)


def test_duplicate_turn_ids_are_processed_once():
    async def go():
        mon = m.OnlineMonitor(Sink())
        await mon.process(ev("t1"))
        await mon.process(ev("t1"))
        return mon.counters
    counters = run(go())
    assert counters["processed"] == 1 and counters["duplicates"] == 1


def test_error_rate_alert_needs_enough_samples_and_respects_cooldown():
    async def go():
        sink, now = Sink(), [0.0]
        mon = m.OnlineMonitor(sink, clock=lambda: now[0])
        for _ in range(10):                      # 100% errors but only 10 samples
            await mon.process(ev(ok=False, error_type="LLMError"))
        assert sink.alerts == []
        for _ in range(10):
            await mon.process(ev(ok=False, error_type="LLMError"))
        assert [a.rule for a in sink.alerts] == ["elevated_error_rate"]
        for _ in range(5):
            await mon.process(ev(ok=False))      # still bad, inside cooldown
        assert len(sink.alerts) == 1
        now[0] = 10_000
        await mon.process(ev(ok=False))
        return sink.alerts
    assert len(run(go())) == 2


def test_latency_alert_needs_20_latency_samples():
    async def go():
        sink = Sink()
        mon = m.OnlineMonitor(sink, rules=(m.AlertRule("slow", "p95_llm_ms", 1000),))
        for _ in range(19):
            await mon.process(ev(llm_ms=9000))
        none_yet = list(sink.alerts)
        await mon.process(ev(llm_ms=9000))
        return none_yet, sink.alerts
    none_yet, alerts = run(go())
    assert none_yet == [] and alerts[0].value == 9000


def test_secret_in_reply_fires_on_first_occurrence():
    async def go():
        sink = Sink()
        mon = m.OnlineMonitor(sink)
        await mon.process(ev(secret_kinds=("SECRET",)))
        return sink.alerts
    (alert,) = run(go())
    assert alert.rule == "secret_in_reply" and alert.severity == "critical"


def test_sink_failure_and_timeout_are_counted_not_raised():
    async def go(sink):
        mon = m.OnlineMonitor(sink, sink_timeout=0.05)
        await mon.process(ev(secret_kinds=("SECRET",)))
        return mon.counters
    assert run(go(Sink(fail=True)))["sink_failures"] == 1
    assert run(go(Sink(hang=True)))["sink_failures"] == 1


def test_local_sinks_are_marked_simulated_and_webhook_only_after_2xx(tmp_path):
    alert = m.Alert("r", "warning", "error_rate", 0.5, 0.05, 30, 0.0)
    out = run(m.FileAlertSink(str(tmp_path / "a.jsonl")).send(alert))
    assert out.simulated and not out.delivered
    assert json.loads((tmp_path / "a.jsonl").read_text())["simulated"] is True

    ok = m.WebhookAlertSink("https://example.invalid/hook", transport=httpx.MockTransport(lambda r: httpx.Response(204)))
    sent = run(ok.send(m.Alert("r", "warning", "error_rate", 0.5, 0.05, 30, 0.0)))
    assert sent.delivered and not sent.simulated

    bad = m.WebhookAlertSink("https://example.invalid/hook", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(httpx.HTTPStatusError):
        run(bad.send(m.Alert("r", "warning", "error_rate", 0.5, 0.05, 30, 0.0)))


def test_submit_never_blocks_drops_when_full_and_worker_processes_events():
    async def go():
        mon = m.OnlineMonitor(Sink(), queue_size=2)
        mon._queue = asyncio.Queue(maxsize=2)
        mon._worker = asyncio.get_running_loop().create_future()  # pretend a worker exists; nothing consumes
        results = [mon.submit(ev()) for _ in range(4)]
        dropped = mon.counters["dropped"]

        live = m.OnlineMonitor(Sink())
        for _ in range(5):
            live.submit(ev())
        await live.drain()
        return results, dropped, live.counters["processed"]
    results, dropped, processed = run(go())
    assert results == [True, True, False, False] and dropped == 2 and processed == 5


def test_worker_runs_in_an_empty_context_so_it_cannot_inherit_request_state():
    import contextvars
    var = contextvars.ContextVar("request_run", default="none")
    seen = []

    async def hook(event):
        seen.append(var.get())

    async def go():
        var.set("request-run-tree")
        mon = m.OnlineMonitor(Sink(), judge_hook=hook)
        mon.submit(ev())
        await mon.drain()
        await asyncio.sleep(0.01)
    run(go())
    assert seen == ["none"]


def test_events_carry_no_text_fields():
    assert not {"message", "reply", "text", "content", "email", "name"} & set(m.TurnEvent.__dataclass_fields__)


def test_org_snapshot_isolates_tenants():
    async def go():
        mon = m.OnlineMonitor(Sink())
        for _ in range(3):
            await mon.process(ev(organization_id="org-a", ok=False))
        await mon.process(ev(organization_id="org-b"))
        return mon.org_snapshot("org-b"), mon.org_snapshot("org-a")
    b, a = run(go())
    assert b["metrics"]["samples"] == 1 and b["metrics"]["error_rate"] == 0
    assert a["metrics"]["samples"] == 3 and "counters" not in a and "recent_alerts" not in a


def test_disabled_by_default(monkeypatch):
    import types

    import api.config
    m._reset_for_tests()
    monkeypatch.setattr(api.config, "get_settings", lambda: types.SimpleNamespace(online_monitoring=False))
    assert m.get_monitor() is None
    monkeypatch.setattr(api.config, "get_settings", lambda: types.SimpleNamespace(
        online_monitoring=True, alert_webhook_url=None, alert_file=None))
    mon = m.get_monitor()
    assert isinstance(mon, m.OnlineMonitor) and type(mon._sink).__name__ == "LogAlertSink"
    m._reset_for_tests()
