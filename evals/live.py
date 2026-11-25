"""Post-hoc evaluation of persisted live turns (chat and voice).

Pure logic, no I/O: it takes what a turn already stored (the visitor's text, the reply, and the
`latency_ms` JSON that both channels write on the assistant message) and returns an evaluation record.
Nothing here can slow or fail a call, because it runs after the turn was persisted.

Trust model
-----------
A live turn has no reference answer of its own. The only references this project owns are the golden
dataset cases, so a turn is graded ONLY when the visitor's question matches a dataset case exactly
(after normalisation). Everything else is "ungraded: no trusted reference" and is never counted as a
pass or a failure. Reference-free checks (PII, length, plain text, contact promises, a heuristic
grounding flag) are recorded as signals, not as correctness.

  reference.quality = "reviewed"           facts were checked by a named human (facts_review_status)
                    = "author_unreviewed"  the dataset author wrote the facts; no reviewer has checked
                                           them, so any correctness verdict is provisional
                    = "none"               no matching case: ungraded

Status values follow the offline runner (passed | failed | ungraded | error) plus `skipped` for a turn
that should not be judged at all (empty reply, or a voice reply the visitor interrupted, which is only
partly spoken). `pending` is a database-level state for a turn that has no evaluation row yet; this
module never returns it.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from evals import evaluators, judges
from evals.schema import Dataset, EvalCase

EVALUATOR_VERSION = "live-1"
CONCISE_MAX_WORDS = 60  # same provisional limit as evals.judge_suite

STATUSES = ("passed", "failed", "ungraded", "error", "skipped")
CORRECTNESS = ("correct", "partial", "incorrect")
REFERENCE_QUALITIES = ("reviewed", "author_unreviewed", "none")


def normalize_question(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (text or "").lower())).strip()


def build_case_index(dataset: Dataset) -> dict[str, EvalCase]:
    """Normalised question -> case, for single-question answer cases only.

    A question that appears in more than one case is dropped rather than guessed, and excluded cases
    never serve as references.
    """
    seen: dict[str, EvalCase | None] = {}
    for case in dataset.cases:
        if case.kind != "rag_answer" or case.review_status == "excluded":
            continue
        if sum(1 for m in case.messages if m["role"] == "user") != 1:
            continue
        key = normalize_question(case.query)
        seen[key] = None if key in seen else case
    return {key: case for key, case in seen.items() if case is not None}


def reference_quality(case: EvalCase | None) -> str:
    if case is None:
        return "none"
    if case.review_status == "reviewed" and case.facts_review_status == "reviewed":
        return "reviewed"
    return "author_unreviewed"


def _retrieved(latency: dict | None):
    """(headings or None, state). None means retrieval was not recorded or failed: never 'empty'."""
    latency = latency or {}
    if latency.get("rag_error"):
        return None, "error"
    sources = latency.get("rag_sources")
    if not isinstance(sources, list):
        return None, "not_instrumented"
    return [str(s) for s in sources], "measured"


def _correctness(case: EvalCase, reply: str) -> tuple[str | None, str]:
    """correct | partial | incorrect from the case's own expectations, with a one-line reason."""
    if evaluators.forbidden_hits(reply, case.forbidden_facts, case.forbidden_regex):
        return "incorrect", "reply contains a forbidden fact (contradicts the reference)"
    if case.required_facts:
        missing = evaluators.missing_facts(reply, case.required_facts)
        found = len(case.required_facts) - len(missing)
        if not missing:
            return "correct", "all required facts present"
        if found == 0:
            return "incorrect", f"none of {len(case.required_facts)} required facts present"
        return "partial", f"missing {len(missing)} of {len(case.required_facts)} required facts: {missing}"
    if case.expect_abstain:
        ok = evaluators.looks_like_abstention(reply)
        return ("correct", "declined as expected") if ok else ("incorrect", "answered where the reference expects a decline")
    return None, "the matching case states no required facts or expected decline"


def evaluate_turn(*, user_text: str, reply: str, latency: dict | None, case_index: dict[str, EvalCase],
                  visitor_texts: list[str] | None = None, reference_available: bool = True) -> dict:
    """One evaluation record for one assistant turn. Never raises for odd input; returns status 'error'.

    reference_available=False means no reference dataset applies to this organization at all; every
    turn is then ungraded with that explicit reason.
    """
    try:
        return _evaluate(user_text, reply, latency, case_index, visitor_texts or [user_text], reference_available)
    except Exception as error:  # an evaluator bug must never look like a wrong answer
        return {"evaluator_version": EVALUATOR_VERSION, "status": "error", "correctness": None,
                "error": type(error).__name__, "reference": {"quality": "none", "case_id": None},
                "checks": [], "flags": [], "retrieval": {"state": "unknown"}, "scores": {}}


