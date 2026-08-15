#!/usr/bin/env python3
"""
Local, offline redaction for enterprise-safe harness export.
Standard library only. No network calls.
"""

import ipaddress
import math
import re
from collections import Counter

SUSPICIOUS_ENTROPY_THRESHOLD = 3.5
MAX_JSON_DEPTH = 100

REDACTED = "[REDACTED_SECRET:%s]"


class DepthLimitExceeded(ValueError):
    """A JSON document nested deeper than MAX_JSON_DEPTH."""


# --- secret key names ----------------------------------------------------
# Matching is segment-aware: a key qualifies only when a whole `_`/`-`/camelCase
# segment (or an adjacent pair of them) is a secret word. Substring matching used
# to blank `author`, `oauth_provider` and `MAX_THINKING_TOKENS`.

_SECRET_WORDS = frozenset((
    "password", "passwd", "passphrase", "secret", "token", "authorization",
    "authentication", "credential", "auth", "bearer", "cookie", "apikey",
    "accesskey",
))

_SECRET_PAIRS = frozenset((
    "apikey", "accesskey", "privatekey", "secretkey", "clientsecret",
    "authtoken", "authkey", "authheader", "bearertoken", "sessionkey",
    "refreshtoken", "accesstoken", "signingkey", "encryptionkey",
    "sshkey", "deploykey", "masterkey", "subscriptionkey", "apisecret",
    "apitoken", "setcookie", "hostkey", "signingsecret", "webhooksecret",
    # Azure and GCP connection-string vocabulary. `accountkey` sat directly
    # beside `accesskey` and was simply missing, so the canonical Azure Storage
    # connection string passed through untouched.
    "accountkey", "sharedaccesskey", "sharedaccesssignature", "primarykey",
    "secondarykey", "connectionstring", "sastoken", "keydata", "privatekeyid",
))

# Matched against a single unsplittable run, so PGPASSWORD and AUTHTOKEN are
# caught. Suffix-only, and "auth" is deliberately absent: "author" starts with
# it and "oauth" ends with it, and blanking those was the original I5 defect.
_SECRET_SUFFIXES = tuple(sorted((
    "password", "passwd", "passphrase", "secret", "token", "credential",
    "apikey", "accesskey", "privatekey", "secretkey", "clientsecret",
    "authtoken", "sshkey", "deploykey", "masterkey", "apisecret",
), key=len, reverse=True))

# "tokens" next to any of these is a size, not a credential.
_COUNT_QUALIFIERS = frozenset((
    "max", "min", "input", "output", "cache", "thinking", "total", "num",
    "count", "budget", "limit", "used", "remaining", "per", "avg", "est",
    "estimated", "size", "length", "window", "usage", "spent", "prompt",
    "completion",
    # Durations: TOKEN_TTL_SECONDS is a lifetime, not a credential.
    "ttl", "expiry", "expires", "lifetime", "duration", "seconds", "minutes",
    "hours", "days", "ms", "interval", "age", "timeout",
))

_DOC_QUALIFIERS = frozenset((
    "code", "flow", "endpoint", "url", "uri", "doc", "docs", "guide",
    "example", "grant",
))

_SEGMENT_SPLIT = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")


def _key_segments(name):
    return [s.lower() for s in _SEGMENT_SPLIT.split(name.strip("\"'")) if s]


def _singular(seg):
    if len(seg) > 3 and seg.endswith("s") and not seg.endswith("ss"):
        return seg[:-1]
    return seg


# `pass`/`pwd` only count alongside another segment: bare PWD is the shell's
# working-directory variable, but DB_PASS, MYSQL_PWD and ODBC `Pwd=` are
# credentials.
_QUALIFIED_ONLY = frozenset(("pass", "pwd", "pat"))

# Segments that make a credential-ish word describe a credential rather than
# hold one: auth_mode, secret_name, cookie_samesite, key_algorithm.
_DESCRIPTOR_QUALIFIERS = frozenset((
    "mode", "name", "enabled", "disabled", "required", "optional", "provider",
    "issuer", "audience", "endpoint", "url", "uri", "domain", "backend",
    "algo", "algorithm", "hash", "samesite", "policy", "strategy", "kind",
    "format", "version", "scheme", "path", "file", "id", "rotation",
))

# Key names that introduce a group of settings rather than hold one credential.
_SECTION_WORDS = frozenset((
    "auth", "authorization", "authentication", "credential", "credentials",
    "secret", "secrets", "token", "tokens", "cookie", "cookies", "keys",
))


def _is_section_key(name):
    segs = [_singular(s) for s in _key_segments(name)]
    return bool(segs) and segs[-1] in {_singular(w) for w in _SECTION_WORDS}


