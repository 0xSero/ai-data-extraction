#!/usr/bin/env python3
"""
Enterprise-safe export of a Claude Code harness: portable config, memory,
skills, prompt patterns, and optionally sanitized conversations.
All processing is local. Standard library only.
"""

import argparse
import hashlib
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
            else:
                all_warnings.append("could not read %s; skipped" % p)

    for name, out in (("settings.json", "settings.sanitized.json"),
                      ("settings.local.json", "settings.local.sanitized.json"),
                      (".mcp.json", "mcp.sanitized.json")):
        p = installation / name
        if p.exists():
            rec = _sanitize_json_file(p, out, sanitizer)
            if rec is None:
                all_warnings.append("could not read %s; skipped" % p)
            elif rec["content"] is None:
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
                all_warnings.append("could not read %s; skipped" % f)
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


def _iter_session_files(installation):
    projects = installation / "projects"
    if projects.exists():
        for proj in projects.iterdir():
            if proj.is_dir():
                for f in proj.glob("*.jsonl"):
                    if not f.name.startswith("agent-"):
                        yield f
    else:
        for f in installation.glob("*.jsonl"):
            if not f.name.startswith("agent-"):
                yield f


def _user_text(obj):
    """Return the plain user prompt text from a JSONL 'user' event, or None."""
    if obj.get("type") != "user":
        return None
    content = obj.get("message", {}).get("content", "")
    if isinstance(content, str):
        return content or None
    # content can be a list of blocks; keep only text blocks
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        joined = "\n".join(p for p in parts if p)
        return joined or None
    return None


def collect_prompts(installation, sanitizer):
    records = []
    counts = {"secrets": 0, "paths": 0, "emails": 0, "ips": 0}
    for f in _iter_session_files(installation):
        session_id = f.stem
        text_lines = _read_text(f)
        if text_lines is None:
            continue
        for line in text_lines.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            ut = _user_text(obj)
            if not ut:
                continue
            scrubbed, c = sanitizer.scrub_text(ut)
            for k, v in c.items():
                counts[k] += v
            records.append({"text": scrubbed, "source_session": session_id})
    return records, counts


_DROP_FIELDS = ("tool_use", "tool_uses", "tool_results",
                "suggested_diffs", "diff_histories", "code_context")


def _sanitize_obj(obj, sanitizer, counts):
    """Recursively sanitize every string in a JSON-like object, merging counts."""
    if isinstance(obj, dict):
        return {k: _sanitize_obj(v, sanitizer, counts) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_obj(v, sanitizer, counts) for v in obj]
    if isinstance(obj, str):
        scrubbed, c = sanitizer.scrub_text(obj)
        for k, v in c.items():
            counts[k] += v
        return scrubbed
    return obj


def _parse_messages(session_file):
    """Reduce a session JSONL into simple role/content messages with optional tool fields."""
    messages = []
    text = _read_text(session_file)
    if text is None:
        return messages
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = obj.get("type")
        if t == "user":
            content = obj.get("message", {}).get("content", "")
            if content:
                messages.append({"role": "user", "content": content,
                                 "timestamp": obj.get("timestamp")})
        elif t == "assistant":
            content = obj.get("message", {}).get("content", [])
            text_parts, tool_uses = [], []
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            text_parts.append(item.get("text", ""))
                        elif item.get("type") == "tool_use":
                            tool_uses.append(item)
            elif isinstance(content, str):
                text_parts.append(content)
            msg = {"role": "assistant", "content": "\n".join(text_parts),
                   "timestamp": obj.get("timestamp")}
            if tool_uses:
                msg["tool_uses"] = tool_uses
            messages.append(msg)
        elif t == "tool_result":
            tr = obj.get("toolResult", {})
            if tr and messages:
                messages[-1].setdefault("tool_results", []).append(tr)
    return messages


def collect_conversations(installation, sanitizer, level):
    conversations = []
    counts = {"secrets": 0, "paths": 0, "emails": 0, "ips": 0}
    dropped = {}
    for f in _iter_session_files(installation):
        messages = _parse_messages(f)
        if not messages:
            continue
        clean = []
        for msg in messages:
            m = dict(msg)
            if level == "strict":
                for field in _DROP_FIELDS:
                    if field in m:
                        dropped[field] = dropped.get(field, 0) + 1
                        del m[field]
                # Drop tool-output blocks embedded in user content lists
                c = m.get("content")
                if isinstance(c, list):
                    kept = [b for b in c
                            if isinstance(b, dict) and b.get("type") == "text"]
                    removed = len(c) - len(kept)
                    if removed:
                        dropped["content_blocks"] = dropped.get("content_blocks", 0) + removed
                    m["content"] = "\n".join((b.get("text") or "") for b in kept)
                m = _sanitize_obj(m, sanitizer, counts)
            else:  # balanced: keep fields, sanitize everything
                m = _sanitize_obj(m, sanitizer, counts)
            clean.append(m)
        conversations.append({
            "messages": clean, "source": "claude-code", "session_id": f.stem,
        })
    return conversations, counts, dropped


