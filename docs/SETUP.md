# Setup Guide

> Voice Sales Agent — Local Development Setup

## Prerequisites

- Python 3.13+
- Node.js 22+ (for JS tests)
- PostgreSQL 16 with pgvector extension
- Redis (optional — set `REDIS_ENABLED=false` to skip)
- Git

## 1. Clone and Branch

```bash
git clone https://github.com/AshishChaubey2003/Voice-sales-agent.git
cd Voice-sales-agent
git checkout feature/voice-agent-evaluation-dashboard
```

## 2. Python Environment

```bash
python -m venv .venv313
# Windows:
.venv313\Scripts\activate
# Linux/macOS:
source .venv313/bin/activate

pip install -r requirements.txt -c constraints-ci.txt
```

The constraints file pins the voice stack (pipecat-ai 1.12.0, langgraph 1.2.24, mcp 2.3.0)
to tested versions.

## 3. Environment Variables

Copy the template and fill in your values:

```bash
cp .env.example .env
```

**Required variables:**
| Variable | Description |
|---|---|
| `POSTGRES_HOST` | Database host (default: `127.0.0.1`) |
| `POSTGRES_PORT` | Database port (default: `5432`) |
| `POSTGRES_USER` | Database user |
| `POSTGRES_PASSWORD` | Database password |
| `POSTGRES_DB` | Database name |
| `GROQ_API_KEY` | Groq API key for LLM/STT/TTS |
| `GROQ_MODEL` | Groq model name (e.g., `llama-3.3-70b-versatile`) |

**Optional variables:**
| Variable | Default | Description |
|---|---|---|
| `REDIS_ENABLED` | `true` | Set `false` to skip Redis |
| `TTS_PROVIDER` | `groq` | `groq` or `deepgram` |
| `VOICE_TOKEN_SECRET` | (none) | ≥32 chars for voice token signing |
| `CHAT_ORCHESTRATOR` | `langgraph` | `langgraph` or `legacy` |
| `LANGSMITH_TRACING` | `false` | Enable LangSmith tracing |
| `HANDOFF_EMAIL_ENABLED` | `false` | Enable SMTP notifications |

**Never commit `.env` to git.**

## 4. Database Setup

```bash
# Create the database (PostgreSQL must be running)
createdb voice_sales

# Enable pgvector
psql -d voice_sales -c "CREATE EXTENSION IF NOT EXISTS vector;"

# Run migrations
alembic upgrade head
```

For tests, create a separate database:
```bash
createdb vsa_test
psql -d vsa_test -c "CREATE EXTENSION IF NOT EXISTS vector;"
export TEST_DATABASE_URL="postgresql+asyncpg://user:pass@localhost:5432/vsa_test"
```

## 5. Ingest Knowledge Base

```bash
python scripts/ingest_knowledge.py
```

This processes `knowledge/nimbuscrm/*.md` into embeddings stored in pgvector.

## 6. Run the Application

```bash
uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
```

API: http://localhost:8000  
Docs: http://localhost:8000/docs  
Eval dashboard: http://localhost:8000/dashboard/evals  
Sales dashboard: http://localhost:8000/dashboard/sales

## 7. Run Tests

```bash
# Full suite
python -m pytest -q

# With test database
TEST_DATABASE_URL="postgresql+asyncpg://..." python -m pytest -q

# JavaScript tests
node --test tests/js/voice-client.test.mjs

# Specific test files
python -m pytest tests/test_mcp.py -q
python -m pytest tests/test_handoff.py -q
python -m pytest tests/test_chat_graph.py -q
```

## 8. Docker (Optional)

```bash
docker-compose up -d postgres redis
# Then run the app outside Docker, or add api/voice services
```

See `docs/DEPLOYMENT.md` for full containerized setup.

## 9. Voice Testing

Voice requires:
- `GROQ_API_KEY` with STT/TTS access
- `VOICE_TOKEN_SECRET` (≥32 chars)
- A microphone and speakers
- Chrome/Edge browser with WebRTC support

See `docs/VOICE_TESTING.md` for step-by-step instructions.
