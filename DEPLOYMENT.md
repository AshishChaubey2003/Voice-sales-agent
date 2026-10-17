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

---

## 6. Deployment (Day 2 changes)

Section 5 lists the blockers the Day 1 audit found. This section records what
the Day 2 changes did about each, the platform decision, and the exact steps.
What is implemented here was unit-tested; what needs a live call to confirm is
marked so. Provider sizes and free-tier quotas are deliberately not quoted —
check the provider's pricing page when you deploy.

### 6.1 Status of the section 5 blockers

| Blocker | Status after Day 2 |
|---|---|
| 5.1 ICE servers | **Configurable, not auto-fixed.** The bot now reads `PIPECAT_ICE_SERVERS`, validates it, and logs at startup whether a remote visitor can connect (warns loudly when it is unset or STUN-only). You still have to supply a STUN URL and a TURN relay. |
| 5.2 Two processes, one port | **Decided:** two separate services. `Dockerfile` runs the voice bot (port 7860); `Dockerfile.api` runs the API (port 8001). A single-port host runs the voice bot; the API goes to a second service. |
| 5.3 Auth / abuse | **Partly closed.** `VOICE_MAX_CALL_SECONDS` and `VOICE_MAX_CONCURRENT_CALLS` are enforced in the bot; `PIPECAT_ALLOWED_ORIGINS` is documented. There is still no user authentication — see 6.6. |
| 5.4 Reproducible build | **Closed.** `requirements.lock.txt` is generated from the environment where the 67 tests passed, and both Dockerfiles install from it. |
| 5.5 Model downloaded at runtime | **Closed for Docker.** Both Dockerfiles bake the model into the image at build time (`BAKE_EMBEDDING_MODEL`), so a cold start does not re-download it. |
| 5.6 Memory | Unchanged; still measure the real peak on the target host. |

### 6.2 Verified facts behind the WebRTC decision

From the official Pipecat SmallWebRTC documentation (checked October 2026):

- With no ICE servers, the transport **gathers host candidates only** — which is
  why localhost works and any other network does not.
- For production it lists **STUN as required and TURN as recommended**. STUN
  alone is not guaranteed across symmetric NAT or mobile CGNAT.
- Browsers block microphone access on insecure origins, so the public URL
  **must be HTTPS**. Hugging Face Spaces and Render both terminate TLS for you.

`PIPECAT_ICE_SERVERS` accepts the same values the runner's `--ice-servers` flag
does: comma-separated bare URLs, or a JSON array of objects with `urls` and
optional `username`/`credential`:

```bash
# STUN only — direct connections work, strict NAT may fail
PIPECAT_ICE_SERVERS=stun:stun.l.google.com:19302

# STUN + TURN — what actually makes a public demo reliable
PIPECAT_ICE_SERVERS=[{"urls":"stun:stun.l.google.com:19302"},{"urls":"turn:turn.example.com:3478","username":"user","credential":"secret"}]
```

**Where the value actually goes (verified against the code):** the Pipecat
runner reads `PIPECAT_ICE_SERVERS` at server start, and in its `/api/offer`
handler builds `IceServer(**server)` for each entry and passes them to the
`SmallWebRTCConnection` it creates. That connection is handed to `bot()` as
`SmallWebRTCRunnerArguments.webrtc_connection`, which our `create_transport`
uses. So the ICE servers reach the transport through the runner — the read in
`voice/bot.py` is **only** an advisory startup log and does not itself wire the
transport. Because the config parses does **not** mean a real connection works;
only a live call (6.7) proves that.

The bot logs its ICE status on startup. The log reports schemes and counts only,
never usernames or credentials. If you see
`PIPECAT_ICE_SERVERS is not set: the bot will gather host candidates only`,
no remote visitor can connect yet.

**Production fail-fast:** when `APP_ENV=production`, the bot refuses to start if
`PIPECAT_ICE_SERVERS` has no STUN server (or is malformed) or if
`PIPECAT_ALLOWED_ORIGINS` is unset — so it cannot come up "healthy" and then
reject every visitor, or serve with wildcard CORS. In development (the default)
these are warnings, not errors, so local work is unaffected.

Transport is **not** changed to Daily. The SmallWebRTC + TURN route is a
configuration change and keeps the architecture as it is. If TURN proves hard
to obtain, Daily remains the alternative (audit report, section 7), added as a
second switchable transport rather than a replacement.

### 6.3 Platform choice