def is_secret_key(name, json_key=False):
    """True when a key name denotes a credential.

    Matching is segment-aware so `author`, `oauth_provider` and
    `MAX_THINKING_TOKENS` are left alone, with a suffix test on unsplittable
    runs so `PGPASSWORD` and `AUTHTOKEN` are not missed.

    json_key relaxes the plural rule. `{"tokens": [...]}` in a settings file is
    a credential store, but `tokens = tokenizer.encode(x)` in a code block is
    not, and only the JSON walker can tell the difference.
    """
    if not isinstance(name, str):
        return False
    raw = _key_segments(name)
    if not raw:
        return False
    segs = [_singular(s) for s in raw]
    if (not json_key and len(raw) == 1 and raw[0] != segs[0]
            and not name.isupper()):
        # A bare lowercase plural in free text is usually an ordinary variable
        # (`tokens = tokenizer.encode(x)`). An all-caps one is an environment
        # variable (`CREDENTIALS=...`), which is config.
        return False
    if "token" in segs and any(s in _COUNT_QUALIFIERS for s in segs):
        return False
    # "authorization_code" is the OAuth grant type, not a credential.
    if "authorization" in segs and any(s in _DOC_QUALIFIERS for s in segs):
        return False
    # A descriptor segment means the key names or configures a credential
    # rather than holding one, so blanking its value destroys real config.
    if len(segs) > 1 and segs[-1] in _DESCRIPTOR_QUALIFIERS:
        return False
    if any(s in _SECRET_WORDS for s in segs):
        return True
    if len(segs) > 1 and any(s in _QUALIFIED_ONLY for s in segs):
        return True
    # ODBC/JDBC connection strings write `Pwd=` on its own. All-caps PWD is the
    # shell's working-directory variable, so title case is the discriminator.
    if (len(segs) == 1 and segs[0] in ("pwd", "pass")
            and name[:1].isupper() and not name.isupper()):
        return True
    if any(segs[i] + segs[i + 1] in _SECRET_PAIRS for i in range(len(segs) - 1)):
        return True
    if len(segs) == 1 and any(segs[0].endswith(suf) for suf in _SECRET_SUFFIXES):
        return True
    return "".join(segs) in _SECRET_PAIRS


# --- secret shapes -------------------------------------------------------
# Order matters: most specific first.
# The private-key body is a tempered, length-capped token. An unbounded `.*?`
# under DOTALL is quadratic when a document holds many BEGIN markers and no END:
# every BEGIN rescans to EOF. Tempering stops each attempt at the next BEGIN.

_PRIVATE_KEY_BODY = r"(?:(?!-----BEGIN )[\s\S]){0,20000}?"

# PGP blocks say "PGP PRIVATE KEY BLOCK", and PGP MESSAGE is ciphertext worth
# removing too, so the header word list cannot be just "PRIVATE KEY".
_KEY_HEADER = r"(?:[A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?|PGP MESSAGE|OPENSSH PRIVATE KEY)"

_SECRET_PATTERNS = [
    ("private_key", re.compile(
        r"-----BEGIN " + _KEY_HEADER + r"-----" + _PRIVATE_KEY_BODY +
        r"-----END " + _KEY_HEADER + r"-----")),
    # A BEGIN marker with no END still means key material is on the page.
    ("private_key_truncated", re.compile(
        r"-----BEGIN " + _KEY_HEADER + r"-----"
        r"(?:[ \t]*\r?\n[A-Za-z0-9+/=:. ]{8,}){1,}")),
    # `(?<![A-Za-z0-9])` rather than `\b`: `_` and `-` are word characters on
    # one side only, so `\b` never fires between `PREFIX_` and `AKIA...` and the
    # key escaped redaction. Letters and digits still bound the match.
    ("aws_access_key", re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}\b")),
    ("github_pat", re.compile(r"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{22,}\b")),
    ("github_token", re.compile(r"(?<![A-Za-z0-9])(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b")),
    ("openai_anthropic_key", re.compile(r"(?<![A-Za-z0-9])sk-(?:ant-)?[A-Za-z0-9\-_]{20,}\b")),
    ("google_api_key", re.compile(r"(?<![A-Za-z0-9])AIza[0-9A-Za-z\-_]{35}\b")),
    ("slack_token", re.compile(r"(?<![A-Za-z0-9])xox[baprs]-[0-9A-Za-z\-]{10,}\b")),
    ("slack_webhook", re.compile(
        r"https://hooks\.slack\.com/services/[A-Za-z0-9/+_\-]{10,}")),
    ("stripe_key", re.compile(
        r"(?<![A-Za-z0-9])(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{10,}\b")),
    ("gitlab_token", re.compile(r"(?<![A-Za-z0-9])glpat-[A-Za-z0-9_\-]{16,}\b")),
    ("npm_token", re.compile(r"(?<![A-Za-z0-9])npm_[A-Za-z0-9]{30,}\b")),
    ("digitalocean_token", re.compile(
        r"(?<![A-Za-z0-9])dop_v1_[a-f0-9]{40,}\b")),
    ("sendgrid_key", re.compile(
        r"(?<![A-Za-z0-9])SG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}\b")),
    ("twilio_key", re.compile(r"(?<![A-Za-z0-9])SK[a-f0-9]{32}\b")),
    ("jwt", re.compile(
        r"(?<![A-Za-z0-9])eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b")),
]

