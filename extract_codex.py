#!/usr/bin/env python3
"""
Extract ALL Codex chat data from all installations.
Includes: messages, reasoning/thoughts, tool calls & results, diffs, code context.

Auto-discovers Codex installations on the device and supports every on-disk
layout that openai/codex has used:

  1. Classic CLI layout:    ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
                            ~/.codex/archived_sessions/YYYY/MM/DD/rollout-*.jsonl
  2. Thread index:          ~/.codex/state_5.sqlite (threads.rollout_path) — used
                            both to discover rollout files and to enrich metadata
                            (title, cwd, model, git branch, timestamps, tokens).
  3. Desktop app (Windows): %APPDATA%\\Codex and %LOCALAPPDATA%\\OpenAI\\Codex
                            (same rollout layout under the app's data dirs).

Rollout JSONL event schema handled (one JSON object per line):
  - session_meta            first line: id, timestamp, cwd, model, provider, git...
  - event_msg               payload.type: user_message, agent_message,
                            agent_reasoning, tool_use, tool_result, diff,
                            user_review
  - response_item           payload.type: message, reasoning, function_call,
                            function_call_output (Responses-API style events)
  - meta                    housekeeping (model_update, agent_message, ...)
  - turn_context / token_count    token-usage bookkeeping

Note on logs_2.sqlite / memories_*.sqlite / goals_*.sqlite: these hold debug
tracing, background-memory jobs and thread-goal state — NOT conversation text —
so they are intentionally not treated as chat sources.
"""

import json
import sqlite3
from pathlib import Path
import platform
import os


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

def find_codex_installations():
    """Find all Codex installation/data directories on this device."""
    system = platform.system()
    home = Path.home()

    locations = []
    codex_patterns = ['codex', 'codex-local', '.codex', '.codex-local']

    if system == "Darwin":  # macOS
        base_dirs = [
            home / "Library/Application Support",
            home / ".config",
            home,
        ]
        # macOS desktop app keeps its data in a capitalised dir.
        if (home / 'Library/Application Support' / 'Codex').exists():
            locations.append(home / 'Library/Application Support' / 'Codex')
    elif system == "Linux":
        base_dirs = [
            home / ".config",
            home / ".local/share",
            home,
        ]
    elif system == "Windows":
        base_dirs = [
            Path(os.environ.get('APPDATA', home / 'AppData/Roaming')),
            Path(os.environ.get('LOCALAPPDATA', home / 'AppData/Local')),
            home,
        ]
        # Codex desktop app (Windows) keeps its data in a differently-cased dir.
        for extra in (home / 'AppData/Roaming' / 'Codex',
                      home / 'AppData/Local' / 'OpenAI' / 'Codex'):
            if extra.exists():
                locations.append(extra)
    else:
        base_dirs = [home / ".config", home]

    for base_dir in base_dirs:
        if not base_dir.exists():
            continue
        for pattern in codex_patterns:
            codex_dir = base_dir / pattern
            if codex_dir.exists():
                locations.append(codex_dir)

    return sorted(set(locations))


def _read_thread_index(installation):
    """Read threads.rollout_path from state_*.sqlite (the Codex thread index).

    Returns {rollout_path_str: thread_row_dict} so we can discover rollout
    files that live outside the standard sessions/ tree AND enrich the
    conversation with index metadata.
    """
    threads = {}
    for db_name in ('state_5.sqlite', 'state_4.sqlite', 'state.sqlite'):
        db = installation / db_name
        if not db.exists():
            continue
        try:
            # db.resolve().as_uri() yields a proper file:/// URI — a raw
            # Windows path (backslashes, spaces) is NOT a valid file: URI
            # and would silently fail to open.
            conn = sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)
            try:
                conn.row_factory = sqlite3.Row
                cols = [c[1] for c in conn.execute('PRAGMA table_info(threads)')]
                if 'rollout_path' not in cols:
                    continue
                for r in conn.execute('SELECT * FROM threads'):
                    row = dict(r)
                    path = row.get('rollout_path')
                    if path:
                        threads.setdefault(str(path), row)
            finally:
                conn.close()
        except Exception as e:
            print(f"    (could not read thread index {db_name}: {e})")
    return threads


