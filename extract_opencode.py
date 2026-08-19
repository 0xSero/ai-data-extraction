#!/usr/bin/env python3
"""
Extract ALL OpenCode conversation data
Supports: CLI (SQLite database) and legacy JSON storage, plus Desktop (Tauri .dat files)

Storage locations:
- CLI (new): ~/.local/share/opencode/opencode.db (SQLite)
- CLI (legacy): ~/.local/share/opencode/storage/ (JSON files)
- Desktop: Platform-specific Tauri app data directories

Features:
- Extracts conversations from SQLite database (v1.2.0+)
- Falls back to legacy JSON storage for older versions
- Assembles complete messages from message metadata + parts
- Handles all message part types (text, tool, reasoning, etc.)
"""

import json
import struct
import sqlite3
from pathlib import Path
from datetime import datetime
import platform
import os
from collections import defaultdict

def find_opencode_installations():
    """Find all OpenCode installation directories"""
    system = platform.system()
    home = Path.home()
    
    locations = []
    
    # CLI storage locations (XDG Base Directory)
    if system == "Darwin":  # macOS
        cli_dirs = [
            home / "Library/Application Support/opencode",
            Path(os.environ.get('XDG_DATA_HOME', home / '.local/share')) / 'opencode'
        ]
    elif system == "Linux":
        cli_dirs = [
            Path(os.environ.get('XDG_DATA_HOME', home / '.local/share')) / 'opencode'
        ]
    elif system == "Windows":
        cli_dirs = [
            Path(os.environ.get('APPDATA', home / 'AppData/Roaming')) / 'opencode'
        ]
    else:
        cli_dirs = [home / '.local/share/opencode']
    
    for cli_dir in cli_dirs:
        if cli_dir.exists():
            locations.append(('cli', cli_dir))
    
    # Desktop storage locations (Tauri app data)
    if system == "Darwin":  # macOS
        desktop_dirs = [
            home / "Library/Application Support/ai.opencode.app"
        ]
    elif system == "Linux":
        desktop_dirs = [
            home / ".local/share/ai.opencode.app"
        ]
    elif system == "Windows":
        desktop_dirs = [
            Path(os.environ.get('APPDATA', home / 'AppData/Roaming')) / 'ai.opencode.app'
        ]
    else:
        desktop_dirs = []
    
    for desktop_dir in desktop_dirs:
        if desktop_dir.exists():
            locations.append(('desktop', desktop_dir))
    
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

