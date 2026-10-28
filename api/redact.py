"""Masks values that must never leave the service in a trace, log or judge prompt.

Pattern-based, so it is a safety net and not a guarantee: names, addresses and free-form
personal details are not detectable this way. Prefer sending counts instead of text.
"""

from __future__ import annotations

import re

_PATTERNS = [
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")),
    ("SECRET", re.compile(r"\b(?:sk|gsk|lsv2_pt|lsv2_sk|ghp|xox[abp])[-_][A-Za-z0-9_\-]{12,}")),
    ("SECRET", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}")),
    ("SECRET", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}")),
    ("DBURL", re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:[^\s@]+@[^\s]+")),
    ("PHONE", re.compile(r"(?<![\w.])\+?\d[\d\s().\-]{8,}\d(?!\w)")),
]


def find_sensitive(text: str) -> list[str]:
    """Kinds of sensitive values present (never the values themselves)."""
    if not isinstance(text, str):
        return []
    kinds = []
    for kind, pattern in _PATTERNS:
        if pattern.search(text) and kind not in kinds:
            kinds.append(kind)
    return kinds


def sensitive_values(text: str) -> list[str]:
    """The sensitive substrings themselves. Only for in-process comparison (for example
    'did the reply echo something the visitor did not say'); never log or store the result."""
    if not isinstance(text, str):
        return []
    found: list[str] = []
    for _kind, pattern in _PATTERNS:
        found.extend(match.group(0) for match in pattern.finditer(text))
    return found


def scrub(text):
    """Returns `text` with sensitive values replaced by [KIND]. Non-strings pass through."""
    if not isinstance(text, str):
        return text
    for kind, pattern in _PATTERNS:
        text = pattern.sub(f"[{kind}]", text)
    return text


def scrub_messages(messages):
    """Scrubs the 'content' of chat-style message dicts, leaving structure untouched."""
    out = []
    for message in messages or []:
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            out.append({**message, "content": scrub(message["content"])})
        else:
            out.append(message)
    return out
