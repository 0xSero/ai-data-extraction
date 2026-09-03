#!/usr/bin/env python3
"""Store extracted AI conversations in SQLite.

Extractor scripts still emit temporary JSONL snapshots. This module imports
those snapshots into a durable table keyed by source + conversation id, then
can export the latest conversations back to JSONL for existing consumers.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_DB = Path("extracted_data/conversations.sqlite3")

SOURCE_PATTERNS = [
    ("claude_code_conversations_*.jsonl", "claude_code"),
    ("codex_conversations_*.jsonl", "codex"),
    ("cursor_ultimate_*.jsonl", "cursor"),
    ("gemini_conversations_*.jsonl", "gemini_cli"),
    ("opencode_conversations_*.jsonl", "opencode"),
    ("trae_conversations_*.jsonl", "trae"),
    ("windsurf_conversations_*.jsonl", "windsurf"),
    ("continue_conversations_*.jsonl", "continue"),
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_timestamp(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
        except (OSError, OverflowError, ValueError):
            return str(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        if text.isdigit():
            return normalize_timestamp(int(text))
        return text
    return str(value)


def source_from_filename(path: Path) -> str:
    for pattern, source in SOURCE_PATTERNS:
        prefix = pattern.split("*", 1)[0]
        if path.name.startswith(prefix):
            return source
    return path.stem.rsplit("_", 2)[0]


def source_from_conversation(conv: dict[str, Any], fallback: str) -> str:
    raw_source = str(conv.get("source", "")).lower()
    installation = str(conv.get("installation", "")).lower()
    if raw_source == "codex" or ".codex" in installation or "codex" in str(conv.get("session_file", "")).lower():
        return "codex"
    if raw_source.startswith("cursor") or conv.get("workspace_id") or conv.get("composer_id"):
        return "cursor"
    if "gemini" in raw_source or ".gemini" in installation or conv.get("project_hash"):
        return "gemini_cli"
    if "opencode" in raw_source or "opencode" in installation:
        return "opencode"
    if "claude" in raw_source or ".claude" in installation:
        return "claude_code"
    return fallback


def conversation_id(conv: dict[str, Any], source: str, content_hash: str) -> str:
    candidates = [
        conv.get("session_id"),
        conv.get("composer_id"),
        conv.get("tab_id"),
        conv.get("conversation_id"),
        conv.get("id"),
        conv.get("source_file"),
    ]
    for value in candidates:
        if value:
            return f"{value}:{content_hash[:16]}"

    return content_hash


def conversation_timestamps(conv: dict[str, Any]) -> tuple[str, str]:
    timestamps = []
    for msg in conv.get("messages", []):
        if isinstance(msg, dict):
            ts = normalize_timestamp(msg.get("timestamp"))
            if ts:
                timestamps.append(ts)
    fallback = normalize_timestamp(conv.get("timestamp") or conv.get("created_at"))
    if not timestamps and fallback:
        timestamps.append(fallback)
    if not timestamps:
        return "", ""
    return min(timestamps), max(timestamps)


def conversation_model(conv: dict[str, Any]) -> str:
    if conv.get("model"):
        return str(conv["model"])
    for msg in reversed(conv.get("messages", [])):
        if isinstance(msg, dict) and msg.get("model"):
            return str(msg["model"])
    return ""


def conversation_project_path(conv: dict[str, Any]) -> str:
    for key in ("project_path", "cwd", "workspace_path", "project_dir"):
        if conv.get(key):
            return str(conv[key])
    return ""


def conversation_title(conv: dict[str, Any]) -> str:
    for key in ("name", "chat_title", "title", "project_name"):
        if conv.get(key):
            return str(conv[key])
    return ""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS conversations (
            source TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            first_timestamp TEXT,
            last_timestamp TEXT,
            message_count INTEGER NOT NULL,
            model TEXT,
            project_path TEXT,
            title TEXT,
            content_hash TEXT NOT NULL,
            source_file TEXT,
            imported_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            PRIMARY KEY (source, conversation_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            source TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            message_index INTEGER NOT NULL,
            role TEXT,
            timestamp TEXT,
            model TEXT,
            content_preview TEXT,
            PRIMARY KEY (source, conversation_id, message_index),
            FOREIGN KEY (source, conversation_id)
                REFERENCES conversations(source, conversation_id)
                ON DELETE CASCADE
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_last_timestamp ON conversations(last_timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_source_last ON conversations(source, last_timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_role ON messages(role)")
    return conn


def import_file(conn: sqlite3.Connection, path: Path, source: str | None = None) -> tuple[int, int]:
    file_source = source or source_from_filename(path)
    imported = 0
    skipped = 0
    now = utc_now()

    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                conv = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not isinstance(conv, dict):
                skipped += 1
                continue

            raw_json = stable_json(conv)
            row_source = source_from_conversation(conv, file_source)
            content_hash = sha256_text(raw_json)
            conv_id = conversation_id(conv, row_source, content_hash)
            messages = conv.get("messages", [])
            if not isinstance(messages, list):
                messages = []
            first_ts, last_ts = conversation_timestamps(conv)

            conn.execute(
                """
                INSERT INTO conversations (
                    source, conversation_id, first_timestamp, last_timestamp,
                    message_count, model, project_path, title, content_hash,
                    source_file, imported_at, updated_at, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, conversation_id) DO UPDATE SET
                    first_timestamp = excluded.first_timestamp,
                    last_timestamp = excluded.last_timestamp,
                    message_count = excluded.message_count,
                    model = excluded.model,
                    project_path = excluded.project_path,
                    title = excluded.title,
                    content_hash = excluded.content_hash,
                    source_file = excluded.source_file,
                    updated_at = excluded.updated_at,
                    raw_json = excluded.raw_json
                WHERE conversations.content_hash != excluded.content_hash
                   OR conversations.last_timestamp != excluded.last_timestamp
                   OR conversations.message_count != excluded.message_count
                """,
                (
                    row_source,
                    conv_id,
                    first_ts,
                    last_ts,
                    len(messages),
                    conversation_model(conv),
                    conversation_project_path(conv),
                    conversation_title(conv),
                    content_hash,
                    str(path),
                    now,
                    now,
                    raw_json,
                ),
            )

            conn.execute(
                "DELETE FROM messages WHERE source = ? AND conversation_id = ?",
                (row_source, conv_id),
            )
            for idx, msg in enumerate(messages):
                if not isinstance(msg, dict):
                    msg = {"content": str(msg)}
                conn.execute(
                    """
                    INSERT INTO messages (
                        source, conversation_id, message_index, role,
                        timestamp, model, content_preview
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row_source,
                        conv_id,
                        idx,
                        str(msg.get("role", "")),
                        normalize_timestamp(msg.get("timestamp")),
                        str(msg.get("model", "")) if msg.get("model") else "",
                        (msg.get("content", "") if isinstance(msg.get("content", ""), str) else stable_json(msg.get("content", "")))[:500],
                    ),
                )
            imported += 1
    return imported, skipped


