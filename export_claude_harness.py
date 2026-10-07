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
import re
from pathlib import Path

from sanitize import DepthLimitExceeded, Sanitizer, find_suspicious_tokens

_TEXT_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".sh", ".py"}
_CONFIG_TREES = ("skills", "commands", "agents", "memory")


def find_claude_installations():
    """Find all Claude Code installation directories."""
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
    # Home-rooted installations are probed separately. Nesting this inside the
    # loop above made ~/.claude undiscoverable whenever no platform base
    # directory happened to exist, which is the normal state in a container.
    for p in patterns:
        if (home / p).exists():
            found.append(home / p)
    return sorted(set(found), key=str)


def _read_text(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def _record(rel_path, content, redactions, warnings=None, source=None):
    return {
        "rel_path": rel_path,
        "content": content,
        "redactions": redactions,
        "warnings": warnings or [],
        "source": source,
    }


def _safe_component(name):
    """Opaque label for a path component that encodes a real filesystem path.

    Claude Code names each project directory after its working directory with
    the separators turned into dashes, so `projects/-Users-carol-repos-acme`
    is installation-relative and still spells out the operator's username and
    private project names. Relative is not the same as safe.
    """
    # Hash anything that is not a plain identifier. Keying on a leading dash or
    # a dash count let shallow and Windows-derived names like `D--work` through.
    if re.fullmatch(r"[A-Za-z0-9_]+", name):
        return name
    return "project-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]


def _rel(path, installation):
    """Installation-relative label for warnings.

    Warnings land in MANIFEST.json, which is the one bundle file that is never
    sanitized and the only file --dry-run emits, so an absolute path here would
    publish the operator's username.
    """
    try:
        rel = path.relative_to(installation)
    except ValueError:
        return path.name
    parts = list(rel.parts)
    if parts and parts[0] == "projects":
        # Every component after `projects`, including the last. A project
        # directory's relative path is only two parts, so exempting the last
        # one published the dash-encoded working directory verbatim.
        # Session files are named by uuid and carry no identity.
        parts = [parts[0]] + [p if p.endswith(".jsonl") else _safe_component(p)
                              for p in parts[1:]]
    return "/".join(parts)


def _outside_source(path, installation):
    """Resolved target when a symlink points out of the installation, else None."""
    try:
        resolved = path.resolve()
        resolved.relative_to(installation.resolve())
        return None
    except ValueError:
        return "<outside installation>"
    except OSError:
        return "<unresolvable symlink>"


def _reject_constant(name):
    raise ValueError("%s is not valid JSON" % name)


def _sanitize_json_file(path, rel_out, sanitizer, installation):
    raw = _read_text(path)
    if raw is None:
        return None
    label = _rel(path, installation)
    try:
        # json.loads itself recurses, so it belongs inside the guard too.
        # parse_constant rejects NaN/Infinity, which are not JSON and which no
        # conforming reader downstream could load back.
        obj = json.loads(raw, parse_constant=_reject_constant)
    except ValueError:
        # JSONDecodeError subclasses ValueError, as does the NaN/Infinity
        # rejection raised by parse_constant.
        return _record(rel_out, None, {},
                       ["%s is not valid JSON; skipped (cannot sanitize)" % label])
    except RecursionError:
        return _record(rel_out, None, {},
                       ["%s is nested too deeply to parse; skipped" % label])
    # scrub_json and json.dumps both recurse, so both belong inside the guard.
    try:
        scrubbed, counts = sanitizer.scrub_json(obj)
        content = json.dumps(scrubbed, indent=2, ensure_ascii=False)
    except (DepthLimitExceeded, RecursionError, ValueError, TypeError) as exc:
        return _record(rel_out, None, {},
                       ["%s could not be sanitized (%s); skipped"
                        % (label, type(exc).__name__)])
    return _record(rel_out, content, counts,
                   source=_outside_source(path, installation))


def _walk_tree(base, warnings, installation):
    """Yield files under base, following symlinks with loop protection.

    rglob silently omits unreadable and symlinked subdirectories. Dotfile
    harnesses managed by stow or chezmoi are almost entirely symlinks, so
    skipping them would drop most of the export.
    """
    def onerror(exc):
        name = getattr(exc, "filename", None)
        label = _rel(Path(name), installation) if name else "?"
        warnings.append("could not read %s; skipped" % label)

    for root, dirs, files in os.walk(base, onerror=onerror, followlinks=True):
        # Prune only a genuine cycle, where this directory resolves to one of
        # its own ancestors. A global visited-set keyed on realpath also pruned
        # a second, legitimate alias of an already-seen directory, dropping real
        # skills from the export with no warning.
        try:
            real = os.path.realpath(root)
            parent = os.path.dirname(root)
            looped = False
            while len(parent) >= len(str(base)):
                if os.path.realpath(parent) == real:
                    looped = True
                    break
                nxt = os.path.dirname(parent)
                if nxt == parent:
                    break
                parent = nxt
            if looped:
                warnings.append("%s is a symlink loop; not traversed"
                                % _rel(Path(root), installation))
                dirs[:] = []
                continue
        except OSError:
            continue
        dirs.sort()
        for name in sorted(files):
            yield Path(root) / name


def collect_config(installation, sanitizer):
    records = []
    all_warnings = []
    skipped_assets = {}

    for name, out in (("CLAUDE.md", "CLAUDE.md"),
                      ("CLAUDE.local.md", "CLAUDE.local.md")):
        p = installation / name
        if p.exists():
            text = _read_text(p)
            if text is not None:
                scrubbed, counts = sanitizer.scrub_text(text)
                records.append(_record(out, scrubbed, counts,
                                       source=_outside_source(p, installation)))
            else:
                all_warnings.append("could not read %s; skipped" % _rel(p, installation))

    for name, out in (("settings.json", "settings.sanitized.json"),
                      ("settings.local.json", "settings.local.sanitized.json"),
                      (".mcp.json", "mcp.sanitized.json")):
        p = installation / name
        if p.exists():
            rec = _sanitize_json_file(p, out, sanitizer, installation)
            if rec is None:
                all_warnings.append("could not read %s; skipped" % _rel(p, installation))
            elif rec["content"] is None:
                all_warnings.extend(rec["warnings"])
            else:
                records.append(rec)

    for tree in _CONFIG_TREES:
        base = installation / tree
        if not base.is_dir():
            continue
        for f in _walk_tree(base, all_warnings, installation):
            if f.is_symlink() and not f.exists():
                all_warnings.append("%s is a dangling symlink; skipped"
                                    % _rel(f, installation))
                continue
            if not f.is_file():
                continue
            if f.suffix.lower() not in _TEXT_SUFFIXES:
                # Silent omission would let an operator ship a skill whose
                # scripts are missing without anything saying so.
                skipped_assets.setdefault(tree, []).append(_rel(f, installation))
                continue
            text = _read_text(f)
            if text is None:
                all_warnings.append("could not read %s; skipped" % _rel(f, installation))
                continue
            rel = _rel(f, installation)
            if f.suffix.lower() == ".json":
                rec = _sanitize_json_file(f, rel, sanitizer, installation)
                if rec is None:
                    all_warnings.append("could not read %s; skipped" % rel)
                elif rec["content"] is None:
                    all_warnings.extend(rec["warnings"])
                else:
                    records.append(rec)
            else:
                scrubbed, counts = sanitizer.scrub_text(text)
                records.append(_record(rel, scrubbed, counts,
                                       source=_outside_source(f, installation)))

    for tree, paths in sorted(skipped_assets.items()):
        shown = ", ".join(paths[:5])
        more = "" if len(paths) <= 5 else " (+%d more)" % (len(paths) - 5)
        all_warnings.append(
            "skipped %d non-text asset(s) under %s/: %s%s"
            % (len(paths), tree, shown, more))

    if all_warnings:
        records.append(_record("", "", {}, all_warnings))

    return records


def _iter_session_files(installation, warnings):
    projects = installation / "projects"
    try:
        if projects.is_dir():
            entries = sorted(projects.iterdir())
        else:
            entries = None
    except OSError as exc:
        warnings.append("could not list projects/ (%s); skipped"
                        % type(exc).__name__)
        return

    if entries is None:
        try:
            for f in sorted(installation.glob("*.jsonl")):
                if not f.name.startswith("agent-"):
                    yield f
        except OSError as exc:
            warnings.append("could not list session files (%s); skipped"
                            % type(exc).__name__)
        return

    for proj in entries:
        try:
            if not proj.is_dir():
                continue
            # os.listdir, not Path.glob: pathlib swallows the OSError and
            # returns nothing, so an unreadable project vanished in silence.
            found = sorted(proj / n for n in os.listdir(proj)
                           if n.endswith(".jsonl"))
        except OSError as exc:
            # Silently dropping a whole project's sessions would leave the
            # operator believing they had been reviewed.
            warnings.append("could not list %s (%s); skipped"
                            % (_rel(proj, installation), type(exc).__name__))
            continue
        for f in found:
            if not f.name.startswith("agent-"):
                yield f


def _iter_json_lines(session_file, warnings, installation):
    """Yield parsed objects, warning about lines that could not be used."""
    text = _read_text(session_file)
    if text is None:
        warnings.append("could not read %s; skipped"
                        % _rel(session_file, installation))
        return
    bad = 0
    total = 0
    # split("\n"), not splitlines(): splitlines also breaks on U+2028, U+2029
    # and U+0085, which are legal inside a JSON string and destroyed the record.
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        total += 1
        try:
            obj = json.loads(line, parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            bad += 1
            continue
        if not isinstance(obj, dict):
            bad += 1
            continue
        yield obj
    if bad:
        warnings.append("%s: %d of %d line(s) were not usable JSON objects; skipped"
                        % (_rel(session_file, installation), bad, total))


# Events Claude Code injects into the user turn: skill payloads, slash-command
# expansions, command stdout, hook output and subagent notifications. They are
# machine output, not the operator's prompt patterns, and often carry file
# bodies verbatim.
_INJECTED_FLAGS = ("isMeta", "isSidechain", "isCompactSummary", "isVisibleInTranscriptOnly")
_INJECTED_KEYS = ("sourceToolUseID", "sourceToolAssistantUUID", "toolUseResult")
_INJECTED_PREFIXES = (
    "<task-notification>", "<system-reminder>", "<local-command-stdout>",
    "<local-command-stderr>", "<command-name>", "<command-message>",
    "<command-args>", "<user-prompt-submit-hook>", "<bash-input>",
    "<bash-stdout>", "<bash-stderr>", "Base directory for this skill",
    "Caveat: The messages below were generated by the user while running",
    "<function_results>", "<tool_use_error>", "<attachment", "<ide_",
    "[Request interrupted", "API Error", "Result of calling the",
    "The user opened the file", "This session is being continued from",
    "<local-command-caveat>", "<skill-instructions>",
)

# Wrappers that can appear after a leading caveat line rather than at position 0.
_INJECTED_TAGS = (
    "<task-notification>", "<system-reminder>", "<local-command-stdout>",
    "<local-command-stderr>", "<command-name>", "<user-prompt-submit-hook>",
    "<bash-stdout>", "<bash-stderr>", "<local-command-caveat>",
    "<function_results>", "<tool_use_error>",
)


def _is_injected_user_event(obj):
    if any(obj.get(f) for f in _INJECTED_FLAGS):
        return True
    return any(obj.get(k) is not None for k in _INJECTED_KEYS)


def _block_text(block):
    """Text of a content block, or '' when the field is not a string."""
    if not isinstance(block, dict):
        return ""
    t = block.get("text")
    return t if isinstance(t, str) else ""


def _is_injected_text(text):
    if not isinstance(text, str):
        return False
    # Claude Code often prefixes a wrapper with a short caveat line, so anchor
    # the scan to the head of the message rather than to position 0 exactly.
    stripped = text.lstrip()
    if stripped.startswith(_INJECTED_PREFIXES):
        return True
    # A wrapper can also be appended after a long operator prompt, so check the
    # tail as well. A 600-character head window missed exactly that shape.
    return any(tag in stripped[:4000] or tag in stripped[-2000:]
               for tag in _INJECTED_TAGS)


def _content_is_injected(content):
    """Injected-shape check across every content shape, not just plain strings."""
    if isinstance(content, str):
        return _is_injected_text(content)
    if isinstance(content, dict):
        # A non-text block is tool data, not an injected user turn. Leave it to
        # the strict filter so it is counted as a dropped content block.
        return _is_injected_text(_block_text(content))
    if isinstance(content, list):
        texts = [_block_text(b) for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return bool(texts) and all(_is_injected_text(t) for t in texts)
    return False


def _strip_injected_blocks(content):
    """Drop injected text blocks, keep the operator's own.

    Claude Code appends a system-reminder to genuine user turns, so an
    all-or-nothing verdict either exports the reminder or discards the prompt.
    """
    if not isinstance(content, list):
        return content
    return [b for b in content
            if not (isinstance(b, dict) and b.get("type") == "text"
                    and _is_injected_text(_block_text(b)))]


def _message_content(obj):
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return None
    return msg.get("content", "")


def _user_text(obj):
    """Return the operator's own prompt text from a JSONL 'user' event, or None."""
    if obj.get("type") != "user":
        return None
    if _is_injected_user_event(obj):
        return None
    content = _message_content(obj)
    # The string branch is the one command stdout and slash-command expansions
    # take, so the shape guard has to cover every shape, not just lists.
    if _content_is_injected(content):
        return None
    if isinstance(content, str):
        return content or None
    if isinstance(content, list):
        parts = [_block_text(b) for b in _strip_injected_blocks(content)
                 if isinstance(b, dict) and b.get("type") == "text"]
        joined = "\n".join(p for p in parts if p)
        return joined or None
    return None


def collect_prompts(installation, sanitizer):
    records = []
    counts = {"secrets": 0, "paths": 0, "emails": 0, "ips": 0}
    warnings = []
    dropped = {}
    for f in _iter_session_files(installation, warnings):
        session_id = f.stem
        for obj in _iter_json_lines(f, warnings, installation):
            if obj.get("type") != "user":
                continue
            ut = _user_text(obj)
            if not ut:
                if _message_content(obj):
                    dropped["injected_user_events"] = dropped.get(
                        "injected_user_events", 0) + 1
                continue
            scrubbed, c = sanitizer.scrub_text(ut)
            for k, v in c.items():
                counts[k] = counts.get(k, 0) + v
            records.append({"text": scrubbed, "source_session": session_id})
    return records, counts, dropped, warnings


_DROP_FIELDS = ("tool_use", "tool_uses", "tool_results",
                "suggested_diffs", "diff_histories", "code_context")


def _parse_messages(session_file, warnings, installation):
    """Reduce a session JSONL into role/content messages with optional tool fields."""
    messages = []
    for obj in _iter_json_lines(session_file, warnings, installation):
        t = obj.get("type")
        if t == "user":
            content = _message_content(obj)
            if content:
                msg = {"role": "user", "content": content,
                       "timestamp": obj.get("timestamp")}
                if _is_injected_user_event(obj) or _content_is_injected(content):
                    # Machine output wearing a user turn. Strict drops it; the
                    # prompts lane already excludes it everywhere.
                    msg["injected"] = True
                messages.append(msg)
        elif t == "assistant":
            content = _message_content(obj)
            if content is None:
                content = []
            text_parts, tool_uses, discarded = [], [], 0
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            text_parts.append(_block_text(item))
                        elif item.get("type") == "tool_use":
                            tool_uses.append(item)
                        else:
                            # thinking, redacted_thinking, images and anything
                            # else. Dropping them is right; dropping them
                            # without a tally understated what left behind.
                            discarded += 1
            elif isinstance(content, str):
                text_parts.append(content)
            msg = {"role": "assistant", "content": "\n".join(text_parts),
                   "timestamp": obj.get("timestamp")}
            if tool_uses:
                msg["tool_uses"] = tool_uses
            if discarded:
                msg["_discarded_blocks"] = discarded
            messages.append(msg)
        elif t == "tool_result":
            tr = obj.get("toolResult", {})
            if tr and messages:
                messages[-1].setdefault("tool_results", []).append(tr)
    return messages


def _strip_tool_content(m, dropped):
    """Strict mode: keep only positively-recognised text, count what was removed."""
    for field in _DROP_FIELDS:
        if field in m:
            value = m[field]
            # Count items, not messages, so every key in `dropped` means the
            # same thing. Only lists carry a meaningful length here.
            n = len(value) if isinstance(value, list) else 1
            dropped[field] = dropped.get(field, 0) + n
            del m[field]

    c = m.get("content")
    if isinstance(c, dict):
        c = [c]
    if isinstance(c, list):
        kept = [b for b in c if isinstance(b, dict) and b.get("type") == "text"]
        removed = len(c) - len(kept)
        if removed:
            dropped["content_blocks"] = dropped.get("content_blocks", 0) + removed
        # _block_text, not b.get("text"): a block typed "text" whose text field
        # is a dict used to abort the entire export with a TypeError in join.
        m["content"] = "\n".join(_block_text(b) for b in kept)
    elif not isinstance(c, str):
        # Anything strict cannot positively identify as text is dropped, not
        # passed through. An unrecognised shape is exactly where payloads hide.
        if c is not None:
            dropped["content_blocks"] = dropped.get("content_blocks", 0) + 1
        m["content"] = ""
    return m


def collect_conversations(installation, sanitizer, level):
    conversations = []
    counts = {"secrets": 0, "paths": 0, "emails": 0, "ips": 0}
    dropped = {}
    warnings = []
    for f in _iter_session_files(installation, warnings):
        messages = _parse_messages(f, warnings, installation)
        if not messages:
            continue
        clean = []
        for msg in messages:
            m = dict(msg)
            injected = m.pop("injected", False)
            discarded = m.pop("_discarded_blocks", 0)
            if discarded:
                dropped["assistant_blocks"] = dropped.get(
                    "assistant_blocks", 0) + discarded
            if level == "strict":
                if injected:
                    dropped["injected_user_events"] = dropped.get(
                        "injected_user_events", 0) + 1
                    continue
                m = _strip_tool_content(m, dropped)
            # One sanitizer for every asset class. A separate string-only walker
            # here used to leak credentials that the config lane redacted by key
            # name in the same bundle.
            try:
                m, c = sanitizer.scrub_json(m)
            except (DepthLimitExceeded, RecursionError) as exc:
                warnings.append("%s: a message was too deeply nested (%s); dropped"
                                % (_rel(f, installation), type(exc).__name__))
                dropped["unsanitizable_messages"] = dropped.get(
                    "unsanitizable_messages", 0) + 1
                continue
            for k, v in c.items():
                counts[k] = counts.get(k, 0) + v
            clean.append(m)
        conversations.append({
            "messages": clean, "source": "claude-code", "session_id": f.stem,
        })
    return conversations, counts, dropped, warnings


def _merge_counts(into, other):
    for k, v in other.items():
        into[k] = into.get(k, 0) + v


def _identity_tokens(installations):
    """Names that identify the operator: the home directory and install parents.

    Used only to flag bundle paths, never to rewrite file contents. Redacting
    every occurrence of the home directory name in content would be reckless
    when that name is an ordinary word.
    """
    tokens = set()
    try:
        home = Path.home()
    except (RuntimeError, OSError):
        home = None
    for candidate in ([home] if home else []) + [p.parent for p in installations]:
        name = candidate.name
        if name and len(name) >= 3 and not name.startswith("."):
            tokens.add(name)
    return {t for t in tokens if t not in ("Application Support", "usr", "opt")}


def _install_provenance(path):
    """Which discovery location an installation came from.

    A location class, never a filesystem name. A sha256 of the path is a
    brute-forceable oracle for the username, and the parent directory name IS
    the username for the standard ~/.claude layout.
    """
    try:
        home = Path.home()
    except (RuntimeError, OSError):
        return "other"
    parent = path.parent
    if parent == home:
        return "home"
    for probe, label in (("Library/Application Support", "app-support"),
                         (".config", "xdg-config"),
                         (".local/share", "local-share"),
                         ("AppData/Roaming", "appdata-roaming"),
                         ("AppData/Local", "appdata-local")):
        if parent == home / probe:
            return label
    return "other"


def _allocate_namespaces(installations):
    """Assign a unique bundle namespace to each installation.

    Suffixes must not collide with a basename that a later installation
    genuinely owns: [claude, claude, claude-2] previously produced claude-2
    twice and aborted the whole export.
    """
    installs = sorted(installations, key=lambda p: (p.name, str(p)))
    if len(installs) <= 1:
        return [(inst, "") for inst in installs]
    taken = {inst.name or "install" for inst in installs}
    used = set()
    out = []
    for inst in installs:
        base = inst.name or "install"
        if base not in used:
            label = base
        else:
            n = 2
            while "%s-%d" % (base, n) in used or "%s-%d" % (base, n) in taken:
                n += 1
            label = "%s-%d" % (base, n)
        used.add(label)
        out.append((inst, label + "/"))
    return out


def build_export(installations, include, level, use_detect_secrets, now):
    sanitizer = Sanitizer(use_detect_secrets=use_detect_secrets)
    files = {}
    redaction_counts = {"secrets": 0, "paths": 0, "emails": 0, "ips": 0}
    dropped_total = {}
    warnings = []
    file_entries = []
    install_entries = []

    def add_file(rel, content, redactions, source=None):
        if rel in files:
            raise ValueError("duplicate bundle path: %s" % rel)
        files[rel] = content
        data = content.encode("utf-8")
        entry = {
            "path": rel,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "redactions": redactions,
            "suspicious_tokens": len(find_suspicious_tokens(content)),
        }
        if source:
            entry["source"] = source
            # README tells the operator that warnings name symlink-sourced
            # files, so the warning has to actually be emitted.
            warnings.append(
                "%s was read through a symlink pointing %s" % (rel, source))
        file_entries.append(entry)

    for inst, ns in _allocate_namespaces(installations):
        install_entries.append({
            "namespace": ns.rstrip("/") or ".",
            "name": inst.name,
            "location": _install_provenance(inst),
        })

        if "config" in include:
            for rec in collect_config(inst, sanitizer):
                warnings.extend(rec["warnings"])
                if rec["content"] is None or not rec["rel_path"]:
                    continue
                add_file("config/" + ns + rec["rel_path"], rec["content"],
                         rec["redactions"], rec.get("source"))
                _merge_counts(redaction_counts, rec["redactions"])

        if "prompts" in include:
            recs, counts, dropped, warns = collect_prompts(inst, sanitizer)
            warnings.extend(warns)
            _merge_counts(dropped_total, dropped)
            if recs:
                content = "\n".join(json.dumps(r, ensure_ascii=False) for r in recs)
                add_file("prompts/" + ns + "prompt_patterns.jsonl", content, counts)
                _merge_counts(redaction_counts, counts)

        if "conversations" in include:
            convs, counts, dropped, warns = collect_conversations(inst, sanitizer, level)
            warnings.extend(warns)
            _merge_counts(dropped_total, dropped)
            if convs:
                content = "\n".join(json.dumps(c, ensure_ascii=False) for c in convs)
                add_file("conversations/" + ns + "conversations.sanitized.jsonl",
                         content, counts)
                _merge_counts(redaction_counts, counts)

    # Bundle paths are published in the manifest and on disk but never pass
    # through scrub_text, so a sensitive file name has to be flagged separately.
    # Renaming is not an option: scrubbing a path eats the extension and can
    # collide two siblings into one.
    identity = _identity_tokens(installations)
    for rel in sorted(files):
        # Scan components, not the whole path: the token class includes "/", so
        # any ordinary nested path reads as one long high-entropy run.
        leaked = any(find_suspicious_tokens(part) for part in rel.split("/"))
        scrubbed, _counts = sanitizer.scrub_text(rel)
        # Renaming is not an option here: scrubbing a path eats the extension
        # and can collide two siblings. A file named after the operator is
        # therefore reported rather than rewritten.
        named = sorted(t for t in identity if t.lower() in rel.lower())
        if named:
            warnings.append(
                "bundle path contains the operator's name (%s), which is NOT "
                "redacted in file names: %s" % (", ".join(named), rel))
        elif scrubbed != rel or leaked:
            warnings.append(
                "bundle path may itself carry sensitive content: %s" % rel)

    flagged = [e["path"] for e in file_entries if e["suspicious_tokens"]]
    if flagged:
        total = sum(e["suspicious_tokens"] for e in file_entries)
        shown = sorted(flagged)[:10]
        more = "" if len(flagged) <= 10 else \
            " (and %d more; see files[].suspicious_tokens)" % (len(flagged) - 10)
        warnings.append(
            "%d high-entropy token(s) survived redaction in %d file(s); review %s%s"
            % (total, len(flagged), ", ".join(shown), more))

    if sanitizer.detect_secrets_status == "used" and sanitizer.detect_secrets_errors:
        warnings.append(
            "detect-secrets raised on %d line(s); those lines had built-in "
            "patterns only" % sanitizer.detect_secrets_errors)

    # The prompts and conversations lanes parse the same session files, so an
    # unusable line is reported by both. Keep first-seen order.
    warnings = list(dict.fromkeys(warnings))

    # Backstop: the identity scan above only inspects bundle paths, so sweep the
    # finished manifest too. Warning text and provenance fields have both leaked
    # the operator's name in the past.
    draft = json.dumps({"installations": install_entries, "warnings": warnings,
                        "files": file_entries}, ensure_ascii=False)
    exposed = sorted(t for t in identity if t in draft)
    if exposed:
        warnings.append(
            "MANIFEST.json itself mentions the operator's name (%s); review it "
            "before sharing" % ", ".join(exposed))

    manifest = {
        "generated_at": now,
        "tool": "claude-code",
        "asset_classes": sorted(include),
        "redaction_level": level,
        "detect_secrets": sanitizer.detect_secrets_status,
        "installations": install_entries,
        "redaction_counts": redaction_counts,
        "dropped": dropped_total,
        "files": file_entries,
        "warnings": warnings,
    }
    return {"manifest": manifest, "files": files}


def write_bundle(export, output_dir, dry_run):
    output_dir = Path(output_dir)
    base = output_dir / ("claude_harness_export_" + _timestamp())
    # A per-second directory name with exist_ok=True let two back-to-back runs
    # share a bundle: the second manifest described only its own files while the
    # first run's content sat beside it, unlisted and unhashed.
    bundle = base
    n = 2
    # Bounded: a dangling symlink at the target makes mkdir raise FileExistsError
    # forever, and an unbounded loop spun at 100% CPU with no output.
    while True:
        try:
            bundle.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            if n > 100:
                raise FileExistsError(
                    "could not create a bundle directory under %s; %s already "
                    "exists (a broken symlink there will also cause this)"
                    % (output_dir, bundle))
            bundle = Path(str(base) + "_%d" % n)
            n += 1
    if not dry_run:
        expected = {e["path"]: (e["bytes"], e["sha256"])
                    for e in export["manifest"]["files"]}
        for rel, content in export["files"].items():
            dest = bundle / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
        # Read back and compare against what the manifest already claims. On a
        # case-insensitive filesystem two paths differing only by case collapse
        # into one file, and the manifest would otherwise describe a file that
        # is not there.
        mismatched = []
        for rel, (size, digest) in expected.items():
            try:
                data = (bundle / rel).read_bytes()
            except OSError:
                mismatched.append(rel)
                continue
            if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
                mismatched.append(rel)
        if mismatched:
            export["manifest"]["warnings"].append(
                "%d file(s) on disk do not match their manifest entry, most "
                "likely a case-insensitive filename collision: %s"
                % (len(mismatched), ", ".join(sorted(mismatched)[:10])))
    # Written last, so a bundle never exists on disk with a manifest that
    # under-describes it.
    (bundle / "MANIFEST.json").write_text(
        json.dumps(export["manifest"], indent=2, ensure_ascii=False), encoding="utf-8")
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
    try:
        bundle = write_bundle(export, args.output, dry_run=args.dry_run)
    except OSError as exc:
        print("Could not write the bundle to %s: %s" % (args.output, exc))
        return 1

    m = export["manifest"]
    print("Manifest: %s" % (bundle / "MANIFEST.json"))
    print("Redactions: %s" % m["redaction_counts"])
    if m["dropped"]:
        print("Dropped: %s" % m["dropped"])
    for w in m["warnings"]:
        print("WARNING: %s" % w)
    if args.dry_run:
        print("Dry run: no content written. Review the manifest, then re-run without --dry-run.")
    else:
        print("Bundle written to: %s" % bundle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
