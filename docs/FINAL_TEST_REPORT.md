# Final Test Report

> Voice Sales Agent — Test Suite Results
> Current run: 2026-11-25, on the reconciled environment (commit f0fc4d0)

## Current results

| Metric | Windows (.venv313) | Cloud (Linux container) |
|---|---|---|
| **Passed** | **847** | **844** |
| **Failed** | **0** | **0** |
| **Errors** | **0** | **0** |
| Skipped | 1 | 4 |
| Total collected | 848 | 848 |
| JavaScript tests | 14 / 14 | 14 / 14 |
| `/mcp` endpoint checks | 15 / 15 | 15 / 15 |
| `pip check` | No broken requirements found | n/a |
| Python | 3.13 | 3.13 |
| PostgreSQL | 16.15 + pgvector 0.8.6 (Docker, port 5433) | 16.15 + pgvector 0.6.0 |

The Windows run was made in a shell with no `POSTGRES_*` or `REDIS_*` variables exported,
reading `.env` alone, apart from `TEST_DATABASE_URL` (see below). The 3-test difference
against cloud is entirely three MCP knowledge-search tests, which skip in the container
because the embedding model cannot be downloaded through its network proxy. They run and
pass on Windows, where the model is cached. Windows' 1 skip plus those 3 is the cloud's 4.

## TEST_DATABASE_URL must be an operating-system variable

`tests/test_live_eval_service.py` reads it directly:

```python
TEST_DB = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    ..., reason="set TEST_DATABASE_URL to a database whose name contains 'test'")
```

`.env` is parsed by pydantic-settings into the `Settings` object and never reaches
`os.environ`, so putting `TEST_DATABASE_URL` there has no effect on these 22 tests. The
guard is deliberate: they create and drop schemas, so they refuse to run unless pointed
explicitly at a database whose name contains `test`.

Both of these runs are correct, and describe the same 848 tests:

| Shell | Result |
|---|---|
| `TEST_DATABASE_URL` exported | **847 passed, 1 skipped** |
| not exported | 825 passed, 23 skipped |

The remaining skip in the first case is the LangSmith opt-in.

## Test breakdown by file

| Test file | Tests | Status |
|---|---|---|
| tests/test_auth.py | ~15 | All passed |
| tests/test_chat.py | ~30 | All passed |
| tests/test_chat_graph.py | 20 | All passed |
| tests/test_handoff.py | 19 | All passed |
| tests/test_mcp.py | 20 | 20 passed (Windows) / 17 passed + 3 skipped (cloud) |
| tests/test_leads.py | ~10 | All passed |
| tests/test_live_eval_service.py | 22 | All passed, with `TEST_DATABASE_URL` exported |
| tests/test_eval_*.py | ~80 | All passed |
| tests/test_grounding_*.py | ~50 | All passed |
| tests/test_voice_runner_contract.py | ~30 | All passed |
| tests/test_tracing.py | ~15 | All passed |
| tests/test_monitoring*.py | ~40 | All passed |
| tests/test_review_*.py | ~50 | All passed |
| tests/test_turn_evaluation_model.py | ~20 | All passed |
| tests/test_knowledge*.py | ~30 | All passed |
| Other test files | ~30+ | All passed |
| tests/js/voice-client.test.mjs | 14 | All passed |

## Skipped tests

| Test | Environment | Reason |
|---|---|---|
| `test_langsmith_sync.py` | both | Opt-in: the SDK probes api.smith.langchain.com even with `upload_results=False` |
| `test_search_knowledge_valid_returns_chunks_key` | cloud only | Embedding model not downloadable through the container's proxy |
| `test_search_knowledge_top_k_capped` | cloud only | Same |
| `test_search_knowledge_tenant_isolation` | cloud only | Same |

No test is skipped to hide a failure. Each skip is environmental and self-reported.

## Environment reconciliation (2026-11-25)

`.env` had drifted from both the running Docker container and the committed
`.env.example`:

| Source | user / database |
|---|---|
| `.env.example` (committed template) | `voice_app` / `voice_sales` |
| Docker container (baked into its volume) | `voice_app` / `voice_sales` |
| `.env` before reconciliation | `app` / `voice_agent` |

`docker compose` substitutes shell variables ahead of `.env`, and PostgreSQL writes the
resulting user and database into its volume on first initialization. A container created
while `POSTGRES_USER` was exported therefore cannot be reached using a later `.env`, and
fails with `role "..." does not exist`. The volume must be recreated, or `.env` corrected
to match it.

`.env` was corrected to `voice_app` / `voice_sales` and `REDIS_ENABLED=true` added. No
repository file needed changing: `docker-compose.yml` reads `.env`, so the two were never
in conflict with each other.

Before and after, same machine, no overrides exported:

| | before | after |
|---|---|---|
| passed | 758 | **825** (847 with `TEST_DATABASE_URL`) |
| failed | 1 | **0** |
| errors | 66 | **0** |

## MCP endpoint verification

Run against a live `uvicorn api.main:app` over real HTTP, on both platforms, by
`.models/mcp_endpoint_check.py`:

