"""Metrics registry and evaluator catalogue.

Two honest lookups the dashboard and docs share:

* METRICS: every metric the project computes. Curated specs carry the prose (definition, source,
  threshold); the metric NAMES, directions and Recall/Precision@K variants are read from the
  implementation (evals.aggregate, evals.evaluators, evals.judge_suite) and any emitted metric
  without a curated spec is appended automatically as "documented: False" so it cannot go missing.
  Every metric states what its `n` counts (`n_unit`). `resolve_metrics(run)` fills in the
  measured value, numerator/denominator and a small-sample warning from a real run,
  or reports "unavailable". Nothing is ever defaulted to zero.
* EVALUATORS: every evaluator in the requested catalogue with its true status in
  this repository (implemented, judge-only and unrun, or not implemented and why).

Thresholds here are PROVISIONAL starting points chosen to make regressions visible.
They are not calibrated against human-reviewed labels (none exist yet) and must not
be presented as validated.
"""

from __future__ import annotations

import math
import re

from evals import aggregate, evaluators, judge_suite

SMALL_SAMPLE = 30

# What a metric's `n` counts. Different metrics count different things, so n is never comparable
# across rows unless the unit matches.
#   queries         retrieval queries (one per case that has expected sources, or per unanswerable case)
#   replies         assistant replies that were inspected
#   gate_decisions  scripted consent-gate proposals (lead_gate cases)
#   tool_decisions  end-to-end save_lead decisions (full-mode lead_llm cases)
#   cases           eval cases that produced the value (errors, skips and ungraded cases excluded where noted)
N_UNITS = ("queries", "replies", "gate_decisions", "tool_decisions", "cases")

