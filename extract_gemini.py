#!/usr/bin/env python3
"""
Extract ALL Google Gemini CLI chat data
Includes: messages, thoughts (reasoning), token usage, model info
Auto-discovers Gemini CLI installations on the device

Only extracts the **gemini CLI** conversations (readable session files under
~/.gemini/tmp/<project>/chats/). The separate Antigravity (Gemini 3) app
storage — where conversation bodies are encrypted — is handled by the
companion script `extract_antigravity.py`.

Two on-disk formats are supported:
  * Legacy JSON  — tmp/<project>/chats/session-*.json  (whole-file dict with
                   a top-level "messages" array).
  * New JSONL    — tmp/<project>/chats/session-*.jsonl (line 0 = session
                   header; following lines = message dicts or incremental
                   "$set" updates, e.g. {"$set": {"messages": [...]}}).

Message "content" may be a plain string OR a list of parts
([{"text": ...}, {"functionCall": {...}}], …); both are normalized to text.
Sessions that only contain info/error lines (login prompts, update notices)
are stubs and are skipped, with a count reported. Conversations saved as
multiple snapshot files (same session_id across project dirs or runs) are
deduplicated keeping the richest copy.
"""

import json
from pathlib import Path
import platform
import os


def find_gemini_installations():
    """Find all Gemini CLI installation directories"""
    system = platform.system()
    home = Path.home()

    locations = []

    # Search patterns for Gemini directories
    gemini_patterns = [
        'gemini', '.gemini'
    ]

    if system == "Darwin":  # macOS
        base_dirs = [
            home,
            home / ".config"
        ]
    elif system == "Linux":
        base_dirs = [
            home / ".gemini",
            home / ".config/gemini",
            home / ".local/share/gemini",
            home
        ]
    elif system == "Windows":
        base_dirs = [
            Path(os.environ.get('USERPROFILE', home)) / ".gemini",
            Path(os.environ.get('LOCALAPPDATA', home / 'AppData/Local')) / "gemini",
            home
        ]
    else:
        base_dirs = [home / ".gemini", home / ".config", home]

    for base_dir in base_dirs:
        if not base_dir.exists():
            continue

        for pattern in gemini_patterns:
            gemini_dir = base_dir / pattern
            if gemini_dir.exists():
                locations.append(gemini_dir)

    return list(set(locations))


def _normalize_content(content):
    """Normalize a Gemini message 'content' to a plain string.

    Newer Gemini CLI versions store content as either a plain string or a
    list of parts, e.g. [{"text": "..."}, {"functionCall": {...}}].
    """
    if content is None:
        return ''
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                if p.get('text'):
                    parts.append(p['text'])
                elif 'functionCall' in p:
                    fc = p['functionCall'] or {}
                    parts.append(f"[function_call: {fc.get('name', '?')} "
                                 f"{json.dumps(fc.get('args') or {}, ensure_ascii=False)}]")
                elif 'functionResponse' in p:
                    parts.append(json.dumps(p, ensure_ascii=False))
                else:
                    parts.append(json.dumps(p, ensure_ascii=False))
            else:
                parts.append(str(p))
        return '\n'.join(parts)
    return str(content)


def _normalize_message(msg):
    """Normalize a single Gemini CLI message dict -> standardized dict.

    Returns None for info/error lines (session-level noise, not a turn).
    """
    if not isinstance(msg, dict):
        return None

    msg_type = msg.get('type')
    content = _normalize_content(msg.get('content', ''))
    if not content:
        content = _normalize_content(msg.get('displayContent', ''))
    timestamp = msg.get('timestamp')

    if msg_type == 'user':
        normalized = {
            'role': 'user',
            'content': content,
            'timestamp': timestamp,
        }
    elif msg_type == 'gemini':
        normalized = {
            'role': 'assistant',
            'content': content,
            'timestamp': timestamp,
        }
        if msg.get('model'):
            normalized['model'] = msg['model']
        if msg.get('thoughts'):
            normalized['thoughts'] = msg['thoughts']
        if msg.get('tokens'):
            normalized['tokens'] = msg['tokens']
        if msg.get('toolCalls'):
            normalized['tool_calls'] = msg['toolCalls']
    elif msg_type in ('info', 'error'):
        # Session-level info/error lines are not user/assistant turns.
        return None
    else:
        # Unknown type — keep the raw data rather than silently dropping it.
        normalized = {
            'role': msg_type or 'unknown',
            'content': content,
            'timestamp': timestamp,
        }
    return normalized