def find_all_codex_sessions(installation):
    """Find all Codex rollout session files.

    Returns a list of (session_file_path, thread_meta_or_None).
    """
    session_files = []

    # Canonical CLI layout: sessions/YYYY/MM/DD/rollout-*.jsonl
    sessions_dir = installation / 'sessions'
    if sessions_dir.exists():
        session_files.extend(sorted(sessions_dir.rglob('rollout-*.jsonl')))

    # Archived threads
    archived_dir = installation / 'archived_sessions'
    if archived_dir.exists():
        session_files.extend(sorted(archived_dir.rglob('rollout-*.jsonl')))

    # Legacy / project-based structure
    projects_dir = installation / 'projects'
    if projects_dir.exists():
        session_files.extend(sorted(projects_dir.rglob('*.jsonl')))

    # Thread index may point at rollout files anywhere (incl. custom paths)
    thread_index = _read_thread_index(installation)
    known = set(session_files)
    for path_str in thread_index:
        p = Path(path_str)
        # Match by path identity (not raw string) so slash-style differences
        # between the DB and this platform don't cause a missed lookup.
        if p.exists() and p not in known:
            session_files.append(p)
            known.add(p)

    return [(p, thread_index.get(str(p)) or next(
        (meta for key, meta in thread_index.items() if Path(key) == p), None))
        for p in session_files]


# --------------------------------------------------------------------------
# Rollout parsing
# --------------------------------------------------------------------------

