#!/usr/bin/env python3
"""
ULTIMATE Cursor extraction - EVERY VERSION, EVERY FORMAT:
1. Chat mode (workspace ItemTable - workbench.panel.aichat)
2. aiService (OLD format - pre-v0.43 prompts/generations)
3. Workspace composers (composer.composerData in workspace ItemTable)
4. Global composers inline (composerData.conversation[] in global cursorDiskKV)
5. Global composers separate (bubbleId:{composer}:{bubble} in global cursorDiskKV)

This gets EVERYTHING from v0.2 through v2.0+

LIMITATIONS:
- aiService (old format): Assistant responses are not persisted, only summaries
- Model information: Only available for conversations with modelConfig stored
  in composerData. Older conversations may not have model information.
"""

import json
import sqlite3
from pathlib import Path
import platform
import os
from collections import defaultdict

def find_cursor_installations():
    """Find all Cursor installation directories"""
    system = platform.system()
    home = Path.home()

    locations = []

    if system == "Darwin":  # macOS
        base_dirs = [
            home / "Library/Application Support",
            home / ".config"
        ]
    elif system == "Linux":
        base_dirs = [
            home / ".config",
            home / ".local/share"
        ]
    elif system == "Windows":
        base_dirs = [
            Path(os.environ.get('APPDATA', home / 'AppData/Roaming')),
            Path(os.environ.get('LOCALAPPDATA', home / 'AppData/Local'))
        ]
    else:
        base_dirs = [home / ".config"]

    for base_dir in base_dirs:
        if not base_dir.exists():
            continue

        cursor_dir = base_dir / 'Cursor'
        if cursor_dir.exists():
            locations.append(cursor_dir)

    return list(set(locations))

def extract_aiservice_conversations(db_path, workspace_id):
    """Extract OLD Cursor format (pre-v0.43) aiService prompts and generations"""
    conversations = []

    try:
        conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
        cursor = conn.cursor()

        # Get prompts
        cursor.execute("SELECT value FROM ItemTable WHERE key = 'aiService.prompts'")
        prompts_result = cursor.fetchone()

        # Get generations
        cursor.execute("SELECT value FROM ItemTable WHERE key = 'aiService.generations'")
        gens_result = cursor.fetchone()

        if prompts_result or gens_result:
            prompts = json.loads(prompts_result[0]) if prompts_result else []
            generations = json.loads(gens_result[0]) if gens_result else []

            # Pair prompts with generations
            max_len = max(len(prompts), len(generations))

            for i in range(max_len):
                messages = []

                if i < len(prompts):
                    prompt = prompts[i]
                    messages.append({
                        'role': 'user',
                        'content': prompt.get('text', ''),
                        'command_type': prompt.get('commandType')
                    })

                if i < len(generations):
                    gen = generations[i]
                    msg = {
                        'role': 'assistant',
                        'content': gen.get('text', gen.get('message', '')),
                    }
                    # Extract model if available
                    if 'model' in gen:
                        msg['model'] = gen['model']
                    elif 'modelId' in gen:
                        msg['model'] = gen['modelId']
                    
                    messages.append(msg)

                if messages:
                    # Try to get model from generation data
                    model = None
                    if i < len(generations):
                        gen = generations[i]
                        if isinstance(gen, dict):
                            model = gen.get('model') or gen.get('modelId') or gen.get('modelName')
                    
                    # Add model to assistant messages
                    for msg in messages:
                        if isinstance(msg, dict) and msg.get('role') == 'assistant':
                            msg['model'] = model
                    
                    conv = {
                        'messages': messages,
                        'source': 'cursor-aiservice',
                        'workspace_id': workspace_id
                    }
                    if model:
                        conv['model'] = model
                    
                    conversations.append(conv)

        conn.close()
    except Exception as e:
        pass

    return conversations

