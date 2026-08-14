#!/usr/bin/env python3
"""
Enterprise-safe export of a Claude Code harness: portable config, memory,
skills, prompt patterns, and optionally sanitized conversations.
All processing is local. Standard library only.
"""

import json
import os
import platform
from pathlib import Path

from sanitize import Sanitizer, find_suspicious_tokens

_TEXT_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".sh", ".py"}
_CONFIG_TREES = ("skills", "commands", "memory")


def find_claude_installations():
    """Find all Claude Code installation directories (copied from extract_claude_code)."""
    system = platform.system()
    home = Path.home()
    if system == "Darwin":
        base_dirs = [home / "Library/Application Support", home / ".config"]
    elif system == "Linux":
        base_dirs = [home / ".config", home / ".local/share"]
    elif system == "Windows":
        base_dirs = [
            Path(os.environ.get("APPDATA", home / "AppData/Roaming")),
            Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local")),
        ]
    else:
        base_dirs = [home / ".config"]

    patterns = [
        "claude", "claude-code", "claude-local", "claude-m2", "claude-zai",
        ".claude", ".claude-code", ".claude-local", ".claude-m2", ".claude-zai",
    ]
    found = []
    for base_dir in base_dirs:
        if not base_dir.exists():
            continue
        for p in patterns:
            if (base_dir / p).exists():
                found.append(base_dir / p)
            if (home / p).exists():
                found.append(home / p)
    return list(set(found))


def _read_text(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def _record(rel_path, content, redactions, warnings=None):
    return {
        "rel_path": rel_path,
        "content": content,
        "redactions": redactions,
        "warnings": warnings or [],
    }


def _sanitize_json_file(path, rel_out, sanitizer):
    raw = _read_text(path)
    if raw is None:
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return _record(rel_out, None, {}, ["%s is not valid JSON; skipped (cannot sanitize)" % path.name])
    scrubbed, counts = sanitizer.scrub_json(obj)
    return _record(rel_out, json.dumps(scrubbed, indent=2, ensure_ascii=False), counts)


def collect_config(installation, sanitizer):
    records = []
    all_warnings = []

    # Top-level single files
    for name, out in (("CLAUDE.md", "CLAUDE.md"),
                      ("CLAUDE.local.md", "CLAUDE.local.md")):
        p = installation / name
        if p.exists():
            text = _read_text(p)
            if text is not None:
                scrubbed, counts = sanitizer.scrub_text(text)
                records.append(_record(out, scrubbed, counts))

    for name, out in (("settings.json", "settings.sanitized.json"),
                      ("settings.local.json", "settings.local.sanitized.json"),
                      (".mcp.json", "mcp.sanitized.json")):
        p = installation / name
        if p.exists():
            rec = _sanitize_json_file(p, out, sanitizer)
            if rec is not None:
                if rec["content"] is None:
                    # Unparseable JSON - collect warning but don't add record
                    all_warnings.extend(rec["warnings"])
                else:
                    records.append(rec)

    # Recursive text trees
    for tree in _CONFIG_TREES:
        base = installation / tree
        if not base.is_dir():
            continue
        for f in sorted(base.rglob("*")):
            if not f.is_file() or f.suffix.lower() not in _TEXT_SUFFIXES:
                continue
            text = _read_text(f)
            if text is None:
                continue
            rel = f.relative_to(installation).as_posix()
            if f.suffix.lower() == ".json":
                rec = _sanitize_json_file(f, rel, sanitizer)
                if rec is not None:
                    if rec["content"] is None:
                        # Unparseable JSON - collect warning but don't add record
                        all_warnings.extend(rec["warnings"])
                    else:
                        records.append(rec)
            else:
                scrubbed, counts = sanitizer.scrub_text(text)
                records.append(_record(rel, scrubbed, counts))

    # Add accumulated warnings as a meta-record if any
    if all_warnings:
        records.append(_record("", "", {}, all_warnings))

    return records
