#!/usr/bin/env python3
"""
Extract ALL omp (Oh My Pi) chat/agent data from all projects.

Storage layout:
  ~/.omp/agent/sessions/<slugified-cwd>/<ISO8601>_<uuid>.jsonl   session transcript
  ~/.omp/agent/sessions/<slugified-cwd>/<ISO8601>_<uuid>/        sidecar tool logs
  ~/.omp/agent/history.db                                        prompt history (FTS)

Transcript records are newline-delimited JSON with a top-level "type":
  session / title / title_change                     -> session metadata
  model_change / mode_change / thinking_level_change -> runtime state
  message                                            -> {"message": {"role", "content"}}
        roles: user | assistant | developer | toolResult
        content part types: text | thinking | toolCall
  custom / custom_message                            -> extension payloads
  compaction / branch_summary                        -> context compaction markers

Includes: user/assistant messages, thinking blocks, tool calls + arguments,
tool results, model attribution, compaction boundaries, project path.
No dependencies beyond the Python 3 standard library.
"""

import json
import os
import platform
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

# Runtime-state records: no conversational payload.
STATE_TYPES = {'model_change', 'mode_change', 'thinking_level_change'}


def find_omp_installations():
    """Find all omp data directories."""
    system = platform.system()
    home = Path.home()
    candidates = []

    # Explicit override honoured by the omp harness.
    env_dir = os.environ.get('OMP_HOME') or os.environ.get('OMP_DATA_DIR')
    if env_dir:
        candidates.append(Path(env_dir))

    # Primary location on every platform.
    candidates.append(home / '.omp')

    if system == 'Darwin':
        candidates += [
            home / 'Library/Application Support/omp',
            home / '.config/omp',
        ]
    elif system == 'Linux':
        candidates += [
            home / '.local/share/omp',
            home / '.config/omp',
        ]
    elif system == 'Windows':
        candidates += [
            Path(os.environ.get('APPDATA', home / 'AppData/Roaming')) / 'omp',
            Path(os.environ.get('LOCALAPPDATA', home / 'AppData/Local')) / 'omp',
        ]

    found = []
    for c in candidates:
        # Accept either <dir>/agent/sessions or <dir>/sessions.
        for root in (c / 'agent', c):
            if (root / 'sessions').is_dir() and root not in found:
                found.append(root)
    return found


def _parts(content):
    """Normalise a message content field into a list of part dicts."""
    if isinstance(content, list):
        return [p for p in content if isinstance(p, dict)]
    if isinstance(content, str):
        return [{'type': 'text', 'text': content}]
    return []


def _text(parts):
    return '\n'.join(p.get('text', '') for p in parts if p.get('type') == 'text').strip()


