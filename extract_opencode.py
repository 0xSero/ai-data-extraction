#!/usr/bin/env python3
"""
Extract ALL OpenCode conversation data
Supports: CLI (JSON files) and Desktop (Tauri .dat files)

Storage locations:
- CLI: ~/.local/share/opencode/storage/ (Linux/macOS)
- Desktop: Platform-specific Tauri app data directories

Features:
- Extracts conversations from sessions WITH and WITHOUT metadata files
- Reconstructs session metadata (directory, title, timestamps) from message content
- Assembles complete messages from message metadata + parts
- Handles sessions where session files are missing or corrupted
"""

import json
import struct
from pathlib import Path
import platform
import os
import sqlite3
import re
import shutil
from collections import defaultdict


def _safe_json_loads(s):
    """Parse a JSON string, returning the raw string on failure."""
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return s


def find_opencode_installations():
    """Find all OpenCode installation directories and SQLite databases.
    
    Paths per official opencode docs (https://opencode.ai/docs/troubleshooting):
      - CLI (all platforms): ~/.local/share/opencode/
      - Desktop (macOS):      ~/Library/Application Support/ai.opencode.app
      - Desktop (Linux):      ~/.local/share/ai.opencode.app
      - Desktop (Windows):    %APPDATA%/ai.opencode.app
    """
    system = platform.system()
    home = Path.home()
    
    locations = []
    
    # CLI storage location (same on all platforms per docs)
    cli_dir = home / '.local' / 'share' / 'opencode'
    if cli_dir.exists():
        # Check for legacy JSON-file storage (1.1.x)
        if (cli_dir / 'storage' / 'message').exists():
            locations.append(('cli', cli_dir))
        # Check for SQLite database (1.2+)
        for db_name in ('opencode.db', 'opencode-local.db'):
            db = cli_dir / db_name
            if db.exists() and db.stat().st_size > 0:
                locations.append(('sqlite', db))
    
    # macOS fallback: old CLI path (~/Library/Application Support/opencode)
    if system == "Darwin":
        mac_old = home / "Library/Application Support/opencode"
        if mac_old.exists() and mac_old.resolve() != cli_dir.resolve():
            if (mac_old / 'storage' / 'message').exists():
                locations.append(('cli', mac_old))
            for db_name in ('opencode.db', 'opencode-local.db'):
                db = mac_old / db_name
                if db.exists() and db.stat().st_size > 0:
                    locations.append(('sqlite', db))
    
    # Desktop storage locations (Tauri app data)
    if system == "Darwin":
        desktop_dirs = [home / "Library/Application Support/ai.opencode.app"]
    elif system == "Linux":
        desktop_dirs = [home / ".local/share/ai.opencode.app"]
    elif system == "Windows":
        desktop_dirs = [Path(os.environ.get('APPDATA', home / 'AppData/Roaming')) / 'ai.opencode.app']
    else:
        desktop_dirs = []
    
    for desktop_dir in desktop_dirs:
        if desktop_dir.exists():
            locations.append(('desktop', desktop_dir))
            db_desktop = desktop_dir / 'opencode.db'
            if db_desktop.exists() and db_desktop.stat().st_size > 0:
                locations.append(('sqlite', db_desktop))

    return locations

def read_tauri_store(dat_file):
    """
    Parse Tauri store .dat files
    Format: Simple key-value pairs with length prefixes
    """
    try:
        with open(dat_file, 'rb') as f:
            data = f.read()
        
        store = {}
        offset = 0
        
        while offset < len(data):
            # Try to read key length (4 bytes, little-endian)
            if offset + 4 > len(data):
                break
            
            key_len = struct.unpack('<I', data[offset:offset+4])[0]
            offset += 4
            
            # Sanity check
            if key_len > 10000 or offset + key_len > len(data):
                break
            
            # Read key
            key = data[offset:offset+key_len].decode('utf-8', errors='ignore')
            offset += key_len
            
            # Read value length
            if offset + 4 > len(data):
                break
            
            value_len = struct.unpack('<I', data[offset:offset+4])[0]
            offset += 4
            
            # Sanity check
            if value_len > 1000000 or offset + value_len > len(data):
                break
            
            # Read value
            try:
                value_bytes = data[offset:offset+value_len]
                value = json.loads(value_bytes.decode('utf-8'))
                store[key] = value
            except:
                pass
            
            offset += value_len
        
        return store
    
    except Exception as e:
        print(f"Error reading Tauri store {dat_file}: {e}")
        return {}

