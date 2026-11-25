# PROJECT_AUDIT.md — Voice Sales Agent (Phase 1, read-only audit)

Audit date: 2026-11-25. Method: static inspection of `D:\voice-sales-agent` and its git metadata
through a read-only shell. **No project file was modified except creating this report.**
Nothing was run on Windows in this phase (no pytest, no server, no browser, no microphone).
"Verified" below therefore means: confirmed by direct inspection in this audit, or by a
result the project owner personally ran and pasted earlier. Everything else is marked as such.

## 1. Git and GitHub state (verified by inspection)

| Item | Fact |
|---|---|
| Current branch | `feature/voice-agent-evaluation-dashboard` @ `77e3d57`, in sync with its `origin` branch |
| `main` (local and `origin/main`) | `origin/main` = `cb605c1`; local `main` is 7 commits behind it. Both predate the auth/eval/CI work |
| Remote URL on file | `github.com/AshishChaubey2003/ai-voice-sales-agent.git` (repo was later renamed to `Voice-sales-agent`; GitHub redirects) |
| What the public repo page shows | The default branch (`main`): "48 pytest tests", "no authentication or leads dashboard". **That page is stale relative to the pushed feature branch.** |
| Pushed but not on `main` | `feature/voice-agent-evaluation-dashboard` is on GitHub: 125 files / +21,736 lines vs `origin/main` (auth, voice tokens, `evals/`, dashboard, CI file, docs) |
| Not pushed anywhere | 12 modified + 13 untracked files (see section 5) |
| `.env` | Git-ignored (`.gitignore:2`); not tracked |
| CI | `.github/workflows/ci.yml` exists on the feature branch. It triggers only on `push` to `main` and on pull requests. No evidence it has ever run or passed: **do not add a CI badge or claim CI passing** |

## 2. Architecture (from code)

- **API**: FastAPI (`api/main.py`) with routers for chat (`/v1/conversations`), evaluation (`/v1/evals/*`, `/dashboard/evals`), voice tokens, plus `/healthz` and `/readyz`.
- **Voice**: Pipecat SmallWebRTC bot (`voice/bot.py`), Silero VAD, Groq STT/LLM, Deepgram or Groq TTS, `voice/lead_tool.py` consent-gated `save_lead`, `voice/voice_auth.py` token auth, call limits and ICE hardening.
- **Retrieval**: `voice/rag.py` + `api/knowledge.py`; fastembed embeddings; pgvector + full-text hybrid search with RRF.
- **Persistence**: PostgreSQL 16 + pgvector, SQLAlchemy async, Alembic. Migration chain is linear: `79d5c9dc732d` -> `a33f0d420da5` -> `25ae4fc65be4` -> `b7e1f0a2c3d4` -> `c4d2a91e7f35` (the last one, `turn_evaluations`, is **untracked**).
- **Evaluation**: `evals/` (runner, evaluators, judges, review workflow, registry, aggregate), 75-case golden set and 60-case candidate set (documented as **0 human-reviewed**), live-turn evaluation (`evals/live.py`, `api/live_eval.py`, `scripts/evaluate_live_turns.py`), latency telemetry (`evals/latency_telemetry.py`).
- **Dashboard**: `api/static/eval_dashboard.html`, served at `/dashboard/evals`, reads `/v1/evals/*` with an organization API key.

## 3. Feature classification

Legend: **IV** implemented and verified; **INV** implemented, not verified; **P** partial; **M** missing; **B** broken; **D** deferred intentionally.

| Feature | Class | Basis |
|---|---|---|
| FastAPI startup, `/healthz`, `/readyz` (Postgres + Redis) | IV | Owner ran uvicorn on Windows: `/readyz` returned postgres ok, redis ok (earlier in this engagement) |
| PostgreSQL + migrations through `b7e1f0a2c3d4` | IV (tracked part) | Earlier real run; chain inspected here |
| Migration `c4d2a91e7f35` (turn_evaluations) | INV | Untracked; exercised by DB tests on owner's DB earlier, not re-run in this audit |
| Org API-key auth, hash-only storage | INV | Code and tests present (`test_auth.py`, `test_create_api_key.py`); covered by the owner's earlier full run, not re-run here |
| Multi-tenant isolation | INV | Composite FKs + tests present; not re-run |
| Consent-gated lead capture (text and voice tool) | INV | `voice/lead_tool.py`, `test_voice_lead_tool.py`, `test_leads.py`; **never exercised in a real spoken conversation** |
| RAG hybrid retrieval | INV | Unit tests and a diagnostic fixture exist; no baseline retrieval run recorded (README says so) |
| Live-turn evaluation idempotence | IV | Owner ran `evaluate_live_turns` three times on the real DB: `selected 0, written 0, pending_after 0` each time. **Weak evidence**: zero eligible turns, so idempotence under real writes was exercised only by tests |
| Voice timing instrumentation (Stage 3) | INV | Code + tests; no real call has produced recorded timings in this audit |
| Latency dashboard panels | INV | HTML logic inspected and one real bug fixed earlier; **never opened in a browser with real data** |
| Real mic -> STT -> LLM -> TTS -> speaker | M (unverified) | Never performed in this engagement |
| Golden/candidate evaluation datasets | P | 75 + 60 cases exist, none human-reviewed, no baseline recorded |
| Barge-in / interruption | INV | Claimed in README; not exercised |
| GitHub Actions CI | P | File exists; trigger is `main`/PR only; never observed running |
| LangGraph, MCP, embed widget, production deploy | D | Not implemented; intentionally later |