def extract_session(jsonl_file, include_thinking=True, include_developer=True):
    """Extract one omp session transcript into a normalised conversation dict."""
    messages = []
    session_id = None
    title = None
    project_path = None
    created_at = None
    models = []
    current_model = None
    compactions = 0
    parse_errors = 0

    with open(jsonl_file, 'r', errors='replace') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                parse_errors += 1
                continue

            rtype = obj.get('type')

            if rtype == 'session':
                session_id = obj.get('id') or session_id
                project_path = obj.get('cwd') or project_path
                created_at = obj.get('timestamp') or created_at
                title = obj.get('title') or title
                continue

            if rtype in ('title', 'title_change'):
                title = obj.get('title') or title
                continue

            if rtype in STATE_TYPES:
                if rtype == 'model_change':
                    # Older builds emit "model"; newer ones "modelId" (+ "provider").
                    current_model = obj.get('model') or obj.get('modelId')
                    provider = obj.get('provider')
                    if current_model and provider and '/' not in current_model:
                        current_model = f'{provider}/{current_model}'
                    if current_model and current_model not in models:
                        models.append(current_model)
                continue

            if rtype in ('compaction', 'branch_summary'):
                compactions += 1
                messages.append({
                    'role': 'system',
                    'event': rtype,
                    'content': obj.get('summary') or obj.get('text') or '',
                    'timestamp': obj.get('timestamp'),
                })
                continue

            if rtype == 'custom_message':
                # Extension-authored turns keep content at the top level, not
                # inside a nested "message" object.
                content = obj.get('content')
                text = content if isinstance(content, str) else _text(_parts(content))
                if not text:
                    continue
                messages.append({
                    'role': obj.get('role') or obj.get('attribution') or 'system',
                    'content': text,
                    'timestamp': obj.get('timestamp'),
                    'id': obj.get('id'),
                    'parent_id': obj.get('parentId'),
                    'custom': True,
                    'custom_type': obj.get('customType'),
                    'display': obj.get('display'),
                    'details': obj.get('details'),
                })
                continue

            if rtype != 'message':
                # 'custom' and anything else is extension-private; skip.
                continue

            inner = obj.get('message') or {}
            role = inner.get('role')
            parts = _parts(inner.get('content'))
            if not role or not parts:
                continue

            if role == 'developer' and not include_developer:
                continue

            if role == 'toolResult':
                result = {
                    'tool_call_id': inner.get('toolCallId') or obj.get('toolCallId'),
                    'content': _text(parts),
                    'timestamp': obj.get('timestamp'),
                }
                # Attach to the owning assistant turn when possible.
                for prev in reversed(messages):
                    if prev.get('role') == 'assistant':
                        prev.setdefault('tool_results', []).append(result)
                        break
                else:
                    messages.append({'role': 'toolResult', **result})
                continue

            msg = {
                'role': role,
                'content': _text(parts),
                'timestamp': obj.get('timestamp'),
                'id': obj.get('id'),
                'parent_id': obj.get('parentId'),
            }

            if role == 'assistant':
                msg['model'] = inner.get('model') or current_model
                thinking = [p.get('thinking', '') for p in parts if p.get('type') == 'thinking']
                if thinking and include_thinking:
                    msg['thinking'] = '\n'.join(t for t in thinking if t)
                tool_calls = [
                    {
                        'id': p.get('id') or p.get('toolCallId'),
                        'name': p.get('name') or p.get('tool'),
                        'arguments': p.get('arguments', p.get('args')),
                    }
                    for p in parts if p.get('type') == 'toolCall'
                ]
                if tool_calls:
                    msg['tool_calls'] = tool_calls

            if msg['content'] or msg.get('tool_calls') or msg.get('thinking'):
                messages.append(msg)

    if not messages:
        return None

    # Sidecar tool logs live in a directory named after the transcript stem.
    sidecar = jsonl_file.with_suffix('')
    tool_logs = sorted(p.name for p in sidecar.iterdir()) if sidecar.is_dir() else []

    if session_id is None:
        # Filename form: <ISO8601>_<uuid>
        session_id = jsonl_file.stem.split('_', 1)[-1]

    return {
        'messages': messages,
        'source': 'omp',
        'session_id': session_id,
        'name': title,
        'project_path': project_path,
        'project_slug': jsonl_file.parent.name,
        'created_at': created_at,
        'models': models,
        'compactions': compactions,
        'tool_logs': tool_logs,
        'source_file': str(jsonl_file),
        'parse_errors': parse_errors,
    }


def extract_prompt_history(root):
    """Extract the standalone prompt history database (best effort).

    omp runs history.db in WAL mode, so committed-but-uncheckpointed rows live
    in the -wal sidecar. Opening with immutable=1 would ignore that sidecar and
    silently return only the older main-file rows. Instead snapshot the db and
    its -wal/-shm companions into a temp dir and read the copy: WAL-aware,
    while never writing to the live files.
    """
    db = root / 'history.db'
    if not db.exists():
        return []
    rows = []
    try:
        with tempfile.TemporaryDirectory(prefix='omp-history-') as tmp:
            snapshot = Path(tmp) / db.name
            for suffix in ('', '-wal', '-shm'):
                src = db.with_name(db.name + suffix)
                if src.exists():
                    shutil.copy2(src, snapshot.with_name(snapshot.name + suffix))
            conn = sqlite3.connect(f'file:{snapshot}?mode=ro', uri=True)
            conn.row_factory = sqlite3.Row
            for r in conn.execute('SELECT * FROM history'):
                rows.append({k: r[k] for k in r.keys()})
            conn.close()
    except (sqlite3.Error, OSError) as e:
        print(f'   ⚠️  history.db unreadable: {e}')
    return rows