def extract_directory_from_content(text):
    """
    Try to extract a directory path from text content (e.g., tool commands).
    Looks for common patterns like 'cd /path/to/dir' or paths in commands.
    """
    if not text:
        return None
    
    # Pattern 1: cd command followed by path
    cd_pattern = r'cd\s+(["\']?)([^\s\'"]+)\1'
    matches = re.findall(cd_pattern, text)
    for match in matches:
        path = match[1] if isinstance(match, tuple) else match
        if path and (path.startswith('/') or path.startswith('~') or path[1:].startswith(':')):
            return path
    
    # Pattern 2: Common working directory indicators
    cwd_pattern = r'(?:working\s+)?directory[:\s]+(["\']?)([^\s\'"]+)\1'
    matches = re.findall(cwd_pattern, text)
    for match in matches:
        path = match[1] if isinstance(match, tuple) else match
        if path and (path.startswith('/') or path.startswith('~') or path[1:].startswith(':')):
            return path
    
    # Pattern 3: Extract absolute paths (Unix-style)
    abs_path_pattern = r'(?:^|\s|/)(/[^/\s\'"]{2,})'
    matches = re.findall(abs_path_pattern, text)
    for path in matches:
        if path and len(path) > 3 and not path.endswith('.') and not path.endswith('..'):
            return path
    
    return None


def extract_project_id_from_content(text):
    """
    Try to extract a project ID from text content.
    Often appears in tool commands or git operations.
    """
    if not text:
        return None
    
    # Pattern: project IDs in commands
    project_pattern = r'(?:project[-_]?id|project)[=:\s]+([a-zA-Z0-9_-]+)'
    match = re.search(project_pattern, text, re.IGNORECASE)
    if match:
        return match.group(1)
    
    return None


def _extract_message_fields(msg_data):
    """Extract ALL fields from a message data dict."""
    m = {}
    m['id'] = msg_data.get('id')
    m['session_id'] = msg_data.get('sessionID')
    m['role'] = msg_data.get('role')
    m['timestamp'] = msg_data.get('time', {}).get('created')
    m['time_completed'] = msg_data.get('time', {}).get('completed')
    if 'modelID' in msg_data:
        m['model'] = msg_data['modelID']
    if 'providerID' in msg_data:
        m['provider'] = msg_data['providerID']
    if isinstance(msg_data.get('model'), dict):
        m['model_object'] = msg_data['model']
    if 'agent' in msg_data:
        m['agent'] = msg_data['agent']
    if 'mode' in msg_data:
        m['mode'] = msg_data['mode']
    p = msg_data.get('path', {})
    if p:
        m['path_cwd'] = p.get('cwd')
        m['path_root'] = p.get('root')
    if 'tokens' in msg_data:
        m['tokens'] = msg_data['tokens']
    if 'cost' in msg_data:
        m['cost'] = msg_data['cost']
    if 'finish' in msg_data:
        m['finish_reason'] = msg_data['finish']
    if 'error' in msg_data:
        m['message_error'] = msg_data['error']
    if 'parentID' in msg_data:
        m['parent_message_id'] = msg_data['parentID']
    if 'summary' in msg_data:
        m['summary'] = msg_data['summary']
    if 'tools' in msg_data:
        m['tools'] = msg_data['tools']
    if 'variant' in msg_data:
        m['variant'] = msg_data['variant']
    if 'system' in msg_data:
        m['system'] = msg_data['system']
    return m


def _extract_part_fields(part_data):
    """Extract ALL fields from a part data dict."""
    p = {}
    p['id'] = part_data.get('id')
    p['type'] = part_data.get('type')
    p['text'] = part_data.get('text', '')
    p['synthetic'] = part_data.get('synthetic')
    p['session_id'] = part_data.get('sessionID')
    p['message_id'] = part_data.get('messageID')
    t = part_data.get('time', {})
    if t:
        p['time_start'] = t.get('start')
        p['time_end'] = t.get('end')
    if 'metadata' in part_data:
        p['metadata'] = part_data['metadata']
    if part_data.get('type') in ('tool', 'tool-call'):
        p['tool'] = part_data.get('tool')
        p['call_id'] = part_data.get('callID')
        s = part_data.get('state', {})
        if s:
            p['state_status'] = s.get('status')
            p['state_input'] = s.get('input')
            p['state_output'] = s.get('output')
            p['state_title'] = s.get('title')
            if 'metadata' in s:
                p['state_metadata'] = s['metadata']
            if 'time' in s:
                p['state_time'] = s['time']
    if part_data.get('type') == 'step-finish':
        p['reason'] = part_data.get('reason')
        p['cost'] = part_data.get('cost')
        if 'tokens' in part_data:
            p['tokens'] = part_data['tokens']
    if part_data.get('type') == 'file':
        p['filename'] = part_data.get('filename')
        p['mime'] = part_data.get('mime')
        p['url'] = part_data.get('url')
        if 'source' in part_data:
            p['source'] = part_data['source']
    if 'snapshot' in part_data:
        p['snapshot'] = part_data['snapshot']
    if 'files' in part_data:
        p['files'] = part_data['files']
    if 'hash' in part_data:
        p['hash'] = part_data['hash']
    # state.error for tool parts with failed status
    if part_data.get('type') in ('tool', 'tool-call'):
        s = part_data.get('state', {})
        if 'error' in s:
            p['state_error'] = s['error']
    return p


