"""Local conversation history and incremental compaction; raw messages stay intact."""
from locations import project_path
import json
from functools import lru_cache
from pathlib import Path
from storage import database


@lru_cache(maxsize=2)
def _tokenizer(path):
    from context_budget import _tokenizer as load
    return load(path)


def _tokens(root, text):
    path = str(project_path(Path(root), 'vendor/tokenizers/deepseek-v4/tokenizer.json'))
    return len(_tokenizer(path).encode(str(text or '')).ids)


def _continuation(root, path, conversation):
    """The one continuation material for this place, from the common-experience ledger.

    Compression owns this now. The old per-store ``chat_summaries`` row is still read
    once for continuity after an upgrade (no new rows are written there); a checkpoint
    committed by the current compression path always wins over it.
    """
    from fish_agent.experience import compaction
    return compaction.summary_material(root, source=path.name, conversation=conversation)


class DesktopContext:
    # Records travel verbatim until the one request budget trims optional layers or
    # compression replaces an older complete range with its checkpoint; this module
    # never deletes an original or writes a second summary store.

    def __init__(self, root, path, *, channel='desktop'):
        self.root, self.path, self.channel = root, path, channel
        with database(path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS chat_inputs(message INTEGER PRIMARY KEY,content TEXT NOT NULL)')

    def remember_input(self, mid, content):
        # Images remain in the existing media archive; never duplicate base64 in SQLite.
        cached = []
        for block in content:
            if block['type'] == 'image':
                if not block.get('_fish_path'):
                    raise ValueError('图片缺少本地归档路径')
                cached.append({'type': 'image', '_fish_path': block['_fish_path']})
            else:
                cached.append(dict(block))
        with database(self.path) as db:
            db.execute('INSERT OR REPLACE INTO chat_inputs VALUES(?,?)', (mid, json.dumps(cached, ensure_ascii=False)))

    def history(self, cid, current_mid, media, gateway, cancelled, *, after=None, event=None):
        """One view over existing QQ/desktop records; originals stay in their own stores.

        The message adapter knows schemas. The one request budget owns size and
        clipping, and compression owns which older complete range became a checkpoint,
        so this path never invokes an inline summary or counts characters.
        """
        import sqlite3
        from datetime import datetime
        from governance import Governance

        virtual_event = event
        self.recent_window = int((virtual_event or {}).get('recent_window') or 1800)
        from contextlib import contextmanager

        @contextmanager
        def connect(path):
            conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=15)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
            finally:
                conn.close()

        considered = set()
        ledger = project_path(self.root, 'data/social/experience.db')
        if ledger.is_file():
            with connect(ledger) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='considered'").fetchone():
                    considered = {row[0] for row in db.execute('SELECT reference FROM considered')}

        def objects(value):
            if not value:
                return []
            data = json.loads(value) if isinstance(value, str) else value
            return data if isinstance(data, list) else []

        owner = str(Governance(self.root).owner_id())
        current_place = 'desktop' if self.channel == 'desktop' else ('private:' + owner if self.channel == 'qq' else self.channel)
        with connect(self.path) as db:
            current = virtual_event or db.execute('SELECT * FROM messages WHERE id=? AND conversation_id=?', (current_mid, cid)).fetchone()
            if not current:
                raise ValueError('当前输入记录不存在，未拼接其他对话代替')
            current = dict(current)
            timestamp = str(current.get('created_at') or current.get('ts') or '')
        continuation = None if after is not None else _continuation(self.root, self.path, cid)
        paths = [self.path]
        if self.path.parent.name == 'qq':
            for name in ('qq.db', 'desktop-chat.db'):
                other = project_path(self.root, 'data/qq', name)
                if other != self.path and other.exists():
                    paths.append(other)
        names = {}
        roster = project_path(self.root, 'data/social/people.db')
        if roster.exists():
            with connect(roster) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='members'").fetchone():
                    for row in db.execute('SELECT group_id,user_id,nickname,card FROM members'):
                        names[('group:' + str(row['group_id']), str(row['user_id']))] = row['card'] or row['nickname']
        events = []
        positions = {}
        index = {}
        bundles = {}
        for path in paths:
            if cancelled is not None and cancelled.is_set():
                raise InterruptedError('共同上下文准备已取消')
            source = 'desktop' if path.name == 'desktop-chat.db' else 'qq'
            with connect(path) as db:
                if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'").fetchone():
                    continue
                maximum = int(db.execute('SELECT COALESCE(MAX(id),0) FROM messages').fetchone()[0])
                positions[source] = maximum
                for row in db.execute('SELECT * FROM messages WHERE id>? AND id<=? ORDER BY id',
                                      (int((after or {}).get(source, 0)), maximum)):
                    row = dict(row)
                    stamp = str(row.get('created_at') or row.get('ts') or '')
                    if path == self.path and row['id'] == current_mid:
                        continue
                    outbound = row['direction'] == 'outbound'
                    parts = objects(row.get('segments'))
                    system_notice = any(str(part.get('origin') or '').startswith('system-') for part in parts)
                    if source == 'desktop':
                        place = 'desktop'
                        sender = 'Fish' if outbound else '主人'
                    else:
                        place = str(row.get('chat_id') or 'private:' + owner)
                        sender_id = str(row.get('sender_id') or row.get('user_id') or '')
                        sender = ('Fish' if outbound else '主人' if sender_id == owner
                                  else names.get((place, sender_id)) or '群友 ' + sender_id)
                    if system_notice:
                        sender = '系统通知'
                    delivery = ('已确认' if row['status'] in ('sent', 'done') else
                                '未确认' if row['status'] in ('pending', 'uncertain', 'unknown', 'sending') else
                                '未送达') if outbound else '收到'
                    conversation_key = source + ':' + str(row.get('conversation_id') or '')
                    if not outbound:
                        bundles[conversation_key] = source + ':' + str(row['id'])
                    bundle = str(row.get('runtime_turn_id') or bundles.get(conversation_key) or source + ':' + str(row['id']))
                    event = {'reference': source + ':' + str(row['id']), 'bundle': bundle, 'time': stamp,
                             'place': place, 'speaker': sender, 'text': str(row.get('text') or ''),
                             'reply_to': str(row.get('reply_to') or ''), 'delivery': delivery,
                             'attachments': [b for b in objects(row.get('segments')) if b.get('type') not in ('text', 'at')],
                             'current_place': place == current_place,
                             'current_conversation': path == self.path and row.get('conversation_id') == cid,
                             'outbound': outbound and not system_notice, 'remote_id': str(row.get('channel_message_id') or ''),
                             # 每条记录带自己的库名、会话和消息号：跨库时不能用别的库的行号，
                             # 也不能在循环结束后拿最后一行代替。
                             'store': path.name, 'id': int(row['id']),
                             'conversation': str(row.get('conversation_id') or cid)}
                    events.append(event)
                    if event['remote_id']:
                        index[(place, event['remote_id'])] = event
        # Records are event material, not new instructions or a claim of delivery.
        # Relevant cross-place selection and summary reuse happen before the sole cap.
        import re
        question = str(current.get('text') or '').casefold()
        words = set(re.findall(r'[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,}', question))
        for span in re.findall(r'[\u4e00-\u9fff]{2,}', question):
            words.update(span[i:i+2] for i in range(len(span)-1))
        cutoff = datetime.fromisoformat(timestamp).timestamp() if timestamp else 0
        picked = []
        for event in events:
            age = cutoff - datetime.fromisoformat(event['time']).timestamp() if event['time'] else float('inf')
            relevant = any(word in event['text'].casefold() for word in words)
            summarized = bool(continuation and continuation.get('summary') and event['current_conversation'] and
                              int(event['reference'].split(':')[1]) <= int(continuation['through']))
            if after is None and summarized and not relevant:
                continue
            if after is None and not event['current_conversation'] and not (relevant or age <= self.recent_window):
                continue
            if cancelled is not None and cancelled.is_set():
                raise InterruptedError('共同上下文准备已取消')
            quoted = index.get((event['place'], event['reply_to']))
            quote = ({'speaker': quoted['speaker'], 'time': quoted['time'], 'text': quoted['text'],
                      'reference': quoted['reference']} if quoted else
                     {'message_id': event['reply_to'], 'availability': '原引用未取得'} if event['reply_to'] else None)
            header = {'reference': event['reference'], 'time': event['time'], 'place': event['place'],
                      'speaker': event['speaker'], 'delivery': event['delivery'],
                      'already_considered': event['reference'] in considered}
            body = '【历史消息】' + json.dumps(header, ensure_ascii=False) + '\n'
            if quote:
                body += '引用：' + json.dumps(quote, ensure_ascii=False) + '\n'
            body += '正文：' + event['text']
            if event['attachments']:
                # Avoid copying large inline payloads or running a vision model on every old image.
                attached = [{k: v for k, v in a.items() if k in ('type','id','asset_id','name','path','url','file_id','message_id')}
                            for a in event['attachments']]
                body += '\n附件引用：' + json.dumps(attached, ensure_ascii=False)
            # A delivered message is a source event, not the original provider
            # assistant envelope (its full protocol reasoning may be unavailable).
            # Keep the explicit Fish/sender metadata, never invent reasoning or
            # insert an incomplete assistant envelope into a thinking/tool request.
            role = 'user'
            picked.append((event['time'], event['reference'],
                           {'role': role, 'content': body, '_fish_layer': 1 if event['current_conversation'] else 2,
                            '_fish_refs': [event['reference']], '_fish_bundle': event['bundle']},
                           {'source': event['store'], 'conversation': event['conversation'],
                            'id': int(event['id'])}))
        # 时间相同时按各库自己的消息号排序：消息号是数字，不能按 "qq:10" < "qq:2" 的字符串比。
        picked.sort(key=lambda item: (item[0], item[3]['source'], int(item[3]['id'])))
        # The surface is the ordered, unmodified original material of this place, priced
        # once here: the one request budget fits the request, and compression selects
        # complete older ranges from exactly this order. Originals are never removed.
        surface = []
        for item in picked:
            info = item[3]
            surface.append({'reference': info['source'] + ':' + str(info['id']),
                            'bundle': item[2].get('_fish_bundle') or '',
                            'layer': int(item[2].get('_fish_layer', 1)),
                            'source': info['source'], 'conversation': info['conversation'],
                            'id': int(info['id']), 'role': 'user',
                            'tokens': _tokens(self.root, item[2]['content']),
                            'text': item[2]['content']})
        history = [item[2] for item in picked]
        if continuation and continuation.get('summary'):
            history.insert(0, {'role': 'user', 'content': continuation.get('material') or continuation['summary'],
                               '_fish_layer': 1,
                               '_fish_refs': [self.path.name + ':checkpoint:' + str(continuation['through'])]})
        if after is None and not self.channel.startswith('group:'):
            history.extend(self.period_material(question, words))
        current_quote = index.get((current_place, str(current.get('reply_to') or '')))
        current_quote = ({'speaker': current_quote['speaker'], 'time': current_quote['time'],
                          'text': current_quote['text'], 'reference': current_quote['reference']} if current_quote else
                         {'message_id': current['reply_to'], 'availability': '原消息未取得'} if current.get('reply_to') else None)
        reference = (str(virtual_event['reference']) if virtual_event else
                     ('desktop' if self.channel == 'desktop' else 'qq') + ':' + str(current_mid))
        current_event = {'reference': reference, 'time': timestamp, 'place': current_place,
                         'speaker': '系统定时事件' if virtual_event else
                         ('主人' if self.channel in ('desktop', 'qq') or str(current.get('sender_id') or '') == owner
                          else names.get((current_place, str(current.get('sender_id') or ''))) or
                          '群友 ' + str(current.get('sender_id') or '')),
                         'quote': current_quote}
        return history, {'source': 'common-experience over original message stores',
                         'stores': [p.name for p in paths], 'current_place': current_place,
                         'current_reference': reference, 'current_event': current_event, 'positions': positions,
                         'surface': surface, 'continuation': (continuation or {}).get('kind', ''),
                         'material_count': len(history), 'budget': 'shared actual-model token budget'}

    def period_material(self, question, words):
        """Reuse existing day/week/month stores, without inventing another digest."""
        import sqlite3
        import time
        path = project_path(self.root, 'data/qq/memory.db')
        if not path.exists():
            return []
        result = []
        with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            queries = []
            if 'memory_digest' in tables:
                queries.append(('day', db.execute('SELECT day AS period,text FROM memory_digest ORDER BY day')))
            if 'memory_digest_period' in tables:
                queries.append(('', db.execute('SELECT scope,period,text FROM memory_digest_period ORDER BY period')))
            for scope, rows in queries:
                for row in rows:
                    text = str(row['text'] or '')
                    if not text or text.startswith('今天没整理成：'):
                        continue
                    kind = scope or row['scope']
                    relevant = any(word in text.casefold() for word in words)
                    if not relevant and not (kind == 'day' and row['period'] == time.strftime('%Y-%m-%d')):
                        continue
                    result.append({'role': 'user', 'content': '[派生小结，以原文及更正为准] ' +
                                   kind + ' ' + str(row['period']) + '\n' + text,
                                   '_fish_layer': 3 if kind == 'day' else 5,
                                   '_fish_refs': ['digest:' + kind + ':' + str(row['period'])]})
        return result
