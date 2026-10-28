import uuid

from evals import langsmith_probe as probe


class FakeClient:
    def __init__(self, *, visible_after=0, fail_create=False, wrong_id=False):
        self.created, self.updated, self.reads = [], [], 0
        self.visible_after, self.fail_create, self.wrong_id = visible_after, fail_create, wrong_id
        self.flushed = False

    def create_run(self, **kw):
        if self.fail_create:
            raise PermissionError("secret-looking message must not be surfaced")
        self.created.append(kw)

    def update_run(self, run_id, **kw):
        self.updated.append((run_id, kw))

    def flush(self):
        self.flushed = True

    def read_run(self, run_id):
        self.reads += 1
        if self.reads <= self.visible_after:
            raise LookupError("not found yet")

        class Run:
            id = uuid.uuid4() if self.wrong_id else run_id

        return Run()


def instant():
    t = [0.0]
    return (lambda s: t.__setitem__(0, t[0] + s)), (lambda: t[0])


def test_verified_only_after_read_back_returns_same_id():
    sleep, clock = instant()
    client = FakeClient(visible_after=2)
    result = probe.run_probe(client, "proj", wait_seconds=30, sleep=sleep, clock=clock)
    assert result["submitted"] and result["verified"] and result["error"] is None
    assert client.created[0]["project_name"] == "proj" and client.created[0]["inputs"] == {"probe": True}
    assert client.flushed and client.reads == 3


def test_not_verified_when_run_never_appears():
    sleep, clock = instant()
    result = probe.run_probe(FakeClient(visible_after=999), "p", wait_seconds=6, sleep=sleep, clock=clock)
    assert result["submitted"] and not result["verified"] and result["error"] == "LookupError"


def test_wrong_run_id_is_not_verification():
    sleep, clock = instant()
    result = probe.run_probe(FakeClient(wrong_id=True), "p", wait_seconds=2, sleep=sleep, clock=clock)
    assert not result["verified"]


def test_submit_failure_reports_type_only_never_the_message():
    result = probe.run_probe(FakeClient(fail_create=True), "p")
    assert result == {**result, "submitted": False, "verified": False, "error": "PermissionError"}
    assert "secret" not in str(result)


def test_cli_makes_no_network_call_without_confirm(monkeypatch, capsys):
    from api import config

    class S:
        langsmith_api_key = object()
        langsmith_tracing = True
        langsmith_project = "voice-sales-agent"
        langsmith_endpoint = None

    monkeypatch.setattr(config, "get_settings", lambda: S())
    monkeypatch.setattr(probe, "make_client", lambda s: (_ for _ in ()).throw(AssertionError("network client built")))
    try:
        import langsmith  # noqa: F401
    except ImportError:
        assert probe.main([]) == 2  # SDK missing: reports and stops
        return
    assert probe.main([]) == 0
    out = capsys.readouterr().out
    assert "Plan only" in out and "voice-sales-agent" in out


def test_cli_stops_cleanly_without_key(monkeypatch, capsys):
    from api import config

    class S:
        langsmith_api_key = None
        langsmith_tracing = False
        langsmith_project = "p"
        langsmith_endpoint = None

    monkeypatch.setattr(config, "get_settings", lambda: S())
    assert probe.main(["--confirm"]) == 2
    assert "value never printed" in capsys.readouterr().out


def test_recent_runs_returns_metadata_only():
    class R:
        def __init__(self, i):
            self.id, self.name, self.run_type, self.status = i, "retrieval", "retriever", "success"
            self.start_time, self.error = "2026-10-27 00:00:00", None
            self.inputs = {"query": "SECRET VISITOR TEXT"}

    class C:
        def list_runs(self, **kw):
            assert kw["project_name"] == "p" and kw["limit"] == 2
            return [R("a"), R("b")]

    rows = probe.recent_runs(C(), "p", 2)
    assert [r["id"] for r in rows] == ["a", "b"] and rows[0]["name"] == "retrieval"
    assert "SECRET" not in str(rows) and "inputs" not in rows[0]


def test_read_run_falls_back_when_deprecated_method_is_removed():
    class New:
        class runs:
            @staticmethod
            def retrieve(run_id):
                return ("new", run_id)

    assert probe._read_run(New(), "x") == ("new", "x")


