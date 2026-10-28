"""Reviewer identity registry for the human review (read-only).

A sheet's file name is a label somebody typed, not evidence of who filled it. The review lead therefore
declares, in evals/review/reviewers.csv, which people exist and which sheet files each one filled in:

    reviewer_id,display_name,role,sheets,attested_by
    asha,Asha Verma,reviewer,asha-golden; asha-candidates,review lead
    ravi,Ravi Menon,reviewer,ravi-golden; ravi-candidates,review lead
    kiran,Kiran Das,resolver,,review lead

reviewer_id   the identity used everywhere (lower case letters, digits, . _ -, 2 to 40 characters)
display_name  the person's name as people know them; shown in reports, never used to match anyone
role          reviewer (fills sheets) or resolver (settles disputes; may not hold a sheet)
sheets        the sheet files this person filled in, without "sheet_" and ".csv" (separated by ;)
attested_by   who vouches that this is a distinct human (a name, never blank)

What the checks can and cannot show
  * They enforce that every counted sheet belongs to exactly one declared identity, that one identity
    never counts twice for a dataset, that a resolver is not also a reviewer, and that two identities do
    not carry the same display name.
  * They cannot prove that two different ids are different humans: that is the attestation of the person
    named in attested_by. Names are compared exactly (case and spacing ignored), never fuzzily.
No file is written, edited or created here. With no reviewers.csv the legacy rule (identity = file name
without -golden/-candidates) still applies, but readiness for validated metrics is then withheld.

Run standalone, before generating assignments, to check the registry on its own:

    python -m evals.review_identity [--review-dir evals/review] [--json]

Exit status: 0 when reviewers.csv is absent (nothing to validate yet) or present with no problems and at
least one declared resolver; 1 when it is present but has a problem, or has reviewers but no resolver
(disputes could not be adjudicated yet); this never blocks anything by itself, it only reports.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

REGISTRY_FILE = "reviewers.csv"
COLUMNS = ("reviewer_id", "display_name", "role", "sheets", "attested_by")
ROLES = ("reviewer", "resolver")
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,39}$")
_SPACES = re.compile(r"\s+")


def norm_sheet(name: str) -> str:
    """Sheet key as load_sheets names it: 'asha-golden' for sheet_asha-golden.csv (case ignored)."""
    key = (name or "").strip().lower()
    if key.endswith(".csv"):
        key = key[:-4]
    if key.startswith("sheet_"):
        key = key[len("sheet_"):]
    return key


def norm_name(name: str) -> str:
    return _SPACES.sub(" ", (name or "").strip()).casefold()


def _problem(problems: list[dict], code: str, message: str, **extra) -> None:
    problems.append({"code": code, "severity": "error", "message": message, **extra})


def load_registry(review_dir: Path) -> dict:
    """The declared identities. {"present", "entries", "sheet_owner", "problems"}; never raises, never writes."""
    path = Path(review_dir) / REGISTRY_FILE
    registry = {"present": path.exists(), "path": str(path), "entries": {}, "sheet_owner": {}, "problems": []}
    if not registry["present"]:
        return registry
    problems = registry["problems"]
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            fieldnames = [(f or "").strip() for f in (reader.fieldnames or [])]
            rows = list(reader)
    except (OSError, UnicodeDecodeError, csv.Error) as error:
        _problem(problems, "registry_unreadable", f"{REGISTRY_FILE} cannot be read ({type(error).__name__})")
        return registry
    absent = [c for c in COLUMNS if c not in fieldnames]
    if absent:
        hint = " It looks semicolon- or tab-separated; save as 'CSV UTF-8 (Comma delimited)'." \
            if len(fieldnames) == 1 and (";" in fieldnames[0] or "\t" in fieldnames[0]) else ""
        _problem(problems, "registry_header", f"{REGISTRY_FILE} is missing column(s): {', '.join(absent)}.{hint}")
        return registry

    def cell(row: dict, column: str) -> str:
        for key, value in row.items():
            if (key or "").strip() == column:
                return (value or "").strip()
        return ""

    seen_ids: dict[str, int] = {}
    for number, row in enumerate(rows, start=2):
        if not any((v or "").strip() for v in row.values()):
            continue
        rid, name, role = cell(row, "reviewer_id"), cell(row, "display_name"), cell(row, "role").lower()
        sheets = [norm_sheet(s) for s in cell(row, "sheets").split(";") if s.strip()]
        where = f"{REGISTRY_FILE} line {number}"
        if not rid:
            _problem(problems, "identity_missing", f"{where}: reviewer_id is blank", line=number)
            continue
        if not ID_PATTERN.match(rid):
            _problem(problems, "identity_malformed",
                     f"{where}: reviewer_id '{rid}' must be 2 to 40 lower-case letters, digits, '.', '_' or '-'",
                     reviewer_id=rid, line=number)
            continue
        if rid in seen_ids:
            _problem(problems, "identity_duplicate",
                     f"{where}: reviewer_id '{rid}' is already declared on line {seen_ids[rid]}; "
                     "one identity must have one row, so conflicting declarations are not merged", reviewer_id=rid, line=number)
            registry["entries"].pop(rid, None)
            continue
        seen_ids[rid] = number
        bad = [label for label, value in (("display_name", name), ("attested_by", cell(row, "attested_by"))) if not value]
        if role not in ROLES:
            bad.append(f"role (one of {'/'.join(ROLES)})")
        if bad:
            _problem(problems, "identity_incomplete", f"{where}: '{rid}' needs {', '.join(bad)}", reviewer_id=rid, line=number)
            continue
        if role == "resolver" and sheets:
            _problem(problems, "resolver_has_sheets",
                     f"{where}: '{rid}' is a resolver but lists sheet(s) {', '.join(sheets)}; a person who settles "
                     "disputes may not also review", reviewer_id=rid, line=number)
            continue
        registry["entries"][rid] = {"display_name": name, "role": role, "sheets": sheets,
                                    "attested_by": cell(row, "attested_by")}
    owners: dict[str, list[str]] = {}
    for rid, entry in registry["entries"].items():
        for key in entry["sheets"]:
            owners.setdefault(key, []).append(rid)
    for key, ids in sorted(owners.items()):
        if len(ids) > 1:
            _problem(problems, "sheet_claimed_twice",
                     f"sheet '{key}' is claimed by {', '.join(sorted(ids))}; a sheet has exactly one author", sheet=key)
        else:
            registry["sheet_owner"][key] = ids[0]
    by_name: dict[str, list[str]] = {}
    for rid, entry in registry["entries"].items():
        by_name.setdefault(norm_name(entry["display_name"]), []).append(rid)
    for ids in by_name.values():
        if len(ids) > 1:
            _problem(problems, "identity_ambiguous",
                     f"{', '.join(sorted(ids))} share the display name '{registry['entries'][ids[0]]['display_name']}'. "
                     "Independence cannot be established; make the names distinguish the people or merge the ids "
                     "if they are one person")
    return registry


def identity_of(registry: dict | None):
    """sheet name -> identity. Legacy file-name rule without a registry; None = unregistered under a registry."""
    from evals import review_status as rs
    if not registry or not registry.get("present"):
        return rs.reviewer_identity

    def lookup(sheet: str):
        return registry["sheet_owner"].get(norm_sheet(sheet))
    return lookup


def summarise(registry: dict | None, sheets_with_rows: list[str], min_reviewers: int = 2) -> dict:
    """Is who-reviewed-what established for these sheets? Plain dict for reports; read-only."""
    lookup = identity_of(registry)
    present = bool(registry and registry.get("present"))
    owner: dict[str, list[str]] = {}
    unregistered = []
    for sheet in sheets_with_rows:
        who = lookup(sheet)
        if who is None:
            unregistered.append(sheet)
        else:
            owner.setdefault(who, []).append(sheet)
    twice = {who: sorted(files) for who, files in owner.items() if len(files) > 1}
    reasons = []
    if not present:
        reasons.append(f"no {REGISTRY_FILE}: reviewer identity is only inferred from sheet file names, which is not evidence")
    else:
        reasons += [p["message"] for p in registry["problems"]]
        reasons += [f"sheet '{s}' is not listed in {REGISTRY_FILE}; its labels are not counted" for s in sorted(unregistered)]
    reasons += [f"reviewer '{w}' has more than one sheet for this dataset ({', '.join(f)}); they count once"
                for w, f in sorted(twice.items())]
    if len(owner) < min_reviewers:
        reasons.append(f"{len(owner)} distinct reviewer identity(ies) with a sheet; {min_reviewers} are required")
    return {"source": "registry" if present else "filename", "established": present and not reasons,
            "reviewers": sorted(owner), "unregistered_sheets": sorted(unregistered),
            "same_reviewer_twice": twice, "reasons": reasons}


def registry_report(review_dir: Path) -> dict:
    """Standalone registry check, independent of any sheet or dataset: {"present","problems","reviewers",
    "resolvers","resolver_available","ok"}. Used by the CLI and reusable from review_assign/preflight."""
    registry = load_registry(review_dir)
    reviewers = sorted(rid for rid, e in registry["entries"].items() if e["role"] == "reviewer")
    resolvers = sorted(rid for rid, e in registry["entries"].items() if e["role"] == "resolver")
    problems = [p["message"] for p in registry["problems"]]
    return {"present": registry["present"], "path": registry["path"], "problems": problems,
            "reviewers": reviewers, "resolvers": resolvers,
            "resolver_available": bool(resolvers),
            "ok": registry["present"] and not problems and bool(resolvers)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate evals/review/reviewers.csv on its own, read-only.")
    parser.add_argument("--review-dir", type=Path, default=Path("evals/review"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = registry_report(args.review_dir)
    if args.json:
        print(json.dumps(report, indent=2))
    elif not report["present"]:
        print(f"{report['path']}: does not exist yet. Required columns: {', '.join(COLUMNS)} "
              f"(role must be one of {'/'.join(ROLES)}). Nothing to assign or resolve until it is created.")
    else:
        print(f"{report['path']}: {len(report['reviewers'])} reviewer(s), {len(report['resolvers'])} resolver(s)")
        if report["reviewers"]:
            print(f"  reviewers: {', '.join(report['reviewers'])}")
        if report["resolvers"]:
            print(f"  resolvers: {', '.join(report['resolvers'])}")
        for message in report["problems"]:
            print(f"  problem: {message}")
        if not report["problems"] and report["reviewers"] and not report["resolvers"]:
            print("  note: no resolver is registered yet; a dispute between two reviewers cannot be adjudicated "
                  "until one is added (role=resolver, holding no sheet)")
    if not report["present"]:
        return 0
    needs_resolver = bool(report["reviewers"]) and not report["resolver_available"]
    return 1 if (report["problems"] or needs_resolver) else 0


if __name__ == "__main__":
    raise SystemExit(main())
