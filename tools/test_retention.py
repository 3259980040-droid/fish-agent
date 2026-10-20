"""Offline checks for retention backups and disposable cache pruning."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from data_retention import DataRetention
from memory_store import MemoryStore


class RetentionTests(unittest.TestCase):
    def test_backup_keeps_facts_and_only_prunes_expired_cache(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            (root / 'data/social').mkdir(parents=True)
            (root / 'data/qq').mkdir(parents=True)
            store = MemoryStore(root, 'qq')
            fact = store.save('主人喜欢先看结论。', scope='owner', source='manual')
            for name in ('group-language.db', 'people.db'):
                con = sqlite3.connect(root / 'data/social' / name)
                con.execute('CREATE TABLE IF NOT EXISTS marker(value TEXT)')
                con.execute("INSERT INTO marker VALUES('keep')")
                con.commit(); con.close()
            cache = root / 'data/qq/web-cache.db'
            con = sqlite3.connect(cache)
            con.execute('CREATE TABLE searches(id TEXT PRIMARY KEY, expires REAL)')
            con.execute('INSERT INTO searches VALUES(?,?)', ('expired', 1.0))
            con.execute('INSERT INTO searches VALUES(?,?)', ('keep', 9999999999.0))
            con.commit(); con.close()

            result = DataRetention(root).run(now=100.0)
            self.assertGreaterEqual(result['files'], 3)
            self.assertEqual(result['long_term_deleted'], 0)
            self.assertIn(fact['id'], [row['id'] for row in store.search('结论', scope='owner')])
            con = sqlite3.connect(cache)
            ids = {row[0] for row in con.execute('SELECT id FROM searches')}
            con.close()
            self.assertEqual(ids, {'keep'})


if __name__ == '__main__':
    unittest.main()
