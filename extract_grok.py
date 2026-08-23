#!/usr/bin/env python3
"""
Extract ALL Grok Build (xAI's terminal coding agent) chat data
Includes: messages, tool calls, tool results, reasoning, timestamps, metadata
Auto-discovers Grok Build session storage on the device

Grok Build persists every session under ~/.grok/sessions/ (or $GROK_HOME/sessions):

    sessions/<urlencoded-cwd>/<session-id>/
      summary.json         # metadata: title, timestamps, model, parent session
      chat_history.jsonl   # raw chat messages sent to the model
      updates.jsonl        # ACP display log (timestamps, tool statuses)
      signals.json         # token usage, tool/turn counters
      subagents/           # per-subagent metadata (meta.json)

Format reference:
https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/17-sessions.md
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote


def find_grok_installations():
    """Find all Grok Build home directories that contain session data"""
    locations = []

    grok_home = os.environ.get('GROK_HOME')
    if grok_home:
        locations.append(Path(grok_home))

    locations.append(Path.home() / '.grok')

    found = []
    for location in locations:
        if (location / 'sessions').is_dir() and location not in found:
            found.append(location)

    return found


def load_json(path):
    """Load a JSON file, returning None on any error"""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def iter_jsonl(path):
    """Yield parsed objects from a JSONL file, skipping bad lines"""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except (OSError, UnicodeDecodeError):
        return


def update_timestamp(line):
    """ISO timestamp of one updates.jsonl line, or None.

    Grok records milliseconds at params._meta.agentTimestampMs; older logs
    carry unix seconds in a top-level "timestamp" field.
    """
    millis = None
    params = line.get('params')
    if isinstance(params, dict):
        meta = params.get('_meta')
        if isinstance(meta, dict) and isinstance(meta.get('agentTimestampMs'), (int, float)):
            millis = meta['agentTimestampMs']

    if millis is None and isinstance(line.get('timestamp'), (int, float)):
        millis = line['timestamp'] * 1000

    if millis is None:
        return None
    try:
        dt = datetime.fromtimestamp(millis / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return dt.isoformat().replace('+00:00', 'Z')


def index_updates(session_dir):
    """Index updates.jsonl for what chat_history.jsonl lacks.

    The chat log carries no timestamps and no tool failure status; the ACP
    display log has both. Returns per-prompt / per-assistant-message timestamp
    queues plus per-tool-call timestamps and failure verdicts.
    """
    index = {
        'prompt_ts': [],       # first timestamp of each user prompt, in order
        'agent_ts': [],        # timestamp of each assistant message chunk, in order
        'call_ts': {},         # tool_call_id -> tool call timestamp
        'result_ts': {},       # tool_call_id -> tool result timestamp
        'failed': set(),       # tool_call_ids whose final status was "failed"
    }

    updates_file = session_dir / 'updates.jsonl'
    if not updates_file.is_file():
        return index

    seen_prompt_indexes = set()
    for line in iter_jsonl(updates_file):
        params = line.get('params')
        update = params.get('update') if isinstance(params, dict) else None
        ts = update_timestamp(line)
        if not isinstance(update, dict) or ts is None:
            continue

        kind = update.get('sessionUpdate')
        call_id = update.get('toolCallId')

        if kind == 'user_message_chunk':
            # Chunks of one prompt share a promptIndex; only the first
            # chunk's timestamp per prompt matters.
            meta = update.get('_meta') if isinstance(update.get('_meta'), dict) else {}
            prompt_index = meta.get('promptIndex')
            if prompt_index is None:
                prompt_index = len(index['prompt_ts'])
            if prompt_index not in seen_prompt_indexes:
                seen_prompt_indexes.add(prompt_index)
                index['prompt_ts'].append(ts)
        elif kind == 'agent_message_chunk':
            index['agent_ts'].append(ts)
        elif kind == 'tool_call':
            if isinstance(call_id, str):
                index['call_ts'].setdefault(call_id, ts)
        elif kind == 'tool_call_update':
            status = update.get('status')
            if isinstance(call_id, str) and status in ('completed', 'failed'):
                index['result_ts'][call_id] = ts
                if status == 'failed':
                    index['failed'].add(call_id)

    return index


def content_to_text(content):
    """Flatten a chat_history content value (string or block array) to text"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get('text')
                if isinstance(text, str):
                    parts.append(text)
        return '\n'.join(part for part in parts if part)
    return ''