# The 40-char AWS secret access key has no distinctive prefix, so it is only
# redacted when the surrounding text gives AWS context. Redacting every 40-char
# base64 run unconditionally would eat hashes and content IDs.
_AWS_CONTEXT = re.compile(
    r"(?i)aws.{0,40}?secret|secret[_\- ]?access[_\- ]?key|\bAKIA[0-9A-Z]{16}\b")
_AWS_SECRET_BODY = re.compile(
    r"(?<![A-Za-z0-9/+=])[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])")

# Credentials in a URL userinfo section. Must run before the email pass, which
# would otherwise eat `pass@host.tld` and report it as an email, not a secret.
_URL_CRED = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/\s:@]*):([^/\s@]*)@")

# A single-field userinfo (`https://<token>@host`) is the GitHub/npm shape.
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/\s:@]{8,})@")

# Credentials handed over in a query string.
_URL_QUERY_CRED = re.compile(
    r"(?i)([?&](?:access_token|api_?key|auth|token|secret|password|passwd"
    r"|signature|sig|key)=)([^&\s\"'<>]+)")

# Credential-bearing CLI flags. `-p` is deliberately absent: `mkdir -p`,
# `docker run -p 8080:80`. `-u`/`--user` redact only the `user:pass` form, so
# `sort -u file` and `docker run -u 1000` are untouched.
# Any long flag whose name reads as a credential, rather than a fixed list of
# six spellings, so --auth-token / --client-secret / --registry-password are
# covered. Short flags stay an explicit list: most are ambiguous.
_CLI_CRED = re.compile(
    r"(?i)(?<![\w\-])(--[a-z0-9][a-z0-9\-]*|-u)([ =]+)(\S+)")

# Last-resort prose rule: `password is Xk9mQ2vL`, `.netrc` style
# `login bob password Hunter2Prod`. Guarded by _looks_like_secret_value so
# "the secret is important" survives.
# Longest alternatives first: "secret access key is X" must not match on bare
# "secret" and then measure the value as the word "access".
# The plural "s" stays inside the capture group: leaving it outside silently
# renamed the operator's identifiers by deleting it from the output.
_PROSE_SECRET = re.compile(
    r"(?i)\b(secret[ _\-]?access[ _\-]?keys?|access[ _\-]?keys?|api[ _\-]?keys?"
    r"|client[ _\-]?secrets?|private[ _\-]?keys?|secret[ _\-]?keys?"
    r"|passwords?|passphrases?|passwds?|secrets?|tokens?|credentials?)\b"
    # Bounded repetition, not `+`: an unbounded run backtracked quadratically
    # and a 100 KB whitespace pad stalled a default export for 42 seconds.
    r"([ \t]{1,40}(?:(?:is|was|=)[ \t]{1,40})?)"
    r"([^\s,;.'\"\n][^\s,;'\"\n]{3,})")

# A line whose text before the key is only structure (indent, list bullet,
# quote, `export`) is a config assignment, so the value runs to end of line.
# Anything else is prose, where consuming the rest of the line deletes
# documentation. This distinction is what keeps I10 without regressing I5.
# Only indentation, an optional `export`, and an optional opening quote. List
# bullets, blockquote markers and headings are prose structure: treating them as
# config made the value run to end of line and deleted 17 of 43 lines of a
# credential-free CLAUDE.md. Everything else falls to the prose branch, which
# still redacts but only a credential-shaped token.
# A leading `-`/`*` bullet is allowed because YAML sequences are config, but
# only when the key is the FIRST word after it. `#` headings and `>` blockquotes
# stay out: "### Token: what the field means" and "> ... and password policy."
# are prose, and treating them as config deleted the rest of the line.
_CONFIG_PREFIX = re.compile(
    r"""(?ix)\A[ \t]*(?:[-*][ \t]+)?(?:export|set|setx|env)?[ \t]*["']?\Z""")

_YAML_BLOCK = re.compile(r"\A[|>][+-]?\d*[ \t]*\Z")

_VALUE_STOP = re.compile(r"\s")

_LITERAL_VALUE = re.compile(
    r"(?i)[\"']?(?:true|false|null|none|nil|yes|no|undefined)[\"']?[ \t]*[,;)\]}]?[ \t]*(?:\n|\Z|#)")

