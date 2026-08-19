#!/usr/bin/env python3
"""
Extract ALL Continue AI assistant data
Includes: messages, tool calls, reasoning, context
"""

import json
from pathlib import Path

def extract_continue_sessions():
    """Extract all Continue sessions"""
    continue_dir = Path.home() / ".continue/sessions"

    if not continue_dir.exists():
        return []

    conversations = []

    for session_file in continue_dir.glob("*.json"):
        if session_file.name == "sessions.json":
            continue

        try:
            with open(session_file) as f:
                data = json.loads(f.read())

            if 'history' not in data:
                continue

            messages = []
            for item in data['history']:
                if 'message' not in item:
                    continue

                msg = item['message']
                role = msg.get('role')

                # Extract content
                content_parts = msg.get('content', [])
                if isinstance(content_parts, str):
                    content = content_parts
                elif isinstance(content_parts, list):
                    content = '\n'.join([
                        c.get('text', '') for c in content_parts
                        if isinstance(c, dict) and c.get('type') == 'text'
                    ])
                else:
                    content = ''

                message = {
                    'role': role,
                    'content': content
                }

                # Add tool calls for assistant messages
                if 'toolCalls' in msg:
                    message['tool_calls'] = msg['toolCalls']

                # Add reasoning
                if 'reasoning' in item and item['reasoning']:
                    message['reasoning'] = item['reasoning'].get('text', '')

                # Add context items
                if 'contextItems' in item and item['contextItems']:
                    message['context_items'] = item['contextItems']

                # Add tool results
                if 'toolCallStates' in item:
                    tool_results = []
                    for state in item['toolCallStates']:
                        if state.get('status') == 'done' and 'output' in state:
                            tool_results.append({
                                'tool': state.get('tool', {}).get('function', {}).get('name'),
                                'output': state['output']
                            })
                    if tool_results:
                        message['tool_results'] = tool_results

                messages.append(message)

            if messages:
                conversations.append({
                    'messages': messages,
                    'source': 'continue',
                    'session_id': data.get('sessionId'),
                    'title': data.get('title'),
                    'workspace': data.get('workspaceDirectory')
                })

        except Exception as e:
            print(f"Error processing {session_file}: {e}")
            continue

    return conversations

def main():
    print("="*80)
    print("CONTINUE AI ASSISTANT EXTRACTION")
    print("="*80)
    print()

    conversations = extract_continue_sessions()

    if not conversations:
        print("❌ No Continue sessions found!")
        return

    print(f"✅ Found {len(conversations)} conversations")

    total_messages = sum(len(c['messages']) for c in conversations)
    with_tools = sum(1 for c in conversations
                     if any('tool_calls' in m or 'tool_results' in m
                           for m in c['messages']))
    with_reasoning = sum(1 for c in conversations
                        if any('reasoning' in m for m in c['messages']))

    print(f"Total messages: {total_messages}")
    print(f"With tool use: {with_tools}")
    print(f"With reasoning: {with_reasoning}")
    print()

    # Save: one HF-traces JSONL file per session (one message per line)
    from traces_export import write_session_files
    n_files, n_lines = write_session_files(conversations, 'continue')
    print(f"✅ Saved {n_files} session file(s) to extracted_data/continue/sessions/")
    print(f"   Total message lines: {n_lines:,}")
    print(f"   Format: HF-traces JSONL (one message per line, one file per session)")

if __name__ == '__main__':
    main()
