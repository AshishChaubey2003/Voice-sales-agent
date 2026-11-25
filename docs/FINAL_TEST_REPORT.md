# Final Test Report

> Voice Sales Agent — Test Suite Results  
> Cloud verification: 2026-11-25

## Summary

| Metric | Cloud (Linux) | Windows | Notes |
|---|---|---|---|
| **Total passed** | 844 | ⏳ Pending | Includes all new tests |
| **Total skipped** | 4 | ⏳ Pending | 3 MCP (embedding model), 1 other |
| **Total failed** | 0 | ⏳ Pending | — |
| **Python version** | 3.13 | 3.13 | — |
| **PostgreSQL** | 16 + pgvector 0.8.0 | ⏳ Pending | — |
| **JS tests** | 14/14 | ⏳ Pending | `node --test tests/js/voice-client.test.mjs` |

## Test Breakdown by File

| Test file | Tests | Cloud Status |
|---|---|---|
| tests/test_auth.py | ~15 | ✅ All passed |
| tests/test_chat.py | ~30 | ✅ All passed |
| tests/test_chat_graph.py | 20 | ✅ All passed |
| tests/test_handoff.py | 19 | ✅ All passed |
| tests/test_mcp.py | 20 | ✅ 17 passed, 3 skipped |
| tests/test_leads.py | ~10 | ✅ All passed |
| tests/test_live_eval_service.py | ~100 | ✅ All passed |
| tests/test_eval_*.py | ~80 | ✅ All passed |
| tests/test_grounding_*.py | ~50 | ✅ All passed |
| tests/test_voice_runner_contract.py | ~30 | ✅ All passed |
| tests/test_tracing.py | ~15 | ✅ All passed |
| tests/test_monitoring*.py | ~40 | ✅ All passed |
| tests/test_review_*.py | ~50 | ✅ All passed |
| tests/test_turn_evaluation_model.py | ~20 | ✅ All passed |
| tests/test_knowledge*.py | ~30 | ✅ All passed |
| Other test files | ~30+ | ✅ All passed |
| tests/js/voice-client.test.mjs | 14 | ✅ All passed |

## Skipped Tests

| Test | Reason |
|---|---|
| `test_search_knowledge_valid_returns_chunks_key` | Embedding model not downloadable in cloud (network proxy) |
| `test_search_knowledge_top_k_capped` | Same — embedding model |
| `test_search_knowledge_tenant_isolation` | Same — embedding model |
| 1 other skip | Pre-existing (non-MCP) |

These 3 MCP skips are expected in cloud-only environments. On Windows with the cached embedding model at `.models/models--qdrant--bge-small-en-v1.5-onnx-q`, they will pass.

## Historical Count Reconciliation

| Source | Count | Explanation |
|---|---|---|
| Previous reports: 690 | Before handoff/graph tests added |
| Previous reports: 703 | After some additions |
| Previous reports: 773 | After evaluation tests |
| Static analysis count: 721 | Collection count without DB-dependent tests |
| Previous cloud run: 827 | Before MCP tests |
| **Current cloud run: 844** | Full suite with MCP, handoff, graph |

The count increased as new tests were added for LangGraph (20), handoff (19), and MCP (20).
No tests were removed or weakened.

## Commands to Reproduce

```bash
# Cloud / CI
GROQ_API_KEY=placeholder GROQ_MODEL=llama-3.3-70b-versatile TTS_PROVIDER=groq \
REDIS_ENABLED=false LANGSMITH_TRACING=false \
POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=5432 POSTGRES_USER=voice_app \
POSTGRES_PASSWORD=ci-test-password POSTGRES_DB=voice_sales \
TEST_DATABASE_URL="postgresql+asyncpg://voice_app:ci-test-password@127.0.0.1:5432/vsa_test" \
python -m pytest -q --tb=short

# Windows
.\.venv313\Scripts\python.exe -m pytest -q -p no:cacheprovider
```

## Environment

- **Cloud**: Linux container, Python 3.13, PostgreSQL 16, pgvector 0.8.0
- **Packages**: pipecat-ai==1.12.0, langgraph==1.2.24, mcp==2.3.0 (constraints-ci.txt)
- **Redis**: Disabled (REDIS_ENABLED=false)
- **LLM**: Mocked in tests (no real Groq calls)