def extract_from_sqlite(db_path):
    """
    Extract conversations from SQLite database (OpenCode v1.2.0+)
    """
    conversations = []
    
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Get all sessions
        cursor.execute("""
            SELECT s.*, p.name as project_name, p.worktree as project_worktree
            FROM session s
            LEFT JOIN project p ON s.project_id = p.id
            ORDER BY s.time_created
        """)
        sessions = cursor.fetchall()
        
        print(f"  Found {len(sessions)} sessions in database")
        
        for session in sessions:
            session_id = session['id']
            
            # Get all messages for this session
            cursor.execute("""
                SELECT * FROM message 
                WHERE session_id = ? 
                ORDER BY time_created, id
            """, (session_id,))
            messages_rows = cursor.fetchall()
            
            if not messages_rows:
                continue
            
            messages = []
            
            for msg_row in messages_rows:
                message_id = msg_row['id']
                msg_data = json.loads(msg_row['data'])
                
                # Build message
                message = {
                    'role': msg_data.get('role', 'assistant'),
                    'content': '',
                    'timestamp': msg_row['time_created'],
                }
                
                # Add metadata from message data
                if 'modelID' in msg_data:
                    message['model'] = msg_data['modelID']
                if 'providerID' in msg_data:
                    message['provider'] = msg_data['providerID']
                if 'agent' in msg_data:
                    message['agent'] = msg_data['agent']
                if 'mode' in msg_data:
                    message['mode'] = msg_data['mode']
                if 'cost' in msg_data:
                    message['cost'] = msg_data['cost']
                if 'tokens' in msg_data:
                    message['tokens'] = msg_data['tokens']
                if 'finish' in msg_data:
                    message['finish_reason'] = msg_data['finish']
                if 'parentID' in msg_data:
                    message['parent_id'] = msg_data['parentID']
                
                # Get all parts for this message
                cursor.execute("""
                    SELECT * FROM part 
                    WHERE message_id = ? 
                    ORDER BY time_created, id
                """, (message_id,))
                part_rows = cursor.fetchall()
                
                content_parts = []
                tool_calls = []
                tool_results = []
                reasoning_parts = []
                
                for part_row in part_rows:
                    part_data = json.loads(part_row['data'])
                    part_type = part_data.get('type', '')
                    
                    if part_type == 'text':
                        text = part_data.get('text', '')
                        if text:
                            content_parts.append(text)
                    
                    elif part_type == 'tool':
                        tool_call = {
                            'id': part_data.get('callID'),
                            'name': part_data.get('tool'),
                            'input': part_data.get('state', {}).get('input'),
                        }
                        tool_calls.append(tool_call)
                        
                        # If completed, also add to tool_results
                        state = part_data.get('state', {})
                        if state.get('status') == 'completed' and 'output' in state:
                            tool_results.append({
                                'tool_call_id': part_data.get('callID'),
                                'tool': part_data.get('tool'),
                                'output': state['output']
                            })
                    
                    elif part_type == 'tool-result':
                        tool_results.append({
                            'tool_call_id': part_data.get('toolCallID'),
                            'output': part_data.get('output')
                        })
                    
                    elif part_type == 'reasoning':
                        reasoning_text = part_data.get('text', '')
                        if reasoning_text:
                            reasoning_parts.append(reasoning_text)
                    
                    elif part_type == 'patch':
                        # Code/diff patches
                        old_str = part_data.get('oldString', '')
                        new_str = part_data.get('newString', '')
                        file_path = part_data.get('filePath', '')
                        if file_path:
                            content_parts.append(f"```diff\n# {file_path}\n-{old_str}\n+{new_str}\n```")
                    
                    elif part_type == 'step-finish':
                        # Step finish metadata - can include cost info
                        if 'cost' in part_data and part_data['cost']:
                            if 'cost' not in message:
                                message['cost'] = part_data['cost']
                        if 'tokens' in part_data and part_data['tokens']:
                            if 'tokens' not in message:
                                message['tokens'] = part_data['tokens']
                
                message['content'] = '\n'.join(content_parts)
                
                if tool_calls:
                    message['tool_calls'] = tool_calls
                if tool_results:
                    message['tool_results'] = tool_results
                if reasoning_parts:
                    message['reasoning'] = '\n'.join(reasoning_parts)
                
                messages.append(message)
            
            if not messages:
                continue
            
            # Build conversation
            conversation = {
                'messages': messages,
                'source': 'opencode-cli-sqlite',
                'session_id': session_id,
                'title': session['title'],
                'directory': session['directory'],
                'created_at': session['time_created'],
                'updated_at': session['time_updated'],
                'version': session['version'],
                'slug': session['slug'],
            }
            
            # Add optional fields if present
            if session['project_id']:
                conversation['project_id'] = session['project_id']
                if session['project_name']:
                    conversation['project_name'] = session['project_name']
                if session['project_worktree']:
                    conversation['project_worktree'] = session['project_worktree']
            
            if session['parent_id']:
                conversation['parent_session_id'] = session['parent_id']
            
            if session['workspace_id']:
                conversation['workspace_id'] = session['workspace_id']
            
            if session['share_url']:
                conversation['share_url'] = session['share_url']
            
            # Add summary stats if available
            if session['summary_additions'] is not None:
                conversation['summary_additions'] = session['summary_additions']
            if session['summary_deletions'] is not None:
                conversation['summary_deletions'] = session['summary_deletions']
            if session['summary_files'] is not None:
                conversation['summary_files'] = session['summary_files']
            if session['summary_diffs']:
                try:
                    conversation['summary_diffs'] = json.loads(session['summary_diffs'])
                except:
                    pass
            
            # Add revert info if present
            if session['revert']:
                try:
                    conversation['revert'] = json.loads(session['revert'])
                except:
                    pass
            
            # Add permission rules if present
            if session['permission']:
                try:
                    conversation['permission'] = json.loads(session['permission'])
                except:
                    pass
            
            # Check for todos
            cursor.execute("""
                SELECT * FROM todo 
                WHERE session_id = ? 
                ORDER BY position
            """, (session_id,))
            todo_rows = cursor.fetchall()
            if todo_rows:
                todos = []
                for todo_row in todo_rows:
                    todos.append({
                        'content': todo_row['content'],
                        'status': todo_row['status'],
                        'priority': todo_row['priority'],
                        'position': todo_row['position'],
                        'created': todo_row['time_created'],
                        'updated': todo_row['time_updated'],
                    })
                conversation['todos'] = todos
            
            conversations.append(conversation)
        
        conn.close()
        
    except Exception as e:
        print(f"  Error reading SQLite database: {e}")
        import traceback
        traceback.print_exc()
    
    return conversations

