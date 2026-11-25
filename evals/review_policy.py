"""The open product-owner decisions and whether each has been approved (read-only).

The tooling does not decide any of these. Each decision is OPEN until the product owner records it in
evals/review/policy_approvals.csv (written by a person; this module never creates or edits it):

    decision_id,approved_by,approved_on,reference
    C3,<owner name>,2026-11-02,<link or document where the decision is written down>

A row is accepted only when decision_id is one of the ids below, appears once, and approved_by, approved_on
(YYYY-MM-DD) and reference are all filled. An invalid row is reported and the decision stays open.

Recording an approval here changes NOTHING in evals/review_status.py's readiness computation today:
that module never imports this one and does not read policy_approvals.csv. For C1, C2 and C3 the strict,
conservative rule (a resolver must hold no sheet; resolution_evidence must be non-blank; identity must be
an established registry, never inferred) is already enforced unconditionally in review_status.py and
review_identity.py - approving these three only records that the product owner agrees the strict rule
IS the policy; it does not relax anything, and a dataset's state (ready / identity_unverified / etc.) is
identical whether or not the row exists. For A (lead-consent) and B (facts-verification) nothing is
coded yet at all, so approving either changes nothing either - a separate, reviewed code change (the
consent rule in api/leads.py, the facts-review stage) is still required first, and the report says
"approved, not implemented" rather than "done". In short: this module is a record for people to read
(surfaced by evals/review_preflight.py's `policy_open` findings), not a switch any other module checks.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

APPROVALS_FILE = "policy_approvals.csv"
COLUMNS = ("decision_id", "approved_by", "approved_on", "reference")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

DECISIONS = {
    "A": {
        "title": "Lead-consent rule for the golden lead and consent cases",
        "affects": "The lead_gate and lead_llm cases (what counts as permission to be contacted, what must be read back, "
                   "what is a clear yes, whether consent before the read-back counts, which languages).",
        "needs": "A written rule covering those five points, and a decision whether the gate in api/leads.py or the "
                 "expected labels change.",
        "blocks": ["labelling the lead and consent rows", "assigning them to reviewers", "lead.* metrics as validated",
                   "a golden dataset reaching ready (those rows stay unlabelled)"],
        "implemented": False,
    },
    "B": {
        "title": "Verification policy for author-written required_facts",
        "affects": "required_facts, forbidden_facts and forbidden_regex are the author's keywords; no reviewer sheet checks them.",
        "needs": "Who verifies (independence), what evidence per fact (a quoted sentence in a named heading), how many "
                 "verifiers, how fact disagreements are settled, and the threshold at which facts count as verified.",
        "blocks": ["marking any fact verified", "answer.required_facts, answer.forbidden_clear, run.pass_rate and "
                   "judge.correctness as validated", "correctness scoring against reference facts"],
        "implemented": False,
    },
    "C1": {
        "title": "May reviewers resolve their own disputes when no third person is available?",
        "affects": "Who may settle a dispute. The tooling currently accepts only a declared resolver who filled no sheet.",
        "needs": "Yes or no; if yes, under what recorded conditions.",
        "blocks": ["treating any adjudicated dispute as authoritative", "a dataset with adjudicated cases reaching ready"],
        "implemented": True,
    },
    "C2": {
        "title": "What counts as sufficient resolution evidence?",
        "affects": "The resolution_evidence column, which is currently only required to be non-blank.",
        "needs": "The minimum content (heading only, quoted sentence, second signature) and who checks it.",
        "blocks": ["treating any adjudicated dispute as authoritative", "a dataset with adjudicated cases reaching ready"],
        "implemented": True,
    },
    "C3": {
        "title": "Who may attest that reviewer ids are distinct people?",
        "affects": "attested_by in reviewers.csv. There is no identity system; attestation is a person's word.",
        "needs": "Who may attest, and whether a second person must co-sign.",
        "blocks": ["any dataset reaching ready (independence rests on the attestation)"],
        "implemented": True,
    },
}


def load_approvals(review_dir: Path) -> dict:
    """{"present", "approved": {decision_id: row}, "problems": [str]}. Never raises, never writes."""
    path = Path(review_dir) / APPROVALS_FILE
    out = {"present": path.exists(), "approved": {}, "problems": []}
    if not out["present"]:
        return out
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            names = [(f or "").strip() for f in (reader.fieldnames or [])]
            rows = list(reader)
    except (OSError, UnicodeDecodeError, csv.Error) as error:
        out["problems"].append(f"{APPROVALS_FILE} cannot be read ({type(error).__name__}); every decision stays open")
        return out
    absent = [c for c in COLUMNS if c not in names]
    if absent:
        out["problems"].append(f"{APPROVALS_FILE} is missing column(s): {', '.join(absent)}; every decision stays open")
        return out
    seen: dict[str, int] = {}
    for number, row in enumerate(rows, start=2):
        if not any((v or "").strip() for v in row.values()):
            continue
        cell = {c: (row.get(c) or "").strip() for c in COLUMNS}
        where = f"{APPROVALS_FILE} line {number}"
        if cell["decision_id"] not in DECISIONS:
            out["problems"].append(f"{where}: '{cell['decision_id']}' is not a decision id ({', '.join(DECISIONS)})")
            continue
        if cell["decision_id"] in seen:
            out["problems"].append(f"{where}: {cell['decision_id']} is already recorded on line {seen[cell['decision_id']]}; "
                                   "conflicting approvals are not merged, the decision stays open")
            out["approved"].pop(cell["decision_id"], None)
            continue
        seen[cell["decision_id"]] = number
        blank = [c for c in COLUMNS[1:] if not cell[c]]
        if blank:
            out["problems"].append(f"{where}: {cell['decision_id']} needs {', '.join(blank)}; the decision stays open")
        elif not _DATE.match(cell["approved_on"]):
            out["problems"].append(f"{where}: approved_on must be YYYY-MM-DD; the decision stays open")
        else:
            out["approved"][cell["decision_id"]] = cell
    return out


def status(review_dir: Path) -> dict:
    """Every decision with its state. approved + not implemented is reported as such, never as 'done'."""
    approvals = load_approvals(review_dir)
    decisions = []
    for decision_id, info in DECISIONS.items():
        approved = decision_id in approvals["approved"]
        decisions.append({"id": decision_id, "title": info["title"], "status": "approved" if approved else "open",
                          "implemented": bool(info["implemented"]) or None if approved else False,
                          "needs_code_change": approved and not info["implemented"],
                          "affects": info["affects"], "needs": info["needs"], "blocks": info["blocks"]})
    return {"file_present": approvals["present"], "problems": approvals["problems"], "decisions": decisions,
            "open": [d["id"] for d in decisions if d["status"] == "open"]}


def approved(review_dir: Path, decision_id: str) -> bool:
    return decision_id in load_approvals(review_dir)["approved"]