def parse_tool_calls(tool_calls):
    """Normalize assistant tool_calls; arguments are JSON-in-a-string"""
    if not isinstance(tool_calls, list):
        return []

    parsed = []
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        entry = dict(call)
        function = entry.get('function')
        if isinstance(function, dict) and isinstance(function.get('arguments'), str):
            function = dict(function)
            try:
                function['arguments'] = json.loads(function['arguments'])
            except json.JSONDecodeError:
                pass
            entry['function'] = function
        parsed.append(entry)
    return parsed


def extract_subagents(session_dir):
    """Collect per-subagent metadata from the subagents/ directory"""
    subagents_dir = session_dir / 'subagents'
    if not subagents_dir.is_dir():
        return []

    subagents = []
    try:
        children = sorted(subagents_dir.iterdir())
    except OSError:
        return []

    for child in children:
        if not child.is_dir():
            continue
        entry = {'id': child.name}
        meta = load_json(child / 'meta.json')
        if meta is not None:
            entry['meta'] = meta
        subagents.append(entry)
    return subagents


def extract_session(session_dir, project_path, installation):
    """Extract one session directory into a normalized conversation dict"""
    chat_file = session_dir / 'chat_history.jsonl'
    if not chat_file.is_file():
        return None

    summary = load_json(session_dir / 'summary.json') or {}
    info = summary.get('info') if isinstance(summary.get('info'), dict) else {}
    index = index_updates(session_dir)

    prompt_ts = list(index['prompt_ts'])
    agent_ts = list(index['agent_ts'])

    messages = []
    pending_reasoning = []

    for record in iter_jsonl(chat_file):
        record_type = record.get('type')

        if record_type == 'user':
            text = content_to_text(record.get('content'))
            # Grok injects a <user_info> preamble as a synthetic user
            # message; it is environment boilerplate, not a prompt.
            if not text or '<user_info>' in text:
                continue
            msg = {'role': 'user', 'content': text}
            if prompt_ts:
                msg['timestamp'] = prompt_ts.pop(0)
            messages.append(msg)

        elif record_type == 'reasoning':
            text = content_to_text(record.get('summary'))
            if text:
                pending_reasoning.append(text)

        elif record_type == 'assistant':
            text = content_to_text(record.get('content'))
            tool_calls = parse_tool_calls(record.get('tool_calls'))
            if not text and not tool_calls:
                continue

            msg = {'role': 'assistant', 'content': text}
            if record.get('model_id'):
                msg['model'] = record['model_id']
            if pending_reasoning:
                msg['reasoning'] = '\n\n'.join(pending_reasoning)
                pending_reasoning = []
            if tool_calls:
                msg['tool_calls'] = tool_calls

            # A text message's time comes from its display chunk; a
            # tool-only message's time is its first tool call.
            if text and agent_ts:
                msg['timestamp'] = agent_ts.pop(0)
            elif tool_calls:
                for call in tool_calls:
                    call_id = call.get('id')
                    if call_id in index['call_ts']:
                        msg['timestamp'] = index['call_ts'][call_id]
                        break
            messages.append(msg)

        elif record_type == 'tool_result':
            call_id = record.get('tool_call_id')
            result = {
                'tool_call_id': call_id,
                'content': record.get('content'),
            }
            if call_id in index['failed']:
                result['is_error'] = True
            if call_id in index['result_ts']:
                result['timestamp'] = index['result_ts'][call_id]

            # Attach to the assistant message that issued the call
            target = None
            for msg in reversed(messages):
                if msg['role'] == 'assistant' and any(
                    call.get('id') == call_id for call in msg.get('tool_calls', [])
                ):
                    target = msg
                    break
            if target is None:
                target = next(
                    (m for m in reversed(messages) if m['role'] == 'assistant'), None
                )
            if target is not None:
                target.setdefault('tool_results', []).append(result)

    if not messages:
        return None

    title = summary.get('generated_title') or summary.get('session_summary')
    model = summary.get('current_model_id') or next(
        (m['model'] for m in messages if m.get('model')), None
    )
    conversation = {
        'messages': messages,
        'source': 'grok-build',
        'session_id': info.get('id') or session_dir.name,
        'name': title,
        'project_path': info.get('cwd') or project_path,
        'model': model,
        'created_at': summary.get('created_at'),
        'updated_at': summary.get('updated_at'),
        'source_file': str(chat_file),
        'installation': str(installation),
    }

    if summary.get('head_branch'):
        conversation['git_branch'] = summary['head_branch']
    if summary.get('parent_session_id'):
        conversation['parent_session_id'] = summary['parent_session_id']
    if summary.get('agent_name'):
        conversation['agent_name'] = summary['agent_name']

    signals = load_json(session_dir / 'signals.json')
    if signals is not None:
        conversation['usage'] = signals

    subagents = extract_subagents(session_dir)
    if subagents:
        conversation['subagents'] = subagents

    return conversation