def _merge_counts(into, other):
    for k, v in other.items():
        into[k] = into.get(k, 0) + v


def build_export(installations, include, level, use_detect_secrets, now):
    sanitizer = Sanitizer(use_detect_secrets=use_detect_secrets)
    files = {}
    redaction_counts = {"secrets": 0, "paths": 0, "emails": 0, "ips": 0}
    dropped_total = {}
    warnings = []
    file_entries = []

    def add_file(rel, content, redactions):
        if rel in files:
            raise ValueError("duplicate bundle path: %s" % rel)
        files[rel] = content
        data = content.encode("utf-8")
        file_entries.append({
            "path": rel,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "redactions": redactions,
        })

    multi = len(installations) > 1
    used_ns = {}
    for inst in installations:
        if multi:
            base = inst.name or "install"
            n = used_ns.get(base, 0)
            used_ns[base] = n + 1
            ns = (base if n == 0 else "%s-%d" % (base, n + 1)) + "/"
        else:
            ns = ""

        if "config" in include:
            for rec in collect_config(inst, sanitizer):
                warnings.extend(rec["warnings"])
                if rec["content"] is None or not rec["rel_path"]:
                    continue
                add_file("config/" + ns + rec["rel_path"], rec["content"], rec["redactions"])
                _merge_counts(redaction_counts, rec["redactions"])

        if "prompts" in include:
            recs, counts = collect_prompts(inst, sanitizer)
            if recs:
                content = "\n".join(json.dumps(r, ensure_ascii=False) for r in recs)
                add_file("prompts/" + ns + "prompt_patterns.jsonl", content, counts)
                _merge_counts(redaction_counts, counts)

        if "conversations" in include:
            convs, counts, dropped = collect_conversations(inst, sanitizer, level)
            if convs:
                content = "\n".join(json.dumps(c, ensure_ascii=False) for c in convs)
                add_file("conversations/" + ns + "conversations.sanitized.jsonl", content, counts)
                _merge_counts(redaction_counts, counts)
                _merge_counts(dropped_total, dropped)

    # High-entropy leftovers across everything that will be written
    suspicious = 0
    for content in files.values():
        suspicious += len(find_suspicious_tokens(content))
    if suspicious:
        warnings.append(
            "%d high-entropy token(s) survived redaction; review before sharing" % suspicious)

    manifest = {
        "generated_at": now,
        "tool": "claude-code",
        "asset_classes": sorted(include),
        "redaction_level": level,
        "detect_secrets": sanitizer.detect_secrets_status,
        "redaction_counts": redaction_counts,
        "dropped": dropped_total,
        "files": file_entries,
        "warnings": warnings,
    }
    return {"manifest": manifest, "files": files}


def write_bundle(export, output_dir, dry_run):
    output_dir = Path(output_dir)
    bundle = output_dir / ("claude_harness_export_" + _timestamp())
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "MANIFEST.json").write_text(
        json.dumps(export["manifest"], indent=2, ensure_ascii=False), encoding="utf-8")
    if not dry_run:
        for rel, content in export["files"].items():
            dest = bundle / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
    return bundle


def _timestamp():
    from datetime import datetime
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _now_iso():
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Enterprise-safe export of a Claude Code harness.")
    p.add_argument("--include", default="config,prompts",
                   help="Comma list of config,prompts,conversations (default: config,prompts)")
    p.add_argument("--exclude", default="",
                   help="Comma list to remove from the include set")
    p.add_argument("--redact-level", default="strict", choices=["strict", "balanced"])
    p.add_argument("--dry-run", action="store_true",
                   help="Emit MANIFEST.json only; write no bundle content")
    p.add_argument("--output", default="extracted_data", help="Output directory")
    p.add_argument("--no-detect-secrets", action="store_true",
                   help="Do not use detect_secrets even if installed")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    include = {x.strip() for x in args.include.split(",") if x.strip()}
    include -= {x.strip() for x in args.exclude.split(",") if x.strip()}
    valid = {"config", "prompts", "conversations"}
    include &= valid
    if not include:
        print("Nothing to export: --include resolved to an empty set.")
        return 1

    installations = find_claude_installations()
    if not installations:
        print("No Claude Code installations found.")
        return 1

    print("Found %d installation(s). Exporting: %s (level=%s)%s" % (
        len(installations), ", ".join(sorted(include)), args.redact_level,
        " [DRY RUN]" if args.dry_run else ""))

    export = build_export(
        installations, include, args.redact_level,
        use_detect_secrets=not args.no_detect_secrets, now=_now_iso())
    bundle = write_bundle(export, args.output, dry_run=args.dry_run)

    m = export["manifest"]
    print("Manifest: %s" % (bundle / "MANIFEST.json"))
    print("Redactions: %s" % m["redaction_counts"])
    if m["dropped"]:
        print("Dropped (strict): %s" % m["dropped"])
    for w in m["warnings"]:
        print("WARNING: %s" % w)
    if args.dry_run:
        print("Dry run: no content written. Review the manifest, then re-run without --dry-run.")
    else:
        print("Bundle written to: %s" % bundle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
