#!/usr/bin/env python3
"""
Extract ALL Cline chat data from all projects
Supports: CLI (JSON files) and any Cline storage location

Storage locations:
- All platforms: ~/.cline/data/
  - sessions/<session_id>/<session_id>.json (metadata)
  - sessions/<session_id>/<session_id>.messages.json (messages + system_prompt)
  - workspaces/<workspace_id>/workspaceState.json (workspace state)
  - data.db (SQLite database, if present)

Output format per conversation (JSONL):
{
  "session_id": str,
  "source": "cline",
  "source_root": str,
  "system_prompt": str,
  "llm_call": {                          # aggregate across all assistant messages
    "total_input_tokens": int,
    "total_output_tokens": int,
    "total_cache_read_tokens": int,
    "total_cache_write_tokens": int,
    "total_cost": float
  },
  "messages": [
    {
      "id": str,
      "role": "user" | "assistant",
      "content": str,                     # assembled text content
      "thinking": str,                    # reasoning/thinking blocks
      "reasoning": str,                   # alias for thinking
      "tool_calls": [                     # from tool_use content items
        {"id": str, "name": str, "input": {...}}
      ],
      "tool_results": [                   # from tool_result content items
        {"tool_use_id": str, "name": str, "content": [...]}
      ],
      "llm_call": {                       # per-assistant-message LLM metrics
        "model_id": str,
        "provider": str,
        "model_family": str,
        "input_tokens": int,
        "output_tokens": int,
        "cache_read_tokens": int,
        "cache_write_tokens": int,
        "cost": float
      },
      "ts": int
    }
  ],
  "message_count": int,
  "has_tool_calls": bool,
  "metadata": {                           # structured session metadata
    "provider": str,
    "model": str,
    "agent": str,
    "cwd": str,
    "workspace_root": str,
    "prompt": str,                        # raw user prompt (may contain XML tags)
    "status": str,
    "interactive": bool,
    "enable_tools": bool,
    "enable_spawn": bool,
    "enable_teams": bool,
    "version": str,
    "pid": int,
    "exit_code": int,
    "team_name": str,
    "started_at": str,
    "session_id": str,
    "source": str,
    "mode": str,                          # e.g. "act"
    "system_prompt": str,                 # full system prompt from metadata
    "checkpoint_enabled": bool,
    "title": str,
    "usage": {...},                       # session-level token usage
    "aggregate_usage": {...},             # aggregated token usage
    "total_cost": float,
    "aggregated_agents_cost": float,
    "metadata": {...}                     # raw nested metadata dict
  },
  "session_metadata": {...},              # raw session JSON metadata
  "provider": str,                        # convenience top-level
  "model": str,
  "agent": str,
  "cwd": str,
  "workspace_root": str,
  "directory": str,
  "team_name": str,
  "created_at": str,
  "updated_at": str,
  "first_message_time": str,
  "last_message_time": str
}
"""

import json
import sqlite3
from pathlib import Path
from datetime import datetime
import platform
import os


def find_cline_installations():
    """Find all Cline data directories on the device."""
    system = platform.system()
    home = Path.home()

    locations = []

    cline_dirs = [
        home / '.cline',
        home / '.cline' / 'data',
    ]

    if system == 'Linux':
        xdg_data = os.environ.get('XDG_DATA_HOME', home / '.local' / 'share')
        cline_dirs.append(xdg_data / 'cline')
        cline_dirs.append(xdg_data / 'cline' / 'data')

    if system == 'Windows':
        appdata = Path(os.environ.get('APPDATA', home / 'AppData' / 'Roaming'))
        cline_dirs.append(appdata / 'Cline')
        cline_dirs.append(appdata / 'Cline' / 'data')
        localappdata = Path(os.environ.get('LOCALAPPDATA', home / 'AppData' / 'Local'))
        cline_dirs.append(localappdata / 'Cline')
        cline_dirs.append(localappdata / 'Cline' / 'data')

    seen = set()
    for d in cline_dirs:
        resolved = d.resolve()
        key = str(resolved).lower()
        if key not in seen and d.exists():
            seen.add(key)
            locations.append(d)

    return locations


def find_session_dirs(cline_data_dir):
    """Find all session directories under a Cline data directory."""
    sessions_dir = cline_data_dir / 'sessions'
    if not sessions_dir.exists():
        return []
    return [d for d in sessions_dir.iterdir() if d.is_dir()]