| Platform | Verdict |
|---|---|
| **Hugging Face Spaces (Docker)** | Suitable for the **voice bot**. It runs a Docker image on port 7860 over HTTPS. Storage is ephemeral, so the baked-in model matters; set secrets as Space secrets. |
| **Render** | Suitable for the **API** and a hosted Postgres. Its small instances are tight for the bot's memory; measure first. |
| **Neon** | The hosted Postgres (section 4). |

Recommended free-tier shape: voice bot on a Spaces Docker Space, API + Postgres
elsewhere (or skip the hosted API for a demo, since the bot is the public piece),
Neon for the database, a TURN provider for `PIPECAT_ICE_SERVERS`.

**Do not assume HTTP support means WebRTC audio works.** A host can serve the
signaling page over HTTPS and still fail the media path without a TURN relay.
Prove it with a real call from mobile data (6.7), not from the same network as
the server.

For a Hugging Face **Docker Space**, the Space's own README needs this front
matter (keep it in the Space repo, not necessarily in this GitHub README):

```yaml
---
title: Voice Sales Agent
sdk: docker
app_port: 7860
---
```

### 6.4 Build

```bash
# Voice bot (the public demo)
docker build -t voice-bot .

# Text chat API (separate service)
docker build -f Dockerfile.api -t voice-api .

# Skip baking the model (smaller image, slower first call):
docker build --build-arg BAKE_EMBEDDING_MODEL=false -t voice-bot .
```

Supported Python: **3.13** (the image base and the lock file) or 3.12. If you
build on 3.12, regenerate `requirements.lock.txt` on 3.12 first — one pinned
package (`audioop-lts`) is a 3.13-only backport.

### 6.5 Run order on the host

Migrations, seeding and ingestion are **explicit operational steps**, not run on
container boot, so a restart never silently re-ingests or touches data. Run them
once against the hosted database (from your machine or a one-off job), as in
section 4, then start the services:

```bash
# one-off, against the hosted DB
alembic upgrade head
python -m scripts.seed_demo
python -m scripts.ingest_knowledge

# then the long-running services (the container CMDs do exactly this)
python -m voice.bot -t webrtc --host 0.0.0.0 --port 7860
python -m uvicorn api.main:app --host 0.0.0.0 --port 8001
```

### 6.6 Protecting the demo

Set all of these before putting the URL on LinkedIn:

```bash
PIPECAT_ALLOWED_ORIGINS=https://your-space.hf.space   # not the default "*"
VOICE_MAX_CALL_SECONDS=300                             # caps the worst case per visitor
VOICE_MAX_CONCURRENT_CALLS=1                           # bounds how many pipelines run at once
```

How the guards behave: the bot runs every call through `run_with_call_guards`
(`voice/limits.py`), which `run_session` calls on the live path. It refuses a
call once `VOICE_MAX_CONCURRENT_CALLS` is reached — the refused call is
cancelled and never runs the pipeline — and a watchdog ends any call that passes
`VOICE_MAX_CALL_SECONDS`, releasing the slot in a `finally` so a crash cannot
leak it. This path is covered by regression tests with a fake runner
(`tests/test_limits.py`): they assert the slot is released on success, on
refusal, on a `run()` exception, and that the watchdog cancels a long call.
What the tests do **not** cover is the real Pipecat `WorkerRunner` and a live
audio call — do one test call that runs past the limit before trusting it in
public.

