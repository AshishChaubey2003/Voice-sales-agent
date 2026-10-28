# Evaluation plan

Status of the evaluation system for the voice sales agent, what each metric
means, and what is still missing. Written from an audit of the code; nothing here
is a claim about measured quality, because **no evaluation run against the real
system has been recorded in this repository yet**.

## 1. What existed before this work

| Area | Where | What it does | Limits |
|---|---|---|---|
| Unit/integration tests | `tests/` | Check code paths with fakes (chat route, consent gate, fusion, grounding prompt, limits, auth) | Test the code, not answer quality. The LLM and retriever are stubbed |
| Retrieval inspection | `scripts/search_knowledge.py` | Prints retrieved chunks and distances for one query | Manual; no ground truth, no metrics |
| Voice latency telemetry | `voice/turn_recorder.py`, `voice/metrics_observer.py` | Stores STT/LLM/TTS TTFB, voice-to-voice, first-speech, RAG ms, `interrupted` in `messages.latency_ms` | Real calls only; no scoring |
| Latency report | `scripts/latency_report.py` | p50/p95 per config from that telemetry | CLI only, voice only |
| Per-turn text telemetry | `api/chat.py` | Stores `llm`, `rag` ms, `rag_sources`, rewritten query, lead status | No aggregation |
| Distance-gate tuning | README "Retrieval" | 0.30 chosen from a handful of manual questions | Not reproducible |

There was no golden dataset, no runner, no answer scoring, no tracing and no
LangChain/LangGraph/LangSmith dependency (the app calls Groq directly and uses
Pipecat for voice).

## 2. What this work adds

| Component | File |
|---|---|
| Dataset format + strict validation | `evals/schema.py` |
| Golden dataset v1 (75 cases, **all unreviewed**) | `evals/datasets/nimbuscrm_golden_v1.jsonl` |
| Retrieval metrics (Recall@K, Precision@K, MRR), latency percentiles | `evals/metrics.py` |
| Deterministic evaluators | `evals/evaluators.py` |
| Optional LLM judge | `evals/judge.py` |
| Runner and CLI | `evals/runner.py`, `evals/run.py` |
| Systems under test (retrieval, in-process API, replay) | `evals/targets.py` |
| Run storage, secret scrubbing, baselines | `evals/store.py`, `evals/baseline.py` |
| Aggregation, baseline comparison, report | `evals/aggregate.py`, `evals/compare.py`, `evals/report.py` |
| LangSmith dataset/experiment sync | `evals/langsmith_sync.py` (see `docs/LANGSMITH.md`) |
| Opt-in production tracing | `api/tracing.py` |
| Dashboard API and page | `api/eval_routes.py`, `api/static/eval_dashboard.html` |
| Shared consent-gate function | `api/leads.py::check_lead_proposal` |

## 3. Running it

```powershell
# No services, no cost: shows the plan only
.\.venv313\Scripts\python.exe -m evals.run --dry-run

# Retrieval only. Needs Postgres, the ingested knowledge base and the local
# embedding model. Makes no LLM calls.
.\.venv313\Scripts\python.exe -m evals.run --mode retrieval --org nimbus-crm-demo

# Full pipeline. Paid LLM calls: prints an upper-bound estimate first and stops at --max-llm-calls
.\.venv313\Scripts\python.exe -m evals.run --mode full --allow-llm --dry-run
.\.venv313\Scripts\python.exe -m evals.run --mode full --allow-llm --max-llm-calls 150

# Add the LLM judge (extra calls, same cap)
.\.venv313\Scripts\python.exe -m evals.run --mode full --allow-llm --judge

# Re-score a saved run with updated evaluators; no services needed
.\.venv313\Scripts\python.exe -m evals.run --mode replay --replay evals\results\<run_id>\cases.jsonl
```

Consent-gate cases (`lead_gate`) are pure code and run in every mode.
`--mode full` refuses to run with `APP_ENV=production` because it creates
conversations, messages and leads for the evaluation organization (it deletes
them again when it finishes).

Each run writes `evals/results/<run_id>/` (`run.json`, `cases.jsonl`,
`summary.md`). `evals/results/` is git-ignored and excluded from the Docker image.

Baselines:

```powershell
.\.venv313\Scripts\python.exe -m evals.baseline set <run_id> main
.\.venv313\Scripts\python.exe -m evals.run --mode retrieval --baseline main --fail-on-regression
```

### Diagnosing empty retrievals (read-only)

The live path applies the distance gate before returning anything, so a miss hides how near the best chunk was. This lists every candidate with its distance:

