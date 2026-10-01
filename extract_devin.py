#!/usr/bin/env python3
"""
Extract ALL Devin Desktop conversation data (Devin is the successor to Windsurf).
Includes: user/agent messages, agent reasoning, tool calls (with inputs,
locations and status), nested subagent threads, and user file edits.

Devin stores each session as a SQLite database:
    <Devin>/User/acp-messages/<uuid>.db
        meta     (key TEXT, value TEXT)   -- 'info' JSON, message_count, truncated
        messages (position INTEGER, kind TEXT, payload TEXT)  -- payload is JSON

Session names live in <Devin>/User/globalStorage/state.vscdb under keys:
    windsurf.acp.messageStore.session.acp/<connector>/<name> -> {"uuid", "lastUpdated"}

Auto-discovers Devin installations on macOS, Linux, and Windows.
"""

import json
import sqlite3
from pathlib import Path
from datetime import datetime
import platform
import os
import shutil
import tempfile
from collections import defaultdict

SESSION_KEY_PREFIX = 'windsurf.acp.messageStore.session.'

ROLE_MAP = {
    'user_message': 'user',
    'agent_message': 'assistant',
    'agent_thought': 'assistant',
    'tool_call': 'tool',
    'subagent': 'assistant',
}


def find_devin_installations():
    """Find all Devin installation directories"""
    system = platform.system()
    home = Path.home()

    locations = []
    devin_patterns = ['Devin', 'devin', '.devin']

    if system == "Darwin":  # macOS
        base_dirs = [
            home / "Library/Application Support",
            home / ".config"
        ]
    elif system == "Linux":
        base_dirs = [
            home / ".config",
            home / ".local/share"
        ]
    elif system == "Windows":
        base_dirs = [
            Path(os.environ.get('APPDATA', home / 'AppData/Roaming')),
            Path(os.environ.get('LOCALAPPDATA', home / 'AppData/Local'))
        ]
    else:
        base_dirs = [home / ".config"]

    seen_inodes = set()
    for base_dir in base_dirs:
        if not base_dir.exists():
            continue
        for pattern in devin_patterns:
            devin_dir = base_dir / pattern
            # installation dirs contain a User/ folder (rules out ~/.devin config dirs)
            if (devin_dir / 'User').exists():
                # dedupe case-variant paths on case-insensitive filesystems
                st = (devin_dir / 'User').stat()
                inode = (st.st_dev, st.st_ino)
                if inode not in seen_inodes:
                    seen_inodes.add(inode)
                    locations.append(devin_dir)

    return locations


def open_db_readonly(db_path):
    """Open a SQLite DB read-only; fall back to a temp copy if locked
    (Devin may hold the live DB open in WAL mode)."""
    try:
        conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
        conn.execute('SELECT 1 FROM sqlite_master LIMIT 1')
        return conn
    except sqlite3.Error:
        tmpdir = Path(tempfile.mkdtemp(prefix='devin_extract_'))
        copy = tmpdir / db_path.name
        shutil.copy2(db_path, copy)
        for suffix in ('-wal', '-shm'):
            side = Path(str(db_path) + suffix)
            if side.exists():
                shutil.copy2(side, Path(str(copy) + suffix))
        return sqlite3.connect(copy)


