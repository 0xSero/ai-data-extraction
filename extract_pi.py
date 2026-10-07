#!/usr/bin/env python3
"""
Extract ALL pi coding-agent session data
Includes: messages, thinking/reasoning, tool calls + results, model/provider per turn,
token usage, timestamps, system prompt and subagent sessions
Auto-discovers pi installations on the device

pi (and its fork omp, see extract_omp.py) stores each session as JSONL under
~/.pi/agent/sessions/<encoded-cwd>/<timestamp>_<uuid>.jsonl. Every line is an
entry with id/parentId (a tree, because sessions can branch). The script follows
the parentId chain back from the last entry, so it exports the active branch.
"""

import json
from pathlib import Path
from datetime import datetime, timezone
import os

SKIP_ROLES = {'bashExecution', 'branchSummary', 'compactionSummary', 'custom'}


def find_installations(dir_names, env_var=None):
    """Find agent session roots, e.g. ~/.pi/agent/sessions"""
    home = Path.home()
    roots = []
    for key in [env_var] + (['OMP_HOME', 'OMP_DATA_DIR'] if env_var == 'OMP_CODING_AGENT_DIR' else []):
        if key and os.environ.get(key):
            base = Path(os.environ[key])
            roots.extend([base / 'sessions', base / 'agent' / 'sessions'])
    for name in dir_names:
        roots.append(home / name / 'agent' / 'sessions')
    return [r for r in dict.fromkeys(roots) if r.exists()]


def _parts(content):
    if isinstance(content, str):
        return [{'type': 'text', 'text': content}]
    return [p for p in content if isinstance(p, dict)] if isinstance(content, list) else []


def _text(content):
    if isinstance(content, str):
        return content
    parts = []
    for item in _parts(content):
        if isinstance(item, dict) and item.get('type') == 'text' and isinstance(item.get('text'), str):
            parts.append(item.get('text', ''))
    return '\n'.join(parts)


def _images(content):
    return sum(1 for item in _parts(content) if isinstance(item, dict) and item.get('type') == 'image')


def _iso(ts):
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat().replace('+00:00', 'Z')
    return ts


def active_branch(entries, leaf_id=None):
    """Return the entries on the path root -> last entry (the active branch)."""
    by_id = {e['id']: e for e in entries if isinstance(e.get('id'), str)}
    if not by_id:
        return entries
    leaf = by_id.get(leaf_id) if leaf_id else next(e for e in reversed(entries) if isinstance(e.get('id'), str))
    chain, seen = [], set()
    cur = leaf
    while cur is not None and cur['id'] not in seen:
        seen.add(cur['id'])
        chain.append(cur)
        parent = cur.get('parentId')
        cur = by_id.get(parent) if isinstance(parent, str) else None
    chain.reverse()
    return chain