def extract_workspace_composers(db_path, workspace_id):
    """Extract workspace-specific composer data (pre-migration to global storage)"""
    conversations = []

    try:
        conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
        cursor = conn.cursor()

        cursor.execute("SELECT value FROM ItemTable WHERE key = 'composer.composerData'")
        result = cursor.fetchone()

        if result:
            data = json.loads(result[0])

            if isinstance(data, dict) and 'allComposers' in data:
                all_composers = data['allComposers']

                if isinstance(all_composers, list):
                    for composer_data in all_composers:
                        if not isinstance(composer_data, dict):
                            continue

                        messages = []
                        code_contexts = []
                        diffs = []

                        conversation = composer_data.get('conversation', [])

                        for bubble in conversation:
                            bubble_type = bubble.get('type')
                            text = bubble.get('text', '')

                            if bubble_type == 1:  # User
                                msg = {
                                    'role': 'user',
                                    'content': text
                                }

                                context = bubble.get('context', {})
                                if context and 'selections' in context:
                                    ctx = []
                                    for sel in context['selections']:
                                        if 'uri' in sel and 'fsPath' in sel['uri']:
                                            ctx.append({
                                                'file': sel['uri']['fsPath'],
                                                'code': sel.get('text', sel.get('rawText', '')),
                                                'range': sel.get('range')
                                            })
                                    if ctx:
                                        msg['code_context'] = ctx
                                        code_contexts.extend(ctx)

                                messages.append(msg)

                            elif bubble_type == 2:  # AI
                                msg = {
                                    'role': 'assistant',
                                    'content': text
                                }

                                # Extract model name if available
                                if 'modelId' in bubble:
                                    msg['model'] = bubble['modelId']
                                elif 'model' in bubble:
                                    msg['model'] = bubble['model']
                                elif 'modelName' in bubble:
                                    msg['model'] = bubble['modelName']

                                if 'codeBlocks' in bubble and bubble['codeBlocks']:
                                    msg['code_blocks'] = bubble['codeBlocks']

                                if 'suggestedCodeBlocks' in bubble and bubble['suggestedCodeBlocks']:
                                    msg['suggested_code_blocks'] = bubble['suggestedCodeBlocks']
                                    diffs.extend(bubble['suggestedCodeBlocks'])

                                if 'diffHistories' in bubble and bubble['diffHistories']:
                                    msg['diff_histories'] = bubble['diffHistories']
                                    diffs.extend(bubble['diffHistories'])

                                messages.append(msg)

                        if messages:
                            # Get model from modelConfig
                            model = composer_data.get('modelConfig', {}).get('modelName')
                            
                            # Add model to all assistant messages
                            for msg in messages:
                                if isinstance(msg, dict) and msg.get('role') == 'assistant':
                                    msg['model'] = model
                            
                            conversations.append({
                                'messages': messages,
                                'source': 'cursor-workspace-composer',
                                'composer_id': composer_data.get('composerId'),
                                'name': composer_data.get('name', 'Untitled'),
                                'workspace_id': workspace_id,
                                'model': model,
                                'has_code_context': len(code_contexts) > 0,
                                'has_diffs': len(diffs) > 0
                            })

        conn.close()
    except Exception as e:
        pass

    return conversations

def extract_chat_mode(db_path, workspace_id):
    """Extract Chat mode conversations"""
    conversations = []

    try:
        conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM ItemTable WHERE key = 'workbench.panel.aichat.view.aichat.chatdata'")
        result = cursor.fetchone()

        if result:
            data = json.loads(result[0])
            if 'tabs' in data:
                for tab in data['tabs']:
                    if 'bubbles' in tab and len(tab['bubbles']) > 0:
                        messages = []
                        code_context = []
                        suggested_diffs = []

                        for bubble in tab['bubbles']:
                            bubble_type = bubble.get('type')
                            content = bubble.get('rawText', bubble.get('text', ''))

                            msg = {
                                'role': 'user' if bubble_type == 'user' else 'assistant',
                                'content': content
                            }

                            if 'selections' in bubble and bubble['selections']:
                                ctx = []
                                for sel in bubble['selections']:
                                    if 'uri' in sel and 'fsPath' in sel['uri']:
                                        ctx.append({
                                            'file': sel['uri']['fsPath'],
                                            'code': sel.get('text', sel.get('rawText', '')),
                                            'range': sel.get('range')
                                        })
                                if ctx:
                                    msg['code_context'] = ctx
                                    code_context.extend(ctx)

                            if 'suggestedDiffs' in bubble and bubble['suggestedDiffs']:
                                msg['suggested_diffs'] = bubble['suggestedDiffs']
                                suggested_diffs.extend(bubble['suggestedDiffs'])

                            messages.append(msg)

                        if messages:
                            conversations.append({
                                'messages': messages,
                                'source': 'cursor-chat',
                                'chat_title': tab.get('chatTitle'),
                                'tab_id': tab.get('tabId'),
                                'workspace_id': workspace_id,
                                'has_code_context': len(code_context) > 0,
                                'has_diffs': len(suggested_diffs) > 0
                            })

        conn.close()
    except Exception as e:
        pass

    return conversations