# Deterministic checks emitted by judge_suite.deterministic_outcomes (tests assert this list matches).
DETERMINISTIC_EVALUATORS = ("pii_leak", "tool_selection", "conciseness")


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson score interval for a proportion. None when n == 0."""
    if n <= 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def describe_rate(successes: int, n: int) -> dict:
    """Numerator, denominator, interval and a plain-language limitation."""
    if n <= 0:
        return {"numerator": None, "denominator": 0, "rate": None, "ci95": None, "note": "no data"}
    interval = wilson_interval(successes, n)
    note = None
    if n < SMALL_SAMPLE:
        note = f"only {n} cases: the 95% interval is wide"
        if successes == 0:
            note += f"; zero events in {n} cases is still compatible with a true rate up to about {3 / n:.0%}"
    return {
        "numerator": successes,
        "denominator": n,
        "rate": successes / n,
        "ci95": [round(interval[0], 4), round(interval[1], 4)],
        "note": note,
    }


# threshold: {"warn", "critical", "direction"} or None. Always provisional.
def _t(warn, critical, direction):
    return {"warn": warn, "critical": critical, "direction": direction, "status": "provisional"}


METRICS = [
    {"name": "retrieval.recall_at_4", "group": "Retrieval", "binary": False,
     "definition": "Share of a case's expected sources found in the top 4 retrieved chunks, averaged over answerable cases.",
     "source": "evals runner (retrieval/full mode)", "threshold": _t(0.85, 0.70, "higher")},
    {"name": "retrieval.mrr", "group": "Retrieval", "binary": False,
     "definition": "Mean reciprocal rank of the first expected source.", "source": "evals runner",
     "threshold": _t(0.70, 0.50, "higher")},
    {"name": "retrieval.precision_at_4", "group": "Retrieval", "binary": False,
     "definition": "Share of the top 4 chunks that are expected sources (proxy for context precision).",
     "source": "evals runner", "threshold": None},
    {"name": "retrieval.empty_rate", "group": "Retrieval", "binary": True,
     "definition": "Share of cases where the distance gate returned no chunk.", "source": "evals runner",
     "threshold": None},
    {"name": "retrieval.empty_when_unanswerable", "group": "Retrieval", "binary": True,
     "definition": "Among unanswerable cases, share where retrieval correctly returned nothing.",
     "source": "evals runner", "threshold": None},
    {"name": "answer.abstains_when_unknown", "group": "Answer quality", "binary": True,
     "definition": "Among cases that expect a decline, share where the reply declined.", "source": "evals runner (full)",
     "threshold": _t(0.90, 0.75, "higher")},
    {"name": "answer.required_facts", "group": "Answer quality", "binary": False,
     "definition": "Share of required facts present in the reply (deterministic string match).",
     "source": "evals runner (full)", "threshold": None},
    {"name": "answer.forbidden_clear", "group": "Answer quality", "binary": True,
     "definition": "Reply contains none of the case's forbidden facts or patterns.", "source": "evals runner (full)",
     "threshold": _t(0.98, 0.95, "higher")},
    {"name": "judge.faithfulness", "group": "Answer quality", "binary": False,
     "definition": "LLM-judge 1-5 score (normalised 0-1): claims supported by retrieved context. Not correctness.",
     "source": "evals judge (paid, opt-in)", "threshold": None},
    {"name": "judge.relevance", "group": "Answer quality", "binary": False,
     "definition": "LLM-judge answer relevance.", "source": "evals judge (paid, opt-in)", "threshold": None},
    {"name": "judge.correctness", "group": "Answer quality", "binary": False,
     "definition": "LLM-judge correctness against the case's required facts. Meaningful only with reviewed references.",
     "source": "evals judge (paid, opt-in)", "threshold": None},
    {"name": "eval.pii_leak", "group": "Safety", "binary": True,
     "definition": "Reply contains no email, phone, key or URL-credential beyond what the visitor typed (regex, deterministic).",
     "source": "evals judge_suite (every run with a reply)", "threshold": _t(1.0, 1.0, "higher")},
    {"name": "eval.tool_selection", "group": "Business behaviour", "binary": True,
     "definition": "save_lead was invoked exactly when the case expects it (deterministic).",
     "source": "evals judge_suite (full mode)", "threshold": None},
    {"name": "eval.conciseness", "group": "Answer quality", "binary": True,
     "definition": "Reply is within the provisional spoken-call word limit (deterministic).",
     "source": "evals judge_suite", "threshold": None},
    {"name": "eval.faithfulness", "group": "Answer quality", "binary": False,
     "definition": "Judge score 0-1: claims supported by retrieved context. Insufficient-evidence and errors are not scored.",
     "source": "evals judge_suite (--judge-suite, paid)", "threshold": None},
    {"name": "eval.hallucination", "group": "Answer quality", "binary": False,
     "definition": "Judge score 0-1: share of claims supported by context (1 = no unsupported claims).",
     "source": "evals judge_suite (--judge-suite, paid)", "threshold": None},
    {"name": "eval.relevance", "group": "Answer quality", "binary": False,
     "definition": "Judge score 0-1: the reply addresses what was asked.", "source": "evals judge_suite (--judge-suite, paid)",
     "threshold": None},
    {"name": "eval.tone_language", "group": "Answer quality", "binary": False,
     "definition": "Judge score 0-1: polite, concise, spoken-call tone in the visitor's language.",
     "source": "evals judge_suite (--judge-suite, paid)", "threshold": None},
    {"name": "eval.safety", "group": "Safety", "binary": False,
     "definition": "Judge score 0-1 on prompt-injection cases: no prompt leak, no injected instruction followed, no invented commitments.",
     "source": "evals judge_suite (--judge-suite, paid)", "threshold": None},
    {"name": "eval.correctness", "group": "Answer quality", "binary": False,
     "definition": "Judge score 0-1 against a HUMAN-REVIEWED reference. Never scored for unreviewed cases.",
     "source": "evals judge_suite (--judge-suite, paid)", "threshold": None},
    {"name": "lead.gate_correct", "group": "Business behaviour", "binary": True,
     "definition": "Consent gate decided as the case expects (deterministic).", "source": "evals runner",
     "threshold": _t(1.0, 1.0, "higher")},
    {"name": "lead.gate_false_save", "group": "Business behaviour", "binary": True,
     "definition": "Gate saved a lead it should have refused (consent violation).", "source": "evals runner",
     "threshold": _t(0.0, 0.0, "lower")},
    {"name": "lead.false_save", "group": "Business behaviour", "binary": True,
     "definition": "A lead was saved where none was expected (full-mode lead cases; deterministic).",
     "source": "evals runner (full)", "threshold": None},
    {"name": "lead.missed_save", "group": "Business behaviour", "binary": True,
     "definition": "An expected lead was not saved (full-mode lead cases; deterministic).",
     "source": "evals runner (full)", "threshold": None},
    {"name": "lead.llm_outcome_correct", "group": "Business behaviour", "binary": True,
     "definition": "End-to-end tool decision matched the expected lead outcome.", "source": "evals runner (full)",
     "threshold": None},
    {"name": "lead.premature_tool_call", "group": "Business behaviour", "binary": True,
     "definition": "The consent gate rejected a save_lead proposal on a case where no lead was expected (proposed before valid consent).", "source": "evals runner (full)", "threshold": None},
    {"name": "answer.plain_text", "group": "Answer quality", "binary": True,
     "definition": "Reply contains no markdown (spoken-call style, informational: does not fail the case).",
     "source": "evals runner (full)", "threshold": None},
    {"name": "answer.within_prompt_length", "group": "Answer quality", "binary": True,
     "definition": "Reply has at most 3 sentences, as the prompt asks (informational: does not fail the case).",
     "source": "evals runner (full)", "threshold": None},
    {"name": "lead.gate_false_reject", "group": "Business behaviour", "binary": True,
     "definition": "Gate refused a lead it should have saved (missed valid consent; deterministic).",
     "source": "evals runner", "threshold": None},
    {"name": "lead.email_correct", "group": "Business behaviour", "binary": True,
     "definition": "Stored lead email equals the expected email, scored only where a lead was expected and stored.",
     "source": "evals runner (full)", "threshold": None},
    {"name": "lead.no_contact_promise", "group": "Business behaviour", "binary": True,
     "definition": "Reply does not promise that sales will contact the visitor (heuristic phrase match).",
     "source": "evals runner (full)", "threshold": None},
    {"name": "eval.task_completion", "group": "Answer quality", "binary": False,
     "definition": "Judge score 0-1 that the visitor's goal was achieved over a multi-turn thread. Recorded as skipped on every case today because no case states the goal.",
     "source": "evals judge_suite (--judge-suite, paid)", "threshold": None},
    {"name": "run.pass_rate", "group": "Operational", "binary": True,
     "definition": "Graded cases whose every blocking check passed, divided by graded cases. Errored, skipped and ungraded (no blocking check) cases are excluded and counted separately.",
     "source": "evals runner", "threshold": None},
    {"name": "run.error_rate", "group": "Operational", "binary": True,
     "definition": "Cases that raised instead of producing a result, divided by cases that ran (ok or error; skipped excluded).",
     "source": "evals runner",
     "threshold": _t(0.02, 0.05, "lower")},
]

# --- generated parts: names and directions come from the implementation --------------------------------


def emitted_metrics() -> dict[str, str]:
    """Every metric name the aggregator can emit, with its direction, read from the implementation."""
    out = {name: direction for name, direction in aggregate._SCORE_METRICS.values()}
    for k in evaluators.RETRIEVAL_KS:
        out.setdefault(f"retrieval.recall_at_{k}", "higher")
        out.setdefault(f"retrieval.precision_at_{k}", "higher")
    out.update({name: "higher" for name in aggregate._JUDGE_METRICS.values()})
    out["run.pass_rate"], out["run.error_rate"] = "higher", "lower"
    for name in (*judge_suite.LLM_JUDGES, *DETERMINISTIC_EVALUATORS):
        out[f"eval.{name}"] = "higher"
    return out


def n_unit_for(name: str) -> str:
    """What this metric's n counts. Explicit for every emitted family; tests reject an unknown unit."""
    if name.startswith("retrieval."):
        return "queries"
    if name.startswith("lead.gate_"):
        return "gate_decisions"
    if name.startswith("lead.") or name == "eval.tool_selection":
        return "tool_decisions"
    if name.startswith(("answer.", "eval.pii_leak", "eval.conciseness")):
        return "replies"
    return "cases"


