#!/usr/bin/env python3
"""
Extract ALL Droid CLI / Factory session data
Includes: messages, tool use/results, thinking blocks, todo snapshots,
compaction summaries, and per-session settings metadata.
"""

import json
import re
from pathlib import Path
from datetime import datetime

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def strip_ansi(text):
    if not isinstance(text, str):
        return text
    return ANSI_RE.sub("", text)


def extract_text_content(content):
    if isinstance(content, str):
        return strip_ansi(content)

    if isinstance(content, dict):
        if content.get("text"):
            return strip_ansi(content["text"])
        return ""

    if not isinstance(content, list):
        return ""

    parts = []
    for item in content:
        if isinstance(item, str):
            parts.append(strip_ansi(item))
        elif isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
            parts.append(strip_ansi(item["text"]))

    return "\n".join(parts)


def find_droid_sessions():
    sessions_dir = Path.home() / ".factory" / "sessions"
    if not sessions_dir.exists():
        return None
    return sessions_dir


def load_settings(settings_file):
    if not settings_file.exists():
        return None

    try:
        with open(settings_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def normalize_droid_message(entry):
    message = entry.get("message", {})
    role = message.get("role")
    content_items = message.get("content", [])

    if not isinstance(content_items, list):
        content_items = [content_items] if content_items else []

    text_parts = []
    thinking_parts = []
    tool_uses = []
    tool_results = []

    for item in content_items:
        if not isinstance(item, dict):
            continue

        item_type = item.get("type")
        if item_type == "text" and item.get("text"):
            text_parts.append(strip_ansi(item["text"]))
        elif item_type == "thinking" and item.get("thinking"):
            thinking_parts.append(strip_ansi(item["thinking"]))
        elif item_type == "tool_use":
            tool_uses.append({
                "id": item.get("id"),
                "name": item.get("name"),
                "input": item.get("input"),
            })
        elif item_type == "tool_result":
            tool_results.append({
                "tool_use_id": item.get("tool_use_id"),
                "content": extract_text_content(item.get("content")),
            })

    normalized = {
        "role": role,
        "content": "\n".join(text_parts),
        "timestamp": entry.get("timestamp"),
        "message_id": entry.get("id"),
        "parent_id": entry.get("parentId"),
    }

    if message.get("visibility"):
        normalized["visibility"] = message.get("visibility")

    if message.get("openaiMessageId"):
        normalized["openai_message_id"] = message.get("openaiMessageId")

    if message.get("openaiPhase"):
        normalized["openai_phase"] = message.get("openaiPhase")

    if thinking_parts:
        normalized["thinking"] = "\n\n".join(thinking_parts)

    if tool_uses:
        normalized["tool_uses"] = tool_uses

    if tool_results:
        normalized["tool_results"] = tool_results

    return normalized


def extract_droid_sessions():
    sessions_dir = find_droid_sessions()
    if not sessions_dir:
        return []

    conversations = []
    jsonl_files = sorted(sessions_dir.rglob("*.jsonl"))

    for jsonl_file in jsonl_files:
        try:
            session_id = jsonl_file.stem
            settings_file = jsonl_file.with_suffix(".settings.json")
            settings = load_settings(settings_file)

            messages = []
            todo_states = []
            compaction_states = []
            session_title = None
            session_display_title = None
            session_owner = None
            session_cwd = None
            session_version = None
            session_type = None
            mission_id = None
            calling_session_id = None
            calling_tool_use_id = None
            is_manual_title = None

            with open(jsonl_file, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue

                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    entry_type = obj.get("type")

                    if entry_type == "session_start":
                        session_id = obj.get("id", session_id)
                        session_title = obj.get("title")
                        session_display_title = obj.get("sessionTitle")
                        session_owner = obj.get("owner")
                        session_version = obj.get("version")
                        session_cwd = obj.get("cwd")
                        session_type = obj.get("decompSessionType")
                        mission_id = obj.get("decompMissionId")
                        calling_session_id = obj.get("callingSessionId")
                        calling_tool_use_id = obj.get("callingToolUseId")
                        is_manual_title = obj.get("isSessionTitleManuallySet")

                    elif entry_type == "message":
                        normalized = normalize_droid_message(obj)
                        if normalized.get("content") or normalized.get("thinking") or normalized.get("tool_uses") or normalized.get("tool_results"):
                            messages.append(normalized)

                    elif entry_type == "todo_state":
                        todo_states.append({
                            "timestamp": obj.get("timestamp"),
                            "message_index": obj.get("messageIndex"),
                            "todos": obj.get("todos"),
                        })

                    elif entry_type == "compaction_state":
                        compaction_states.append({
                            "timestamp": obj.get("timestamp"),
                            "summary_text": obj.get("summaryText"),
                        })

            if not messages:
                continue

            conversation = {
                "messages": messages,
                "source": "droid",
                "session_id": session_id,
                "project_path": session_cwd,
                "source_file": str(jsonl_file),
            }

            if session_title:
                conversation["session_title"] = session_title

            if session_display_title:
                conversation["session_display_title"] = session_display_title

            if session_owner:
                conversation["session_owner"] = session_owner

            if session_version is not None:
                conversation["session_version"] = session_version

            if session_type:
                conversation["session_type"] = session_type

            if mission_id:
                conversation["mission_id"] = mission_id

            if calling_session_id:
                conversation["calling_session_id"] = calling_session_id

            if calling_tool_use_id:
                conversation["calling_tool_use_id"] = calling_tool_use_id

            if is_manual_title is not None:
                conversation["manual_title"] = is_manual_title

            if todo_states:
                conversation["todo_states"] = todo_states

            if compaction_states:
                conversation["compaction_states"] = compaction_states

            if settings:
                conversation["settings"] = settings

                if settings.get("model"):
                    conversation["model"] = settings.get("model")

                if settings.get("providerLock"):
                    conversation["provider_lock"] = settings.get("providerLock")

                if settings.get("reasoningEffort"):
                    conversation["reasoning_effort"] = settings.get("reasoningEffort")

                if settings.get("interactionMode"):
                    conversation["interaction_mode"] = settings.get("interactionMode")

                if settings.get("autonomyMode"):
                    conversation["autonomy_mode"] = settings.get("autonomyMode")

                if settings.get("autonomyLevel"):
                    conversation["autonomy_level"] = settings.get("autonomyLevel")

                if settings.get("tokenUsage"):
                    conversation["token_usage"] = settings.get("tokenUsage")

                if settings.get("tags"):
                    conversation["tags"] = settings.get("tags")

                conversation["settings_file"] = str(settings_file)

            conversations.append(conversation)

        except Exception as e:
            print(f"Error processing {jsonl_file}: {e}")
            continue

    return conversations


def main():
    print("=" * 80)
    print("DROID CLI SESSION EXTRACTION")
    print("=" * 80)
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

    print(f"Total conversations: {len(conversations):,}")

    total_messages = sum(len(c["messages"]) for c in conversations)
    user_messages = sum(1 for c in conversations for m in c["messages"] if m["role"] == "user")
    assistant_messages = sum(1 for c in conversations for m in c["messages"] if m["role"] == "assistant")
    with_tools = sum(
        1
        for c in conversations
        if any("tool_uses" in m or "tool_results" in m for m in c["messages"])
    )
    with_thinking = sum(1 for c in conversations if any("thinking" in m for m in c["messages"]))
    with_settings = sum(1 for c in conversations if "settings" in c)

    print(f"Total messages: {total_messages:,}")
    print(f"User messages: {user_messages:,}")
    print(f"Assistant messages: {assistant_messages:,}")
    print(f"With tool use/results: {with_tools:,}")
    print(f"With reasoning/thinking: {with_thinking:,}")
    print(f"With settings metadata: {with_settings:,}")
    print()

    output_dir = Path("extracted_data")
    output_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = output_dir / f"droid_conversations_{timestamp}.jsonl"

    with open(output_file, "w", encoding="utf-8") as f:
        for conv in conversations:
            f.write(json.dumps(conv, ensure_ascii=False) + "\n")

    file_size = output_file.stat().st_size / 1024 / 1024
    print(f"✅ Saved to: {output_file}")
    print(f"   Size: {file_size:.2f} MB")
    print("   Format: JSONL (one session per line)")


if __name__ == "__main__":
    main()