def extract_bubbles_for_composer(cursor, composer_id):
    """Extract separate bubble storage for a composer"""
    bubbles = []

    try:
        cursor.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE ?",
            (f'bubbleId:{composer_id}:%',)
        )

        for key, value in cursor.fetchall():
            if not value:
                continue

            try:
                bubble_data = json.loads(value)
                bubble_type = bubble_data.get('type')
                text = bubble_data.get('text', '')

                msg = {
                    'role': 'user' if bubble_type == 1 else 'assistant',
                    'content': text,
                    'bubble_id': key.split(':')[2]
                }

                if bubble_type == 1:
                    if 'selections' in bubble_data:
                        ctx = []
                        for sel in bubble_data.get('selections', []):
                            if 'uri' in sel and 'fsPath' in sel.get('uri', {}):
                                ctx.append({
                                    'file': sel['uri']['fsPath'],
                                    'code': sel.get('text', sel.get('rawText', '')),
                                    'range': sel.get('range')
                                })
                        if ctx:
                            msg['code_context'] = ctx

                elif bubble_type == 2:
                    msg = {
                        'role': 'assistant',
                        'content': text,
                        'bubble_id': key.split(':')[2]
                    }

                    # Extract model name if available
                    if 'modelId' in bubble_data:
                        msg['model'] = bubble_data['modelId']
                    elif 'model' in bubble_data:
                        msg['model'] = bubble_data['model']
                    elif 'modelName' in bubble_data:
                        msg['model'] = bubble_data['modelName']

                    if 'codeBlocks' in bubble_data and bubble_data['codeBlocks']:
                        msg['code_blocks'] = bubble_data['codeBlocks']

                    if 'suggestedCodeBlocks' in bubble_data and bubble_data['suggestedCodeBlocks']:
                        msg['suggested_code_blocks'] = bubble_data['suggestedCodeBlocks']

                    if 'diffHistories' in bubble_data and bubble_data['diffHistories']:
                        msg['diff_histories'] = bubble_data['diffHistories']

                    if 'toolResults' in bubble_data and bubble_data['toolResults']:
                        msg['tool_results'] = bubble_data['toolResults']

                bubbles.append(msg)

            except json.JSONDecodeError:
                continue

    except Exception as e:
        pass

    return bubbles

def extract_global_composers(global_db_path):
    """Extract global composer data (both inline and separate storage)"""
    conversations = []

    try:
        conn = sqlite3.connect(f'file:{global_db_path}?mode=ro', uri=True)
        cursor = conn.cursor()

        cursor.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'")
        results = cursor.fetchall()

        for key, value in results:
            if not value:
                continue

            try:
                data = json.loads(value)
                composer_id = data.get('composerId', key.split(':')[1])

                messages = []
                code_contexts = []
                diffs = []

                inline_conversation = data.get('conversation', [])

                if inline_conversation and len(inline_conversation) > 0:
                    # INLINE STORAGE
                    for bubble in inline_conversation:
                        bubble_type = bubble.get('type')
                        text = bubble.get('text', '')

                        if bubble_type == 1:
                            msg = {
                                'role': 'user',
                                'content': text
                            }

                            context = bubble.get('context', {})
                            if context and 'selections' in context:
                                ctx = []
                                for sel in context['selections']:
                                    if 'uri' in sel and 'fsPath' in sel['uri']:
                                        ctx.append({
                                            'file': sel['uri']['fsPath'],
                                            'code': sel.get('text', sel.get('rawText', '')),
                                            'range': sel.get('range')
                                        })
                                if ctx:
                                    msg['code_context'] = ctx
                                    code_contexts.extend(ctx)

                            messages.append(msg)

                        elif bubble_type == 2:
                            msg = {
                                'role': 'assistant',
                                'content': text
                            }

                            # Extract model name if available
                            if 'modelId' in bubble:
                                msg['model'] = bubble['modelId']
                            elif 'model' in bubble:
                                msg['model'] = bubble['model']
                            elif 'modelName' in bubble:
                                msg['model'] = bubble['modelName']

                            if 'codeBlocks' in bubble and bubble['codeBlocks']:
                                msg['code_blocks'] = bubble['codeBlocks']

                            if 'suggestedCodeBlocks' in bubble and bubble['suggestedCodeBlocks']:
                                msg['suggested_code_blocks'] = bubble['suggestedCodeBlocks']
                                diffs.extend(bubble['suggestedCodeBlocks'])

                            if 'diffHistories' in bubble and bubble['diffHistories']:
                                msg['diff_histories'] = bubble['diffHistories']
                                diffs.extend(bubble['diffHistories'])

                            messages.append(msg)
                else:
                    # SEPARATE STORAGE
                    messages = extract_bubbles_for_composer(cursor, composer_id)

                    for msg in messages:
                        if 'code_context' in msg:
                            code_contexts.extend(msg['code_context'])
                        if 'suggested_code_blocks' in msg:
                            diffs.extend(msg['suggested_code_blocks'])
                        if 'diff_histories' in msg:
                            diffs.extend(msg['diff_histories'])

                if messages:
                    # Get model from modelConfig
                    model = data.get('modelConfig', {}).get('modelName')
                    
                    # Add model to all assistant messages
                    for msg in messages:
                        if isinstance(msg, dict) and msg.get('role') == 'assistant':
                            msg['model'] = model
                    
                    conversations.append({
                        'messages': messages,
                        'source': 'cursor-global-composer',
                        'composer_id': composer_id,
                        'name': data.get('name', 'Untitled'),
                        'status': data.get('status'),
                        'unified_mode': data.get('unifiedMode'),
                        'created_at': data.get('createdAt'),
                        'updated_at': data.get('lastUpdatedAt'),
                        'model': model,
                        'has_code_context': len(code_contexts) > 0,
                        'has_diffs': len(diffs) > 0,
                        'storage_type': 'inline' if inline_conversation else 'separate'
                    })

            except (json.JSONDecodeError, KeyError) as e:
                continue

        conn.close()
    except Exception as e:
        print(f"Error extracting global composers: {e}")

    return conversations

