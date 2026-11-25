# Project Completion Tracker

> Voice Sales Agent — Status as of 2026-11-25, commit f0fc4d0
> Windows, reconciled environment: **847 passed, 1 skipped, 0 failed, 0 errors**
> Cloud: **844 passed, 4 skipped, 0 failed** · `/mcp`: 15/15 both platforms
> Consent demo: 16/16 both platforms · JS: 14/14 · `pip check`: clean

| # | Phase | Status | Key files | Tests | Evidence | Blocker |
|---|---|---|---|---|---|---|
| 1 | Project audit | VERIFIED | PROJECT_AUDIT.md | n/a | Git state, feature classification, risk analysis | — |
| 2 | Backend completion | VERIFIED | api/*.py | 847 | Routes, auth, DI, error handling, health and readiness | — |
| 3 | Consent-based lead capture | VERIFIED | api/leads.py, docs/CONSENT_FLOW_VERIFICATION.md | 30+ | All six required scenarios mapped to code paths and named tests; demonstrated against the real database, 16/16 | — |
| 4 | Voice pipeline | TESTED (contract only) | voice/*.py | ~30 + 14 JS | Contract and client tests pass | **Real microphone-to-speaker test needs hardware and Groq credentials** |
| 5 | RAG completion | VERIFIED | api/knowledge.py, api/embeddings.py | passes on Windows | Hybrid search, distance gate, query rewrite; 3 tests skip in cloud only (no model download) | — |
| 6 | LangGraph orchestration | VERIFIED | api/chat_graph.py | 20 | 8-node StateGraph, conditional routing, `CHAT_ORCHESTRATOR=legacy` fallback | — |
| 7 | Human handoff | VERIFIED | api/handoff.py, api/notifications.py, api/static/sales_dashboard.html | 19 | State machine, atomic claim, partial unique index, SMTP notifier | Email delivery untested — **needs an SMTP account** |
| 8 | Evaluation and observability | VERIFIED | evals/*.py, api/live_eval.py, api/eval_routes.py | ~200 | 75 golden and 60 candidate cases, /dashboard/evals | Human review of cases not yet done |
| 9 | MCP integration | VERIFIED | api/mcp_server.py, api/main.py | 20 + 15 endpoint | Three tools over live HTTP; auth, tenant isolation and consent gate proven against a running server on both platforms | — |
| 10 | Website embed widget | VERIFIED | voice/static/demo.html, voice/static/voice-client.js | 14 JS | No API key in browser JS; voice-token auth; no innerHTML, no eval | — |
| 11 | Database and migrations | VERIFIED | migrations/versions/*.py | upgrade/downgrade/check | Applied to an empty volume during the reconciliation; `alembic check` clean; single linear head `d5e3b82a4c16` | — |
| 12 | Security review | VERIFIED | docs/SECURITY_REVIEW.md | n/a | 0 critical, 0 high, 1 medium (no rate limiting), 1 low (demo-widget CSP) | — |
| 13 | Complete testing | VERIFIED | tests/*.py | 847 / 844 | Both platforms, 0 failures, 0 errors; counts reconciled in docs/FINAL_TEST_REPORT.md | — |
| 14 | CI/CD | IMPLEMENTED | .github/workflows/ci.yml | YAML validated | Three jobs: quality, security, test; `feature/**` triggers | **Never executed — needs a push, which needs approval** |
| 15 | Docker and deployment | IMPLEMENTED | docker-compose.yml, Dockerfile, Dockerfile.api | n/a | postgres and redis started cleanly from the compose file and the volume rebuilt from scratch | **api and voice services never started; production deploy needs a provider and credentials** |
| 16 | Line endings | VERIFIED | .gitattributes | n/a | 8 CRLF files identified; each also carried real content changes, so normalization landed with that content | — |
| 17 | Documentation | VERIFIED | docs/ (12 files), README.md | n/a | Architecture, setup, API, consent, security, evaluation, voice testing, demo, test report | — |
| 18 | Demo script | IMPLEMENTED | docs/DEMO_SCRIPT.md | n/a | Five-act script written | **Not performed end to end; recording needs local hardware** |
| 19 | GitHub release | NOT STARTED | — | — | 9 local commits, nothing pushed | **Needs approval to push** |

## Summary

| Status | Count | Phases |
|---|---|---|
| VERIFIED | 14 | 1, 2, 3, 5, 6, 7, 8, 9, 10, 11, 12, 13, 16, 17 |
| TESTED (needs hardware) | 1 | 4 — voice pipeline |
| IMPLEMENTED (not yet exercised) | 3 | 14 CI run, 15 Docker/deploy, 18 demo performance |
| NOT STARTED | 1 | 19 — release |

Nothing above is marked complete on the strength of a plan or a code read. Every VERIFIED
row has a named test file or a document containing actual recorded output.

## Explicitly NOT verified

| Item | Status |
|---|---|
| **Redis health** | **UNVERIFIED.** No run has performed a successful ping. `.env` now sets `REDIS_ENABLED=true` and the container is running, but `stack_and_consent_check.py` reported `SKIP` on every run so far and has not been re-run since the `.env` correction. |
| Real voice conversation | Not performed |
| CI workflow run | Never executed |
| `docker compose up` of all four services | Not performed; only postgres and redis were started |
| Demo script performed end to end | Not performed |

## Defects found and fixed during verification

| Defect | How it surfaced | Fix |
|---|---|---|
| `/mcp` never mounted | Reviewing the real `git diff` before committing | Import, mount and `mcp` dependency added for real |
| Endpoint exposed at `/mcp/mcp` | Inspecting the mounted app's inner routes | `streamable_http_path="/"` so the single prefix comes from the mount |
| Every `/mcp` request returned 500 | Driving a live server with a real MCP client | Starlette does not run a mounted app's lifespan; the parent lifespan now enters `session_manager.run()` |
| Handoff notifier tests depended on `REDIS_ENABLED=false` being exported | A Windows run in a shell with nothing exported | `_settings()` now passes `redis_enabled=False` explicitly (commit f0fc4d0). The earlier "fix" was verified in a shell that had the variable set, so it proved nothing and never reached the repository |
| 9 live-eval tests failed | Cloud run | Hard-coded timestamp aged out of the 30-day window; replaced with a relative one, assertions untouched |
| `.env` named a role the Docker volume did not contain | A test run with no shell overrides | `.env` corrected to `voice_app` / `voice_sales`, matching both the container and `.env.example`. No repository file needed changing |

The first three would all have shipped broken: the test suite passed throughout, because
`tests/test_mcp.py` exercises the tools in-process and never goes through the HTTP mount.
The fourth is the same class of error in reverse — a test that passed only because of an
environment variable nobody had noticed was set.

## Environment note

`docker compose` substitutes shell variables ahead of `.env`, and PostgreSQL writes the
resulting user and database into its volume on first initialization. A container created
while `POSTGRES_*` variables were exported cannot be reached using a later `.env`. Start
the stack from a shell with none of them set, or the volume and `.env` will disagree.

`TEST_DATABASE_URL` is read from the operating-system environment, not from `.env`, so it
must be exported for the 22 live-evaluation tests to run. Without it they skip, by design.

## What still needs you

1. **Approval to push** — 9 local commits, nothing on the remote
2. **Real voice test** — Groq credentials plus a microphone and speakers (phase 4)
3. **CI run** — requires pushing the branch (phase 14)
4. **SMTP account** — to prove handoff email delivery (phase 7)
5. **Deployment provider and credentials** — for production (phase 15)
