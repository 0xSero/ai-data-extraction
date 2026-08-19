#!/usr/bin/env python3
"""
Extract ALL Freebuff chat data from all projects

Storage locations:
- All platforms: ~/.config/manicode/
  - projects/<project_name>/chats/<chat_id>/chat-messages.json  (the conversation)
  - projects/<project_name>/chats/<chat_id>/chat-meta.json      (messageCount, firstPrompt, mtime)
  - projects/<chat_id>/run-state.json                            (sessionState: cwd, fileTree, trace id)
  - projects/<chat_id>/log.jsonl                                 (event log, opt-in --logs)
  - settings.json                                                (mode, model, ads)
  - freebuff-metadata.json                                       (version, target)
  - message-history.json                                         (raw prompt list, opt-in --history)

chat-messages.json is an array of message objects:
  {
    "id": "user-<ts>" | "ai-<ts>-<hash>" | "divider-<ts>",
    "variant": "user" | "ai",
    "content": str,
    "blocks": [ { "type": "text"|"tool"|"agent"|"ask-user"|"mode-divider", ... } ],
    "timestamp": "01:06 PM",          # display time only
    "metadata": {...},
    "completionTime": ...,
    "isComplete": bool,
    "credits": {...},                 # token/cost data when present
    "fileAttachments": [...],
    "textAttachments": [...],
    "validationErrors": [...]
  }

Block shapes:
  text:      { type, content, textType: "text"|"reasoning", thinkingId, ... }
  tool:      { type, toolCallId, toolName, input, agentId, output }
  agent:     { type, agentId, agentName, agentType, status, initialPrompt, params, blocks: [...] }
  ask-user:  { type, toolCallId, questions: [...], answers: [...] }
  mode-divider: { type, mode }

Output format per conversation (JSONL):
{
  "session_id": str,            # chat dir name (ISO timestamp, e.g. 2026-08-01T07-35-33.146Z)
  "source": "freebuff",
  "source_root": str,           # manicode root dir
  "project": str,               # project folder name
  "title": str,                 # first user prompt (from chat-meta)
  "first_prompt": str,
  "model": str,                 # from settings.json freebuffModel
  "version": str,               # from freebuff-metadata.json
  "mode": str,                  # first mode-divider mode seen (e.g. "LITE")
  "messages": [
    {
      "id": str,
      "role": "user" | "assistant",
      "content": str,
      "reasoning": str,         # text blocks with textType == "reasoning"
      "tool_calls": [ {"id", "name", "input"} ],
      "tool_results": [ {"tool_call_id", "name", "output"} ],
      "subagents": [ {"agent_id", "agent_name", "agent_type", "status", "initial_prompt", "params",
                       "spawn_tool_call_id", "spawn_index", "content", "reasoning",
                       "tool_calls", "tool_results", "ask_user", "modes"} ],
      "ask_user": [ {"tool_call_id", "questions", "answers"} ],
      "timestamp": str,
      "completion_time": ...,
      "is_complete": bool,
      "user_error": ...,
      "credits": {...},
      "file_attachments": [...],
      "text_attachments": [...],
      "validation_errors": [...],
      "metadata": {...}
    }
  ],
  "message_count": int,
  "has_tool_calls": bool,
  "created_at": str,            # chat dir name (ISO)
  "updated_at": int,            # chat-meta messagesMtimeMs (epoch ms)
  "updated_at_iso": str,        # ISO-normalized copy of updated_at
  "cwd": str,
  "project_root": str,
  "file_tree": [...],           # from run-state sessionState.fileContext
  "trace_session_id": str,
  "run_output": {...},          # from run-state output
  "session_state": {...},       # raw run-state sessionState
  "chat_meta": {...}            # raw chat-meta.json
}
"""

import json
import platform
import os
from pathlib import Path
from datetime import datetime


