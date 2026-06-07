#!/usr/bin/env python3
"""
Extract ALL Devin chat and agent data
Includes: messages, tool calls, tool results, diffs, file references, workspace metadata
Auto-discovers Devin installations on the device
"""

import json
import os
import platform
import sqlite3
from datetime import datetime
from pathlib import Path


def find_devin_installations():
    """Find all Devin installation and data directories"""
    system = platform.system()
    home = Path.home()

    locations = []
    devin_patterns = ['devin', 'Devin', '.devin', 'ai.devin.app', 'com.devin.app']

    if system == "Darwin":
        base_dirs = [
            home / "Library/Application Support",
            home / "Library/Caches",
            home / ".config",
            home,
        ]
    elif system == "Linux":
        base_dirs = [
            home / ".config",
            home / ".local/share",
            home / ".cache",
            home,
        ]
    elif system == "Windows":
        base_dirs = [
            Path(os.environ.get('APPDATA', home / 'AppData/Roaming')),
            Path(os.environ.get('LOCALAPPDATA', home / 'AppData/Local')),
            home,
        ]
    else:
        base_dirs = [home / ".config", home / ".local/share", home]

    for base_dir in base_dirs:
        if not base_dir.exists():
            continue

        for pattern in devin_patterns:
            devin_dir = base_dir / pattern
            if devin_dir.exists():
                locations.append(devin_dir)

    return sorted(set(locations))


def normalize_content(content):
    """Normalize common message content shapes to text"""
    if content is None:
        return ''
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get('text') or item.get('content') or item.get('message')
                if text:
                    parts.append(str(text))
        return '\n'.join(parts)
    if isinstance(content, dict):
        for key in ['text', 'content', 'message', 'body', 'value']:
            if key in content:
                return normalize_content(content[key])
    return str(content)


def normalize_role(role):
    """Normalize common Devin/event roles to user or assistant"""
    if role is None:
        return None
    role_text = str(role).lower()
    if role_text in ['user', 'human', 'client', 'developer']:
        return 'user'
    if role_text in ['assistant', 'agent', 'devin', 'ai', 'bot']:
        return 'assistant'
    if 'user' in role_text or 'human' in role_text:
        return 'user'
    if 'assistant' in role_text or 'agent' in role_text or 'devin' in role_text:
        return 'assistant'
    return None


def looks_like_message(obj):
    """Return True when a dict resembles a chat message or agent event"""
    if not isinstance(obj, dict):
        return False

    role = normalize_role(obj.get('role') or obj.get('author') or obj.get('sender') or obj.get('type'))
    has_text = any(key in obj for key in ['content', 'text', 'message', 'body'])
    has_tooling = any(key in obj for key in ['tool_calls', 'toolCalls', 'tool_use', 'toolUse', 'tool_results', 'toolResults', 'diffs', 'edits'])
    return bool(role and (has_text or has_tooling))


def normalize_message(obj):
    """Normalize a Devin message/event dict"""
    role = normalize_role(obj.get('role') or obj.get('author') or obj.get('sender') or obj.get('type'))
    if not role:
        return None

    message = {
        'role': role,
        'content': normalize_content(obj.get('content', obj.get('text', obj.get('message', obj.get('body', ''))))),
    }

    for source_key, target_key in [
        ('timestamp', 'timestamp'),
        ('created_at', 'created_at'),
        ('createdAt', 'created_at'),
        ('updated_at', 'updated_at'),
        ('updatedAt', 'updated_at'),
        ('model', 'model'),
        ('modelName', 'model'),
        ('model_id', 'model'),
        ('modelId', 'model'),
        ('tool_calls', 'tool_calls'),
        ('toolCalls', 'tool_calls'),
        ('tool_use', 'tool_use'),
        ('toolUse', 'tool_use'),
        ('tool_results', 'tool_results'),
        ('toolResults', 'tool_results'),
        ('diffs', 'diffs'),
        ('edits', 'edits'),
        ('files', 'files'),
        ('file_references', 'file_references'),
        ('context', 'context'),
        ('code_context', 'code_context'),
        ('workspace', 'workspace'),
        ('event_id', 'event_id'),
        ('id', 'message_id'),
    ]:
        if source_key in obj and obj[source_key] not in (None, '', [], {}):
            message[target_key] = obj[source_key]

    return message


