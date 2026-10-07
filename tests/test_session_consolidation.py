import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import extract_droid
import extract_omp
from extract_pi import extract_session, extract_sessions


def message(id_, parent, role, content, **extra):
    return {'type': 'message', 'id': id_, 'parentId': parent,
            'message': {'role': role, 'content': content, **extra}}


class SharedSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'session.jsonl'

    def write(self, rows):
        self.path.write_text(''.join(json.dumps(r) + '\n' for r in rows))
        return self.path

    def test_strings_custom_context_state_and_malformed_lines(self):
        rows = [17, ['not a record'], {'type': 'session', 'id': 's'},
                {'type': 'model_change', 'id': 'm', 'parentId': None, 'provider': 'p', 'modelId': 'model'},
                {'type': 'custom_message', 'id': 'ctx', 'parentId': 'm', 'content': 'extension context', 'customType': 'context'},
                message('u', 'ctx', 'user', 'question'), message('a', 'u', 'assistant', 'string answer'),
                {'type': 'branch_summary', 'id': 'summary', 'parentId': 'a', 'summary': 'branch context'},
                {'type': 'message', 'id': 'bad', 'parentId': 'summary', 'message': 'malformed'}]
        conv = extract_session(self.write(rows), 'omp')
        self.assertEqual([m['content'] for m in conv['messages']], ['extension context', 'question', 'string answer', 'branch context'])
        self.assertEqual(conv['messages'][2]['model'], 'model')
        self.assertEqual(conv['messages'][2]['provider'], 'p')
        self.assertTrue(conv['messages'][0]['custom'])

    def test_active_and_all_forks_keep_separate_ancestry(self):
        rows = [{'type': 'session', 'id': 's'}, message('u', None, 'user', 'question'),
                message('left', 'u', 'assistant', 'left answer'), message('right', 'u', 'assistant', 'right answer')]
        self.write(rows)
        self.assertEqual([m['content'] for m in extract_session(self.path, 'pi')['messages']], ['question', 'right answer'])
        branches = extract_sessions(self.path, 'omp', all_branches=True)
        self.assertEqual({b['branch_id'] for b in branches}, {'left', 'right'})
        self.assertEqual({tuple(m['content'] for m in b['messages']) for b in branches}, {('question', 'left answer'), ('question', 'right answer')})

    def test_delayed_tool_result_retains_owner_call_id(self):
        call = lambda id_: [{'type': 'toolCall', 'id': id_, 'name': 'read', 'arguments': {}}]
        rows = [message('a', None, 'assistant', call('call-a')), message('b', 'a', 'assistant', call('call-b')),
                message('result', 'b', 'toolResult', 'result for a', toolCallId='call-a', toolName='read')]
        messages = extract_session(self.write(rows), 'pi')['messages']
        self.assertEqual(messages[-1]['role'], 'tool')
        self.assertEqual(messages[-1]['tool_call_id'], messages[0]['tool_calls'][0]['id'])
        self.assertNotIn('tool_results', messages[1])

    def test_droid_bad_records_settings_and_string_answer_preserve_valid_turns(self):
        self.write([99, {'type': 'message', 'message': 'bad'}, {'type': 'session_start', 'id': 'droid-s'},
                    message('u', None, 'user', 'question'), message('a', 'u', 'assistant', '\x1b[31manswer\x1b[0m')])
        self.path.with_suffix('.settings.json').write_text('[]')
        with patch.object(extract_droid, 'find_droid_sessions', return_value=self.root):
            result = extract_droid.extract_droid_sessions()
        self.assertEqual(len(result), 1)
        self.assertEqual([m['content'] for m in result[0]['messages']], ['question', 'answer'])
        self.assertNotIn('settings', result[0])

    def test_prompt_history_readonly_snapshot_includes_live_wal_and_escaped_path(self):
        root = self.root / 'uri#? space'; root.mkdir()
        db = root / 'history.db'
        writer = sqlite3.connect(db)
        self.addCleanup(writer.close)
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute('CREATE TABLE history (prompt TEXT)')
        writer.execute("INSERT INTO history VALUES ('committed WAL prompt')"); writer.commit()
        files = [db, Path(str(db) + '-wal')]
        before = [f.read_bytes() for f in files]
        self.assertEqual(extract_omp.extract_prompt_history(root), [{'prompt': 'committed WAL prompt'}])
        self.assertEqual(before, [f.read_bytes() for f in files])


if __name__ == '__main__':
    unittest.main()