def extract_directory_from_content(text):
    """
    Try to extract a directory path from text content (e.g., tool commands).
    Looks for common patterns like 'cd /path/to/dir' or paths in commands.
    """
    if not text:
        return None
    
    import re
    
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
    
    import re
    
    # Pattern: project IDs in commands
    project_pattern = r'(?:project[-_]?id|project)[=:\s]+([a-zA-Z0-9_-]+)'
    match = re.search(project_pattern, text, re.IGNORECASE)
    if match:
        return match.group(1)
    
    return None

def extract_cli_conversations(storage_dir):
    """
    Extract conversations from legacy CLI JSON storage.
    For OpenCode versions < 1.2.0
    """
    conversations = []
    
    message_dir = storage_dir / 'storage' / 'message'
    part_dir = storage_dir / 'storage' / 'part'
    
    if not message_dir.exists():
        print(f"  Legacy message directory not found: {message_dir}")
        return conversations
    
    # Find all session directories (each is a directory named ses_xxx)
    session_dirs = [d for d in message_dir.iterdir() if d.is_dir() and d.name.startswith('ses_')]
    
    print(f"  Found {len(session_dirs)} legacy session directories")
    
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
                    
                    message_id = msg_data.get('id')
                    role = msg_data.get('role', 'assistant')
                    msg_time = msg_data.get('time', {}).get('created')
                    
                    # Track timestamps
                    if msg_time:
                        if not first_message_time or msg_time < first_message_time:
                            first_message_time = msg_time
                        if not last_message_time or msg_time > last_message_time:
                            last_message_time = msg_time
                    
                    # Build the message
                    message = {
                        'role': role,
                        'content': '',
                        'timestamp': msg_time
                    }
                    
                    # Add metadata
                    if 'modelID' in msg_data:
                        message['model'] = msg_data['modelID']
                    if 'providerID' in msg_data:
                        message['provider'] = msg_data['providerID']
                    if 'agent' in msg_data:
                        message['agent'] = msg_data['agent']
                    if 'mode' in msg_data:
                        message['mode'] = msg_data['mode']
                    
                    # Add token usage
                    if 'tokens' in msg_data:
                        message['tokens'] = msg_data['tokens']
                    if 'cost' in msg_data:
                        message['cost'] = msg_data['cost']
                    
                    # Find all parts for this message
                    message_part_dir = part_dir / message_id
                    
                    if message_part_dir.exists():
                        part_files = sorted(message_part_dir.glob('prt_*.json'))
                        content_parts = []
                        tool_calls = []
                        tool_results = []
                        reasoning_parts = []
                        
                        for part_file in part_files:
                            try:
                                with open(part_file) as f:
                                    part_data = json.load(f)
                                
                                part_type = part_data.get('type')
                                part_text = part_data.get('text', '')
                                
                                # Collect content for metadata reconstruction
                                if part_text:
                                    all_content.append(part_text)
                                
                                if part_type == 'text':
                                    content_parts.append(part_text)
                                elif part_type == 'tool' or part_type == 'tool-call':
                                    # OpenCode uses 'tool' type with state containing input/output
                                    state = part_data.get('state', {})
                                    tool_name = part_data.get('tool', part_data.get('name'))
                                    
                                    tool_call = {
                                        'id': part_data.get('callID', part_data.get('id')),
                                        'name': tool_name,
                                        'input': state.get('input', part_data.get('input'))
                                    }
                                    
                                    # If completed, also add to tool_results
                                    if state.get('status') == 'completed' and 'output' in state:
                                        tool_results.append({
                                            'tool_call_id': part_data.get('callID'),
                                            'tool': tool_name,
                                            'output': state['output']
                                        })
                                    
                                    tool_calls.append(tool_call)
                                elif part_type == 'tool-result':
                                    tool_results.append({
                                        'tool_call_id': part_data.get('toolCallID'),
                                        'output': part_data.get('output')
                                    })
                                elif part_type == 'code':
                                    # Code blocks
                                    code_text = part_data.get('text', '')
                                    language = part_data.get('language', '')
                                    content_parts.append(f"```{language}\n{code_text}\n```")
                                elif part_type == 'reasoning':
                                    # Reasoning/thinking content
                                    reasoning_text = part_data.get('text', '')
                                    if reasoning_text:
                                        reasoning_parts.append(reasoning_text)
                                
                            except Exception as e:
                                print(f"    Error reading part {part_file}: {e}")
                                continue
                        
                        message['content'] = '\n'.join(content_parts)
                        
                        if tool_calls:
                            message['tool_calls'] = tool_calls
                        if tool_results:
                            message['tool_results'] = tool_results
                        if reasoning_parts:
                            message['reasoning'] = '\n'.join(reasoning_parts)
                    
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
                'source': 'opencode-cli-legacy',
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
                
                # Add summary stats if available
                if 'summary' in session_data:
                    conversation['summary'] = session_data['summary']
                
                # Add parent session if it's a child session
                if 'parentID' in session_data:
                    conversation['parent_session_id'] = session_data['parentID']
            else:
                # RECONSTRUCT metadata from messages/parts
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