def _assemble_parts(parts_data_list, raw=False):
    """Assemble parts into content, tool_calls, tool_results, reasoning, full parts array.

    When ``raw`` is True, ``parts`` preserves the full original part JSON dicts
    instead of the curated field extraction (opt-in via --raw-parts).
    """
    content_parts = []
    tool_calls = []
    tool_results = []
    reasoning_parts = []
    parts_out = []
    for pd in parts_data_list:
        parts_out.append(pd if raw else _extract_part_fields(pd))
        pt = pd.get('type')
        txt = pd.get('text', '')
        if pt == 'text':
            content_parts.append(txt)
        elif pt in ('tool', 'tool-call'):
            s = pd.get('state', {})
            tn = pd.get('tool', pd.get('name'))
            has_output = s.get('status') == 'completed' and 'output' in s
            tc = {
                'id': pd.get('callID', pd.get('id')),
                'name': tn,
                'input': s.get('input', pd.get('input')),
                # Flag interrupted / in-flight tools (no output yet recorded)
                # so consumers can distinguish a genuine interruption from a
                # lost result line (freebuff Bug 5 pattern). Preserved through
                # traces_export._tool_call_entry as an extra key.
                'has_result': has_output,
            }
            if has_output:
                tool_results.append({
                    'tool_call_id': pd.get('callID'),
                    'tool': tn,
                    'output': s['output'],
                })
            tool_calls.append(tc)
        elif pt == 'tool-result':
            tool_results.append({
                'tool_call_id': pd.get('toolCallID'),
                'output': pd.get('output'),
            })
        elif pt == 'code':
            lang = pd.get('language', '')
            content_parts.append(f"```{lang}\n{txt}\n```")
        elif pt == 'reasoning':
            if txt:
                reasoning_parts.append(txt)
        elif pt == 'file':
            content_parts.append(f"[File: {pd.get('filename', 'unknown')} ({pd.get('mime', '')})]")
        elif pt == 'patch':
            if txt:
                content_parts.append(f"[Patch: {len(txt)} bytes]")
    return {
        'content': '\n'.join(content_parts),
        'tool_calls': tool_calls,
        'tool_results': tool_results,
        'reasoning': '\n'.join(reasoning_parts),
        'parts': parts_out,
    }