```powershell
.\.venv313\Scripts\python.exe -m evals.diagnose_retrieval --run evals\results\<run_id>
.\.venv313\Scripts\python.exe -m evals.diagnose_retrieval --case-id fact-import --top 6
```

pgvector `<=>` is cosine **distance** (lower is closer; a chunk passes when `distance <= RAG_MAX_DISTANCE`); similarity is `1 - distance`. It only issues SELECTs, calls no LLM, and changes nothing.

## 4. Dataset format

JSONL, one case per line, validated by `evals/schema.py` (unknown fields fail).

| Field | Meaning |
|---|---|
| `id`, `kind`, `category` | `kind` is `rag_answer`, `lead_gate` or `lead_llm` |
| `messages` | Conversation so far. For chat kinds the last message is the visitor's question; earlier turns are seeded as history |
| `expected_sources` | Relevant chunk **headings** (stable across re-ingestion; chunk UUIDs are not). Empty means nothing in the knowledge base answers it |
| `required_facts` | Each entry must appear in the reply. `a|b` means either alternative |
| `forbidden_facts`, `forbidden_regex` | Must not appear (hallucination traps, prompt injection) |
| `expect_abstain` | The reply should say it lacks the information or hand over to sales |
| `proposed_tool_call`, `expected_gate` | `lead_gate`: scripted `save_lead` proposal and whether the server-side gate must `saved`/`rejected` it |
| `expected_lead_saved`, `expected_lead_email` | `lead_llm`: whether the model should end with a stored lead |
| `review_status`, `reviewer` | `unreviewed`, `reviewed` or `excluded`. `reviewed` and `excluded` **require a named reviewer**; the parser rejects them otherwise. See `EVAL_REVIEW_GUIDE.md` |
| `facts_review_status`, `facts_reviewer` | `unreviewed` (default) or `reviewed`; `reviewed` **requires a named `facts_reviewer`**. Records that a human checked the case's `required_facts` against the knowledge files. Separate from `review_status` because the reviewer sheets do not check the keyword lists. Correctness is scored only when both are `reviewed`; see `EVAL_POLICY_DECISIONS.md` |
| `known_gap` | Free text for a case that documents behaviour the system is known not to meet yet |

Coverage of v1 (75 cases): 22 factual, 6 multi-fact, 3 follow-up (history
seeded), 6 unanswerable, 7 hallucination traps, 3 prompt-injection, 1 out of
scope, 3 edge (Hinglish, greeting, long question), 18 consent-gate, 6 live
lead-capture.

**Gap against the 100-200 target:** v1 has 75 cases, written from the three
files in `knowledge/nimbuscrm/`, and **0 are human-reviewed**. Expected sources
and facts are the author's reading of those files. A human must review each case
and set `review_status` and `reviewer` before any number can be treated as a
quality measure. Until then every report shows a warning.

## 5. Metrics

"Deterministic" means computed from strings and numbers with no model involved.

### Retrieval (deterministic; `rag_answer` cases with `expected_sources`)

Input: the ordered chunk headings returned by `search_knowledge` (after the
distance gate and fusion), compared with `expected_sources`.

* **Recall@K** = relevant headings found in the first K / relevant headings. K = 1, 3, 4 (4 is `RAG_TOP_K`).
* **Precision@K** = relevant among the first K returned / number returned (at most K). The denominator is what was returned, not K, because the distance gate may return fewer than K on purpose.
* **MRR** = 1 / rank of the first relevant heading, 0 if none.
* **empty_rate** (lower is better) = share of answerable cases where nothing was retrieved.
* **empty_when_unanswerable** = share of unanswerable cases where the gate returned nothing. Informational: unrelated chunks are not by themselves a wrong answer.

### Answer checks (deterministic heuristics; need a reply)

