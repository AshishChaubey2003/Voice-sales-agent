"""Connects evals.judges to the evaluation runner.

Two groups, kept apart on purpose:

* Deterministic evaluators (pii_leak, tool_selection, conciseness) always run when a
  reply exists. They cost nothing and never change a case's pass/fail: they add
  evidence next to it.
* LLM judges (faithfulness, hallucination, relevance, tone_language, safety,
  correctness, task_completion) run only when a JudgeSuite is supplied, which needs
  the explicit --judge-suite flag. A judge whose required input is missing is recorded
  as skipped_missing_input and never reaches the model (for example correctness
  without a human-reviewed reference, or task_completion without a stated goal).

Every outcome is a plain dict (evals.judges.EvalOutcome.to_dict) stored under
result["evaluators"], so a failure is visible next to the case that produced it.
"""

from __future__ import annotations

from evals import judges
from evals.judges import EvalOutcome
from evals.schema import EvalCase

DETERMINISTIC_VERSION = "1"
DETERMINISTIC_MODEL = "deterministic"
# PROVISIONAL: a spoken sales reply is expected to be short; not calibrated against listeners.
CONCISE_MAX_WORDS = 60
NO_CONTEXT = "(no knowledge-base context was retrieved for this question)"

LLM_JUDGES = ("faithfulness", "hallucination", "relevance", "tone_language", "safety", "correctness", "task_completion")


def _det(name: str, ok: bool, reason: str) -> dict:
    return EvalOutcome(
        evaluator=name, version=DETERMINISTIC_VERSION, model=DETERMINISTIC_MODEL, status="ok",
        verdict="pass" if ok else "fail", score=1.0 if ok else 0.0, reason=reason[:400],
    ).to_dict()


def _visitor_text(case: EvalCase) -> str:
    return "\n".join(m["content"] for m in case.messages if m["role"] == "user")


def deterministic_outcomes(case: EvalCase, output: dict) -> list[dict]:
    """Free checks on the reply and the tool decision. Empty when there is no reply to inspect."""
    if case.kind not in ("rag_answer", "lead_llm"):
        return []
    reply = output.get("reply")
    if not isinstance(reply, str) or not reply:
        return []
    from api.redact import sensitive_values

    out = []
    # A value the visitor typed themselves may be read back; anything else is a leak.
    leaked = judges.pii_leak(reply, allowed=tuple(sensitive_values(_visitor_text(case))))
    out.append(_det("pii_leak", not leaked["leaked"],
                    "no sensitive value beyond what the visitor supplied" if not leaked["leaked"]
                    else f"reply contains: {', '.join(leaked['kinds'])}"))

    if case.kind == "lead_llm":
        expected = ["save_lead"] if case.expected_lead_saved else []
    else:
        expected = []  # a factual question should never trigger the lead tool
    actual = ["save_lead"] if output.get("lead_status") else []
    selection = judges.tool_selection(expected, actual)
    out.append(_det("tool_selection", selection["correct"],
                    "tool use as expected" if selection["correct"]
                    else f"missing: {selection['missing'] or '-'}; unexpected: {selection['unexpected'] or '-'}"))

    concise = judges.conciseness_check(reply, CONCISE_MAX_WORDS)
    out.append(_det("conciseness", concise["within_limit"],
                    f"{concise['words']} words (provisional limit {concise['max_words']})"))
    return out


def _reference(case: EvalCase) -> str | None:
    """A reference answer exists only where a named human has reviewed the required facts themselves.

    required_facts are keywords the dataset author wrote. A case passing the review workflow
    (review_status "reviewed") means reviewers checked the evidence sections and the expected behaviour,
    not these keywords, so that alone is NOT enough: facts_review_status must be "reviewed" too.
    Without it the correctness judge is skipped (skipped_missing_input) and never sees a guess.
    """
    if case.review_status != "reviewed" or case.facts_review_status != "reviewed" or not case.required_facts:
        return None
    facts = [" or ".join(p.strip() for p in fact.split("|")) for fact in case.required_facts]
    return "The answer should convey: " + "; ".join(facts)


def has_reviewed_reference(case: EvalCase) -> bool:
    """True only where correctness may be judged: the facts themselves were reviewed (see _reference)."""
    return _reference(case) is not None


def _transcript(case: EvalCase, reply: str) -> str:
    lines = [f"{m['role']}: {m['content']}" for m in case.messages]
    lines.append(f"assistant: {reply}")
    return "\n".join(lines)


def plan(case: EvalCase, output: dict) -> list[tuple[str, dict]]:
    """(judge name, inputs) for every LLM judge that applies to this case."""
    if case.kind != "rag_answer":
        return []
    reply = output.get("reply")
    if not isinstance(reply, str) or not reply:
        return []
    raw_context = output.get("context")
    # None = retrieved text unknown (a replay stores none): the context judges are skipped, not guessed.
    # [] = retrieval ran and returned nothing: say exactly that.
    context = None if raw_context is None else (raw_context or [NO_CONTEXT])
    base = {"question": case.query, "answer": reply}
    steps = [
        ("faithfulness", {**base, "context": context}),
        ("hallucination", {**base, "context": context}),
        ("relevance", dict(base)),
        ("tone_language", dict(base)),
        ("correctness", {**base, "reference": _reference(case)}),
    ]
    if case.category == "prompt_injection":
        steps.append(("safety", dict(base)))
    if len(case.messages) > 1:
        # No case states the visitor's goal, so this is recorded as skipped, not guessed.
        steps.append(("task_completion", {"conversation": _transcript(case, reply), "goal": None}))
    return steps


def call_count(case: EvalCase, output: dict) -> int:
    """Model calls the suite will make for this case (skipped judges cost nothing)."""
    total = 0
    for name, inputs in plan(case, output):
        spec = judges.SPECS[name]
        if all(judges._present(inputs.get(key)) for key in spec.needs):
            total += 1
    return total


def estimate_calls(case: EvalCase, *, has_context: bool) -> int:
    """Upper-bound for the pre-run plan, from the case alone."""
    if case.kind != "rag_answer":
        return 0
    probe = {"reply": "x"}
    if has_context:
        probe["context"] = ["x"]
    return call_count(case, probe)


class JudgeSuite:
    """Runs the applicable LLM judges for a case with one provider."""

    def __init__(self, provider, model_label: str) -> None:
        self._provider = provider
        self.model_label = model_label

    @property
    def budget_exhausted(self) -> bool:
        return bool(getattr(self._provider, "budget_exhausted", False))

    async def evaluate(self, case: EvalCase, output: dict) -> list[dict]:
        results = []
        for name, inputs in plan(case, output):
            outcome = await judges.run_judge(self._provider, name, inputs, model_label=self.model_label)
            results.append(outcome.to_dict())
        return results
