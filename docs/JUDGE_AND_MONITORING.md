# LLM judge, real-chat trace check and online monitoring

Status key: **tested offline** = unit tests with fakes; **live-verified** = actually run against the real
service and read back. Nothing below is live-verified unless it says so.

## Gemini judge

* Code: `evals/gemini.py` (REST client, `x-goog-api-key` header only, retries on 429/5xx, errors carry
  a status code only) and `evals/judges.py` (8 versioned evaluators, strict pydantic output).
  Status: **implemented, tested offline. Not configured or live-tested.**
* A Gemini *subscription* does not include API quota. You need an API key with billing from Google AI
  Studio / Google Cloud. Put it in `.env` (never in chat or code):
  `GEMINI_API_KEY`, `GEMINI_JUDGE_MODEL` (a model id your key can use; there is deliberately no default).
* Use it in a run: `python -m evals.run --mode full --allow-llm --judge --judge-provider gemini ...`
  (adds faithfulness / relevance / correctness through the existing judge, recorded with the model label).
* The per-criterion judges are wired into `evals.run` through `--judge-suite` (see the next section).
* Rules enforced in code: a judge whose required input is missing (for example a reviewed reference for
  `correctness`) is not called and not scored; the model may answer `insufficient_evidence`; invalid output or a
  provider failure is recorded as an error, never as a score; emails, phones and keys are masked before a prompt
  leaves the process. The pass threshold 0.7 is provisional.
* Deterministic, no model: `pii_leak`, `tool_selection`, `conciseness_check`.

## Evaluator suite in `evals.run` (`evals/judge_suite.py`)

* **Always on, free, deterministic** (any run with a reply): `pii_leak` (a value the visitor typed may be read back),
  `tool_selection` (`save_lead` invoked exactly when the case expects it), `conciseness` (provisional 60-word limit).
  They are stored beside a case's result and never change its pass/fail.
* **Opt-in, paid** `--judge-suite` (needs `--allow-llm`; `--judge-provider gemini|groq`): faithfulness, hallucination,
  relevance, tone_language on every answered case; `safety` on `prompt_injection` cases; `correctness` only for cases that are `reviewed` **and** whose required facts a named human reviewed
  (`facts_review_status: reviewed`; a passed case review alone is not enough, and none exists today); `task_completion` is recorded as
  skipped because no case states the visitor's goal.
* Missing input is `skipped_missing_input` and never reaches the model. In `--mode replay` retrieved text is not stored,
  so the context judges are skipped too. Provider or validation failures are `error` (type or short code only, no
  message text). `insufficient_evidence` has no score. None of these enter a mean; the dashboard counts each separately.
* Cost preview: `python -m evals.run --dry-run --mode full --allow-llm --judge-suite --judge-provider gemini --case-id <id>`
  prints the call upper bound; `--max-llm-calls` is a hard cap per provider client.
* Token totals come only from what the provider reports and are shown per client; **cost is never computed** (no pricing is
  configured). Run-level only: per-case tokens are not stored.
* LangSmith: with `--langsmith`, every scored evaluator becomes feedback `eval_<name>` on that example's experiment run
  (comment = `<name> v<version> [<model>]: <reason>`); an errored evaluator becomes `eval_<name>_error`; insufficient evidence
  becomes a categorical value. After upload the runner reads the feedback back per example and the run's `langsmith.feedback`
  block reports matched examples and any missing keys. **Offline-tested with fakes; not yet run against the real workspace.**

## Real chat test (paid; run only after approving the cost)

    python -m evals.run --mode full --allow-llm --org nimbus-crm-demo --case-id fact-pro-price --case-id lead-confirm-saves --max-llm-calls 6
    python -m evals.langsmith_probe --verify-chat-run <RUN_ID> --confirm

* Calls Groq only (the model in `GROQ_MODEL`), at most 3 expected and 4 worst case, hard-capped at 6. No judge.
* Visitors are the synthetic golden-dataset cases; the organization is the seeded fictional `nimbus-crm-demo`.
* Writes to Postgres: one conversation per case, their messages, and one synthetic lead row for the lead case.
  `ApiTarget` deletes the conversations it created (messages and leads cascade) when the run ends.
  Lead capture only writes that row: it sends no email or webhook.
* Real traces go to the LangSmith project. `--verify-chat-run` reads them back by conversation id and checks:
  `chat_turn` parent exists, `retrieval` / `llm_call` (and `tool_save_lead` if a lead was saved) nest under it,
  token usage and timing recorded, no error, and none of the synthetic text appears in any trace.

**Live-verified** (run `20261026T222057Z-86ee1f09`, read back with `--verify-chat-run`): both traces had the `chat_turn`
parent with `retrieval` and `llm_call` children nested (and `tool_save_lead` for the lead case), token usage recorded, no
error, and none of the synthetic text found. This used the Groq model only; the Gemini judge was not called.

## Online monitoring (`api/monitoring.py`)

* Off by default: `ONLINE_MONITORING=true` enables it. **Not deployed, so not active anywhere.**
* Chat turns submit counts, timings and statuses only (no text). A background worker (empty context, tracing
  disabled) keeps a rolling window of 200 turns, ignores duplicate turn ids, never blocks the request, and counts
  dropped events and alert-sink failures.
* Default rules (provisional): error rate >= 5% (20+ turns), p95 model latency >= 8000 ms (20+ latency samples),
  a secret or database URL in a reply (first occurrence). Alerts have a 15-minute cooldown per rule.
* Sinks: log (default) and `ALERT_FILE` are **simulated**: nothing is delivered. `ALERT_WEBHOOK_URL` posts JSON and
  marks an alert `delivered` only after an HTTP 2xx. Check `"simulated"` / `"delivered"` in each alert.