def extract_cli_conversations(storage_dir, raw_parts=False):
    """
    Extract conversations from CLI JSON storage.
    
    Handles sessions both WITH and WITHOUT session metadata files.
    For sessions without metadata, reconstructs session info from messages/parts.
    Each conversation dict gets a 'source_root' field pointing back to storage_dir.
    """
    conversations = []
    storage_root = str(storage_dir)
    
    message_dir = storage_dir / 'storage' / 'message'
    part_dir = storage_dir / 'storage' / 'part'
    
    if not message_dir.exists():
        print(f"  Message directory not found: {message_dir}")
        return conversations
    
    # Find all session directories (each is a directory named ses_xxx)
    session_dirs = [d for d in message_dir.iterdir() if d.is_dir() and d.name.startswith('ses_')]
    
    print(f"  Found {len(session_dirs)} session directories")
    
    processed_sessions = set()
    
    for session_dir_path in session_dirs:
        try:
            session_id = session_dir_path.name
            
            # Skip if already processed (deduplication)
            if session_id in processed_sessions:
                continue
            processed_sessions.add(session_id)
            
            # Try to load session metadata if available
            session_data = None
            session_file = storage_dir / 'storage' / 'session' / 'global' / f'{session_id}.json'
            
            if session_file.exists():
                with open(session_file) as f:
                    session_data = json.load(f)
            
            # Collect all messages for this session
            message_files = sorted(session_dir_path.glob('msg_*.json'))
            
            if not message_files:
                continue
            
            messages = []
            all_content = []  # For reconstructing metadata
            first_message_time = None
            last_message_time = None
            
            for msg_file in message_files:
                try:
                    with open(msg_file) as f:
                        msg_data = json.load(f)
                    
                    message = _extract_message_fields(msg_data)
                    message['content'] = ''
                    message['tool_calls'] = []
                    message['tool_results'] = []
                    message['reasoning'] = ''
                    message['parts'] = []
                    
                    # Track timestamps
                    ts = message.get('timestamp')
                    if ts:
                        if not first_message_time or ts < first_message_time:
                            first_message_time = ts
                        if not last_message_time or ts > last_message_time:
                            last_message_time = ts
                    
                    # Find all parts for this message
                    mid = msg_data.get('id')
                    message_part_dir = part_dir / mid if mid else None
                    
                    if message_part_dir and message_part_dir.exists():
                        part_files = sorted(message_part_dir.glob('prt_*.json'))
                        parts_raw = []
                        for part_file in part_files:
                            try:
                                with open(part_file) as f:
                                    part_data = json.load(f)
                                parts_raw.append(part_data)
                                # Collect content for metadata reconstruction
                                ptxt = part_data.get('text', '')
                                if ptxt:
                                    all_content.append(ptxt)
                            except Exception as e:
                                print(f"    Error reading part {part_file}: {e}")
                                continue
                        
                        assembled = _assemble_parts(parts_raw, raw_parts)
                        message['content'] = assembled['content']
                        message['parts'] = assembled['parts']
                        if assembled['tool_calls']:
                            message['tool_calls'] = assembled['tool_calls']
                        if assembled['tool_results']:
                            message['tool_results'] = assembled['tool_results']
                        if assembled['reasoning']:
                            message['reasoning'] = assembled['reasoning']
                    
                    messages.append(message)
                
                except Exception as e:
                    print(f"    Error reading message {msg_file}: {e}")
                    continue
            
            if not messages:
                continue
            
            # Build conversation - use session data if available, otherwise reconstruct
            combined_content = '\n'.join(all_content)
            
            conversation = {
                'messages': messages,
                'source': 'opencode-cli',
                'source_root': storage_root,
                'session_id': session_id,
            }
            
            if session_data:
                # Use metadata from session file
                conversation['title'] = session_data.get('title')
                conversation['created_at'] = session_data.get('time', {}).get('created')
                conversation['updated_at'] = session_data.get('time', {}).get('updated')
                conversation['project_id'] = session_data.get('projectID')
                conversation['directory'] = session_data.get('directory')
                conversation['version'] = session_data.get('version')
                
                if 'slug' in session_data:
                    conversation['slug'] = session_data['slug']
                if 'permission' in session_data:
                    conversation['permission'] = session_data['permission']
                
                # Add summary stats if available
                if 'summary' in session_data:
                    conversation['summary'] = session_data['summary']
                
                # Add parent session if it's a child session
                if 'parentID' in session_data:
                    conversation['parent_session_id'] = session_data['parentID']
            else:
                # RECONSTRUCT metadata from messages/parts (no session file).
                # No parent link available without a session metadata file.
                conversation['parent_session_id'] = None
                conversation['created_at'] = first_message_time
                conversation['updated_at'] = last_message_time
                
                # Try to extract directory from content
                conversation['directory'] = extract_directory_from_content(combined_content)
                
                # Try to extract project ID from content
                conversation['project_id'] = extract_project_id_from_content(combined_content)
                
                # Generate a title from first user message
                for msg in messages:
                    if msg.get('role') == 'user' and msg.get('content'):
                        # Take first 100 chars of first user message as title
                        title = msg['content'][:100].strip()
                        if len(msg['content']) > 100:
                            title += '...'
                        conversation['title'] = title
                        break
                
                # Set default version
                conversation['version'] = 'unknown'
            
            conversations.append(conversation)
        
        except Exception as e:
            print(f"  Error processing session {session_dir_path}: {e}")
            continue
    
    return conversations


