#!/usr/bin/env python3
"""
Extract Google's Antigravity (Gemini 3) app conversations.

Antigravity stores conversation *bodies* encrypted at rest
(~/.gemini/antigravity/conversations/<uuid>.pb, AES w/ nonce prefix).
Rather than cracking AES, this script reuses Antigravity's own local
`language_server` HTTP API (which decrypts + deserializes the .pb files) —
the approach of https://github.com/Arlowen/antigravity-decryptor and
https://github.com/neo1027144-creator/antigravity-history.

Extraction sources, in order:
  1. Live LanguageServer API (if a server is running or can be launched):
     full step-by-step messages incl. thinking chains, code diffs, command
     outputs, timestamps, model info (ported step parser).
  2. Plaintext protobuf summary index (agyhub_summaries_proto.pb) fallback:
     title, message count, timestamps, workspace, git branch/remotes,
     agent/project UUIDs.
  3. Plaintext brain artifacts (markdown plans + metadata), annotations
     (*.pbtxt), project configs, and implicit-trace listings.

Encrypted bodies that cannot be fetched via the API are still recorded with
their file/size and a body_encrypted flag instead of being dropped.

Discovered layouts: ~/.gemini/antigravity/ (live), plus any sibling
antigravity-backup/ and antigravity-ide/ datastores.

Stdlib only — no third-party dependencies (urllib instead of requests,
no typer/rich).
"""

import json
import os
import platform
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Minimal protobuf wire-format decoding (stdlib only, no protoc dependency)
# ---------------------------------------------------------------------------

