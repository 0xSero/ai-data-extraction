#!/usr/bin/env python3
"""
Extract ALL Windsurf chat and agent data from all projects
Includes: messages, code context, diffs, file references
Auto-discovers Windsurf installations on the device
"""

import json
import sqlite3
from pathlib import Path
import platform
import os
from collections import defaultdict

def find_windsurf_installations():
    """Find all Windsurf installation directories"""
    system = platform.system()
    home = Path.home()

    locations = []

    # Windsurf patterns
    windsurf_patterns = ['Windsurf', 'windsurf', '.windsurf']

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

    for base_dir in base_dirs:
        if not base_dir.exists():
            continue

        for pattern in windsurf_patterns:
            windsurf_dir = base_dir / pattern
            if windsurf_dir.exists():
                locations.append(windsurf_dir)

    return list(set(locations))

def extract_windsurf_chat(db_path, workspace_id):
    """Extract Windsurf chat conversations (similar to VSCode/Cursor format)"""
    conversations = []

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Try different key patterns
        keys_to_try = [
            'workbench.panel.aichat.view.aichat.chatdata',
            'aiChat.chatdata',
            'chat.data',
            'cascade.chatdata'  # Windsurf might use Cascade branding
        ]

        for key in keys_to_try:
            try:
                cursor.execute(f"SELECT value FROM ItemTable WHERE key = ?", (key,))
                result = cursor.fetchone()

                if result:
                    data = json.loads(result[0])

                    if 'tabs' in data:
                        for tab in data['tabs']:
                            if 'bubbles' in tab and len(tab['bubbles']) > 0:
                                messages = []
                                code_context = []

                                for bubble in tab['bubbles']:
                                    bubble_type = bubble.get('type')
                                    content = bubble.get('rawText', bubble.get('text', ''))

                                    msg = {
                                        'role': 'user' if bubble_type == 'user' else 'assistant',
                                        'content': content
                                    }

                                    # Extract code context
                                    if 'selections' in bubble and bubble['selections']:
                                        ctx = []
                                        for sel in bubble['selections']:
                                            if 'uri' in sel and 'fsPath' in sel['uri']:
                                                ctx.append({
                                                    'file': sel['uri']['fsPath'],
                                                    'code': sel.get('text', sel.get('rawText', '')),
                                                    'range': sel.get('range')
                                                })
                                        if ctx:
                                            msg['code_context'] = ctx
                                            code_context.extend(ctx)

                                    # Extract diffs
                                    if 'suggestedDiffs' in bubble:
                                        msg['suggested_diffs'] = bubble['suggestedDiffs']

                                    messages.append(msg)

                                if messages:
                                    conversations.append({
                                        'messages': messages,
                                        'source': 'windsurf-chat',
                                        'chat_title': tab.get('chatTitle'),
                                        'tab_id': tab.get('tabId'),
                                        'workspace_id': workspace_id,
                                        'has_code_context': len(code_context) > 0
                                    })

                    break  # Found data, no need to try other keys

            except:
                continue

        conn.close()

    except Exception as e:
        pass

    return conversations

def extract_windsurf_agent(global_db_path):
    """Extract Windsurf agent/flow conversations"""
    conversations = []

    try:
        conn = sqlite3.connect(global_db_path)
        cursor = conn.cursor()

        # Try different table formats
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]

        # Check for cursorDiskKV (if Windsurf uses similar format to Cursor)
        if 'cursorDiskKV' in tables:
            cursor.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%' OR key LIKE 'agentData:%' OR key LIKE 'flowData:%'")
            results = cursor.fetchall()

            for key, value in results:
                if not value:
                    continue

                try:
                    data = json.loads(value)
                    conv = extract_agent_conversation(data, key)
                    if conv:
                        conversations.append(conv)
                except:
                    continue

        # Also check ItemTable
        if 'ItemTable' in tables:
            cursor.execute("SELECT key, value FROM ItemTable WHERE key LIKE '%agent%' OR key LIKE '%flow%' OR key LIKE '%cascade%'")
            results = cursor.fetchall()

            for key, value in results:
                if not value:
                    continue

                try:
                    data = json.loads(value)
                    conv = extract_agent_conversation(data, key)
                    if conv:
                        conversations.append(conv)
                except:
                    continue

        conn.close()

    except Exception as e:
        pass

    return conversations