def extract_sqlite_conversations(db_path, include_events=False, raw_parts=False):
    """Extract conversations from OpenCode 1.2+ SQLite database.

    Schema (opencode.db / opencode-local.db):
      session  (id, title, directory, parent_id, project_id, version, slug,
                time_created, time_updated, agent, model, cost,
                tokens_input, tokens_output, tokens_reasoning,
                tokens_cache_read, tokens_cache_write, metadata)
      message  (id, session_id, time_created, data)   -- data=JSON of role, modelID, ...
      part     (id, message_id, session_id, data)     -- data=JSON of type, text, tool, ...
    """
    conversations = []
    db_path = Path(db_path)
    if not db_path.exists():
        print(f"  Database not found: {db_path}")
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
        # Detect available columns in session table (schema varies by version)
        cur.execute("PRAGMA table_info(session)")
        existing_cols = {row['name'] for row in cur.fetchall()}
        all_session_cols = [
            'id', 'title', 'directory', 'parent_id', 'project_id', 'version', 'slug',
            'time_created', 'time_updated', 'agent', 'model', 'cost',
            'tokens_input', 'tokens_output', 'tokens_reasoning',
            'tokens_cache_read', 'tokens_cache_write', 'metadata',
            'share_url', 'summary_additions', 'summary_deletions',
            'summary_files', 'summary_diffs', 'revert', 'permission',
            'time_compacting', 'time_archived', 'workspace_id', 'path',
        ]
        cols = [c for c in all_session_cols if c in existing_cols]
        cur.execute(
            f"SELECT {', '.join(cols)} FROM session ORDER BY time_created"
        )
        sessions = cur.fetchall()

        # Messages + parts are fetched PER SESSION through the indexed columns
        # (message_session_time_created_id_idx, part_session_idx) instead of
        # loading all ~2.6 GB of JSON blobs into memory at once — on a 35 GB
        # db the bulk fetchall() stalls for minutes and risks OOM.
        msg_cur = conn.cursor()

        # Note: the event table (94% of the db, 842k rows) is fetched
        # per-session below (opt-in --events) to avoid loading all rows into
        # memory and to avoid a large temp sort spilling to disk.

        storage_root = str(db_path)

        # Dedicated cursor for per-session event fetch (opt-in --events).
        ev_cur = conn.cursor() if include_events else None

        for s in sessions:
            try:
                s = dict(s)  # sqlite3.Row has no .get()
                session_id = s['id']
                title = s.get('title')
                directory = s.get('directory')
                parent_id = s.get('parent_id')
                version = s.get('version')
                project_id = s.get('project_id')
                ses_created = s.get('time_created')
                ses_updated = s.get('time_updated')
                agent = s.get('agent')
                model_raw = s.get('model')
                cost = s.get('cost')
                t_input = s.get('tokens_input')
                t_output = s.get('tokens_output')
                t_reasoning = s.get('tokens_reasoning')
                t_cache_read = s.get('tokens_cache_read')
                t_cache_write = s.get('tokens_cache_write')
                metadata_json = s.get('metadata')
                slug = s.get('slug')
                share_url = s.get('share_url')
                summary_additions = s.get('summary_additions')
                summary_deletions = s.get('summary_deletions')
                summary_files = s.get('summary_files')
                summary_diffs = s.get('summary_diffs')
                revert = s.get('revert')
                permission = s.get('permission')
                time_compacting = s.get('time_compacting')
                time_archived = s.get('time_archived')
                workspace_id = s.get('workspace_id')
                session_path = s.get('path')

                msg_cur.execute(
                    "SELECT id, session_id, time_created, data "
                    "FROM message WHERE session_id = ? "
                    "ORDER BY time_created",
                    (session_id,),
                )
                messages_raw = msg_cur.fetchall()
                if not messages_raw:
                    continue

                # Group this session's parts by message id (indexed by session).
                msg_cur.execute(
                    "SELECT id, message_id, data "
                    "FROM part WHERE session_id = ? "
                    "ORDER BY time_created",
                    (session_id,),
                )
                parts_by_message = defaultdict(list)
                for p in msg_cur.fetchall():
                    parts_by_message[p['message_id']].append(p)

                messages_out = []
                first_message_time = None
                last_message_time = None

                for msg_row in messages_raw:
                    try:
                        msg_data = json.loads(msg_row['data'])
                    except (json.JSONDecodeError, TypeError):
                        continue

                    message = _extract_message_fields(msg_data)
                    message['content'] = ''
                    message['tool_calls'] = []
                    message['tool_results'] = []
                    message['reasoning'] = ''
                    message['parts'] = []

                    # Fall back to SQLite row-level columns if data JSON lacks them
                    if not message.get('id'):
                        message['id'] = msg_row['id']
                    if not message.get('session_id'):
                        message['session_id'] = msg_row['session_id']
                    if not message.get('timestamp'):
                        message['timestamp'] = msg_row['time_created']

                    ts = message.get('timestamp')
                    if ts:
                        if not first_message_time or ts < first_message_time:
                            first_message_time = ts
                        if not last_message_time or ts > last_message_time:
                            last_message_time = ts

                    # Assemble parts
                    parts_raw = parts_by_message.get(msg_row['id'], [])
                    parts_data = []
                    for p in parts_raw:
                        try:
                            parts_data.append(json.loads(p['data']))
                        except (json.JSONDecodeError, TypeError):
                            continue

                    assembled = _assemble_parts(parts_data, raw_parts)
                    message['content'] = assembled['content']
                    message['parts'] = assembled['parts']
                    if assembled['tool_calls']:
                        message['tool_calls'] = assembled['tool_calls']
                    if assembled['tool_results']:
                        message['tool_results'] = assembled['tool_results']
                    if assembled['reasoning']:
                        message['reasoning'] = assembled['reasoning']

                    messages_out.append(message)

                if not messages_out:
                    continue

                conv = {
                    'messages': messages_out,
                    'source': 'opencode-sqlite',
                    'source_root': storage_root,
                    'session_id': session_id,
                    'title': title or None,
                    'directory': directory or None,
                    'project_id': project_id,
                    'version': version,
                    'parent_session_id': parent_id,
                    'created_at': ses_created,
                    'updated_at': ses_updated,
                }

                if slug:
                    conv['slug'] = slug
                if share_url:
                    conv['share_url'] = share_url
                if summary_additions is not None or summary_deletions is not None or summary_files is not None:
                    conv['summary'] = {
                        'additions': summary_additions,
                        'deletions': summary_deletions,
                        'files': summary_files,
                    }
                if summary_diffs:
                    conv['summary_diffs'] = summary_diffs
                if revert:
                    conv['revert'] = revert
                if permission:
                    conv['permission'] = permission
                if time_compacting:
                    conv['time_compacting'] = time_compacting
                if time_archived:
                    conv['time_archived'] = time_archived
                if workspace_id:
                    conv['workspace_id'] = workspace_id
                if session_path:
                    conv['path'] = session_path

                # Parse model JSON string
                if model_raw:
                    try:
                        conv['model'] = json.loads(model_raw)
                    except (json.JSONDecodeError, TypeError):
                        conv['model'] = model_raw

                if agent:
                    conv['agent'] = agent
                if cost:
                    conv['cost'] = cost
                if t_input or t_output or t_reasoning or t_cache_read or t_cache_write:
                    conv['tokens'] = {
                        'input': t_input,
                        'output': t_output,
                        'reasoning': t_reasoning,
                        'cache_read': t_cache_read,
                        'cache_write': t_cache_write,
                    }
                if metadata_json:
                    try:
                        md = json.loads(metadata_json)
                        if md:
                            conv['metadata'] = md
                    except Exception:
                        pass

                # Attach the event-sourcing log for this session (opt-in --events).
                # Fetched per-session with a dedicated cursor to avoid loading
                # all 842k events into memory and to avoid a large temp sort
                # spilling to disk (the C: temp dir is small).
                if include_events:
                    try:
                        ev_cur.execute(
                            "SELECT type, seq, data FROM event "
                            "WHERE aggregate_id = ? ORDER BY seq",
                            (session_id,),
                        )
                        evs_raw = ev_cur.fetchall()
                        if evs_raw:
                            conv['events'] = [
                                {
                                    'type': e['type'],
                                    'seq': e['seq'],
                                    'data': _safe_json_loads(e['data']),
                                }
                                for e in evs_raw
                            ]
                    except Exception as e:
                        print(f"  WARN: could not read events for {session_id} — {e}")

                conversations.append(conv)

            except Exception as e:
                print(f"  Error processing session {s['id']}: {e}")
                continue

    finally:
        conn.close()

    return conversations