def extract_gemini_session_jsonl(session_file):
    """Extract conversation from a new-format Gemini CLI session JSONL file.

    Format: line 0 is the session header {sessionId, projectHash, startTime,
    lastUpdated, kind}; subsequent lines are either message dicts
    {id, timestamp, type, content, thoughts, tokens, model, toolCalls} or
    incremental "$set" updates (e.g. {"$set": {"lastUpdated": ...}} or
    {"$set": {"messages": [...]}}). Messages are deduplicated by their id.
    """
    header = {}
    messages_by_id = {}

    try:
        with open(session_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue

                if 'type' not in obj:
                    # Header line, or a $set update containing a full message
                    # snapshot.
                    if 'sessionId' in obj:
                        header = obj
                    elif '$set' in obj:
                        msgs = (obj.get('$set') or {}).get('messages')
                        if isinstance(msgs, list):
                            for m in msgs:
                                if isinstance(m, dict) and m.get('id'):
                                    messages_by_id[m['id']] = m
                    continue

                # Individual message line.
                if obj.get('id'):
                    messages_by_id[obj['id']] = obj
    except OSError:
        return None

    if not messages_by_id:
        return None

    messages = []
    # Gemini session-level info/error lines (not user/assistant turns) are
    # preserved verbatim so no source data is lost; they land in the traces
    # meta file rather than being discarded.
    session_lines = []
    for msg in messages_by_id.values():
        normalized = _normalize_message(msg)
        if normalized:
            messages.append(normalized)
        else:
            session_lines.append(msg)

    if not messages:
        return None

    conv = {
        'source': 'gemini-cli',
        'session_format': 'jsonl',
        'messages': messages,
        'session_id': header.get('sessionId'),
        'project_hash': header.get('projectHash'),
        'start_time': header.get('startTime'),
        'last_updated': header.get('lastUpdated'),
        'source_file': str(session_file),
    }
    if session_lines:
        conv['session_level_lines'] = session_lines
    return conv


def extract_gemini_session(session_file):
    """Extract conversation from a Gemini CLI session file (.json or .jsonl)."""
    if session_file.suffix == '.jsonl':
        return extract_gemini_session_jsonl(session_file)

    # Legacy JSON format: whole-file dict with a top-level "messages" array.
    try:
        with open(session_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    if not isinstance(data, dict):
        return None
    if 'messages' not in data or not data['messages']:
        return None

    messages = []
    session_lines = []
    for msg in data['messages']:
        normalized = _normalize_message(msg)
        if normalized:
            messages.append(normalized)
        else:
            session_lines.append(msg)

    if not messages:
        return None

    conv = {
        'source': 'gemini-cli',
        'session_format': 'json',
        'messages': messages,
        'session_id': data.get('sessionId'),
        'project_hash': data.get('projectHash'),
        'start_time': data.get('startTime'),
        'last_updated': data.get('lastUpdated'),
        'source_file': str(session_file),
    }
    if session_lines:
        conv['session_level_lines'] = session_lines
    return conv


def find_all_gemini_sessions(installation):
    """Find all Gemini CLI session files in an installation.

    Covers both the legacy JSON and the new JSONL session formats under
    tmp/<project>/chats/.
    """
    session_files = []

    # Search for session files in tmp/[hash]/chats/session-*.json(l) pattern
    tmp_dir = installation / 'tmp'
    if tmp_dir.exists():
        session_files.extend(list(tmp_dir.rglob('chats/session-*.json')))
        session_files.extend(list(tmp_dir.rglob('chats/session-*.jsonl')))

    return session_files


def main():
    print("="*80)
    print("GOOGLE GEMINI CLI DATA EXTRACTION")
    print("="*80)
    print()

    # Find all Gemini installations
    print("🔍 Searching for Gemini CLI installations...")
    installations = find_gemini_installations()

    if not installations:
        print("❌ No Gemini CLI installations found!")
        return

    print(f"✅ Found {len(installations)} installation(s):")
    for inst in installations:
        print(f"   - {inst}")
    print()

    # Extract from all installations
    all_conversations = []
    installation_stats = {}

    for installation in installations:
        print(f"📂 Processing: {installation}")

        session_files = find_all_gemini_sessions(installation)
        print(f"   Found {len(session_files)} session files")

        conversations = []
        stubs = 0
        for session_file in session_files:
            conv = extract_gemini_session(session_file)
            if conv:
                conv['installation'] = str(installation)
                conversations.append(conv)
            else:
                stubs += 1

        if conversations:
            all_conversations.extend(conversations)
            installation_stats[str(installation)] = len(conversations)
            print(f"   ✅ {len(conversations)} conversations "
                  f"({stubs} info-only stubs skipped)")
        else:
            print(f"   ⚠️  No conversations found "
                  f"({stubs} info-only stubs skipped)")

    print()
    print("="*80)
    print("EXTRACTION COMPLETE")
    print("="*80)

    if not all_conversations:
        print("No conversations found!")
        return

    # Deduplicate by session_id: the same conversation can be saved as several
    # snapshot files (continued over multiple runs, or copied across project
    # dirs). Keep the richest copy (most messages; JSONL wins ties since it
    # carries tool calls and richer fields). Conversations without a
    # session_id are kept as-is, never dropped.
    merged = 0
    best = {}
    orphaned = []
    for conv in all_conversations:
        sid = conv.get('session_id')
        if not sid:
            orphaned.append(conv)
            continue
        prev = best.get(sid)
        if prev is None:
            best[sid] = conv
            continue
        merged += 1
        prev_msgs = len(prev['messages'])
        cur_msgs = len(conv['messages'])
        if cur_msgs > prev_msgs:
            best[sid] = conv
        elif cur_msgs == prev_msgs:
            if (conv.get('session_format') == 'jsonl'
                    and prev.get('session_format') != 'jsonl'):
                best[sid] = conv
    all_conversations = orphaned + list(best.values())

    # Recompute per-installation counts from the deduplicated output so the
    # breakdown matches the total (previously the snapshot duplicates made
    # the breakdown overstate the per-installation numbers).
    installation_stats = {}
    for conv in all_conversations:
        inst = conv.get('installation')
        if inst:
            installation_stats[inst] = installation_stats.get(inst, 0) + 1

    print(f"Total conversations: {len(all_conversations):,}")

    # Statistics
    total_messages = sum(len(c['messages']) for c in all_conversations)
    with_thoughts = sum(1 for c in all_conversations
                        if any('thoughts' in m for m in c['messages']))
    complete = sum(1 for c in all_conversations
                   if any(m['role'] == 'assistant' for m in c['messages']))
    jsonl_count = sum(1 for c in all_conversations
                      if c.get('session_format') == 'jsonl')

    print(f"Complete conversations: {complete:,}")
    print(f"Total messages: {total_messages:,}")
    print(f"With thoughts: {with_thoughts:,}")
    if merged:
        print(f"Duplicate session snapshots merged: {merged:,}")
    if jsonl_count:
        print(f"New-format (JSONL) conversations: {jsonl_count:,}")
    print()

    print("Breakdown by installation:")
    for inst, count in sorted(installation_stats.items(), key=lambda x: -x[1]):
        print(f"  {Path(inst).name:20} {count:5,} conversations")
    print()

    # Save: one HF-traces JSONL file per session (one message per line)
    from traces_export import write_session_files
    n_files, n_lines = write_session_files(all_conversations, 'gemini')
    print(f"✅ Saved {n_files} session file(s) to extracted_data/gemini/sessions/")
    print(f"   Total message lines: {n_lines:,}")
    print(f"   Format: HF-traces JSONL (one message per line, one file per session)")


if __name__ == '__main__':
    main()