def main():
    print("="*80)
    print("CURSOR ULTIMATE EXTRACTION - ALL VERSIONS (v0.2 - v2.0+)")
    print("="*80)
    print()

    installations = find_cursor_installations()

    if not installations:
        print("❌ No Cursor installations found!")
        return

    print(f"✅ Found {len(installations)} installation(s):")
    for inst in installations:
        print(f"   - {inst}")
    print()

    all_conversations = []
    stats = defaultdict(int)

    for installation in installations:
        print(f"📂 Processing: {installation}")

        # Extract from ALL workspace databases
        workspace_storage = installation / 'User/workspaceStorage'
        if workspace_storage.exists():
            aiservice_count = 0
            workspace_composer_count = 0
            chat_count = 0

            for workspace in workspace_storage.iterdir():
                if workspace.is_dir() and workspace.name != 'ext-dev':
                    db_file = workspace / 'state.vscdb'
                    if db_file.exists():
                        # Extract aiService (old format)
                        convs = extract_aiservice_conversations(db_file, workspace.name)
                        all_conversations.extend(convs)
                        aiservice_count += len(convs)

                        # Extract workspace composers
                        convs = extract_workspace_composers(db_file, workspace.name)
                        all_conversations.extend(convs)
                        workspace_composer_count += len(convs)

                        # Extract Chat mode
                        convs = extract_chat_mode(db_file, workspace.name)
                        all_conversations.extend(convs)
                        chat_count += len(convs)

            print(f"   ✅ aiService (old format): {aiservice_count} conversations")
            print(f"   ✅ Workspace composers: {workspace_composer_count} conversations")
            print(f"   ✅ Chat mode: {chat_count} conversations")
            stats['aiservice'] += aiservice_count
            stats['workspace_composer'] += workspace_composer_count
            stats['chat'] += chat_count

        # Extract global composers
        global_storage = installation / 'User/globalStorage/state.vscdb'
        if global_storage.exists():
            convs = extract_global_composers(global_storage)
            all_conversations.extend(convs)

            inline_count = sum(1 for c in convs if c.get('storage_type') == 'inline')
            separate_count = sum(1 for c in convs if c.get('storage_type') == 'separate')

            print(f"   ✅ Global composers: {len(convs)} conversations")
            print(f"      - Inline storage: {inline_count}")
            print(f"      - Separate storage: {separate_count}")

            stats['global_composer'] += len(convs)
        else:
            print(f"   ⚠️  No global storage found")

    print()
    print("="*80)
    print("ULTIMATE EXTRACTION COMPLETE")
    print("="*80)
    print(f"Total conversations: {len(all_conversations):,}")
    print(f"  aiService (v0.2-v0.43): {stats['aiservice']:,}")
    print(f"  Workspace composers (v0.43-v1.x): {stats['workspace_composer']:,}")
    print(f"  Global composers (v1.x-v2.0+): {stats['global_composer']:,}")
    print(f"  Chat mode (v0.x-v1.x): {stats['chat']:,}")

    if not all_conversations:
        print("No conversations found!")
        return

    # Statistics
    total_messages = sum(len(c['messages']) for c in all_conversations)
    with_code = sum(1 for c in all_conversations if c.get('has_code_context'))
    with_diffs = sum(1 for c in all_conversations if c.get('has_diffs'))
    complete = sum(1 for c in all_conversations
                   if any(m['role'] == 'assistant' for m in c['messages']))

    print(f"\nComplete conversations: {complete:,}")
    print(f"Total messages: {total_messages:,}")
    print(f"With code context: {with_code:,}")
    print(f"With diffs: {with_diffs:,}")
    print()

    # Save: one HF-traces JSONL file per session (one message per line)
    from traces_export import write_session_files
    n_files, n_lines = write_session_files(all_conversations, 'cursor')
    print(f"✅ Saved {n_files} session file(s) to extracted_data/cursor/sessions/")
    print(f"   Total message lines: {n_lines:,}")
    print(f"   Format: HF-traces JSONL (one message per line, one file per session)")

if __name__ == '__main__':
    main()