# A second `KEY=` on the same line starts a sibling assignment, so the first
# value must not run through it.
_NEXT_ASSIGNMENT = re.compile(r"[ \t](?=[A-Za-z_][A-Za-z0-9_.\-]*[ \t]*=)")

# A line that is itself an assignment is a sibling key, not the value of the
# empty `KEY=` above it.
_LOOKS_LIKE_ASSIGNMENT = re.compile(r"\A[\s>*\#-]*[A-Za-z_][A-Za-z0-9_.\-]*[ \t]*[:=]")

# Markdown fences, headings, table rows, and closing brackets.
_STRUCTURAL_LINE = re.compile(r"\A(?:```|~~~|#{1,6}\s|\||[)\]}]|---|\*\*\*)")

# The leading lookbehind confines attempts to token starts. Without it,
# finditer retried inside every long identifier run, which is quadratic: a
# 32 KB single-line blob cost 7.5 s.
_ASSIGN_HEAD = re.compile(
    r"""(?x)
    (?<![A-Za-z0-9_.\-])
    (?P<key>["']?[A-Za-z_][A-Za-z0-9_.\-]*["']?)
    (?P<sep>[ \t]*[:=][ \t]*)
    """)

# No \A here: this is used with .match(text, pos), which already anchors at pos,
# whereas \A would pin the match to the start of the whole document.
_AUTH_SCHEME = re.compile(
    r"(?i)(?:Bearer|Basic|Token|Digest|JWT|ApiKey|Negotiate|NTLM)[ \t]+")

# Case-insensitive (macOS and Windows are), any drive letter, and a UNC share
# with only two components (\\server\user) as well as three.
_HOME_PATH = re.compile(
    r"(?i)(?:/Users/|/home/|/root/|/Volumes/[^/\s]+/|/mnt/[^/\s]+/"
    r"|[A-Z]:[\\/]Users[\\/]"
    r"|\\\\[^\\\s]+\\)"
    # The tail crosses a single space only when a path separator still follows,
    # so `/Users/First Last/repos/x for detail` consumes the name but stops at
    # the sentence. Without any crossing the surname shipped; without the
    # lookahead the rest of the sentence was eaten.
    r"(?:[^\s\"',:;)\]}`*<>]|[ ](?=[^\s\"',:;)\]}`*<>]*[/\\]))*")

# ~username as a path root. Not the bare ~/ shorthand (no identity), not
# ~~strikethrough~~, and not the "~approximately" idiom: a path separator is
# required after the name.
_TILDE_USER = re.compile(r"(?<![A-Za-z0-9_~])~([A-Za-z][A-Za-z0-9._\-]*)(?=[/\\])")

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
# Candidates only. Validated with ipaddress so a four-part version number like
# 1.2.3.4 is not reported as an address.
_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")

# IPv6 candidates are validated with the stdlib ipaddress module plus a
# disambiguation pass, because a bare `::` is both the unspecified address and a
# Python extended slice.
_IPV6_CANDIDATE = re.compile(
    r"(?<![0-9A-Za-z_.])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![0-9A-Za-z_.])")

# "=" only as trailing base64 padding. Allowing it mid-token made
# `DATABASE_URL=postgres` and `MAX_THINKING_TOKENS=31999` read as one long
# high-entropy run, which is most of what the old backstop reported.
_TOKEN = re.compile(r"[A-Za-z0-9+/_\-]{20,}={0,2}")

_LONE_SURROGATE = re.compile(r"[\ud800-\udfff]")

# Spans this tool itself emits, plus session UUIDs. Excluded from the entropy
# backstop so its warning count reflects real leftovers rather than our output.
_PLACEHOLDER = re.compile(r"\[(?:REDACTED_SECRET:[^\]\n]*|PATH|EMAIL|IP)\]")
_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")


def _shannon_entropy(s):
    if not s:
        return 0.0
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in Counter(s).values())


def _looks_like_secret_value(v):
    """Heuristic gate for the prose rule: credential-shaped, not an English word.

    Deliberately conservative. An earlier `not v.isalpha()` fallback matched
    anything containing punctuation, which turned ordinary markdown headings and
    links into redaction markers.
    """
    if len(v) < 6:
        return False
    if v.startswith("["):  # already one of our own placeholders
        return False
    if v.startswith(("http://", "https://", "./", "../", "/", "-", "`")):
        return False
    if "](" in v or "://" in v or "<" in v or ">" in v:
        return False
    # Credentials are dense runs of alphanumerics. Prose and markup are not.
    alnum = sum(c.isalnum() for c in v)
    if alnum / len(v) < 0.7:
        return False
    letters = sum(c.isalpha() for c in v)
    if any(c.isdigit() for c in v) and letters:
        return True
    if len(v) >= 10 and v != v.lower() and v != v.upper() and v.isalnum():
        return True
    # isalnum matters: without it, ordinary hyphenated English such as
    # "well-documented-approach" clears the entropy bar and gets deleted.
    return len(v) >= 16 and v.isalnum() and _shannon_entropy(v) >= 3.5


