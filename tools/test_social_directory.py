"""X08 directory resolution on a temporary root, without app startup or models."""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from social_context import SocialContext
from storage import database


class SocialDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'fish'
        self.contexts = SocialContext(self.root)

    def test_invalid_identifiers_raise_instead_of_becoming_paths(self):
        values = ('../escape', '..\\escape', str(self.root.parent / 'escape'),
                  'C:\\outside', 'a' * 31, 'G' * 32, '', None)
        for sid in values:
            with self.subTest(sid=sid):
                with self.assertRaisesRegex(ValueError, '无效的会话或人格版本编号'):
                    self.contexts.directory(sid)
        self.assertFalse((self.root / 'data/qq').exists())
        with database(self.contexts.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM spaces').fetchone()[0], 0)

    def test_valid_missing_space_keeps_the_existing_scratch_location_without_creating_it(self):
        sid = 'a' * 32
        expected = self.root / 'data/qq/spaces' / sid
        self.assertEqual(self.contexts.directory(sid), expected)
        self.assertFalse(expected.exists())
        with database(self.contexts.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM spaces').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM personas').fetchone()[0], 0)

    def test_existing_qq_and_research_spaces_keep_their_role_directories(self):
        for role in ('qq', 'research'):
            with self.subTest(role=role):
                persona = self.contexts.save_persona(role, role + ' 人格', role + ' 正文')
                space = self.contexts.create_space(role, revision=persona['id'])
                expected = self.root / 'data' / role / 'spaces' / space['id']
                self.assertEqual(self.contexts.directory(space['id']), expected)
                self.assertEqual((expected / 'persona.md').read_text(encoding='utf-8'), persona['body'])

    def test_existing_space_with_missing_persona_raises_instead_of_falling_back(self):
        persona = self.contexts.save_persona('qq', '待损坏的人格', '测试正文')
        space = self.contexts.create_space('qq', revision=persona['id'])
        # Build one corrupt historical binding only in this disposable database.
        with closing(sqlite3.connect(self.contexts.path)) as db:
            db.execute('PRAGMA foreign_keys=OFF')
            db.execute('DELETE FROM personas WHERE id=?', (persona['id'],))
            db.commit()
        with self.assertRaisesRegex(ValueError, '人格版本不存在'):
            self.contexts.directory(space['id'])
        with database(self.contexts.path) as db:
            self.assertIsNotNone(db.execute('SELECT 1 FROM spaces WHERE id=?', (space['id'],)).fetchone())
        print('X08：非法标识明确拒绝；仅合法缺失空间保留scratch路径；现存空间缺人格明确报错。', flush=True)


if __name__ == '__main__':
    unittest.main()
