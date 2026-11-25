"""Reading and writing evaluation runs on disk.

Layout (one directory per run, written atomically):

    evals/results/<run_id>/run.json      metadata, aggregate metrics, latency
    evals/results/<run_id>/cases.jsonl   one result per case
    evals/results/<run_id>/summary.md    readable report

A run belongs to one organization (run.json -> organization_id). Readers that
serve data to users must filter on it; see api/eval_routes.py.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path

RUN_ID_PATTERN = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")

# Values that must never reach a results file, a log or a dashboard.
_SECRET_PATTERNS = [
    re.compile(r"gsk_[A-Za-z0-9]{10,}"),  # Groq
    re.compile(r"lsv2_[A-Za-z0-9_]{10,}"),  # LangSmith
    re.compile(r"\bvsa_[A-Za-z0-9_\-]{20,}"),  # this project's org API keys
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"(?i)(postgres(?:ql)?(?:\+\w+)?://)[^@\s/]+@"),
]


def scrub(text: str) -> str:
    cleaned = text
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            cleaned = pattern.sub(lambda m: m.group(1) + "***@", cleaned)
        else:
            cleaned = pattern.sub("[redacted]", cleaned)
    return cleaned


def deep_scrub(value):
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, list):
        return [deep_scrub(v) for v in value]
    if isinstance(value, dict):
        return {k: deep_scrub(v) for k, v in value.items()}
    return value


def new_run_id(now: datetime | None = None) -> str:
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return f"{moment:%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"


def is_valid_run_id(run_id: str) -> bool:
    return bool(RUN_ID_PATTERN.match(run_id or ""))


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temp_name, path)
    except BaseException:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
        raise


def write_run(results_dir: str | Path, run: dict, cases: list[dict], summary_md: str) -> Path:
    run_id = run["run_id"]
    if not is_valid_run_id(run_id):
        raise ValueError("invalid run id")
    directory = Path(results_dir) / run_id
    directory.mkdir(parents=True, exist_ok=False)
    # Cases first, run.json last: a directory without run.json is an unfinished
    # write and is ignored by list_runs.
    _atomic_write(
        directory / "cases.jsonl",
        "".join(json.dumps(deep_scrub(c), ensure_ascii=False) + "\n" for c in cases),
    )
    _atomic_write(directory / "summary.md", scrub(summary_md))
    _atomic_write(
        directory / "run.json",
        json.dumps(deep_scrub(run), ensure_ascii=False, indent=2) + "\n",
    )
    return directory


def load_run(results_dir: str | Path, run_id: str) -> dict | None:
    if not is_valid_run_id(run_id):
        return None
    path = Path(results_dir) / run_id / "run.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def load_cases(results_dir: str | Path, run_id: str) -> list[dict]:
    if not is_valid_run_id(run_id):
        return []
    path = Path(results_dir) / run_id / "cases.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return []
    cases = []
    for line in lines:
        if line.strip():
            try:
                cases.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return cases


def list_runs(results_dir: str | Path, organization_id: str | None = None) -> list[dict]:
    """Newest first. When organization_id is given, only that organization's runs."""
    root = Path(results_dir)
    if not root.is_dir():
        return []
    runs = []
    for child in root.iterdir():
        if not child.is_dir() or not is_valid_run_id(child.name):
            continue
        run = load_run(root, child.name)
        if run is None:
            continue
        if organization_id is not None and run.get("organization_id") != organization_id:
            continue
        runs.append(run)
    runs.sort(key=lambda r: r.get("started_at", ""), reverse=True)
    return runs


# ---- baselines -------------------------------------------------------------

BASELINE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")


def baseline_path(baselines_dir: str | Path, name: str) -> Path:
    if not BASELINE_NAME_PATTERN.match(name):
        raise ValueError("baseline name may contain letters, digits, '_' and '-' only")
    return Path(baselines_dir) / f"{name}.json"


def save_baseline(baselines_dir: str | Path, name: str, run: dict) -> Path:
    """Stores the metadata and metrics of a run. Case text is not copied."""
    keep = {
        k: run[k]
        for k in (
            "schema_version",
            "run_id",
            "started_at",
            "status",
            "mode",
            "organization_id",
            "dataset",
            "model",
            "rag",
            "judge",
            "metrics",
            "latency",
            "counts",
        )
        if k in run
    }
    keep["baseline_name"] = name
    path = baseline_path(baselines_dir, name)
    _atomic_write(path, json.dumps(deep_scrub(keep), ensure_ascii=False, indent=2) + "\n")
    return path


def load_baseline(baselines_dir: str | Path, name: str) -> dict | None:
    try:
        return json.loads(baseline_path(baselines_dir, name).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return None