def latest_files(data_dir: Path) -> list[Path]:
    paths = []
    for pattern, _source in SOURCE_PATTERNS:
        matches = sorted(data_dir.glob(pattern))
        if matches:
            paths.append(matches[-1])
    return paths


def import_jsonl(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    total = 0
    skipped = 0
    paths = [Path(p) for p in args.paths] if args.paths else latest_files(args.data_dir)
    with conn:
        for path in paths:
            count, bad = import_file(conn, path)
            total += count
            skipped += bad
            print(f"imported {count} from {path.name}")
    if args.delete_after_import:
        for path in paths:
            path.unlink(missing_ok=True)
    stored = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    print(f"stored {stored} unique conversations in {args.db} ({total} rows imported)")
    if skipped:
        print(f"skipped {skipped} malformed lines")


def export_latest(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    sources = ["claude_code", "codex", "cursor", "gemini_cli", "opencode"]
    with args.output.open("w") as f:
        total = 0
        for source in sources:
            rows = conn.execute(
                """
                SELECT raw_json
                FROM conversations
                WHERE source = ?
                ORDER BY COALESCE(last_timestamp, first_timestamp, updated_at) DESC
                """,
                (source,),
            )
            count = 0
            for (raw_json,) in rows:
                f.write(raw_json + "\n")
                total += 1
                count += 1
            print(f"{source}: {count} exported")
    print(f"exported {total} conversations -> {args.output}")


def stats(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    for source, count, messages in conn.execute(
        """
        SELECT source, COUNT(*), SUM(message_count)
        FROM conversations
        GROUP BY source
        ORDER BY source
        """
    ):
        print(f"{source}: {count} conversations, {messages or 0} messages")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="command", required=True)

    import_parser = sub.add_parser("import-jsonl")
    import_parser.add_argument("paths", nargs="*")
    import_parser.add_argument("--data-dir", type=Path, default=Path("extracted_data"))
    import_parser.add_argument("--delete-after-import", action="store_true")
    import_parser.set_defaults(func=import_jsonl)

    export_parser = sub.add_parser("export-latest")
    export_parser.add_argument("output", type=Path)
    export_parser.set_defaults(func=export_latest)

    stats_parser = sub.add_parser("stats")
    stats_parser.set_defaults(func=stats)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