def main():
    print('=' * 80)
    print('OMP (OH MY PI) COMPLETE DATA EXTRACTION')
    print('=' * 80)
    print()

    print('🔍 Searching for omp installations...')
    installations = find_omp_installations()
    if not installations:
        print('❌ No omp installations found! (expected ~/.omp/agent/sessions)')
        return

    print(f'✅ Found {len(installations)} installation(s):')
    for inst in installations:
        print(f'   - {inst}')
    print()

    all_conversations = []
    project_stats = {}
    history_rows = []

    for root in installations:
        print(f'📂 Processing: {root}')
        transcripts = sorted((root / 'sessions').glob('*/*.jsonl'))
        # Skip extension sidecar transcripts (e.g. __advisor.jsonl).
        transcripts = [t for t in transcripts if not t.name.startswith('__')]
        print(f'   {len(transcripts)} transcript file(s)')

        for t in transcripts:
            try:
                conv = extract_session(t)
            except OSError as e:
                print(f'   Error reading {t}: {e}')
                continue
            if not conv:
                continue
            conv['installation'] = str(root)
            all_conversations.append(conv)
            key = conv.get('project_path') or conv['project_slug']
            project_stats[key] = project_stats.get(key, 0) + 1

        history_rows.extend(extract_prompt_history(root))

    print()
    print('=' * 80)
    print('EXTRACTION COMPLETE')
    print('=' * 80)
    print(f'Total conversations: {len(all_conversations):,}')

    if not all_conversations:
        print('No conversations found!')
        return

    total_messages = sum(len(c['messages']) for c in all_conversations)
    with_tools = sum(
        1 for c in all_conversations
        if any('tool_calls' in m or 'tool_results' in m for m in c['messages'])
    )
    with_thinking = sum(
        1 for c in all_conversations if any('thinking' in m for m in c['messages'])
    )
    complete = sum(
        1 for c in all_conversations
        if any(m['role'] == 'assistant' for m in c['messages'])
    )

    print(f'Complete conversations: {complete:,}')
    print(f'Total messages: {total_messages:,}')
    print(f'With tool calls/results: {with_tools:,}')
    print(f'With thinking blocks: {with_thinking:,}')
    print(f'Prompt history rows: {len(history_rows):,}')
    print()

    print('Breakdown by project (top 20):')
    for proj, count in sorted(project_stats.items(), key=lambda x: -x[1])[:20]:
        print(f'  {str(proj)[:50]:50} {count:5,} conversations')
    print()

    output_dir = Path('extracted_data')
    output_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    output_file = output_dir / f'omp_conversations_{timestamp}.jsonl'
    with open(output_file, 'w') as f:
        for conv in all_conversations:
            f.write(json.dumps(conv, ensure_ascii=False) + '\n')

    size_mb = output_file.stat().st_size / 1024 / 1024
    print(f'✅ Saved to: {output_file}')
    print(f'   Size: {size_mb:.2f} MB')
    print('   Format: JSONL (one conversation per line)')

    if history_rows:
        # Prompt-history rows do not follow the conversation schema, so they must
        # stay out of extracted_data/*.jsonl — extract_all.sh wildcards that glob
        # when counting conversations and building ALL_CONVERSATIONS.
        hist_dir = output_dir / 'omp_prompt_history'
        hist_dir.mkdir(exist_ok=True)
        hist_file = hist_dir / f'omp_prompt_history_{timestamp}.jsonl'
        with open(hist_file, 'w') as f:
            for row in history_rows:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + '\n')
        print(f'✅ Saved to: {hist_file}')
        print('   (kept in a subdirectory: not a conversation-schema file)')


if __name__ == '__main__':
    main()