* Judge scoring of live traffic is an extension point (`judge_hook`) and is NOT wired.
* `GET /v1/evals/monitoring` and the dashboard card show only the caller's own organization; state is per process.

## Online evaluators in LangSmith: not configured (manual, on purpose)

The SDK in this project (langsmith 0.14.12) has no API for online-evaluator rules, and the only programmatic route I found is a
REST endpoint documented in a support article, not in the SDK. I did not call it: it would change your LangSmith workspace, and
with `LANGSMITH_TRACE_CONTENT` off the traces hold no message text for a judge to read. If you want one later (per the LangSmith docs):
Tracing, select the project, Evaluators tab, `+ Evaluator`, create an LLM-as-a-Judge evaluator, optionally filter runs
(for example `name = chat_turn`), set a sampling rate, then Apply. The judge model's provider key is set in the workspace settings.
Check your plan's evaluator and spend limits there. Reviewing content tracing is a separate decision.

## LangSmith UI checks still needed (navigation not verified by me)

1. Open smith.langchain.com, then Tracing, then the project `voice-sales-agent-evals`.
2. Open a `chat_turn` row. In the run tree, `retrieval` and `llm_call` should be indented beneath it.
3. Select `chat_turn`: Input should show `message_chars` and no message text; metadata shows `session_id`.
4. Open the Threads tab (if shown for your plan) and look for the conversation's `session_id`.


## Retrieval K and new deterministic scores

* `python -m evals.run --mode retrieval --ks 1,3,5` computes Recall@K and Precision@K for those K. Default is 1,3,4. The chosen list is recorded in the run (`retrieval_ks`). A K above `RAG_TOP_K` cannot exceed the value at `RAG_TOP_K`; the CLI prints a note.
* Relevance is binary (expected heading or not). NDCG is not computed because no case has graded labels.
* `false_lead_save` and `missed_lead_save` are separate deterministic scores in `--mode full` lead cases.

## Review progress

`python -m evals.review_status [--text] [--require-ready] [--dataset PATH] [--review-dir DIR]` is read-only. It reports each dataset's review state (`not_started`, `in_progress`, `unresolved`, `awaiting_dataset_update`, `evidence_inconsistent`, `identity_unverified`, `no_eligible_cases`, `ready`), per-case counts, eligible metric cases and confirmed exclusions, and whether `ready_for_validated_metrics` holds. Every run of `python -m evals.run` stores the same calculation in its dataset information, so its report, console note and the dashboard say *provisional* unless the dataset was `ready` at run time (the banner is a snapshot and may be older than the current review). Excluded cases are skipped by the runner and are never metric examples. See `EVAL_REVIEW_GUIDE.md`.

Before and during review, `python -m evals.review_preflight` (read-only) checks that the blank templates are intact, that each reviewer sheet is well formed (columns, delimiter, rows, ids, completeness), that two files by one person do not count as two reviewers, and that disagreements have complete resolutions. A clean preflight means the files are well formed, not that the dataset is ready. Even a `ready` dataset leaves metrics that depend on unreviewed `required_facts` (answer facts, `run.pass_rate`, correctness) and every LLM-judge score `provisional`; the registry says why.

## Manual LangSmith UI steps still open (nothing here is active)

1. Projects > your tracing project > confirm a recent `chat_turn` shows nested `retrieval`, `llm_call`, `tool_save_lead` and that inputs contain `message_chars` only.
2. Datasets & Experiments > `vsa-publish-check-20261027` > open experiment `voice-agent-eval-da3c1510` and confirm each run has one `recall_at_K`/`mrr` feedback value per example.
3. Only after reviewed data exists and content tracing is privacy-approved: Project > Rules/Online evaluation > add an evaluator on the `chat_turn` runs with a sampling rate, then Alerts > add a threshold. Record the rule names here once visible in the UI. Until then monitoring is **off**.

## Setting the Gemini judge model safely

1. Look up a model id your API key can use in Google AI Studio. A Gemini subscription does not by itself give API quota.
2. Put it on its own uncommented line in `.env`, with no quotes, spaces or angle brackets: `GEMINI_JUDGE_MODEL=your-real-id`. (Or, for one PowerShell window only: `$env:GEMINI_JUDGE_MODEL = "your-real-id"`; remove it afterwards with `Remove-Item Env:GEMINI_JUDGE_MODEL`, because an environment variable overrides `.env`.)
3. Placeholders and malformed ids are refused before any call. Existence and permission are only checked by the API.

## Counting judge errors

`counts.judge_errors` = cases with at least one failed legacy-judge or `--judge-suite` call; `counts.evaluator_errors` = failed evaluator calls. Skips are not errors; errors are never averaged into scores.

## Reading the dry run

`Mode X: N case(s) will run = A answer/lead + G free consent-gate` then `Skipped: S (reason counts)`. Only A cases can cost model calls; the estimate line is an upper bound.

## Judge timeout

`GEMINI_JUDGE_TIMEOUT_SECONDS` (optional, default 60, must be above 0 and at most 600) applies to Gemini judge calls only and does not change `LLM_TIMEOUT_SECONDS` or the voice agent. A read timeout is not retried; connection errors and 429/5xx are retried up to 2 times. A timed-out evaluator is recorded as an error (never a score) and logged with its name and a short code.

## Judge call latency

Each evaluator result carries `latency_ms`: the provider-measured duration of that single model call, or `null` (shown as `N/A`) when none was measured (deterministic checks, skipped evaluators, timeouts and other failures before a reply). It is never estimated and is not the experiment duration. The run summary lists every judge call with its score or error code and latency. Runs saved before this field existed show `N/A`.

## Review workflow

See `EVAL_REVIEW_GUIDE.md` (quick start, golden sheet, readiness). Judge scores stay provisional until a reviewed sample exists to compare them against.
