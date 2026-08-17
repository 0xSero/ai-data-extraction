#!/usr/bin/env python3
"""
Extract Qwen Code CLI chat data.

Qwen Code is a fork of the Gemini CLI, so its on-disk layout is very close
to the Gemini CLI's, with two formats under the config dir (~/.qwen):

  1. Legacy JSON — tmp/<project-hash>/chats/session-*.json  (whole-file dict
     with a top-level "messages" array; assistant messages use type "qwen").

  2. New JSONL   — projects/<project-name>/chats/<uuid>.jsonl.  A linear
     append-only tree of message nodes (uuid/parentUuid chain, no branching)
     with types user / assistant / tool_result / system. Each node carries a
     "message" dict of parts ({text, thought:true}, {functionCall}, ...),
     model/usage metadata, and tool_call results. Optional sibling
     <uuid>.runtime.json holds session runtime metadata (qwen_version,
     work_dir, hostname, started_at).

The two formats never overlap (observed: 54 legacy + 159 new = 213 unique
sessions). Conversations are deduplicated by session_id, system/telemetry
nodes are skipped (ui_telemetry alone is ~98% of the system stream), and
info-only stubs are reported rather than silently dropped.

Stdlib only — no third-party dependencies.
"""

import json
import os
import platform
from collections import Counter
from pathlib import Path


def find_qwen_installations():
    """Find all Qwen Code installation/config directories."""
    system = platform.system()
    home = Path.home()

    locations = []
    patterns = ['qwen', '.qwen']

    if system == "Darwin":
        base_dirs = [home, home / ".config"]
    elif system == "Linux":
        base_dirs = [home / ".qwen", home / ".config/qwen",
                     home / ".local/share/qwen", home]
    elif system == "Windows":
        base_dirs = [
            Path(os.environ.get('USERPROFILE', home)) / ".qwen",
            Path(os.environ.get('LOCALAPPDATA', home / 'AppData/Local')) / "qwen",
            home,
        ]
    else:
        base_dirs = [home / ".qwen", home / ".config", home]

    for base_dir in base_dirs:
        if not base_dir.exists():
            continue
        for pattern in patterns:
            qwen_dir = base_dir / pattern
            if qwen_dir.exists():
                locations.append(qwen_dir)

    return list(set(locations))


# ---------------------------------------------------------------------------
# Legacy JSON sessions (Gemini-CLI-compatible, assistant type "qwen")
# ---------------------------------------------------------------------------

def _normalize_parts(content):
    """Normalize message content (str or list of parts) to a plain string.

    Parts may be {"text": ...}, {"functionCall": ...}, {"functionResponse": ...}.
    """
    if content is None:
        return ''
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for p in content:
            if isinstance(p, dict):
                if p.get('text'):
                    out.append(p['text'])
                elif 'functionCall' in p:
                    fc = p['functionCall'] or {}
                    out.append(f"[function_call: {fc.get('name', '?')} "
                               f"{json.dumps(fc.get('args') or {}, ensure_ascii=False)}]")
                elif 'functionResponse' in p:
                    out.append(json.dumps(p, ensure_ascii=False))
                else:
                    out.append(json.dumps(p, ensure_ascii=False))
            else:
                out.append(str(p))
        return '\n'.join(out)
    return str(content)


def _split_thought_parts(parts):
    """Split part-list into (plain_text, thoughts) joining text parts.

    Gemini/Qwen CLI mark reasoning parts with {"text": ..., "thought": true}.
    functionCall parts are intentionally NOT rendered into the plain text:
    the assistant branch captures them structurally into ``tool_calls``, so
    rendering them here too would double-represent each call.
    """
    plain, thoughts = [], []
    for p in parts:
        if isinstance(p, dict) and p.get('thought'):
            thoughts.append(p.get('text', ''))
        elif isinstance(p, dict) and p.get('text'):
            plain.append(p['text'])
        elif isinstance(p, dict) and 'functionCall' in p:
            continue  # captured structurally as tool_calls
        elif isinstance(p, dict):
            plain.append(json.dumps(p, ensure_ascii=False))
        else:
            plain.append(str(p))
    return '\n'.join(plain), '\n'.join(thoughts)


