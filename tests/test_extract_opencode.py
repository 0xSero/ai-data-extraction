import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from extract_opencode import (
    extract_cli_conversations,
    extract_json_conversations,
    extract_sqlite_conversations,
)


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding='utf-8')


def build_sqlite(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            parent_id TEXT,
            directory TEXT,
            title TEXT,
            version TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            summary_additions INTEGER,
            summary_deletions INTEGER,
            summary_files INTEGER
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            data TEXT
        );
        CREATE TABLE part (
            id TEXT PRIMARY KEY,
            message_id TEXT,
            session_id TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            data TEXT
        );
        """
    )
    conn.execute(
        """
        INSERT INTO session (
            id, project_id, parent_id, directory, title, version,
            time_created, time_updated, summary_additions, summary_deletions, summary_files
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            'ses_1',
            'proj_abc',
            None,
            '/tmp/demo',
            'Fix the extractor',
            '1.18.23',
            1000,
            2000,
            3,
            1,
            2,
        ),
    )
    conn.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
        (
            'msg_user',
            'ses_1',
            1100,
            1100,
            json.dumps({'role': 'user', 'model': {'modelID': 'gpt-4', 'providerID': 'openai'}}),
        ),
    )
    conn.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
        (
            'msg_asst',
            'ses_1',
            1200,
            1300,
            json.dumps({
                'role': 'assistant',
                'modelID': 'qwen2.5-coder:7b',
                'providerID': 'ollama',
                'agent': 'build',
                'mode': 'build',
                'cost': 0.01,
                'tokens': {'input': 10, 'output': 4},
            }),
        ),
    )
    conn.execute(
        "INSERT INTO part (id, message_id, session_id, time_created, data) VALUES (?, ?, ?, ?, ?)",
        (
            'prt_user',
            'msg_user',
            'ses_1',
            1100,
            json.dumps({'type': 'text', 'text': 'Please extract the sessions'}),
        ),
    )
    conn.execute(
        "INSERT INTO part (id, message_id, session_id, time_created, data) VALUES (?, ?, ?, ?, ?)",
        (
            'prt_text',
            'msg_asst',
            'ses_1',
            1200,
            json.dumps({'type': 'text', 'text': 'Done'}),
        ),
    )
    conn.execute(
        "INSERT INTO part (id, message_id, session_id, time_created, data) VALUES (?, ?, ?, ?, ?)",
        (
            'prt_think',
            'msg_asst',
            'ses_1',
            1210,
            json.dumps({'type': 'reasoning', 'text': 'Need the SQLite schema'}),
        ),
    )
    conn.execute(
        "INSERT INTO part (id, message_id, session_id, time_created, data) VALUES (?, ?, ?, ?, ?)",
        (
            'prt_tool',
            'msg_asst',
            'ses_1',
            1220,
            json.dumps({
                'type': 'tool',
                'tool': 'read',
                'callID': 'call_1',
                'state': {
                    'status': 'completed',
                    'input': {'filePath': 'extract_opencode.py'},
                    'output': 'old json tree',
                },
            }),
        ),
    )
    conn.commit()
    conn.close()


class SqliteExtractionTests(unittest.TestCase):
    def test_extracts_session_messages_and_parts(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / 'opencode.db'
            build_sqlite(db_path)

            conversations = extract_sqlite_conversations(db_path)

            self.assertEqual(len(conversations), 1)
            conversation = conversations[0]
            self.assertEqual(conversation['session_id'], 'ses_1')
            self.assertEqual(conversation['title'], 'Fix the extractor')
            self.assertEqual(conversation['directory'], '/tmp/demo')
            self.assertEqual(conversation['project_id'], 'proj_abc')
            self.assertEqual(conversation['version'], '1.18.23')
            self.assertEqual(conversation['created_at'], 1000)
            self.assertEqual(conversation['source'], 'opencode-cli')
            self.assertEqual(conversation['summary']['additions'], 3)

            self.assertEqual(len(conversation['messages']), 2)
            user, assistant = conversation['messages']
            self.assertEqual(user['role'], 'user')
            self.assertEqual(user['content'], 'Please extract the sessions')
            self.assertEqual(user['model'], 'gpt-4')
            self.assertEqual(user['provider'], 'openai')

            self.assertEqual(assistant['role'], 'assistant')
            self.assertEqual(assistant['content'], 'Done')
            self.assertEqual(assistant['model'], 'qwen2.5-coder:7b')
            self.assertEqual(assistant['reasoning'], 'Need the SQLite schema')
            self.assertEqual(assistant['tool_calls'][0]['name'], 'read')
            self.assertEqual(assistant['tool_results'][0]['output'], 'old json tree')

    def test_cli_reads_sqlite_when_json_tree_is_gone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build_sqlite(root / 'opencode.db')

            conversations = extract_cli_conversations(root)

            self.assertEqual(len(conversations), 1)
            self.assertEqual(conversations[0]['session_id'], 'ses_1')
            self.assertEqual(conversations[0]['messages'][0]['content'], 'Please extract the sessions')

    def test_cli_prefers_sqlite_over_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build_sqlite(root / 'opencode.db')

            write_json(
                root / 'storage' / 'message' / 'ses_json' / 'msg_1.json',
                {'id': 'msg_1', 'role': 'user', 'time': {'created': 1}},
            )
            write_json(
                root / 'storage' / 'part' / 'msg_1' / 'prt_1.json',
                {'type': 'text', 'text': 'json only'},
            )

            conversations = extract_cli_conversations(root)
            self.assertEqual(len(conversations), 1)
            self.assertEqual(conversations[0]['session_id'], 'ses_1')


class JsonExtractionTests(unittest.TestCase):
    def test_extracts_legacy_json_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(
                root / 'storage' / 'session' / 'proj_abc' / 'ses_json.json',
                {
                    'title': 'Legacy session',
                    'directory': '/old',
                    'projectID': 'proj_abc',
                    'version': '1.1.0',
                    'time': {'created': 10, 'updated': 20},
                },
            )
            write_json(
                root / 'storage' / 'message' / 'ses_json' / 'msg_1.json',
                {'id': 'msg_1', 'role': 'user', 'time': {'created': 11}},
            )
            write_json(
                root / 'storage' / 'part' / 'ses_json' / 'msg_1' / 'prt_1.json',
                {'type': 'text', 'text': 'hello from json'},
            )

            conversations = extract_json_conversations(root)
            self.assertEqual(len(conversations), 1)
            conversation = conversations[0]
            self.assertEqual(conversation['title'], 'Legacy session')
            self.assertEqual(conversation['directory'], '/old')
            self.assertEqual(conversation['messages'][0]['content'], 'hello from json')


if __name__ == '__main__':
    unittest.main()
