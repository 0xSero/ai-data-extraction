#!/usr/bin/env python3
"""
Extract ALL OpenCode conversation data
Supports: CLI (SQLite + JSON files) and Desktop (Tauri .dat files)

Storage locations:
- CLI: ~/.local/share/opencode/opencode.db (OpenCode 1.14+)
- CLI fallback: ~/.local/share/opencode/storage/{session,message,part}/ (pre-1.14)
- Desktop: Platform-specific Tauri app data directories

Features:
- Reads the current SQLite session/message/part schema
- Falls back to the pre-1.14 JSON tree when no database exists
- Reconstructs session metadata from message content when needed
- Assembles complete messages from message metadata + parts
"""

import json
import os
import platform
import re
import sqlite3
import struct
from pathlib import Path
from datetime import datetime


def find_opencode_installations():
    """Find all OpenCode installation directories"""
    system = platform.system()
    home = Path.home()

    locations = []
    seen = set()

    def add(install_type, path):
        resolved = path.resolve() if path.exists() else path
        key = (install_type, str(resolved))
        if key in seen:
            return
        if path.exists():
            seen.add(key)
            locations.append((install_type, path))

    env_db = os.environ.get('OPENCODE_DB')
    if env_db:
        db_path = Path(env_db).expanduser()
        if db_path.is_file():
            add('cli', db_path.parent)

    if system == "Darwin":
        cli_dirs = [
            home / "Library/Application Support/opencode",
            Path(os.environ.get('XDG_DATA_HOME', home / '.local/share')) / 'opencode'
        ]
    elif system == "Linux":
        cli_dirs = [
            Path(os.environ.get('XDG_DATA_HOME', home / '.local/share')) / 'opencode'
        ]
    elif system == "Windows":
        cli_dirs = [
            Path(os.environ.get('APPDATA', home / 'AppData/Roaming')) / 'opencode'
        ]
    else:
        cli_dirs = [home / '.local/share/opencode']

    for cli_dir in cli_dirs:
        add('cli', cli_dir)

    if system == "Darwin":
        desktop_dirs = [
            home / "Library/Application Support/ai.opencode.app"
        ]
    elif system == "Linux":
        desktop_dirs = [
            home / ".local/share/ai.opencode.app"
        ]
    elif system == "Windows":
        desktop_dirs = [
            Path(os.environ.get('APPDATA', home / 'AppData/Roaming')) / 'ai.opencode.app'
        ]
    else:
        desktop_dirs = []

    for desktop_dir in desktop_dirs:
        add('desktop', desktop_dir)

    return locations


def find_sqlite_dbs(storage_dir):
    """Locate OpenCode SQLite databases under a CLI data directory."""
    found = []
    seen = set()

    def add(path):
        resolved = path.resolve()
        if resolved in seen or not path.is_file():
            return
        seen.add(resolved)
        found.append(path)

    env_db = os.environ.get('OPENCODE_DB')
    if env_db:
        env_path = Path(env_db).expanduser()
        try:
            if env_path.is_file() and env_path.resolve().parent == Path(storage_dir).resolve():
                add(env_path)
                if found:
                    return found
        except OSError:
            pass

    for path in sorted(storage_dir.glob('opencode*.db')):
        add(path)

    return found


def connect_readonly(db_path):
    """Open an OpenCode SQLite database read-only, including WAL copies."""
    uri = Path(db_path).resolve().as_posix()
    last_error = None

    for query in ('mode=ro', 'mode=ro&immutable=1'):
        conn = None
        try:
            conn = sqlite3.connect(f'file:{uri}?{query}', uri=True, timeout=30)
            conn.execute('PRAGMA query_only = ON')
            conn.execute('SELECT 1 FROM sqlite_master LIMIT 1')
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.Error as error:
            last_error = error
            if conn is not None:
                conn.close()

    raise last_error