def convert_message(m, entry_ts):
    role = m.get('role')
    content = m.get('content')
    ts = _iso(m.get('timestamp')) or entry_ts

    if role in ('user', 'developer', 'system'):
        msg = {'role': 'user' if role == 'user' else 'system',
               'content': _text(content), 'timestamp': ts}
        if _images(content):
            msg['images'] = _images(content)
        if m.get('synthetic'):
            msg['synthetic'] = True
        if m.get('attribution') and m.get('attribution') != 'user':
            msg['attribution'] = m['attribution']
        return msg

    if role == 'assistant':
        text, thinking, tool_calls = [], [], []
        for item in _parts(content):
            if not isinstance(item, dict):
                continue
            t = item.get('type')
            if t == 'text' and isinstance(item.get('text'), str):
                text.append(item.get('text', ''))
            elif t == 'thinking':
                if isinstance(item.get('thinking'), str):
                    thinking.append(item['thinking'])
            elif t == 'redactedThinking':
                thinking.append('[redacted]')
            elif t == 'toolCall':
                args = item.get('arguments', {})
                tool_calls.append({
                    'id': item.get('id'),
                    'type': 'function',
                    'function': {
                        'name': item.get('name'),
                        'arguments': args if isinstance(args, str) else json.dumps(args, ensure_ascii=False),
                    },
                })
        msg = {'role': 'assistant', 'content': '\n'.join(text), 'timestamp': ts,
               'model': m.get('model'), 'provider': m.get('provider'), 'api': m.get('api')}
        if m.get('upstreamModel'):
            msg['upstream_model'] = m['upstreamModel']
        if m.get('responseModel'):
            msg['upstream_model'] = m['responseModel']
        if thinking:
            msg['reasoning'] = '\n'.join(thinking)
        if tool_calls:
            msg['tool_calls'] = tool_calls
        if m.get('usage'):
            u = m['usage']
            msg['usage'] = {k: u.get(k) for k in ('input', 'output', 'cacheRead', 'cacheWrite', 'totalTokens')}
        if m.get('stopReason'):
            msg['stop_reason'] = m['stopReason']
        if m.get('errorMessage'):
            msg['error'] = m['errorMessage']
        return msg

    if role == 'toolResult':
        msg = {'role': 'tool', 'tool_call_id': m.get('toolCallId'), 'name': m.get('toolName'),
               'content': _text(content), 'is_error': bool(m.get('isError')), 'timestamp': ts}
        if _images(content):
            msg['images'] = _images(content)
        return msg

    return None


def _read_session(session_file):
    """Read only object records; malformed records do not discard the session."""
    entries = []
    try:
        with open(session_file, 'r', errors='replace') as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    entries.append(obj)
    except OSError:
        return []

    return entries


def _convert_session(entries, session_file, source, leaf_id=None):
    header = next((e for e in entries if e.get('type') == 'session'), {})
    branch = active_branch([e for e in entries if e.get('type') not in ('session', 'title')], leaf_id)

    messages, models, system_prompt, tools, title, init = [], [], None, None, None, {}
    current_model = current_provider = None
    for e in branch:
        t = e.get('type')
        if t == 'message' and isinstance(e.get('message'), dict):
            if e['message'].get('role') in SKIP_ROLES:
                continue
            msg = convert_message(e['message'], e.get('timestamp'))
            if msg:
                if msg['role'] == 'assistant':
                    msg['model'] = msg.get('model') or current_model
                    msg['provider'] = msg.get('provider') or current_provider
                messages.append(msg)
        elif t == 'model_change':
            mid = e.get('model') or '/'.join(x for x in (e.get('provider'), e.get('modelId')) if x)
            models.append(mid)
            current_model = e.get('model') or e.get('modelId')
            current_provider = e.get('provider')
        elif t == 'session_init':
            init = e
            system_prompt = e.get('systemPrompt')
            tools = e.get('tools')
        elif t == 'custom_message':
            messages.append({'role': e.get('role') or 'system', 'content': _text(e.get('content')),
                'timestamp': e.get('timestamp'), 'custom': True, 'custom_type': e.get('customType'),
                'display': e.get('display'), 'details': e.get('details'), 'attribution': e.get('attribution')})
        elif t in ('compaction', 'branch_summary'):
            messages.append({'role': 'user', 'content': e.get('summary', ''), 'timestamp': e.get('timestamp'),
                             'event': t, 'compaction_summary': t == 'compaction', 'synthetic': True})
        elif t in ('title_change', 'session_info'):
            title = e.get('title') or e.get('name') or title

    for e in entries:
        if e.get('type') == 'title' and e.get('title'):
            title = e['title']

    if not any(m['role'] == 'assistant' for m in messages):
        return None

    conv = {
        'messages': messages,
        'source': source,
        'session_id': header.get('id') or Path(session_file).stem,
        'branch_id': branch[-1].get('id') if branch else None,
        'project_path': header.get('cwd'),
        'name': title,
        'created_at': header.get('timestamp') or (messages[0].get('timestamp')),
        'models': sorted({m['model'] for m in messages if m.get('model')}),
        'model_changes': models,
        'source_file': str(session_file),
    }
    if header.get('parentSession'):
        conv['parent_session'] = header['parentSession']
    # Subagent / advisor sessions live in a directory named after the parent session
    if Path(session_file).parent.name[:4].isdigit() and '_' in Path(session_file).parent.name:
        conv['subagent'] = True
        conv['agent'] = init.get('agent') or Path(session_file).stem
    if system_prompt:
        conv['system_prompt'] = system_prompt
    if tools:
        conv['tools'] = tools
    return conv