def _pb_varint(data, i):
    """Decode a protobuf varint starting at index i -> (value, next_index)."""
    result = 0
    shift = 0
    n = len(data)
    while True:
        if i >= n:
            raise ValueError("varint truncated")
        b = data[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i
        shift += 7
        if shift >= 64:
            raise ValueError("varint too long")


def _pb_fields(data):
    """Iterate protobuf fields -> (field_number, wire_type, value).

    wire_type 0 -> int value; wire_type 1/5 -> raw bytes (fixed); 2 -> bytes.
    """
    i = 0
    n = len(data)
    while i < n:
        tag, i = _pb_varint(data, i)
        field, wire = tag >> 3, tag & 7
        if wire == 0:
            v, i = _pb_varint(data, i)
            yield field, wire, v
        elif wire == 1:
            yield field, wire, data[i:i + 8]
            i += 8
        elif wire == 2:
            ln, i = _pb_varint(data, i)
            yield field, wire, data[i:i + ln]
            i += ln
        elif wire == 5:
            yield field, wire, data[i:i + 4]
            i += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")


def _pb_str(v):
    """Decode a length-delimited protobuf value as UTF-8 text."""
    try:
        return v.decode('utf-8', errors='replace')
    except (AttributeError, UnicodeDecodeError):
        return ''


def _pb_timestamp(fields):
    """Decode a protobuf Timestamp message (seconds, nanos) -> ISO-8601 str."""
    seconds = None
    nanos = 0
    try:
        for f, w, v in fields:
            if f == 1 and w == 0:
                seconds = v
            elif f == 2 and w == 0:
                nanos = v
    except Exception:
        return None
    if seconds is None:
        return None
    try:
        dt = datetime.fromtimestamp(seconds + nanos / 1e9, tz=timezone.utc)
        return dt.isoformat().replace('+00:00', 'Z')
    except (OverflowError, OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def find_antigravity_datastores():
    """Find all Antigravity datastores under the Gemini home directory.

    Returns a list of (label, path) — e.g. ('antigravity', ~/.gemini/antigravity).
    """
    system = platform.system()
    home = Path.home()

    candidates = []
    if system == "Darwin":
        candidates = [home / ".gemini", home / ".config/gemini"]
    elif system == "Linux":
        candidates = [
            home / ".gemini", home / ".config/gemini",
            home / ".local/share/gemini",
        ]
    elif system == "Windows":
        candidates = [
            Path(os.environ.get('USERPROFILE', home)) / ".gemini",
            Path(os.environ.get('LOCALAPPDATA', home / 'AppData/Local')) / "gemini",
        ]
    else:
        candidates = [home / ".gemini"]

    found = []
    for base in candidates:
        if not base.is_dir():
            continue
        for sub in sorted(base.glob('antigravity*')):
            if sub.is_dir():
                found.append((sub.name, sub))
    return found


def default_ls_binary():
    """Locate the official language_server binary for this platform.

    Mirrors antigravity-decryptor's launcher defaults.
    """
    system = platform.system()
    if system == "Windows":
        candidates = [
            Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / 'antigravity'
            / 'resources' / 'bin' / 'language_server.exe',
            Path.home() / 'AppData' / 'Local' / 'Programs' / 'antigravity'
            / 'resources' / 'bin' / 'language_server.exe',
        ]
    elif system == "Darwin":
        candidates = [
            Path('/Applications/Antigravity.app/Contents/Resources/app/'
                 'extensions/antigravity/bin/language_server_macos_arm'),
        ]
    else:
        candidates = []
    for c in candidates:
        if c.exists():
            return c
    return None


# ---------------------------------------------------------------------------
# LanguageServer discovery + API client (ported from antigravity-decryptor
# and antigravity-history; stdlib only)
# ---------------------------------------------------------------------------

LS_SERVICE = "exa.language_server_pb.LanguageServerService"


def _daemon_dir(store_path):
    return Path(store_path) / 'daemon'


def read_discovery_records(store_path):
    """Read daemon/ls_*.json discovery files -> list of dicts (newest last)."""
    records = []
    ddir = _daemon_dir(store_path)
    if not ddir.is_dir():
        return records
    for f in sorted(ddir.glob('ls_*.json')):
        try:
            data = json.loads(f.read_text(encoding='utf-8'))
            data['_file'] = str(f)
            data['_mtime'] = f.stat().st_mtime
            records.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    records.sort(key=lambda r: r.get('_mtime', 0))
    return records


def _port_open(host, port, timeout=1.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _api_call(port, method, params=None, csrf_token=None, timeout=15):
    """POST a gRPC-web style JSON call to the language server.

    Tries plain HTTP first (like antigravity-decryptor), then HTTPS with the
    CSRF token (like antigravity-history). Returns parsed JSON or None.
    """
    body = json.dumps(params or {}).encode('utf-8')
    attempts = []

    if csrf_token:
        attempts.append((
            f"https://localhost:{port}/{LS_SERVICE}/{method}",
            {
                'Content-Type': 'application/json',
                'Connect-Protocol-Version': '1',
                'X-Codeium-Csrf-Token': csrf_token,
            },
            True,
        ))
    attempts.append((
        f"http://127.0.0.1:{port}/{LS_SERVICE}/{method}",
        {'Content-Type': 'application/json'},
        False,
    ))

    for url, headers, use_ssl in attempts:
        try:
            req = urllib.request.Request(url, data=body, method='POST')
            for k, v in headers.items():
                req.add_header(k, v)
            ctx = ssl._create_unverified_context() if use_ssl else None
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                if resp.status >= 400:
                    continue
                raw = resp.read()
                try:
                    return json.loads(raw.decode('utf-8', errors='replace'))
                except json.JSONDecodeError:
                    return None
        except (urllib.error.URLError, OSError, ssl.SSLError,
                socket.timeout, ValueError):
            continue
    return None


def is_server_alive(port, csrf_token=None, timeout=5):
    """Probe GetAllCascadeTrajectories; returns True if the server responds."""
    return _api_call(port, "GetAllCascadeTrajectories", timeout=timeout,
                     csrf_token=csrf_token) is not None


def get_cascade_trajectory(port, cascade_id, csrf_token=None, timeout=60):
    """GetCascadeTrajectory -> raw trajectory JSON or None."""
    return _api_call(port, "GetCascadeTrajectory",
                     {"cascadeId": cascade_id}, timeout=timeout,
                     csrf_token=csrf_token)


def fetch_model_catalog(port, csrf_token=None, timeout=15):
    """Fetch the LSP's model catalog via GetAvailableModels.

    The language server serves a catalog of every model it can route to
    (including the current login's available models). Each entry maps the
    obfuscated ``MODEL_PLACEHOLDER_M*`` id back to the REAL model name (the
    catalog key, e.g. ``gemini-3-flash`` / ``claude-opus-4-6-thinking``) plus
    provider, display name, token limits, and per-model quota.

    Returns ``{placeholder: {'model': real_name, 'api_provider': ...,
    'display_name': ..., 'max_tokens': ...}}`` for every entry whose catalog
    key is a real model name (entries whose key is itself obfuscated — e.g.
    ``chat_20706`` / ``MODEL_CHAT_*`` internal models — are skipped, since
    their placeholder gives no real name to resolve to).
    """
    raw = _api_call(port, "GetAvailableModels", params={},
                    csrf_token=csrf_token, timeout=timeout)
    models = ((raw or {}).get('response') or {}).get('models') or {}
    catalog = {}
    for key, v in models.items():
        if not isinstance(v, dict):
            continue
        ph = v.get('model')
        if not ph or not str(ph).startswith('MODEL_PLACEHOLDER_M'):
            continue
        # Skip entries whose catalog *key* is also obfuscated (internal only).
        if str(key).startswith(('MODEL_', 'chat_')):
            continue
        catalog[str(ph)] = {
            'model': str(key),
            'api_provider': v.get('apiProvider'),
            'display_name': v.get('displayName'),
            'max_tokens': v.get('maxTokens'),
        }
    return catalog


def launch_server(store_path, ls_binary=None, verbose=True):
    """Launch the official language server in standalone persistent mode.

    Ported from antigravity-decryptor internal/server/launcher.go, with the
    launch flags taken from the official app (dist/languageServer.js).

    Note: the standalone LS logs its random HTTPS/HTTP ports to stdout/stderr
    ("Language server listening on random port at N for HTTP") but does NOT
    write a daemon discovery file — that file is written by the Electron app
    when it launches the LS itself. So we capture the LS log and parse the
    ports out of it, then probe for liveness.

    Returns (port, csrf_token) or (None, None) on failure.
    """
    binary = ls_binary or default_ls_binary()
    if binary is None or not Path(binary).exists():
        if verbose:
            print(f"  ⚠️  language_server binary not found; set "
                  f"ANTIGRAVITY_LS_PATH to override")
        return None, None

    flags = [
        str(binary),
        "--standalone",
        "--override_ide_name", "antigravity",
        "--subclient_type", "hub",
        "--override_ide_version", "0.0.0",
        "--override_user_agent_name", "antigravity",
        "--api_server_url", "https://generativelanguage.googleapis.com",
        "--cloud_code_endpoint", "https://daily-cloudcode-pa.googleapis.com",
        "--enable_sidecars",
        # The official app passes getAppDataDirName() — the datastore *name*
        # (e.g. "antigravity"), which the LS resolves under ~/.gemini/ itself.
        "--app_data_dir", Path(store_path).name,
    ]

    # Capture the LS log so we can parse the advertised ports.
    log_path = None
    try:
        ddir = _daemon_dir(store_path)
        ddir.mkdir(parents=True, exist_ok=True)
        log_path = str(ddir / f'ls_{os.getpid()}.standalone.log')
    except OSError:
        log_path = None
    ls_log = open(log_path, 'wb') if log_path else None

    try:
        # stdin=DEVNULL mirrors the official app, which closes the child's
        # stdin immediately to stop the LS from blocking on interactive input.
        if platform.system() == "Windows":
            creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            proc = subprocess.Popen(
                flags, stdin=subprocess.DEVNULL,
                stdout=ls_log, stderr=ls_log,
                creationflags=creationflags,
            )
        else:
            proc = subprocess.Popen(
                flags, stdin=subprocess.DEVNULL,
                stdout=ls_log, stderr=ls_log,
                start_new_session=True,
            )
    except OSError as e:
        if verbose:
            print(f"  ⚠️  Could not launch language server: {e}")
        if ls_log:
            ls_log.close()
        return None, None

    if verbose:
        print(f"  🚀 Launched language server (pid {proc.pid})…")

    port_re = re.compile(r'listening on random port at (\d+) for (HTTPS|HTTP)')
    deadline = time.time() + 30
    port = None
    csrf = None

    try:
        while time.time() < deadline:
            # 1. Parse the advertised ports from the LS log.
            if log_path is not None:
                try:
                    with open(log_path, 'r', encoding='utf-8',
                              errors='replace') as fh:
                        log_text = fh.read()
                except OSError:
                    log_text = ''
                for m in port_re.finditer(log_text):
                    p = int(m.group(1))
                    # Prefer the HTTP port (no TLS/CSRF needed, like the Go tool).
                    if m.group(2) == 'HTTP' and port is None:
                        port = p

            # 2. Also accept a discovery file written for our pid, if any.
            if port is None:
                for rec in read_discovery_records(store_path):
                    if rec.get('pid') == proc.pid:
                        p = rec.get('httpPort') or rec.get('httpsPort')
                        if p:
                            port, csrf = p, rec.get('csrfToken')
                            break

            # 3. Probe liveness on the found port.
            if port is not None:
                if is_server_alive(port, timeout=5):
                    if verbose:
                        print(f"  ✅ Language server ready on port {port}")
                    return port, csrf, proc
                port = None  # not ready yet; keep waiting

            time.sleep(0.5)
    finally:
        if ls_log is not None:
            # Parent handle only — the child inherited its own copy, so
            # closing here is safe and prevents a handle leak per run.
            ls_log.close()

    if verbose:
        print("  ⚠️  language server did not become ready within 30s")
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    except OSError:
        pass
    # Tidy up the standalone log once the child has exited (its handle to the
    # file is released, so the unlink can succeed even on Windows).
    if log_path:
        try:
            Path(log_path).unlink(missing_ok=True)
        except OSError:
            pass
    return None, None, None


def acquire_server(store_path, ls_binary=None, verbose=True):
    """Reuse a live server (via daemon discovery) or launch a new one.

    Returns (port, csrf_token, proc_or_None). When the server is launched
    here, proc is the child Popen so the caller can terminate it after use;
    when reusing an existing server, proc is None (do NOT kill it).
    """
    # 1. Reuse existing server from discovery files
    for rec in read_discovery_records(store_path):
        p = rec.get('httpPort') or rec.get('httpsPort')
        if p and _port_open('127.0.0.1', p):
            tok = rec.get('csrfToken')
            if is_server_alive(p, tok):
                if verbose:
                    print(f"  ♻️  Reusing live language server on port {p}")
                return p, tok, None

    # 2. Launch a new standalone server
    port, csrf, proc = launch_server(store_path, ls_binary=ls_binary,
                                     verbose=verbose)
    return port, csrf, proc


# ---------------------------------------------------------------------------
# Step parser (ported from antigravity-history parser.py + decryptor
# normalized.go)
# ---------------------------------------------------------------------------

class FieldLevel:
    DEFAULT = "default"
    THINKING = "thinking"
    FULL = "full"


_SYSTEM_SOURCE = "CORTEX_STEP_SOURCE_SYSTEM"
_SYSTEM_STEP_TYPES = {
    "CORTEX_STEP_TYPE_CONVERSATION_HISTORY",
    "CORTEX_STEP_TYPE_KNOWLEDGE_ARTIFACTS",
    "CORTEX_STEP_TYPE_EPHEMERAL_MESSAGE",
    "CORTEX_STEP_TYPE_CHECKPOINT",
}


_DIFF_PREFIX = {
    "UNIFIED_DIFF_LINE_TYPE_INSERT": "+",
    "UNIFIED_DIFF_LINE_TYPE_DELETE": "-",
    "UNIFIED_DIFF_LINE_TYPE_CONTEXT": " ",
}


def _normalize_diff(diff):
    """Normalize diff (str or structured dict) to a string."""
    if isinstance(diff, str):
        return diff
    if isinstance(diff, dict):
        lines_data = (diff.get("unifiedDiff") or {}).get("lines") or []
        if not lines_data:
            return str(diff)
        parts = []
        for line in lines_data:
            text = line.get("text", "")
            prefix = _DIFF_PREFIX.get(line.get("type", ""), " ")
            parts.append(f"{prefix}{text}")
        return "\n".join(parts)
    return str(diff)


def _extract_step_text(step):
    """Extract human-readable text from a step, mirroring decryptor's logic."""
    ui = step.get("userInput")
    if isinstance(ui, dict):
        for k in ("userResponse", "text", "content"):
            if ui.get(k):
                return ui[k]
    pr = step.get("plannerResponse")
    if isinstance(pr, dict):
        for k in ("modifiedResponse", "response", "text"):
            if pr.get(k):
                return pr[k]
        if pr.get("thinking"):
            return "*Thinking:*\n" + pr["thinking"]
    tb = step.get("taskBoundary")
    if isinstance(tb, dict):
        name = tb.get("taskName", "")
        summary = tb.get("taskSummary", "")
        if name:
            return f"**Task**: {name}\n{summary}"
    for key in ("notifyUser", "conversationHistory", "ephemeralMessage",
                "knowledgeArtifacts"):
        sub = step.get(key)
        if isinstance(sub, dict):
            for text_key in ("text", "message", "content", "summary"):
                if sub.get(text_key):
                    return sub[text_key]
    return step.get("text", "")


def _parse_user_input(step, include_full):
    ui = step.get("userInput") or {}
    content = ui.get("userResponse") or ui.get("text") or ""
    if not content:
        return None
    msg = {"role": "user", "content": content}
    if include_full:
        state = ui.get("activeUserState") or {}
        doc = state.get("activeDocument") or {}
        if doc.get("absoluteUri"):
            msg["active_file"] = doc["absoluteUri"]
            msg["editor_language"] = doc.get("editorLanguage", "")
    return msg


def _parse_planner_response(step, include_thinking, include_full):
    pr = step.get("plannerResponse") or {}
    content = pr.get("modifiedResponse") or pr.get("response") or ""
    if not content:
        return None
    msg = {"role": "assistant", "content": content}
    if include_thinking:
        if pr.get("thinking"):
            msg["thinking"] = pr["thinking"]
        if pr.get("stopReason"):
            msg["stop_reason"] = pr["stopReason"]
    if include_full:
        # Model is attached centrally in _parse_step (real name recovered from
        # trajectory-level generatorMetadata when the step metadata only
        # carries Antigravity's MODEL_PLACEHOLDER_M* scrub value).
        if pr.get("thinkingDuration"):
            msg["thinking_duration"] = pr["thinkingDuration"]
        if pr.get("messageId"):
            msg["message_id"] = pr["messageId"]
    return msg


def _parse_code_action(step, include_full):
    ca = step.get("codeAction") or {}
    action_spec = ca.get("actionSpec") or {}
    command_spec = action_spec.get("command") or {}
    description = ca.get("description", "") or command_spec.get("instruction", "")
    file_path = ""
    action_result = ca.get("actionResult") or {}
    edit = action_result.get("edit") or {}
    if edit.get("absoluteUri"):
        file_path = edit["absoluteUri"]
    elif command_spec.get("targetFile"):
        file_path = command_spec["targetFile"]
    else:
        cf = action_spec.get("createFile") or {}
        p = cf.get("path")
        if isinstance(p, dict):
            file_path = p.get("absoluteUri") or ""
        elif isinstance(p, str):
            file_path = p

    summary = f"[Code Edit] {file_path}" if file_path else "[Code Edit]"
    if description:
        summary += f"\n{description}"

    msg = {"role": "tool", "tool_name": "code_edit", "content": summary}
    if file_path:
        msg["file_path"] = file_path

    if include_full:
        if edit.get("diff"):
            msg["diff"] = _normalize_diff(edit["diff"])
        artifact = ca.get("artifactMetadata") or {}
        if artifact.get("summary"):
            msg["artifact_summary"] = artifact["summary"]
        if artifact.get("artifactType"):
            msg["artifact_type"] = artifact["artifactType"]
        if ca.get("isArtifactFile"):
            msg["is_artifact"] = True
    return msg


def _parse_run_command(step, include_thinking, include_full):
    rc = step.get("runCommand") or {}
    command = (rc.get("commandLine") or rc.get("proposedCommandLine")
               or rc.get("command") or "")
    if not command:
        return None
    msg = {"role": "tool", "tool_name": "run_command", "content": command}
    if include_thinking:
        if rc.get("cwd"):
            msg["cwd"] = rc["cwd"]
        if rc.get("exitCode") is not None:
            msg["exit_code"] = rc["exitCode"]
        if rc.get("commandId"):
            msg["command_id"] = rc["commandId"]
    if include_full:
        output = (rc.get("combinedOutput") or {}).get("full")
        if output:
            msg["output"] = output
    return msg


def _parse_view_file(step, include_thinking):
    vf = step.get("viewFile") or {}
    path = (vf.get("absolutePathUri") or vf.get("filePath")
            or vf.get("path") or "")
    content = vf.get("content") or ""
    if not path and not content:
        return None
    msg = {"role": "tool", "tool_name": "view_file",
           "content": content or path}
    if path:
        msg["file_path"] = path
    if include_thinking:
        if vf.get("numLines"):
            msg["num_lines"] = vf["numLines"]
        if vf.get("numBytes"):
            msg["num_bytes"] = vf["numBytes"]
    return msg


def _parse_search_web(step, include_full):
    sw = step.get("searchWeb") or {}
    query = sw.get("query", "")
    summary = sw.get("summary", "")
    content = query
    if summary:
        content = f"{query}\n\n{summary}"
    msg = {"role": "tool", "tool_name": "search_web",
           "content": content or "[Web Search]"}
    if include_full:
        if summary:
            msg["search_summary"] = summary
        provider = (sw.get("thirdPartyConfig") or {}).get("provider")
        if provider:
            msg["search_provider"] = provider
    return msg


def _parse_arguments_json(raw):
    """Parse ``metadata.toolCall.argumentsJson`` (JSON string) -> dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw
    return raw


_PLACEHOLDER_RE = re.compile(r"^\[[A-Z][A-Za-z ]{2,40}\]$")


def _tool_call_args_content(tool_call):
    """Rebuild a readable tool message from a model tool call's arguments.

    Used when a step's payload is cleared/absent (``CORTEX_STEP_STATUS_CLEARED``)
    or the payload parser has nothing to show: the call's arguments still
    record what the tool was asked to do, so surface them instead of a bare
    placeholder like ``[Web Search]``.
    """
    if not isinstance(tool_call, dict):
        return ""
    args = tool_call.get("args")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (json.JSONDecodeError, TypeError):
            return args or ""
    if not isinstance(args, dict):
        return ""
    name = tool_call.get("name") or ""
    # Preferred per-tool display fields (argument keys observed on raw data).
    keys_by_tool = {
        "list_dir": ("DirectoryPath", "directoryPath", "path"),
        "find_by_name": ("Pattern", "pattern"),
        "search_web": ("Query", "query"),
        "read_url_content": ("Url", "URL", "url"),
        "write_to_file": ("TargetFile", "targetFile", "File"),
        "replace_file_content": ("TargetFile", "targetFile", "File"),
        "multi_replace_file_content": ("TargetFile", "targetFile"),
        "code_edit": ("TargetFile", "targetFile", "File"),
        "run_command": ("CommandLine", "commandLine", "command"),
        "view_file": ("FilePath", "filePath", "path"),
    }
    for key in keys_by_tool.get(name, ()):
        val = args.get(key)
        if val:
            return str(val)
    if name == "command_status":
        cid = args.get("CommandId") or args.get("commandId")
        if cid is not None:
            return f"command {cid}"
    # Fallback: free-text description / instruction fields
    for key in ("Description", "description", "Instruction", "instruction",
                "Query", "query", "Prompt", "prompt"):
        val = args.get(key)
        if val:
            s = str(val)
            return s[:500] + ("…" if len(s) > 500 else "")
    return ""


def _parse_find(step, include_thinking):
    find = step.get("find") or {}
    out = find.get("truncatedOutput") or find.get("rawOutput") or ""
    pattern = find.get("pattern") or ""
    sdir = find.get("searchDirectory") or ""
    cmd = find.get("commandRun") or ""
    total = find.get("totalResults")
    content = out
    if not content:
        # The raw find payload carries the search spec (pattern / directory /
        # the exact fd command run) but not the output body; surface the spec
        # so the call's intent survives instead of a bare placeholder.
        bits = []
        if pattern:
            bits.append(f"pattern: {pattern}")
        if sdir:
            bits.append(f"in: {sdir}")
        if cmd:
            bits.append(cmd)
        content = "\n".join(bits)
    if total is not None:
        prefix = f"[{total} result(s)]"
        content = f"{prefix}\n{content}" if content else prefix
    msg = {"role": "tool", "tool_name": "find", "content": content}
    if include_thinking:
        if pattern:
            msg["pattern"] = pattern
        if sdir:
            msg["search_directory"] = sdir
        if total is not None:
            msg["total_results"] = total
        if cmd:
            msg["command"] = cmd
    return msg


def _parse_list_directory(step, include_thinking):
    ld = step.get("listDirectory") or {}
    path = (ld.get("directoryPathUri") or ld.get("directoryPath")
            or ld.get("path") or "")
    results = ld.get("results") or []
    lines = []
    for r in results:
        if not isinstance(r, dict):
            continue
        name = r.get("name") or ""
        if r.get("isDir"):
            lines.append(f"{name}/")
        else:
            size = r.get("sizeBytes")
            lines.append(f"{name} ({size} B)" if size else name)
    content = path
    if lines:
        content = f"{path}\n" + "\n".join(lines) if path else "\n".join(lines)
    msg = {"role": "tool", "tool_name": "list_dir",
           "content": content or "[List Directory]"}
    if path:
        msg["directory_path"] = path
    if include_thinking and lines:
        msg["entries"] = results
    return msg


def _parse_command_status(step, include_thinking):
    cs = step.get("commandStatus") or {}
    step_status = step.get("status") or ""
    err = step.get("error") or {}
    short_err = err.get("shortError") or ""
    cmd_id = cs.get("commandId")
    oc = cs.get("outputCharacterCount")
    wait = cs.get("waitDurationSeconds")
    bits = []
    if cmd_id is not None:
        bits.append(f"command {cmd_id}")
    if oc is not None:
        bits.append(f"{oc} output chars")
    if wait is not None:
        bits.append(f"waited {wait}s")
    content = "; ".join(bits)
    if short_err:
        content = (content + "; " if content else "") + f"ERROR: {short_err}"
    msg = {"role": "tool", "tool_name": "command_status", "content": content}
    if include_thinking:
        if cmd_id is not None:
            msg["command_id"] = cmd_id
        if step_status:
            # Preserve the raw step status under its own key so it does not
            # collide with the HF-traces 'success'/'error' status convention.
            msg["step_status"] = step_status
        if oc is not None:
            msg["output_character_count"] = oc
        if wait is not None:
            msg["waited_duration_seconds"] = wait
        if err.get("fullError"):
            msg["error_detail"] = err["fullError"]
    return msg


def _parse_read_url_content(step):
    ru = step.get("readUrlContent") or {}
    url = ru.get("url") or ""
    body = (ru.get("content") or ru.get("markdown")
            or ru.get("body") or "")
    msg = {"role": "tool", "tool_name": "read_url",
           "content": body or url or "[Read URL]"}
    if url:
        msg["url"] = url
    return msg


def _parse_code_acknowledgement(step, include_full):
    ca = step.get("codeAcknowledgement") or {}
    infos = ca.get("codeAcknowledgementInfos") or []
    files = []
    diffs = []
    for info in infos:
        if not isinstance(info, dict):
            continue
        uri = info.get("uriPath") or ""
        if uri:
            files.append(uri)
        diff = _normalize_diff(info.get("diff"))
        if diff:
            diffs.append(diff)
    accepted = ca.get("isAccept")
    content = "Accepted code" if accepted else "Rejected code"
    if files:
        content += f" ({len(files)} file(s)): " + ", ".join(files)
    msg = {"role": "tool", "tool_name": "code_acknowledgement",
           "content": content}
    if include_full and diffs:
        msg["diffs"] = diffs
    return msg


def _parse_step(step, include_thinking, include_full, model_override=None):
    """Parse a single step -> message dict or None (skip system types).

    Model-originated tool steps carry ``metadata.toolCall``
    (``{id, name, argumentsJson}``); the real call id, tool name and parsed
    arguments are attached to the message so ``parse_steps`` can emit a linked
    assistant ``tool_calls`` entry + ``tool`` result pair.

    ``model_override`` (optional dict from :func:`_extract_step_models`) lets
    the caller attach the REAL model name recovered from the trajectory-level
    ``generatorMetadata`` instead of Antigravity's ``MODEL_PLACEHOLDER_M*``
    scrub value stored in per-step ``metadata.generatorModel``.
    """
    step_type = step.get("type") or step.get("stepType") or ""
    md = step.get("metadata") or {}

    # System steps (conversation-history packing, ephemeral status messages,
    # token-usage checkpoints, knowledge artifacts) are internal noise — not
    # user-visible text and not tool calls. Filter by source, and by type as a
    # belt-and-suspenders guard for steps missing the source field.
    if md.get("source") == _SYSTEM_SOURCE or step_type in _SYSTEM_STEP_TYPES:
        return None

    tc = md.get("toolCall")
    tool_call = None
    if isinstance(tc, dict) and tc.get("id"):
        tool_call = {
            "id": tc["id"],
            "name": tc.get("name") or "",
            "args": _parse_arguments_json(tc.get("argumentsJson")),
        }

    if step_type == "CORTEX_STEP_TYPE_USER_INPUT":
        msg = _parse_user_input(step, include_full)
    elif step_type == "CORTEX_STEP_TYPE_PLANNER_RESPONSE":
        msg = _parse_planner_response(step, include_thinking, include_full)
    elif step_type == "CORTEX_STEP_TYPE_CODE_ACTION":
        msg = _parse_code_action(step, include_full)
    elif step_type == "CORTEX_STEP_TYPE_CODE_ACKNOWLEDGEMENT":
        msg = _parse_code_acknowledgement(step, include_full)
    elif step_type == "CORTEX_STEP_TYPE_RUN_COMMAND":
        msg = _parse_run_command(step, include_thinking, include_full)
    elif step_type == "CORTEX_STEP_TYPE_VIEW_FILE":
        msg = _parse_view_file(step, include_thinking)
    elif step_type == "CORTEX_STEP_TYPE_FIND":
        msg = _parse_find(step, include_thinking)
    elif step_type == "CORTEX_STEP_TYPE_LIST_DIRECTORY":
        msg = _parse_list_directory(step, include_thinking)
    elif step_type == "CORTEX_STEP_TYPE_SEARCH_WEB":
        msg = _parse_search_web(step, include_full)
    elif step_type == "CORTEX_STEP_TYPE_READ_URL_CONTENT":
        msg = _parse_read_url_content(step)
    elif step_type == "CORTEX_STEP_TYPE_COMMAND_STATUS":
        msg = _parse_command_status(step, include_thinking)
    else:
        msg = None

    # A model tool call must never be silently dropped, even when its payload
    # parser returns None (e.g. an empty or cleared result).
    if msg is None and tool_call is not None:
        msg = {"role": "tool", "tool_name": tool_call["name"] or "tool",
               "content": ""}
    if msg is not None:
        if tool_call is not None:
            msg["_tool_call"] = tool_call
            msg["tool_call_id"] = tool_call["id"]
            if tool_call["name"]:
                msg["tool_name"] = tool_call["name"]

        # Model attribution (FULL level only, matching the documented level
        # contract). Prefer the real name recovered from the trajectory-level
        # generatorMetadata (model_override); fall back to the per-step
        # metadata value (which is often a MODEL_PLACEHOLDER_M* scrub). Users
        # never carry a model, so only annotate assistant/tool turns.
        if include_full and msg.get('role') != 'user' and model_override:
            real = model_override.get('model')
            if real:
                if not msg.get('model'):
                    msg['model'] = real
                # Prefer the *entry's* own placeholder over the step's scrub:
                # for forward-filled steps the step's scrub can belong to a
                # different entry than the model we attributed, which would
                # fabricate false (placeholder, provider) -> model pairs in the
                # empirical resolver map. The entry's placeholder is always
                # self-consistent with its real model.
                raw_model = model_override.get('placeholder')
                if not raw_model:
                    raw_model = md.get('generatorModel')
                if raw_model and str(raw_model).startswith('MODEL_PLACEHOLDER'):
                    msg['model_placeholder'] = raw_model
                if model_override.get('api_provider'):
                    msg['api_provider'] = model_override['api_provider']
            else:
                # No real name for this entry — keep the scrub value but still
                # carry the api_provider so the post-pass placeholder resolver
                # (catalog / provider-keyed empirical map) can disambiguate it.
                if md.get('generatorModel'):
                    msg.setdefault('model', md['generatorModel'])
                if model_override.get('api_provider'):
                    msg['api_provider'] = model_override['api_provider']
        elif include_full and msg.get('role') != 'user' and md.get('generatorModel'):
            msg.setdefault('model', md['generatorModel'])

        # CLEARED / absent step bodies (payload removed upstream): rebuild the
        # content from the call's arguments so the tool's intent survives
        # instead of a bare placeholder like "[Web Search]" / "[Code Edit]".
        content = str(msg.get("content") or "").strip()
        if (not content or _PLACEHOLDER_RE.match(content)) \
                and tool_call is not None:
            rebuilt = _tool_call_args_content(tool_call)
            if rebuilt:
                msg["content"] = rebuilt
        if step.get("status"):
            msg["step_status"] = step["status"]
            if step["status"] == "CORTEX_STEP_STATUS_CLEARED":
                msg["payload_cleared"] = True
                # A cleared step whose tool-call metadata did NOT survive the
                # upstream scrub has no arguments to rebuild from. Replace the
                # bare label with an explicit note so consumers know the body
                # was cleared rather than lost.
                cur = str(msg.get("content") or "").strip()
                if not cur or _PLACEHOLDER_RE.match(cur):
                    msg["content"] = "(payload cleared upstream)" if not cur \
                        else cur + " (payload cleared upstream)"
        return msg

    # Fallback: any other non-system step with extractable text
    text = _extract_step_text(step)
    if text:
        role = "assistant" if "RESPONSE" in step_type else "tool"
        return {"role": role, "tool_name": step_type, "content": text}
    return None


def parse_steps(steps, level=FieldLevel.FULL, step_models=None,
                fallback_model=None):
    """Parse raw API steps into structured messages.

    level: default (basic) / thinking (+thinking/timestamps/cwd/exit) /
           full (+diff/output/search/model). Defaults to full.

    Model-originated tool steps are emitted as an assistant ``tool_calls``
    entry followed by a ``tool``-role result message carrying the matching
    ``tool_call_id``. Consecutive tool steps sharing an ``executionId`` (one
    model turn issuing several parallel tool calls) are batched onto a single
    assistant message.

    ``step_models`` (optional, from :func:`_extract_step_models`) maps each
    step index to the real model recovered from the trajectory-level
    ``generatorMetadata``; the last known real model is forward-filled so a
    step that fell outside a mapped generatorMetadata block still gets the
    session's actual model rather than a bare placeholder.

    ``fallback_model`` (optional) is the session-level real model (the last
    generatorMetadata entry carrying one) applied to steps whose own entry
    mapped to no real name — older trajectories only record the real model on
    their final entry.
    """
    include_thinking = level in (FieldLevel.THINKING, FieldLevel.FULL)
    include_full = level == FieldLevel.FULL
    step_models = step_models or {}

    messages = []
    pending = []        # [(tool_call, tool_msg, timestamp, model_override)]
    pending_exec = None
    last_model = None   # forward-fill: last real model seen in this session

    def flush():
        nonlocal pending, pending_exec
        if not pending:
            return
        first_ts = pending[0][2]
        asst = {
            "role": "assistant",
            "content": "",
            # Raw source tool-call objects; traces-format normalization happens
            # later in traces_export.normalize_message (write time only).
            "tool_calls": [tc for tc, _, _, _ in pending],
        }
        if first_ts:
            asst["timestamp"] = first_ts
        # Attribute the batch with the real model of its (usually single) turn.
        for _, _, _, mo in pending:
            if not mo:
                continue
            if mo.get('model') and not asst.get('model'):
                asst['model'] = mo['model']
            if mo.get('placeholder') and not asst.get('model'):
                # No real name on this turn's entry: keep the scrub value so
                # the post-pass resolver (catalog / empirical map) can still
                # work; without it the model key would vanish entirely.
                asst['model'] = mo['placeholder']
            if mo.get('api_provider') and not asst.get('api_provider'):
                asst['api_provider'] = mo['api_provider']
            if mo.get('placeholder') and not asst.get('model_placeholder'):
                asst['model_placeholder'] = mo['placeholder']
            if asst.get('model') and asst.get('api_provider'):
                break
        messages.append(asst)
        for _, tmsg, _, _ in pending:
            messages.append(tmsg)
        pending = []
        pending_exec = None

    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        metadata = step.get("metadata") or {}
        exec_id = metadata.get("executionId")
        timestamp = metadata.get("createdAt") if include_thinking else None
        info = step_models.get(i)
        if info and info.get('model'):
            last_model = info
        # Resolve the real model for this step, in order:
        #   1. the step's own generatorMetadata entry (when it has a model),
        #   2. the step's own entry even when it has NO real name — the entry
        #      still carries the placeholder + api_provider, which the
        #      post-pass resolver (catalog / provider-keyed empirical map)
        #      needs to disambiguate later,
        #   3. the last real model seen so far in this session (forward-fill),
        #   4. the trajectory's final real model (older-format sessions),
        #   5. none -> caller falls back to the per-step placeholder value.
        if info and info.get('model'):
            model_override = info
        elif info:
            model_override = info
        elif last_model:
            model_override = last_model
        elif fallback_model and fallback_model.get('model'):
            model_override = fallback_model
        else:
            model_override = None
        msg = _parse_step(step, include_thinking, include_full, model_override)
        if msg is None:
            continue
        tool_call = msg.pop("_tool_call", None)
        if timestamp:
            msg["timestamp"] = timestamp
        if tool_call is not None:
            if pending and exec_id is not None and pending_exec == exec_id:
                pending.append((tool_call, msg, timestamp, model_override))
            else:
                flush()
                pending = [(tool_call, msg, timestamp, model_override)]
                pending_exec = exec_id
        else:
            flush()
            messages.append(msg)
    flush()
    return messages


def _extract_step_models(raw):
    """Map step index -> real model info from trajectory ``generatorMetadata``.

    Antigravity scrubs per-step ``metadata.generatorModel`` down to
    ``MODEL_PLACEHOLDER_M*`` values, but each ``generatorMetadata`` entry
    (one per model invocation, keyed by ``stepIndices``) still records the
    REAL model name:

      * ``chatModel.responseModel``  (current format, e.g. gemini-3.1-pro-low)
      * ``plannerConfig.modelName``  (older format, e.g. gemini-3-pro-high)

    plus ``chatModel.usage.apiProvider`` (e.g. API_PROVIDER_GOOGLE_GEMINI /
    API_PROVIDER_ANTHROPIC_VERTEX).

    Returns ``(step_map, fallback)`` where ``step_map`` is
    ``{step_index: {'model': str|None, 'api_provider': str|None,
    'placeholder': str|None}}`` and ``fallback`` is the LAST entry that
    carries a real model (older trajectories often only record the real name
    on their final generatorMetadata entry, with ``None`` on all earlier
    ones) — used as the session-level model for steps outside any mapped
    block or mapped to a model-less entry.
    """
    traj = raw.get('trajectory') if isinstance(raw, dict) else None
    if not isinstance(traj, dict):
        return {}, None
    out = {}
    fallback = None
    for gm in (traj.get('generatorMetadata') or []):
        if not isinstance(gm, dict):
            continue
        chat = gm.get('chatModel') or {}
        real = chat.get('responseModel')
        if not real or str(real).startswith('MODEL_PLACEHOLDER'):
            real = (gm.get('plannerConfig') or {}).get('modelName')
        if not real or str(real).startswith('MODEL_PLACEHOLDER'):
            real = None
        usage = chat.get('usage')
        provider = usage.get('apiProvider') if isinstance(usage, dict) else None
        entry = {'model': real, 'api_provider': provider,
                 'placeholder': chat.get('model')}
        if real:
            fallback = entry
        for si in (gm.get('stepIndices') or []):
            out[si] = entry
    return out, fallback


def _extract_steps(raw):
    """Pull the steps array out of a GetCascadeTrajectory response.

    Response shape (loose): {trajectory: {steps: [...]}, ...} or
    {steps: [...]} or {messages: [...]}.
    """
    if not isinstance(raw, dict):
        return []
    traj = raw.get("trajectory")
    if isinstance(traj, dict):
        steps = traj.get("steps")
        if isinstance(steps, list):
            return steps
    for key in ("steps", "messages"):
        val = raw.get(key)
        if isinstance(val, list):
            return val
    return []


# ---------------------------------------------------------------------------
# Antigravity extraction
# ---------------------------------------------------------------------------

def _decode_conversation_summaries(summaries_file):
    """Parse agyhub_summaries_proto.pb -> list of conversation metadata dicts.

    Schema (field numbers observed on real data):
      repeated Entry { 1: string uuid, 2: Summary }
      Summary {
        1: string title, 2: int message_count,
        3: Timestamp created, 4: string agent_uuid, 5: int,
        7: Timestamp modified, 9: Workspace, 10: Timestamp last_active,
        15: optional, 16: int, 17: Details, 22: int
      }
      Workspace { 1: file uri, 2: file uri, 3: {1,2: git remotes}, 4: branch }
      Details { 1: Workspace, 2: Timestamp, 3: string agent_uuid,
                7: encoded uri, 18: string project_id }
    """
    summaries = []
    try:
        data = summaries_file.read_bytes()
    except OSError:
        return summaries

    try:
        top_fields = list(_pb_fields(data))
    except Exception as e:
        print(f"  ⚠️  Could not decode summaries protobuf: {e}")
        return summaries

    for f, w, v in top_fields:
        if f != 1 or w != 2:
            continue
        try:
            entry = {}
            for ef, ew, ev in _pb_fields(v):
                if ef == 1 and ew == 2:
                    entry['uuid'] = _pb_str(ev)
                elif ef == 2 and ew == 2:
                    s = {}
                    for sf, sw, sv in _pb_fields(ev):
                        if sf == 1 and sw == 2:
                            s['title'] = _pb_str(sv)
                        elif sf == 2 and sw == 0:
                            s['message_count'] = sv
                        elif sf == 3 and sw == 2:
                            s['created_at'] = _pb_timestamp(_pb_fields(sv))
                        elif sf == 4 and sw == 2:
                            s['agent_uuid'] = _pb_str(sv)
                        elif sf == 7 and sw == 2:
                            s['updated_at'] = _pb_timestamp(_pb_fields(sv))
                        elif sf == 9 and sw == 2:
                            ws = {}
                            for wf, ww, wv in _pb_fields(sv):
                                if wf == 1 and ww == 2:
                                    ws['uri'] = _pb_str(wv)
                                elif wf == 2 and ww == 2:
                                    ws['uri_alt'] = _pb_str(wv)
                                elif wf == 4 and ww == 2:
                                    ws['branch'] = _pb_str(wv)
                                elif wf == 3 and ww == 2:
                                    remotes = []
                                    for rf, rw, rv in _pb_fields(wv):
                                        if rw == 2:
                                            remotes.append(_pb_str(rv))
                                    ws['git_remotes'] = remotes
                            s['workspace'] = ws
                        elif sf == 10 and sw == 2:
                            s['last_active_at'] = _pb_timestamp(_pb_fields(sv))
                        elif sf == 16 and sw == 0:
                            s['size_hint'] = sv
                        elif sf == 17 and sw == 2:
                            d = {}
                            for df, dw, dv in _pb_fields(sv):
                                if df == 3 and dw == 2:
                                    d['agent_uuid'] = _pb_str(dv)
                                elif df == 7 and dw == 2:
                                    d['encoded_uri'] = _pb_str(dv)
                                elif df == 18 and dw == 2:
                                    d['project_id'] = _pb_str(dv)
                            s['details'] = d
                        elif sf == 22 and sw == 0:
                            s['kind'] = sv
                    entry['summary'] = s
            if entry.get('uuid') or entry.get('summary'):
                summaries.append(entry)
        except Exception:
            # Skip a single malformed entry instead of aborting the whole file
            continue

    return summaries


def _parse_annotation_pbtxt(ann_file):
    """Parse a tiny text proto like: last_user_view_time:{seconds:... nanos:...}"""
    try:
        text = ann_file.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return None
    m = re.search(r'seconds:\s*(\d+)', text)
    if not m:
        return None
    seconds = int(m.group(1))
    n = re.search(r'nanos:\s*(\d+)', text)
    nanos = int(n.group(1)) if n else 0
    try:
        dt = datetime.fromtimestamp(seconds + nanos / 1e9, tz=timezone.utc)
        return dt.isoformat().replace('+00:00', 'Z')
    except (OverflowError, OSError, ValueError):
        return None


def _collect_brain_artifacts(brain_dir):
    """Collect plaintext brain artifacts (task/plan/walkthrough markdown)."""
    artifacts = []
    if not brain_dir.is_dir():
        return artifacts
    for md in sorted(brain_dir.glob('*.md')):
        meta = {}
        meta_file = md.with_name(md.name + '.metadata.json')
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text(encoding='utf-8'))
            except (json.JSONDecodeError, OSError):
                meta = {}
        try:
            content = md.read_text(encoding='utf-8', errors='replace')
        except OSError:
            content = ''
        artifacts.append({
            'name': md.stem,
            'artifact_type': meta.get('artifactType'),
            'summary': meta.get('summary'),
            'updated_at': meta.get('updatedAt'),
            'content': content,
        })
    return artifacts


def _load_projects_config(projects_dir):
    """Load config/projects/<uuid>.json -> {project_id: {name, folder_uri, ...}}"""
    projects = {}
    if not projects_dir.is_dir():
        return projects
    for jf in projects_dir.glob('*.json'):
        try:
            p = json.loads(jf.read_text(encoding='utf-8'))
            pid = p.get('id')
            if not pid:
                continue
            folder = None
            for res in (p.get('projectResources') or {}).get('resources') or []:
                gf = (res or {}).get('gitFolder') or {}
                folder = gf.get('folderUri') or folder
            projects[pid] = {
                'name': p.get('name'),
                'folder_uri': folder,
                'updated_at': p.get('updatedAt'),
            }
        except (json.JSONDecodeError, OSError):
            continue
    return projects


def _pb_uuid_set(conv_dir):
    """UUIDs of physical .pb conversation bodies on disk."""
    uuids = set()
    if conv_dir.is_dir():
        for pb in conv_dir.glob('*.pb'):
            uuids.add(pb.stem)
    return uuids


def _fetch_messages(port, cascade_id, csrf_token, level=FieldLevel.FULL):
    """Fetch + parse one conversation via the LanguageServer API."""
    raw = get_cascade_trajectory(port, cascade_id, csrf_token)
    if not raw:
        return None
    steps = _extract_steps(raw)
    if not steps:
        return None
    step_models, fallback = _extract_step_models(raw)
    return parse_steps(steps, level, step_models, fallback)


def resolve_placeholder_models(api_messages, catalog):
    """Resolve remaining MODEL_PLACEHOLDER_M* models across all sessions.

    After per-step recovery (generatorMetadata) there are still placeholder
    models on sessions whose trajectory records the real name nowhere (e.g.
    ``796137b4`` — every generatorMetadata entry carries ``responseModel:
    None`` and no ``plannerConfig.modelName``). Antigravity's placeholder
    numbers are NOT globally stable (the same M-number can map to different
    models across configs/eras), so the mapping is derived two ways and
    merged, with the empirical map taking precedence:

      1. LSP catalog (``fetch_model_catalog``): the live model list, which
         pairs each ``MODEL_PLACEHOLDER_M*`` with the real catalog key.
      2. Empirical, provider-keyed: every message that DID get a real model
         also kept its ``model_placeholder`` + ``api_provider``; the majority
         (placeholder, provider) -> real-model pairing observed in THIS run is
         applied to matching placeholder-only messages. This covers retired
         models absent from the live catalog (e.g. M12 -> claude-opus-4-5-
         thinking) and is scoped by provider to avoid cross-provider aliasing
         (M12 is claude on ANTHROPIC_VERTEX but gemini-3-pro-high on
         GOOGLE_GEMINI).

    Mutates the message dicts in place (sets ``model`` to the resolved name
    and ``model_resolved_from_placeholder`` true). Returns the number of
    messages resolved.
    """
    # --- Build the empirical (placeholder, provider) -> model map ---
    # Majority vote per (placeholder, provider): the real model most often
    # observed alongside that scrub value + provider in this run.
    pair_votes = defaultdict(Counter)
    for msgs in api_messages.values():
        for m in msgs:
            if not isinstance(m, dict):
                continue
            ph = m.get('model_placeholder')
            mv = m.get('model')
            if (ph and mv and isinstance(mv, str)
                    and not mv.startswith('MODEL_PLACEHOLDER')):
                pair_votes[(ph, m.get('api_provider'))][mv] += 1
    # Confidence gate: only trust a pairing when the majority is decisive AND
    # well-observed (>=5 votes, >=80% share). Low-confidence pairings are left
    # out so the LSP catalog (authoritative for current ids) decides instead —
    # this keeps a 3-vote historical anomaly (e.g. M35 -> claude-opus-4-6-
    # thinking) from overriding the catalog's answer (M35 -> claude-sonnet-4-6).
    pair_map = {}
    for key, c in pair_votes.items():
        total = sum(c.values())
        top_model, top_count = c.most_common(1)[0]
        if total >= 5 and top_count / total >= 0.8:
            pair_map[key] = top_model

    # --- Apply ---
    resolved = 0
    for msgs in api_messages.values():
        for m in msgs:
            if not isinstance(m, dict):
                continue
            mv = m.get('model')
            if not isinstance(mv, str) or not mv.startswith('MODEL_PLACEHOLDER'):
                continue
            prov = m.get('api_provider')
            real = pair_map.get((mv, prov)) or pair_map.get((mv, None))
            if not real:
                real = (catalog or {}).get(mv, {}).get('model')
            if real and not real.startswith('MODEL_PLACEHOLDER'):
                m['model'] = real
                m.setdefault('model_placeholder', mv)
                m['model_resolved_from_placeholder'] = True
                resolved += 1
    return resolved


def extract_antigravity_store(store_path, port=None, csrf_token=None,
                              use_api=True, level=FieldLevel.FULL,
                              verbose=False):
    """Extract one Antigravity datastore.

    If a live language server (port) is available, conversation bodies are
    fetched via the official API (full messages). Otherwise the plaintext
    protobuf summary index + brain artifacts are used, and encrypted bodies
    are recorded with body_encrypted flags.

    Returns (conversations, implicit_conversations, stats).
    """
    ag = Path(store_path)
    if not ag.is_dir():
        return [], [], {}

    stats = {
        'summaries': 0,
        'bodies': 0,
        'implicit': 0,
        'brain_artifacts': 0,
        'via_api': 0,
        'placeholder_resolved': 0,
    }

    projects = _load_projects_config(ag / 'config' / 'projects')

    summaries = _decode_conversation_summaries(ag / 'agyhub_summaries_proto.pb')
    stats['summaries'] = len(summaries)

    conv_dir = ag / 'conversations'
    implicit_dir = ag / 'implicit'
    brain_dir = ag / 'brain'
    ann_dir = ag / 'annotations'

    conversations = []
    seen_uuids = set()

    # --- Build the candidate list (uuid -> base metadata) ---
    candidates = {}

    for s in summaries:
        uuid = s.get('uuid')
        summary = s.get('summary') or {}
        if not uuid:
            continue
        seen_uuids.add(uuid)
        candidates[uuid] = summary

    # .pb files not present in the summary index (e.g. unindexed/recovered)
    for uuid in _pb_uuid_set(conv_dir) - seen_uuids:
        candidates[uuid] = {}
        seen_uuids.add(uuid)

    # Conversations present in brain/ but missing everywhere else
    if brain_dir.is_dir():
        for sub in brain_dir.iterdir():
            if not sub.is_dir() or sub.name in seen_uuids:
                continue
            artifacts = _collect_brain_artifacts(sub)
            if not artifacts:
                continue
            candidates[sub.name] = {'_brain_only': artifacts}
            seen_uuids.add(sub.name)

    # --- Optional: fetch full messages via the language server API ---
    api_messages = {}
    if use_api and port is not None:
        cids = sorted(candidates.keys())
        if verbose:
            print(f"   📡 Fetching {len(cids)} conversations via LanguageServer…")
        with ThreadPoolExecutor(max_workers=4) as pool:
            futs = {
                pool.submit(_fetch_messages, port, cid, csrf_token, level): cid
                for cid in cids
            }
            for fut in as_completed(futs):
                cid = futs[fut]
                try:
                    msgs = fut.result()
                    if msgs:
                        api_messages[cid] = msgs
                except Exception:
                    continue
        stats['via_api'] = len(api_messages)

        # Resolve any remaining MODEL_PLACEHOLDER_M* models via the LSP model
        # catalog + a provider-keyed empirical map derived from this run.
        if api_messages:
            try:
                catalog = fetch_model_catalog(port, csrf_token)
                if verbose and catalog:
                    print(f"   📇 Model catalog from LSP: "
                          f"{len(catalog)} placeholder→model mappings")
            except Exception:
                catalog = {}
            stats['placeholder_resolved'] = \
                resolve_placeholder_models(api_messages, catalog)

    # --- Build conversation records ---
    for uuid, summary in candidates.items():
        brain_only = summary.pop('_brain_only', None)
        if brain_only is not None:
            summary = {}

        body_file = None
        body_size = None
        if conv_dir.is_dir():
            bf = conv_dir / f'{uuid}.pb'
            if bf.exists():
                body_file = str(bf)
                body_size = bf.stat().st_size
                stats['bodies'] += 1

        details = summary.get('details') or {}
        workspace = summary.get('workspace') or {}
        project_id = details.get('project_id')

        messages = api_messages.get(uuid, [])
        via_api = bool(messages)

        # Session-level dominant real model (recovered from generatorMetadata),
        # recorded on the meta file so consumers have a single model answer.
        session_model = None
        if messages:
            real = [m['model'] for m in messages
                    if isinstance(m.get('model'), str)
                    and not m['model'].startswith('MODEL_PLACEHOLDER')]
            if real:
                session_model = Counter(real).most_common(1)[0][0]

        conv = {
            'source': 'antigravity',
            'source_format': 'antigravity-ls-api' if via_api
            else 'antigravity-protobuf',
            'session_id': uuid,
            'title': summary.get('title'),
            'model': session_model,
            'message_count': summary.get('message_count') or len(messages) or None,
            'created_at': summary.get('created_at'),
            'updated_at': summary.get('updated_at'),
            'last_active_at': summary.get('last_active_at'),
            'workspace_uri': workspace.get('uri'),
            'git_branch': workspace.get('branch'),
            'git_remotes': workspace.get('git_remotes') or [],
            'agent_uuid': summary.get('agent_uuid') or details.get('agent_uuid'),
            'project_id': project_id,
            'body_encrypted': not via_api,
            'body_file': body_file,
            'body_size_bytes': body_size,
            'messages': messages,
        }

        if via_api:
            conv['note'] = ('Full conversation fetched via the official '
                            'language server API (decrypted).')
        else:
            conv['note'] = ('Conversation body could not be fetched '
                            '(no live language server / API failure); '
                            'encrypted body recorded with size.')

        if project_id and project_id in projects:
            conv['project'] = projects[project_id]

        # Plaintext annotations (text proto)
        if ann_dir.is_dir():
            ann = _parse_annotation_pbtxt(ann_dir / f'{uuid}.pbtxt')
            if ann:
                conv['last_user_view_time'] = ann

        # Plaintext brain artifacts (markdown plans + metadata)
        artifacts = brain_only or []
        if not artifacts and brain_dir.is_dir():
            artifacts = _collect_brain_artifacts(brain_dir / uuid)
        if artifacts:
            conv['brain_artifacts'] = artifacts
            stats['brain_artifacts'] += len(artifacts)

        conversations.append(conv)

    # --- Implicit (background/unsaved) conversations — encrypted bodies ---
    implicit = []
    if implicit_dir.is_dir():
        for pb in sorted(implicit_dir.glob('*.pb')):
            st = pb.stat()
            implicit.append({
                'source': 'antigravity-implicit',
                'source_format': 'antigravity-protobuf',
                'session_id': pb.stem,
                'title': None,
                'messages': [],
                'body_encrypted': True,
                'body_file': str(pb),
                'body_size_bytes': st.st_size,
                'body_mtime': datetime.fromtimestamp(
                    st.st_mtime, tz=timezone.utc
                ).isoformat().replace('+00:00', 'Z'),
                'note': 'Implicit/background conversation; body encrypted.',
            })
        stats['implicit'] = len(implicit)

    return conversations, implicit, stats


def main():
    print("="*80)
    print("ANTIGRAVITY (GEMINI 3) DATA EXTRACTION")
    print("="*80)
    print()

    # Argument parsing (stdlib only, mirrors other extract_*.py scripts)
    arg_no_ls = False
    arg_ls_binary = None
    arg_level = FieldLevel.FULL
    i = 1
    while i < len(sys.argv):
        a = sys.argv[i]
        if a in ('--help', '-h'):
            print("Usage: extract_antigravity.py [--no-ls] [--ls-binary PATH]")
            print("  --no-ls          skip language-server API (protobuf summary only)")
            print("  --ls-binary PATH language_server binary (or ANTIGRAVITY_LS_PATH)")
            print("  --basic          only user/assistant/tool content (no thinking/diffs)")
            return
        elif a == '--no-ls':
            arg_no_ls = True
        elif a == '--ls-binary':
            i += 1
            if i < len(sys.argv):
                arg_ls_binary = sys.argv[i]
        elif a == '--basic':
            arg_level = FieldLevel.DEFAULT
        else:
            print(f"  Warning: unknown flag, ignoring: {a}")
        i += 1

    print("🔍 Searching for Antigravity datastores...")
    datastores = find_antigravity_datastores()

    if not datastores:
        print("❌ No Antigravity datastores found!")
        return

    print(f"✅ Found {len(datastores)} datastore(s):")
    for label, path in datastores:
        print(f"   - {label}: {path}")
    print()

    # Acquire the language server once (reuse or launch)
    port = None
    csrf = None
    ls_proc = None
    if not arg_no_ls:
        print("🖥️  Locating Antigravity language server…")
        port, csrf, ls_proc = acquire_server(datastores[0][1],
                                             ls_binary=arg_ls_binary)
        if port is None:
            print("   ⚠️  No live language server — falling back to encrypted "
                  "summaries only (bodies will be flagged, not decrypted).")
        print()

    all_records = []
    per_store = {}

    for label, path in datastores:
        print(f"📂 Processing: {label} ({path})")

        convs, implicit, stats = extract_antigravity_store(
            path, port=port, csrf_token=csrf,
            use_api=port is not None, level=arg_level, verbose=True,
        )

        for c in convs + implicit:
            c['datastore'] = label
            c['installation'] = str(path)

        all_records.extend(convs + implicit)
        per_store[label] = (len(convs), len(implicit), stats)

        if convs or implicit or stats.get('summaries'):
            print(f"   🧠 {stats.get('summaries', 0)} summaries, "
                  f"{stats.get('bodies', 0)} encrypted bodies, "
                  f"{stats.get('implicit', 0)} implicit traces, "
                  f"{stats.get('brain_artifacts', 0)} brain artifacts")
            print(f"      → {len(convs)} conversation records "
                  f"({stats.get('via_api', 0)} decrypted via API, "
                  f"{stats.get('placeholder_resolved', 0)} placeholders "
                  f"resolved via catalog), {len(implicit)} implicit records")
        else:
            print(f"   ⚠️  No Antigravity data found here")

    print()
    print("="*80)
    print("EXTRACTION COMPLETE")
    print("="*80)
    print(f"Total records: {len(all_records):,}")

    if not all_records:
        print("No records found!")
        return

    convs_total = sum(c for c, _, _ in per_store.values())
    implicit_total = sum(i for _, i, _ in per_store.values())
    via_api_total = sum(s.get('via_api', 0) for _, _, s in per_store.values())
    print(f"Conversation records: {convs_total:,}")
    print(f"Implicit records: {implicit_total:,}")
    print(f"Decrypted via LanguageServer API: {via_api_total:,}")
    print()

    print("Breakdown by datastore:")
    for label, (nc, ni, _) in sorted(per_store.items(), key=lambda x: -x[1][0]):
        print(f"  {label:20} {nc:5,} conversations, {ni:5,} implicit")
    print()

    # Save: one HF-traces JSONL file per session (one message per line)
    from traces_export import write_session_files
    n_files, n_lines = write_session_files(all_records, 'antigravity')
    print(f"✅ Saved {n_files} session file(s) to extracted_data/antigravity/sessions/")
    print(f"   Total message lines: {n_lines:,}")
    print(f"   Format: HF-traces JSONL (one message per line, one file per session)")

    # Clean up a language server we launched ourselves (standalone LS never
    # writes a discovery file, so it can't be reused next run — terminate it
    # to avoid orphaned processes accumulating). Reused servers are untouched.
    if ls_proc is not None:
        try:
            ls_proc.terminate()
            try:
                ls_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                ls_proc.kill()
                ls_proc.wait()
        except OSError:
            pass
        # Remove the standalone log for this run now that the child has exited
        # (the log is named after the *parent* pid, so it is deterministic).
        try:
            (Path(datastores[0][1]) / 'daemon'
             / f'ls_{os.getpid()}.standalone.log').unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == '__main__':
    main()