def find_message_lists(data):
    """Find candidate message lists inside nested JSON data"""
    candidates = []

    def visit(value, path):
        if isinstance(value, list):
            normalized = []
            for item in value:
                if looks_like_message(item):
                    msg = normalize_message(item)
                    if msg:
                        normalized.append(msg)
            if normalized:
                candidates.append((path, normalized))

            for idx, item in enumerate(value):
                if isinstance(item, (dict, list)):
                    visit(item, path + [str(idx)])

        elif isinstance(value, dict):
            for key in ['messages', 'conversation', 'history', 'events', 'turns', 'transcript']:
                if key in value and isinstance(value[key], list):
                    visit(value[key], path + [key])

            for key, item in value.items():
                if key not in ['messages', 'conversation', 'history', 'events', 'turns', 'transcript'] and isinstance(item, (dict, list)):
                    visit(item, path + [key])

    visit(data, [])
    return candidates


def build_conversation(data, messages, source_file, path=None):
    """Build a normalized conversation record"""
    conversation = {
        'messages': messages,
        'source': 'devin',
        'source_file': str(source_file),
    }

    if path:
        conversation['json_path'] = '.'.join(path)

    if isinstance(data, dict):
        for source_key, target_key in [
            ('session_id', 'session_id'),
            ('sessionId', 'session_id'),
            ('conversation_id', 'conversation_id'),
            ('conversationId', 'conversation_id'),
            ('id', 'session_id'),
            ('title', 'title'),
            ('name', 'title'),
            ('workspace', 'workspace'),
            ('workspace_path', 'workspace_path'),
            ('workspacePath', 'workspace_path'),
            ('project_path', 'project_path'),
            ('projectPath', 'project_path'),
            ('repository', 'repository'),
            ('repo', 'repository'),
            ('created_at', 'created_at'),
            ('createdAt', 'created_at'),
            ('updated_at', 'updated_at'),
            ('updatedAt', 'updated_at'),
        ]:
            if source_key in data and data[source_key] not in (None, '', [], {}):
                conversation[target_key] = data[source_key]

    return conversation


def extract_from_json_file(json_file):
    """Extract conversations from a JSON file"""
    conversations = []

    try:
        with open(json_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return conversations

    for path, messages in find_message_lists(data):
        if messages:
            conversations.append(build_conversation(data, messages, json_file, path))

    return conversations


def extract_from_jsonl_file(jsonl_file):
    """Extract conversations from a JSONL session/event file"""
    conversations = []
    messages = []
    metadata = {}

    try:
        with open(jsonl_file, 'r', encoding='utf-8') as f:
            for line in f:
                if not line.strip():
                    continue

                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if looks_like_message(obj):
                    msg = normalize_message(obj)
                    if msg:
                        messages.append(msg)
                elif isinstance(obj, dict):
                    for key in ['session_id', 'sessionId', 'conversation_id', 'conversationId', 'title', 'workspace', 'project_path', 'created_at', 'createdAt']:
                        if key in obj and obj[key] not in (None, '', [], {}):
                            metadata[key] = obj[key]

                    for path, nested_messages in find_message_lists(obj):
                        if nested_messages:
                            conversations.append(build_conversation(obj, nested_messages, jsonl_file, path))

        if messages:
            conversation = build_conversation(metadata, messages, jsonl_file)
            conversations.append(conversation)

    except Exception as e:
        print(f"Error processing {jsonl_file}: {e}")

    return conversations


def extract_from_sqlite(db_file):
    """Extract conversations from SQLite key-value stores"""
    conversations = []

    try:
        conn = sqlite3.connect(f'file:{db_file}?mode=ro', uri=True)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]

        key_value_tables = []
        for table in tables:
            try:
                cursor.execute(f"PRAGMA table_info({table})")
                columns = [row[1] for row in cursor.fetchall()]
                if 'value' in columns and ('key' in columns or 'id' in columns):
                    key_column = 'key' if 'key' in columns else 'id'
                    key_value_tables.append((table, key_column))
            except Exception:
                continue

        for table, key_column in key_value_tables:
            try:
                cursor.execute(f"SELECT {key_column}, value FROM {table}")
                rows = cursor.fetchall()
            except Exception:
                continue

            for key, value in rows:
                if not value or not isinstance(value, str):
                    continue

                key_text = str(key).lower()
                if not any(token in key_text for token in ['devin', 'chat', 'conversation', 'session', 'message', 'agent', 'history']):
                    continue

                try:
                    data = json.loads(value)
                except Exception:
                    continue

                for path, messages in find_message_lists(data):
                    if messages:
                        conversation = build_conversation(data, messages, db_file, path)
                        conversation['storage_key'] = str(key)
                        conversation['storage_table'] = table
                        conversations.append(conversation)

        conn.close()

    except Exception:
        pass

    return conversations