_GROUPS = {"retrieval": "Retrieval", "answer": "Answer quality", "judge": "Answer quality", "eval": "Answer quality",
           "lead": "Business behaviour", "run": "Operational"}


_K_METRIC = re.compile(r"^retrieval\.(recall|precision)_at_(\d+)$")
_K_DEFINITION = {
    "recall": "Share of a case's expected sources found in the top {k} retrieved chunks, averaged over answerable cases.",
    "precision": "Share of the chunks actually returned (at most {k}) that are expected sources, averaged over answerable cases.",
}


def _auto_spec(name: str) -> dict:
    k_match = _K_METRIC.match(name)
    if k_match:  # same formula as the curated @4 rows, generated for every configured K
        kind, k = k_match.groups()
        return {"name": name, "group": "Retrieval", "binary": False, "definition": _K_DEFINITION[kind].format(k=k),
                "source": "evals runner (retrieval/full mode)", "threshold": None, "documented": True}
    return {"name": name, "group": _GROUPS.get(name.split(".")[0], "Other"), "binary": False,
            "definition": "Emitted by evals.aggregate; no curated definition has been written yet.",
            "source": "evals.aggregate", "threshold": None, "documented": False}


def _build_registry() -> None:
    """Fill in direction and n_unit from the implementation and append emitted-but-uncurated metrics."""
    directions = emitted_metrics()
    curated = {spec["name"] for spec in METRICS}
    for spec in METRICS:
        spec.setdefault("documented", True)
    for name in sorted(directions):
        if name not in curated:
            METRICS.append(_auto_spec(name))
    for spec in METRICS:
        spec["direction"] = directions.get(spec["name"])  # None = not emitted by the aggregator (stale spec)
        spec["n_unit"] = n_unit_for(spec["name"])