def read_session_registry(global_db_path):
    """Map session uuid -> {name, last_updated} from Devin globalStorage."""
    registry = {}
    if not global_db_path.exists():
        return registry

    try:
        conn = open_db_readonly(global_db_path)
        rows = conn.execute(
            "SELECT key, value FROM ItemTable WHERE key LIKE ?",
            (SESSION_KEY_PREFIX + '%',)
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return registry

    for key, value in rows:
        try:
            v = json.loads(value)
            registry[v['uuid']] = {
                'name': key[len(SESSION_KEY_PREFIX):],
                'last_updated': v.get('lastUpdated'),
            }
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    return registry


def chunk_timestamp(chunk):
    """cognition.ai/timestamp embedded in a content chunk's _meta"""
    return ((chunk or {}).get('_meta') or {}).get('cognition.ai/timestamp')


def normalize_acp_message(payload, depth=0):
    """Normalize one messages.payload JSON blob into repo message format."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return {'role': 'unknown', 'content': payload}

    kind = payload.get('kind', 'unknown')
    msg = {
        'role': ROLE_MAP.get(kind, 'unknown'),
        'kind': kind,
    }
    if payload.get('id'):
        msg['id'] = payload['id']

    content = payload.get('content')
    if isinstance(content, list):
        # Chunked text content (user_message / agent_message / agent_thought)
        texts, timestamps = [], []
        for chunk in content:
            if not isinstance(chunk, dict):
                texts.append(str(chunk))
                continue
            ts = chunk_timestamp(chunk)
            if ts:
                timestamps.append(ts)
            inner = chunk.get('content')
            if isinstance(inner, dict):
                texts.append(inner.get('text') or json.dumps(inner))
            elif isinstance(inner, str):
                texts.append(inner)
        msg['content'] = ''.join(texts)
        if timestamps:
            msg['timestamp'] = timestamps[0]
        if kind == 'agent_thought':
            msg['is_thought'] = True
    elif isinstance(content, dict):
        # tool_call: object payload
        meta = content.get('_meta') or {}
        msg['tool'] = meta.get('cognition.ai/inferenceToolName') or content.get('kind')
        msg['title'] = content.get('title')
        msg['status'] = content.get('status')
        if content.get('rawInput') is not None:
            msg['tool_input'] = content['rawInput']
        if content.get('rawOutput') is not None:
            msg['tool_output'] = content['rawOutput']
        if content.get('locations'):
            msg['code_context'] = content['locations']
        ts = meta_timestamp_or_ms(content, payload)
        if ts:
            msg['timestamp'] = ts

    if 'timestamp' not in msg:
        ts = meta_timestamp_or_ms(payload)
        if ts:
            msg['timestamp'] = ts

    # User edits attached to the turn (files the user changed manually)
    if payload.get('userEdits'):
        msg['user_edits'] = payload['userEdits']

    # Subagent threads carry their own nested message list
    if kind == 'subagent':
        for k in ('agentId', 'title', 'task', 'profile', 'status', 'isBackground'):
            if payload.get(k) is not None:
                msg[k] = payload[k]
        if depth < 4 and payload.get('childMessages'):
            msg['child_messages'] = [
                normalize_acp_message(c, depth + 1)
                for c in payload['childMessages']
            ]
        elif payload.get('childMessages'):
            msg['child_messages_truncated'] = len(payload['childMessages'])

    return msg


def meta_timestamp_or_ms(*objs):
    """First available timestamp: cognition.ai _meta, then sourceEventTimestampMs."""
    for obj in objs:
        ts = chunk_timestamp(obj)
        if ts:
            return ts
    for obj in objs:
        ms = (obj or {}).get('sourceEventTimestampMs')
        if ms:
            try:
                return datetime.fromtimestamp(ms / 1000).isoformat()
            except (OSError, ValueError, OverflowError):
                pass
    return None


def extract_acp_sessions(installation):
    """Extract all ACP session DBs under <Devin>/User/acp-messages."""
    acp_dir = installation / 'User/acp-messages'
    if not acp_dir.exists():
        return []

    registry = read_session_registry(
        installation / 'User/globalStorage/state.vscdb')

    conversations = []
    for db_file in sorted(acp_dir.glob('*.db')):
        uuid = db_file.stem
        try:
            conn = open_db_readonly(db_file)
            meta = dict(conn.execute('SELECT key, value FROM meta'))
            rows = conn.execute(
                'SELECT position, kind, payload FROM messages ORDER BY position'
            ).fetchall()
            conn.close()
        except sqlite3.Error as e:
            print(f"   ⚠️  Skipping {db_file.name}: {e}")
            continue

        if not rows:
            continue

        session_info = {}
        if meta.get('info'):
            try:
                session_info = json.loads(meta['info'])
            except json.JSONDecodeError:
                pass

        reg = registry.get(uuid, {})
        messages = [normalize_acp_message(p) for _, _, p in rows]

        conversations.append({
            'messages': messages,
            'source': 'devin-acp',
            'session_id': uuid,
            'name': reg.get('name', uuid),
            'created_at': reg.get('last_updated'),
            'session_info': session_info,
            'truncated': meta.get('truncated') == '1',
            'has_code_context': any(m.get('code_context') or m.get('tool_input')
                                    for m in messages),
        })

    return conversations


def main():
    print("="*80)
    print("DEVIN DESKTOP COMPLETE DATA EXTRACTION (ACP sessions)")
    print("="*80)
    print()

    print("🔍 Searching for Devin installations...")
    installations = find_devin_installations()

    if not installations:
        print("❌ No Devin installations found!")
        return

    print(f"✅ Found {len(installations)} installation(s):")
    for inst in installations:
        print(f"   - {inst}")
    print()

    all_conversations = []
    seen_sessions = set()
    stats = defaultdict(int)

    for installation in installations:
        print(f"📂 Processing: {installation}")
        convs = extract_acp_sessions(installation)
        convs = [c for c in convs if c['session_id'] not in seen_sessions]
        seen_sessions.update(c['session_id'] for c in convs)
        all_conversations.extend(convs)
        stats['sessions'] += len(convs)
        stats['messages'] += sum(len(c['messages']) for c in convs)
        stats['truncated'] += sum(1 for c in convs if c.get('truncated'))
        print(f"   ✅ ACP sessions: {len(convs)} conversations")

    print()
    print("="*80)
    print("EXTRACTION COMPLETE")
    print("="*80)
    print(f"Total conversations: {stats['sessions']:,}")
    print(f"Total messages: {stats['messages']:,}")
    if stats['truncated']:
        print(f"Sessions truncated at Devin's message cap: {stats['truncated']:,}")

    if not all_conversations:
        print("No conversations found!")
        return

    with_code = sum(1 for c in all_conversations if c.get('has_code_context'))
    complete = sum(1 for c in all_conversations
                   if any(m['role'] == 'assistant' for m in c['messages']))
    print(f"Complete conversations: {complete:,}")
    print(f"With tool calls / code context: {with_code:,}")
    print()

    # Save to organized JSONL
    output_dir = Path('extracted_data')
    output_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_file = output_dir / f'devin_conversations_{timestamp}.jsonl'

    with open(output_file, 'w') as f:
        for conv in all_conversations:
            f.write(json.dumps(conv, ensure_ascii=False) + '\n')

    file_size = output_file.stat().st_size / 1024 / 1024
    print(f"✅ Saved to: {output_file}")
    print(f"   Size: {file_size:.2f} MB")
    print(f"   Format: JSONL (one conversation per line)")


if __name__ == '__main__':
    main()