def table_names(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {row[0] for row in rows}


def table_columns(conn, table):
    rows = conn.execute(f'PRAGMA table_info({table})').fetchall()
    return {row[1] for row in rows}


def load_json_value(value):
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode('utf-8', errors='replace')
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return {}
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def apply_part(part_data, content_parts, tool_calls, tool_results, reasoning_parts, all_content):
    """Fold one OpenCode part JSON object into message collectors."""
    part_type = part_data.get('type')
    part_text = part_data.get('text', '')

    if part_text:
        all_content.append(part_text)

    if part_type == 'text':
        content_parts.append(part_text)
    elif part_type == 'subtask':
        prompt = part_data.get('prompt', '')
        if prompt:
            all_content.append(prompt)
            content_parts.append(prompt)
    elif part_type in ('tool', 'tool-call'):
        state = part_data.get('state', {})
        if not isinstance(state, dict):
            state = {}
        tool_name = part_data.get('tool', part_data.get('name'))

        tool_call = {
            'id': part_data.get('callID', part_data.get('id')),
            'name': tool_name,
            'input': state.get('input', part_data.get('input'))
        }
        tool_calls.append(tool_call)

        if state.get('status') == 'completed' and 'output' in state:
            tool_results.append({
                'tool_call_id': part_data.get('callID'),
                'tool': tool_name,
                'output': state['output']
            })
        elif state.get('status') == 'error':
            tool_results.append({
                'tool_call_id': part_data.get('callID'),
                'tool': tool_name,
                'output': state.get('error', state.get('output')),
                'error': True
            })
    elif part_type == 'tool-result':
        tool_results.append({
            'tool_call_id': part_data.get('toolCallID'),
            'output': part_data.get('output')
        })
    elif part_type == 'code':
        code_text = part_data.get('text', '')
        language = part_data.get('language', '')
        content_parts.append(f"```{language}\n{code_text}\n```")
    elif part_type == 'reasoning':
        reasoning_text = part_data.get('text', '')
        if reasoning_text:
            reasoning_parts.append(reasoning_text)


def assemble_message(msg_data, parts):
    """Build one output message from OpenCode message JSON plus part JSON."""
    role = msg_data.get('role', 'assistant')
    msg_time = None
    time_info = msg_data.get('time')
    if isinstance(time_info, dict):
        msg_time = time_info.get('created')
    elif isinstance(msg_data.get('time_created'), (int, float)):
        msg_time = msg_data.get('time_created')

    message = {
        'role': role,
        'content': '',
        'timestamp': msg_time
    }

    model = msg_data.get('modelID')
    if not model:
        nested_model = msg_data.get('model')
        if isinstance(nested_model, dict):
            model = nested_model.get('modelID')
    if model:
        message['model'] = model
    if 'providerID' in msg_data:
        message['provider'] = msg_data['providerID']
    elif isinstance(msg_data.get('model'), dict) and 'providerID' in msg_data['model']:
        message['provider'] = msg_data['model']['providerID']
    if 'agent' in msg_data:
        message['agent'] = msg_data['agent']
    if 'mode' in msg_data:
        message['mode'] = msg_data['mode']
    if 'tokens' in msg_data:
        message['tokens'] = msg_data['tokens']
    if 'cost' in msg_data:
        message['cost'] = msg_data['cost']

    content_parts = []
    tool_calls = []
    tool_results = []
    reasoning_parts = []
    all_content = []

    for part_data in parts:
        apply_part(part_data, content_parts, tool_calls, tool_results, reasoning_parts, all_content)

    message['content'] = '\n'.join(content_parts)
    if tool_calls:
        message['tool_calls'] = tool_calls
    if tool_results:
        message['tool_results'] = tool_results
    if reasoning_parts:
        message['reasoning'] = '\n'.join(reasoning_parts)

    return message, all_content, msg_time


def build_conversation(session_id, session_data, messages, all_content, first_message_time, last_message_time):
    conversation = {
        'messages': messages,
        'source': 'opencode-cli',
        'session_id': session_id,
    }

    if session_data:
        conversation['title'] = session_data.get('title')
        time_info = session_data.get('time') or {}
        conversation['created_at'] = time_info.get('created')
        conversation['updated_at'] = time_info.get('updated')
        conversation['project_id'] = session_data.get('projectID')
        conversation['directory'] = session_data.get('directory')
        conversation['version'] = session_data.get('version')

        if 'summary' in session_data:
            conversation['summary'] = session_data['summary']

        if 'parentID' in session_data:
            conversation['parent_session_id'] = session_data['parentID']
    else:
        conversation['created_at'] = first_message_time
        conversation['updated_at'] = last_message_time
        combined_content = '\n'.join(all_content)
        conversation['directory'] = extract_directory_from_content(combined_content)
        conversation['project_id'] = extract_project_id_from_content(combined_content)

        for msg in messages:
            if msg.get('role') == 'user' and msg.get('content'):
                title = msg['content'][:100].strip()
                if len(msg['content']) > 100:
                    title += '...'
                conversation['title'] = title
                break

        conversation['version'] = 'unknown'

    return conversation


def session_row_to_metadata(row):
    """Map a SQLite session row onto the JSON session-file shape."""
    data = dict(row)
    session_data = {
        'title': data.get('title'),
        'directory': data.get('directory'),
        'projectID': data.get('project_id'),
        'version': data.get('version'),
        'time': {
            'created': data.get('time_created'),
            'updated': data.get('time_updated'),
        }
    }
    parent_id = data.get('parent_id')
    if parent_id:
        session_data['parentID'] = parent_id

    summary = {}
    for key in ('summary_additions', 'summary_deletions', 'summary_files'):
        if data.get(key) is not None:
            summary[key.replace('summary_', '')] = data[key]
    if summary:
        session_data['summary'] = summary

    return session_data


def extract_sqlite_conversations(db_path):
    """Extract conversations from an OpenCode SQLite database."""
    conversations = []

    try:
        conn = connect_readonly(db_path)
    except sqlite3.Error as error:
        print(f"  Error opening SQLite database {db_path}: {error}")
        return conversations

    try:
        names = table_names(conn)
        required = {'session', 'message', 'part'}
        if not required.issubset(names):
            missing = ', '.join(sorted(required - names))
            print(f"  SQLite database is missing tables: {missing}")
            return conversations

        session_cols = table_columns(conn, 'session')
        wanted_session = [
            'id', 'project_id', 'parent_id', 'directory', 'title', 'version',
            'time_created', 'time_updated', 'summary_additions',
            'summary_deletions', 'summary_files'
        ]
        select_session = [col for col in wanted_session if col in session_cols]
        if 'id' not in select_session:
            print("  SQLite session table has no id column")
            return conversations

        message_cols = table_columns(conn, 'message')
        part_cols = table_columns(conn, 'part')
        if 'data' not in message_cols or 'session_id' not in message_cols:
            print("  SQLite message table is missing data or session_id")
            return conversations
        if 'data' not in part_cols or 'message_id' not in part_cols:
            print("  SQLite part table is missing data or message_id")
            return conversations

        session_sql = f"SELECT {', '.join(select_session)} FROM session ORDER BY time_created, id" \
            if 'time_created' in session_cols else \
            f"SELECT {', '.join(select_session)} FROM session ORDER BY id"

        session_rows = conn.execute(session_sql).fetchall()
        print(f"  Found {len(session_rows)} sessions in SQLite")

        message_order = "time_created ASC, id ASC" if 'time_created' in message_cols else "id ASC"
        part_order = "time_created ASC, id ASC" if 'time_created' in part_cols else "id ASC"

        for session_row in session_rows:
            session_id = session_row['id']
            try:
                msg_rows = conn.execute(
                    f"SELECT id, data, time_created FROM message WHERE session_id = ? ORDER BY {message_order}"
                    if 'time_created' in message_cols else
                    "SELECT id, data FROM message WHERE session_id = ? ORDER BY id",
                    (session_id,)
                ).fetchall()

                if not msg_rows:
                    continue

                if 'session_id' in part_cols:
                    part_rows = conn.execute(
                        f"SELECT id, message_id, data FROM part WHERE session_id = ? ORDER BY {part_order}",
                        (session_id,)
                    ).fetchall()
                else:
                    message_ids = [row['id'] for row in msg_rows]
                    part_rows = []
                    for message_id in message_ids:
                        part_rows.extend(conn.execute(
                            f"SELECT id, message_id, data FROM part WHERE message_id = ? ORDER BY {part_order}",
                            (message_id,)
                        ).fetchall())

                parts_by_message = {}
                for part_row in part_rows:
                    parts_by_message.setdefault(part_row['message_id'], []).append(
                        load_json_value(part_row['data'])
                    )

                messages = []
                all_content = []
                first_message_time = None
                last_message_time = None

                for msg_row in msg_rows:
                    msg_data = load_json_value(msg_row['data'])
                    msg_data.setdefault('id', msg_row['id'])
                    msg_data.setdefault('sessionID', session_id)
                    if 'time_created' in msg_row.keys() and msg_row['time_created'] is not None:
                        time_info = msg_data.setdefault('time', {})
                        if isinstance(time_info, dict) and 'created' not in time_info:
                            time_info['created'] = msg_row['time_created']

                    message, part_content, msg_time = assemble_message(
                        msg_data,
                        parts_by_message.get(msg_row['id'], [])
                    )
                    all_content.extend(part_content)

                    if msg_time:
                        if not first_message_time or msg_time < first_message_time:
                            first_message_time = msg_time
                        if not last_message_time or msg_time > last_message_time:
                            last_message_time = msg_time

                    messages.append(message)

                if not messages:
                    continue

                session_data = session_row_to_metadata(session_row)
                conversations.append(build_conversation(
                    session_id,
                    session_data,
                    messages,
                    all_content,
                    first_message_time,
                    last_message_time,
                ))
            except Exception as error:
                print(f"  Error processing session {session_id}: {error}")
                continue
    finally:
        conn.close()

    return conversations


def read_tauri_store(dat_file):
    """
    Parse Tauri store .dat files
    Format: Simple key-value pairs with length prefixes
    """
    try:
        with open(dat_file, 'rb') as f:
            data = f.read()

        store = {}
        offset = 0

        while offset < len(data):
            if offset + 4 > len(data):
                break

            key_len = struct.unpack('<I', data[offset:offset+4])[0]
            offset += 4

            if key_len > 10000 or offset + key_len > len(data):
                break

            key = data[offset:offset+key_len].decode('utf-8', errors='ignore')
            offset += key_len

            if offset + 4 > len(data):
                break

            value_len = struct.unpack('<I', data[offset:offset+4])[0]
            offset += 4

            if value_len > 1000000 or offset + value_len > len(data):
                break

            try:
                value_bytes = data[offset:offset+value_len]
                value = json.loads(value_bytes.decode('utf-8'))
                store[key] = value
            except Exception:
                pass

            offset += value_len

        return store

    except Exception as e:
        print(f"Error reading Tauri store {dat_file}: {e}")
        return {}


def extract_directory_from_content(text):
    """
    Try to extract a directory path from text content (e.g., tool commands).
    Looks for common patterns like 'cd /path/to/dir' or paths in commands.
    """
    if not text:
        return None

    cd_pattern = r'cd\s+(["\']?)([^\s\'"]+)\1'
    matches = re.findall(cd_pattern, text)
    for match in matches:
        path = match[1] if isinstance(match, tuple) else match
        if path and (path.startswith('/') or path.startswith('~') or path[1:].startswith(':')):
            return path

    cwd_pattern = r'(?:working\s+)?directory[:\s]+(["\']?)([^\s\'"]+)\1'
    matches = re.findall(cwd_pattern, text)
    for match in matches:
        path = match[1] if isinstance(match, tuple) else match
        if path and (path.startswith('/') or path.startswith('~') or path[1:].startswith(':')):
            return path

    abs_path_pattern = r'(?:^|\s|/)(/[^/\s\'"]{2,})'
    matches = re.findall(abs_path_pattern, text)
    for path in matches:
        if path and len(path) > 3 and not path.endswith('.') and not path.endswith('..'):
            return path

    return None


def extract_project_id_from_content(text):
    """
    Try to extract a project ID from text content.
    Often appears in tool commands or git operations.
    """
    if not text:
        return None

    project_pattern = r'(?:project[-_]?id|project)[=:\s]+([a-zA-Z0-9_-]+)'
    match = re.search(project_pattern, text, re.IGNORECASE)
    if match:
        return match.group(1)

    return None


def load_json_file(path):
    try:
        with open(path) as handle:
            return json.load(handle)
    except Exception as error:
        print(f"    Error reading {path}: {error}")
        return None


def find_json_session_file(storage_dir, session_id):
    session_root = storage_dir / 'storage' / 'session'
    candidates = [
        session_root / 'global' / f'{session_id}.json',
        session_root / 'info' / f'{session_id}.json',
    ]
    if session_root.exists():
        candidates.extend(session_root.glob(f'*/{session_id}.json'))

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def find_json_part_dir(part_root, session_id, message_id):
    candidates = [
        part_root / message_id,
        part_root / session_id / message_id,
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def extract_json_conversations(storage_dir):
    """
    Extract conversations from pre-1.14 CLI JSON storage.

    Handles sessions both WITH and WITHOUT session metadata files.
    For sessions without metadata, reconstructs session info from messages/parts.
    """
    conversations = []

    message_dir = storage_dir / 'storage' / 'message'
    part_dir = storage_dir / 'storage' / 'part'

    if not message_dir.exists():
        print(f"  Message directory not found: {message_dir}")
        return conversations

    session_dirs = [d for d in message_dir.iterdir() if d.is_dir() and d.name.startswith('ses_')]

    print(f"  Found {len(session_dirs)} session directories")

    processed_sessions = set()

    for session_dir_path in session_dirs:
        try:
            session_id = session_dir_path.name

            if session_id in processed_sessions:
                continue
            processed_sessions.add(session_id)

            session_data = None
            session_file = find_json_session_file(storage_dir, session_id)
            if session_file is not None:
                session_data = load_json_file(session_file)

            message_files = sorted(session_dir_path.glob('msg_*.json'))
            if not message_files:
                continue

            messages = []
            all_content = []
            first_message_time = None
            last_message_time = None

            for msg_file in message_files:
                msg_data = load_json_file(msg_file)
                if not msg_data:
                    continue

                message_id = msg_data.get('id')
                parts = []
                message_part_dir = find_json_part_dir(part_dir, session_id, message_id)
                if message_part_dir is not None:
                    for part_file in sorted(message_part_dir.glob('prt_*.json')):
                        part_data = load_json_file(part_file)
                        if part_data:
                            parts.append(part_data)

                message, part_content, msg_time = assemble_message(msg_data, parts)
                all_content.extend(part_content)

                if msg_time:
                    if not first_message_time or msg_time < first_message_time:
                        first_message_time = msg_time
                    if not last_message_time or msg_time > last_message_time:
                        last_message_time = msg_time

                messages.append(message)

            if not messages:
                continue

            conversations.append(build_conversation(
                session_id,
                session_data,
                messages,
                all_content,
                first_message_time,
                last_message_time,
            ))

        except Exception as e:
            print(f"  Error processing session {session_dir_path}: {e}")
            continue

    return conversations


def extract_cli_conversations(storage_dir):
    """Extract CLI conversations from SQLite when present, else JSON files."""
    conversations = []
    sqlite_dbs = find_sqlite_dbs(storage_dir)

    if sqlite_dbs:
        for db_path in sqlite_dbs:
            print(f"  Reading SQLite database: {db_path}")
            conversations.extend(extract_sqlite_conversations(db_path))
        if conversations:
            return conversations
        print("  SQLite database had no conversations, trying JSON storage")

    return extract_json_conversations(storage_dir)


def extract_desktop_conversations(desktop_dir):
    """Extract conversations from Desktop Tauri store files"""
    conversations = []

    dat_files = list(desktop_dir.rglob('*.dat'))

    if not dat_files:
        return conversations

    print(f"  Found {len(dat_files)} .dat store files")

    for dat_file in dat_files:
        store = read_tauri_store(dat_file)

        if not store:
            continue

        for key, value in store.items():
            if not isinstance(value, dict):
                continue

            if 'messages' in value or 'history' in value:
                try:
                    messages = value.get('messages', value.get('history', []))

                    if not messages:
                        continue

                    conversation = {
                        'messages': messages,
                        'source': 'opencode-desktop',
                        'store_key': key,
                        'store_file': str(dat_file.name)
                    }

                    for meta_key in ['session_id', 'title', 'created_at', 'workspace']:
                        if meta_key in value:
                            conversation[meta_key] = value[meta_key]

                    conversations.append(conversation)

                except Exception:
                    continue

    return conversations


def main():
    print("="*80)
    print("OPENCODE EXTRACTION")
    print("="*80)
    print()

    installations = find_opencode_installations()

    if not installations:
        print("❌ No OpenCode installations found!")
        print()
        print("Searched locations:")
        print("  CLI: ~/.local/share/opencode (Linux/macOS)")
        print("       ~/Library/Application Support/opencode (macOS)")
        print("  Desktop: ~/.local/share/ai.opencode.app (Linux)")
        print("           ~/Library/Application Support/ai.opencode.app (macOS)")
        return

    print(f"✅ Found {len(installations)} installation(s)")
    print()

    all_conversations = []

    for install_type, install_dir in installations:
        print(f"Processing {install_type} installation: {install_dir}")

        if install_type == 'cli':
            conversations = extract_cli_conversations(install_dir)
        else:
            conversations = extract_desktop_conversations(install_dir)

        print(f"  Extracted {len(conversations)} conversations")
        all_conversations.extend(conversations)
        print()

    if not all_conversations:
        print("❌ No conversation data found!")
        return

    print(f"✅ Total conversations extracted: {len(all_conversations)}")

    total_messages = sum(len(c['messages']) for c in all_conversations)
    with_tools = sum(1 for c in all_conversations
                     if any('tool_calls' in m or 'tool_results' in m
                           for m in c['messages']))
    with_models = sum(1 for c in all_conversations
                     if any('model' in m for m in c['messages']))
    with_reasoning = sum(1 for c in all_conversations
                        if any('reasoning' in m for m in c['messages']))

    with_session_file = sum(1 for c in all_conversations if c.get('directory'))
    without_session_file = len(all_conversations) - with_session_file

    print(f"Total messages: {total_messages}")
    print(f"With tool use: {with_tools}")
    print(f"With model info: {with_models}")
    print(f"With reasoning: {with_reasoning}")
    print(f"Full metadata (has session file): {with_session_file}")
    print(f"Reconstructed (no session file): {without_session_file}")
    print()

    output_dir = Path('extracted_data')
    output_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_file = output_dir / f'opencode_conversations_{timestamp}.jsonl'

    with open(output_file, 'w') as f:
        for conv in all_conversations:
            f.write(json.dumps(conv, ensure_ascii=False) + '\n')

    file_size = output_file.stat().st_size / 1024
    print(f"✅ Saved to: {output_file}")
    print(f"   Size: {file_size:.2f} KB")


if __name__ == '__main__':
    main()
