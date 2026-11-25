"""Deterministic evaluators.

Every function here is a plain computation on strings, lists and numbers. None
of them call a model. Heuristic checks (abstention, plain text, brevity, contact
promises) are regular expressions and are documented as such in
docs/EVALUATION_PLAN.md; a pass means "no pattern fired", not "a human agrees".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from evals import metrics
from evals.schema import EvalCase

RETRIEVAL_KS = (1, 3, 4)

_ABSTAIN_PATTERNS = [
    re.compile(
        r"\b(don't|do not|doesn't|does not|can't|cannot|couldn't|unable to)\b[^.?!]{0,50}"
        r"\b(have|know|find|see|confirm|share|provide|verify|say)\b",
        re.I,
    ),
    re.compile(r"\bnot (sure|certain|able to confirm|aware)\b", re.I),
    re.compile(r"\b(no|isn't|is not|aren't|are not) (any )?(information|details|data)\b", re.I),
    re.compile(
        r"\b(pass|forward|share|refer|route|connect|check with|ask)\b[^.?!]{0,60}"
        r"\b(sales|team)\b",
        re.I,
    ),
]

_CONTACT_PROMISE = re.compile(
    r"\b(sales team|sales|team|someone|we|they)\b[^.?!]{0,30}"
    r"\b(will|'ll|shall|going to)\b[^.?!]{0,25}"
    r"\b(contact|call|reach|get in touch|be in touch|email|ring)\b",
    re.I,
)

_MARKDOWN = re.compile(r"(\*\*|__|`|^\s*#{1,6}\s|^\s*[-*•]\s|^\s*\d+[.)]\s)", re.M)
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(‘“])")


def normalize(text: str) -> str:
    cleaned = text.replace("’", "'").replace("‘", "'")
    cleaned = cleaned.replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", cleaned).strip().lower()


def _contains(haystack: str, needle: str) -> bool:
    """Case-insensitive match that does not fire inside a longer word or number."""
    pattern = r"(?<![a-z0-9])" + re.escape(normalize(needle)) + r"(?![a-z0-9])"
    return re.search(pattern, haystack) is not None


def fact_present(reply: str, fact: str) -> bool:
    """A fact is 'alt1|alt2|...'. It is present when any alternative appears."""
    text = normalize(reply)
    return any(_contains(text, alt) for alt in fact.split("|") if alt.strip())


def missing_facts(reply: str, facts: list[str]) -> list[str]:
    return [fact for fact in facts if not fact_present(reply, fact)]


def forbidden_hits(reply: str, forbidden_facts: list[str], forbidden_regex: list[str]) -> list[str]:
    hits = [fact for fact in forbidden_facts if fact_present(reply, fact)]
    text = normalize(reply)
    for pattern in forbidden_regex:
        if re.search(pattern, text, re.I):
            hits.append(f"/{pattern}/")
    return hits


def looks_like_abstention(reply: str) -> bool:
    return any(pattern.search(reply) for pattern in _ABSTAIN_PATTERNS)


def promises_contact(reply: str) -> bool:
    return _CONTACT_PROMISE.search(reply) is not None


def is_plain_text(reply: str) -> bool:
    return _MARKDOWN.search(reply) is None


def sentence_count(reply: str) -> int:
    parts = [p for p in _SENTENCE_BREAK.split(reply.strip()) if p.strip()]
    return len(parts)


@dataclass
class Check:
    name: str
    passed: bool | None  # None = not applicable
    detail: str = ""
    blocking: bool = True

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "blocking": self.blocking,
        }


def retrieval_scores(retrieved: list[str], relevant: list[str], ks=None) -> dict[str, float | None]:
    """Recall@K and Precision@K for each K, plus MRR. Relevance is binary: a heading is in
    `expected_sources` or it is not (no graded labels exist, so NDCG is not computed)."""
    scores: dict[str, float | None] = {}
    for k in (ks or RETRIEVAL_KS):
        scores[f"recall_at_{k}"] = metrics.recall_at_k(retrieved, relevant, k)
        scores[f"precision_at_{k}"] = metrics.precision_at_k(retrieved, relevant, k)
    scores["mrr"] = metrics.reciprocal_rank(retrieved, relevant)
    return scores


def evaluate_retrieval(case: EvalCase, retrieved: list[str], ks=None) -> tuple[dict, list[Check]]:
    """Scores and checks for one rag_answer case, from retrieved headings only."""
    scores: dict[str, float | None] = {}
    checks: list[Check] = []

    if case.expected_sources:
        scores.update(retrieval_scores(retrieved, case.expected_sources, ks))
        missing = [h for h in case.expected_sources if h not in retrieved]
        checks.append(
            Check(
                "retrieval_hit",
                not missing,
                "all expected sources retrieved"
                if not missing
                else f"missing: {', '.join(missing)}; retrieved: {retrieved or 'nothing'}",
            )
        )
        scores["empty_retrieval"] = 1.0 if not retrieved else 0.0
    else:
        # Nothing in the knowledge base answers this. Returning no chunks is the
        # ideal outcome of the distance gate, but unrelated chunks are not a
        # failure of the answer, so this stays informational.
        scores["empty_when_unanswerable"] = 1.0 if not retrieved else 0.0
        checks.append(
            Check(
                "retrieval_empty_when_unanswerable",
                not retrieved,
                "no chunks returned" if not retrieved else f"returned: {retrieved}",
                blocking=False,
            )
        )
    return scores, checks


def evaluate_reply(case: EvalCase, reply: str) -> tuple[dict, list[Check]]:
    scores: dict[str, float | None] = {}
    checks: list[Check] = []

    if case.required_facts:
        missing = missing_facts(reply, case.required_facts)
        found = len(case.required_facts) - len(missing)
        scores["required_facts"] = found / len(case.required_facts)
        checks.append(
            Check(
                "required_facts",
                not missing,
                "all required facts present" if not missing else f"missing: {missing}",
            )
        )

    if case.forbidden_facts or case.forbidden_regex:
        hits = forbidden_hits(reply, case.forbidden_facts, case.forbidden_regex)
        scores["forbidden_clear"] = 0.0 if hits else 1.0
        checks.append(
            Check("forbidden_clear", not hits, "none present" if not hits else f"present: {hits}")
        )

    if case.expect_abstain:
        abstained = looks_like_abstention(reply)
        scores["abstained"] = 1.0 if abstained else 0.0
        checks.append(
            Check(
                "abstains_when_unknown",
                abstained,
                "reply admits it lacks the information or hands over to sales"
                if abstained
                else "no lack-of-information or hand-over phrase found",
            )
        )

    plain = is_plain_text(reply)
    scores["plain_text"] = 1.0 if plain else 0.0
    checks.append(Check("plain_text", plain, "" if plain else "markdown found", blocking=False))

    sentences = sentence_count(reply)
    within = sentences <= 3
    scores["within_length"] = 1.0 if within else 0.0
    checks.append(
        Check("within_prompt_length", within, f"{sentences} sentence(s); prompt asks for 1-3", blocking=False)
    )
    return scores, checks


def evaluate_lead_gate(case: EvalCase) -> tuple[dict, list[Check], dict]:
    """Runs the real server-side consent gate on a scripted proposal. No DB, no LLM."""
    from api.leads import check_lead_proposal

    arguments = case.proposed_tool_call["arguments"]
    raw_arguments = arguments if isinstance(arguments, str) else json.dumps(arguments)
    lead_input, problem = check_lead_proposal(case.messages, raw_arguments)
    actual = "saved" if lead_input is not None else "rejected"
    correct = actual == case.expected_gate

    checks = [
        Check(
            "gate_decision",
            correct,
            f"expected {case.expected_gate}, got {actual}"
            + (f" ({problem})" if problem and actual == "rejected" else ""),
        )
    ]
    scores: dict[str, float | None] = {
        "gate_correct": 1.0 if correct else 0.0,
        # The dangerous direction: saving personal data without valid consent.
        "gate_false_save": 1.0 if (actual == "saved" and case.expected_gate == "rejected") else 0.0,
        "gate_false_reject": 1.0 if (actual == "rejected" and case.expected_gate == "saved") else 0.0,
    }
    return scores, checks, {"gate_status": actual, "gate_reason": problem}


def evaluate_lead_outcome(
    case: EvalCase,
    lead_status: str | None,
    lead_saved: bool,
    email_ok: bool | None,
    reply: str,
) -> tuple[dict, list[Check]]:
    """Scores the end state of a seeded conversation.

    lead_saved   a lead row exists for the conversation afterwards
    email_ok     the stored email equals expected_lead_email (None if not applicable)
    """
    saved = lead_status == "saved" and lead_saved
    expected = bool(case.expected_lead_saved)
    promise = promises_contact(reply)
    scores: dict[str, float | None] = {
        "lead_outcome_correct": 1.0 if saved == expected else 0.0,
        "premature_tool_call": 1.0 if (lead_status == "rejected" and not expected) else 0.0,
        "no_contact_promise": 0.0 if promise else 1.0,
        # The two business errors, kept apart because they cost different things.
        "false_lead_save": 1.0 if (saved and not expected) else 0.0,
        "missed_lead_save": 1.0 if (expected and not saved) else 0.0,
    }
    checks = [
        Check(
            "lead_outcome",
            saved == expected,
            f"expected saved={expected}; lead_status={lead_status!r}, lead stored={'yes' if lead_saved else 'no'}",
        )
    ]
    if expected and email_ok is not None:
        scores["lead_email_correct"] = 1.0 if email_ok else 0.0
        checks.append(
            Check("lead_email", email_ok, "stored email matches" if email_ok else "stored email differs")
        )
    checks.append(
        Check(
            "no_contact_promise",
            not promise,
            "" if not promise else "reply promises that sales will contact the visitor",
            blocking=False,
        )
    )
    return scores, checks
