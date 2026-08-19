import json

from conversation_store import connect, import_file


def test_import_keeps_distinct_same_session_rows(tmp_path):
    path = tmp_path / "conversations.jsonl"
    rows = [
        {
            "source": "codex",
            "session_id": "same-session",
            "messages": [{"role": "user", "content": "first", "timestamp": "2026-05-01T00:00:00Z"}],
        },
        {
            "source": "codex",
            "session_id": "same-session",
            "messages": [{"role": "user", "content": "second", "timestamp": "2026-05-02T00:00:00Z"}],
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    db_path = tmp_path / "conversations.sqlite3"
    conn = connect(db_path)
    with conn:
        imported, skipped = import_file(conn, path)

    assert imported == 2
    assert skipped == 0
    assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
    assert conn.execute("SELECT DISTINCT source FROM conversations").fetchall() == [("codex",)]


def test_import_collapses_identical_duplicate_rows(tmp_path):
    row = {
        "source": "cursor-composer",
        "workspace_id": "workspace",
        "messages": [{"role": "user", "content": "same"}],
    }
    path = tmp_path / "cursor_ultimate_20260503_000000.jsonl"
    path.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n")

    conn = connect(tmp_path / "conversations.sqlite3")
    with conn:
        imported, skipped = import_file(conn, path)

    assert imported == 2
    assert skipped == 0
    assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1
    assert conn.execute("SELECT source FROM conversations").fetchone()[0] == "cursor"


def test_schema_is_readable_from_sqlite(tmp_path):
    conn = connect(tmp_path / "conversations.sqlite3")
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }

    assert {"conversations", "messages"}.issubset(tables)
