#!/usr/bin/env python3
"""
Extract ALL Google Gemini CLI chat data
Includes: messages, thoughts (reasoning), token usage, model info
Auto-discovers Gemini CLI installations on the device
"""

import json
from pathlib import Path
from datetime import datetime
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

def extract_gemini_session(session_file):
    """Extract conversation from a Gemini CLI session file"""
    try:
        with open(session_file, 'r') as f:
            data = json.load(f)

        if 'messages' not in data or not data['messages']:
            return None

        messages = []

        for msg in data['messages']:
            msg_type = msg.get('type')
            content = msg.get('content', '')

            if msg_type == 'user':
                normalized_msg = {
                    'role': 'user',
                    'content': content,
                    'timestamp': msg.get('timestamp')
                }
                messages.append(normalized_msg)

            elif msg_type == 'gemini':
                normalized_msg = {
                    'role': 'assistant',
                    'content': content,
                    'timestamp': msg.get('timestamp')
                }

                # Preserve Gemini-specific features
                if 'model' in msg:
                    normalized_msg['model'] = msg['model']

                if 'thoughts' in msg and msg['thoughts']:
                    normalized_msg['thoughts'] = msg['thoughts']

                if 'tokens' in msg and msg['tokens']:
                    normalized_msg['tokens'] = msg['tokens']

                messages.append(normalized_msg)

        if not messages:
            return None

        conv = {
            'messages': messages,
            'source': 'gemini-cli',
            'session_id': data.get('sessionId'),
            'project_hash': data.get('projectHash'),
            'start_time': data.get('startTime'),
            'last_updated': data.get('lastUpdated'),
            'source_file': str(session_file)
        }

        return conv

    except (json.JSONDecodeError, KeyError, Exception) as e:
        return None

def find_all_gemini_sessions(installation):
    """Find all Gemini CLI session files in an installation"""
    session_files = []

    # Search for session files in tmp/[hash]/chats/session-*.json pattern
    tmp_dir = installation / 'tmp'
    if tmp_dir.exists():
        # Find all session-*.json files under tmp/*/chats/
        session_files.extend(list(tmp_dir.rglob('chats/session-*.json')))

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
        for session_file in session_files:
            conv = extract_gemini_session(session_file)
            if conv:
                conv['installation'] = str(installation)
                conversations.append(conv)

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
    with_thoughts = sum(1 for c in all_conversations
                       if any('thoughts' in m for m in c['messages']))
    complete = sum(1 for c in all_conversations
                   if any(m['role'] == 'assistant' for m in c['messages']))

    print(f"Complete conversations: {complete:,}")
    print(f"Total messages: {total_messages:,}")
    print(f"With thoughts: {with_thoughts:,}")
    print()

    print("Breakdown by installation:")
    for inst, count in sorted(installation_stats.items(), key=lambda x: -x[1]):
        print(f"  {Path(inst).name:20} {count:5,} conversations")
    print()

    # Save to organized JSONL
    output_dir = Path('extracted_data')
    output_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_file = output_dir / f'gemini_conversations_{timestamp}.jsonl'

    with open(output_file, 'w') as f:
        for conv in all_conversations:
            f.write(json.dumps(conv, ensure_ascii=False) + '\n')

    file_size = output_file.stat().st_size / 1024 / 1024
    print(f"✅ Saved to: {output_file}")
    print(f"   Size: {file_size:.2f} MB")
    print(f"   Format: JSONL (one conversation per line)")

if __name__ == '__main__':
    main()