def find_freebuff_installations():
    """Find all Freebuff (manicode) data directories on the device."""
    system = platform.system()
    home = Path.home()

    locations = []
    candidates = [
        home / '.config' / 'manicode',
        home / '.manicode',
        home / '.local' / 'share' / 'manicode',
    ]

    if system == 'Windows':
        appdata = Path(os.environ.get('APPDATA', home / 'AppData' / 'Roaming'))
        localappdata = Path(os.environ.get('LOCALAPPDATA', home / 'AppData' / 'Local'))
        candidates.append(appdata / 'manicode')
        candidates.append(localappdata / 'manicode')

    if system == 'Darwin':
        candidates.append(home / 'Library' / 'Application Support' / 'manicode')

    seen = set()
    for d in candidates:
        if not d.exists():
            continue
        resolved = d.resolve()
        key = str(resolved).lower()
        if key not in seen:
            seen.add(key)
            locations.append(d)

    return locations


def load_json_file(path):
    """Load a JSON file, returning None on failure."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError, TypeError):
        return None


def assemble_blocks(blocks, include_raw_blocks=False):
    """Assemble a Freebuff message's blocks into structured fields.

    Returns dict with keys: content, reasoning, tool_calls, tool_results,
    subagents, ask_user, modes.

    ``include_raw_blocks`` preserves the original block dicts inside
    subagent records (opt-in via --raw-blocks); agent tool outputs can be
    very large, so raw preservation is off by default.
    """
    result = {
        'content': '',
        'reasoning': '',
        'tool_calls': [],
        'tool_results': [],
        'subagents': [],
        'ask_user': [],
        'modes': [],
    }

    if not isinstance(blocks, list):
        return result

    content_parts = []
    reasoning_parts = []

    for b in blocks:
        if not isinstance(b, dict):
            continue
        btype = b.get('type')

        if btype == 'text':
            txt = b.get('content', '')
            # textType is the authoritative discriminator. thinkingId alone is
            # NOT a reliable signal (plain text blocks can carry one), so only
            # fall back to it when textType is absent entirely.
            text_type = b.get('textType')
            if text_type == 'reasoning' or (not text_type and b.get('thinkingId')):
                reasoning_parts.append(txt)
            else:
                content_parts.append(txt)

        elif btype == 'tool':
            has_output = b.get('output') is not None
            tool_call = {
                'id': b.get('toolCallId'),
                'name': b.get('toolName'),
                'input': b.get('input'),
                # Calls whose block carries no output field were interrupted
                # / never completed in the source app (verified against raw
                # storage — the id also never appears in log.jsonl). Flag them
                # explicitly so consumers can distinguish a genuine
                # interruption from an extraction gap (Bug 5).
                'has_result': has_output,
            }
            result['tool_calls'].append(tool_call)
            if has_output:
                result['tool_results'].append({
                    'tool_call_id': b.get('toolCallId'),
                    'name': b.get('toolName'),
                    'output': b.get('output'),
                })

        elif btype == 'agent':
            sub = {
                'agent_id': b.get('agentId'),
                'agent_name': b.get('agentName'),
                'agent_type': b.get('agentType'),
                'status': b.get('status'),
                'initial_prompt': b.get('initialPrompt'),
                'params': b.get('params'),
                'spawn_tool_call_id': b.get('spawnToolCallId'),
                'spawn_index': b.get('spawnIndex'),
            }
            # Preserve the agent block's own top-level content field (some
            # agents put their summary text there instead of in nested blocks).
            agent_own_content = b.get('content')
            nested = assemble_blocks(b.get('blocks'), include_raw_blocks)
            if agent_own_content:
                content_parts.append(agent_own_content)
            sub['content'] = nested['content']
            sub['reasoning'] = nested['reasoning']
            sub['tool_calls'] = nested['tool_calls']
            sub['tool_results'] = nested['tool_results']
            sub['ask_user'] = nested['ask_user']
            sub['modes'] = nested['modes']
            if include_raw_blocks:
                sub['blocks'] = b.get('blocks') or []
            result['subagents'].append(sub)

        elif btype == 'ask-user':
            result['ask_user'].append({
                'tool_call_id': b.get('toolCallId'),
                'questions': b.get('questions'),
                'answers': b.get('answers'),
            })

        elif btype == 'mode-divider':
            mode = b.get('mode')
            if mode:
                result['modes'].append(mode)

    result['content'] = '\n'.join(content_parts)
    result['reasoning'] = '\n'.join(reasoning_parts)
    return result


def extract_conversation_from_chat(chat_dir, project_name, manicode_root,
                                   include_logs=False, include_raw_blocks=False,
                                   root_model=None, root_version=None):
    """Extract a single conversation from a Freebuff chat directory.

    ``root_model`` / ``root_version`` come from settings.json and
    freebuff-metadata.json at the manicode root (passed in once per root).
    """
    chat_id = chat_dir.name

    messages_file = chat_dir / 'chat-messages.json'
    meta_file = chat_dir / 'chat-meta.json'
    run_state_file = chat_dir / 'run-state.json'

    messages_data = load_json_file(messages_file)
    if not isinstance(messages_data, list):
        return None

    chat_meta = load_json_file(meta_file)
    run_state = load_json_file(run_state_file)

    messages = []
    has_tool_calls = False
    first_mode = None

    for msg in messages_data:
        if not isinstance(msg, dict):
            continue

        variant = msg.get('variant')
        blocks = msg.get('blocks', [])

        assembled = assemble_blocks(blocks, include_raw_blocks)

        # Capture mode before the divider skip so it works for both divider
        # messages and (in some schema versions) normal messages.
        if not first_mode and assembled['modes']:
            first_mode = assembled['modes'][0]

        # Divider messages are UI chrome (mode dividers with empty content).
        if variant == 'divider' or str(msg.get('id', '')).startswith('divider-'):
            continue

        role = 'user' if variant == 'user' else 'assistant'

        structured = {
            'id': msg.get('id'),
            'role': role,
            'content': assembled['content'],
            'reasoning': assembled['reasoning'],
            'tool_calls': assembled['tool_calls'],
            'tool_results': assembled['tool_results'],
            'subagents': assembled['subagents'],
            'ask_user': assembled['ask_user'],
            'timestamp': msg.get('timestamp'),
            'completion_time': msg.get('completionTime'),
            'is_complete': msg.get('isComplete'),
            'user_error': msg.get('userError'),
            'credits': msg.get('credits'),
            'file_attachments': msg.get('fileAttachments'),
            'text_attachments': msg.get('textAttachments'),
            'validation_errors': msg.get('validationErrors'),
            'metadata': msg.get('metadata'),
        }

        # Fall back to the message-level content field when no text blocks
        if not structured['content'] and msg.get('content'):
            structured['content'] = msg['content']

        if assembled['tool_calls'] or assembled['tool_results']:
            has_tool_calls = True

        messages.append(structured)

    if not messages:
        return None

    conversation = {
        'session_id': chat_id,
        'source': 'freebuff',
        'source_root': str(manicode_root),
        'project': project_name,
        'messages': messages,
        'message_count': len(messages),
        'has_tool_calls': has_tool_calls,
    }

    # Root-level metadata (same for every chat in this installation).
    if root_model:
        conversation['model'] = root_model
    if root_version:
        conversation['version'] = root_version

    # Title / first prompt from chat-meta.json
    if isinstance(chat_meta, dict):
        conversation['chat_meta'] = chat_meta
        first_prompt = chat_meta.get('firstPrompt')
        if first_prompt:
            conversation['first_prompt'] = first_prompt
            conversation['title'] = first_prompt[:200]
        if chat_meta.get('messagesMtimeMs'):
            conversation['updated_at'] = chat_meta['messagesMtimeMs']
            # ISO-normalized copy for consumers that sort by timestamp.
            conversation['updated_at_iso'] = format_epoch_ms(chat_meta['messagesMtimeMs'])
        conversation['meta_message_count'] = chat_meta.get('messageCount')

    # Session state from run-state.json
    if isinstance(run_state, dict):
        conversation['trace_session_id'] = run_state.get('traceSessionId')
        session_state = run_state.get('sessionState')
        if isinstance(session_state, dict):
            conversation['session_state'] = session_state
            fc = session_state.get('fileContext')
            if isinstance(fc, dict):
                conversation['cwd'] = fc.get('cwd')
                conversation['project_root'] = fc.get('projectRoot')
                if fc.get('fileTree'):
                    conversation['file_tree'] = fc['fileTree']
        if run_state.get('output'):
            conversation['run_output'] = run_state['output']

    # Chat dir name is an ISO timestamp (e.g. 2026-08-01T07-35-33.146Z)
    conversation['created_at'] = chat_id

    if first_mode:
        conversation['mode'] = first_mode

    # Attach the event log (opt-in --logs)
    if include_logs:
        log_file = chat_dir / 'log.jsonl'
        events = []
        if log_file.exists():
            with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        events.append({'raw': line})
        if events:
            conversation['log_events'] = events

    return conversation


def format_epoch_ms(ms):
    """Convert epoch milliseconds to an ISO-8601 string, or None."""
    try:
        return datetime.fromtimestamp(float(ms) / 1000.0).isoformat()
    except (ValueError, OSError, TypeError, OverflowError):
        return None


def extract_from_manicode_root(manicode_root, include_logs=False, include_raw_blocks=False):
    """Extract all conversations from a Freebuff (manicode) data directory."""
    conversations = []
    projects_dir = manicode_root / 'projects'
    if not projects_dir.is_dir():
        return conversations

    # Root-level metadata shared by all chats in this installation.
    settings = load_json_file(manicode_root / 'settings.json')
    root_model = None
    if isinstance(settings, dict):
        root_model = settings.get('freebuffModel')
    fb_meta = load_json_file(manicode_root / 'freebuff-metadata.json')
    root_version = None
    if isinstance(fb_meta, dict):
        root_version = fb_meta.get('version')

    for project_dir in sorted(projects_dir.iterdir()):
        if not project_dir.is_dir():
            continue
        chats_dir = project_dir / 'chats'
        if not chats_dir.is_dir():
            continue
        for chat_dir in sorted(chats_dir.iterdir()):
            if not chat_dir.is_dir():
                continue
            if not (chat_dir / 'chat-messages.json').exists():
                continue
            try:
                conv = extract_conversation_from_chat(
                    chat_dir, project_dir.name, manicode_root,
                    include_logs, include_raw_blocks,
                    root_model, root_version
                )
                if conv:
                    conversations.append(conv)
            except Exception as e:
                print(f"  Error processing chat {chat_dir.name}: {e}")
                continue

    return conversations


def discover_manicode_roots(path_args):
    """Resolve path arguments to manicode data roots."""
    roots = []
    seen = set()

    for arg in path_args:
        cand = Path(arg)
        if not cand.exists():
            print(f"  Warning: path does not exist, skipping: {arg}")
            continue

        # A root directly containing a projects/ dir (chat data)
        if (cand / 'projects').is_dir():
            k = cand.resolve().as_posix().lower()
            if k not in seen:
                seen.add(k)
                roots.append(cand)
            continue

        # A projects/ dir itself (or a chat dir inside one)
        if cand.name == 'projects' and cand.is_dir():
            k = cand.parent.resolve().as_posix().lower()
            if k not in seen:
                seen.add(k)
                roots.append(cand.parent)
            continue

        # Recursive search for projects/ dirs
        try:
            for hit in cand.rglob('projects'):
                if hit.is_dir():
                    parent = hit.parent
                    k = parent.resolve().as_posix().lower()
                    if k not in seen:
                        seen.add(k)
                        roots.append(parent)
        except (PermissionError, OSError) as e:
            print(f"  Error scanning {cand}: {e}")
            continue

    return roots


def main():
    import sys

    print("=" * 80)
    print("FREEBUFF COMPLETE DATA EXTRACTION")
    print("=" * 80)
    print()

    arg_paths = []
    no_auto = False
    include_logs = False
    include_history = False
    include_raw_blocks = False

    i = 1
    while i < len(sys.argv):
        a = sys.argv[i]
        if a in ('-h', '--help'):
            print("Usage: extract_freebuff.py [PATH...] [--no-auto] [--logs] [--history] [--raw-blocks]")
            print("  PATH        manicode data dir (containing projects/), or any dir")
            print("              searched recursively for projects/ folders.")
            print("  --no-auto   skip default-location auto-discovery.")
            print("  --logs      ALSO extract the per-chat log.jsonl event log.")
            print("  --history   ALSO save message-history.json (raw prompt list).")
            print("  --raw-blocks ALSO preserve raw block JSON inside subagent")
            print("               records (large; agent tool outputs are verbose).")
            return
        elif a == '--no-auto':
            no_auto = True
        elif a == '--logs':
            include_logs = True
        elif a == '--history':
            include_history = True
        elif a == '--raw-blocks':
            include_raw_blocks = True
        elif a.startswith('--') or a.startswith('-'):
            print(f"  Warning: unknown flag, ignoring: {a}")
        else:
            arg_paths.append(a)
        i += 1

    installations = []

    if not no_auto:
        auto_roots = find_freebuff_installations()
        installations.extend(auto_roots)

    if arg_paths:
        print(f"Resolving {len(arg_paths)} path argument(s)...")
        extra_roots = discover_manicode_roots(arg_paths)
        print(f"  Discovered {len(extra_roots)} root(s) from path args")
        for d in extra_roots:
            print(f"    - {d}")

        all_keys = set(d.resolve().as_posix().lower() for d in installations)
        for d in extra_roots:
            k = d.resolve().as_posix().lower()
            if k not in all_keys:
                all_keys.add(k)
                installations.append(d)

    if not installations:
        print("No Freebuff installations found!")
        print()
        print("Searched locations:")
        print("  ~/.config/manicode (all platforms)")
        print("  ~/.manicode, ~/.local/share/manicode")
        print("  %APPDATA%/manicode, %LOCALAPPDATA%/manicode (Windows)")
        print("  Pass explicit paths as args, e.g.:")
        print("    python extract_freebuff.py ~/.config/manicode")
        return

    print(f"Found {len(installations)} Freebuff installation(s):")
    for d in installations:
        print(f"  - {d}")
    print()

    all_conversations = []

    for install_dir in installations:
        print(f"Processing: {install_dir}")
        conversations = extract_from_manicode_root(install_dir, include_logs, include_raw_blocks)
        print(f"  Extracted {len(conversations)} conversations")
        all_conversations.extend(conversations)
        print()

    if not all_conversations:
        print("No conversation data found!")
        return

    print("=" * 80)
    print(f"Total conversations extracted: {len(all_conversations)}")

    total_messages = sum(c.get('message_count', len(c.get('messages', []))) for c in all_conversations)
    with_tools = sum(1 for c in all_conversations if c.get('has_tool_calls'))
    with_reasoning = sum(1 for c in all_conversations
                         if any(m.get('reasoning') for m in c.get('messages', [])))
    with_subagents = sum(1 for c in all_conversations
                         if any(m.get('subagents') for m in c.get('messages', [])))

    print(f"Total messages: {total_messages}")
    print(f"Conversations with tool calls: {with_tools}")
    print(f"Conversations with reasoning/thinking: {with_reasoning}")
    print(f"Conversations with subagent blocks: {with_subagents}")
    print()

    # Save: one HF-traces JSONL file per session (one message per line)
    from traces_export import write_session_files
    n_files, n_lines = write_session_files(all_conversations, 'freebuff')
    print(f"✅ Saved {n_files} session file(s) to extracted_data/freebuff/sessions/")
    print(f"   Total message lines: {n_lines:,}")
    print(f"   Format: HF-traces JSONL (one message per line, one file per session)")

    # Optional: message-history.json (raw prompt list)
    if include_history:
        for install_dir in installations:
            hist_file = install_dir / 'message-history.json'
            hist = load_json_file(hist_file)
            if isinstance(hist, list) and hist:
                hist_output = output_dir / f'freebuff_message_history_{timestamp}.jsonl'
                with open(hist_output, 'w', encoding='utf-8') as f:
                    for prompt in hist:
                        f.write(json.dumps({'prompt': prompt}, ensure_ascii=False) + '\n')
                print(f"Saved prompt history: {hist_output} ({len(hist)} prompts)")
                break


if __name__ == '__main__':
    main()