## 4. Test status and the count discrepancy

No test was executed in this audit. Numbers in circulation:

| Number | Origin | Status |
|---|---|---|
| 48 | `main` README (Stage 1) | Stale |
| 690 passed, 1 skipped | Local **uncommitted** README claim ("last full local run") | Unverified, cannot be reproduced from this audit |
| 703 passed, 1 skipped, 2 warnings (53.70 s) | Owner's native Windows run, pasted earlier | Real, but which files were present at that moment is not recorded |
| 773 passed, 1 skipped | Earlier Linux sandbox run of a copy | Different environment; not comparable |
| 721 | Static count of `def test_` in `tests/test_*.py` today | A static count only; parametrization and conditional skips change the collected total |

Facts that bear on the gap: there is no `pytest.ini`/`pyproject.toml`; `tests/test_live_eval_service.py` (22 defs) is module-skipped unless a test database URL is set; other tests skip when `langsmith` is absent. 8 files use `parametrize`. **The discrepancy cannot be resolved without `pytest --collect-only` on Windows.** Do not publish any single number yet.

## 5. Uncommitted work (in the working tree, not on GitHub)

Modified (12): `README.md`, `api/eval_routes.py`, `api/models.py`, `api/static/eval_dashboard.html`, `api/tracing.py`, `evals/aggregate.py`, `evals/registry.py`, `tests/test_eval_registry.py`, `voice/bot.py`, `voice/metrics_observer.py`, `voice/rag.py`, `voice/turn_recorder.py`.
Untracked (13): `.devcontainer/devcontainer-lock.json`, `api/live_eval.py`, `evals/latency_telemetry.py`, `evals/live.py`, `migrations/versions/c4d2a91e7f35_add_turn_evaluations.py`, `scripts/evaluate_live_turns.py`, and seven test files (`test_eval_counts`, `test_eval_live`, `test_latency_telemetry`, `test_live_eval_pure`, `test_live_eval_service`, `test_turn_evaluation_model`, `test_voice_timing`).

## 6. Risks

1. **Line-ending churn.** `git diff --stat` reports +1,776/-866, but ignoring end-of-line changes it is +1,013/-103. `README.md`, `api/models.py`, `voice/rag.py`, `voice/turn_recorder.py` and `voice/metrics_observer.py` show large EOL-only churn. Committing as-is would bury the real changes. No `core.autocrlf` is set and `.gitattributes` only covers the jsonl datasets.
2. **Public face is stale.** Default branch says 48 tests / no auth; the real work is on a feature branch plus uncommitted files.
3. **Unreproduced test number** (690 vs 703 vs 773) already written into a local README.
4. **Evaluation ground truth unreviewed**: any accuracy-style claim from `evals/` would be unsupported.
5. **No real-voice or real-dashboard evidence.**
6. **CI never observed**; trigger excludes the feature branch.
7. Local clutter (`bot.log`, `tts_test.wav`, three venvs, `.models/`) is git-ignored, so safe, but large.

## 7. Recommended execution order

1. Owner runs `pytest --collect-only -q` and the full suite on Windows (commands in the chat reply) -> one reproducible number and the discrepancy explained.
2. Decide the line-ending policy for the 5 churned files, then produce a clean, reviewable diff.
3. Verify the dashboard in a real browser against the owner's local API.
4. One real voice conversation including the consent flow (manual; steps provided separately).
5. Security/quality review pass (auth, tenant isolation, consent, secrets).
6. Rewrite README to match only verified facts; no CI badge, no production/deploy/voice-verified claims.
7. Prepare separated commits; show diff; **wait for explicit approval before any commit or push.**
