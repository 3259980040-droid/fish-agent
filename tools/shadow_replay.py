"""Offline shadow replay: one synthetic group message in, one event record out.

The replay walks harness/group_decide.py's public entry (shadow_ready /
respond_as_group) with a scripted fake model, so the decision is the real one and
the model call never leaves the machine. No transport is imported here, nothing is
sent, and the only store this module writes is the shadow ledger under the root it
was pointed at, so --root must be a disposable root, never the live one.

Evidence levels are not interchangeable:

  fixture               synthetic group messages + a scripted model (this round)
  recorded-history      real de-identified rows + a scripted model
  real-model-shadow     a real model call, for a later round

Every record carries real_model_called and sent, and both stay False here: the
model is a script, and no send path is reachable from this module.

    # fixture 等级，输出三份证据到同一个目录
    python tools/shadow_replay.py --root <临时根> \
        --fixture tools/fixtures/shadow_replay_basic.json \
        --out <证据目录>/影子回放_证据.json

    # recorded-history 等级：把 --fixture 换成脱敏后的真实历史夹具即可，
    # 夹具字段与 fixture 等级完全一致，模型仍是脚本
    python tools/shadow_replay.py --root <临时根> --fixture <脱敏历史.json> \
        --out <证据目录>/影子回放_证据.json

Fixture shape (every key but chat and messages is optional):

    {"name": "basic", "chat": "group:987654321", "window": "", "revision": "",
     "policy": {"engine": "shadow", "read": true, "listen": true, "learn": true,
                "reply": true, "shadow_daily_limit": 60},
     "paused": false,
     "members": [{"user_id": "2100000001", "name": "群友甲"}],
     "model": {"text": "候选文本", "sleep_s": 0},
     "messages": [{"message_id": "m1", "minutes_ago": 2,
                   "sender": {"id": "2100000001", "display_name": "群友甲"},
                   "text": "在吗", "reply_to": ""}],
     "targets": ["m3"]}
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

HARNESS = Path(__file__).resolve().parents[1]
if str(HARNESS) not in sys.path:
    sys.path[:0] = [str(HARNESS), str(HARNESS / 'harness'), str(HARNESS / 'tools')]

from model_gateway import ChatOptions, ChatResult  # noqa: E402
import group_decide  # noqa: E402

EVIDENCE_LEVELS = ('fixture', 'recorded-history', 'real-model-shadow')
EVIDENCE_LEVEL = 'fixture'
# One replay may not hang on a stuck model: past this the run is recorded as
# timed out instead of raising, and the worker is given a grace to settle so its
# scripted model cannot outlive the call. The value mirrors the bound the live
# group handler puts around the same call; this module keeps its own copy so it
# imports no transport.
SHADOW_TIMEOUT_S = 25.0
WORKER_GRACE_S = 5.0
EVIDENCE_JSON = '影子回放_证据.json'
EVIDENCE_MD = '影子回放_证据.md'
LEVELS_MD = '证据等级说明.md'
QQ_COLUMNS = ('ts,chat_id,user_id,text,replied,conversation_id,sender_id,direction,'
              'channel_message_id,reply_to,segments,status')
MESSAGES_DDL = ('CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, '
                'ts TEXT, chat_id TEXT, user_id TEXT, text TEXT, replied INTEGER DEFAULT 0, '
                'conversation_id TEXT, sender_id TEXT, direction TEXT, channel_message_id TEXT, '
                'reply_to TEXT, segments TEXT, status TEXT)')
SKIP_NOTES = {
    'paused': '主人已暂停 QQ 角色，影子不运行',
    'busy': '同群已有影子在跑（并发占用），本次跳过',
    'daily-limit': '该群今日影子额度已用完（shadow_daily_limit=0 或已耗尽），本次跳过',
    'no-read': '主人已撤销该群的读取授权（或该群不在 allowlist），影子不读也不算',
}
RECORD_FIELDS = ('case', 'evidence_level', 'chat', 'message_id', 'engine', 'requested_engine',
                 'skipped',
                 'skip_note', 'candidate', 'tools', 'current', 'history', 'references',
                 'request_view', 'revision', 'test_window_revision', 'permissions',
                 'fixture', 'model_calls', 'real_model_called', 'sent', 'timed_out',
                 'elapsed_ms', 'error')

__all__ = ['EVIDENCE_LEVELS', 'SHADOW_TIMEOUT_S', 'load_fixture', 'prepare_root', 'replay',
           'replay_one', 'permissions', 'write_evidence']


def _chat_of(fixture) -> str:
    chat = str((fixture or {}).get('chat') or '')
    if not chat:
        raise ValueError('夹具缺少 chat（应为 group:群号）')
    return chat


def load_fixture(path) -> dict:
    """One fixture JSON file, read as data; the shape is documented above."""
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError('夹具必须是 JSON 对象')
    _chat_of(data)
    if not isinstance(data.get('messages'), list) or not data['messages']:
        raise ValueError('夹具必须带非空的 messages 列表')
    return data


def _stamp(message) -> str:
    """Fixture rows carry either an explicit ts or "minutes_ago"; default is now."""
    ts = str(message.get('ts') or '').strip()
    if ts:
        return ts
    try:
        minutes = float(message.get('minutes_ago', 2))
    except (TypeError, ValueError):
        minutes = 2.0
    return (datetime.now() - timedelta(minutes=minutes)).strftime('%Y-%m-%d %H:%M:%S')


def _segments(message) -> list:
    segments = message.get('segments')
    if isinstance(segments, list) and segments:
        return [item for item in segments if isinstance(item, dict)]
    return [{'type': 'text', 'text': str(message.get('text') or '')}]


def _connect(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    return db


def _seed_messages(root, fixture) -> int:
    """Fixture rows become ordinary messages rows, which is all group_decide reads."""
    chat = _chat_of(fixture)
    added = 0
    with closing(_connect(Path(root) / 'data/qq/qq.db')) as db:
        db.execute(MESSAGES_DDL)
        for message in fixture.get('messages') or []:
            if not isinstance(message, dict):
                continue
            mid = str(message.get('message_id') or '')
            if not mid:
                raise ValueError('夹具消息缺少 message_id')
            if db.execute('SELECT 1 FROM messages WHERE chat_id=? AND channel_message_id=?',
                          (chat, mid)).fetchone():
                continue
            sender = message.get('sender') if isinstance(message.get('sender'), dict) else {}
            db.execute('INSERT INTO messages(' + QQ_COLUMNS + ') VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (_stamp(message), chat, str(sender.get('id') or ''), str(message.get('text') or ''),
                        0, 'fixture', str(sender.get('id') or ''),
                        str(message.get('direction') or 'inbound'), mid,
                        str(message.get('reply_to') or ''), '',
                        str(message.get('status') or 'observed')))
            db.commit()
            added += 1
    return added


def _seed_members(root, fixture) -> int:
    """Optional roster rows, so evidence shows names instead of bare sender ids."""
    chat = _chat_of(fixture)
    members = [item for item in (fixture.get('members') or []) if isinstance(item, dict)]
    if not members:
        return 0
    with closing(_connect(Path(root) / 'data/social/people.db')) as db:
        db.execute('CREATE TABLE IF NOT EXISTS members (group_id TEXT, user_id TEXT, nickname TEXT, '
                   'card TEXT, PRIMARY KEY (group_id, user_id))')
        for member in members:
            db.execute('INSERT OR REPLACE INTO members VALUES(?,?,?,?)',
                       (chat[6:], str(member.get('user_id') or ''),
                        str(member.get('nickname') or ''), str(member.get('name') or '')))
        db.commit()
    return len(members)


def _seed_channel(root, fixture) -> bool:
    """A root without a channel config has no owner id and no bot name at all."""
    path = Path(root) / 'channels/fish_qq/config.json'
    if path.is_file():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'allowlist': {'owner_id': str(fixture.get('owner_id') or ''),
                                             'group_ids': [_chat_of(fixture)[6:]]},
                                'bot': {'display_name': '大肥鱼'}}, ensure_ascii=False),
                    encoding='utf-8')
    return True


def _owner_id(root) -> str:
    try:
        data = json.loads((Path(root) / 'channels/fish_qq/config.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return ''
    allow = data.get('allowlist') if isinstance(data, dict) else None
    return str(allow.get('owner_id') or '') if isinstance(allow, dict) else ''


def _has_rows(path, sql, args=()) -> bool:
    try:
        db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    except sqlite3.Error:
        return False
    try:
        return bool(db.execute(sql, args).fetchall())
    except sqlite3.Error:
        return False
    finally:
        db.close()


def _seed_personas(root, fixture) -> dict:
    """Optional personas: a fixture that names them can run on a bare root.

    The window persona goes through the real group-test surface (GroupTests.create),
    so a fixture can reproduce "old test window persona != owner private persona".
    """
    applied = {}
    persona = fixture.get('persona')
    if isinstance(persona, dict) and (persona.get('body') or persona.get('name')):
        from social_context import SocialContext
        contexts = SocialContext(root)
        if not _has_rows(Path(root) / 'data/social/context.db',
                         'SELECT revision FROM default_personas WHERE role=?', ('qq',)):
            saved = contexts.save_persona('qq', str(persona.get('name') or '影子夹具人格'),
                                          str(persona.get('body') or ''))
            contexts.set_default('qq', saved['id'])
            applied['persona'] = saved['id']
    window = fixture.get('window_persona')
    group = _chat_of(fixture)[6:]
    if isinstance(window, dict) and (window.get('body') or window.get('name')):
        from social_context import SocialContext
        from group_tests import GroupTests
        if not _has_rows(Path(root) / 'data/social/context.db',
                         'SELECT space FROM group_test_routes WHERE group_id=?', (group,)):
            saved = SocialContext(root).save_persona('qq', str(window.get('name') or '群测试夹具人格'),
                                                    str(window.get('body') or ''))
            GroupTests(root).create(group, saved['id'], _owner_id(root))
            applied['window_persona'] = saved['id']
    return applied


def prepare_root(root, fixture) -> dict:
    """Write only this fixture's inputs into a disposable root; nothing else is touched.

    The switches are ordinary stored settings, so the skip paths (paused, no daily
    allowance) are reached by the same code the live bot uses, not by faking it.
    """
    applied = {'messages_added': _seed_messages(root, fixture), 'members_added': _seed_members(root, fixture),
               'channel_created': _seed_channel(root, fixture)}
    from group_policy import DEFAULTS, GroupPolicy
    updates = {key: value for key, value in (fixture.get('policy') or {}).items() if key in DEFAULTS}
    if updates:
        GroupPolicy(root).configure(_chat_of(fixture), updates)
    applied['policy'] = dict(updates)
    if 'paused' in fixture:
        from governance import Governance
        Governance(root, 'qq').save({'paused': bool(fixture.get('paused'))})
    applied['paused'] = bool(fixture.get('paused', False))
    applied.update(_seed_personas(root, fixture))
    return applied


def permissions(root, chat) -> dict:
    """What the stored switches really say, window takeover included (read-only)."""
    from group_read import authorized_groups
    try:
        listed = [row for row in authorized_groups(root) if str(row.get('chat')) == str(chat)]
    except Exception as exc:  # an unreadable store is a fact to report, not a crash
        return {'authorized': False, 'error': type(exc).__name__ + ': ' + str(exc)[:200]}
    if not listed:
        return {'authorized': False, 'error': '该群未获主人读取授权，权限项无从读取'}
    row = dict(listed[0])
    row['authorized'] = True
    return row


def _replies(fixture) -> tuple:
    model = fixture.get('model') if isinstance(fixture.get('model'), dict) else {}
    replies = model.get('replies')
    if isinstance(replies, list) and replies:
        script = [str(item) for item in replies]
    else:
        script = [str(model.get('text') or '')]
    try:
        sleep_s = float(model.get('sleep_s') or 0)
    except (TypeError, ValueError):
        sleep_s = 0.0
    return script, sleep_s


class ScriptedGateway:
    """A fake model: a fixed script, no connection, and a receipt of every call."""

    base = 'fixture://scripted-model'
    model = 'fixture-model'

    def __init__(self, replies, sleep_s=0.0):
        self.replies = list(replies) or ['']
        self.sleep_s = float(sleep_s or 0)
        self.calls = []

    def chat(self, messages, **kwargs):
        tools = kwargs.get('tools') or ()
        self.calls.append({'messages': len(messages or ()),
                           'tools': [str((item or {}).get('function', {}).get('name')) for item in tools]})
        if self.sleep_s:
            time.sleep(self.sleep_s)
        index = min(len(self.calls) - 1, len(self.replies) - 1)
        return ChatResult(self.replies[index], {}, 'stop')


def _scripted_run(root, chat, payload, *, window, revision, replies, sleep_s):
    """The real shadow decision over a scripted model; the model seam is the only fake."""
    gateway = ScriptedGateway(replies, sleep_s)
    with patch('model_gateway.ModelGateway', lambda *args, **kwargs: gateway), \
            patch('model_gateway.chat_options', lambda root: ChatOptions('high', 512, 30)):
        candidate = group_decide.respond_as_group(root, chat, payload=payload, window=window,
                                                  revision=revision)
    return candidate, gateway.calls


def _bounded(root, chat, payload, *, window, revision, replies, sleep_s, timeout):
    """Run one shadow call under a time budget; a stuck model becomes data, not a raise."""
    outcome = {}

    def work():
        try:
            outcome['candidate'], outcome['calls'] = _scripted_run(
                root, chat, payload, window=window, revision=revision, replies=replies, sleep_s=sleep_s)
        except BaseException as exc:  # a replay failure is reported, never re-raised upstream
            outcome['error'] = type(exc).__name__ + ': ' + str(exc)[:400]

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(timeout)
    timed_out = worker.is_alive()
    if timed_out:
        worker.join(WORKER_GRACE_S)
    return outcome, timed_out, worker.is_alive()


def _fixture_summary(fixture, target) -> dict:
    messages = [item for item in fixture.get('messages') or [] if isinstance(item, dict)]
    return {'name': str(fixture.get('name') or ''),
            'chat': _chat_of(fixture),
            'window': str(fixture.get('window') or ''),
            'message_count': len(messages),
            'message_ids': [str(item.get('message_id') or '') for item in messages],
            'target': str(target),
            'policy': {key: value for key, value in (fixture.get('policy') or {}).items()},
            'paused': bool(fixture.get('paused', False)),
            'model_text': _replies(fixture)[0][0],
            'evidence_level': EVIDENCE_LEVEL}


def _payload(fixture, target) -> dict:
    """The inbound payload group_decide expects, rebuilt from the fixture row."""
    chat = _chat_of(fixture)
    for message in fixture.get('messages') or []:
        if not isinstance(message, dict) or str(message.get('message_id')) != str(target):
            continue
        sender = message.get('sender') if isinstance(message.get('sender'), dict) else {}
        return {'chat_type': 'group', 'chat_id': chat, 'message_id': str(target),
                'sender': {'id': str(sender.get('id') or ''),
                           'display_name': str(sender.get('display_name') or sender.get('name') or '')},
                'reply_to': str(message.get('reply_to') or ''), 'mentions_bot': False,
                'segments': _segments(message)}
    raise ValueError('target 不在夹具消息里：' + str(target))


def _targets(fixture) -> list:
    targets = fixture.get('targets')
    if isinstance(targets, list) and targets:
        return [str(item) for item in targets]
    return [str(item.get('message_id') or '') for item in fixture.get('messages') or []
            if isinstance(item, dict) and str(item.get('direction') or 'inbound') == 'inbound']


def replay_one(root, fixture, target, *, timeout=None, case='') -> dict:
    """One replayed message: the candidate plus every fact a reviewer needs to judge it."""
    chat = _chat_of(fixture)
    payload = _payload(fixture, target)
    ready = group_decide.shadow_ready(root, chat)
    record = {'case': case or str(fixture.get('name') or '') + ':' + str(target),
              'evidence_level': EVIDENCE_LEVEL, 'chat': chat, 'message_id': str(target),
              'engine': ready['engine'], 'requested_engine': ready.get('requested_engine', ''),
              'skipped': '', 'skip_note': '', 'candidate': '', 'tools': [], 'current': '',
              'history': [], 'references': [], 'request_view': {}, 'revision': '',
              'test_window_revision': '', 'permissions': permissions(root, chat),
              'fixture': _fixture_summary(fixture, target), 'model_calls': 0,
              'real_model_called': False, 'sent': False, 'timed_out': False,
              'elapsed_ms': 0, 'error': ''}
    if ready['engine'] != 'shadow':
        # engine=dsh keeps the old path; a replay must not spend a call there.
        record['skipped'] = 'engine:' + str(ready['engine'])
        record['skip_note'] = '该群 engine=' + str(ready['engine']) + '，影子完全不运行'
        return record
    replies, sleep_s = _replies(fixture)
    budget = SHADOW_TIMEOUT_S if timeout is None else float(timeout)
    outcome, timed_out, abandoned = _bounded(
        root, chat, payload, window=str(fixture.get('window') or ''),
        revision=str(fixture.get('revision') or ''), replies=replies, sleep_s=sleep_s, timeout=budget)
    candidate = outcome.get('candidate') if isinstance(outcome.get('candidate'), dict) else {}
    calls = outcome.get('calls') if isinstance(outcome.get('calls'), list) else []
    view = candidate.get('request_view') if isinstance(candidate.get('request_view'), dict) else {}
    late = str(candidate.get('text') or '')
    record.update({
        'candidate': late, 'tools': candidate.get('tools') or [],
        'current': str(candidate.get('current') or ''), 'history': candidate.get('history') or [],
        'references': candidate.get('references') or [], 'request_view': view,
        'revision': str(candidate.get('revision') or ''),
        'test_window_revision': str(candidate.get('test_window_revision') or ''),
        'skipped': str(candidate.get('skipped') or ''),
        'model_calls': len(calls), 'timed_out': bool(timed_out),
        'elapsed_ms': int(candidate.get('elapsed_ms') or 0),
        'error': str(candidate.get('error') or outcome.get('error') or '')})
    if record['skipped']:
        record['skip_note'] = SKIP_NOTES.get(record['skipped'], '影子跳过：' + record['skipped'])
    if timed_out:
        # A late answer is not the answer to this message: it stays out of the
        # candidate column and is only named as the reason the run was cut.
        record['candidate'] = ''
        note = 'shadow replay 超时（' + str(budget) + ' 秒），未抛异常'
        note += '；工作线程仍未结束' if abandoned else (
            '；工作线程稍后返回了文本，未计入候选' if late else '；工作线程稍后返回空文本')
        record['error'] = record['error'] or note
    return record


def replay(root, fixture, *, timeout=None, prepare=True, ledger=False) -> dict:
    """Every target in the fixture, replayed one at a time against the same root."""
    setup = prepare_root(root, fixture) if prepare else {}
    chat = _chat_of(fixture)
    records = []
    for target in _targets(fixture):
        record = replay_one(root, fixture, target, timeout=timeout)
        records.append(record)
        if ledger and not record['skipped'] and not record['error']:
            group_decide.record_shadow(root, chat, payload=_payload(fixture, target),
                                       candidate={'engine': record['engine'], 'text': record['candidate'],
                                                  'tools': record['tools'],
                                                  'elapsed_ms': record['elapsed_ms']})
    return {'tool': 'tools/shadow_replay.py', 'evidence_level': EVIDENCE_LEVEL,
            'real_model_called': False, 'sent': False, 'chat': chat, 'setup': setup,
            'records': records}


def _view_lines(record) -> list:
    view = record.get('request_view') or {}
    request = view.get('current_request') or {}
    excluded = view.get('excluded') or []
    window = view.get('window') or {}
    lines = ['- 正在回答：' + str(request.get('focus_message_id') or '-')
             + '（发送者 ' + str(request.get('focus_sender_id') or '-') + '）',
             '- 选入历史：' + (', '.join(str(item.get('message_id')) + '←' + str(item.get('reason'))
                                      for item in (view.get('related_history') or [])) or '无'),
             '- 排除：' + (', '.join(str(item.get('message_id')) + '(' + str(item.get('reason')) + ')'
                                   for item in excluded) or '无'),
             '- 时间窗口：候选 ' + str(window.get('candidate_rows', '-')) + ' 行，保留 '
             + str(window.get('kept_rows', '-')) + ' 行，排除 ' + str(window.get('excluded_rows', '-')) + ' 行',
             '- 引用链：' + (', '.join(str(item.get('message_id')) for item in (record.get('references') or []))
                            or '无')]
    return lines


def _permissions_lines(record) -> list:
    row = record.get('permissions') or {}
    if not row.get('authorized'):
        return ['- 群权限：' + str(row.get('error') or '未授权')]
    return ['- 群权限：read=' + str(row.get('read')) + '，listen=' + str(row.get('listen'))
            + '，learn=' + str(row.get('learn')) + '，reply=' + str(row.get('reply'))
            + '（来源 ' + str(row.get('participation_source')) + '；主策略 listen='
            + str(row.get('main_listen')) + '/reply=' + str(row.get('main_reply')) + '）']


def _markdown(records, *, generated_at: str, source: str) -> str:
    lines = ['# 影子隔离回放证据（轮次：fixture 等级）', '',
             '生成时间：' + generated_at, '输入夹具：' + str(source or '-'),
             '证据等级：fixture（合成群消息 + 脚本化假模型）',
             '真实模型调用：false　实际发送：false', '',
             '说明：每条记录都由 harness/group_decide.py 的公开入口（shadow_ready / '
             'respond_as_group）真实跑一遍，模型换成固定脚本，因此不会外连、不会发言。'
             'request_view 里的 excluded 与 reason 就是这条消息被排除或选中的真实原因。', '',
             '| 用例 | engine | skipped | 候选 | 真实模型 | 发送 | 耗时 ms |',
             '|---|---|---|---|---|---|---|']
    for record in records:
        lines.append('| {} | {} | {} | {} | {} | {} | {} |'.format(
            record.get('case'), record.get('engine'), record.get('skipped') or '否',
            (record.get('candidate') or '（空）').replace('|', '｜'),
            'false', 'false', record.get('elapsed_ms')))
    for record in records:
        lines += ['', '## ' + str(record.get('case')), '',
                  '- 群 / 消息：' + str(record.get('chat')) + ' / ' + str(record.get('message_id')),
                  '- 引擎：' + str(record.get('engine'))
                  + ('（请求值 ' + str(record.get('requested_engine')) + '）'
                     if record.get('requested_engine') else ''),
                  '- 人格版本：' + str(record.get('revision') or '-')
                  + '；旧测试窗口人格：' + str(record.get('test_window_revision') or '-'),
                  '- 跳过：' + (str(record.get('skipped')) + '（' + str(record.get('skip_note')) + '）'
                              if record.get('skipped') else '否'),
                  '- 候选文本：' + (record.get('candidate') or '（空）'),
                  '- 候选工具：' + (', '.join(map(str, record.get('tools') or [])) or '无'),
                  '- 真实模型调用：false；实际发送：false',
                  '- 脚本模型调用次数：' + str(record.get('model_calls')),
                  '- 夹具：' + json.dumps(record.get('fixture') or {}, ensure_ascii=False)]
        lines += _permissions_lines(record)
        lines += _view_lines(record)
        if record.get('error'):
            lines.append('- 错误/超时：' + str(record.get('error')))
    return '\n'.join(lines) + '\n'


def _levels_markdown(payload, *, generated_at: str) -> str:
    records = payload.get('records') or []
    counts = {}
    for record in records:
        counts[record.get('evidence_level')] = counts.get(record.get('evidence_level'), 0) + 1
    listed = ', '.join(sorted(counts)) if counts else '（无记录）'
    return '\n'.join([
        '# 证据等级说明', '',
        '生成时间：' + generated_at, '',
        '## 三个等级', '',
        '- fixture：合成群消息 + 脚本化假模型。证明的是"决策路径本身可复现"，'
        '不能证明模型回复质量。',
        '- recorded-history：真实脱敏历史 + 脚本化假模型。证明的是"同样的真实输入下投影/候选稳定"，'
        '仍不能证明模型质量。',
        '- real-model-shadow：真实模型调用。只有这一级才能评价回复质量，代价是一次付费调用，'
        '本轮没有使用。', '',
        '## 本轮（fixture）', '',
        '- 本轮全部用例等级：' + listed,
        '- 真实模型调用（real_model_called）：false',
        '- 实际发送（sent）：false',
        '- 记录条数：' + str(len(records)), '',
        '## 怎么把等级升上去', '',
        '把 --fixture 换成脱敏后的真实历史夹具即为 recorded-history；'
        '要评价回复质量再单独用 real-model-shadow 跑一次，并把两级的证据分开存放，不要混在同一次结论里。',
        ''])


def write_evidence(payload, out_json, *, generated_at=None, source='') -> list:
    """Write the three evidence files; the JSON names the directory for the other two."""
    stamp = generated_at or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    out = Path(out_json)
    out.parent.mkdir(parents=True, exist_ok=True)
    body = {'tool': 'tools/shadow_replay.py', 'generated_at': stamp,
            'source_fixture': str(source), 'evidence_level': EVIDENCE_LEVEL,
            'evidence_levels': list(EVIDENCE_LEVELS),
            'real_model_called': False, 'sent': False,
            'note': ('fixture 等级：合成群消息 + 脚本化假模型；证明决策路径，不证明回复质量'),
            'records': payload.get('records') or []}
    out.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding='utf-8')
    md = out.parent / EVIDENCE_MD
    md.write_text(_markdown(body['records'], generated_at=stamp, source=source), encoding='utf-8')
    levels = out.parent / LEVELS_MD
    levels.write_text(_levels_markdown(body, generated_at=stamp), encoding='utf-8')
    return [str(out), str(md), str(levels)]


def main(argv=None):
    parser = argparse.ArgumentParser(description='离线影子回放：合成夹具进，事件记录出，不发消息')
    parser.add_argument('--root', required=True, help='一次性根目录（夹具会写进去，别指生产根）')
    parser.add_argument('--fixture', required=True, help='夹具 JSON')
    parser.add_argument('--out', help='证据 JSON 路径；同级目录还会写出 md 与证据等级说明')
    parser.add_argument('--timeout', type=float, default=SHADOW_TIMEOUT_S, help='单次回放超时秒数')
    parser.add_argument('--ledger', action='store_true', help='同时写入影子账本，供 A/B 对比脚本读取')
    parser.add_argument('--stdout', action='store_true')
    args = parser.parse_args(argv)
    fixture = load_fixture(args.fixture)
    payload = replay(args.root, fixture, timeout=args.timeout, ledger=args.ledger)
    written = write_evidence(payload, args.out or (Path(args.root) / EVIDENCE_JSON),
                             source=str(args.fixture)) if args.out else []
    if args.stdout or not args.out:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    for path in written:
        print('written: ' + path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