def load_json_file(path):
    """Load a JSON file, returning None on failure."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError, TypeError):
        return None


def assemble_message_content(content_data):
    """Assemble a Cline message's content list into structured fields.

    Cline content items have types: text, thinking, tool_use, tool_result.
    Returns dict with keys: content, thinking, tool_calls, tool_results.
    """
    result = {
        'content': '',
        'thinking': '',
        'reasoning': '',
        'tool_calls': [],
        'tool_results': [],
    }

    if not isinstance(content_data, list):
        if isinstance(content_data, str):
            result['content'] = content_data
        return result

    text_parts = []
    thinking_parts = []

    for item in content_data:
        if not isinstance(item, dict):
            continue
        item_type = item.get('type')

        if item_type == 'text':
            text_parts.append(item.get('text', ''))

        elif item_type == 'thinking':
            thinking_parts.append(item.get('thinking', ''))

        elif item_type == 'tool_use':
            tool_call = {
                'id': item.get('id'),
                'name': item.get('name'),
                'input': item.get('input'),
            }
            result['tool_calls'].append(tool_call)

        elif item_type == 'tool_result':
            tool_result = {
                'tool_use_id': item.get('tool_use_id'),
                'name': item.get('name'),
                'content': item.get('content'),
            }
            result['tool_results'].append(tool_result)

    result['content'] = '\n'.join(text_parts)
    result['thinking'] = '\n'.join(thinking_parts)
    result['reasoning'] = result['thinking']

    return result


def extract_llm_call_from_message(msg):
    """Extract LLM call data (model, tokens, cost) from an assistant message."""
    llm_call = {}

    model_info = msg.get('modelInfo')
    if isinstance(model_info, dict):
        llm_call['model_id'] = model_info.get('id')
        llm_call['provider'] = model_info.get('provider')
        llm_call['model_family'] = model_info.get('family')

    metrics = msg.get('metrics')
    if isinstance(metrics, dict):
        llm_call['input_tokens'] = metrics.get('inputTokens')
        llm_call['output_tokens'] = metrics.get('outputTokens')
        llm_call['cache_read_tokens'] = metrics.get('cacheReadTokens')
        llm_call['cache_write_tokens'] = metrics.get('cacheWriteTokens')
        llm_call['cost'] = metrics.get('cost')

    return llm_call if llm_call else None


def extract_session_metadata(session_dir):
    """Extract metadata from a Cline session directory."""
    session_id = session_dir.name
    metadata_file = session_dir / f'{session_id}.json'
    messages_file = session_dir / f'{session_id}.messages.json'

    metadata = load_json_file(metadata_file)
    messages_data = load_json_file(messages_file)

    return metadata, messages_data


def extract_conversation_from_session(session_dir, cline_data_dir):
    """Extract a single conversation from a Cline session directory with all training data fields."""
    session_id = session_dir.name
    metadata, messages_data = extract_session_metadata(session_dir)

    if messages_data is None and metadata is None:
        return None

    # Extract system prompt from messages.json top-level field
    system_prompt = None
    raw_messages = []
    if isinstance(messages_data, dict):
        system_prompt = messages_data.get('system_prompt')
        raw_messages = messages_data.get('messages', [])
    elif isinstance(messages_data, list):
        raw_messages = messages_data

    # Assemble structured messages with all training data fields
    messages = []
    total_input_tokens = 0
    total_output_tokens = 0
    total_cache_read_tokens = 0
    total_cache_write_tokens = 0
    total_cost = 0.0
    has_tool_calls = False

    for msg in raw_messages:
        if not isinstance(msg, dict):
            continue

        role = msg.get('role')
        content_data = msg.get('content', [])
        ts = msg.get('ts')

        assembled = assemble_message_content(content_data)

        structured_msg = {
            'id': msg.get('id'),
            'role': role,
            'content': assembled['content'],
            'thinking': assembled['thinking'],
            'reasoning': assembled['reasoning'],
            'tool_calls': assembled['tool_calls'],
            'tool_results': assembled['tool_results'],
            'ts': ts,
        }

        # Extract LLM call data from assistant messages
        if role == 'assistant':
            llm_call = extract_llm_call_from_message(msg)
            if llm_call:
                structured_msg['llm_call'] = llm_call
                if llm_call.get('model_id'):
                    structured_msg['model'] = llm_call['model_id']
                total_input_tokens += llm_call.get('input_tokens') or 0
                total_output_tokens += llm_call.get('output_tokens') or 0
                total_cache_read_tokens += llm_call.get('cache_read_tokens') or 0
                total_cache_write_tokens += llm_call.get('cache_write_tokens') or 0
                total_cost += llm_call.get('cost') or 0.0

        # Track tool usage
        if assembled['tool_calls']:
            has_tool_calls = True

        # Cline stores tool results as user-role messages whose content is a
        # list of tool_result blocks (no text) and no text parts. The result
        # payloads must be preserved — they become tool-role lines via
        # traces_export.conversation_to_lines, which emits one line per entry
        # in a message's ``tool_results`` list. So, mirroring the claude_code
        # extractor: attach each result to the assistant message that issued
        # the call (resolved by tool_use_id across prior assistant messages,
        # last-assistant fallback), then drop the empty user shell to avoid
        # double-emitting a blank user line.
        if (role == 'user' and not assembled['content'].strip()
                and assembled['tool_results']):
            name_map = {}
            by_id = {}
            last_asst = None
            for m in messages:
                if m.get('role') == 'assistant':
                    last_asst = m
                    for tc in (m.get('tool_calls') or []):
                        if isinstance(tc, dict) and tc.get('id'):
                            name_map[tc['id']] = tc.get('name')
                            by_id.setdefault(tc['id'], m)
            for tr in assembled['tool_results']:
                tid = tr.get('tool_use_id')
                target = by_id.get(tid, last_asst)
                if target is not None:
                    # Fill in the tool name from the call if the result
                    # block didn't carry one (helps dim-4 name coverage).
                    if not tr.get('name') and name_map.get(tid):
                        tr['name'] = name_map[tid]
                    target.setdefault('tool_results', []).append(tr)
                else:
                    # No prior assistant to attach to: emit as a standalone
                    # tool message so the result is never lost.
                    messages.append({
                        'role': 'tool',
                        'tool_call_id': tid,
                        'name': tr.get('name'),
                        'content': tr.get('content'),
                        'is_error': any(
                            isinstance(p, dict)
                            and (p.get('success') is False
                                 or p.get('is_error'))
                            for p in (tr.get('content') or [])
                            if isinstance(p, dict)),
                        'ts': ts,
                    })
            continue

        messages.append(structured_msg)

    # Build conversation object
    conversation = {
        'session_id': session_id,
        'source': 'cline',
        'source_root': str(cline_data_dir),
        'system_prompt': system_prompt,
        'messages': messages,
        'message_count': len(messages),
        'has_tool_calls': has_tool_calls,
    }

    # Aggregate LLM call metrics across all assistant messages
    if total_input_tokens or total_output_tokens or total_cost:
        conversation['llm_call'] = {
            'total_input_tokens': total_input_tokens,
            'total_output_tokens': total_output_tokens,
            'total_cache_read_tokens': total_cache_read_tokens,
            'total_cache_write_tokens': total_cache_write_tokens,
            'total_cost': total_cost,
        }

    # Extract ALL session-level metadata for training data
    if metadata:
        # Full metadata dict (raw)
        conversation['session_metadata'] = metadata

        # Structured metadata with all training-relevant fields
        conv_metadata = {}

        # Session identity
        conv_metadata['session_id'] = metadata.get('session_id')
        conv_metadata['source'] = metadata.get('source')
        conv_metadata['version'] = metadata.get('version')
        conv_metadata['pid'] = metadata.get('pid')
        conv_metadata['exit_code'] = metadata.get('exit_code')

        # Model/provider info
        conv_metadata['provider'] = metadata.get('provider')
        conv_metadata['model'] = metadata.get('model')
        conv_metadata['agent'] = metadata.get('agent')

        # Environment
        conv_metadata['cwd'] = metadata.get('cwd')
        conv_metadata['workspace_root'] = metadata.get('workspace_root')
        conv_metadata['directory'] = metadata.get('cwd') or metadata.get('workspace_root')

        # Feature flags
        conv_metadata['interactive'] = metadata.get('interactive')
        conv_metadata['enable_tools'] = metadata.get('enable_tools')
        conv_metadata['enable_spawn'] = metadata.get('enable_spawn')
        conv_metadata['enable_teams'] = metadata.get('enable_teams')

        # Team info
        conv_metadata['team_name'] = metadata.get('team_name')

        # User prompt (raw, may contain XML tags)
        conv_metadata['prompt'] = metadata.get('prompt')

        # Status and timing
        conv_metadata['status'] = metadata.get('status')
        conv_metadata['started_at'] = metadata.get('started_at')
        conv_metadata['updated_at'] = metadata.get('updated_at')

        # Nested metadata dict (mode, systemPrompt, usage, etc.)
        nested_meta = metadata.get('metadata')
        if isinstance(nested_meta, dict):
            conv_metadata['mode'] = nested_meta.get('mode')
            conv_metadata['system_prompt'] = nested_meta.get('systemPrompt')
            conv_metadata['checkpoint_enabled'] = nested_meta.get('checkpointEnabled')
            conv_metadata['title'] = nested_meta.get('title')

            # Usage at session level
            usage = nested_meta.get('usage')
            if isinstance(usage, dict):
                conv_metadata['usage'] = {
                    'input_tokens': usage.get('inputTokens'),
                    'output_tokens': usage.get('outputTokens'),
                    'cache_read_tokens': usage.get('cacheReadTokens'),
                    'cache_write_tokens': usage.get('cacheWriteTokens'),
                    'total_cost': usage.get('totalCost'),
                }

            # Aggregate usage
            agg_usage = nested_meta.get('aggregateUsage')
            if isinstance(agg_usage, dict):
                conv_metadata['aggregate_usage'] = {
                    'input_tokens': agg_usage.get('inputTokens'),
                    'output_tokens': agg_usage.get('outputTokens'),
                    'cache_read_tokens': agg_usage.get('cacheReadTokens'),
                    'cache_write_tokens': agg_usage.get('cacheWriteTokens'),
                    'total_cost': agg_usage.get('totalCost'),
                }

            # Cost tracking
            conv_metadata['total_cost'] = nested_meta.get('totalCost')
            conv_metadata['aggregated_agents_cost'] = nested_meta.get('aggregatedAgentsCost')

            # Nested metadata fields
            conv_metadata['metadata'] = nested_meta

        conversation['metadata'] = conv_metadata

        # Convenience top-level fields
        conversation['provider'] = metadata.get('provider')
        conversation['model'] = metadata.get('model')
        conversation['agent'] = metadata.get('agent')
        conversation['cwd'] = metadata.get('cwd') or metadata.get('workspace_root')
        conversation['workspace_root'] = metadata.get('workspace_root')
        conversation['directory'] = metadata.get('cwd') or metadata.get('workspace_root')
        conversation['team_name'] = metadata.get('team_name')

        # Timestamps from metadata
        if metadata.get('started_at'):
            conversation['created_at'] = metadata.get('started_at')
        if metadata.get('updated_at'):
            conversation['updated_at'] = metadata.get('updated_at')

    # Try to extract directory from first user message if not found
    if not conversation.get('directory'):
        for msg in messages:
            if msg.get('role') == 'user' and msg.get('content'):
                conversation['directory'] = msg['content'][:200]
                break

    # Find first and last timestamps
    timestamps = []
    for msg in messages:
        ts = msg.get('ts')
        if ts:
            timestamps.append(ts)
    if timestamps:
        parsed = []
        for ts in timestamps:
            dt = parse_timestamp(ts)
            if dt:
                parsed.append(dt)
        if parsed:
            conversation['first_message_time'] = min(parsed).isoformat()
            conversation['last_message_time'] = max(parsed).isoformat()

    # Extract workspace state if workspace_id is available
    workspace_id = None
    if metadata:
        workspace_id = metadata.get('workspace_id')
    if workspace_id:
        conversation['workspace_id'] = workspace_id
        workspace_state = extract_workspace_state(cline_data_dir, workspace_id)
        if workspace_state:
            conversation['workspace_state'] = workspace_state

    return conversation


def parse_timestamp(ts):
    """Parse a timestamp from various formats."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        if ts > 1e12:
            return datetime.fromtimestamp(ts / 1000.0)
        return datetime.fromtimestamp(ts)
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace('Z', '+00:00'))
        except ValueError:
            pass
        try:
            val = float(ts)
            if val > 1e12:
                return datetime.fromtimestamp(val / 1000.0)
            return datetime.fromtimestamp(val)
        except ValueError:
            pass
    return None