def test_verify_turn_requires_root_session_and_nested_children():
    from types import SimpleNamespace as NS

    from evals.langsmith_probe import verify_turn

    root = NS(id="r1", parent_run_id=None, name="chat_turn", extra={"metadata": {"session_id": "s1"}})
    child = NS(id="c1", parent_run_id="r1", name="retrieval", extra={})

    class Client:
        def __init__(self, runs):
            self.runs = runs

        def list_runs(self, **kw):
            assert kw["trace_id"] == "t1"
            return iter(self.runs)

    ok = verify_turn(Client([root, child]), "p", "t1", "s1", wait_seconds=0)
    assert ok["verified"] and ok["children_nested"]
    assert not verify_turn(Client([root, child]), "p", "t1", "other", wait_seconds=0)["verified"]
    assert not verify_turn(Client([root]), "p", "t1", "s1", wait_seconds=0)["verified"]
    orphan = NS(id="c2", parent_run_id="zzz", name="retrieval", extra={})
    assert not verify_turn(Client([root, orphan]), "p", "t1", "s1", wait_seconds=0)["verified"]

    class Broken:
        def list_runs(self, **kw):
            raise RuntimeError("down")

    assert verify_turn(Broken(), "p", "t1", "s1", wait_seconds=0)["error"] == "RuntimeError"


def test_turn_probe_without_confirm_makes_no_call(monkeypatch, capsys):
    from api import config

    class S:
        langsmith_api_key = object()
        langsmith_tracing = True
        langsmith_project = "voice-sales-agent"
        langsmith_endpoint = None

    monkeypatch.setattr(config, "get_settings", lambda: S())
    monkeypatch.setattr(probe, "emit_turn_probe", lambda: (_ for _ in ()).throw(AssertionError("emitted")))
    monkeypatch.setattr(probe, "make_client", lambda s: (_ for _ in ()).throw(AssertionError("network client built")))
    assert probe.main(["--turn-probe"]) == 0
    assert "Plan only" in capsys.readouterr().out


def _fake_trace(leak=False, with_tool=True, nested=True, usage=True):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace as NS

    t0 = datetime.now(timezone.utc)
    root = NS(id="r", trace_id="t", name="chat_turn", parent_run_id=None, error=None, start_time=t0,
              end_time=t0 + timedelta(milliseconds=900), inputs={"message_chars": 24},
              outputs={"n_sources": 1})
    kids = [
        NS(id="a", trace_id="t", name="retrieval", parent_run_id="r", error=None, inputs={"query_chars": 5}, outputs={"n_chunks": 1}),
        NS(id="b", trace_id="t", name="llm_call", parent_run_id="r" if nested else "zzz", error=None,
           inputs={"n_history_messages": 1}, outputs={"text_chars": 9, **({"usage": {"total_tokens": 12}} if usage else {})}),
    ]
    if with_tool:
        kids.append(NS(id="c", trace_id="t", name="tool_save_lead", parent_run_id="r", error=None, inputs={},
                       outputs={"status": "saved", **({"leaked": "jane@example.com"} if leak else {})}))

    class Client:
        def list_runs(self, **kw):
            return iter([root] if kw.get("is_root") else [root, *kids])

    return Client()


def test_verify_chat_traces_accepts_nested_redacted_traces_and_reports_only_booleans():
    from evals.langsmith_probe import verify_chat_traces

    report = verify_chat_traces(_fake_trace(), "p", [{"conversation_id": "c1", "expect_tool": True}],
                                ["How much is the Pro plan?", "jane@example.com"], wait_seconds=0)
    row = report["conversations"][0]
    assert report["verified"] and row["children"] == {"retrieval": 1, "llm_call": 1, "tool_save_lead": 1}
    assert row["leak_found"] is False and row["token_usage_recorded"] and row["root_ms"] == 900
    assert row["root_input_keys"] == ["message_chars"]
    assert "jane@example.com" not in repr(report)


def test_verify_chat_traces_rejects_leaks_missing_tool_span_and_bad_nesting():
    from evals.langsmith_probe import verify_chat_traces

    conv = [{"conversation_id": "c1", "expect_tool": True}]
    assert not verify_chat_traces(_fake_trace(leak=True), "p", conv, ["jane@example.com"], wait_seconds=0)["verified"]
    assert not verify_chat_traces(_fake_trace(with_tool=False), "p", conv, [], wait_seconds=0)["verified"]
    assert not verify_chat_traces(_fake_trace(nested=False), "p", conv, [], wait_seconds=0)["verified"]
    assert verify_chat_traces(_fake_trace(with_tool=False), "p", [{"conversation_id": "c1", "expect_tool": False}], [], wait_seconds=0)["verified"]
    assert verify_chat_traces(_fake_trace(), "p", [], [], wait_seconds=0)["verified"] is False