* **required_facts** = share of required facts present (word-boundary, case-insensitive; `29` does not match `290`).
* **forbidden_clear** = no forbidden fact or regex present.
* **abstains_when_unknown** = a lack-of-information or hand-over phrase matched (regex heuristic).
* **plain_text** and **within_prompt_length**: no markdown; at most 3 sentences (the agent prompt's own rules). Non-blocking.

These are string checks. A paraphrase that omits the literal fact fails;
a wrong answer that happens to contain it passes. They exist to catch
regressions cheaply, not to judge answers.

### Lead capture and tool calls

* **gate_correct / gate_false_save / gate_false_reject** (deterministic, `lead_gate`): the scripted proposal is run through `api.leads.check_lead_proposal`, the same function the route uses before saving. **gate_false_save** (saving without valid consent) should be 0 and is the most important lead metric.
* **llm_outcome_correct, premature_tool_call, email_correct, no_contact_promise** (`lead_llm`, needs a live model): did the model end with a stored lead exactly when it should, did it call the tool too early, is the stored email right, did it promise that sales will contact the visitor (the prompt forbids it). Tool *argument* correctness beyond name/email is observable only in gate cases, because the route does not expose raw tool-call arguments.

### LLM judge (`--judge`; not ground truth)

One call per answered case returns faithfulness, relevance and correctness as 1-5
integers, normalised to 0-1, plus unsupported claims and a short explanation.
Inputs: question, retrieved chunk text, `required_facts` as the reference, and
whether the agent should abstain. Limits: it is a model and can be wrong or
inconsistent; by default it is the same model as the agent, which favours its own
style; it cannot see chunks the retriever missed; 1-5 scores are coarse. Invalid
or failed judge output is recorded as a judge error and excluded, never scored.
Treat judge numbers as a second opinion to read alongside the failed examples.

### Latency, errors, health

* **End to end** = wall-clock of the in-process route call; **model call** = the route's reported LLM time; **retrieval** = `search_knowledge` time. p90/p95/p99 are reported only with at least 10/20/100 observations.
* **error_rate** = cases where the target raised (provider, database, HTTP non-200). Only the exception type is stored.
* **pass_rate** = cases whose blocking checks all passed.
* Voice latency, interruptions: not part of a run. They come from stored call telemetry (`/v1/evals/voice-latency`, dashboard bottom panel).

## 6. Coverage against the requested areas

| # | Area | Status |
|---|---|---|
| 1 | Correctness / hallucination | Deterministic fact and trap checks; judge optional. Literal-match limits above |
| 2 | Faithfulness to context | Judge only (no deterministic proxy) |
| 3 | Answer relevance | Judge only |
| 4 | Retrieval quality | Recall@K, Precision@K, MRR, empty rate. Ground truth unreviewed |
| 5 | Voice latency | Telemetry percentiles on the dashboard; **no scripted voice evaluation** |
| 6 | Turn-taking, interruptions | Only the stored `interrupted` rate. **No evaluation of VAD/turn detection quality** |
| 7 | Tool calling, lead capture | Gate cases (deterministic) and 6 live cases. Few live cases |
| 8 | Failure handling | Target errors counted; empty retrieval measured. **Malformed model output and LLM-down paths are covered by unit tests, not by dataset cases** |
| 9 | Security / tenant isolation | `tests/test_auth.py` and `tests/test_eval_routes.py`. Prompt-injection cases are few (3) |
| 10 | Dataset, runner, regression, storage, reports | Implemented as above |

## 7. Baselines and acceptance criteria

**No baseline exists.** Creating one requires a real run, which has not been done
here. Procedure: run `--mode retrieval` and `--mode full --allow-llm` three times
on an unchanged build, inspect the spread, then save the best-understood run.

Provisional comparison tolerances (`evals/compare.py`, **uncalibrated**): a ratio
metric regresses when it drops by more than 0.05 absolute; a latency percentile
when it rises by more than 25%; metrics with fewer than 5 observations on either
side are not compared. They are conservative starting points for a small dataset.

Proposed acceptance criteria, to be adopted only after calibration and review of
the dataset:

* `lead.gate_false_save` = 0 on every run (a hard rule, independent of calibration).
* No `error_rate` increase over baseline.
* Retrieval Recall@4 and MRR not below baseline minus the calibrated tolerance.
* No new failures in `hallucination_trap` and `prompt_injection` categories.

### Calibrating thresholds

1. Review every case (`review_status: reviewed`, with a reviewer).
2. Run the same build at least 5 times; record the run-to-run spread per metric.
3. Set each tolerance above that spread (a metric that varies by 0.04 by itself cannot use 0.05).
4. Record the chosen values and the runs they came from here.

## 8. Known limitations

* The dataset is small, single-tenant (NimbusCRM demo) and unreviewed.
* Answer checks are literal; the judge is the same model family.
* `follow_up` rewriting is exercised only in `--mode full` (it needs the model); `retrieval` mode skips those cases.
* `--mode full` drives the real routes in-process but bypasses API-key auth (covered by `tests/test_auth.py`) and does not run the Pipecat voice pipeline.
* Latency from `full` mode reflects the machine and network it ran on.
* Results are files on disk; a container deployment needs a volume (or run evaluations elsewhere) for the dashboard to show them.
* The dashboard shows `evals/baselines` (tracked in Git) and results of this organization only, selected by `organization_id` in `run.json`.
* Not evaluated: STT accuracy, TTS quality, barge-in behaviour, end-of-turn detection, multilingual quality beyond one Hinglish case.
