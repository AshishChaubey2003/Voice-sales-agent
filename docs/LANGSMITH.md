# LangSmith (optional)

LangSmith is optional and off by default. Nothing in the app or the local
evaluation runner needs it. **No LangSmith run has been made from this
repository: the integration was implemented and unit-tested with mocks, and the
SDK's evaluator interface was checked offline, but it has not been exercised
against a live LangSmith workspace.** Treat the first real run as the real test.

The project does not use LangChain or LangGraph; it uses the `langsmith` SDK
directly (checked against langsmith 0.14.12).

## Variables

Set in `.env` (never committed; `.env.example` has placeholders):

| Variable | Meaning |
|---|---|
| `LANGSMITH_TRACING` | `true` enables tracing of the app (retrieval, model call, tool call). Default off |
| `LANGSMITH_API_KEY` | Your key. Required for tracing and for `--langsmith`. Held as a secret value; never printed or written to results |
| `LANGSMITH_PROJECT` | Trace project name. Default `voice-sales-agent` |
| `LANGSMITH_ENDPOINT` | Only for EU/self-hosted regions |
| `LANGSMITH_TRACE_CONTENT` | `true` also sends visitor text, prompts and model text. Default off |

Tracing turns on only when **both** `LANGSMITH_TRACING=true` and a key are set and
the package is installed; otherwise every function runs unchanged.

## Setup

```powershell
.\.venv313\Scripts\python.exe -m pip install -r requirements-eval.txt
# Edit .env yourself (do not paste the key anywhere else):
#   LANGSMITH_TRACING=true
#   LANGSMITH_API_KEY=<your key>
#   LANGSMITH_PROJECT=voice-sales-agent
```

## What is traced

| Boundary | Name | Sent by default |
|---|---|---|
| Retrieval | `retrieval` (retriever) | query length, top_k, max distance, returned chunk **headings**, ms |
| Model call | `llm_call` (llm) | message count, prompt length, tool names, latency, reply length, tool names called |
| Tool call | `tool_save_lead` (tool) | only the resulting status. Lead name/email are never sent, even with content tracing |

Not traced: the HTTP request itself, and the Pipecat voice pipeline's own
STT/LLM/TTS services (voice latency is stored in the database instead). If a
redaction hook fails, the SDK drops the payload rather than sending it raw.

Tracing sends data to a third party. Leave `LANGSMITH_TRACE_CONTENT` off for real
visitor traffic unless that is acceptable under your privacy terms.

## Verify that LangSmith actually accepts traces

An environment variable or a log line is not proof. This submits one tiny non-LLM run (no prompt, no visitor text) and reads it back:

```powershell
.\.venv313\Scripts\python.exe -m evals.langsmith_probe             # plan only, no network call
.\.venv313\Scripts\python.exe -m evals.langsmith_probe --confirm   # one run to your workspace, then read-back
.\.venv313\Scripts\python.exe -m evals.langsmith_probe --confirm --project voice-sales-agent-evals
```

It prints the project and run id, and reports success only if LangSmith returns that same run. Until you have seen `verified by read-back: True`, treat the integration as unverified.

To keep evaluation traces apart from application traces, set the project for that shell only: `$env:LANGSMITH_PROJECT = "voice-sales-agent-evals"` before running an evaluation with tracing enabled.

## Dataset and experiment

```powershell
# Runs locally, then publishes: creates/updates the LangSmith dataset and uploads
# the local run as an experiment. No model is called a second time.
.\.venv313\Scripts\python.exe -m evals.run --mode retrieval --org nimbus-crm-demo --langsmith
```

* Dataset name defaults to the dataset file name (`nimbuscrm_golden_v1`); override with `--langsmith-dataset`.
* Example ids are derived from the case ids, so re-running updates instead of duplicating.
* The experiment contains the outputs and scores the local run already produced; the local report is the source of truth.
* The experiment URL, when returned, is stored in `run.json` and shown on the dashboard.
* If the key or package is missing, or LangSmith fails, the run still completes and records `langsmith.status` (`skipped: ...` or `failed: <ErrorType>`).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| No traces appear | Both `LANGSMITH_TRACING=true` and the key must be in `.env`; restart the API. Check the log line "LangSmith tracing enabled" |
| "the 'langsmith' package is not installed" | `pip install -r requirements-eval.txt` into the same venv |
| `skipped: LANGSMITH_API_KEY is not set` | Key missing from `.env` |
| `failed: LangSmithAuthError` / connection errors | Wrong key, wrong region (`LANGSMITH_ENDPOINT`), or no network |
| Traces show counts but no text | Intended; set `LANGSMITH_TRACE_CONTENT=true` only if acceptable |
| Offline SDK check | `$env:EVAL_TEST_LANGSMITH_SDK=1; python -m pytest tests/test_langsmith_sync.py` (the SDK probes langsmith.com even with uploads off) |
