"""Read-only production snapshot; real Fish replay, real model, fake QQ only.

Experimental scheduling/reranking live in this file. No harness file is changed.
M0 is a controlled serial replay of existing entry points, not a live traffic test.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import sqlite3
import sys
import threading
import time
from contextlib import ExitStack, closing
from datetime import datetime
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path[:0] = [str(SOURCE), str(SOURCE / 'harness'), str(SOURCE / 'tools')]
import dependencies  # noqa: F401,E402
from storage import database, read_json, write_json
from model_gateway import ModelGateway, ModelLedger
from governance import Governance
from group_policy import GroupPolicy
from memory_store import MemoryStore
from social_context import SocialContext
from tool_host import ToolHost, peek_says, take_said
from qq_agent_loop import QQAgentLoop
from rehearse_private import VirtualClock
import fish_chat
import group_decide

OWNER, BOT = '10001001', '90009000'
CHATS = {'P': 'private:' + OWNER, 'A': 'group:20002001', 'B': 'group:20002002'}
PEOPLE = {'P': OWNER, 'A': '30003001', 'B': '30003002'}
SEED_TIME = datetime(2026, 10, 24, 12, 0).timestamp()
SOURCE_FILES = ('harness/fish_chat.py', 'harness/qq_agent_loop.py', 'harness/group_round.py',
                'harness/group_policy.py', 'harness/group_decide.py', 'harness/tool_host.py',
                'harness/memory_store.py', 'harness/owner_private.py')


def digest(value):
    data = value if isinstance(value, bytes) else str(value).encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def ro_count(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        return db.execute('SELECT COUNT(*) FROM requests').fetchone()[0]


def corpus():
    # Every account/character/message here is invented. No history is exported.
    entries = [
        ('P', '昨晚说的夜航项目，封面最后留蓝色。', '', False),
        ('A', '阿柚：我的夜航练习稿是红色封面，别记成蓝色。', '', False),
        ('B', '阿柚：我不是隔壁群的阿柚，我的练习稿是绿色封面。', '', False),
        ('P', '还有那张时间表，先别丢，我一会接着说。', '', False),
        ('A', '路人甲：阿柚，你那张红封面的字能再大点。', 'A01', False),
        ('B', '', '', False),  # pure image, without a caption
        ('A', '小鱼，你看阿柚和路人甲在讨论谁的封面？', 'A02', True),
        ('B', '路人乙：这张图我只发给这个群了。', 'B02', False),
        ('A', '阿柚：路人甲，我只换标题字体，颜色还是红色。', 'A02', False),
        ('P', '我这里另一个代号是折纸，不是群里的封面练习。', '', False),
        ('B', '安静了好一会，大家是不是都在忙。', '', False),
        ('P', '折纸的事先不展开，还是先说夜航。', '', False),
        ('A', '路人甲：好，我说的就是你那份，不是在问小鱼。', 'A04', False),
        ('B', '大家歇着就歇着，不用为了说话硬找话题。', '', False),
        ('A', '阿柚：红色那份是我的，路人甲负责看标题。', '', False),
        ('P', '夜航的封面和时间表都是我的项目，别和群友的稿子混了。', '', False),
        ('B', '小鱼，刚才发图的是谁？我们这个阿柚的封面是什么颜色？', 'B02', True),
        ('P', '接上次夜航那个话题：我最后定了哪种封面？你想说再说。', 'P01', False),
        ('A', '小鱼，我们刚才是谁对谁说话？阿柚的封面定了什么颜色？', 'A06', True),
        ('B', '图里是红三角和蓝圆，还是蓝三角和红圆？不确定就别猜。', 'B02', False),
        ('P', '时间表先放着，封面这件事我想接着聊。', 'P06', False),
    ]
    counts = dict.fromkeys(CHATS, 0)
    result = []
    for index, (place, text, reply, mention) in enumerate(entries):
        counts[place] += 1
        short = place + str(counts[place]).zfill(2)
        # All message identifiers are numeric, as required by the existing group tool.
        mid = str(700000000 + {'P': 1, 'A': 2, 'B': 3}[place] * 1000 + counts[place])
        sender = PEOPLE[place]
        display = '主人' if place == 'P' else '阿柚'
        if text.startswith('路人甲'):
            sender, display = '30003011', '路人甲'
        elif text.startswith('路人乙'):
            sender, display = '30003012', '路人乙'
        result.append({'id': short, 'message_id': mid, 'place': place, 'chat_id': CHATS[place],
                       'sender': {'id': sender, 'display_name': display}, 'text': text,
                       'reply_alias': reply, 'mentions_bot': mention, 'arrival': index,
                       'epoch': index // 7})
    by_alias = {row['id']: row['message_id'] for row in result}
    for row in result:
        row['reply_to'] = by_alias.get(row['reply_alias'], '')
    assert len(result) == 21 and set(counts.values()) == {7}
    return result


def snapshot(source, target):
    target.mkdir(parents=True)
    # Copy configuration and encrypted credentials; never put credentials in reports.
    shutil.copytree(source / 'harness', target / 'harness',
                    ignore=shutil.ignore_patterns('*.py', '__pycache__', 'runtime', '*.log'))
    (target / 'channels/fish_qq').mkdir(parents=True)
    shutil.copy2(source / 'channels/fish_qq/config.json', target / 'channels/fish_qq/config.json')
    copied = []
    for path in sorted((source / 'data').rglob('*.db')):
        relative = path.relative_to(source)
        dest = target / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as src, \
                closing(sqlite3.connect(dest)) as dst:
            src.backup(dst)
        copied.append(relative.as_posix())
    for relative in ('homes', 'qq-work', 'knowledge'):
        base = source / relative
        if base.exists():
            # Only prompt/config texts, no external project trees or junctions.
            for file in base.glob('*.md'):
                dest = target / relative / file.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(file, dest)
    return copied


def source_profile(source=SOURCE):
    config = read_json(source / 'channels/fish_qq/config.json')
    groups = [str(x) for x in config['allowlist'].get('group_ids', [])]
    if not groups:
        raise ValueError('没有可复用的真实授权群策略')
    with closing(sqlite3.connect((source/'data/social/group-policy.db').as_uri()+'?mode=ro',uri=True)) as db:
        row = db.execute('SELECT config FROM groups WHERE chat=?', ('group:'+groups[0],)).fetchone()
        if row is None:
            raise ValueError('授权群没有已存策略，不能凭空制造基线')
        group_config = json.loads(row[0])
    return config, group_config


def prepare(root, config, group_config, records):
    """Only the disposable copy is mutated: pseudonyms, synthetic timeline/memories."""
    old_owner = str(config['allowlist'].get('owner_id', ''))
    rewritten = copy.deepcopy(config)
    rewritten['allowlist'].update(owner_id=OWNER, group_ids=[CHATS['A'][6:], CHATS['B'][6:]])
    write_json(root / 'channels/fish_qq/config.json', rewritten)
    with database(root / 'data/social/context.db') as db:
        db.execute('UPDATE owner_routes SET owner=? WHERE owner=?', (OWNER, old_owner))
        db.execute('UPDATE approvals SET owner=? WHERE owner=?', (OWNER, old_owner))
    # These identities belong to live sessions, not this independent process.
    # The untouched-copy pilot is retained separately; all comparative trials
    # start without imported runtime credentials and rebind through real ingress.
    with database(root / 'data/tool-host.db') as db:
        for table in ('identities','identity_modes','chat_says','chat_say_turns','chat_previews',
                      'chat_replies','emoji_choices','group_contexts'):
            if db.execute("SELECT 1 FROM sqlite_master WHERE name=?",(table,)).fetchone():
                db.execute('DELETE FROM '+table)
    # Retain actual memory and persona. Remove historical chat only in this copy,
    # otherwise the experiment would export real third-party conversation content.
    with database(root / 'data/qq/qq.db') as db:
        for table in ('messages', 'runtime_turns', 'incidents', 'conversations'):
            db.execute('DELETE FROM '+table)
    with database(root / 'data/social/group-policy.db') as db:
        for table in ('groups', 'observations', 'dedup', 'reservations', 'summaries', 'clears',
                      'group_energy', 'relay_bypass'):
            if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                db.execute('DELETE FROM '+table)
    # Copied send ledgers must not make an unrelated old send count as this fixture.
    with database(root / 'data/social/owner-control.db') as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='sends'").fetchone():
            db.execute('DELETE FROM sends')
    policy = GroupPolicy(root)
    for place in ('A', 'B'):
        policy.configure(CHATS[place], group_config)
    revision = group_decide._owner_revision(root)
    persona = SocialContext(root).persona(revision)
    memory = MemoryStore(root, 'qq')
    seeds = [
        ('夜航项目封面最后保留蓝色，时间表稍后再接着聊。', 'owner', 'qq:'+OWNER, 'P01/P06'),
        ('夜航项目公开讨论涉及封面和时间表，具体方案需要问清。', 'public', '', '公开素材'),
        ('夜航练习稿是红色封面，阿柚负责颜色，路人甲看标题。', 'chat:'+CHATS['A'], 'qq:'+PEOPLE['A'], 'A06'),
        ('本群阿柚的夜航练习稿为绿色封面，和隔壁群阿柚不是同一个人。', 'chat:'+CHATS['B'], 'qq:'+PEOPLE['B'], 'B01'),
    ]
    memory_ids = []
    for text, scope, subject, source in seeds:
        value = memory.save(text, category='多场所实验合成事实', scope=scope,
                            source='manual', source_text='虚构实验记录 '+source,
                            subject=subject, evidence='owner_quote' if scope == 'owner' else 'manual')
        memory_ids.append(value['id'])
    from PIL import Image, ImageDraw
    image = Image.new('RGB', (240, 150), 'white')
    draw = ImageDraw.Draw(image)
    draw.polygon([(25, 125), (65, 20), (105, 125)], fill='red')
    draw.ellipse((130, 30, 225, 125), fill='blue')
    image_path = root / 'artifacts' / 'synthetic-shapes.png'
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(image_path)
    return persona, memory_ids, image_path


class FakeChannel:
    def __init__(self):
        self.bot = {'id': BOT}
        self.sent = []
        self.turn = None

    async def action(self, name, args, timeout=0):
        if name not in ('send_message', 'get_image', 'get_forward_msg'):
            raise PermissionError('实验假通道不执行动作 '+str(name))
        if name != 'send_message':
            return {'ok': False, 'error': {'code': '合成语料没有远端资源'}}
        receipt = {'name': name, 'args': copy.deepcopy(args), 'at': time.monotonic(),
                   'turn': self.turn, 'message_id': 'fake-'+str(len(self.sent)+1)}
        self.sent.append(receipt)
        return {'ok': True, 'payload': {'message_id': receipt['message_id']}}


def payload(row, image_path):
    # This is a local fixture, not a QQ CDN resource. attachment_path() treats
    # a nonempty url as a download, so use its existing local-path branch.
    segments = ([{'type': 'image', 'file': str(image_path),
                  'path': str(image_path)}] if row['id'] == 'B02' else
                [{'type': 'text', 'text': row['text']}])
    return {'chat_type': 'private' if row['place'] == 'P' else 'group',
            'chat_id': row['chat_id'], 'sender': row['sender'],
            'message_id': row['message_id'], 'segments': segments,
            'reply_to': row['reply_to'], 'mentions_bot': row['mentions_bot'], 'reply_to_bot': False}


class FairOrder:
    """Experiment-only weighted lottery + service debt, not a production rule."""
    def __init__(self, seed):
        self.rng = random.Random(seed)
        self.service = dict.fromkeys(CHATS, 0.0)
        self.weights = {'P': 1.35, 'A': 1.0, 'B': 1.0}

    def order(self, places):
        pending, result = list(places), []
        while pending:
            minimum = min(self.service[p] / self.weights[p] for p in CHATS)
            weights = [self.weights[p] / (1.0 + max(0.0, self.service[p]/self.weights[p]-minimum))
                       for p in pending]
            chosen = self.rng.choices(pending, weights=weights, k=1)[0]
            result.append(chosen)
            pending.remove(chosen)
        return result


def install_io_guard(root, out, copied_databases, allowed_hosts):
    allowed = {str((root / p).resolve()).casefold() for p in copied_databases}
    counts = {'write_checks': 0, 'database_checks': 0, 'blocked': []}
    roots = (root.resolve(), out.resolve())

    def inside(path):
        value = Path(path).resolve()
        return any(value == base or base in value.parents for base in roots)

    def audit(event, args):
        if event == 'sqlite3.connect':
            name = str(args[0])
            if name.startswith('file:'):
                from urllib.parse import unquote, urlparse
                name = unquote(urlparse(name).path).lstrip('/') if os.name == 'nt' else unquote(urlparse(name).path)
            counts['database_checks'] += 1
            if str(Path(name).resolve()).casefold() not in allowed:
                raise PermissionError('实验禁止打开未复制的数据库')
        elif event == 'open' and not isinstance(args[0], int):
            mode, flags = args[1], args[2]
            writing = (isinstance(mode, str) and any(x in mode for x in 'wax+')) or (
                isinstance(flags, int) and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)))
            if writing:
                counts['write_checks'] += 1
                if not inside(args[0]):
                    counts['blocked'].append('write-outside-experiment')
                    raise PermissionError('实验拒绝写入临时根与结果目录之外')
        elif event in ('subprocess.Popen', 'os.system'):
            counts['blocked'].append('subprocess')
            raise PermissionError('实验不启动真实执行服务/电脑任务')
        elif event == 'socket.getaddrinfo':
            host = (args[0].decode('ascii') if isinstance(args[0],bytes) else str(args[0])).lower()
            if host not in allowed_hosts:
                counts['blocked'].append('network-other-than-model:'+host)
                raise PermissionError('实验仅允许已配置模型提供方网络')
    sys.addaudithook(audit)
    return counts


async def replay(args, root, persona, image_path, rows, out):
    host = ToolHost(root)
    loop = QQAgentLoop(root)
    channel = FakeChannel()
    loop.channel = channel
    loop.group_ids = {CHATS['A'][6:], CHATS['B'][6:]}
    loop.group_mode = 'all'
    loop.conversation = loop.store.get_or_create_conversation(CHATS['P'], 'qq')
    contexts = SocialContext(root)
    selected = contexts.owner_space(OWNER)
    pconv = loop.store.get_or_create_conversation('test:'+selected['id'], 'qq') if selected else loop.conversation
    memory = MemoryStore(root, 'qq')
    trace, reorders, decisions, tool_results = [], [], [], []
    original_respond = fish_chat.respond
    original_chat = ModelGateway.chat
    original_call = ModelGateway.call
    original_recall = MemoryStore.recall
    original_reserve = ModelLedger.reserve
    reserve_calls = []
    active = {'place': None, 'epoch': None, 'row': None, 'offered_at': None}
    with database(root/'data/qq/governance.db') as db:
        audit_start=db.execute('SELECT COALESCE(MAX(id),0) FROM audit').fetchone()[0]
    fair = FairOrder(2718 + args.repeat)
    source_mid = {r['message_id']: r['id'] for r in rows}

    def chat(self, messages, **kwargs):
        row = {'origin': kwargs.get('origin'), 'model': self.model, 'start': time.monotonic(),
               'place': active['place'], 'epoch': active['epoch'],
               'request_wait_ms':round((time.monotonic()-active['offered_at'])*1000,2),
               'fixture_ids': sorted({source_mid[x] for x in source_mid if x in json.dumps(messages,ensure_ascii=False)}),
               'image_blocks': sum(1 for m in messages if isinstance(m.get('content'),list)
                                   for b in m['content'] if b.get('type')=='image_url'),
               'input_chars': len(json.dumps(messages,ensure_ascii=False))}
        trace.append(row)
        try:
            value = original_chat(self, messages, **kwargs)
            row.update(finish=value.finish_reason, usage=value.usage,
                       text=value.text, tools=[{'name': c['function']['name'],
                                               'arguments': c['function'].get('arguments','')} for c in value.tool_calls])
            return value
        except Exception as exc:
            row['error'] = str(exc)[:250]
            raise
        finally:
            row['elapsed_ms'] = round((time.monotonic()-row['start'])*1000,2)

    def call(self,messages,**kwargs):
        row={'origin':kwargs.get('origin'),'model':self.model,'start':time.monotonic(),
             'place':active['place'],'epoch':active['epoch'],'kind':'json-or-background',
             'request_wait_ms':round((time.monotonic()-active['offered_at'])*1000,2),
             'image_blocks':sum(1 for m in messages if isinstance(m.get('content'),list)
                                for b in m['content'] if b.get('type')=='image_url'),
             'fixture_ids':sorted({source_mid[x] for x in source_mid if x in json.dumps(messages,ensure_ascii=False)}),
             'input_chars':len(json.dumps(messages,ensure_ascii=False))}
        trace.append(row)
        try:
            value=original_call(self,messages,**kwargs)
            row['text']=str(value)
            return value
        except Exception as exc:
            row['error']=str(exc)[:250]
            raise
        finally:
            row['elapsed_ms']=round((time.monotonic()-row['start'])*1000,2)

    def reserve(self, origin):
        value = original_reserve(self, origin)
        reserve_calls.append({'id': value, 'origin': origin})
        return value

    def recall(self, query='', **kwargs):
        value = original_recall(self, query, **kwargs)
        results = value['results'] if isinstance(value,dict) else value
        if args.mode in ('M1','M3') and results:
            old = [r['id'] for r in results]
            bases = [float(r.get('relevance',r.get('lexical',r.get('score',0))) or 0) for r in results]
            span = max(bases)-min(bases)
            scored = []
            for row, base in zip(results,bases):
                normalized = (base-min(bases))/span if span > 1e-9 else 0.5
                relation = 0.05 if row.get('scope') == 'owner' and row.get('subject') == 'qq:'+OWNER else 0.0
                unfinished = 0.02 if row.get('kind') in ('event','promise') and row.get('action')=='open' else 0.0
                scored.append((normalized+relation+unfinished,row))
            # Stable sort and no visibility expansion, no new cutoff.
            results[:] = [r for _,r in sorted(scored,key=lambda x:x[0],reverse=True)]
            reorders.append({'place': active['place'], 'query': str(query)[:100],
                             'before': old, 'after': [r['id'] for r in results],
                             'scope': kwargs.get('scope'), 'changed':old != [r['id'] for r in results]})
        return value

    def bind(_root, role, session, scope='owner', sender='', desktop=False, *, chat_mode=''):
        assert Path(_root).resolve() == root.resolve()
        return host.bind(role, session, scope, sender, desktop, chat_mode)

    def tool(_root, role, session, name, arguments, call_id, timeout):
        start = time.monotonic()
        try:
            value = host.execute(role, session, name, arguments, call_id)
            tool_results.append({'place': active['place'], 'name': name, 'arguments':arguments,
                                 'result':value, 'elapsed_ms':round((time.monotonic()-start)*1000,2)})
            return value
        except Exception as exc:
            tool_results.append({'place':active['place'],'name':name,'arguments':arguments,'error':str(exc)})
            raise

    def respond(*pos, **kw):
        place = active['place']
        before = len(trace)
        if args.mode == 'M4':
            seen = [r for r in rows if r['epoch'] <= active['epoch']]
            shared = '\n'.join(f"[{r['id']} 场所{r['place']} {r['chat_id']} {r['sender']['display_name']}({r['sender']['id']}) 引用{r['reply_alias'] or '无'}] {r['text'] or '[纯图]'}" for r in seen)
            kw['references'] += '\n[实验同语料多场所概况，不是必须发言指令]\n'+shared
            if active['epoch'] == 2:
                kw['current'] = [*kw['current'], {'type':'text','text':
                    '[实验能力问题，新增输入条件] 你可以选择在同一回合对我的私聊和群A各说一句，'
                    '群A目标为 '+CHATS['A']+'。如果选择发，请调用已有发送工具；仍允许不发送。'}]
        record = {'place':place,'epoch':active['epoch'],'trigger':active['row']['id'],
                  'history_source':kw['history_source'], 'scope':kw['scope'],
                  'persona_id':kw['persona']['id'], 'persona_body_sha256':digest(kw['persona']['body']),
                  'history_count':len(kw['history']), 'current':kw['current'],
                  'references':kw['references'], 'memory':kw['memory'],
                  'model_trace_from':before, 'started':time.monotonic()}
        decisions.append(record)
        try:
            value = original_respond(*pos, **kw)
            record['output'] = value
            return value
        except Exception as exc:
            record['error'] = type(exc).__name__+': '+str(exc)
            raise
        finally:
            record['model_trace_to']=len(trace)
            record['elapsed_ms']=round((time.monotonic()-record['started'])*1000,2)

    def cross_transport(target, text, key, reply_to=''):
        # BotControl is still responsible for its own ledger and receipts.
        return asyncio.run(channel.action('send_message', {'chat_id':target,
            'segments':[{'type':'text','text':text}], 'idempotency_key':key,'reply_to':reply_to}))

    processed, offered = [], []
    skipped_streak = dict.fromkeys(CHATS,0)
    maximum_streak = dict.fromkeys(CHATS,0)
    clock = VirtualClock()
    clock.install()
    try:
        with ExitStack() as patches:
            patches.enter_context(patch.object(fish_chat,'respond',respond))
            patches.enter_context(patch('tool_host.bind_session',bind))
            patches.enter_context(patch('fish_chat.call_tool',tool))
            patches.enter_context(patch.object(ModelGateway,'chat',chat))
            patches.enter_context(patch.object(ModelGateway,'call',call))
            patches.enter_context(patch.object(ModelLedger,'reserve',reserve))
            patches.enter_context(patch.object(MemoryStore,'recall',recall))
            patches.enter_context(patch('bot_control.BotControl._channel_transport',staticmethod(cross_transport)))
            for epoch in range(3):
                clock.set(SEED_TIME+epoch*900)
                batch = [r for r in rows if r['epoch']==epoch]
                arrival_time = time.monotonic()
                latest = {}
                mids = {}
                for row in batch:
                    latest[row['place']] = row
                    if row['place']=='P':
                        mid = loop.store.record_message(pconv['id'],CHATS['P'],OWNER,'inbound',row['message_id'],
                            row['reply_to'] or None,payload(row,image_path)['segments'],row['text'],'queued')
                        mids[row['id']]=mid
                    elif row is not [r for r in batch if r['place']==row['place']][-1]:
                        event = payload(row,image_path)
                        GroupPolicy(root).observe(event)
                        loop._fish_record(row['chat_id'],event,text=row['text'],direction='inbound',status='observed',
                                          reply_to=row['reply_to'])
                order = sorted(latest, key=lambda p: next(r['arrival'] for r in batch if r['place']==p))
                if args.mode in ('M2','M3'):
                    order = fair.order(order)
                if args.mode == 'M4':
                    # M4 is an owner-private cross-target capability comparison,
                    # not a new shared planner or replacement for the group worker.
                    order = ['P']
                for position,place in enumerate(order):
                    for pending in latest:
                        if pending not in order[:position+1]:
                            skipped_streak[pending]+=1
                            maximum_streak[pending]=max(maximum_streak[pending],skipped_streak[pending])
                    skipped_streak[place]=0
                    row = latest[place]
                    active.update(place=place,epoch=epoch,row=row,offered_at=arrival_time)
                    channel.turn = f"{args.mode}-r{args.repeat}-e{epoch}-{place}"
                    before_calls, before_sends, before_decisions = len(trace),len(channel.sent),len(decisions)
                    start = time.monotonic()
                    offer = {'epoch':epoch,'place':place,'order':position,'trigger':row['id'],
                             'wait_ms':round((start-arrival_time)*1000,2)}
                    offered.append(offer)
                    if place=='P':
                        item={'message_id':mids[row['id']],'payload':payload(row,image_path),
                              'text':row['text'],'space_id':selected['id'] if selected else '',
                              'conversation':pconv}
                        await loop._process(item)
                    else:
                        await loop._fish_reply(payload(row,image_path),CHATS[place],threading.Event())
                    elapsed = time.monotonic()-start
                    fair.service[place]+=elapsed
                    processed.append({**offer,'elapsed_ms':round(elapsed*1000,2),
                        'model_calls':len(trace)-before_calls,'decisions':len(decisions)-before_decisions,
                        'fake_sends':len(channel.sent)-before_sends,
                        'outcome':'已交假通道' if len(channel.sent)>before_sends else
                                  '模型处理后未发送' if len(decisions)>before_decisions else '未进入主回复生成'})
                    print(json.dumps({'mode':args.mode,'repeat':args.repeat,'epoch':epoch,'place':place,
                                      'calls':len(trace)-before_calls,'fake_sends':len(channel.sent)-before_sends},ensure_ascii=False),flush=True)
    finally:
        clock.restore()
    with database(root/'data/qq/governance.db') as db:
        audits=[dict(r) for r in db.execute('SELECT * FROM audit WHERE id>? ORDER BY id',(audit_start,))]
    return {'processed':processed,'offered':offered,'max_unconsidered':maximum_streak,
            'decisions':decisions,'model_trace':trace,'tools':tool_results,'memory_reranks':reorders,
            'fake_channel':channel.sent,'reserve_calls':reserve_calls,'audits':audits}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=[f'M{i}' for i in range(6)])
    parser.add_argument('--matrix',action='store_true')
    parser.add_argument('--repeat',type=int,default=None,choices=(1,2,3))
    parser.add_argument('--repeats',type=int,default=None,choices=(3,))
    parser.add_argument('--snapshot',type=Path)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    if args.matrix:
        import subprocess
        from concurrent.futures import ThreadPoolExecutor,as_completed
        args.out.mkdir(parents=True,exist_ok=True)
        before=ro_count(SOURCE/'data/qq/model-usage.db')
        def mode_run(mode):
            command=[sys.executable,'-B',str(Path(__file__).resolve()),'--mode',mode,
                     '--repeats','3','--out',str(args.out)]
            if args.snapshot: command+=['--snapshot',str(args.snapshot)]
            with (args.out/(mode+'.log')).open('w',encoding='utf-8') as log:
                result=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,check=False)
            return {'mode':mode,'exit_code':result.returncode}
        statuses=[]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(mode_run,f'M{i}') for i in range(6)]
            for future in as_completed(futures):
                row=future.result();statuses.append(row)
                print(json.dumps(row,ensure_ascii=False),flush=True)
        write_json(args.out/'运行批次.json',{'runs':statuses,'parallel_modes':2,
            'production_requests_before':before,'production_requests_after':ro_count(SOURCE/'data/qq/model-usage.db'),
            'note':'生产计数受在线服务影响；实验请求按各临时根账本归因'})
        return max(r['exit_code'] for r in statuses)
    if not args.mode:
        parser.error('需要 --mode 或 --matrix')
    if args.repeats:
        if args.repeat:
            parser.error('--repeat 与 --repeats 不能同时使用')
        import subprocess
        args.out.mkdir(parents=True,exist_ok=True)
        statuses=[]
        for rep in (1,2,3):
            command=[sys.executable,'-B',str(Path(__file__).resolve()),'--mode',args.mode,
                     '--repeat',str(rep),'--out',str(args.out)]
            if args.snapshot:
                command+=['--snapshot',str(args.snapshot)]
            statuses.append(subprocess.run(command,check=False).returncode)
        return max(statuses)
    args.repeat=args.repeat or 1
    os.environ['PYTHONDONTWRITEBYTECODE']='1'
    # Keep only model credentials. No child can inherit live QQ connection tokens.
    for key in ('FISH_QQ_TOKEN','NAPCAT_TOKEN','FISH_QQ_URL'):
        os.environ.pop(key,None)
    from rehearse_group import adopt_user_env
    adopt_user_env()
    baseline=args.snapshot.resolve() if args.snapshot else SOURCE
    source_cfg, policy_cfg=source_profile(baseline)
    run_id=f'{args.mode}-r{args.repeat}-{datetime.now():%Y%m%d-%H%M%S}-{os.getpid()}'
    out=args.out.resolve()/run_id
    out.mkdir(parents=True)
    root=SOURCE/'artifacts'/'multi-place-20261024'/run_id/'root'
    before_hash={p:digest((SOURCE/p).read_bytes()) for p in SOURCE_FILES}
    production_count_before=ro_count(SOURCE/'data/qq/model-usage.db')
    copied=snapshot(baseline,root)
    rows=corpus()
    persona, memory_ids,image_path=prepare(root,source_cfg,policy_cfg,rows)
    write_json(out/'语料.json',{'fictional':True,'records':rows})
    from urllib.parse import urlparse
    agent=next(x for x in read_json(root/'harness/agents.json')['agents'] if x['id']=='qq')
    allowed_hosts={urlparse(agent.get('base_url','https://api.deepseek.com')).hostname}
    media=read_json(root/'harness/multimodal.json',{})
    for section in media.values():
        if isinstance(section,dict) and section.get('base_url'):
            allowed_hosts.add(urlparse(section['base_url']).hostname)
    count_before=ro_count(root/'data/qq/model-usage.db')
    with database(root/'data/qq/model-usage.db') as db:
        tool_events_before=db.execute('SELECT COUNT(*) FROM tool_events').fetchone()[0]
    # Tools use the normal tempfile API. Keep their working files inside this
    # replay root instead of blocking valid tool use at the write boundary.
    import tempfile
    temporary=root/'artifacts'/'temporary'
    temporary.mkdir(parents=True,exist_ok=True)
    for key in ('TMP','TEMP','TMPDIR'):
        os.environ[key]=str(temporary)
    tempfile.tempdir=str(temporary)
    guard=install_io_guard(root,out,copied,allowed_hosts)
    report={'mode':args.mode,'repeat':args.repeat,'root':str(root),'real_model':True,
            'baseline_snapshot':str(baseline),'experiment_script_sha256':digest(Path(__file__).read_bytes()),
            'real_qq_sent':False,'model':agent['model'],'persona_id':persona['id'],
            'persona_body_sha256':digest(persona['body']),'group_policy':policy_cfg,
            'synthetic_memory_ids':memory_ids,'copied_databases':copied,
            'differences':[
                '实际配置只在临时副本中映射为虚构账号与两个群；B复用A策略，非生产已授权第二群',
                '真实记忆保留在副本，增加明确合成事实；真实聊天历史仅从副本移除',
                '受控串行回放，每7条一批，非生产并发/壁钟延迟测量；12:00/12:15/12:30虚拟事件时间',
                '工具宿主同进程执行真实bind/execute，不连接常驻工具服务；仅配置模型网络可访问',
                '比较试验不继承线上运行时identities；首轮未清理副本的阻断证据另保留，非生产修复',
                'M5现状已分场所组装，作为同条件重复对照；M4仅主人私聊跨目标能力，额外输入另记'],
            'paid_authorization':'主人在任务后明确授权使用模型，替代原零付费条款'}
    try:
        report.update(asyncio.run(replay(args,root,persona,image_path,rows,out)))
    except Exception as exc:
        report['fatal_error']=type(exc).__name__+': '+str(exc)
        import traceback
        report['traceback']=traceback.format_exc()
    report['guard']=guard
    report['requests_delta']=ro_count(root/'data/qq/model-usage.db')-count_before
    report['production_requests_before']=production_count_before
    # Reading the live database for accounting is permitted only after experiment
    # execution; guard intentionally prevents it. A parent collector reads after.
    report['production_requests_after']='由父进程只读采集；在线增量不归因本实验'
    report['harness_unchanged']={p:before_hash[p]==digest((SOURCE/p).read_bytes()) for p in SOURCE_FILES}
    with database(root/'data/qq/model-usage.db') as db:
        report['ledger_rows']=[dict(r) for r in db.execute('SELECT id,origin,status,input_tokens,output_tokens FROM requests ORDER BY rowid DESC LIMIT ?',
                                                         (report['requests_delta'],))] if report['requests_delta'] else []
        added=db.execute('SELECT COUNT(*) FROM tool_events').fetchone()[0]-tool_events_before
        report['tool_events']=[dict(r) for r in db.execute('SELECT * FROM tool_events ORDER BY rowid DESC LIMIT ?',
                                                         (added,))] if added else []
    # Raw local evidence can contain persona/private recall from the authorized copy;
    # the public comparison report is separately redacted by the collector.
    write_json(out/'结果.json',report)
    print(json.dumps({'result':str(out/'结果.json'),'error':report.get('fatal_error'),
                      'requests':report['requests_delta'],'real_qq_sent':False},ensure_ascii=False),flush=True)
    return 1 if report.get('fatal_error') else 0


if __name__=='__main__':
    raise SystemExit(main())
