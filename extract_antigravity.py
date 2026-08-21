#!/usr/bin/env python3
"""
Extract ALL Antigravity (Google's Windsurf fork) Cascade conversation data
Includes: user messages, assistant responses, reasoning traces (thinking steps)
Uses the local Connect-RPC HTTP API — requires Antigravity to be running
Auto-discovers port and CSRF token from the running language server process
"""

import json
import platform
import os
import re
import ssl
import subprocess
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime
from collections import defaultdict


def find_antigravity_installations():
    """Find all Antigravity data directories across platforms"""
    system = platform.system()
    home = Path.home()

    locations = []

    if system == "Darwin":  # macOS
        base_dirs = [
            home / ".gemini",
            home / "Library/Application Support/Antigravity",
            home / ".config",
        ]
    elif system == "Linux":
        base_dirs = [
            home / ".gemini",
            home / ".config",
            home / ".local/share",
        ]
    elif system == "Windows":
        base_dirs = [
            Path(os.environ.get('LOCALAPPDATA', home / 'AppData/Local')) / "Antigravity",
            home / ".gemini",
        ]
    else:
        base_dirs = [home / ".gemini", home / ".config"]

    for base_dir in base_dirs:
        if not base_dir.exists():
            continue

        for pattern in ['antigravity', 'Antigravity']:
            candidate = base_dir / pattern
            if (candidate / 'conversations').exists():
                locations.append(candidate)

        # base_dir itself may be the root (e.g. ~/.gemini/antigravity layout varies)
        if (base_dir / 'conversations').exists():
            locations.append(base_dir)

    return list(set(locations))


def find_language_server():
    """
    Locate the running Antigravity language server process.
    Returns (pid, csrf_token, port) or (None, None, None) if not found.
    """
    system = platform.system()

    try:
        if system == "Windows":
            result = subprocess.run(
                ['wmic', 'process', 'get', 'ProcessId,CommandLine'],
                capture_output=True, text=True
            )
        else:
            result = subprocess.run(['ps', 'aux'], capture_output=True, text=True)

        for line in result.stdout.splitlines():
            if 'language_server_' not in line:
                continue
            if 'grep' in line or 'csrf_token' not in line:
                continue

            csrf_match = re.search(r'--csrf_token\s+(\S+)', line)
            pid_match = re.match(r'\S+\s+(\d+)', line)
            if not csrf_match or not pid_match:
                continue

            pid = pid_match.group(1)
            csrf_token = csrf_match.group(1)
            port = _find_server_port(pid, system)
            if port:
                return pid, csrf_token, port

    except Exception:
        pass

    return None, None, None


def _find_server_port(pid, system):
    """
    Return the first TCP LISTEN port for the language server PID.
    Uses lsof -a so -p and -i TCP are AND-combined (macOS/Linux default is OR).
    """
    try:
        if system == "Windows":
            result = subprocess.run(
                ['netstat', '-ano'], capture_output=True, text=True
            )
            for line in result.stdout.splitlines():
                if 'LISTENING' in line and str(pid) in line:
                    m = re.search(r':(\d+)\s', line)
                    if m:
                        return int(m.group(1))
        else:
            result = subprocess.run(
                ['lsof', '-a', '-p', str(pid), '-i', 'TCP'],
                capture_output=True, text=True
            )
            for line in result.stdout.splitlines():
                if 'LISTEN' in line and 'language_' in line:
                    m = re.search(r':(\d+)\s+\(LISTEN\)', line)
                    if m:
                        return int(m.group(1))

    except Exception:
        pass

    return None


