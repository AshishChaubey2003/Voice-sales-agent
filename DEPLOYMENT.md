# Deployment and configuration guide

This document covers local setup, how to verify it, what a hosted Postgres
migration needs, and what still blocks a public deployment.

Provider free-tier allowances and machine sizes are **not** quoted here on
purpose: they change often. Check the provider's own pricing page before you
rely on one.

---

## 1. Configuration

Every setting is read from the environment or from `.env`, through
`api/config.py`. `.env.example` lists every variable by name with placeholder
values. Copy it and fill in real values:

```bash
cp .env.example .env
```

Configuration is validated when the application starts. A missing or invalid
setting fails immediately with the **field names** that are wrong and the
reason — never the values, so the message is safe to paste into an issue or a
log:

```
Invalid configuration. Fix these in .env or the environment:
  - configuration: Value error, set DATABASE_URL, or all of POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB
```

### Database settings

Pick **one** of the two forms.

| Form | Variables | Use for |
|---|---|---|
| Parts | `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `POSTGRES_HOST`, `POSTGRES_PORT` | the local Docker Compose database |
| Full URL | `DATABASE_URL` | hosted Postgres; it overrides the parts above |

`DATABASE_URL` must use the async driver and must not carry `sslmode`:

```
# correct
DATABASE_URL=postgresql+asyncpg://user:password@host.example.com/dbname
DB_SSL=require

# rejected at startup, with the reason
DATABASE_URL=postgresql://user:password@host/db              # blocking driver
DATABASE_URL=postgresql+asyncpg://u:p@host/db?sslmode=require # asyncpg has no sslmode
```

**Why:** `asyncpg.connect()` accepts `ssl` and has no `sslmode` parameter.
A libpq-style `?sslmode=require` is passed straight through by SQLAlchemy's
asyncpg dialect and fails at connect time. Verified against SQLAlchemy 2.1.5
and asyncpg 0.32.0.

Pool settings: `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_RECYCLE_SECONDS`.
Keep them small on a serverless database — the API process and the voice bot
process each build their own engine, so the real connection count is roughly
double what one of these numbers suggests.

`DB_PREPARED_STATEMENT_CACHE_SIZE=0` is required **only** when connecting
through a transaction pooler (a connection-pooling host), which cannot hold
prepared statements between statements.

### Redis

Redis backs nothing but the `/readyz` check today. To run without it:

```
REDIS_ENABLED=false
```

`/readyz` then reports on Postgres alone and returns `200`, instead of
reporting `not_ready` forever because an unused dependency is absent.

### Text to speech

`TTS_PROVIDER` must be `groq` or `deepgram`. `deepgram` requires
`DEEPGRAM_API_KEY`. Both are checked at startup rather than mid-call.

---

## 2. Local setup

```bash
python -m venv .venv
# Windows:       .venv\Scripts\activate
# macOS / Linux: source .venv/bin/activate
pip install -r requirements.txt

docker compose up -d
docker compose ps                    # wait for healthy
alembic upgrade head
python -m scripts.seed_demo          # copy the agent_id into VOICE_AGENT_ID
python -m scripts.ingest_knowledge   # downloads the embedding model on first run
```

---

## 3. Verification

Run these in order. Each one has an expected result.

```bash
# 1. Tests. Use "python -m pytest", not bare "pytest": the repo has no
#    conftest.py, so only the module form puts the project root on sys.path.
python -m pytest -q
#    expected: 67 passed

# 2. Configuration loads
python -c "from api.config import get_settings; print(get_settings().tts_provider)"

# 3. Migrations are applied
alembic current                      # expected: the latest revision

# 4. API starts and answers
python -m uvicorn api.main:app --host 127.0.0.1 --port 8001
curl -sS -i http://127.0.0.1:8001/healthz   # expected: 200 {"status":"ok"}
curl -sS -i http://127.0.0.1:8001/readyz    # expected: 200 {"status":"ready", ...}

# 5. Retrieval works against the database
python -m scripts.search_knowledge "How much is the Pro plan?"

# 6. Voice bot
python -m voice.bot -t webrtc        # then open http://localhost:7860
```

If `/readyz` returns `503`, the body names the dependency that failed
(`postgres` or `redis`) and the full error is in the application log. The
response deliberately carries no error text.

---

## 4. Hosted Postgres (Neon) migration plan

Neon is suitable for this schema: it runs PostgreSQL with pgvector available
as an extension, and the migrations create the extension themselves. The
migrations were verified end to end on PostgreSQL 16 with pgvector 0.6.0.

**Changing `DATABASE_URL` is not the whole migration.** Steps:

1. **Create the project and database.** Note the connection string. Neon
   offers a direct host and a pooled host; prefer the direct host for
   migrations and ingestion.
2. **Rewrite the connection string** into the form this app accepts:
   `postgresql+asyncpg://…`, with `DB_SSL=require` and no `sslmode`.
   TLS is mandatory on Neon; without `DB_SSL` the connection is plaintext and
   will be refused.
3. **Confirm pgvector is available** before migrating:
   ```sql
   SELECT default_version FROM pg_available_extensions WHERE name = 'vector';
   ```
   The `a33f0d420da5` migration runs `CREATE EXTENSION IF NOT EXISTS vector`
   itself, so the database role must be allowed to create extensions.
