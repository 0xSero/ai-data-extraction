import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import extract_cursor
import extract_cursor_cli


class CursorDatabasePathTests(unittest.TestCase):
    def test_uri_characters_preserve_conversations_in_all_cursor_formats(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ["plain", "project #1", "project?query", "project%20literal"]:
                root = Path(directory) / name
                root.mkdir()
                database = root / "store.db"
                with sqlite3.connect(database) as connection:
                    connection.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
                    connection.execute("CREATE TABLE cursorDiskKV (key TEXT, value TEXT)")
                    connection.execute("CREATE TABLE blobs (id TEXT, data BLOB)")
                    connection.execute("CREATE TABLE meta (value TEXT)")
                    composer = {"composerId": "fixture", "conversation": [{"type": 1, "text": "hello"}]}
                    items = {
                        "aiService.prompts": [{"text": "hello"}],
                        "composer.composerData": {"allComposers": [composer]},
                        "workbench.panel.aichat.view.aichat.chatdata": {"tabs": [{"bubbles": [{"type": "user", "text": "hello"}]}]},
                    }
                    connection.executemany("INSERT INTO ItemTable VALUES (?, ?)", [(key, json.dumps(value)) for key, value in items.items()])
                    connection.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", ("composerData:fixture", json.dumps(composer)))
                    connection.execute("INSERT INTO blobs VALUES (?, ?)", ("a" * 64, json.dumps({"role": "user", "content": "hello"}).encode()))
                    connection.execute("INSERT INTO meta VALUES (?)", (json.dumps({"latestRootBlobId": "a" * 64}).encode().hex(),))
                connection.close()
                original = database.read_bytes()
                extractors = [
                    lambda: extract_cursor.extract_aiservice_conversations(database, "fixture"),
                    lambda: extract_cursor.extract_workspace_composers(database, "fixture"),
                    lambda: extract_cursor.extract_chat_mode(database, "fixture"),
                    lambda: extract_cursor.extract_global_composers(database),
                    lambda: [extract_cursor_cli.extract_store(database, "fixture")],
                ]
                for index, extract in enumerate(extractors):
                    with self.subTest(path=name, format=index):
                        conversations = extract()
                        self.assertEqual(len(conversations), 1)
                        self.assertEqual(conversations[0]["messages"][0]["content"], "hello")
                        self.assertEqual(database.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