def extract_agent_conversation(data, key):
    """Extract agent conversation from data object"""
    if not isinstance(data, dict):
        return None

    messages = []

    # Try different conversation formats
    if 'conversation' in data and isinstance(data['conversation'], list):
        for bubble in data['conversation']:
            bubble_type = bubble.get('type')
            text = bubble.get('text', '')

            if bubble_type == 1 or bubble.get('role') == 'user':
                msg = {
                    'role': 'user',
                    'content': text
                }

                # Add context
                if 'context' in bubble:
                    context = bubble['context']
                    if 'selections' in context:
                        ctx = []
                        for sel in context['selections']:
                            if 'uri' in sel and 'fsPath' in sel['uri']:
                                ctx.append({
                                    'file': sel['uri']['fsPath'],
                                    'code': sel.get('text', sel.get('rawText', '')),
                                    'range': sel.get('range')
                                })
                        if ctx:
                            msg['code_context'] = ctx

                messages.append(msg)

            elif bubble_type == 2 or bubble.get('role') == 'assistant':
                msg = {
                    'role': 'assistant',
                    'content': text
                }

                # Add diffs
                if 'suggestedCodeBlocks' in bubble:
                    msg['suggested_code_blocks'] = bubble['suggestedCodeBlocks']
                if 'diffHistories' in bubble:
                    msg['diff_histories'] = bubble['diffHistories']

                messages.append(msg)

    if messages:
        return {
            'messages': messages,
            'source': 'windsurf-agent',
            'name': data.get('name', 'Untitled'),
            'status': data.get('status'),
            'created_at': data.get('createdAt'),
            'updated_at': data.get('lastUpdatedAt')
        }

    return None

def main():
    print("="*80)
    print("WINDSURF COMPLETE DATA EXTRACTION (Chat + Agent)")
    print("="*80)
    print()

    # Find all Windsurf installations
    print("🔍 Searching for Windsurf installations...")
    installations = find_windsurf_installations()

    if not installations:
        print("❌ No Windsurf installations found!")
        return

    print(f"✅ Found {len(installations)} installation(s):")
    for inst in installations:
        print(f"   - {inst}")
    print()

    all_conversations = []
    stats = defaultdict(int)

    for installation in installations:
        print(f"📂 Processing: {installation}")

        # Extract Chat mode (workspace storage)
        workspace_storage = installation / 'User/workspaceStorage'
        if workspace_storage.exists():
            workspace_count = 0
            for workspace in workspace_storage.iterdir():
                if workspace.is_dir():
                    db_file = workspace / 'state.vscdb'
                    if db_file.exists():
                        convs = extract_windsurf_chat(db_file, workspace.name)
                        all_conversations.extend(convs)
                        workspace_count += len(convs)

            print(f"   ✅ Chat mode: {workspace_count} conversations")
            stats['chat'] += workspace_count

        # Extract Agent/Flow mode (global storage)
        global_storage = installation / 'User/globalStorage/state.vscdb'
        if global_storage.exists():
            convs = extract_windsurf_agent(global_storage)
            all_conversations.extend(convs)
            print(f"   ✅ Agent/Flow: {len(convs)} conversations")
            stats['agent'] += len(convs)
        else:
            print(f"   ⚠️  No global storage found")

    print()
    print("="*80)
    print("EXTRACTION COMPLETE")
    print("="*80)
    print(f"Total conversations: {len(all_conversations):,}")
    print(f"  Chat mode: {stats['chat']:,}")
    print(f"  Agent/Flow: {stats['agent']:,}")

    if not all_conversations:
        print("No conversations found!")
        return

    # Statistics
    total_messages = sum(len(c['messages']) for c in all_conversations)
    with_code = sum(1 for c in all_conversations if c.get('has_code_context'))
    complete = sum(1 for c in all_conversations
                   if any(m['role'] == 'assistant' for m in c['messages']))

    print(f"Complete conversations: {complete:,}")
    print(f"Total messages: {total_messages:,}")
    print(f"With code context: {with_code:,}")
    print()

    # Save: one HF-traces JSONL file per session (one message per line)
    from traces_export import write_session_files
    n_files, n_lines = write_session_files(all_conversations, 'windsurf')
    print(f"✅ Saved {n_files} session file(s) to extracted_data/windsurf/sessions/")
    print(f"   Total message lines: {n_lines:,}")
    print(f"   Format: HF-traces JSONL (one message per line, one file per session)")

if __name__ == '__main__':
    main()