def extract_desktop_conversations(desktop_dir):
    conversations = []
    
    # Look for .dat files
    dat_files = list(desktop_dir.rglob('*.dat'))
    
    if not dat_files:
        return conversations
    
    print(f"  Found {len(dat_files)} .dat store files")
    
    for dat_file in dat_files:
        store = read_tauri_store(dat_file)
        
        if not store:
            continue
        
        # Look for session/conversation data in the store
        # Keys might be like "session:ses_xxxxx" or similar
        for key, value in store.items():
            if not isinstance(value, dict):
                continue
            
            # Check if this looks like a conversation/session
            if 'messages' in value or 'history' in value:
                try:
                    messages = value.get('messages', value.get('history', []))
                    
                    if not messages:
                        continue
                    
                    conversation = {
                        'messages': messages,
                        'source': 'opencode-desktop',
                        'store_key': key,
                        'store_file': str(dat_file.name)
                    }
                    
                    # Add any additional metadata
                    for meta_key in ['session_id', 'title', 'created_at', 'workspace']:
                        if meta_key in value:
                            conversation[meta_key] = value[meta_key]
                    
                    conversations.append(conversation)
                
                except Exception as e:
                    continue
    
    return conversations

def discover_storage_roots_from_paths(path_args):
    """
    Given a list of path strings (CLI args), resolve each to an opencode CLI
    storage root (JSON-file based) or SQLite database.

    Returns list of ('cli', Path) and/or ('sqlite', Path) tuples.
    """
    roots = []
    seen = set()

    for arg in path_args:
        cand = Path(arg)
        if not cand.exists():
            print(f"  Warning: path does not exist, skipping: {arg}")
            continue

        # Case 0: arg is a .db file → SQLite database
        if cand.suffix.lower() in ('.db', '.sqlite', '.sqlite3'):
            # Rough size sanity: opencode dbs are >= 4KB
            if cand.stat().st_size >= 1024:
                k = cand.resolve().as_posix().lower()
                if k not in seen:
                    seen.add(k)
                    roots.append(('sqlite', cand))
            continue

        # Case 1: arg is already a storage root (contains storage/message)
        own_msg = cand / 'storage' / 'message'
        if own_msg.exists() and own_msg.is_dir():
            key = cand.resolve().as_posix().lower()
            if key not in seen:
                if any(c.name.startswith('ses_') for c in own_msg.iterdir() if c.is_dir()):
                    seen.add(key)
                    roots.append(('cli', cand))
                    continue

        # Case 2: arg contains opencode root directly (.local/share/opencode/storage/message)
        nested = cand / '.local' / 'share' / 'opencode'
        nested_msg = nested / 'storage' / 'message'
        if nested_msg.exists() and nested_msg.is_dir():
            key = nested.resolve().as_posix().lower()
            if key not in seen:
                if any(c.name.startswith('ses_') for c in nested_msg.iterdir() if c.is_dir()):
                    seen.add(key)
                    roots.append(('cli', nested))
                    continue

        # Case 2b: arg contains opencode.db SQLite directly
        nested_db = nested / 'opencode.db'
        if nested_db.exists() and nested_db.stat().st_size >= 1024:
            k = nested_db.resolve().as_posix().lower()
            if k not in seen:
                seen.add(k)
                roots.append(('sqlite', nested_db))

        # Case 2c: check opencode-local.db too
        nested_db2 = nested / 'opencode-local.db'
        if nested_db2.exists() and nested_db2.stat().st_size >= 1024:
            k = nested_db2.resolve().as_posix().lower()
            if k not in seen:
                seen.add(k)
                roots.append(('sqlite', nested_db2))

        # Case 3: recursive search for any 'storage/message' with sessions
        try:
            for hit in cand.rglob('storage'):
                msg = hit / 'message'
                if msg.exists() and msg.is_dir():
                    root = hit.parent
                    k = root.resolve().as_posix().lower()
                    if k not in seen:
                        if any(c.name.startswith('ses_') for c in msg.iterdir() if c.is_dir()):
                            seen.add(k)
                            roots.append(('cli', root))

            # Case 4: recursive search for opencode.db files
            for db in cand.rglob('opencode.db'):
                if db.stat().st_size >= 1024:
                    k = db.resolve().as_posix().lower()
                    if k not in seen:
                        seen.add(k)
                        roots.append(('sqlite', db))
            for db in cand.rglob('opencode-local.db'):
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
    print("="*80)
    print("OPENCODE EXTRACTION")
    print("="*80)
    print()

    # Parse CLI args. Accept flags:
    #   --no-auto          skip default-location auto-discovery
    #   --desktop <dir>    add a desktop (Tauri) install dir
    #   any other arg >=3 chars without a leading dash => cli path arg
    installations = []
    extra_roots = []

    arg_paths = []
    no_auto = False
    desktop_dir = None
    include_events = False
    raw_parts = False

    i = 1
    db_dirs = []
    while i < len(sys.argv):
        a = sys.argv[i]
        if a in ('-h', '--help'):
            print("Usage: extract_opencode.py [PATH...] [--no-auto] [--desktop DIR] [--db FILE]")
            print("  PATH        opencode storage root dir(s) or SQLite .db file(s).")
            print("              Storage roots contain 'storage/message' (legacy 1.1.x).")
            print("              SQLite .db files are opencode.db (1.2+).")
            print("              Directories are searched recursively for both.")
            print("  --no-auto   skip default-location auto-discovery.")
            print("  --desktop DIR   add a desktop (Tauri .dat) install dir.")
            print("  --db FILE   explicit path to opencode SQLite database.")
            print("  --events    ALSO extract the 'event' table (append-only change-log).")
            print("               Off by default: it is 94% of the db and largely")
            print("               redundant with message/part final state.")
            print("  --raw-parts ALSO preserve full raw part JSON (every field),")
            print("               instead of the curated field extraction.")
            return
        elif a == '--no-auto':
            no_auto = True
        elif a == '--desktop':
            i += 1
            if i < len(sys.argv):
                desktop_dir = sys.argv[i]
        elif a == '--db':
            i += 1
            if i < len(sys.argv):
                db_dirs.append(sys.argv[i])
        elif a == '--events':
            include_events = True
        elif a == '--raw-parts':
            raw_parts = True
        elif a.startswith('--') or a.startswith('-'):
            print(f"  Warning: unknown flag, ignoring: {a}")
        else:
            arg_paths.append(a)
        i += 1

    # Merge --db paths into path_args for discovery
    if db_dirs:
        arg_paths.extend(db_dirs)

    if arg_paths:
        print(f"Resolving {len(arg_paths)} path argument(s)...")
        extra_roots = discover_storage_roots_from_paths(arg_paths)
        print(f"  Discovered {len(extra_roots)} storage root(s) from path args")
        for t, d in extra_roots:
            print(f"    - {d}")

    # Always also auto-discover default installations (unless --no-auto)
    if no_auto:
        installations = []
    else:
        installations = find_opencode_installations()

    # Add explicit desktop dir if provided
    if desktop_dir:
        ddir = Path(desktop_dir)
        if ddir.exists():
            installations.append(('desktop', ddir))
        else:
            print(f"  Warning: desktop dir does not exist: {desktop_dir}")

    # Merge dedup by resolved path
    all_keys = set(d.resolve().as_posix().lower() for _, d in installations)
    for t, d in extra_roots:
        k = d.resolve().as_posix().lower()
        if k not in all_keys:
            all_keys.add(k)
            installations.append((t, d))

    if not installations:
        print("❌ No OpenCode installations found!")
        print()
        print("Searched locations:")
        print("  CLI defaults: ~/.local/share/opencode (Linux),")
        print("                ~/Library/Application Support/opencode (macOS),")
        print("                %APPDATA%/opencode (Windows)")
        print("  Pass explicit paths as args, e.g.:")
        print("    python extract_opencode.py B:/path/to/opencode/storage_root")
        return

    n_default = len(installations) - len(extra_roots)
    print(f"✅ Found {max(n_default,0)} default installation(s)")
    print(f"✅ Found {len(extra_roots)} storage root(s) from path args")
    print(f"✅ Total: {len(installations)} installation(s)")
    print()
    
    all_conversations = []
    
    for install_type, install_dir in installations:
        print(f"Processing {install_type} installation: {install_dir}")
        
        if install_type == 'cli':
            conversations = extract_cli_conversations(install_dir, raw_parts)
        elif install_type == 'sqlite':
            try:
                conversations = extract_sqlite_conversations(
                    install_dir, include_events, raw_parts
                )
            except sqlite3.OperationalError as e:
                print(f"  WARN: DB error — {e}")
                conversations = []
        else:  # desktop
            conversations = extract_desktop_conversations(install_dir)
        
        print(f"  Extracted {len(conversations)} conversations")
        all_conversations.extend(conversations)
        print()
    
    if not all_conversations:
        print("❌ No conversation data found!")
        return
    
    print(f"✅ Total conversations extracted: {len(all_conversations)}")
    
    # Calculate detailed statistics
    total_messages = sum(len(c['messages']) for c in all_conversations)
    with_tools = sum(1 for c in all_conversations 
                     if any('tool_calls' in m or 'tool_results' in m 
                           for m in c['messages']))
    with_models = sum(1 for c in all_conversations
                     if any('model' in m for m in c['messages']))
    with_reasoning = sum(1 for c in all_conversations
                        if any('reasoning' in m for m in c['messages']))
    
    # Count sessions with and without metadata
    with_session_file = sum(1 for c in all_conversations if c.get('directory'))
    without_session_file = len(all_conversations) - with_session_file
    
    print(f"Total messages: {total_messages}")
    print(f"With tool use: {with_tools}")
    print(f"With model info: {with_models}")
    print(f"With reasoning: {with_reasoning}")
    print(f"Full metadata (has session file): {with_session_file}")
    print(f"Reconstructed (no session file): {without_session_file}")
    print()
    
    # Save: one HF-traces JSONL file per session (one message per line).
    from traces_export import write_session_files
    n_files, n_lines = write_session_files(all_conversations, 'opencode')
    print(f"✅ Saved {n_files} session file(s) to extracted_data/opencode/sessions/")
    print(f"   Total message lines: {n_lines:,}")
    print(f"   Format: HF-traces JSONL (one message per line, one file per session)")

if __name__ == '__main__':
    main()
	