def _evaluate(user_text, reply, latency, case_index, visitor_texts, reference_available=True) -> dict:
    from api.redact import sensitive_values

    reply = reply or ""
    skip_reason = None
    if not reply.strip():
        skip_reason = "the assistant message is empty"
    elif (latency or {}).get("interrupted") is True:
        skip_reason = "the visitor interrupted this reply, so only part of it was spoken; it is not judged"
    if skip_reason:
        return {"evaluator_version": EVALUATOR_VERSION, "status": "skipped", "correctness": None,
                "correctness_reason": None, "correctness_provisional": False, "ungraded_reason": None,
                "skipped_reason": skip_reason, "reference": {"quality": "none", "case_id": None, "match": None},
                "checks": [], "scores": {}, "flags": [], "retrieval": {"state": "not_evaluated"}}
    case = case_index.get(normalize_question(user_text)) if reference_available else None
    quality = reference_quality(case)
    retrieved, retrieval_state = _retrieved(latency)

    checks: list[evaluators.Check] = []
    scores: dict = {}
    correctness, correctness_reason = None, None
    if case is not None:
        reply_scores, reply_checks = evaluators.evaluate_reply(case, reply)
        scores.update(reply_scores)
        checks.extend(reply_checks)
        if retrieved is not None:
            retrieval_scores, retrieval_checks = evaluators.evaluate_retrieval(case, retrieved)
            scores.update({k: v for k, v in retrieval_scores.items() if v is not None})
            checks.extend(retrieval_checks)
        correctness, correctness_reason = _correctness(case, reply)

    # Reference-free signals: recorded for every turn, never used to grade correctness.
    flags: list[dict] = []
    leaked = judges.pii_leak(reply, allowed=tuple(v for text in visitor_texts for v in sensitive_values(text)))
    if leaked["leaked"]:
        flags.append({"name": "pii_leak", "detail": "reply contains: " + ", ".join(leaked["kinds"])})
    words = len(reply.split())
    if words > CONCISE_MAX_WORDS:
        flags.append({"name": "too_long", "detail": f"{words} words (provisional limit {CONCISE_MAX_WORDS})"})
    if evaluators.promises_contact(reply):
        flags.append({"name": "contact_promise", "detail": "reply promises that sales will contact the visitor"})
    if not evaluators.is_plain_text(reply):
        flags.append({"name": "markdown_in_spoken_reply", "detail": "markdown found"})
    if retrieval_state == "measured" and not retrieved and reply and not evaluators.looks_like_abstention(reply):
        flags.append({"name": "possible_ungrounded",
                      "detail": "nothing was retrieved but the reply does not decline (heuristic, not a verdict)"})

    blocking = [c for c in checks if c.blocking and c.passed is not None]
    if case is None:
        status = "ungraded"
    elif not blocking:
        status = "ungraded"
    else:
        status = "passed" if all(c.passed for c in blocking) else "failed"

    ungraded_reason = None
    if status == "ungraded":
        if not reference_available:
            ungraded_reason = "no reference dataset is configured for this organization"
        elif case is None:
            ungraded_reason = "no trusted reference: the question does not match a dataset case"
        else:
            ungraded_reason = "the matching case has no blocking check"

    return {
        "evaluator_version": EVALUATOR_VERSION,
        "status": status,
        "correctness": correctness,
        "correctness_reason": correctness_reason,
        # provisional unless a named human reviewed the facts themselves
        "correctness_provisional": correctness is not None and quality != "reviewed",
        "ungraded_reason": ungraded_reason,
        "reference": {"quality": quality, "case_id": case.id if case else None,
                      "match": "exact_normalized_question" if case else None},
        "checks": [c.to_dict() for c in checks],
        "scores": scores,
        "flags": flags,
        "retrieval": {"state": retrieval_state, "sources": retrieved,
                      "empty": (not retrieved) if retrieved is not None else None},
    }


def summarize(rows: list[dict]) -> dict:
    """Aggregate evaluation records. Each row: {conversation_id, status, correctness, reference_quality, flags}.

    Units are explicit: `turns` are assistant turns (a five-turn call is five turns, ONE conversation).
    Rates use graded turns only; ungraded and errored turns are reported but never enter a rate.
    A conversation is `all_correct` only if every turn that received a correctness verdict is correct.
    """
    turns = len(rows)
    by_status = Counter(r["status"] for r in rows)
    graded = [r for r in rows if r["status"] in ("passed", "failed")]
    verdicts = [r for r in rows if r.get("correctness") in CORRECTNESS]
    by_correctness = Counter(r["correctness"] for r in verdicts)
    flagged = sum(1 for r in rows if r.get("flags"))

    conversations: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        conversations[r["conversation_id"]].append(r)
    graded_conversations = [c for c in conversations.values() if any(t.get("correctness") in CORRECTNESS for t in c)]
    all_correct = sum(1 for c in graded_conversations
                      if all(t["correctness"] == "correct" for t in c if t.get("correctness") in CORRECTNESS))

    def rate(num, den):
        return num / den if den else None  # None, never 0, when nothing could be measured

    return {
        "turns": turns,
        "conversations": len(conversations),
        "counts": {"passed": by_status["passed"], "failed": by_status["failed"], "ungraded": by_status["ungraded"],
                   "error": by_status["error"], "skipped": by_status["skipped"], "graded": len(graded)},
        "counts_consistent": sum(by_status.values()) == turns and set(by_status) <= set(STATUSES),
        "pass_rate": {"value": rate(by_status["passed"], len(graded)), "n": len(graded), "n_unit": "turns"},
        "correctness": {c: {"count": by_correctness[c], "rate": rate(by_correctness[c], len(verdicts))} for c in CORRECTNESS},
        "correctness_n": {"n": len(verdicts), "n_unit": "turns",
                          "provisional_turns": sum(1 for r in verdicts if r.get("correctness_provisional"))},
        # denominator: turns that were actually evaluated (skipped turns are not part of the question)
        "ungraded_rate": {"value": rate(by_status["ungraded"], turns - by_status["skipped"]),
                          "n": turns - by_status["skipped"], "n_unit": "turns"},
        "conversation_correctness": {"graded_conversations": len(graded_conversations), "all_correct": all_correct,
                                     "rate": rate(all_correct, len(graded_conversations)), "n_unit": "conversations"},
        "flagged_turns": flagged,
    }