def extract_workspace_state(cline_data_dir, workspace_id):
    """Extract workspace state for a given workspace ID."""
    workspace_dir = cline_data_dir / 'workspaces' / workspace_id
    state_file = workspace_dir / 'workspaceState.json'
    return load_json_file(state_file)


def extract_from_cline_dir(cline_data_dir):
    """Extract all conversations from a Cline data directory."""
    conversations = []

    if not (cline_data_dir / 'sessions').exists():
        return conversations

    session_dirs = find_session_dirs(cline_data_dir)

    for session_dir in session_dirs:
        try:
            conv = extract_conversation_from_session(session_dir, cline_data_dir)
            if conv:
                conversations.append(conv)
        except Exception as e:
            print(f"  Error processing session {session_dir.name}: {e}")
            continue

    return conversations


def extract_from_sqlite(db_path):
    """Extract conversations from a Cline SQLite database if present."""
    conversations = []
    db_path = Path(db_path)
    if not db_path.exists():
        return conversations

    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.execute("PRAGMA busy_timeout=10000")
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
    except Exception as e:
        print(f"  Error opening database {db_path}: {e}")
        return conversations

    try:
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
        )
        if not cur.fetchone():
            return conversations

        cur.execute("SELECT * FROM sessions ORDER BY created_at")
        sessions = cur.fetchall()

        for s in sessions:
            try:
                s = dict(s)
                session_id = s.get('id') or s.get('session_id')
                if not session_id:
                    continue

                conversation = {
                    'session_id': session_id,
                    'source': 'cline-sqlite',
                    'source_root': str(db_path),
                }

                # Try to get messages from a messages table
                try:
                    cur.execute(
                        "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at",
                        (session_id,),
                    )
                    msg_rows = cur.fetchall()
                    if msg_rows:
                        messages = []
                        for m in msg_rows:
                            m = dict(m)
                            msg_data = m.get('data')
                            if isinstance(msg_data, str):
                                try:
                                    msg_data = json.loads(msg_data)
                                except (json.JSONDecodeError, TypeError):
                                    pass
                            messages.append(msg_data if isinstance(msg_data, dict) else m)
                        conversation['messages'] = messages
                except sqlite3.OperationalError:
                    pass

                conversations.append(conversation)

            except Exception as e:
                print(f"  Error processing session {s.get('id', 'unknown')}: {e}")
                continue

    finally:
        conn.close()

    return conversations


