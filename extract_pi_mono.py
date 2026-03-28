#!/usr/bin/env python3
"""
Extract pi-mono / Pi coding-agent session data.

Includes user and assistant messages, tool calls and results, bash executions,
thinking blocks, and session metadata from ~/.pi/agent/sessions.
"""

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
SESSIONS_DIR = Path.home() / ".pi" / "agent" / "sessions"


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


def find_pi_sessions_dir():
    if SESSIONS_DIR.exists():
        return SESSIONS_DIR
    return None


def normalize_custom_message(entry):
    message = {
        "role": "custom_message",
        "content": extract_text_content(entry.get("content")),
        "timestamp": entry.get("timestamp"),
        "entry_id": entry.get("id"),
        "parent_id": entry.get("parentId"),
    }

    if entry.get("customType"):
        message["custom_type"] = entry.get("customType")
    if entry.get("display") is not None:
        message["display"] = entry.get("display")
    if entry.get("details") is not None:
        message["details"] = entry.get("details")

    return message


def normalize_pi_message(entry):
    message = entry.get("message", {})
    role = message.get("role")
    timestamp = message.get("timestamp", entry.get("timestamp"))

    base = {
        "timestamp": timestamp,
        "entry_id": entry.get("id"),
        "parent_id": entry.get("parentId"),
    }

    if role == "user":
        return {
            "role": "user",
            "content": extract_text_content(message.get("content")),
            **base,
        }

    if role == "assistant":
        content_items = message.get("content", [])
        if not isinstance(content_items, list):
            content_items = [content_items] if content_items else []

        text_parts = []
        thinking_parts = []
        tool_calls = []

        for item in content_items:
            if not isinstance(item, dict):
                continue

            item_type = item.get("type")
            if item_type == "text" and item.get("text"):
                text_parts.append(strip_ansi(item["text"]))
            elif item_type == "thinking" and item.get("thinking"):
                thinking_parts.append(strip_ansi(item["thinking"]))
            elif item_type == "toolCall":
                tool_calls.append(
                    {
                        "id": item.get("id"),
                        "name": item.get("name"),
                        "arguments": item.get("arguments"),
                    }
                )

        normalized = {
            "role": "assistant",
            "content": "\n".join(text_parts),
            **base,
        }

        for key in [
            "api",
            "provider",
            "model",
            "usage",
            "stopReason",
            "errorMessage",
            "responseId",
        ]:
            if message.get(key) is not None:
                normalized[key] = message.get(key)

        if thinking_parts:
            normalized["thinking"] = "\n\n".join(thinking_parts)
        if tool_calls:
            normalized["tool_calls"] = tool_calls

        return normalized

    if role == "toolResult":
        normalized = {
            "role": "tool_result",
            "content": extract_text_content(message.get("content")),
            "tool_call_id": message.get("toolCallId"),
            "tool_name": message.get("toolName"),
            "is_error": message.get("isError", False),
            **base,
        }
        if message.get("details") is not None:
            normalized["details"] = message.get("details")
        return normalized

    if role == "bashExecution":
        normalized = {
            "role": "bash_execution",
            "command": message.get("command"),
            "output": strip_ansi(message.get("output", "")),
            "exit_code": message.get("exitCode"),
            "cancelled": message.get("cancelled", False),
            "truncated": message.get("truncated", False),
            **base,
        }
        if message.get("fullOutputPath"):
            normalized["full_output_path"] = message.get("fullOutputPath")
        if message.get("excludeFromContext") is not None:
            normalized["exclude_from_context"] = message.get("excludeFromContext")
        return normalized

    if role == "branchSummary":
        return {
            "role": "branch_summary",
            "summary": message.get("summary", ""),
            "from_id": message.get("fromId"),
            **base,
        }

    if role == "compactionSummary":
        return {
            "role": "compaction_summary",
            "summary": message.get("summary", ""),
            "tokens_before": message.get("tokensBefore"),
            **base,
        }

    if role == "custom":
        normalized = {
            "role": "custom",
            "content": extract_text_content(message.get("content")),
            **base,
        }
        if message.get("customType"):
            normalized["custom_type"] = message.get("customType")
        if message.get("details") is not None:
            normalized["details"] = message.get("details")
        return normalized

    return None