def _norm_text(value):
    """Coerce any value to a string, JSON-encoding structured data."""
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _content_from_parts(content):
    """Turn a Responses-API content array into a plain string."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        # Structured message objects: {'content': [...]} or {'text': '...'}
        if 'content' in content:
            return _content_from_parts(content['content'])
        if 'text' in content:
            return _norm_text(content['text'])
        return _norm_text(content)
    if isinstance(content, list):
        texts = []
        for part in content:
            if isinstance(part, str):
                texts.append(part)
            elif isinstance(part, dict):
                ptype = part.get('type')
                if ptype in ('output_text', 'input_text', 'text'):
                    texts.append(_norm_text(part.get('text')))
                elif 'text' in part:
                    texts.append(_norm_text(part.get('text')))
        return '\n'.join(t for t in texts if t)
    return _norm_text(content)


def _attach_tool_calls(messages, calls):
    """Attach pending tool calls to the most recent assistant message.

    Returns True if they were attached to an existing assistant message;
    False if no assistant message exists yet (the caller may then attach
    them to the message currently being built).
    """
    if not calls:
        return False
    for m in reversed(messages):
        if m.get('role') == 'assistant':
            m.setdefault('tool_calls', []).extend(calls)
            return True
    return False


def extract_codex_session(session_file, thread_meta=None):
    """Extract a conversation from a Codex rollout file.

    Returns a conversation dict, or None if the file has no extractable
    conversation content.
    """
    messages = []
    tool_results = []
    diffs = []
    session_meta = dict(thread_meta or {})

    pending_reasoning = []          # reasoning text awaiting the next assistant msg
    pending_tool_calls = []         # tool calls awaiting the last assistant msg
    token_usage = {}

    def flush_reasoning(target_msg):
        """Attach buffered reasoning to the given assistant message dict."""
        nonlocal pending_reasoning
        if not pending_reasoning:
            return
        target_msg.setdefault('thoughts', []).extend(pending_reasoning)
        pending_reasoning = []

    try:
        fh = open(session_file, 'r', encoding='utf-8')
    except OSError:
        return None

    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue

            event_type = obj.get('type')
            ts = obj.get('timestamp')

            if event_type == 'session_meta':
                payload = obj.get('payload') or {}
                if isinstance(payload, dict):
                    session_meta.update(payload)
                continue

            if event_type == 'event_msg':
                payload = obj.get('payload') or {}
                if not isinstance(payload, dict):
                    continue
                ptype = payload.get('type')

                if ptype == 'user_message':
                    text = _content_from_parts(payload.get('message', '')).strip()
                    if text:
                        msg = {
                            'role': 'user',
                            'content': text,
                            'timestamp': ts,
                        }
                        if 'context' in payload:
                            msg['context'] = payload['context']
                        messages.append(msg)

                elif ptype == 'agent_message':
                    text = _content_from_parts(payload.get('message', '')).strip()
                    if text:
                        msg = {
                            'role': 'assistant',
                            'content': text,
                            'timestamp': ts,
                        }
                        if 'model' in payload:
                            msg['model'] = payload['model']
                        elif 'model' in session_meta:
                            msg['model'] = session_meta.get('model')
                        # Tool calls from the previous turn belong to the
                        # previous assistant message; if none exists yet they
                        # belong to this one (message-last turn ordering).
                        if pending_tool_calls:
                            if not _attach_tool_calls(messages, pending_tool_calls):
                                msg['tool_calls'] = list(pending_tool_calls)
                            pending_tool_calls.clear()
                        flush_reasoning(msg)
                        messages.append(msg)

                elif ptype == 'agent_reasoning':
                    text = _norm_text(payload.get('message', '')).strip()
                    if text:
                        pending_reasoning.append(text)

                elif ptype == 'tool_use':
                    pending_tool_calls.append({
                        'name': payload.get('tool'),
                        'input': payload.get('input'),
                        'call_id': payload.get('call_id'),
                        'timestamp': ts,
                    })

                elif ptype == 'tool_result':
                    result = {
                        'type': 'tool_result',
                        'tool': payload.get('tool'),
                        'call_id': payload.get('call_id'),
                        'output': payload.get('output'),
                        'timestamp': ts,
                    }
                    tool_results.append(result)
                    # Represent as a tool-role message for uniform export
                    out_text = _norm_text(payload.get('output', ''))
                    tmsg = {
                        'role': 'tool',
                        'tool_name': payload.get('tool'),
                        'tool_call_id': payload.get('call_id'),
                        'content': out_text,
                        'status': 'success' if payload.get('status') != 'error' else 'error',
                        'timestamp': ts,
                    }
                    messages.append(tmsg)

                elif ptype == 'diff':
                    diffs.append({
                        'type': 'diff',
                        'file': payload.get('file'),
                        'diff': payload.get('diff'),
                        'timestamp': ts,
                    })

                elif ptype == 'user_review':
                    text = _norm_text(payload.get('review', '')).strip()
                    if text:
                        messages.append({
                            'role': 'user',
                            'content': text,
                            'timestamp': ts,
                            'kind': 'review',
                        })

                continue

            if event_type == 'response_item':
                payload = obj.get('payload') or {}
                if not isinstance(payload, dict):
                    continue
                rtype = payload.get('type')

                if rtype == 'message':
                    text = _content_from_parts(payload.get('content')).strip()
                    role = payload.get('role', 'assistant')
                    if text or pending_reasoning or pending_tool_calls:
                        msg = {
                            'role': 'assistant' if role != 'user' else 'user',
                            'content': text,
                            'timestamp': ts,
                        }
                        model = payload.get('model') or session_meta.get('model')
                        if model:
                            msg['model'] = model
                        # Tool calls from the previous turn belong to the
                        # previous assistant message; if none exists yet they
                        # belong to this one (message-last turn ordering).
                        if pending_tool_calls:
                            if not _attach_tool_calls(messages, pending_tool_calls):
                                msg['tool_calls'] = list(pending_tool_calls)
                            pending_tool_calls.clear()
                        if msg['role'] == 'assistant':
                            flush_reasoning(msg)
                        messages.append(msg)

                elif rtype == 'reasoning':
                    text = ''
                    summary = payload.get('summary')
                    if isinstance(summary, list):
                        for part in summary:
                            if isinstance(part, dict):
                                text += _norm_text(part.get('text', ''))
                    if not text:
                        text = _norm_text(payload.get('text', ''))
                    if text.strip():
                        pending_reasoning.append(text.strip())

                elif rtype == 'function_call':
                    pending_tool_calls.append({
                        'name': payload.get('name'),
                        'arguments': payload.get('arguments'),
                        'input': payload.get('arguments'),
                        'call_id': payload.get('call_id'),
                        'timestamp': ts,
                    })

                elif rtype == 'function_call_output':
                    out_text = _norm_text(payload.get('output', ''))
                    tmsg = {
                        'role': 'tool',
                        'tool_name': payload.get('name'),
                        'tool_call_id': payload.get('call_id'),
                        'content': out_text,
                        'status': 'error' if payload.get('is_error') else 'success',
                        'timestamp': ts,
                    }
                    messages.append(tmsg)
                    tool_results.append({
                        'type': 'tool_result',
                        'tool': payload.get('name'),
                        'call_id': payload.get('call_id'),
                        'output': payload.get('output'),
                        'timestamp': ts,
                    })

                continue

            if event_type == 'meta':
                payload = obj.get('payload') or {}
                if isinstance(payload, dict):
                    op = payload.get('op')
                    if op == 'model_update' and payload.get('model') and \
                            not session_meta.get('model'):
                        session_meta['model'] = payload['model']
                continue

            if event_type == 'token_count':
                payload = obj.get('payload') or {}
                if isinstance(payload, dict):
                    for k in ('tokens_in', 'tokens_out', 'cached_tokens_in',
                              'context_window_size'):
                        if k in payload:
                            token_usage[k] = payload[k]
                continue

            if event_type == 'turn_context':
                payload = obj.get('payload') or {}
                if isinstance(payload, dict) and payload.get('model') and \
                        not session_meta.get('model'):
                    session_meta['model'] = payload['model']
                continue

    # Attach any trailing reasoning/tool calls to the last assistant message
    for m in reversed(messages):
        if m.get('role') == 'assistant':
            flush_reasoning(m)
            break
    else:
        if pending_reasoning:
            messages.append({'role': 'assistant', 'content': '',
                             'thoughts': pending_reasoning})
            pending_reasoning = []
    if pending_tool_calls and not _attach_tool_calls(messages, pending_tool_calls):
        messages.append({'role': 'assistant', 'content': '',
                         'tool_calls': list(pending_tool_calls)})
        pending_tool_calls = []

    # Drop empty placeholder messages (assistant with no content/calls/thoughts)
    kept = []
    for m in messages:
        if not m.get('content') and not m.get('tool_calls') and not m.get('thoughts'):
            continue
        kept.append(m)
    messages = kept

    if not messages:
        return None

    conv = {
        'messages': messages,
        'session_id': session_meta.get('id') or session_meta.get('thread_id'),
        'cwd': session_meta.get('cwd'),
        'source': 'codex',
        'source_format': 'rollout-jsonl',
        'session_file': str(session_file),
        'timestamp': session_meta.get('timestamp'),
    }

    # Enrich from thread index metadata (title, git, model, tokens)
    for key in ('title', 'model', 'model_provider', 'git_branch',
                'git_origin_url', 'git_sha', 'cli_version', 'first_user_message',
                'created_at_ms', 'updated_at_ms', 'tokens_used', 'thread_source',
                'archived'):
        if key in session_meta and session_meta[key] not in (None, ''):
            conv[key] = session_meta[key]
    if token_usage:
        conv['token_usage'] = token_usage
    if diffs:
        conv['diffs'] = diffs
    if tool_results:
        conv['tool_results'] = tool_results

    return conv


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    print("=" * 80)
    print("CODEX COMPLETE DATA EXTRACTION")
    print("=" * 80)
    print()

    print("🔍 Searching for Codex installations...")
    installations = find_codex_installations()

    if not installations:
        print("❌ No Codex installations found!")
        return

    print(f"✅ Found {len(installations)} installation(s):")
    for inst in installations:
        print(f"   - {inst}")
    print()

    all_conversations = []
    installation_stats = {}
    seen_ids = set()
    merged = 0

    for installation in installations:
        print(f"📂 Processing: {installation}")

        session_files = find_all_codex_sessions(installation)
        print(f"   Found {len(session_files)} session file(s)")

        conversations = []
        for session_file, thread_meta in session_files:
            conv = extract_codex_session(session_file, thread_meta)
            if not conv:
                continue
            conv['installation'] = str(installation)
            sid = conv.get('session_id')
            if sid:
                if sid in seen_ids:
                    merged += 1
                    continue
                seen_ids.add(sid)
            conversations.append(conv)

        if conversations:
            all_conversations.extend(conversations)
            installation_stats[str(installation)] = len(conversations)
            print(f"   ✅ {len(conversations)} conversations")
        else:
            print(f"   ⚠️  No conversations found "
                  f"(no rollout-*.jsonl sessions / empty thread index)")

    print()
    print("=" * 80)
    print("EXTRACTION COMPLETE")
    print("=" * 80)
    print(f"Total conversations: {len(all_conversations):,}")
    if merged:
        print(f"Duplicate sessions merged: {merged:,}")

    if not all_conversations:
        print()
        print("No conversations found!")
        print("Note: logs_2.sqlite / memories_*.sqlite / goals_*.sqlite only hold")
        print("debug tracing and background-job state — not chat text. Codex chat")
        print("history lives in sessions/YYYY/MM/DD/rollout-*.jsonl files.")
        return

    total_messages = sum(len(c['messages']) for c in all_conversations)
    with_tools = sum(1 for c in all_conversations if c.get('tool_results'))
    complete = sum(1 for c in all_conversations
                   if any(m['role'] == 'assistant' for m in c['messages']))
    with_reasoning = sum(1 for c in all_conversations
                         if any(m.get('thoughts') for m in c['messages']))

    print(f"Complete conversations: {complete:,}")
    print(f"Total messages: {total_messages:,}")
    print(f"With tool use/diffs: {with_tools:,}")
    print(f"With reasoning/thoughts: {with_reasoning:,}")
    print()

    print("Breakdown by installation:")
    for inst, count in sorted(installation_stats.items(), key=lambda x: -x[1]):
        print(f"  {Path(inst).name:20} {count:5,} conversations")
    print()

    # Save: one HF-traces JSONL file per session (one message per line)
    from traces_export import write_session_files
    n_files, n_lines = write_session_files(all_conversations, 'codex')
    print(f"✅ Saved {n_files} session file(s) to extracted_data/codex/sessions/")
    print(f"   Total message lines: {n_lines:,}")
    print(f"   Format: HF-traces JSONL (one message per line, one file per session)")


if __name__ == '__main__':
    main()
