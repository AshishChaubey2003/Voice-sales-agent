"""Dashboard API: authentication and tenant isolation.

No database is needed: the organization dependency and the session are replaced
with fakes, and runs are real files in a temporary directory. The 401 test uses
the real dependency, which rejects a missing key before any database access.
"""
import uuid
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from api import eval_routes
from api.deps import get_current_org, get_session
from api.main import app
from evals import runner, store
from evals.schema import Dataset, parse_case

ORG_A = SimpleNamespace(id=uuid.uuid4())
ORG_B = SimpleNamespace(id=uuid.uuid4())
RUN_A = "20260101T000000Z-aaaaaaaa"
RUN_B = "20260102T000000Z-bbbbbbbb"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def make_run(org, run_id, ok=True):
    case = parse_case({
        "id": "c1", "kind": "rag_answer", "category": "factual",
        "messages": [{"role": "user", "content": "How much is Pro?"}],
        "expected_sources": ["P"], "required_facts": ["29"],
    })

    class Target:
        async def run_case(self, c):
            reply = "It is $29." if ok else "No idea."
            return {"reply": reply, "retrieved_sources": ["P"] if ok else [], "wall_ms": 100}

    import asyncio

    results, status = asyncio.run(
        runner.run_suite(Dataset("t", "t", "abc", [case]), Target(), mode="full")
    )
    meta = {"run_id": run_id, "status": status, "mode": "full", "started_at": "2026-01-01T00:00:00+00:00",
            "organization_id": str(org.id), "dataset": {"name": "t", "sha256": "abc", "n_cases": 1, "reviewed_cases": 0},
            "model": {"name": "m"}, "judge": {"enabled": False}, "langsmith": {"status": "off"}}
    return runner.finalize_run(meta, results), results


@pytest.fixture
def results_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(eval_routes, "_results_dir", lambda: str(tmp_path / "results"))
    monkeypatch.setattr(eval_routes, "_baselines_dir", lambda: str(tmp_path / "baselines"))
    run_a, cases_a = make_run(ORG_A, RUN_A, ok=False)
    run_b, cases_b = make_run(ORG_B, RUN_B, ok=True)
    store.write_run(tmp_path / "results", run_a, cases_a, "")
    store.write_run(tmp_path / "results", run_b, cases_b, "")
    store.save_baseline(tmp_path / "baselines", "org-a-base", run_a)
    store.save_baseline(tmp_path / "baselines", "org-b-base", run_b)
    return tmp_path


class FakeSession:
    def __init__(self, rows=()):
        self.rows = list(rows)

    async def execute(self, statement):
        return SimpleNamespace(all=lambda: self.rows)


def client_as(org, rows=()):
    app.dependency_overrides[get_current_org] = lambda: org

    async def fake_session():
        yield FakeSession(rows)

    app.dependency_overrides[get_session] = fake_session
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
def clean_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_data_routes_require_an_api_key():
    paths = ["/v1/evals/runs", f"/v1/evals/runs/{RUN_A}", f"/v1/evals/runs/{RUN_A}/cases",
             "/v1/evals/baselines", f"/v1/evals/runs/{RUN_A}/compare?baseline=x", "/v1/evals/voice-latency",
             "/v1/evals/registry", f"/v1/evals/runs/{RUN_A}/metrics", "/v1/evals/monitoring"]
    async def fake_session():
        yield FakeSession()

    # Only the session is faked. The real get_current_org runs and must refuse
    # a request without a key before it ever queries.
    app.dependency_overrides[get_session] = fake_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for path in paths:
            response = await client.get(path)
            assert response.status_code == 401, path
            assert response.json() == {"detail": "Missing API key"}


@pytest.mark.anyio
async def test_each_organization_lists_only_its_own_runs(results_dir):
    async with client_as(ORG_A) as client:
        runs = (await client.get("/v1/evals/runs")).json()["runs"]
    assert [r["run_id"] for r in runs] == [RUN_A]
    async with client_as(ORG_B) as client:
        runs = (await client.get("/v1/evals/runs")).json()["runs"]
    assert [r["run_id"] for r in runs] == [RUN_B]


@pytest.mark.anyio
async def test_another_organizations_run_is_indistinguishable_from_a_missing_one(results_dir):
    async with client_as(ORG_A) as client:
        other = await client.get(f"/v1/evals/runs/{RUN_B}")
        missing = await client.get("/v1/evals/runs/20260109T000000Z-99999999")
        cases = await client.get(f"/v1/evals/runs/{RUN_B}/cases")
        compare_other = await client.get(f"/v1/evals/runs/{RUN_B}/compare?baseline=org-b-base")
    assert other.status_code == missing.status_code == cases.status_code == 404
    assert other.json() == missing.json() == {"detail": "Run not found"}
    assert compare_other.status_code == 404