def extract_legacy_session(session_file):
    """Extract conversation from a legacy Qwen session JSON file."""
    try:
        with open(session_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    if not isinstance(data, dict) or not data.get('messages'):
        return None

    messages = []
    for msg in data['messages']:
        if not isinstance(msg, dict):
            continue
        msg_type = msg.get('type')
        content = _normalize_parts(msg.get('content', ''))
        timestamp = msg.get('timestamp')

        if msg_type == 'user':
            messages.append({'role': 'user', 'content': content,
                             'timestamp': timestamp})
        elif msg_type in ('qwen', 'gemini', 'model'):
            normalized = {'role': 'assistant', 'content': content,
                          'timestamp': timestamp}
            if msg.get('model'):
                normalized['model'] = msg['model']
            if msg.get('thoughts'):
                normalized['thoughts'] = msg['thoughts']
            if msg.get('tokens'):
                normalized['tokens'] = msg['tokens']
            if msg.get('toolCalls'):
                normalized['tool_calls'] = msg['toolCalls']
            messages.append(normalized)
        # info/error/unknown legacy types are skipped (session-level noise).

    if not messages:
        return None

    return {
        'source': 'qwen-code',
        'session_format': 'json',
        'messages': messages,
        'session_id': data.get('sessionId'),
        'project_hash': data.get('projectHash'),
        'start_time': data.get('startTime'),
        'last_updated': data.get('lastUpdated'),
        'source_file': str(session_file),
    }


# ---------------------------------------------------------------------------
# New JSONL sessions (projects/<name>/chats/<uuid>.jsonl, linear node tree)
# ---------------------------------------------------------------------------

def _extract_message_node(node):
    """Convert one JSONL node dict -> normalized message or None (skip).

    Node types:
      user        -> role user; content from parts
      assistant   -> role assistant; text + thoughts split, functionCall
                     parts extracted as tool_calls, model/usage preserved
      tool_result -> role tool; tool_name + content from functionResponse
      system      -> skipped (telemetry / internal command plumbing)
    """
    node_type = node.get('type')
    message = node.get('message') or {}
    parts = message.get('parts') if isinstance(message, dict) else None
    timestamp = node.get('timestamp')

    if node_type == 'user':
        if not isinstance(parts, list) or not parts:
            return None
        content, _ = _split_thought_parts(parts)
        if not content:
            return None
        return {'role': 'user', 'content': content, 'timestamp': timestamp}

    if node_type == 'assistant':
        if not isinstance(parts, list):
            return None
        content, thoughts = _split_thought_parts(parts)
        tool_calls = []
        for p in parts:
            if isinstance(p, dict) and 'functionCall' in p:
                fc = p['functionCall'] or {}
                tool_calls.append({'id': fc.get('id'),
                                   'name': fc.get('name'),
                                   'args': fc.get('args')})
        if not content and not thoughts and not tool_calls:
            return None
        normalized = {'role': 'assistant', 'content': content,
                      'timestamp': timestamp}
        if thoughts:
            normalized['thoughts'] = thoughts
        if tool_calls:
            normalized['tool_calls'] = tool_calls
        if node.get('model'):
            normalized['model'] = node['model']
        usage = node.get('usageMetadata')
        if isinstance(usage, dict):
            normalized['usage'] = {k: v for k, v in usage.items()
                                   if isinstance(v, (int, float))}
        if node.get('contextWindowSize'):
            normalized['context_window_size'] = node['contextWindowSize']
        return normalized

    if node_type == 'tool_result':
        if not isinstance(parts, list):
            return None
        tool_name = None
        content = ''
        call_id = None
        for p in parts:
            if isinstance(p, dict) and 'functionResponse' in p:
                fr = p['functionResponse'] or {}
                tool_name = fr.get('name') or tool_name
                call_id = fr.get('id') or call_id
                resp = fr.get('response') or {}
                output = resp.get('output') if isinstance(resp, dict) else None
                if output is not None:
                    content = output
        tcr = node.get('toolCallResult') or {}
        if not content:
            content = tcr.get('resultDisplay') or ''
        # Both functionResponse.response.output and toolCallResult.resultDisplay
        # may be structured (dict/list — e.g. ansiOutput, task_execution,
        # plan_summary). Normalize to a string like every other branch.
        if content and not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        if not content and not tool_name:
            return None
        normalized = {'role': 'tool', 'content': content,
                      'timestamp': timestamp}
        if tool_name:
            normalized['tool_name'] = tool_name
        if call_id:
            normalized['call_id'] = call_id
        if tcr.get('status'):
            normalized['status'] = tcr['status']
        return normalized

    # system nodes (ui_telemetry / at_command / slash_command / ...) skipped
    return None


def extract_project_session(jsonl_file):
    """Extract a conversation from a new-format projects/<name>/chats/<id>.jsonl.

    The file is a linear append-only chain of nodes; ordering by timestamp
    reproduces the conversation (0 observed branching).
    """
    nodes = []
    try:
        with open(jsonl_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    node = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(node, dict) and node.get('uuid'):
                    nodes.append(node)
    except OSError:
        return None

    if not nodes:
        return None

    nodes.sort(key=lambda n: (n.get('timestamp') or '', n.get('uuid') or ''))

    messages = []
    for node in nodes:
        msg = _extract_message_node(node)
        if msg:
            messages.append(msg)

    if not messages:
        return None

    # Annotate interrupted tool calls: an assistant tool_call whose id never
    # appears on a tool_result node has no recorded result (user aborted /
    # session ended mid-call). Stamp has_result so consumers can distinguish a
    # genuine interruption from an extraction gap (freebuff Bug 5 pattern).
    # Preserved through traces_export._tool_call_entry as an extra key.
    result_ids = {m.get('call_id') for m in messages
                  if m.get('role') == 'tool' and m.get('call_id')}
    for m in messages:
        if m.get('role') == 'assistant':
            for tc in m.get('tool_calls', []):
                if isinstance(tc, dict) and tc.get('id'):
                    tc['has_result'] = tc['id'] in result_ids

    session_id = nodes[0].get('sessionId')
    git_branch = next((n.get('gitBranch') for n in nodes
                       if n.get('gitBranch')), None)
    cwd = nodes[0].get('cwd')
    start_time = nodes[0].get('timestamp')
    last_updated = nodes[-1].get('timestamp')

    conv = {
        'source': 'qwen-code',
        'session_format': 'jsonl',
        'messages': messages,
        'session_id': session_id,
        'cwd': cwd,
        'git_branch': git_branch,
        'start_time': start_time,
        'last_updated': last_updated,
        'source_file': str(jsonl_file),
    }

    # Optional runtime metadata: <uuid>.runtime.json
    runtime_file = jsonl_file.with_suffix('.runtime.json')
    if runtime_file.exists():
        try:
            rt = json.loads(runtime_file.read_text(encoding='utf-8'))
            for key in ('qwen_version', 'work_dir', 'hostname', 'started_at'):
                if rt.get(key):
                    conv[key] = rt[key]
        except (json.JSONDecodeError, OSError):
            pass

    return conv


def find_legacy_sessions(installation):
    """Find legacy tmp/<hash>/chats/session-*.json files."""
    tmp_dir = installation / 'tmp'
    if tmp_dir.exists():
        return sorted(tmp_dir.rglob('chats/session-*.json'))
    return []


def find_project_sessions(installation):
    """Find new-format projects/<name>/chats/*.jsonl files.

    Returns list of (jsonl_file, project_name). Note: <uuid>.runtime.json
    siblings end in .json, so the *.jsonl glob never matches them (they are
    attached separately via with_suffix in extract_project_session).
    """
    projects_dir = installation / 'projects'
    if projects_dir.exists():
        return [(jf, jf.parent.parent.name)
                for jf in sorted(projects_dir.rglob('*.jsonl'))]
    return []


def main():
    print("=" * 80)
    print("QWEN CODE DATA EXTRACTION")
    print("=" * 80)
    print()

    installations = find_qwen_installations()
    if not installations:
        print("❌ No Qwen Code installations found!")
        return

    print(f"✅ Found {len(installations)} installation(s):")
    for inst in installations:
        print(f"   - {inst}")
    print()

    all_conversations = []
    per_install = {}

    for installation in installations:
        print(f"📂 Processing: {installation}")

        conversations = []
        stubs = 0
        formats = Counter()

        legacy = find_legacy_sessions(installation)
        projects = find_project_sessions(installation)
        print(f"   Legacy sessions (tmp/*/chats/session-*.json): {len(legacy)}")
        print(f"   Project sessions (projects/*/chats/*.jsonl): {len(projects)}")

        for session_file in legacy:
            conv = extract_legacy_session(session_file)
            if conv:
                conv['installation'] = str(installation)
                # Legacy sessions live under tmp/<project-hash>/chats/, so the
                # dir name is the project hash (not a readable project name).
                conv['project_hash'] = session_file.parent.parent.name
                conversations.append(conv)
                formats[conv['session_format']] += 1
            else:
                stubs += 1

        for jsonl_file, project in projects:
            conv = extract_project_session(jsonl_file)
            if conv:
                conv['installation'] = str(installation)
                conv['project'] = project
                conversations.append(conv)
                formats[conv['session_format']] += 1
            else:
                stubs += 1

        all_conversations.extend(conversations)
        per_install[str(installation)] = len(conversations)
        if conversations:
            print(f"   ✅ {len(conversations)} conversations "
                  f"({stubs} stubs skipped) — {dict(formats)}")
        else:
            print(f"   ⚠️  No conversations found ({stubs} stubs skipped)")

    print()
    print("=" * 80)
    print("EXTRACTION COMPLETE")
    print("=" * 80)

    if not all_conversations:
        print("No conversations found!")
        return

    # Deduplicate by session_id across both formats (observed: no overlap,
    # but keep the guard for safety).
    merged = 0
    best = {}
    orphaned = []
    for conv in all_conversations:
        sid = conv.get('session_id')
        if not sid:
            orphaned.append(conv)
            continue
        prev = best.get(sid)
        if prev is None:
            best[sid] = conv
            continue
        merged += 1
        if len(conv['messages']) > len(prev['messages']):
            best[sid] = conv
    all_conversations = orphaned + list(best.values())

    # Recompute per-installation counts post-dedup.
    per_install = {}
    for conv in all_conversations:
        inst = conv.get('installation')
        if inst:
            per_install[inst] = per_install.get(inst, 0) + 1

    print(f"Total conversations: {len(all_conversations):,}")
    total_messages = sum(len(c['messages']) for c in all_conversations)
    with_thoughts = sum(1 for c in all_conversations
                        if any('thoughts' in m for m in c['messages']))
    complete = sum(1 for c in all_conversations
                   if any(m['role'] == 'assistant' for m in c['messages']))
    with_tools = sum(1 for c in all_conversations
                     if any('tool_calls' in m for m in c['messages']))

    print(f"Complete conversations: {complete:,}")
    print(f"Total messages: {total_messages:,}")
    print(f"With thoughts: {with_thoughts:,}")
    print(f"With tool calls: {with_tools:,}")
    if merged:
        print(f"Duplicate sessions merged: {merged:,}")
    print()

    print("Breakdown by installation:")
    for inst, count in sorted(per_install.items(), key=lambda x: -x[1]):
        print(f"  {Path(inst).name:20} {count:5,} conversations")
    print()

    # Save: one HF-traces JSONL file per session (one message per line)
    from traces_export import write_session_files
    n_files, n_lines = write_session_files(all_conversations, 'qwen')
    print(f"✅ Saved {n_files} session file(s) to extracted_data/qwen/sessions/")
    print(f"   Total message lines: {n_lines:,}")
    print(f"   Format: HF-traces JSONL (one message per line, one file per session)")


if __name__ == '__main__':
    main()