**Limitation — the guard is per process (important for #4):** the limiter is a
module-level counter in the runner process, so it bounds concurrent calls within
**one** process. It does **not** coordinate across multiple replicas or a
scaled-out deployment: two replicas with `VOICE_MAX_CONCURRENT_CALLS=1` allow
two simultaneous calls in total, one each. For a single free-tier Space (one
replica, one process) this is exactly right and is the recommended shape. If you
ever scale to more than one replica, move the counter to shared state (for
example a Redis `INCR`/`DECR` with a TTL) or put a one-at-a-time limit in front
of the service; until then, keep the voice bot at a single replica.

Still missing: there is no user authentication, and leads are read with
`python -m scripts.list_leads`, not over an authenticated HTTP endpoint. Keep
the leads data behind the script, and tell demo visitors not to enter real
personal details, because the consent gate stores a name and email.

### 6.7 Verifying a real deployment

1. **API container:** `curl https://<api-host>/readyz` → `200 {"status":"ready",…}`.
2. **Database:** `python -m scripts.search_knowledge "How much is the Pro plan?"`
   against the hosted DB returns the Pro plan chunk.
3. **Voice, the test localhost cannot do:** open the bot URL **on mobile data**
   (not the server's Wi-Fi) and complete a call. This is the only proof that
   STUN/TURN and the media path actually work.
4. **Re-measure latency** and quote the new number. The README's 1,751 ms median
   was laptop-local; a hosted call with a TURN relay in the path will be slower.

### 6.8 What could not be tested in this environment

| Check | Result |
|---|---|
| `python -m pytest -q` | **PASS** — 93 tests, PostgreSQL 16 + pgvector 0.6.0, Python 3.13 |
| Config validation, health/readiness endpoints | **PASS** |
| ICE parsing, ICE-in-logs has no credentials, production fail-fast gate | **PASS** (unit tests) |
| Call-guard wiring on the run path (limiter + watchdog) | **PASS** — regression tests with a fake runner (not the real `WorkerRunner`) |
| ICE → transport path (runner builds `SmallWebRTCConnection` with the servers) | **PASS by inspection** of the installed Pipecat 1.12 runner source |
| Bot container start command (`-t webrtc --host --port`) | **PASS** — flags parse via the runner's argument parser |
| `docker build` (both images) | **BLOCKED** — no Docker daemon in this environment. Build commands are in 6.4; run them on your machine. |
| Dockerfile non-root user + writable model cache | **NOT TESTED** — added, but not build-verified here (no daemon) |
| Live WebRTC call through STUN/TURN | **NOT TESTED** — no browser, microphone or public host here |
| Model baking at build time | **NOT TESTED** — the model host is not reachable from this environment |
| Neon connection | **NOT TESTED** — no Neon project; forbidden to create one |
| Cross-replica concurrency limit | **FAIL by design** — the limiter is per process; see 6.6. Keep one replica. |

### 6.9 Exact configuration and credentials per service

Set every secret through the provider's secret manager (Hugging Face Space
secrets, Render environment groups, Neon's dashboard) — never in the repo. The
account to open for each external credential is named.

**Voice bot — Hugging Face Docker Space (port 7860)**

| Variable | Value / source |
|---|---|
| `DATABASE_URL` | the Neon async URL (see below) |
| `DB_SSL` | `require` |
| `DB_PREPARED_STATEMENT_CACHE_SIZE` | `0` if using Neon's `-pooler` host |
| `REDIS_ENABLED` | `false` (the bot does not use Redis) |
| `GROQ_API_KEY` | console.groq.com → API Keys |
| `GROQ_MODEL` | `openai/gpt-oss-20b` |
| `TTS_PROVIDER` + `DEEPGRAM_API_KEY` | `deepgram` + console.deepgram.com key |
| `VOICE_AGENT_ID` | printed by `python -m scripts.seed_demo` against Neon |
| `PIPECAT_ICE_SERVERS` | STUN URL + TURN URL/username/credential (see TURN below) |
| `PIPECAT_ALLOWED_ORIGINS` | the Space's public origin, e.g. `https://<user>-<space>.hf.space` |
| `VOICE_MAX_CALL_SECONDS`, `VOICE_MAX_CONCURRENT_CALLS` | `300`, `1` |

**Text chat API — separate service (Render web service, port 8001)**

Same `DATABASE_URL` / `DB_SSL` / Groq / TTS variables as above, plus Redis:
set `REDIS_ENABLED=true` with `REDIS_PASSWORD`/`REDIS_HOST`/`REDIS_PORT` only if
you attach a Redis/Key Value instance, otherwise `REDIS_ENABLED=false`. The API
does not need the `PIPECAT_*` or `VOICE_*` variables. For a demo you can skip the
API entirely — the voice bot is the public piece.

**Neon (PostgreSQL + pgvector)**

- Account: neon.tech. Create a project; copy the connection string.
- Rewrite it to `postgresql+asyncpg://USER:PASSWORD@HOST/DB`, add `DB_SSL=require`
  (not `?sslmode=` — asyncpg rejects it). For the `-pooler` host also set
  `DB_PREPARED_STATEMENT_CACHE_SIZE=0`.
- Credentials needed: the Neon role's user and password (in `DATABASE_URL`).
- Run `alembic upgrade head`, `seed_demo`, `ingest_knowledge` once against it
  (section 4) before starting the services.

**TURN provider (the piece that makes a public call work)**

- A TURN relay is required for visitors behind strict NAT / mobile CGNAT; STUN
  alone is not enough (6.2). Options: a managed TURN service (for example
  Cloudflare's TURN, Twilio's Network Traversal Service, Metered) or self-hosted
  coturn on a small VM.
- Credentials needed: the TURN server URL, a username, and a credential. Put
  them in `PIPECAT_ICE_SERVERS` as a JSON array alongside a STUN URL (6.2).
- Google's public STUN (`stun:stun.l.google.com:19302`) is free for the STUN
  part; there is no free public TURN you should rely on for a real demo.