@pytest.mark.anyio
async def test_malformed_run_ids_never_reach_the_filesystem(results_dir):
    async with client_as(ORG_A) as client:
        for bad in ["not-a-run", "..%2F..%2Fetc%2Fpasswd", "20260101T000000Z-AAAAAAAA", "x" * 300]:
            response = await client.get(f"/v1/evals/runs/{bad}")
            assert response.status_code == 404, bad


@pytest.mark.anyio
async def test_run_detail_and_failed_cases(results_dir):
    async with client_as(ORG_A) as client:
        detail = (await client.get(f"/v1/evals/runs/{RUN_A}")).json()
        cases = (await client.get(f"/v1/evals/runs/{RUN_A}/cases")).json()
        passing = (await client.get(f"/v1/evals/runs/{RUN_A}/cases?failed_only=false")).json()
    assert detail["metrics"]["run.pass_rate"]["value"] == 0.0
    assert cases["total"] == 1 and cases["cases"][0]["case_id"] == "c1"
    assert cases["cases"][0]["failure_reasons"]
    assert passing["total"] == 1  # the only case failed, so unfiltered equals filtered


@pytest.mark.anyio
async def test_baselines_and_compare_are_scoped_to_the_organization(results_dir):
    async with client_as(ORG_A) as client:
        names = (await client.get("/v1/evals/baselines")).json()["baselines"]
        own = await client.get(f"/v1/evals/runs/{RUN_A}/compare?baseline=org-a-base")
        foreign = await client.get(f"/v1/evals/runs/{RUN_A}/compare?baseline=org-b-base")
        traversal = await client.get(f"/v1/evals/runs/{RUN_A}/compare?baseline=../x")
    assert names == ["org-a-base"]
    assert own.status_code == 200 and own.json()["regressions"] == []
    assert foreign.status_code == 404 and traversal.status_code == 404


@pytest.mark.anyio
async def test_limit_is_validated(results_dir):
    async with client_as(ORG_A) as client:
        assert (await client.get("/v1/evals/runs?limit=0")).status_code == 422
        assert (await client.get("/v1/evals/runs?limit=1000")).status_code == 422


@pytest.mark.anyio
async def test_empty_state_when_no_runs_exist(tmp_path, monkeypatch):
    monkeypatch.setattr(eval_routes, "_results_dir", lambda: str(tmp_path / "none"))
    async with client_as(ORG_A) as client:
        response = await client.get("/v1/evals/runs")
    assert response.status_code == 200 and response.json() == {"runs": []}


@pytest.mark.anyio
async def test_voice_latency_aggregates_stored_telemetry():
    rows = [("assistant", {"llm_ttfb_ms": 300, "voice_to_voice_ms": 900, "interrupted": True, "reasoning_effort": "low"}),
            ("assistant", {"llm_ttfb_ms": 500, "voice_to_voice_ms": 1100, "interrupted": False, "reasoning_effort": "low"}),
            ("user", {"stt_ttfb_ms": 120, "reasoning_effort": "low"})]
    async with client_as(ORG_A, rows) as client:
        body = (await client.get("/v1/evals/voice-latency?days=7")).json()
    config = next(iter(body["configs"].values()))
    assert config["llm_ttfb_ms"]["n"] == 2 and config["llm_ttfb_ms"]["p50"] == 300
    assert config["llm_ttfb_ms"]["p95"] is None  # two observations are not enough
    assert body["assistant_turns"] == 2 and body["interruption_rate"] == 0.5
    assert body["window_days"] == 7


@pytest.mark.anyio
async def test_dashboard_page_has_no_data_and_a_strict_csp():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.get("/dashboard/evals")
        second = await client.get("/dashboard/evals")
    assert first.status_code == 200
    csp = first.headers["content-security-policy"]
    nonce = csp.split("script-src 'nonce-")[1].split("'")[0]
    assert f'nonce="{nonce}"' in first.text and "__NONCE__" not in first.text
    assert "default-src 'none'" in csp and "connect-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert second.headers["content-security-policy"] != csp  # a fresh nonce per response
    assert first.headers["cache-control"] == "no-store"
    assert "innerHTML" not in first.text  # untrusted strings are inserted as text only
    for forbidden in ("http://", "cdn.", "<iframe", "eval("):
        assert forbidden not in first.text.replace("http://test", "")


@pytest.mark.anyio
async def test_registry_and_resolved_metrics_are_honest_and_tenant_scoped(results_dir):
    async with client_as(ORG_A) as client:
        catalogue = (await client.get("/v1/evals/registry")).json()
        resolved = (await client.get(f"/v1/evals/runs/{RUN_A}/metrics")).json()["metrics"]
        other = await client.get(f"/v1/evals/runs/{RUN_B}/metrics")
    assert "provisional" in catalogue["thresholds_note"]
    assert other.status_code == 404
    by_name = {m["name"]: m for m in resolved}
    assert by_name["judge.faithfulness"]["status"] == "unavailable"  # judge was off
    assert by_name["run.pass_rate"]["status"] == "provisional"
    assert by_name["run.pass_rate"]["rate"]["denominator"] == 1


