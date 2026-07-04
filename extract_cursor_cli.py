#!/usr/bin/env python3
"""Extract cursor-agent CLI conversations (~/.cursor/chats/*/*/store.db).

This is the terminal `cursor-agent` / Composer CLI history, which the GUI
extractor (extract_cursor.py) does NOT cover. Each agent session is a
content-addressed blob store:
  - `blobs` table: id (sha256 hex) -> data. Message blobs are JSON
    ({role, content, id, ...}); tree nodes are protobuf-framed lists of
    32-byte child blob-id references (frame = 0x0a 0x20 <32 bytes>).
  - `meta` table: single hex-encoded JSON row with agentId,
    latestRootBlobId, name, mode, createdAt.
Walking refs from latestRootBlobId reconstructs messages in order.
"""
import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def find_chat_roots():
    """cursor-agent stores CLI chats under ~/.cursor/chats on every platform."""
    root = Path.home() / '.cursor' / 'chats'
    return [root] if root.exists() else []


def parse_ref_list(data):
    """Parse a protobuf-framed tree node into ordered 32-byte blob-id refs."""
    refs = []
    i = 0
    n = len(data)
    while i < n - 1:
        if data[i] == 0x0A and data[i + 1] == 0x20 and i + 34 <= n:
            refs.append(data[i + 2:i + 34].hex())
            i += 34
        else:
            i += 1
    return refs


def resolve_messages(conn, root_id):
    """Depth-first flatten from root_id into ordered message dicts."""
    messages = []
    visited = set()

    def walk(bid):
        if bid in visited:
            return
        visited.add(bid)
        row = conn.execute('SELECT data FROM blobs WHERE id=?', (bid,)).fetchone()
        if not row:
            return
        data = row[0]
        # Try JSON message blob first
        try:
            obj = json.loads(data)
            if isinstance(obj, dict) and 'role' in obj:
                messages.append(obj)
                return
        except (ValueError, UnicodeDecodeError):
            pass
        # Otherwise treat as a tree node: recurse into referenced children
        for ref in parse_ref_list(data):
            walk(ref)

    walk(root_id)
    return messages


def load_meta(conn):
    row = conn.execute('SELECT value FROM meta LIMIT 1').fetchone()
    if not row:
        return {}
    try:
        return json.loads(bytes.fromhex(row[0]).decode('utf-8', 'replace'))
    except Exception:
        return {}


def has_code(messages):
    for m in messages:
        c = m.get('content')
        blob = c if isinstance(c, str) else json.dumps(c)
        if '```' in blob or 'tool-result' in blob or 'tool-call' in blob:
            return True
    return False


def extract_store(db_path, chat_id):
    try:
        conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    except sqlite3.Error:
        return None
    try:
        meta = load_meta(conn)
        root_id = meta.get('latestRootBlobId')
        if not root_id:
            return None
        messages = resolve_messages(conn, root_id)
        if not messages:
            return None
        return {
            'messages': messages,
            'source': 'cursor-agent-cli',
            'chat_id': chat_id,
            'agent_id': meta.get('agentId'),
            'title': meta.get('name'),
            'mode': meta.get('mode'),
            'created_at': meta.get('createdAt'),
            'has_code_context': has_code(messages),
            'has_diffs': any('diff' in json.dumps(m.get('content', ''))
                             for m in messages),
        }
    finally:
        conn.close()


def main():
    print('=' * 80)
    print('CURSOR-AGENT CLI EXTRACTION (~/.cursor/chats)')
    print('=' * 80)

    roots = find_chat_roots()
    if not roots:
        print('No ~/.cursor/chats found.')
        return

    all_convs = []
    for root in roots:
        dbs = sorted(root.glob('*/*/store.db'))
        print(f'Scanning {root}: {len(dbs)} store.db files')
        for db in dbs:
            chat_id = db.parent.parent.name
            conv = extract_store(db, chat_id)
            if conv:
                all_convs.append(conv)

    if not all_convs:
        print('No conversations reconstructed.')
        return

    roles = defaultdict(int)
    for c in all_convs:
        for m in c['messages']:
            roles[m.get('role', '?')] += 1
    total_msgs = sum(len(c['messages']) for c in all_convs)
    complete = sum(1 for c in all_convs
                   if any(m.get('role') == 'assistant' for m in c['messages']))

    print()
    print(f'Total conversations: {len(all_convs):,}')
    print(f'Complete (has assistant): {complete:,}')
    print(f'Total messages: {total_msgs:,}')
    print(f'By role: {dict(roles)}')
    print(f'With code/tool context: {sum(1 for c in all_convs if c["has_code_context"]):,}')

    out_dir = Path('extracted_data')
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_file = out_dir / f'cursor_cli_{ts}.jsonl'
    with open(out_file, 'w') as f:
        for c in all_convs:
            f.write(json.dumps(c, ensure_ascii=False) + '\n')
    size = out_file.stat().st_size / 1024 / 1024
    print(f'\nSaved: {out_file} ({size:.2f} MB)')


if __name__ == '__main__':
    main()