def extract_pi_sessions():
    sessions_dir = find_pi_sessions_dir()
    if not sessions_dir:
        return []

    conversations = []

    for jsonl_file in sorted(sessions_dir.rglob("*.jsonl")):
        try:
            messages = []
            session_id = jsonl_file.stem
            started_at = None
            project_path = None
            parent_session = None
            session_version = None
            session_name = None
            model_changes = []
            thinking_level_changes = []
            compactions = []
            branch_summaries = []
            initial_provider = None
            initial_model_id = None
            initial_thinking_level = None

            with open(jsonl_file, "r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue

                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    entry_type = obj.get("type")

                    if entry_type == "session":
                        session_id = obj.get("id", session_id)
                        started_at = obj.get("timestamp", started_at)
                        project_path = obj.get("cwd")
                        parent_session = obj.get("parentSession")
                        session_version = obj.get("version")
                        initial_provider = obj.get("provider")
                        initial_model_id = obj.get("modelId")
                        initial_thinking_level = obj.get("thinkingLevel")
                    elif entry_type == "message":
                        normalized = normalize_pi_message(obj)
                        if normalized is not None:
                            messages.append(normalized)
                    elif entry_type == "custom_message":
                        messages.append(normalize_custom_message(obj))
                    elif entry_type == "model_change":
                        model_changes.append(
                            {
                                "provider": obj.get("provider"),
                                "model_id": obj.get("modelId"),
                                "timestamp": obj.get("timestamp"),
                                "entry_id": obj.get("id"),
                                "parent_id": obj.get("parentId"),
                            }
                        )
                    elif entry_type == "thinking_level_change":
                        thinking_level_changes.append(
                            {
                                "thinking_level": obj.get("thinkingLevel"),
                                "timestamp": obj.get("timestamp"),
                                "entry_id": obj.get("id"),
                                "parent_id": obj.get("parentId"),
                            }
                        )
                    elif entry_type == "session_info":
                        if obj.get("name"):
                            session_name = obj.get("name")
                    elif entry_type == "compaction":
                        compactions.append(
                            {
                                "summary": obj.get("summary"),
                                "first_kept_entry_id": obj.get("firstKeptEntryId"),
                                "tokens_before": obj.get("tokensBefore"),
                                "timestamp": obj.get("timestamp"),
                                "entry_id": obj.get("id"),
                                "parent_id": obj.get("parentId"),
                            }
                        )
                    elif entry_type == "branch_summary":
                        branch_summaries.append(
                            {
                                "summary": obj.get("summary"),
                                "from_id": obj.get("fromId"),
                                "timestamp": obj.get("timestamp"),
                                "entry_id": obj.get("id"),
                                "parent_id": obj.get("parentId"),
                            }
                        )

            if not messages:
                continue

            conversation = {
                "messages": messages,
                "source": "pi-mono",
                "session_id": session_id,
                "project_path": project_path,
                "started_at": started_at,
                "source_file": str(jsonl_file),
            }

            if session_name:
                conversation["session_name"] = session_name
            if parent_session:
                conversation["parent_session"] = parent_session
            if session_version is not None:
                conversation["session_version"] = session_version
            if initial_provider:
                conversation["initial_provider"] = initial_provider
            if initial_model_id:
                conversation["initial_model_id"] = initial_model_id
            if initial_thinking_level:
                conversation["initial_thinking_level"] = initial_thinking_level
            if model_changes:
                conversation["model_changes"] = model_changes
            if thinking_level_changes:
                conversation["thinking_level_changes"] = thinking_level_changes
            if compactions:
                conversation["compactions"] = compactions
            if branch_summaries:
                conversation["branch_summaries"] = branch_summaries

            conversations.append(conversation)
        except Exception as exc:
            print(f"Error processing {jsonl_file}: {exc}")

    return conversations


def main():
    print("=" * 80)
    print("PI-MONO SESSION EXTRACTION")
    print("=" * 80)
    print()

    print("Searching for pi-mono sessions...")
    sessions_dir = find_pi_sessions_dir()

    if not sessions_dir:
        print("No pi-mono sessions directory found!")
        print(f"Expected: {SESSIONS_DIR}")
        return

    print(f"Found sessions directory: {sessions_dir}")
    print()

    conversations = extract_pi_sessions()
    if not conversations:
        print("No pi-mono sessions found!")
        return

    print(f"Total conversations: {len(conversations):,}")

    total_messages = sum(len(conv["messages"]) for conv in conversations)
    role_counts = Counter(message["role"] for conv in conversations for message in conv["messages"])
    with_tools = sum(
        1
        for conv in conversations
        if any("tool_calls" in message or message.get("role") == "tool_result" for message in conv["messages"])
    )
    with_bash = sum(
        1 for conv in conversations if any(message.get("role") == "bash_execution" for message in conv["messages"])
    )
    with_thinking = sum(1 for conv in conversations if any("thinking" in message for message in conv["messages"]))

    print(f"Total messages: {total_messages:,}")
    print(f"User messages: {role_counts.get('user', 0):,}")
    print(f"Assistant messages: {role_counts.get('assistant', 0):,}")
    print(f"With tool use/results: {with_tools:,}")
    print(f"With bash executions: {with_bash:,}")
    print(f"With reasoning/thinking: {with_thinking:,}")
    print()

    output_dir = Path("extracted_data")
    output_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = output_dir / f"pi_mono_conversations_{timestamp}.jsonl"

    with open(output_file, "w", encoding="utf-8") as handle:
        for conversation in conversations:
            handle.write(json.dumps(conversation, ensure_ascii=False) + "\n")

    file_size = output_file.stat().st_size / 1024 / 1024
    print(f"Saved to: {output_file}")
    print(f"Size: {file_size:.2f} MB")
    print("Format: JSONL (one session per line)")


if __name__ == "__main__":
    main()