def _in_subscript(text, start):
    """True when the position sits inside `identifier[...]`, i.e. a slice."""
    line_start = text.rfind("\n", 0, start) + 1
    depth = 0
    i = start - 1
    while i >= line_start:
        ch = text[i]
        if ch == "]":
            depth += 1
        elif ch == "[":
            if depth == 0:
                prev = text[i - 1] if i > line_start else ""
                return bool(prev) and (prev.isalnum() or prev in "_)]")
            depth -= 1
        i -= 1
    return False


def _url_cred_sub(m):
    """Redact the password half, and the username half when it is itself a token.

    `https://<token>:@host` puts the credential in the username field with an
    empty password, which redacting only group 3 left untouched.
    """
    user, pw = m.group(2), m.group(3)
    if not pw or _looks_like_secret_value(user):
        user = REDACTED % "url_credential"
    return m.group(1) + user + ":" + (REDACTED % "url_credential") + "@"


def _is_ipv4(cand):
    try:
        ipaddress.IPv4Address(cand)
    except ValueError:
        return False
    return True


def _closing_quote(text, vstart, quote, line_end):
    """Index of the quote closing the one at vstart, honouring backslash escapes."""
    i = vstart + 1
    while i < line_end:
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == quote:
            return i
        i += 1
    return -1


def _block_value_spans(text, line_end, key_indent):
    """Spans covering an indented block value that follows `key:` on its own line."""
    spans = []
    pos = line_end
    first = True
    while pos < len(text):
        start = pos + 1
        if start >= len(text):
            break
        end = text.find("\n", start)
        if end == -1:
            end = len(text)
        line = text[start:end]
        if not line.strip():
            pos = end
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= key_indent:
            break
        if first:
            first = False
            # A nested mapping is a section, not this key's value. Its own
            # entries are matched independently, so swallowing the whole block
            # here erased structure that carried no credential.
            if _LOOKS_LIKE_ASSIGNMENT.match(line.lstrip("- ")):
                return []
        content = start + indent
        # Keep a YAML sequence marker so the document still parses.
        if text[content:content + 2] == "- ":
            content += 2
        if end > content:
            spans.append((content, end))
        pos = end
    return spans


def _flush_value_span(text, line_end):
    """Span for a value on the line after `KEY=`, when it is not another key."""
    start = line_end + 1
    if start >= len(text):
        return []
    end = text.find("\n", start)
    if end == -1:
        end = len(text)
    line = text[start:end]
    if not line.strip() or _LOOKS_LIKE_ASSIGNMENT.match(line):
        return []
    # A fence or a structural marker terminates the block; consuming it broke
    # the enclosing markdown.
    if _STRUCTURAL_LINE.match(line.strip()):
        return []
    return [(start + (len(line) - len(line.lstrip())), end)]


def _is_ipv6(cand, text, start, end):
    """True when a colon run is really an address, not a slice or a scope operator."""
    if cand == "::":
        return False
    try:
        ipaddress.ip_address(cand)
    except ValueError:
        return False
    # `arr[::2]` and `m[::2, ::3]` are extended slices, not addresses. A real
    # address in brackets (`http://[::1]:8080/`) is not subscripting anything.
    return not _in_subscript(text, start)


def find_suspicious_tokens(text):
    """High-entropy tokens that may be unredacted secrets (for manifest warnings)."""
    if not isinstance(text, str) or not text:
        return []
    masked = _PLACEHOLDER.sub(" ", text)
    masked = _UUID.sub(" ", masked)
    out = []
    for m in _TOKEN.finditer(masked):
        for tok in m.group(0).split("/"):
            # Score each path segment separately. Scoring the joined run made
            # ordinary markdown paths like `docs/reference/api-guide` read as
            # one high-entropy token, and on a real installation 55 of 58
            # warnings were exactly that, training operators to ignore the one
            # control the README tells them to read.
            if len(tok) < 20:
                continue
            if not any(c.isdigit() for c in tok) and (
                    tok == tok.lower() or tok == tok.upper()):
                continue
            if _shannon_entropy(tok) >= SUSPICIOUS_ENTROPY_THRESHOLD:
                out.append(tok)
    return out


