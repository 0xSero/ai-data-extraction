import json
import sqlite3
import unittest
from extract_cursor import extract_bubbles_for_composer


class CursorIndexTests(unittest.TestCase):
    def test_indexed_range_keeps_row_order_and_literal_composer_prefix(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)')
        for key, text in [('bubbleId:ab_:two', 'second'), ('bubbleId:abX:foreign', 'foreign'), ('bubbleId:ab_:one', 'first')]:
            db.execute('INSERT INTO cursorDiskKV VALUES (?,?)', (key, json.dumps({'type': 2, 'text': text})))
        self.assertEqual([m['content'] for m in extract_bubbles_for_composer(db.cursor(), 'ab_')], ['second', 'first'])
        plan = db.execute('EXPLAIN QUERY PLAN SELECT key,value FROM cursorDiskKV WHERE key>=? AND key<? ORDER BY rowid', ('bubbleId:ab_:', 'bubbleId:ab_;')).fetchall()
        self.assertTrue(any('SEARCH' in row[-1] and 'INDEX' in row[-1] for row in plan), plan)
