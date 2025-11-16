#!/usr/bin/env python3
"""
Extract ALL Droid CLI chat data from all sessions
Includes: messages, tool use, reasoning, context
Auto-discovers Droid session storage on the device
"""

import json
from pathlib import Path
from datetime import datetime
import platform

def find_droid_sessions():
    """Find all Droid session directories"""
    home = Path.home()
    
    # Droid stores sessions in ~/.factory/sessions
    droid_sessions_dir = home / ".factory" / "sessions"
    
    if not droid_sessions_dir.exists():
        return None
    
    return droid_sessions_dir

def extract_droid_sessions():
    """Extract all Droid sessions"""
    sessions_dir = find_droid_sessions()
    
    if not sessions_dir:
        return []
    
    conversations = []
    
    # Find all session JSONL files
    jsonl_files = list(sessions_dir.glob("*.jsonl"))
    
    for jsonl_file in jsonl_files:
        try:
            messages = []
            session_id = jsonl_file.stem
            session_title = None
            session_owner = None
            
            with open(jsonl_file, 'r') as f:
                for line in f:
                    if not line.strip():
                        continue
                    
                    try:
                        obj = json.loads(line)
                        msg_type = obj.get('type')
                        
                        if msg_type == 'session_start':
                            session_title = obj.get('title', 'New Session')
                            session_owner = obj.get('owner')
                        
                        elif msg_type == 'message':
                            message = obj.get('message', {})
                            role = message.get('role')
                            
                            if role not in ['user', 'assistant']:
                                continue
                            
                            # Extract content
                            content_parts = message.get('content', [])
                            if not isinstance(content_parts, list):
                                content_parts = [content_parts] if content_parts else []
                            
                            # Build message
                            msg = {
                                'role': role,
                                'content': '',
                                'timestamp': obj.get('timestamp')
                            }
                            
                            # Process content blocks
                            text_content = []
                            tool_uses = []
                            tool_results = []
                            thinking = None
                            
                            for content_item in content_parts:
                                if not isinstance(content_item, dict):
                                    continue
                                
                                content_type = content_item.get('type')
                                
                                if content_type == 'text':
                                    text_content.append(content_item.get('text', ''))
                                
                                elif content_type == 'thinking':
                                    thinking = content_item.get('thinking')
                                
                                elif content_type == 'tool_use':
                                    tool_uses.append({
                                        'id': content_item.get('id'),
                                        'name': content_item.get('name'),
                                        'input': content_item.get('input')
                                    })
                                
                                elif content_type == 'tool_result':
                                    tool_results.append({
                                        'tool_use_id': content_item.get('tool_use_id'),
                                        'content': content_item.get('content')
                                    })
                            
                            # Combine text content
                            msg['content'] = '\n'.join(text_content)
                            
                            if thinking:
                                msg['thinking'] = thinking
                            
                            if tool_uses:
                                msg['tool_uses'] = tool_uses
                            
                            if tool_results:
                                msg['tool_results'] = tool_results
                            
                            messages.append(msg)
                        
                        elif msg_type == 'todo_state':
                            # Capture todo state if present
                            if messages and 'todos' not in messages[-1]:
                                messages[-1]['todos'] = obj.get('todos')
                    
                    except json.JSONDecodeError:
                        continue
            
            if messages:
                conversations.append({
                    'messages': messages,
                    'source': 'droid',
                    'session_id': session_id,
                    'session_title': session_title,
                    'session_owner': session_owner,
                    'source_file': str(jsonl_file)
                })
        
        except Exception as e:
            print(f"Error processing {jsonl_file}: {e}")
            continue
    
    return conversations

def main():
    print("="*80)
    print("DROID CLI SESSION EXTRACTION")
    print("="*80)
    print()
    
    print("🔍 Searching for Droid sessions...")
    sessions_dir = find_droid_sessions()
    
    if not sessions_dir:
        print("❌ No Droid sessions directory found!")
        print(f"   Expected: {Path.home() / '.factory' / 'sessions'}")
        return
    
    print(f"✅ Found sessions directory: {sessions_dir}")
    print()
    
    conversations = extract_droid_sessions()
    
    if not conversations:
        print("❌ No Droid sessions found!")
        return
    
    print(f"✅ Found {len(conversations)} session(s)")
    print()
    
    # Statistics
    total_messages = sum(len(c['messages']) for c in conversations)
    user_messages = sum(1 for c in conversations for m in c['messages'] if m['role'] == 'user')
    assistant_messages = sum(1 for c in conversations for m in c['messages'] if m['role'] == 'assistant')
    with_tools = sum(1 for c in conversations
                     if any('tool_uses' in m or 'tool_results' in m
                           for m in c['messages']))
    with_thinking = sum(1 for c in conversations
                       if any('thinking' in m for m in c['messages']))
    
    print(f"Total messages: {total_messages:,}")
    print(f"User messages: {user_messages:,}")
    print(f"Assistant messages: {assistant_messages:,}")
    print(f"With tool use/results: {with_tools:,}")
    print(f"With reasoning/thinking: {with_thinking:,}")
    print()
    
    # Save
    output_dir = Path('extracted_data')
    output_dir.mkdir(exist_ok=True)
    
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_file = output_dir / f'droid_conversations_{timestamp}.jsonl'
    
    with open(output_file, 'w') as f:
        for conv in conversations:
            f.write(json.dumps(conv, ensure_ascii=False) + '\n')
    
    file_size = output_file.stat().st_size / 1024 / 1024
    print(f"✅ Saved to: {output_file}")
    print(f"   Size: {file_size:.2f} MB")
    print(f"   Format: JSONL (one session per line)")

if __name__ == '__main__':
    main()