class Sanitizer:
    def __init__(self, use_detect_secrets=True):
        self._ds_scan = None
        self.detect_secrets_errors = 0
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
        # Per line: one line that makes the scanner raise must not silently
        # disable it for the rest of the document.
        for line in text.splitlines():
            try:
                for secret in self._ds_scan(line):
                    val = getattr(secret, "secret_value", None)
                    if val and len(val) >= 6:
                        found.append(val)
            except Exception:
                self.detect_secrets_errors += 1
        return found

    def _redact_assignments(self, text):
        """Redact `key = value` where the key names a credential.

        Scans heads only, then measures the value span itself. A single regex
        with the value baked in would consume the rest of the line even for a
        non-secret key, hiding any real assignment later on that line.
        """
        # Spans this tool already emitted. `[REDACTED_SECRET:url_credential]`
        # otherwise re-parses as the assignment `SECRET: url_credential]` and
        # redacts the rest of the line a second time.
        protected = [(m.start(), m.end()) for m in _PLACEHOLDER.finditer(text)]

        def in_protected(i):
            return any(a <= i < b for a, b in protected)

        def starts_protected(i):
            return any(a == i for a, b in protected)

        spans = []
        pos = 0
        # Heads arrive in order, so the current line start is tracked forward
        # rather than rfind-ing from position 0 for every candidate, which made
        # a credential-dense document quadratic.
        line_start = 0
        scanned = 0
        for m in _ASSIGN_HEAD.finditer(text):
            nl = text.rfind("\n", scanned, m.start())
            if nl != -1:
                line_start = nl + 1
            scanned = m.start()
            if m.start() < pos or in_protected(m.start()):
                continue
            if not is_secret_key(m.group("key")):
                continue
            vstart = m.end()
            scheme = _AUTH_SCHEME.match(text, vstart)
            if scheme:
                vstart = scheme.end()
            line_end = text.find("\n", vstart)
            if line_end == -1:
                line_end = len(text)
            rest = text[vstart:line_end]

            # The shape pass already handled this value; leave its specific
            # label in place rather than overwriting it with a generic one.
            if starts_protected(vstart):
                continue
            # true/false/null are not credentials, and replacing them stops the
            # exported TOML or Python from parsing.
            if _LITERAL_VALUE.match(text, vstart):
                continue

            # Block form: `password:` or `api_key: |` with the value on the
            # following indented lines. Consuming only the current line left the
            # credential on the next one, under a redaction marker.
            if not rest.strip() or _YAML_BLOCK.match(rest.strip()):
                prefix = text[line_start:m.start()]
                key_indent = len(prefix) - len(prefix.lstrip())
                found = _block_value_spans(text, line_end, key_indent)
                if not found and m.group("sep").strip() == "=":
                    # Shell/env style `KEY=` with the value flush on the next
                    # line, so there is no deeper indent to key off.
                    found = _flush_value_span(text, line_end)
                for bstart, bend in found:
                    spans.append((bstart, bend, REDACTED % "assignment"))
                    pos = bend
                continue

            triple = text[vstart:vstart + 3]
            if triple in ('"""', "'''"):
                # A triple-quoted value measured as the empty string between the
                # first two quotes, emitting a marker while the body shipped.
                close = text.find(triple, vstart + 3)
                if close == -1:
                    continue
                spans.append((vstart + 3, close, REDACTED % "assignment"))
                pos = close
                continue

            quote = text[vstart] if text[vstart] in "\"'" else None
            if quote:
                close = _closing_quote(text, vstart, quote, line_end)
                if close == -1:
                    continue
                vend = close + 1
                repl = quote + (REDACTED % "assignment") + quote
            else:
                vend = line_end
                # A second `KEY=` on the same line is a sibling assignment, not
                # part of this value.
                sibling = _NEXT_ASSIGNMENT.search(text, vstart, vend)
                if sibling:
                    vend = sibling.start()
                # If the assignment sits inside an already-open quote, as in
                # `curl -H "Authorization: Bearer tok"`, end the value at that
                # quote rather than swallowing it and unbalancing the line.
                for q in "\"'":
                    if text.count(q, line_start, vstart) % 2 == 1:
                        close = text.find(q, vstart)
                        if close != -1 and close < vend:
                            vend = close
                # In prose, only the next whitespace-delimited token is the
                # value. Running to end of line here deleted sentences.
                # `=` alone is not enough to mean config: it also appears in
                # function signatures and shell commands, where consuming the
                # rest of the line truncated them.
                if not _CONFIG_PREFIX.match(text[line_start:m.start()]):
                    # Stop at whitespace only. Stopping at a comma as well left
                    # `pass,word,123` half-redacted.
                    stop = _VALUE_STOP.search(text, vstart, vend)
                    if stop:
                        vend = stop.start()
                    # In prose the key word may just be a noun ("the auth
                    # token: how it works"), so require the value to look like
                    # a credential before removing it.
                    if not _looks_like_secret_value(text[vstart:vend]):
                        continue
                if vend <= vstart:
                    continue
                repl = REDACTED % "assignment"
            spans.append((vstart, vend, repl))
            pos = vend
        if not spans:
            return text, 0
        # One pass into a list, not a whole-document re-slice per span.
        out = []
        cursor = 0
        for vstart, vend, repl in sorted(spans):
            if vstart < cursor:
                continue
            out.append(text[cursor:vstart])
            out.append(repl)
            cursor = vend
        out.append(text[cursor:])
        return "".join(out), len(spans)

    def _redact_cli_creds(self, text):
        count = 0

        def sub(m):
            nonlocal count
            flag, sep, val = m.group(1), m.group(2), m.group(3)
            # A long flag only qualifies when its own name reads as a credential.
            if flag.startswith("--") and flag.lower() not in ("--user",):
                if not is_secret_key(flag.lstrip("-")):
                    return m.group(0)
            if flag.lower() in ("-u", "--user"):
                # Only the `user:pass` form is a credential. `sort -u file`,
                # `docker run -u 1000` and `-u 1000:1000` must survive.
                user, sep2, pw = val.partition(":")
                if not sep2 or not _looks_like_secret_value(pw):
                    return m.group(0)
                count += 1
                return "%s%s%s:%s" % (flag, sep, user, REDACTED % "cli_flag")
            # `docker --secret my-resource-name` names a resource, not a value.
            if not _looks_like_secret_value(val):
                return m.group(0)
            count += 1
            return "%s%s%s" % (flag, sep, REDACTED % "cli_flag")

        return _CLI_CRED.sub(sub, text), count

    def _redact_prose(self, text):
        count = 0

        def sub(m):
            nonlocal count
            if not _looks_like_secret_value(m.group(3)):
                return m.group(0)
            count += 1
            return m.group(1) + m.group(2) + (REDACTED % "prose")

        return _PROSE_SECRET.sub(sub, text), count

    def _redact_ipv6(self, text):
        spans = []
        for m in _IPV6_CANDIDATE.finditer(text):
            if _is_ipv6(m.group(0), text, m.start(), m.end()):
                spans.append((m.start(), m.end()))
        for start, end in reversed(spans):
            text = text[:start] + "[IP]" + text[end:]
        return text, len(spans)

    def scrub_text(self, text):
        if not isinstance(text, str) or not text:
            return text, {"secrets": 0, "paths": 0, "emails": 0, "ips": 0}

        # json.loads turns a \udXXX escape into a real lone surrogate, which
        # cannot be encoded back to UTF-8. Neutralising it here, at the one
        # boundary every string crosses, keeps it out of both the hash and the
        # file write; a lone surrogate anywhere used to abort the whole export.
        if _LONE_SURROGATE.search(text):
            text = _LONE_SURROGATE.sub("�", text)

        counts = {"secrets": 0, "paths": 0, "emails": 0, "ips": 0}
        # Decided on the original text: the AKIA cue is itself redacted by the
        # shape pass below, which would otherwise erase its own context.
        aws_context = bool(_AWS_CONTEXT.search(text))

        # 1. detect-secrets exact values first (best effort, optional)
        for val in self._detect_secrets_values(text):
            if val in text:
                text = text.replace(val, REDACTED % "detect_secrets")
                counts["secrets"] += 1

        # 2. known secret shapes FIRST. Several are multi-line and anchored on a
        #    delimiter: `deploy_key: -----BEGIN OPENSSH PRIVATE KEY-----` used to
        #    have that BEGIN marker replaced by the assignment pass, after which
        #    the private-key pattern could no longer see the block and the body
        #    shipped in full. Naming a key made redaction strictly worse.
        #    The assignment pass below skips spans already marked, so running
        #    shapes first cannot double-redact.
        for kind, pat in _SECRET_PATTERNS:
            text, n = pat.subn(REDACTED % kind, text)
            counts["secrets"] += n

        # 3. AWS secret access key, only where AWS context is present
        if aws_context:
            text, n = _AWS_SECRET_BODY.subn(REDACTED % "aws_secret_key", text)
            counts["secrets"] += n

        # 4. key = value assignments
        text, n = self._redact_assignments(text)
        counts["secrets"] += n

        # 5. credential-bearing CLI flags
        text, n = self._redact_cli_creds(text)
        counts["secrets"] += n

        # 6. credentials in URL userinfo, before the email pass can eat them and
        #    report a password as an email.
        text, n = _URL_CRED.subn(_url_cred_sub, text)
        counts["secrets"] += n
        text, n = _URL_USERINFO.subn(
            lambda m: m.group(1) + (REDACTED % "url_credential") + "@", text)
        counts["secrets"] += n
        text, n = _URL_QUERY_CRED.subn(
            lambda m: m.group(1) + (REDACTED % "url_query_credential"), text)
        counts["secrets"] += n

        # 7. prose ("password is hunter2"), after every structured form
        text, n = self._redact_prose(text)
        counts["secrets"] += n

        # 8. home / user paths (whole match replaced -> username stripped)
        text, n = _HOME_PATH.subn("[PATH]", text)
        counts["paths"] += n
        text, n = _TILDE_USER.subn("[PATH]", text)
        counts["paths"] += n

        # 9. contacts
        text, n = _EMAIL.subn("[EMAIL]", text)
        counts["emails"] += n
        # Count only real replacements: subn's count included dotted quads that
        # failed validation and were left verbatim.
        ipv4_hits = 0

        def _sub_ipv4(m):
            nonlocal ipv4_hits
            if not _is_ipv4(m.group(0)):
                return m.group(0)
            ipv4_hits += 1
            return "[IP]"

        text = _IPV4.sub(_sub_ipv4, text)
        counts["ips"] += ipv4_hits
        text, n = self._redact_ipv6(text)
        counts["ips"] += n

        return text, counts

    def _merge(self, into, other):
        for k, v in other.items():
            into[k] = into.get(k, 0) + v

    def scrub_json(self, obj):
        """Sanitize a JSON-like object, keys included.

        Raises DepthLimitExceeded past MAX_JSON_DEPTH so callers can degrade to
        skip-with-warning instead of dying on an uncaught RecursionError.
        """
        counts = {"secrets": 0, "paths": 0, "emails": 0, "ips": 0}

        def walk_section(node, depth):
            """Keep the shape of a credential section, redact every leaf."""
            if depth > MAX_JSON_DEPTH:
                raise DepthLimitExceeded(
                    "JSON nested deeper than %d levels" % MAX_JSON_DEPTH)
            if isinstance(node, dict):
                out = {}
                for k, v in node.items():
                    safe_key = self.scrub_text(k)[0] if isinstance(k, str) else k
                    out[safe_key] = walk_section(v, depth + 1)
                return out
            if isinstance(node, list):
                return [walk_section(v, depth + 1) for v in node]
            if node is None or isinstance(node, bool):
                return node
            counts["secrets"] += 1
            return REDACTED % "key:section"

        def walk(node, key_name=None, depth=0, safe_name=None):
            if depth > MAX_JSON_DEPTH:
                raise DepthLimitExceeded(
                    "JSON nested deeper than %d levels" % MAX_JSON_DEPTH)
            if key_name is not None and is_secret_key(key_name, json_key=True):
                # Booleans and nulls cannot carry a credential, and blanking
                # them flips the JSON type for no benefit.
                if node is None or isinstance(node, bool):
                    return node
                # `auth` and `cookies` name a SECTION. Collapsing the container
                # erased its whole shape; recurse instead and redact the leaves,
                # so the structure survives and no value does.
                if isinstance(node, (dict, list)) and _is_section_key(key_name):
                    return walk_section(node, depth + 1)
                counts["secrets"] += 1
                # The marker names the ALREADY-SCRUBBED key. Interpolating the
                # raw key republished the home path it was scrubbed out of.
                return REDACTED % ("key:%s" % (safe_name if safe_name is not None
                                               else "redacted"))
            if isinstance(node, dict):
                out = {}
                taken = {}
                for k, v in node.items():
                    # Key names carry identity too: home paths and emails are
                    # routinely used as JSON keys.
                    if isinstance(k, str):
                        safe_key, c = self.scrub_text(k)
                        self._merge(counts, c)
                    else:
                        safe_key = k
                    if safe_key in out:
                        n = taken.get(safe_key, 1) + 1
                        candidate = "%s#%d" % (safe_key, n)
                        while candidate in out:
                            n += 1
                            candidate = "%s#%d" % (safe_key, n)
                        taken[safe_key] = n
                        safe_key = candidate
                    out[safe_key] = walk(v, k, depth + 1, safe_key)
                return out
            if isinstance(node, list):
                # argv form: ["--api-key", "<secret>"]. Each element reaches
                # scrub_text alone, so the flag and its value are never in the
                # same string and _CLI_CRED cannot see them. This is the
                # canonical way MCP servers take credentials.
                out = []
                redact_next = False
                for v in node:
                    if redact_next and isinstance(v, str):
                        counts["secrets"] += 1
                        out.append(REDACTED % "cli_flag")
                        redact_next = False
                        continue
                    redact_next = (isinstance(v, str) and v.startswith("-")
                                   and is_secret_key(v.lstrip("-")))
                    out.append(walk(v, key_name, depth + 1, safe_name))
                return out
            if isinstance(node, str):
                scrubbed, c = self.scrub_text(node)
                self._merge(counts, c)
                return scrubbed
            return node

        return walk(obj), counts