def discover_cline_roots(path_args):
    """Resolve path arguments to Cline data roots."""
    roots = []
    seen = set()

    for arg in path_args:
        cand = Path(arg)
        if not cand.exists():
            print(f"  Warning: path does not exist, skipping: {arg}")
            continue

        if cand.suffix.lower() in ('.db', '.sqlite', '.sqlite3'):
            if cand.stat().st_size >= 1024:
                k = cand.resolve().as_posix().lower()
                if k not in seen:
                    seen.add(k)
                    roots.append(('sqlite', cand))
            continue

        if (cand / 'sessions').exists() and (cand / 'sessions').is_dir():
            k = cand.resolve().as_posix().lower()
            if k not in seen:
                seen.add(k)
                roots.append(('json', cand))
            continue

        if (cand / 'data' / 'sessions').exists() and (cand / 'data' / 'sessions').is_dir():
            k = (cand / 'data').resolve().as_posix().lower()
            if k not in seen:
                seen.add(k)
                roots.append(('json', cand / 'data'))
            continue

        try:
            for sessions_hit in cand.rglob('sessions'):
                if sessions_hit.is_dir():
                    parent = sessions_hit.parent
                    k = parent.resolve().as_posix().lower()
                    if k not in seen:
                        seen.add(k)
                        roots.append(('json', parent))

            for cline_hit in cand.rglob('.cline'):
                if cline_hit.is_dir() and (cline_hit / 'data' / 'sessions').exists():
                    k = (cline_hit / 'data').resolve().as_posix().lower()
                    if k not in seen:
                        seen.add(k)
                        roots.append(('json', cline_hit / 'data'))

            for db in cand.rglob('*.db'):
                if db.stat().st_size >= 1024:
                    k = db.resolve().as_posix().lower()
                    if k not in seen:
                        seen.add(k)
                        roots.append(('sqlite', db))
        except (PermissionError, OSError) as e:
            print(f"  Error scanning {cand}: {e}")
            continue

    return roots