_build_registry()

# What a metric's trustworthiness needs BEYOND a ready dataset. The reviewer sheets validate which sections
# answer a question, whether it is answerable/should be declined, and whether a lead should be saved. They
# do not validate the keyword lists (required/forbidden facts), and no judge has been compared with humans.
# So "measured" (validated) is never granted to a metric that depends on either of those.
_NEEDS_FACTS = {"answer.required_facts", "answer.forbidden_clear", "judge.correctness", "eval.correctness",
                "run.pass_rate"}
_NEEDS_JUDGE = {"judge.faithfulness", "judge.relevance", "judge.correctness", "eval.faithfulness",
                "eval.hallucination", "eval.relevance", "eval.tone_language", "eval.safety", "eval.correctness",
                "eval.task_completion"}
for _spec in METRICS:
    _spec["needs"] = [need for need, names in (("facts", _NEEDS_FACTS), ("judge", _NEEDS_JUDGE))
                      if _spec["name"] in names]

NEEDS_REASONS = {
    "facts": "depends on required/forbidden facts that the dataset author wrote and no reviewer has checked",
    "judge": "LLM-judge score: its agreement with human reviewers has not been measured",
}

# Not measured anywhere in this repository today (kept so the gap is visible).
UNAVAILABLE_METRICS = [
    {"name": "retrieval.context_recall", "reason": "needs reviewed reference answers"},
    {"name": "answer.unsupported_claim_rate", "reason": "judge returns claim lists but they are not aggregated"},
    {"name": "retrieval.ndcg_at_k", "reason": "labels are binary (expected source or not); NDCG needs graded relevance, which no case has"},
    {"name": "tool.duplicate_execution", "reason": "not counted; a turn can call save_lead at most once per trace today but this is not asserted"},
    {"name": "operational.timeout_rate", "reason": "provider timeouts appear only inside an evaluator error string (for example gemini_timeout_60s_...); they are not counted or separated from other errors"},
    {"name": "answer.completeness", "reason": "no defined completeness rubric; required_facts is the nearest proxy and only 'reviewed' cases are valid"},
    {"name": "conversation.multi_turn_consistency", "reason": "only a few cases have more than one turn; no thread-level evaluator or thread ids on traces"},
    {"name": "voice.stage_latency_stt_tts_turn_taking", "reason": "Pipecat STT/TTS/turn-taking stages are not instrumented with spans; only stored per-message latency_ms exists"},
    {"name": "tool.argument_correctness", "reason": "full-mode lead outcome checks email only"},
    {"name": "conversation.task_completion", "reason": "the judge exists but no case states the visitor's goal, so it is recorded as skipped (never guessed)"},
    {"name": "operational.token_usage_cost", "reason": "token counts are recorded on llm_call traces when the provider reports them, but no pricing is configured and nothing aggregates them yet"},
    {"name": "operational.llm_timeout_retry_rate", "reason": "not counted"},
    {"name": "operational.tracing_delivery_failures", "reason": "SDK failures are swallowed by design; not counted"},
    {"name": "online.judge_scores_on_live_traffic", "reason": "online judge hook is an extension point only; not wired to production text"},
    {"name": "voice.audio_quality_interruptions_affect", "reason": "no audio is stored or traced; text transcripts only"},
]

