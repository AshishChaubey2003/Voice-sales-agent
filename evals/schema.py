"""Golden dataset format and validation.

One JSON object per line (JSONL). Validation is strict on purpose: a typo in a
field name must fail loudly instead of silently disabling a check.

Kinds
-----
rag_answer  A visitor message answered from the knowledge base. Scored on
            retrieval (needs expected_sources) and on the reply text.
lead_gate   A scripted save_lead proposal plus conversation history. Scored on
            whether the server-side consent gate saves or rejects it. Pure
            code, no LLM, no database.
lead_llm    A seeded conversation where the model decides whether to call
            save_lead. Scored on the resulting lead outcome. Needs a live LLM.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

KINDS = ("rag_answer", "lead_gate", "lead_llm")
CATEGORIES = (
    "factual",
    "multi_fact",
    "follow_up",
    "unanswerable",
    "hallucination_trap",
    "prompt_injection",
    "out_of_scope",
    "edge",
    "lead_gate",
    "lead_llm",
)
REVIEW_STATUSES = ("unreviewed", "reviewed", "excluded")
# Whether a human has independently checked required_facts / forbidden_facts against the knowledge files.
# Separate from review_status on purpose: the reviewer sheets validate which sections answer a question and
# what a good reply does, NOT the keyword lists, so a "reviewed" case does not have trustworthy facts.
FACTS_REVIEW_STATUSES = ("unreviewed", "reviewed")
ROLES = ("user", "assistant")

_ALLOWED_FIELDS = {
    "id",
    "kind",
    "category",
    "messages",
    "expected_sources",
    "required_facts",
    "forbidden_facts",
    "forbidden_regex",
    "expect_abstain",
    "proposed_tool_call",
    "expected_gate",
    "expected_lead_saved",
    "expected_lead_email",
    "review_status",
    "reviewer",
    "facts_review_status",
    "facts_reviewer",
    "known_gap",
    "notes",
}


class DatasetError(ValueError):
    """The dataset file is malformed. The message names the line and field."""


@dataclass(frozen=True)
class EvalCase:
    id: str
    kind: str
    category: str
    messages: list[dict]
    expected_sources: list[str] = field(default_factory=list)
    required_facts: list[str] = field(default_factory=list)
    forbidden_facts: list[str] = field(default_factory=list)
    forbidden_regex: list[str] = field(default_factory=list)
    expect_abstain: bool = False
    proposed_tool_call: dict | None = None
    expected_gate: str | None = None
    expected_lead_saved: bool | None = None
    expected_lead_email: str | None = None
    review_status: str = "unreviewed"
    reviewer: str | None = None
    facts_review_status: str = "unreviewed"
    facts_reviewer: str | None = None
    known_gap: str | None = None
    notes: str | None = None

    @property
    def query(self) -> str:
        return self.messages[-1]["content"]


def _fail(line_no: int, case_id: str | None, message: str) -> DatasetError:
    where = f"line {line_no}" + (f" (case {case_id})" if case_id else "")
    return DatasetError(f"{where}: {message}")


def _string_list(raw: dict, key: str, line_no: int, case_id: str | None) -> list[str]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise _fail(line_no, case_id, f"'{key}' must be a list of non-empty strings")
    return list(value)


def parse_case(raw: object, line_no: int = 0) -> EvalCase:
    if not isinstance(raw, dict):
        raise _fail(line_no, None, "each line must be a JSON object")
    case_id = raw.get("id") if isinstance(raw.get("id"), str) else None

    unknown = sorted(set(raw) - _ALLOWED_FIELDS)
    if unknown:
        raise _fail(line_no, case_id, f"unknown field(s): {', '.join(unknown)}")
    if not case_id or not case_id.strip():
        raise _fail(line_no, None, "'id' is required")

    kind = raw.get("kind")
    if kind not in KINDS:
        raise _fail(line_no, case_id, f"'kind' must be one of {', '.join(KINDS)}")
    category = raw.get("category")
    if category not in CATEGORIES:
        raise _fail(line_no, case_id, f"'category' must be one of {', '.join(CATEGORIES)}")

    messages = raw.get("messages")
    if not isinstance(messages, list) or not messages:
        raise _fail(line_no, case_id, "'messages' must be a non-empty list")
    for message in messages:
        if (
            not isinstance(message, dict)
            or message.get("role") not in ROLES
            or not isinstance(message.get("content"), str)
            or not message["content"].strip()
        ):
            raise _fail(
                line_no,
                case_id,
                "each message needs role 'user'/'assistant' and non-empty string content",
            )
    if kind in ("rag_answer", "lead_llm") and messages[-1]["role"] != "user":
        raise _fail(line_no, case_id, "the last message must be from the user")

    review_status = raw.get("review_status", "unreviewed")
    if review_status not in REVIEW_STATUSES:
        raise _fail(line_no, case_id, f"'review_status' must be one of {', '.join(REVIEW_STATUSES)}")
    reviewer = raw.get("reviewer")
    if review_status in ("reviewed", "excluded") and not (isinstance(reviewer, str) and reviewer.strip()):
        # A "reviewed" or "excluded" flag without a named reviewer is exactly the fabricated
        # human-review status the dataset must never contain. "excluded" is a deliberate decision
        # by named reviewers to keep the case out of validated metrics; evals.review_status
        # additionally requires two reviewer sheets that agree before a dataset counts as ready.
        raise _fail(line_no, case_id, f"review_status '{review_status}' requires a named 'reviewer'")
    if review_status == "unreviewed" and reviewer:
        raise _fail(line_no, case_id, "an unreviewed case must not name a reviewer")

    facts_status = raw.get("facts_review_status", "unreviewed")
    if facts_status not in FACTS_REVIEW_STATUSES:
        raise _fail(line_no, case_id, f"'facts_review_status' must be one of {', '.join(FACTS_REVIEW_STATUSES)}")
    facts_reviewer = raw.get("facts_reviewer")
    if facts_status == "reviewed" and not (isinstance(facts_reviewer, str) and facts_reviewer.strip()):
        raise _fail(line_no, case_id, "facts_review_status 'reviewed' requires a named 'facts_reviewer'")
    if facts_status == "unreviewed" and facts_reviewer:
        raise _fail(line_no, case_id, "a case whose facts are unreviewed must not name a facts_reviewer")

    expected_sources = _string_list(raw, "expected_sources", line_no, case_id)
    required_facts = _string_list(raw, "required_facts", line_no, case_id)
    forbidden_facts = _string_list(raw, "forbidden_facts", line_no, case_id)
    forbidden_regex = _string_list(raw, "forbidden_regex", line_no, case_id)

    proposed = raw.get("proposed_tool_call")
    expected_gate = raw.get("expected_gate")
    expected_lead_saved = raw.get("expected_lead_saved")
    if kind == "lead_gate":
        if not isinstance(proposed, dict) or "arguments" not in proposed:
            raise _fail(line_no, case_id, "lead_gate needs 'proposed_tool_call' with 'arguments'")
        if not isinstance(proposed["arguments"], (dict, str)):
            raise _fail(line_no, case_id, "'arguments' must be an object or a raw JSON string")
        if expected_gate not in ("saved", "rejected"):
            raise _fail(line_no, case_id, "lead_gate needs 'expected_gate' saved|rejected")
    if kind == "lead_llm" and not isinstance(expected_lead_saved, bool):
        raise _fail(line_no, case_id, "lead_llm needs boolean 'expected_lead_saved'")
    if kind == "rag_answer" and not (required_facts or forbidden_facts or forbidden_regex or raw.get("expect_abstain") or expected_sources):
        raise _fail(line_no, case_id, "rag_answer needs at least one expectation")

    # expected_lead_email is structural bookkeeping only (it tells evaluate_lead_outcome() which stored
    # email to compare against), never a consent-policy decision. A lead_llm case that expects the lead
    # saved needs it, or evaluate_lead_outcome() silently skips lead_email_correct for that case; a case
    # that expects no save, or a case of any other kind, must not carry one, since nothing reads it there
    # and a value would misleadingly suggest the case checks an email it does not.
    expected_lead_email = raw.get("expected_lead_email")
    if expected_lead_email is not None and not (isinstance(expected_lead_email, str) and expected_lead_email.strip()):
        raise _fail(line_no, case_id, "'expected_lead_email' must be a non-empty string when given")
    if kind == "lead_llm":
        if expected_lead_saved is True and not expected_lead_email:
            raise _fail(line_no, case_id, "lead_llm with expected_lead_saved=true needs 'expected_lead_email'")
        if expected_lead_saved is False and expected_lead_email:
            raise _fail(line_no, case_id,
                        "lead_llm with expected_lead_saved=false must not set 'expected_lead_email'")
    elif expected_lead_email:
        raise _fail(line_no, case_id, "'expected_lead_email' is only meaningful for kind 'lead_llm'")

    expect_abstain = raw.get("expect_abstain", False)
    if not isinstance(expect_abstain, bool):
        raise _fail(line_no, case_id, "'expect_abstain' must be true or false")

    return EvalCase(
        id=case_id,
        kind=kind,
        category=category,
        messages=[{"role": m["role"], "content": m["content"]} for m in messages],
        expected_sources=expected_sources,
        required_facts=required_facts,
        forbidden_facts=forbidden_facts,
        forbidden_regex=forbidden_regex,
        expect_abstain=expect_abstain,
        proposed_tool_call=proposed if kind == "lead_gate" else None,
        expected_gate=expected_gate if kind == "lead_gate" else None,
        expected_lead_saved=expected_lead_saved if kind == "lead_llm" else None,
        expected_lead_email=expected_lead_email if kind == "lead_llm" else None,
        review_status=review_status,
        reviewer=reviewer if review_status in ("reviewed", "excluded") else None,
        facts_review_status=facts_status,
        facts_reviewer=facts_reviewer if facts_status == "reviewed" else None,
        known_gap=raw.get("known_gap"),
        notes=raw.get("notes"),
    )


@dataclass(frozen=True)
class Dataset:
    name: str
    path: str
    sha256: str
    cases: list[EvalCase]

    @property
    def reviewed_count(self) -> int:
        return sum(1 for case in self.cases if case.review_status == "reviewed")

    @property
    def excluded_count(self) -> int:
        return sum(1 for case in self.cases if case.review_status == "excluded")

    @property
    def facts_cases(self) -> int:
        """Cases that carry author-written required/forbidden facts and are not excluded."""
        return sum(1 for case in self.cases if case.review_status != "excluded"
                   and (case.required_facts or case.forbidden_facts or case.forbidden_regex))

    @property
    def facts_reviewed_count(self) -> int:
        return sum(1 for case in self.cases if case.review_status != "excluded"
                   and (case.required_facts or case.forbidden_facts or case.forbidden_regex)
                   and case.facts_review_status == "reviewed")

    def describe(self) -> dict:
        return {
            "name": self.name,
            "sha256": self.sha256,
            "n_cases": len(self.cases),
            "reviewed_cases": self.reviewed_count,
            "excluded_cases": self.excluded_count,
            "unreviewed_cases": len(self.cases) - self.reviewed_count - self.excluded_count,
            "facts_cases": self.facts_cases,
            "facts_reviewed_cases": self.facts_reviewed_count,
        }


def load_dataset(path: str | Path) -> Dataset:
    file_path = Path(path)
    raw_bytes = file_path.read_bytes()
    # Hash with LF endings so the same dataset has the same digest on Windows
    # (autocrlf) and Linux checkouts.
    digest = hashlib.sha256(raw_bytes.replace(b"\r\n", b"\n")).hexdigest()

    cases: list[EvalCase] = []
    seen: set[str] = set()
    for line_no, line in enumerate(raw_bytes.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as error:
            raise _fail(line_no, None, f"invalid JSON ({error.msg})") from None
        case = parse_case(raw, line_no)
        if case.id in seen:
            raise _fail(line_no, case.id, "duplicate case id")
        seen.add(case.id)
        cases.append(case)

    if not cases:
        raise DatasetError(f"{file_path}: dataset has no cases")
    return Dataset(name=file_path.stem, path=file_path.as_posix(), sha256=digest, cases=cases)