def _make_ssl_context():
    """SSL context that accepts the language server's self-signed certificate"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _api_post(base_url, csrf_token, ssl_ctx, method, body=None):
    """POST to the Antigravity Connect-RPC API. Returns parsed JSON or None."""
    headers = {
        'Content-Type': 'application/json',
        'Connect-Protocol-Version': '1',
        'X-Codeium-Csrf-Token': csrf_token,
    }
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        f'{base_url}/{method}', data=data, headers=headers, method='POST'
    )
    try:
        with urllib.request.urlopen(req, context=ssl_ctx) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _build_thoughts_index(steps):
    """
    Pre-pass over all steps: collect PLANNER_RESPONSE thinking text grouped
    by executionId. Multiple planner steps per turn are joined in order.
    Returns {executionId: "combined thinking text"}
    """
    thoughts = defaultdict(list)
    for step in steps:
        if step.get('type') != 'CORTEX_STEP_TYPE_PLANNER_RESPONSE':
            continue
        exec_id = step.get('metadata', {}).get('executionId')
        thinking = step.get('plannerResponse', {}).get('thinking', '').strip()
        if exec_id and thinking:
            thoughts[exec_id].append(thinking)
    return {k: '\n\n'.join(v) for k, v in thoughts.items()}


def extract_antigravity_cascade(base_url, csrf_token, ssl_ctx, installation=None):
    """
    Extract all Cascade conversations via the local Connect-RPC HTTP API.

    Each conversation includes user/assistant messages plus per-turn
    reasoning traces (thoughts) from the model's internal planning steps.
    """
    conversations = []

    list_resp = _api_post(base_url, csrf_token, ssl_ctx, 'GetAllCascadeTrajectories')
    if not list_resp:
        return conversations

    # Response shape: {"trajectorySummaries": {"<cascade-id>": {...}, ...}}
    traj_summaries = list_resp.get('trajectorySummaries') or {}
    if not traj_summaries:
        for v in list_resp.values():
            if isinstance(v, dict) and v:
                traj_summaries = v
                break

    for cascade_id, traj in traj_summaries.items():
        steps_resp = _api_post(
            base_url, csrf_token, ssl_ctx,
            'GetCascadeTrajectorySteps',
            {'cascadeId': cascade_id, 'startIndex': 0, 'endIndex': 9999}
        )
        if not steps_resp:
            continue

        steps = next((v for v in steps_resp.values() if isinstance(v, list)), [])
        thoughts_index = _build_thoughts_index(steps)
        messages = []

        for step in steps:
            step_type = step.get('type', '')
            meta = step.get('metadata', {})
            timestamp = meta.get('createdAt') or meta.get('completedAt')

            if step_type == 'CORTEX_STEP_TYPE_USER_INPUT':
                items = step.get('userInput', {}).get('items', [])
                parts = [i.get('text', '').strip() for i in items if i.get('text', '').strip()]
                if not parts:
                    continue
                msg = {'role': 'user', 'content': '\n'.join(parts)}
                if timestamp:
                    msg['timestamp'] = timestamp
                messages.append(msg)

            elif step_type == 'CORTEX_STEP_TYPE_NOTIFY_USER':
                content = step.get('notifyUser', {}).get('notificationContent', '').strip()
                if not content:
                    continue
                msg = {'role': 'assistant', 'content': content}
                if timestamp:
                    msg['timestamp'] = timestamp
                model = meta.get('generatorModel')
                if model:
                    msg['model'] = model
                thinking = thoughts_index.get(meta.get('executionId'))
                if thinking:
                    msg['thoughts'] = thinking
                messages.append(msg)

        if not messages:
            continue

        workspaces = [
            ws.get('workspaceFolderAbsoluteUri', '').replace('file://', '')
            for ws in traj.get('workspaces', [])
            if ws.get('workspaceFolderAbsoluteUri')
        ]

        conversations.append({
            'messages': messages,
            'source': 'antigravity-cascade',
            'cascade_id': cascade_id,
            'trajectory_id': traj.get('trajectoryId'),
            'summary': traj.get('summary'),
            'step_count': traj.get('stepCount'),
            'created_time': traj.get('createdTime'),
            'last_modified_time': traj.get('lastModifiedTime'),
            'workspaces': workspaces or None,
            'installation': str(installation) if installation else None,
        })

    return conversations


def main():
    print("=" * 80)
    print("ANTIGRAVITY (CASCADE) COMPLETE DATA EXTRACTION")
    print("=" * 80)
    print()

    # Find data directories
    print("🔍 Searching for Antigravity installations...")
    installations = find_antigravity_installations()

    if installations:
        print(f"✅ Found {len(installations)} installation(s):")
        for inst in installations:
            print(f"   - {inst}")
    else:
        print("⚠️  No Antigravity data directories found")
        print("   (Extraction will still proceed via the HTTP API)")
    print()

    # Locate running language server
    print("🔌 Connecting to Antigravity language server...")
    pid, csrf_token, port = find_language_server()

    if not pid:
        print("❌ Antigravity language server not found.")
        print("   Ensure Antigravity is running, then re-run this script.")
        return

    print(f"✅ Language server found: PID {pid}, port {port}")
    print()

    base_url = f'https://localhost:{port}/exa.language_server_pb.LanguageServerService'
    ssl_ctx = _make_ssl_context()
    installation = installations[0] if installations else None

    # Extract via API (single call — server holds all conversations)
    print("📂 Extracting Cascade conversations...")
    all_conversations = extract_antigravity_cascade(base_url, csrf_token, ssl_ctx, installation)
    print(f"   ✅ Cascade: {len(all_conversations)} conversations")
    print()

    print("=" * 80)
    print("EXTRACTION COMPLETE")
    print("=" * 80)
    print(f"Total conversations: {len(all_conversations):,}")

    if not all_conversations:
        print("No conversations found!")
        return

    total_messages = sum(len(c['messages']) for c in all_conversations)
    with_thoughts = sum(
        1 for c in all_conversations
        if any('thoughts' in m for m in c['messages'])
    )
    complete = sum(
        1 for c in all_conversations
        if any(m['role'] == 'assistant' for m in c['messages'])
    )

    print(f"Complete conversations: {complete:,}")
    print(f"Total messages: {total_messages:,}")
    print(f"With reasoning traces: {with_thoughts:,}")
    print()

    output_dir = Path('extracted_data')
    output_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_file = output_dir / f'antigravity_conversations_{timestamp}.jsonl'

    with open(output_file, 'w') as f:
        for conv in all_conversations:
            f.write(json.dumps(conv, ensure_ascii=False) + '\n')

    file_size = output_file.stat().st_size / 1024 / 1024
    print(f"✅ Saved to: {output_file}")
    print(f"   Size: {file_size:.2f} MB")
    print(f"   Format: JSONL (one conversation per line)")


if __name__ == '__main__':
    main()