| Group | Checks |
|---|---|
| initialize handshake | server info returned (`voice-sales-agent` 1.0.0) |
| tools/list | all three tools advertised, each with a description |
| authentication | valid key accepted; invalid refused; empty refused; `api_key` required in schema; omitting it rejected |
| tenant isolation | org B cannot write a lead into org A's conversation; org B has no leads |
| consent gate | missing consent rejected; consented lead saved; database row present with recorded consent |

Known behaviour: a request to `/mcp` receives a 307 redirect to `/mcp/`, because the
mounted app serves at its own root. Clients follow it; the trailing-slash form avoids the
extra round trip and is the documented endpoint.

## Consent gate demonstration

`.models/stack_and_consent_check.py`, against the real database with throwaway data,
16/16 checks on both platforms:

| Scenario | Tool result | Lead rows |
|---|---|---|
| A — visitor says "Yes, that's right." | `{'status': 'saved'}` | 0 to 1 |
| B — visitor says "No, please don't." | `rejected`: "has not clearly agreed yet" | 0 to 0 |
| C — consent never asked | `rejected`: "Read the name and email back before saving" | 0 |
| Organization total across all three | | **1** |

Row A stored the name, email, and the exact `consent_prompt` and `consent_reply` as
evidence. No LLM is involved, so the demonstration costs nothing.

## Not verified

| Item | Status |
|---|---|
| **Redis health** | **UNVERIFIED** — no run has performed a successful ping. `.env` now sets `REDIS_ENABLED=true` and the container is running, but `stack_and_consent_check.py` has not been re-run since. |
| Real voice (microphone to speaker) | Not performed — needs audio hardware and Groq credentials |
| CI workflow execution | Never run — requires pushing the branch |
| Clean end-to-end `docker compose up` of all four services | Not performed |

## Historical results (superseded)

Each was accurate for the code and environment at that time.

| Date / source | Count | Why it differs |
|---|---|---|
| Windows, earlier on 2026-11-25 | 847 passed | Same count, but **before** the MCP mount and lifespan fixes |
| Windows, after the `.env` fix, before exporting `TEST_DATABASE_URL` | 825 passed, 23 skipped | The 22 live-evaluation tests skipped by their own guard |
| Windows, with the handoff fix applied, before the `.env` fix | 758 passed, 1 failed, 66 errors | `.env` still named a role the container did not have |
| Windows, before the handoff fix | 755 passed, 4 failed, 66 errors | Three handoff tests depended on `REDIS_ENABLED=false` being exported |
| Cloud, before the MCP tests | 827 passed | MCP suite not yet written |
| Earlier reports | 773 passed | Before the handoff and LangGraph suites |
| Earlier reports | 703 passed | Before the evaluation suites |
| Earlier reports | 690 passed | Baseline in the README at the start of this work |
| Static collection count | 721 | Collection only, excluding database-dependent modules |

The count grew because tests were added — LangGraph (20), handoff (19), MCP (20) and the
live-evaluation suites. No test was removed, skipped or weakened to raise it.

## Commands to reproduce

Windows, in a shell with no `POSTGRES_*` or `REDIS_*` exported:

```powershell
$env:TEST_DATABASE_URL = "postgresql+asyncpg://voice_app:<password>@127.0.0.1:5433/vsa_test"

.\.venv313\Scripts\python.exe -m pip install -r requirements.txt -c constraints-ci.txt
.\.venv313\Scripts\python.exe -m pip check
.\.venv313\Scripts\python.exe -m pytest -q -p no:cacheprovider -rs
node --test tests/js/voice-client.test.mjs
```

The test database is created once:

```powershell
docker exec voice-sales-agent-postgres-1 psql -U voice_app -d voice_sales -c "CREATE DATABASE vsa_test OWNER voice_app;"
docker exec voice-sales-agent-postgres-1 psql -U voice_app -d vsa_test -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

Endpoint and consent checks, with the server running in another shell:

```powershell
$env:PYTHONPATH = "D:\voice-sales-agent"
.\.venv313\Scripts\python.exe .models\mcp_endpoint_check.py http://127.0.0.1:8001/mcp
.\.venv313\Scripts\python.exe .models\stack_and_consent_check.py http://127.0.0.1:8001
```

Linux / CI:

```bash
GROQ_API_KEY=placeholder GROQ_MODEL=<model> TTS_PROVIDER=groq \
REDIS_ENABLED=false LANGSMITH_TRACING=false \
POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=5432 POSTGRES_USER=voice_app \
POSTGRES_PASSWORD=<password> POSTGRES_DB=voice_sales \
TEST_DATABASE_URL="postgresql+asyncpg://voice_app:<password>@127.0.0.1:5432/vsa_test" \
python -m pytest -q
```

## Environment notes

- Packages pinned through `constraints-ci.txt`: pipecat-ai 1.12.0, langgraph 1.2.24, mcp 2.3.0
- The LLM is mocked throughout — the suite makes no paid API calls
- Migration head: `d5e3b82a4c16`
- The live-evaluation tests create and drop a throwaway schema per run, so `vsa_test`
  is left clean
