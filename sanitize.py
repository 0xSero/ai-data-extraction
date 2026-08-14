#!/usr/bin/env python3
"""
Local, offline redaction for enterprise-safe harness export.
Standard library only. No network calls.
"""

import re
import math
from collections import Counter

SUSPICIOUS_ENTROPY_THRESHOLD = 3.5

# (type, compiled pattern). Order matters: most specific first.
_SECRET_PATTERNS = [
    ("private_key", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL)),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b")),
    ("openai_anthropic_key", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9\-_]{20,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[0-9A-Za-z\-]{10,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b")),
]

# key-like-name = value  (or  "key": "value"). Redacts the value, keeps the key.
# Note: this pattern runs BEFORE secret-shape patterns to avoid double-redaction.
_ASSIGNMENT = re.compile(
    r"""(?ix)
    (                                   # group 1: the key + separator (kept)
      ["']?[a-z0-9_\-]*
      (?:password|passwd|secret|token|api[_-]?key|access[_-]?key|
         authorization|auth|bearer|credential|private[_-]?key)
      [a-z0-9_\-]*["']?
      \s* [:=] \s* ["']?
    )
    ([^\s"',}\n]{6,})                   # group 2: the value (redacted)
    """)

_HOME_PATH = re.compile(r"(?:/Users/|/home/|/root/|C:\\Users\\)[^\s\"',:;)\]}]*")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# IPv6: matches both full form and :: compressed forms, without mangling C++ scope resolution
_IPV6 = re.compile(
    r"\b(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}\b"
    r"|(?<![0-9A-Za-z_:.])"
    r"(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4})*)?::(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4})*)?"
    r"(?![0-9A-Za-z_:.])"
)

_TOKEN = re.compile(r"[A-Za-z0-9+/=_\-]{20,}")


def _shannon_entropy(s):
    if not s:
        return 0.0
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in Counter(s).values())


def find_suspicious_tokens(text):
    """High-entropy tokens that may be unredacted secrets (for manifest warnings)."""
    out = []
    for m in _TOKEN.finditer(text):
        tok = m.group(0)
        if _shannon_entropy(tok) >= SUSPICIOUS_ENTROPY_THRESHOLD:
            out.append(tok)
    return out


class Sanitizer:
    def __init__(self, use_detect_secrets=True):
        self._ds_scan = None
        if not use_detect_secrets:
            self.detect_secrets_status = "disabled"
        else:
            try:
                from detect_secrets.core.scan import scan_line
                self._ds_scan = scan_line
                self.detect_secrets_status = "used"
            except Exception:
                self.detect_secrets_status = "unavailable"

    def _detect_secrets_values(self, text):
        if not self._ds_scan:
            return []
        found = []
        try:
            for line in text.splitlines():
                for secret in self._ds_scan(line):
                    val = getattr(secret, "secret_value", None)
                    if val and len(val) >= 6:
                        found.append(val)
        except Exception:
            pass
        return found

    def scrub_text(self, text):
        if not isinstance(text, str) or not text:
            return text, {"secrets": 0, "paths": 0, "emails": 0, "ips": 0}

        counts = {"secrets": 0, "paths": 0, "emails": 0, "ips": 0}

        # 1. detect-secrets exact values first (best effort, optional)
        for val in self._detect_secrets_values(text):
            if val in text:
                text = text.replace(val, "[REDACTED_SECRET:detect_secrets]")
                counts["secrets"] += 1

        # 2. key = value assignments (runs before secret shapes to avoid double-redaction)
        text, n = _ASSIGNMENT.subn(r"\1[REDACTED_SECRET:assignment]", text)
        counts["secrets"] += n

        # 3. known secret shapes
        for kind, pat in _SECRET_PATTERNS:
            text, n = pat.subn("[REDACTED_SECRET:%s]" % kind, text)
            counts["secrets"] += n

        # 4. home / user paths (whole match replaced -> username stripped)
        text, n = _HOME_PATH.subn("[PATH]", text)
        counts["paths"] += n

        # 5. contacts
        text, n = _EMAIL.subn("[EMAIL]", text)
        counts["emails"] += n
        text, n = _IPV4.subn("[IP]", text)
        counts["ips"] += n
        text, n = _IPV6.subn("[IP]", text)
        counts["ips"] += n

        return text, counts
