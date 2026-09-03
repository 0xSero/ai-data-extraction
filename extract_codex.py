#!/usr/bin/env python3
"""
Extract ALL Codex chat data from all projects
Includes: messages, code context, diffs, file references
Auto-discovers Codex installations on the device
"""

import json
from pathlib import Path
from datetime import datetime
import platform
import os

def find_codex_installations():
    """Find all Codex installation directories"""
    system = platform.system()
    home = Path.home()

    locations = []

    # Search patterns for Codex directories
    codex_patterns = [
        'codex', 'codex-local', '.codex', '.codex-local'
    ]

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

        for pattern in codex_patterns:
            codex_dir = base_dir / pattern
            if codex_dir.exists():
                locations.append(codex_dir)

    return list(set(locations))

def extract_codex_session(session_file):
    """Extract conversation from a Codex rollout file with full context"""
    messages = []
    session_meta = {}
    tool_results = []

    with open(session_file, 'r') as f:
        for line in f:
            try:
                obj = json.loads(line)
                event_type = obj.get('type')

                if event_type == 'session_meta':
                    session_meta = obj.get('payload', {})

                elif event_type == 'event_msg':
                    payload = obj.get('payload', {})
                    payload_type = payload.get('type')

                    if payload_type == 'user_message':
                        message_text = payload.get('message', '').strip()
                        if message_text:
                            msg = {
                                'role': 'user',
                                'content': message_text,
                                'timestamp': obj.get('timestamp')
                            }

                            # Add context if available
                            if 'context' in payload:
                                msg['context'] = payload['context']

                            messages.append(msg)

                    elif payload_type == 'agent_message':
                        message_text = payload.get('message', '').strip()
                        if message_text:
                            msg = {
                                'role': 'assistant',
                                'content': message_text,
                                'timestamp': obj.get('timestamp')
                            }

                            # Add model info if available
                            if 'model' in payload:
                                msg['model'] = payload['model']

                            messages.append(msg)

                    elif payload_type == 'tool_use':
                        # Code execution, file edits, etc.
                        tool_use = {
                            'type': 'tool_use',
                            'tool': payload.get('tool'),
                            'input': payload.get('input'),
                            'timestamp': obj.get('timestamp')
                        }
                        tool_results.append(tool_use)

                    elif payload_type == 'tool_result':
                        # Results from tool execution (diffs, outputs, etc.)
                        tool_result = {
                            'type': 'tool_result',
                            'tool': payload.get('tool'),
                            'output': payload.get('output'),
                            'timestamp': obj.get('timestamp')
                        }
                        tool_results.append(tool_result)

                    elif payload_type == 'diff':
                        # Code diffs
                        diff = {
                            'type': 'diff',
                            'file': payload.get('file'),
                            'diff': payload.get('diff'),
                            'timestamp': obj.get('timestamp')
                        }
                        tool_results.append(diff)

            except json.JSONDecodeError:
                continue

    if messages:
        conv = {
            'messages': messages,
            'session_id': session_meta.get('id'),
            'cwd': session_meta.get('cwd'),
            'source': 'codex',
            'session_file': str(session_file),
            'timestamp': session_meta.get('timestamp')
        }

        if tool_results:
            conv['tool_results'] = tool_results

        return conv

    return None

def find_all_codex_sessions(installation):
    """Find all Codex session files in an installation"""
    session_files = []

    # Check for sessions directory
    sessions_dir = installation / 'sessions'
    if sessions_dir.exists():
        # Sessions are organized by date: YYYY/MM/DD/rollout-*.jsonl
        session_files.extend(list(sessions_dir.rglob('rollout-*.jsonl')))

    # Also check for project-based structure
    projects_dir = installation / 'projects'
    if projects_dir.exists():
        session_files.extend(list(projects_dir.rglob('*.jsonl')))

    return session_files

def main():
    print("="*80)
    print("CODEX COMPLETE DATA EXTRACTION")
    print("="*80)
    print()

    # Find all Codex installations
    print("🔍 Searching for Codex installations...")
    installations = find_codex_installations()

    if not installations:
        print("❌ No Codex installations found!")
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

        session_files = find_all_codex_sessions(installation)
        print(f"   Found {len(session_files)} session files")

        conversations = []
        for session_file in session_files:
            conv = extract_codex_session(session_file)
            if conv:
                conv['installation'] = str(installation)
                conversations.append(conv)

        if conversations:
            all_conversations.extend(conversations)
            installation_stats[str(installation)] = len(conversations)
            print(f"   ✅ {len(conversations)} conversations")
        else:
            print("   ⚠️  No conversations found")

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
    with_tools = sum(1 for c in all_conversations if 'tool_results' in c)
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

    # Save to organized JSONL
    output_dir = Path('extracted_data')
    output_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_file = output_dir / f'codex_conversations_{timestamp}.jsonl'

    with open(output_file, 'w') as f:
        for conv in all_conversations:
            f.write(json.dumps(conv, ensure_ascii=False) + '\n')

    file_size = output_file.stat().st_size / 1024 / 1024
    print(f"✅ Saved to: {output_file}")
    print(f"   Size: {file_size:.2f} MB")
    print("   Format: JSONL (one conversation per line)")

if __name__ == '__main__':
    main()