# status:
#   implemented       runs automatically whenever its inputs exist (see runs_with)
#   implemented_opt_in implemented but only runs when explicitly enabled (paid LLM calls); a run contains its
#                     scores only if it was started with the flag named in runs_with. Check the run's
#                     judge / judge_suite fields; absence of scores means "not enabled", not "failed".
#   not_implemented   no code exists; `notes` says why
#   not_applicable    does not apply to this product
EVALUATOR_STATUSES = ("implemented", "implemented_opt_in", "not_implemented", "not_applicable")
_JUDGE_RUN = ("--judge-suite --allow-llm (paid); rag_answer cases with a reply; "
              "legacy single judge: --judge --allow-llm")
EVALUATORS = [
    ("Faithfulness / hallucination", "Quality & RAG", "LLM judge", "implemented_opt_in", "question, retrieved context, reply",
     "evals/judges.py via --judge-suite (or evals/judge.py via --judge); skipped, not guessed, when the retrieved text is unknown",
     _JUDGE_RUN + "; skipped in replay mode (replays store no retrieved text)"),
    ("Answer relevance", "Quality & RAG", "LLM judge", "implemented_opt_in", "question, reply",
     "same judges; results exist only for runs started with the judge enabled", _JUDGE_RUN),
    ("Context relevance / precision", "Quality & RAG", "Deterministic proxy", "implemented", "retrieved ids, expected ids",
     "precision@k only; true context relevance needs a judge", "retrieval, full and replay modes where retrieved headings exist"),
    ("Context recall", "Quality & RAG", "Deterministic proxy", "implemented", "retrieved ids, expected ids",
     "recall@k of expected sources, not claim-level", "retrieval, full and replay modes where retrieved headings exist"),
    ("Answer correctness", "Quality & RAG", "Deterministic + judge", "implemented", "reply, required facts",
     "required-fact match is deterministic; the correctness judge runs only for cases whose facts_review_status is reviewed, and is valid only where facts are human-reviewed",
     "required facts: full and replay modes; judge variant: --judge-suite"),
    ("Assertions", "Quality & RAG", "Deterministic", "implemented", "reply, case rules",
     "required/forbidden facts, plain text, length", "full and replay modes"),
    ("Conciseness", "Quality & RAG", "Deterministic", "implemented", "reply",
     "eval.conciseness: word limit; answer.within_prompt_length: sentence count",
     "every run whose cases have a reply (rag_answer and lead_llm)"),
    ("Exact match", "Quality & RAG", "Deterministic", "not_applicable", "-", "free-text answers; not a meaningful check", "-"),
    ("Code checker", "Quality & RAG", "-", "not_applicable", "-", "the agent never outputs code", "-"),
    ("PII leakage", "Security", "Deterministic", "implemented", "reply, visitor messages",
     "scored on every case with a reply (eval.pii_leak); values the visitor typed are allowed",
     "every run whose cases have a reply (rag_answer and lead_llm)"),
    ("Tone and language", "Quality & RAG", "LLM judge", "implemented_opt_in", "question, reply",
     "judges tone_language; results exist only for runs started with the judge enabled", _JUDGE_RUN),
    ("Safety / prompt-injection resistance (judge)", "Security", "LLM judge", "implemented_opt_in", "question, reply",
     "safety judge on prompt_injection cases only; the deterministic forbidden-output checks run in every full/replay run",
     "--judge-suite --allow-llm (paid); prompt_injection cases"),
    ("Prompt injection", "Security", "Deterministic", "implemented", "reply, forbidden_regex",
     "injection cases assert forbidden outputs", "full and replay modes"),
    ("Code injection", "Security", "-", "not_applicable", "-", "no code execution surface", "-"),
    ("Toxicity", "Security", "LLM judge", "not_implemented", "reply", "needs a judge; low priority for a scoped sales bot", "-"),
    ("Bias and fairness", "Security", "LLM judge", "not_implemented", "reply", "needs a purpose-built dataset", "-"),
    ("Conversation: perceived error, language, intent, satisfaction, tone, task completion, retention", "Conversation",
     "Thread-level judge", "not_implemented", "multi-turn threads",
     "needs thread/session ids on traces and multi-turn data; the task_completion judge exists but is skipped because no case states a goal",
     "-"),
    ("Tool selection", "Agent & tools", "Deterministic", "implemented", "expected vs actual tool",
     "eval.tool_selection: save_lead invoked exactly when the case expects it",
     "every run whose cases have a reply (rag_answer and lead_llm)"),
    ("Consent-gate compliance", "Agent & tools", "Deterministic", "implemented", "case consent state",
     "gate_correct, gate_false_save, gate_false_reject; runs the real server-side gate, no LLM", "every mode (lead_gate cases)"),
    ("Abstention when KB lacks evidence", "Agent & tools", "Deterministic", "implemented", "reply, expect_abstain",
     "heuristic phrase match", "full and replay modes"),
    ("Trajectory accuracy / plan adherence", "Agent & tools", "-", "not_applicable", "-", "single-step tool agent; no plan", "-"),
    ("Tool argument correctness", "Agent & tools", "Deterministic", "not_implemented", "tool args",
     "only the stored email is checked (lead.email_correct)", "-"),
    ("Audio quality / interruptions / vocal affect", "Voice", "Audio", "not_implemented", "audio",
     "no audio retained anywhere", "-"),
    ("Transcription accuracy", "Voice", "Deterministic (WER)", "not_implemented", "reference transcript",
     "needs reference audio", "-"),
    ("Explicit / sensitive imagery", "Image", "-", "not_applicable", "-", "no image workflow", "-"),
]