4. **Run the migrations** from your machine, against the remote database:
   ```bash
   alembic upgrade head
   ```
   This creates the HNSW vector index and the GIN full-text index. The HNSW
   index needs pgvector 0.5.0 or newer — check step 3's version output.
5. **Seed and ingest** against the remote database:
   ```bash
   python -m scripts.seed_demo
   python -m scripts.ingest_knowledge
   ```
   Embeddings are computed locally, so this needs no API quota, but it does
   download the embedding model the first time.
6. **Verify, don't assume:**
   ```bash
   python -m scripts.search_knowledge "How much is the Pro plan?"
   ```
   A correct result proves TLS, the vector extension, the vector index, the
   full-text index and the generated `search_vector` column all work remotely.
   Also check the row counts:
   ```sql
   SELECT count(*) FROM knowledge_chunks;
   SELECT count(*) FROM knowledge_documents;
   ```
7. **If using the pooled host** for the running application, also set
   `DB_PREPARED_STATEMENT_CACHE_SIZE=0`, and keep `DB_POOL_SIZE` small.

### Rollback

Keep the local Docker database intact until the remote one is verified.
Rollback is then: point `DATABASE_URL` back at the local database (or unset
it to fall back to the `POSTGRES_*` parts) and restart. No data has to move
back, because the demo data is reproducible from the repository with
`seed_demo` plus `ingest_knowledge`.

Schema rollback, if a migration must be undone:

```bash
alembic downgrade -1
```

Every migration in this repository has a working `downgrade()`.

### Backup

The demo data is reproducible from the repository, so the only data worth
backing up is captured **leads** and **transcripts**:

```bash
pg_dump --no-owner --table=leads --table=messages --table=conversations \
        "postgresql://…" > backup.sql
```

Take this before any migration that touches those tables.

---

## 5. What still blocks a public deployment

These are the real blockers, in order. None of them is fixed by this document.

### 5.1 WebRTC has no ICE servers — the hard blocker

The bot currently gathers **host candidates only**. On localhost the browser
and the bot are on the same machine, so that works and hides the problem. Any
other network needs STUN to discover a public address and, behind symmetric
NAT or mobile CGNAT, a **TURN relay** to carry the audio.

The Pipecat runner reads ICE servers from the environment, so this needs no
code change:

```bash
PIPECAT_ICE_SERVERS=stun:stun.l.google.com:19302
# with a TURN relay, which is what actually makes it work:
PIPECAT_ICE_SERVERS=[{"urls":"stun:stun.l.google.com:19302"},{"urls":"turn:turn.example.com:3478","username":"user","credential":"secret"}]
```

STUN alone is not enough. You need a TURN server, either from a provider or
self-hosted (coturn).

**The alternative** is a hosted transport such as Daily, which performs NAT
traversal for you, so the bot only makes outbound connections. Pipecat's
runner already supports it; see section 7 of the audit report for the
trade-off. Either path closes this blocker — the ICE route is a configuration
change, the Daily route is a small code change plus a dependency on a service.

### 5.2 Two processes, one port

`api.main` (the text chat API) and `voice.bot` (the Pipecat runner, which
serves its own HTTP on port 7860) are separate processes on separate ports.
Hosts that expose a single port can run one of them, not both. Decide which
one the public URL serves, or merge them, before deploying.

### 5.3 Nothing is authenticated

The runner's offer endpoint and its `/start` endpoint have no authentication,
and its CORS policy allows every origin by default. A public URL therefore
lets anyone start a voice session and spend your Groq, Deepgram and TURN
allowance. At minimum set:

```bash
PIPECAT_ALLOWED_ORIGINS=https://your-demo-host.example.com
```

and add a call-duration cap and a concurrent-call limit before publishing a
link. There is also no authentication on the leads data; leads are read with
`python -m scripts.list_leads`, not over HTTP.

### 5.4 The build is not reproducible

`requirements.txt` uses `>=` ranges. A fresh install today resolves to newer
Pipecat, SQLAlchemy and pytest majors than a setup installed a month ago.
The test suite passes on both, but a deployment should pin:

```bash
pip freeze > requirements.lock.txt
```

Generate the lock file from the environment you actually tested, and install
from it in the deployment image.

### 5.5 The embedding model is downloaded at runtime

`BAAI/bge-small-en-v1.5` is fetched on first use into `EMBEDDING_CACHE_DIR`,
which is in `.gitignore`. The first boot of a fresh container therefore needs
network access to the model host and takes noticeably longer. On a host with
an ephemeral disk this cost is paid on **every** cold start. Baking the model
into the image avoids it.

### 5.6 Memory

Measured on Linux with Python 3.13, as resident memory after import:

| Stage | RSS |
|---|---|
| Bare interpreter | 9 MB |
| `+ api.main` (FastAPI, SQLAlchemy, Groq client) | 127 MB |
| `+ voice.bot` (Pipecat and services) | 166 MB |
| `+ Silero VAD loaded` | 191 MB |

The embedding model is **not** included — it could not be downloaded in the
measurement environment. Budget for it on top, and measure the real peak
during a live call on the target host before choosing a machine size.
