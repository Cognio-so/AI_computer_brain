from __future__ import annotations

import re

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai-style key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("aws access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("brain agent key", re.compile(r"\bsb_[a-z0-9-]+_[a-z0-9-]+_[a-z0-9-]+_[A-Za-z0-9]{20,}\b")),
    ("brain master key", re.compile(r"\bbm_[A-Za-z0-9_-]{30,}\b")),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("stripe key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    (
        "credential assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|password|passwd|token)\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-/+=]{16,}"
        ),
    ),
    ("connection string with password", re.compile(r"(?i)\b[a-z+]+://[^\s:/]+:[^\s@/]{6,}@[^\s]+")),
)


def find_secret(text: str) -> str | None:
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            return label
    return None


def clamp_summary(summary: str, limit: int) -> str:
    collapsed = " ".join(summary.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"