def extract_session(session_file, source):
    """Export the active branch, preserving the existing public API."""
    return _convert_session(_read_session(session_file), session_file, source)


def extract_sessions(session_file, source, all_branches=False):
    entries = _read_session(session_file)
    if not all_branches:
        conv = _convert_session(entries, session_file, source)
        return [conv] if conv else []
    nodes = [e for e in entries if e.get('type') not in ('session', 'title') and isinstance(e.get('id'), str)]
    parents = {e.get('parentId') for e in nodes}
    leaves = [e['id'] for e in nodes if e['id'] not in parents] or [None]
    return [c for leaf in leaves if (c := _convert_session(entries, session_file, source, leaf))]


def find_all_sessions(root):
    return sorted(root.rglob('*.jsonl'))


def run(tool_label, source, dir_names, env_var, output_prefix, all_branches=False):
    print("=" * 80)
    print(f"{tool_label.upper()} DATA EXTRACTION")
    print("=" * 80)
    print()

    print(f"🔍 Searching for {tool_label} installations...")
    installations = find_installations(dir_names, env_var)
    if not installations:
        print(f"❌ No {tool_label} installations found!")
        return []

    print(f"✅ Found {len(installations)} installation(s):")
    for inst in installations:
        print(f"   - {inst}")
    print()

    all_conversations = []
    for installation in installations:
        print(f"📂 Processing: {installation}")
        session_files = find_all_sessions(installation)
        print(f"   Found {len(session_files)} session files")
        count = 0
        for session_file in session_files:
            for conv in extract_sessions(session_file, source, all_branches):
                conv['installation'] = str(installation)
                all_conversations.append(conv)
                count += 1
        print(f"   ✅ {count} conversations")

    print()
    print("=" * 80)
    print("EXTRACTION COMPLETE")
    print("=" * 80)
    print(f"Total conversations: {len(all_conversations):,}")
    if not all_conversations:
        print("No conversations found!")
        return []

    total_messages = sum(len(c['messages']) for c in all_conversations)
    tool_calls = sum(len(m.get('tool_calls', [])) for c in all_conversations for m in c['messages'])
    with_reasoning = sum(1 for c in all_conversations if any('reasoning' in m for m in c['messages']))
    subagents = sum(1 for c in all_conversations if c.get('subagent'))
    print(f"Total messages: {total_messages:,}")
    print(f"Tool calls: {tool_calls:,}")
    print(f"With reasoning: {with_reasoning:,}")
    print(f"Subagent sessions: {subagents:,}")
    print()

    output_dir = Path('extracted_data')
    output_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_file = output_dir / f'{output_prefix}_conversations_{timestamp}.jsonl'
    with open(output_file, 'w') as f:
        for conv in all_conversations:
            f.write(json.dumps(conv, ensure_ascii=False) + '\n')

    file_size = output_file.stat().st_size / 1024 / 1024
    print(f"✅ Saved to: {output_file}")
    print(f"   Size: {file_size:.2f} MB")
    print("   Format: JSONL (one conversation per line)")
    return all_conversations


def main():
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--all-branches', action='store_true', help='export each leaf separately; default: active branch')
    args = p.parse_args()
    run('pi', 'pi', ['.pi'], 'PI_CODING_AGENT_DIR', 'pi', args.all_branches)


if __name__ == '__main__':
    main()
