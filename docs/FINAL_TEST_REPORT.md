# Final Test Report

> Voice Sales Agent — Test Suite Results
> Current run: 2026-11-25, after the MCP mount fixes (commit-candidate code)

## Current results

| Metric | Windows (.venv313) | Cloud (Linux container) |
|---|---|---|
| **Passed** | **847** | **844** |
| **Failed** | **0** | **0** |
| **Errors** | **0** | **0** |
| Skipped | 1 | 4 |
| JavaScript tests | 14 / 14 | 14 / 14 |
| `/mcp` endpoint checks | 15 / 15 | 15 / 15 |
| `pip check` | No broken requirements found | n/a |
| Python | 3.13 | 3.13 |
| PostgreSQL | 16 + pgvector (Docker, port 5433) | 16 + pgvector 0.8.0 |

The 3-test difference is entirely accounted for: three MCP knowledge-search tests skip in the
cloud container because the embedding model (`BAAI/bge-small-en-v1.5`) cannot be downloaded
through its network proxy. They run and pass on Windows, where the model is cached. Windows
1 skip + those 3 = the cloud's 4.

## Test breakdown by file

| Test file | Tests | Status |
|---|---|---|
| tests/test_auth.py | ~15 | All passed |
| tests/test_chat.py | ~30 | All passed |
| tests/test_chat_graph.py | 20 | All passed |
| tests/test_handoff.py | 19 | All passed |
| tests/test_mcp.py | 20 | 20 passed (Windows) / 17 passed + 3 skipped (cloud) |
| tests/test_leads.py | ~10 | All passed |
| tests/test_live_eval_service.py | ~100 | All passed |
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
| `test_langsmith_sync.py::…` | both | Opt-in: the SDK probes api.smith.langchain.com even with `upload_results=False` |
| `test_search_knowledge_valid_returns_chunks_key` | cloud only | Embedding model not downloadable through the container's proxy |
| `test_search_knowledge_top_k_capped` | cloud only | Same |
| `test_search_knowledge_tenant_isolation` | cloud only | Same |

No test is skipped to hide a failure; each skip is environmental and self-reported by the test.

## MCP endpoint verification

Run against a live `uvicorn api.main:app` over real HTTP (not the in-process transport), on
both platforms, by `.models/mcp_endpoint_check.py`:

| Group | Checks |
|---|---|
| initialize handshake | server info returned (`voice-sales-agent` 1.0.0) |
| tools/list | all three tools advertised, each with a description |
| authentication | valid key accepted; invalid refused; empty refused; `api_key` required in schema; omitting it rejected |
| tenant isolation | org B cannot write a lead into org A's conversation; org B has no leads |
| consent gate | missing consent rejected; consented lead saved; DB row present with recorded consent |

Known behaviour: a request to `/mcp` receives a 307 redirect to `/mcp/` before succeeding,
because the mounted app serves at its own root. Clients that use the trailing slash avoid the
extra round trip. Functionality is unaffected.

## Historical results (superseded)

Kept for reconciliation. Each was accurate for the code at that time.

| Date / source | Count | Why it differs |
|---|---|---|
| Windows, earlier on 2026-11-25 | 847 passed | Same count, but **before** the MCP mount and lifespan fixes — `api/main.py` did not mount `/mcp` and `requirements.txt` omitted `mcp` |
| Cloud, before MCP tests | 827 passed | MCP suite not yet written |
| Windows, mid-session | 825 passed, 22 errors | `TEST_DATABASE_URL` pointed at the wrong port with a placeholder password |
| Windows, mid-session | 758 passed, 1 failed, 88 errors | Postgres credentials not yet matching the container |
| Windows, mid-session | 755 passed, 4 failed, 88 errors | Three handoff tests built `Settings` without `redis_enabled=False`; since fixed |
| Earlier reports | 773 passed | Before the handoff and LangGraph suites |
| Earlier reports | 703 passed | Before the evaluation suites |
| Earlier reports | 690 passed | Baseline in the README at the start of this work |
| Static collection count | 721 | Collection only, excluded DB-dependent modules |

The count grew because tests were added — LangGraph (20), handoff (19), MCP (20) and the
live-evaluation suites. No test was removed, skipped or weakened to raise it.

## Commands to reproduce

Windows:
```powershell
$env:POSTGRES_HOST="127.0.0.1"; $env:POSTGRES_PORT="5433"
$env:POSTGRES_USER="voice_app"; $env:POSTGRES_DB="voice_sales"
$env:POSTGRES_PASSWORD="<your password>"
$env:REDIS_ENABLED="false"
$env:TEST_DATABASE_URL="postgresql+asyncpg://voice_app:<your password>@127.0.0.1:5433/vsa_test"

.\.venv313\Scripts\python.exe -m pip install -r requirements.txt -c constraints-ci.txt
.\.venv313\Scripts\python.exe -m pip check
.\.venv313\Scripts\python.exe -m pytest -q -p no:cacheprovider -rs
node --test tests/js/voice-client.test.mjs
```

MCP endpoint check (server running in another shell):
```powershell
$env:PYTHONPATH="D:\voice-sales-agent"
.\.venv313\Scripts\python.exe .models\mcp_endpoint_check.py http://127.0.0.1:8001/mcp
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
- Redis disabled for tests (`REDIS_ENABLED=false`); `/readyz` uses a fake double
- The LLM is mocked throughout — the suite makes no paid API calls
- Windows runs used shell environment variables rather than `.env`, because the local Docker
  container was created with `voice_app` / `voice_sales` while `.env` declares different values
