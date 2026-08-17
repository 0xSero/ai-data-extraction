#!/usr/bin/env python3
"""
Extract ALL Trae chat and agent data from all projects
Includes: messages, code context, diffs, file references
Auto-discovers Trae installations on the device
"""

import json
import sqlite3
from pathlib import Path
import platform
import os

def find_trae_installations():
    """Find all Trae installation directories"""
    system = platform.system()
    home = Path.home()

    locations = []

    # Search patterns for Trae directories
    trae_patterns = ['trae', '.trae', 'Trae']

    if system == "Darwin":  # macOS
        base_dirs = [
            home / "Library/Application Support",
            home / ".config",
            home
        ]
    elif system == "Linux":
        base_dirs = [
            home / ".config",
            home / ".local/share",
            home
        ]
    elif system == "Windows":
        base_dirs = [
            Path(os.environ.get('APPDATA', home / 'AppData/Roaming')),
            Path(os.environ.get('LOCALAPPDATA', home / 'AppData/Local')),
            home
        ]
    else:
        base_dirs = [home / ".config", home]

    for base_dir in base_dirs:
        if not base_dir.exists():
            continue

        for pattern in trae_patterns:
            trae_dir = base_dir / pattern
            if trae_dir.exists():
                locations.append(trae_dir)

    return list(set(locations))

def extract_trae_data(installation):
    """Extract Trae conversations from various storage formats"""
    conversations = []

    # Trae might use different storage formats
    # Check for common patterns

    # 1. Check for JSONL files in projects directory
    projects_dir = installation / 'projects'
    if projects_dir.exists():
        for project in projects_dir.iterdir():
            if project.is_dir():
                for jsonl_file in project.glob('*.jsonl'):
                    convs = extract_from_jsonl(jsonl_file, 'trae')
                    conversations.extend(convs)

    # 2. Check for SQLite databases
    for db_file in installation.rglob('*.db'):
        convs = extract_from_sqlite(db_file, 'trae')
        conversations.extend(convs)

    for vscdb_file in installation.rglob('*.vscdb'):
        convs = extract_from_sqlite(vscdb_file, 'trae')
        conversations.extend(convs)

    # 3. Check for sessions directory
    sessions_dir = installation / 'sessions'
    if sessions_dir.exists():
        for jsonl_file in sessions_dir.rglob('*.jsonl'):
            convs = extract_from_jsonl(jsonl_file, 'trae')
            conversations.extend(convs)

    return conversations

def extract_from_jsonl(jsonl_file, source):
    """Extract conversations from JSONL format"""
    conversations = []

    try:
        messages = []
        metadata = {}

        with open(jsonl_file, 'r') as f:
            for line in f:
                if not line.strip():
                    continue

                try:
                    obj = json.loads(line)

                    # Handle different JSONL formats
                    msg_type = obj.get('type', obj.get('role'))

                    if msg_type in ['user', 'user_message']:
                        content = obj.get('message', obj.get('content', ''))
                        msg = {
                            'role': 'user',
                            'content': content,
                            'timestamp': obj.get('timestamp')
                        }

                        # Add context
                        if 'context' in obj:
                            msg['context'] = obj['context']
                        if 'files' in obj:
                            msg['files'] = obj['files']

                        messages.append(msg)

                    elif msg_type in ['assistant', 'agent', 'agent_message']:
                        content = obj.get('message', obj.get('content', ''))
                        msg = {
                            'role': 'assistant',
                            'content': content,
                            'timestamp': obj.get('timestamp')
                        }

                        # Add tool use / diffs
                        if 'tool_use' in obj:
                            msg['tool_use'] = obj['tool_use']
                        if 'diffs' in obj:
                            msg['diffs'] = obj['diffs']
                        if 'edits' in obj:
                            msg['edits'] = obj['edits']

                        messages.append(msg)

                    elif msg_type == 'metadata':
                        metadata.update(obj.get('data', {}))

                except json.JSONDecodeError:
                    continue

        if messages:
            conversations.append({
                'messages': messages,
                'source': source,
                'source_file': str(jsonl_file),
                **metadata
            })

    except Exception as e:
        print(f"Error processing {jsonl_file}: {e}")

    return conversations

def extract_from_sqlite(db_file, source):
    """Extract conversations from SQLite database"""
    conversations = []

    try:
        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()

        # Try common table/key patterns
        try:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in cursor.fetchall()]

            # Check for common patterns
            if 'ItemTable' in tables:
                cursor.execute("SELECT key, value FROM ItemTable WHERE key LIKE '%chat%' OR key LIKE '%conversation%' OR key LIKE '%agent%'")
                results = cursor.fetchall()

                for key, value in results:
                    if not value:
                        continue

                    try:
                        data = json.loads(value)
                        conv = extract_conversation_from_data(data, source, str(db_file))
                        if conv:
                            conversations.append(conv)
                    except:
                        continue

        except Exception as e:
            pass

        conn.close()

    except Exception as e:
        pass

    return conversations

def extract_conversation_from_data(data, source, source_file):
    """Extract conversation from data object"""
    if not isinstance(data, dict):
        return None

    messages = []

    # Try different formats
    if 'messages' in data:
        messages = data['messages']
    elif 'conversation' in data:
        conv_data = data['conversation']
        if isinstance(conv_data, list):
            for item in conv_data:
                if isinstance(item, dict) and 'role' in item:
                    messages.append(item)

    if messages:
        return {
            'messages': messages,
            'source': source,
            'source_file': source_file,
            **{k: v for k, v in data.items() if k not in ['messages', 'conversation']}
        }

    return None

def main():
    print("="*80)
    print("TRAE COMPLETE DATA EXTRACTION (Chat + Agent)")
    print("="*80)
    print()

    # Find all Trae installations
    print("🔍 Searching for Trae installations...")
    installations = find_trae_installations()

    if not installations:
        print("❌ No Trae installations found!")
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

        conversations = extract_trae_data(installation)

        if conversations:
            all_conversations.extend(conversations)
            installation_stats[str(installation)] = len(conversations)
            print(f"   ✅ {len(conversations)} conversations")
        else:
            print(f"   ⚠️  No conversations found")

    print()
    print("="*80)
    print("EXTRACTION COMPLETE")
    print("="*80)
    print(f"Total conversations: {len(all_conversations):,}")

    if not all_conversations:
        print("No conversations found!")
        return

    # Statistics
    total_messages = sum(len(c['messages']) for c in all_conversations)
    with_tools = sum(1 for c in all_conversations
                     if any('tool_use' in m or 'diffs' in m or 'edits' in m
                           for m in c['messages']))
    complete = sum(1 for c in all_conversations
                   if any(m['role'] == 'assistant' for m in c['messages']))

    print(f"Complete conversations: {complete:,}")
    print(f"Total messages: {total_messages:,}")
    print(f"With tool use/diffs: {with_tools:,}")
    print()

    print("Breakdown by installation:")
    for inst, count in sorted(installation_stats.items(), key=lambda x: -x[1]):
        print(f"  {Path(inst).name:20} {count:5,} conversations")
    print()

    # Save: one HF-traces JSONL file per session (one message per line)
    from traces_export import write_session_files
    n_files, n_lines = write_session_files(all_conversations, 'trae')
    print(f"✅ Saved {n_files} session file(s) to extracted_data/trae/sessions/")
    print(f"   Total message lines: {n_lines:,}")
    print(f"   Format: HF-traces JSONL (one message per line, one file per session)")

if __name__ == '__main__':
    main()