@pytest.mark.anyio
async def test_monitoring_route_is_honest_when_off_and_tenant_scoped_when_on(monkeypatch):
    import api.monitoring as monitoring

    monkeypatch.setattr(monitoring, "get_monitor", lambda: None)
    async with client_as(ORG_A) as client:
        off = (await client.get("/v1/evals/monitoring")).json()
    assert off["enabled"] is False and "no live monitoring" in off["note"]

    mon = monitoring.OnlineMonitor()
    for org, ok in ((ORG_A, False), (ORG_B, True), (ORG_B, True)):
        await mon.process(monitoring.TurnEvent(turn_id=str(uuid.uuid4()), session_id="s", channel="chat", ok=ok,
                                               organization_id=str(org.id)))
    monkeypatch.setattr(monitoring, "get_monitor", lambda: mon)
    async with client_as(ORG_B) as client:
        on = (await client.get("/v1/evals/monitoring")).json()
    assert on["enabled"] is True and on["metrics"]["samples"] == 2 and on["metrics"]["error_rate"] == 0
    assert "counters" not in on


def make_judged_run(org, run_id):
    """A case that passes every deterministic check while a judge fails it, plus token usage."""
    import asyncio
    from types import SimpleNamespace as NS

    from evals import judge_suite

    case = parse_case({
        "id": "c1", "kind": "rag_answer", "category": "factual",
        "messages": [{"role": "user", "content": "How much is Pro?"}],
        "expected_sources": ["P"], "required_facts": ["29"],
    })

    class Target:
        async def run_case(self, c):
            return {"reply": "It is $29.", "retrieved_sources": ["P"], "context": ["P\nPro is $29."], "wall_ms": 100}

    class Provider:
        async def generate(self, system, history, tools=None, tool_choice=None):
            return NS(text='{"verdict": "fail", "score": 0.2, "reason": "Unsupported.", "evidence": []}')

    results, status = asyncio.run(runner.run_suite(
        Dataset("t", "t", "abc", [case]), Target(), mode="full", suite=judge_suite.JudgeSuite(Provider(), "fake/m")))
    meta = {"run_id": run_id, "status": status, "mode": "full", "started_at": "2026-01-03T00:00:00+00:00",
            "organization_id": str(org.id), "dataset": {"name": "t", "sha256": "abc", "n_cases": 1, "reviewed_cases": 0},
            "model": {"name": "m"}, "judge": {"enabled": False}, "judge_suite": {"enabled": True, "model": "fake/m", "provider": "groq"},
            "langsmith": {"status": "off"},
            "usage": {"agent": {"calls_with_usage": 1, "calls_without_usage": 0, "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                      "cost": None, "cost_note": "not computed: no pricing is configured in this project"}}
    return runner.finalize_run(meta, results), results


@pytest.fixture
def judged_dir(tmp_path, monkeypatch):
    # Built synchronously: the run is produced with asyncio.run, which cannot nest in an async test.
    monkeypatch.setattr(eval_routes, "_results_dir", lambda: str(tmp_path / "results"))
    run, cases = make_judged_run(ORG_A, RUN_A)
    store.write_run(tmp_path / "results", run, cases, "")
    assert cases[0]["passed"] is True  # evaluators never change pass/fail
    return tmp_path


@pytest.mark.anyio
async def test_a_case_an_evaluator_flags_is_listed_even_though_it_passed(judged_dir):
    async with client_as(ORG_A) as client:
        listed = (await client.get(f"/v1/evals/runs/{RUN_A}/cases")).json()
        all_cases = (await client.get(f"/v1/evals/runs/{RUN_A}/cases?failed_only=false")).json()
        summary = (await client.get("/v1/evals/runs")).json()["runs"][0]
    assert listed["total"] == 1 and listed["cases"][0]["case_id"] == "c1"
    assert any(e["evaluator"] == "faithfulness" and e["verdict"] == "fail" for e in listed["cases"][0]["evaluators"])
    assert all_cases["total"] == 1
    assert summary["headline"]["eval_faithfulness"] == 0.2 and summary["headline"]["eval_pii_leak"] == 1.0
    assert summary["judge_suite"]["model"] == "fake/m"


@pytest.mark.anyio
async def test_run_detail_exposes_evaluator_summary_and_reported_tokens_only(judged_dir):
    async with client_as(ORG_A) as client:
        body = (await client.get(f"/v1/evals/runs/{RUN_A}")).json()
    assert body["evaluators"]["faithfulness"]["n_scored"] == 1 and body["evaluators"]["correctness"]["n_skipped"] == 1
    assert body["usage"]["agent"]["total_tokens"] == 15 and body["usage"]["cost"] is None