def extract_devin_data(installation):
    """Extract Devin conversations from known storage formats"""
    conversations = []

    for jsonl_file in installation.rglob('*.jsonl'):
        conversations.extend(extract_from_jsonl_file(jsonl_file))

    for json_file in installation.rglob('*.json'):
        conversations.extend(extract_from_json_file(json_file))

    for db_file in list(installation.rglob('*.db')) + list(installation.rglob('*.sqlite')) + list(installation.rglob('*.sqlite3')) + list(installation.rglob('*.vscdb')):
        conversations.extend(extract_from_sqlite(db_file))

    for conversation in conversations:
        conversation['installation'] = str(installation)

    return conversations


def dedupe_conversations(conversations):
    """Remove duplicate conversations found through overlapping scans"""
    seen = set()
    unique = []

    for conversation in conversations:
        key = (
            conversation.get('source_file'),
            conversation.get('json_path'),
            conversation.get('storage_key'),
            json.dumps(conversation.get('messages', []), sort_keys=True, ensure_ascii=False),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(conversation)

    return unique


def main():
    print("="*80)
    print("DEVIN COMPLETE DATA EXTRACTION")
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
    installation_stats = {}

    for installation in installations:
        print(f"📂 Processing: {installation}")
        conversations = dedupe_conversations(extract_devin_data(installation))

        if conversations:
            all_conversations.extend(conversations)
            installation_stats[str(installation)] = len(conversations)
            print(f"   ✅ {len(conversations)} conversations")
        else:
            print("   ⚠️  No conversations found")

    all_conversations = dedupe_conversations(all_conversations)

    print()
    print("="*80)
    print("EXTRACTION COMPLETE")
    print("="*80)
    print(f"Total conversations: {len(all_conversations):,}")

    if not all_conversations:
        print("No conversations found!")
        return

    total_messages = sum(len(c['messages']) for c in all_conversations)
    with_tools = sum(1 for c in all_conversations if any(
        'tool_calls' in m or 'tool_use' in m or 'tool_results' in m or 'diffs' in m or 'edits' in m
        for m in c['messages']
    ))
    complete = sum(1 for c in all_conversations if any(m['role'] == 'assistant' for m in c['messages']))

    print(f"Complete conversations: {complete:,}")
    print(f"Total messages: {total_messages:,}")
    print(f"With tool use/diffs: {with_tools:,}")
    print()

    print("Breakdown by installation:")
    for inst, count in sorted(installation_stats.items(), key=lambda x: -x[1]):
        print(f"  {Path(inst).name:20} {count:5,} conversations")
    print()

    output_dir = Path('extracted_data')
    output_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_file = output_dir / f'devin_conversations_{timestamp}.jsonl'

    with open(output_file, 'w', encoding='utf-8') as f:
        for conv in all_conversations:
            f.write(json.dumps(conv, ensure_ascii=False) + '\n')

    file_size = output_file.stat().st_size / 1024 / 1024
    print(f"✅ Saved to: {output_file}")
    print(f"   Size: {file_size:.2f} MB")
    print("   Format: JSONL (one conversation per line)")


if __name__ == '__main__':
    main()