def main():
    import sys

    print("=" * 80)
    print("CLINE COMPLETE DATA EXTRACTION")
    print("=" * 80)
    print()

    arg_paths = []
    no_auto = False
    include_sqlite = False

    i = 1
    while i < len(sys.argv):
        a = sys.argv[i]
        if a in ('-h', '--help'):
            print("Usage: extract_cline.py [PATH...] [--no-auto] [--sqlite]")
            print("  PATH        Cline data directory, .cline dir, or SQLite .db file.")
            print("              Directories are searched recursively for sessions/.")
            print("  --no-auto   skip default-location auto-discovery.")
            print("  --sqlite    also extract from Cline SQLite databases (if present).")
            return
        elif a == '--no-auto':
            no_auto = True
        elif a == '--sqlite':
            include_sqlite = True
        elif a.startswith('--') or a.startswith('-'):
            print(f"  Warning: unknown flag, ignoring: {a}")
        else:
            arg_paths.append(a)
        i += 1

    installations = []

    if not no_auto:
        auto_roots = find_cline_installations()
        for root in auto_roots:
            installations.append(('json', root))

    if arg_paths:
        print(f"Resolving {len(arg_paths)} path argument(s)...")
        extra_roots = discover_cline_roots(arg_paths)
        print(f"  Discovered {len(extra_roots)} root(s) from path args")
        for t, d in extra_roots:
            print(f"    - {d}")

        all_keys = set(d.resolve().as_posix().lower() for _, d in installations)
        for t, d in extra_roots:
            k = d.resolve().as_posix().lower()
            if k not in all_keys:
                all_keys.add(k)
                installations.append((t, d))

    if not installations:
        print("No Cline installations found!")
        print()
        print("Searched locations:")
        print("  ~/.cline/data (Linux/macOS/Windows)")
        print("  %APPDATA%/Cline (Windows)")
        print("  %LOCALAPPDATA%/Cline (Windows)")
        print("  Pass explicit paths as args, e.g.:")
        print("    python extract_cline.py ~/.cline/data")
        print("    python extract_cline.py C:/Users/<user>/.cline")
        return

    print(f"Found {len(installations)} Cline installation(s):")
    for install_type, install_dir in installations:
        print(f"  - [{install_type}] {install_dir}")
    print()

    all_conversations = []

    for install_type, install_dir in installations:
        print(f"Processing [{install_type}]: {install_dir}")

        if install_type == 'json':
            conversations = extract_from_cline_dir(install_dir)
            print(f"  Extracted {len(conversations)} conversations from JSON sessions")

        elif install_type == 'sqlite':
            if include_sqlite:
                conversations = extract_from_sqlite(install_dir)
                print(f"  Extracted {len(conversations)} conversations from SQLite")
            else:
                print(f"  Skipping SQLite (use --sqlite to include)")
                conversations = []

        all_conversations.extend(conversations)
        print()

    if not all_conversations:
        print("No conversation data found!")
        return

    print("=" * 80)
    print(f"Total conversations extracted: {len(all_conversations)}")

    total_messages = sum(c.get('message_count', len(c.get('messages', []))) for c in all_conversations)
    with_tools = sum(1 for c in all_conversations if c.get('has_tool_calls'))
    with_llm = sum(1 for c in all_conversations if c.get('llm_call'))

    print(f"Total messages: {total_messages}")
    print(f"Conversations with tool calls: {with_tools}")
    print(f"Conversations with LLM call data: {with_llm}")
    print()

    # Save: one HF-traces JSONL file per session (one message per line)
    from traces_export import write_session_files
    n_files, n_lines = write_session_files(all_conversations, 'cline')
    print(f"✅ Saved {n_files} session file(s) to extracted_data/cline/sessions/")
    print(f"   Total message lines: {n_lines:,}")
    print(f"   Format: HF-traces JSONL (one message per line, one file per session)")


if __name__ == '__main__':
    main()