def extract_desktop_conversations(desktop_dir):
    """Extract conversations from Desktop Tauri store files"""
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

def main():
    print("="*80)
    print("OPENCODE EXTRACTION")
    print("="*80)
    print()
    
    installations = find_opencode_installations()
    
    if not installations:
        print("❌ No OpenCode installations found!")
        print()
        print("Searched locations:")
        print("  CLI: ~/.local/share/opencode (Linux)")
        print("       ~/Library/Application Support/opencode (macOS)")
        print("  Desktop: ~/.local/share/ai.opencode.app (Linux)")
        print("           ~/Library/Application Support/ai.opencode.app (macOS)")
        return
    
    print(f"✅ Found {len(installations)} installation(s)")
    print()
    
    all_conversations = []
    
    for install_type, install_dir in installations:
        print(f"Processing {install_type} installation: {install_dir}")
        
        if install_type == 'cli':
            # Check for SQLite database first (new format)
            db_path = install_dir / 'opencode.db'
            if db_path.exists():
                print(f"  📁 Found SQLite database: {db_path}")
                conversations = extract_from_sqlite(db_path)
                if conversations:
                    print(f"  ✅ Extracted {len(conversations)} conversations from SQLite")
                    all_conversations.extend(conversations)
                else:
                    print(f"  ⚠️  No conversations found in SQLite database")
            
            # Also check for legacy JSON storage
            legacy_storage = install_dir / 'storage' / 'message'
            if legacy_storage.exists():
                print(f"  📁 Found legacy JSON storage")
                conversations = extract_cli_conversations(install_dir)
                if conversations:
                    print(f"  ✅ Extracted {len(conversations)} conversations from legacy storage")
                    all_conversations.extend(conversations)
                else:
                    print(f"  ⚠️  No conversations found in legacy storage")
            
            if not db_path.exists() and not legacy_storage.exists():
                print(f"  ❌ No storage found (neither SQLite nor legacy JSON)")
                
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
    
    # Count by source
    from_sqlite = sum(1 for c in all_conversations if c.get('source') == 'opencode-cli-sqlite')
    from_legacy = sum(1 for c in all_conversations if c.get('source') == 'opencode-cli-legacy')
    from_desktop = sum(1 for c in all_conversations if c.get('source') == 'opencode-desktop')
    
    print(f"Total messages: {total_messages}")
    print(f"With tool use: {with_tools}")
    print(f"With model info: {with_models}")
    print(f"With reasoning: {with_reasoning}")
    print()
    print(f"Sources:")
    if from_sqlite:
        print(f"  📊 SQLite (v1.2.0+): {from_sqlite}")
    if from_legacy:
        print(f"  📄 Legacy JSON (<v1.2.0): {from_legacy}")
    if from_desktop:
        print(f"  🖥️  Desktop app: {from_desktop}")
    print()
    
    # Save
    output_dir = Path('extracted_data')
    output_dir.mkdir(exist_ok=True)
    
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_file = output_dir / f'opencode_conversations_{timestamp}.jsonl'
    
    with open(output_file, 'w') as f:
        for conv in all_conversations:
            f.write(json.dumps(conv, ensure_ascii=False) + '\n')
    
    file_size = output_file.stat().st_size / 1024
    print(f"✅ Saved to: {output_file}")
    print(f"   Size: {file_size:.2f} KB")

if __name__ == '__main__':
    main()