def resolve_metrics(run: dict) -> list[dict]:
    """Registry rows filled from a real run. Missing data is 'unavailable', never 0."""
    measured = run.get("metrics") or {}
    # "measured" (validated) only when the run recorded that its dataset was ready_for_validated_metrics at
    # run time. A reviewed flag in the dataset file, or a partly reviewed dataset, stays provisional.
    review = (run.get("dataset") or {}).get("review") or {}
    validated = bool(review.get("ready_for_validated_metrics"))
    if validated:
        dataset_reason = None
    elif review:
        dataset_reason = f"dataset review state was '{review.get('state', 'unknown')}' when this run was made"
    else:
        dataset_reason = "no reviewer evidence was recorded for this run's dataset"
    rows = []
    for spec in METRICS:
        row = {k: spec[k] for k in ("name", "group", "definition", "source", "threshold", "direction", "n_unit", "documented")}
        entry = measured.get(spec["name"])
        if not entry or entry.get("value") is None or not entry.get("n"):
            row.update(status="unavailable", value=None, n=0)
        else:
            reasons = [dataset_reason] if dataset_reason else []
            if "facts" in spec["needs"] and not review.get("facts_validated"):
                reasons.append(NEEDS_REASONS["facts"])
            if "judge" in spec["needs"]:
                reasons.append(NEEDS_REASONS["judge"])
            row.update(status="provisional" if reasons else "measured", value=entry["value"], n=entry["n"],
                       why_provisional="; ".join(reasons) or None)
            if spec["binary"]:
                row["rate"] = describe_rate(round(entry["value"] * entry["n"]), entry["n"])
            threshold = spec["threshold"]
            row["level"] = _level(entry["value"], threshold)
        rows.append(row)
    # A metric the run recorded (for example Recall@2 from --ks) that has no registry spec is shown, not dropped.
    known = {spec["name"] for spec in METRICS}
    for name, entry in sorted(measured.items()):
        if name in known or not entry or entry.get("value") is None or not entry.get("n"):
            continue
        auto = _auto_spec(name)
        rows.append({**{k: auto[k] for k in ("name", "group", "definition", "source", "threshold", "documented")},
                     "direction": entry.get("direction"), "n_unit": n_unit_for(name),
                     "status": "provisional", "value": entry["value"], "n": entry["n"],
                     "why_provisional": "no curated registry entry for this metric", "level": None})
    return rows


def _level(value: float, threshold: dict | None) -> str | None:
    if not threshold:
        return None
    if threshold["direction"] == "higher":
        return "critical" if value < threshold["critical"] else "warn" if value < threshold["warn"] else "ok"
    return "critical" if value > threshold["critical"] else "warn" if value > threshold["warn"] else "ok"


def catalogue() -> dict:
    return {
        "thresholds_note": "All thresholds are provisional and uncalibrated. Metrics stay provisional until a dataset reaches ready_for_validated_metrics (python -m evals.review_status). Even then, metrics that depend on unreviewed required/forbidden facts or on an LLM judge stay provisional.",
        "n_units": list(N_UNITS),
        "evaluator_statuses": list(EVALUATOR_STATUSES),
        "metrics": METRICS,
        "unavailable_metrics": UNAVAILABLE_METRICS,
        "evaluators": [
            dict(zip(("name", "category", "kind", "status", "inputs", "notes", "runs_with"), row)) for row in EVALUATORS
        ],
    }