def decode_project_path(cwd_dir):
    """Original working directory for one <encoded-cwd> group directory.

    The group name is the URL-encoded cwd; when the encoded name would
    exceed 255 bytes, Grok uses a slug plus hash and records the original
    path in a .cwd file inside the group.
    """
    cwd_file = cwd_dir / '.cwd'
    if cwd_file.is_file():
        try:
            recorded = cwd_file.read_text(encoding='utf-8').strip()
            if recorded:
                return recorded
        except (OSError, UnicodeDecodeError):
            pass
    return unquote(cwd_dir.name)


def extract_grok_conversations(installation):
    """Extract all sessions from one Grok home directory"""
    conversations = []
    sessions_root = installation / 'sessions'

    try:
        cwd_dirs = sorted(sessions_root.iterdir())
    except OSError:
        return conversations

    for cwd_dir in cwd_dirs:
        # Skip stray root files (e.g. the session_search.sqlite index)
        if not cwd_dir.is_dir():
            continue

        project_path = decode_project_path(cwd_dir)

        try:
            session_dirs = sorted(cwd_dir.iterdir())
        except OSError:
            continue

        for session_dir in session_dirs:
            # Skip project-level files such as prompt_history.jsonl
            if not session_dir.is_dir():
                continue

            try:
                conversation = extract_session(session_dir, project_path, installation)
            except Exception as e:
                print(f"Error processing {session_dir}: {e}")
                continue

            if conversation:
                conversations.append(conversation)

    return conversations


def main():
    print("=" * 80)
    print("GROK BUILD COMPLETE DATA EXTRACTION")
    print("=" * 80)
    print()

    print("🔍 Searching for Grok Build session storage...")
    installations = find_grok_installations()

    if not installations:
        print("❌ No Grok Build session storage found!")
        print("   Checked $GROK_HOME and ~/.grok")
        return

    print(f"✅ Found {len(installations)} installation(s):")
    for installation in installations:
        print(f"   - {installation}")
    print()

    all_conversations = []
    installation_stats = {}

    for installation in installations:
        print(f"📂 Processing: {installation}")

        conversations = extract_grok_conversations(installation)

        if conversations:
            all_conversations.extend(conversations)
            installation_stats[str(installation)] = len(conversations)
            print(f"   ✅ {len(conversations)} conversations")
        else:
            print(f"   ⚠️  No conversations found")

    print()
    print("=" * 80)
    print("EXTRACTION COMPLETE")
    print("=" * 80)
    print(f"Total conversations: {len(all_conversations):,}")

    if not all_conversations:
        print("No conversations found!")
        return

    total_messages = sum(len(c['messages']) for c in all_conversations)
    with_tools = sum(1 for c in all_conversations
                     if any('tool_calls' in m or 'tool_results' in m
                            for m in c['messages']))
    with_reasoning = sum(1 for c in all_conversations
                         if any('reasoning' in m for m in c['messages']))
    complete = sum(1 for c in all_conversations
                   if any(m['role'] == 'assistant' for m in c['messages']))
    with_subagents = sum(1 for c in all_conversations if c.get('subagents'))

    print(f"Complete conversations: {complete:,}")
    print(f"Total messages: {total_messages:,}")
    print(f"With tool calls/results: {with_tools:,}")
    print(f"With reasoning: {with_reasoning:,}")
    print(f"With subagents: {with_subagents:,}")
    print()

    print("Breakdown by installation:")
    for installation, count in sorted(installation_stats.items(), key=lambda x: -x[1]):
        print(f"  {Path(installation).name:20} {count:5,} conversations")
    print()

    output_dir = Path('extracted_data')
    output_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_file = output_dir / f'grok_conversations_{timestamp}.jsonl'

    with open(output_file, 'w') as f:
        for conversation in all_conversations:
            f.write(json.dumps(conversation, ensure_ascii=False) + '\n')

    file_size = output_file.stat().st_size / 1024 / 1024
    print(f"✅ Saved to: {output_file}")
    print(f"   Size: {file_size:.2f} MB")
    print(f"   Format: JSONL (one conversation per line)")


if __name__ == '__main__':
    main()
