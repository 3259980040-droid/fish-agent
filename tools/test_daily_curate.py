"""每天一次的整理：三件事在一次里做完，判断都落在记录上。

这里钉住四件事：只看当天群里的图、同一张只存一次、存的每张都得有理由并进审计、
清理只动自动学到的事实（主人手存或认可的原样不动）。模型与下载都换成固定输入，
日期与库的形状用的还是生产那套。
"""
import io
import json
import sqlite3
import sys
import time
import unittest
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'tools')]
import dependencies  # noqa: F401
from PIL import Image
from test_social_base import Fixture
import daily_curate
from emoji_store import EmojiStore
from storage import database, role_dir


def png(colour):
    buffer = io.BytesIO()
    Image.new('RGB', (32, 32), colour).save(buffer, 'PNG')
    return buffer.getvalue()


class CurateTests(Fixture):
    def setUp(self):
        super().setUp()
        self.day = date.today().isoformat()
        self.messages = role_dir(self.root, 'qq') / 'qq.db'
        self.images = Path(self.tmp.name) / 'images'
        self.images.mkdir()
        self.face = self.images / 'face.png'
        self.face.write_bytes(png('orange'))
        self.more = self.images / 'more.png'
        self.more.write_bytes(png('teal'))
        self.digest_face = EmojiStore.inspect(self.face)[0]
        self.digest_more = EmojiStore.inspect(self.more)[0]

    # -- fixture -----------------------------------------------------------
    def store_messages(self, rows):
        """今天/昨天、群/私聊、图/纯文字混合的一份消息库。"""
        with closing(sqlite3.connect(self.messages)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, ts TEXT, chat_id TEXT, '
                       'sender_id TEXT, direction TEXT, segments TEXT)')
            db.executemany('INSERT INTO messages(id,ts,chat_id,sender_id,direction,segments) VALUES(?,?,?,?,?,?)', rows)
            db.commit()

    @staticmethod
    def segment(url, alt=''):
        return json.dumps([{'type': 'image', 'url': url, 'alt': alt}], ensure_ascii=False)

    def two_pictures(self):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        self.store_messages([
            (1, self.day + ' 08:00:00', 'group:' + self.group, '111', 'inbound', self.segment('https://q/a', '[动画表情]')),
            (2, self.day + ' 08:01:00', 'group:' + self.group, '222', 'inbound', self.segment('https://q/a', '[动画表情]')),
            (3, self.day + ' 08:02:00', 'group:' + self.group, '333', 'inbound', self.segment('https://q/b')),
            (4, self.day + ' 08:03:00', 'private:' + self.owner, '999', 'inbound', self.segment('https://q/c')),
            (5, yesterday + ' 23:59:00', 'group:' + self.group, '333', 'inbound', self.segment('https://q/d')),
            (6, self.day + ' 08:04:00', 'group:' + self.group, '333', 'inbound',
             json.dumps([{'type': 'text', 'text': '图呢'}], ensure_ascii=False)),
        ])

    def fake_fetch(self, root, groups, workdir):
        files = {'https://q/a': self.face, 'https://q/b': self.more}
        fetched = []
        for group in groups:
            path = files.get(group['url'])
            if path is None:
                continue
            fetched.append({**group, 'file': path, 'digest': EmojiStore.inspect(path)[0]})
        return fetched

    class Gateway:
        def __init__(self, picks):
            self.picks, self.calls = picks, []

        def json(self, system, user, **kwargs):
            self.calls.append((system, user, kwargs))
            return {'picks': self.picks}

    def run_curate(self, gateway, **kwargs):
        with patch('daily_curate._fetch', side_effect=self.fake_fetch):
            return daily_curate.run(self.root, self.day, gateway=gateway, messages_db=self.messages, **kwargs)

    # -- 只看当天群里的图 ---------------------------------------------------
    def test_only_today_group_images_and_same_picture_counted_together(self):
        self.two_pictures()
        groups = daily_curate._groups(self.messages, self.day)
        self.assertEqual(len(groups), 2)
        repeated = next(group for group in groups if group['url'] == 'https://q/a')
        self.assertEqual(repeated['messages'], [1, 2])
        self.assertEqual(repeated['alt'], '[动画表情]')
        # 群里用得多的排前面
        self.assertEqual(daily_curate._ranked(groups)[0]['url'], 'https://q/a')

    # -- 一次里三件事都做完，理由落进审计 -----------------------------------
    def test_one_run_cleans_digests_and_stores_with_reason(self):
        self.two_pictures()
        gateway = self.Gateway([{'index': 1, 'reason': '今天出现 2 次，大家都在用来接无语',
                                 'description': '一张橘色表情脸', 'tags': ['无语']}])
        record = self.run_curate(gateway)
        emoji = record['emoji']
        self.assertEqual(emoji['seen'], 3)          # 今天群里的三段图，私聊与昨天的没算
        self.assertEqual(emoji['groups'], 2)
        self.assertEqual(len(emoji['stored']), 1)
        stored = emoji['stored'][0]
        self.assertEqual(stored['asset'], self.digest_face)
        self.assertEqual(stored['messages'], [1, 2])
        self.assertIn('出现 2 次', stored['reason'])
        self.assertEqual([row['id'] for row in EmojiStore(self.root).list()], [self.digest_face])
        self.assertTrue(any(block.get('type') == 'image_url' for block in gateway.calls[0][1]))
        self.assertEqual(gateway.calls[0][2]['origin'], 'emoji-curate')
        # 审计：消息号与理由都留下了
        with closing(sqlite3.connect(self.root / 'data/qq/governance.db')) as db:
            rows = [row[2] for row in db.execute("SELECT ts,kind,reason FROM audit WHERE kind='emoji_curate' ORDER BY id")]
        self.assertTrue(any('消息1、2' in reason and '出现 2 次' in reason for reason in rows), rows)
        # 记录就是页面读的那一份
        self.assertEqual(daily_curate.latest(self.root)['day'], self.day)
        self.assertEqual(record['digest']['ok'], True)
        self.assertIn('cleaned', record)

    def test_page_field_is_served_by_memory_overview(self):
        self.two_pictures()
        gateway = self.Gateway([{'index': 1, 'reason': '能接嘲讽', 'description': '橘色表情脸', 'tags': []}])
        self.run_curate(gateway)
        from workbench import Workbench
        view = Workbench(self.root).dispatch('memory.overview', {})
        self.assertEqual(view['daily_curate']['day'], self.day)
        self.assertEqual(len(view['daily_curate']['emoji']['stored']), 1)
        self.assertEqual(view['daily_curate']['emoji']['stored'][0]['reason'], '能接嘲讽')

    # -- 同一张只存一次 -----------------------------------------------------
    def test_same_picture_is_never_stored_twice(self):
        self.two_pictures()
        gateway = self.Gateway([{'index': 1, 'reason': '能接嘲讽', 'description': '橘色表情脸', 'tags': []}])
        self.run_curate(gateway)
        again = self.run_curate(gateway, force=True)
        # 已经入库的那张不再送判定，也不重复存；同一天的另一张是新图，照常收。
        self.assertEqual(again['emoji']['offered'], 1)
        self.assertTrue(any('库里已经有这张' in row['why'] and row['messages'] == [1, 2]
                            for row in again['emoji']['skipped']))
        self.assertEqual([row['asset'] for row in again['emoji']['stored']], [self.digest_more])
        assets = [row['id'] for row in EmojiStore(self.root).list()]
        self.assertEqual(len(assets), len(set(assets)))
        self.assertEqual(set(assets), {self.digest_face, self.digest_more})
        # 入库那道门自己也认这张：再存一次只是 duplicate，不添一行
        added = EmojiStore(self.root).add(self.face, '重复的一张', '', 'test')
        self.assertTrue(added['duplicate'])
        self.assertEqual(len(EmojiStore(self.root).list()), 2)

    def test_pick_without_a_reason_is_refused(self):
        self.two_pictures()
        gateway = self.Gateway([{'index': 1, 'reason': '', 'description': '橘色表情脸', 'tags': []}])
        record = self.run_curate(gateway)
        self.assertEqual(record['emoji']['picked'], 0)
        self.assertEqual(record['emoji']['stored'], [])
        self.assertEqual(EmojiStore(self.root).list(), [])

    def test_out_of_range_or_missing_pictures_are_skipped_not_invented(self):
        self.store_messages([
            (1, self.day + ' 08:00:00', 'group:' + self.group, '111', 'inbound', self.segment('https://q/gone')),
        ])
        record = self.run_curate(self.Gateway([{'index': 1, 'reason': '看着不错', 'description': 'x', 'tags': []}]))
        self.assertEqual(record['emoji']['stored'], [])
        self.assertEqual(record['emoji']['fetched'], 0)
        self.assertTrue(record['emoji']['note'])

    def test_day_without_pictures_says_so(self):
        record = self.run_curate(self.Gateway([]))
        self.assertEqual(record['emoji']['seen'], 0)
        self.assertEqual(record['emoji']['note'], daily_curate.EMPTY)

    # -- 清理：重复合并、过期停用、主人的事实不动 ---------------------------
    def test_cleanup_merges_duplicates_and_expires_without_touching_owner_facts(self):
        from memory_store import MemoryStore
        MemoryStore(self.root, 'qq')
        now = time.time()
        rows = [('dup-a', '主人周三晚上都在北门的小面馆吃面', 'qq:9001', 'observed', 'active', 0),
                ('dup-b', '主人周三晚上都在北门的小面馆吃面了！', 'qq:9002', 'observed', 'active', 0),
                ('old-1', '主人这周在准备季度的产品评审', 'qq:9003', 'observed', 'active', now - 3600),
                ('fact-1', '主人的生日是三月七日', 'manual', 'manual', 'active', 0),
                ('fact-2', '主人要求晚上十一点以后不要主动发消息', 'manual', 'owner_quote', 'active', 0)]
        with database(role_dir(self.root, 'qq') / 'memory.db') as db:
            for memory_id, text, source, evidence, status, expires in rows:
                db.execute('INSERT INTO memories(id,text,category,scope,source,source_text,confidence,created,'
                           'updated,kind,evidence,status,observed,expires) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (memory_id, text, '事实', 'owner', source, '测试夹具', 1.0, now - 86400, now - 86400,
                            'fact', evidence, status, now - 86400, expires))
        cleaned = daily_curate._cleanup(self.root, now)
        self.assertEqual(cleaned['total'], 2)
        self.assertEqual([row['id'] for row in cleaned['merged']], ['dup-b'])
        self.assertEqual(cleaned['merged'][0]['duplicate_of'], 'dup-a')
        self.assertEqual([row['id'] for row in cleaned['expired']], ['old-1'])
        with database(role_dir(self.root, 'qq') / 'memory.db') as db:
            state = {row['id']: dict(row) for row in db.execute('SELECT id,status,text FROM memories')}
        self.assertEqual(len(state), 5)                    # 一条都没删
        self.assertEqual(state['dup-b']['status'], 'candidate')
        self.assertEqual(state['old-1']['status'], 'candidate')
        for key in ('fact-1', 'fact-2'):
            self.assertEqual(state[key]['status'], 'active')
        self.assertEqual(state['fact-1']['text'], '主人的生日是三月七日')

    # -- 调度：凌晨四点后、一天一次 -----------------------------------------
    def test_tick_waits_for_the_small_hours_and_runs_only_once(self):
        early = time.mktime(time.strptime(self.day + ' 03:30:00', '%Y-%m-%d %H:%M:%S'))
        self.assertEqual(daily_curate.tick(self.root, early)['skipped'], '还没到凌晨四点')
        self.assertIsNone(daily_curate.latest(self.root))
        with database(role_dir(self.root, 'qq') / 'memory.db') as db:
            db.execute(daily_curate.TABLE)
            db.execute('INSERT INTO daily_curate(day,result,created) VALUES(?,?,?)',
                       (self.day, json.dumps({'day': self.day, 'created_text': 'x'}), time.time()))
        late = time.mktime(time.strptime(self.day + ' 09:00:00', '%Y-%m-%d %H:%M:%S'))
        self.assertEqual(daily_curate.tick(self.root, late)['skipped'], '今天已经整理过')

    def test_day_must_be_a_real_date(self):
        with self.assertRaises(ValueError):
            daily_curate.run(self.root, '2026-13-01')


if __name__ == '__main__':
    unittest.main()
