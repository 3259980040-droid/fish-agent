"""每天一次的整理：清理记忆、写当天小结、从当天群图里挑表情。

三件事本来是一件事——她自己的判断就够了：记忆留哪些、当天怎么记、群里
哪张图值得收进表情库。所以这里只做编排，能力全部复用现有的：

* 清理：MemoryMaintenance 的 weekly 整理（重复合并成候选、过期停用，从不
  真删，主人手存或认可过的事实由那段代码自己排除在外）；
* 小结：memory_digest.daily，一天一次、force 才重写；
* 表情：读 qq.db 里当天的 image 段，按地址聚合同一张，把候选图交给模型挑
  0-3 张并给理由，再走 EmojiStore.add 入库（同一张只存一次）。

只新增一张表 daily_curate，落在 data/qq/memory.db 里，与 memory_digest 同一
个库、同一种写法，不新开数据库文件。判定与理由同时写进 governance 的审计
（kind=emoji_curate，含消息号与理由），页面要的最近一次结果由 latest() 读。
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path

from storage import database, role_dir

# 日期口径与小结完全一致，免得同一天在小结和整理里被写成两行。
from memory_digest import _day, _readonly

DAY_HOUR = 4          # 凌晨四点以后才算“今天这一次”，与维护循环的每日窗口一致
MAX_CANDIDATES = 6    # 一天最多把几张图送去判定：一次模型调用看 6 张
MAX_ATTEMPTS = 12     # 一天最多试取这么多张：地址过期的往下顺延，不无限翻旧图
MAX_PICKS = 3         # 一天最多入库几张
MAX_LISTED = 20       # 记录里每类最多留几条明细，不把整天的图灌进页面

TABLE = '''CREATE TABLE IF NOT EXISTS daily_curate(
    day TEXT PRIMARY KEY, result TEXT NOT NULL, created REAL NOT NULL)'''

SYSTEM = (
    '你是群聊表情库的守门人。给你几张今天在群里出现过的图，挑出最适合当群聊表情包的 0 到 3 张。\n'
    '优先：群里反复出现、大家都在用的；能接话、能接嘲讽或卖萌的；画面干净、一眼看懂情绪的。\n'
    '一律不挑：人像和生活照、证件或身份证明、聊天截图或文件截图、看不清内容的、低俗违规的。\n'
    '宁少不多，一张都不合适就返回空数组。每一张必须给出理由——写具体的判断依据（出现过几次、\n'
    '能接什么话、为什么适合当表情），不要写“很好看”这种空话。\n'
    '输出 JSON：{"picks":[{"index":1,"reason":"…","description":"画面与文字的客观描述","tags":["情绪","场景"]}]}'
)

EMPTY = '今天群里没有出现可以当表情的图。'


def _memory_path(root):
    return role_dir(Path(root), 'qq') / 'memory.db'


def _messages_db(root, messages_db=None):
    """当天群消息所在的 qq.db：默认本根，也可指向一份只读的真实库。"""
    return Path(messages_db) if messages_db else role_dir(Path(root), 'qq') / 'qq.db'


def _part(call, *args, **kwargs):
    """跑一段整理；坏掉的一段只留下原因，另外两段照常写进记录。"""
    try:
        return call(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - 页面要看到原因，不是一整片空白
        return {'error': type(exc).__name__ + ': ' + str(exc)[:200]}


def _cleanup(root, now):
    """过期与重复的记忆转成候选：沿用维护任务那一套，不另立判断。"""
    from memory_maintenance import MemoryMaintenance, due_windows
    maintenance = MemoryMaintenance(root)
    window = due_windows(now)['weekly']
    merged, expired, roles, kept = [], [], [], 0
    for role in maintenance._roles() or ['qq']:
        # 公开的 run() 按周窗口去重，每天调用会一直回 already-done，所以这里
        # 直接走它执行的那段整理：判断标准、停用方式、保留规则都是同一份。
        summary = maintenance._weekly(role, window, now)
        roles.append(role)
        kept += int(summary.get('kept') or 0)
        for row in summary.get('collapsed') or []:
            merged.append({'id': row['id'], 'duplicate_of': row.get('duplicate_of'), 'role': role})
        for row in summary.get('deactivated') or []:
            expired.append({'id': row['id'], 'reason': row.get('reason'), 'role': role})
    return {'total': len(merged) + len(expired), 'merged': merged[:MAX_LISTED],
            'expired': expired[:MAX_LISTED], 'kept': kept, 'roles': roles,
            'merged_count': len(merged), 'expired_count': len(expired),
            'note': '只动自动学到的事实：重复的合并成候选、过期的停用，没有删除任何记录；'
                    '主人手存或认可过的事实与约定不在这份整理的范围里。'}


def _groups(path, day):
    """当天群里出现过的图，按地址聚在一起：同一张出现几次都记在一条里。"""
    start = day + ' 00:00:00'
    end = (date.fromisoformat(day) + timedelta(days=1)).isoformat() + ' 00:00:00'
    conn = _readonly(path)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT id,ts,chat_id,sender_id,segments FROM messages "
            "WHERE ts>=? AND ts<? AND chat_id LIKE 'group:%' AND segments LIKE '%image%' "
            "ORDER BY id", (start, end)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    groups = {}
    for row in rows:
        try:
            segments = json.loads(row['segments'] or '[]')
        except ValueError:
            continue
        for segment in segments if isinstance(segments, list) else []:
            if not isinstance(segment, dict) or segment.get('type') != 'image':
                continue
            key = str(segment.get('url') or segment.get('path') or '').strip()
            if not key:
                continue  # 没有地址也没有本地文件的图，取不到也不猜
            item = groups.setdefault(key, {'key': key, 'url': str(segment.get('url') or ''),
                                           'path': str(segment.get('path') or ''),
                                           'alt': str(segment.get('alt') or ''),
                                           'chat': str(row['chat_id']), 'messages': [],
                                           'senders': [], 'last': str(row['ts'])})
            item['messages'].append(int(row['id']))
            item['senders'].append(str(row['sender_id'] or ''))
            item['last'] = max(item['last'], str(row['ts']))
    return list(groups.values())


def _ranked(groups):
    """排序：群里用得多的在前，表情类先于普通图，同分取最近的。

    最近的排前面是实用考虑：QQ 图片地址会过期，先看还能取到的那几张。
    """
    def score(group):
        sticker = 2 if group['alt'] == '[动画表情]' else (1 if group['alt'].startswith('[') else 0)
        return (-len(group['messages']), -sticker)
    return sorted(sorted(groups, key=lambda group: group['last'], reverse=True), key=score)


def _fetch(root, groups, workdir):
    """把候选图取到本地：走 QQ 附件那条下载与本地文件路径，不另写下载器。"""
    from multimodal import MediaService
    media = MediaService(root, 'qq')
    fetched = []
    for index, group in enumerate(groups):
        if len(fetched) >= MAX_CANDIDATES:
            break
        segment = {'type': 'image', 'url': group['url']} if group['url'] else {'type': 'image', 'path': group['path']}
        try:
            path = media.attachment_path(segment, Path(workdir) / ('c' + str(index)))
        except Exception as exc:  # noqa: BLE001 - 地址过期、截图过大都只是“这张没取到”
            group['miss'] = type(exc).__name__ + ': ' + str(exc)[:120]
            continue
        try:
            digest, _suffix = __import__('emoji_store').EmojiStore.inspect(path)
        except Exception as exc:  # noqa: BLE001 - 解不开的图不是表情
            group['miss'] = '不是可读的图片：' + str(exc)[:120]
            continue
        fetched.append({**group, 'file': path, 'digest': digest})
    return fetched


def _picks(gateway, root, offered, day):
    """一次模型调用：看图、挑 0-3 张、给理由。"""
    from multimodal import MediaService
    media = MediaService(root, 'qq')
    lines = [f'今天是 {day}。候选图按顺序给出，每张都标了它今天在群里出现的次数和来源消息号：']
    for index, item in enumerate(offered, 1):
        messages = '、'.join(str(value) for value in item['messages'][:6])
        alt = item['alt'] or '无标注'
        lines.append(f'第 {index} 张：出现 {len(item["messages"])} 次，消息号 {messages}，平台标注「{alt}」')
    content = [{'type': 'text', 'text': '\n'.join(lines)}]
    content += [{'type': 'image_url', 'image_url': {'url': media._image_uri(item['file'])}} for item in offered]
    reply = gateway.json(SYSTEM, content, origin='emoji-curate')
    result = []
    for pick in reply.get('picks') or []:
        if len(result) >= MAX_PICKS:
            break
        if not isinstance(pick, dict):
            continue
        reason = str(pick.get('reason') or '').strip()
        try:
            index = int(pick.get('index'))
        except (TypeError, ValueError):
            continue
        if not reason or not 1 <= index <= len(offered):
            continue  # 没有理由的挑选不算数
        result.append({'index': index, 'reason': reason[:300],
                       'description': str(pick.get('description') or '').strip()[:300],
                       'tags': [str(tag)[:20] for tag in (pick.get('tags') or []) if str(tag).strip()][:6]})
    return result


def _emoji(root, day, *, gateway=None, messages_db=None):
    """当天的图 → 候选 → 模型挑 → 入库，每一步都留下看了几张、为什么。"""
    from emoji_store import EmojiStore
    from governance import Governance
    store = EmojiStore(root)
    groups = _groups(_messages_db(root, messages_db), day)
    seen = sum(len(group['messages']) for group in groups)
    ranked = _ranked(groups)[:MAX_ATTEMPTS]
    workdir = role_dir(Path(root), 'qq') / 'media' / 'curate'
    workdir.mkdir(parents=True, exist_ok=True)
    fetched = _fetch(root, ranked, workdir) if ranked else []
    known = {row['id'] for row in store.list()}
    skipped, offered, taken = [], [], set()
    for item in fetched:
        if item['digest'] in known:
            skipped.append({'digest': item['digest'], 'messages': item['messages'],
                            'why': '库里已经有这张，不再送判定也不重复存'})
        elif item['digest'] in taken:
            skipped.append({'digest': item['digest'], 'messages': item['messages'],
                            'why': '同一次里同一张图出现两次，只算一张'})
        else:
            taken.add(item['digest'])
            offered.append(item)
    for item in ranked:
        if item.get('miss'):
            skipped.append({'messages': item['messages'], 'why': '这张没取到：' + item['miss']})
    record = {'seen': seen, 'groups': len(groups), 'candidates': len(ranked),
              'fetched': len(fetched), 'offered': len(offered), 'stored': [],
              'skipped': skipped[:MAX_LISTED], 'picked': 0, 'note': ''}
    if not groups:
        record['note'] = EMPTY
        return record
    if not offered:
        record['note'] = '今天这些图没有新的可收：库里有的、取不到的或重复的都已跳过。'
        return record
    if gateway is None:
        from model_gateway import ModelGateway
        gateway = ModelGateway(root, 'qq')
    picks = _picks(gateway, root, offered, day)
    record['picked'] = len(picks)
    audit = Governance(root)
    for pick in picks:
        item = offered[pick['index'] - 1]
        messages = item['messages'][:6]
        note = pick['description'] or pick['reason']
        try:
            added = store.add(item['file'], note, '，'.join(pick['tags']),
                             'group-curate:' + day, automatic=True)
        except Exception as exc:  # noqa: BLE001 - 库满、内容未过检查都只记一笔
            record['skipped'].append({'messages': messages, 'why': '没能入库：' + str(exc)[:120]})
            continue
        if added.get('duplicate'):
            record['skipped'].append({'digest': item['digest'], 'messages': messages,
                                      'why': '表情库里已有这张，未重复存'})
            continue
        if not added.get('ok'):
            record['skipped'].append({'messages': messages,
                                      'why': '没有存：' + ('主人关掉了自动收集' if added.get('disabled') else '这张被标记为不收')})
            continue
        record['stored'].append({'asset': added['id'], 'messages': messages, 'reason': pick['reason'],
                                 'description': note, 'tags': pick['tags']})
        # 审计里带消息号与理由，事后能回答“为什么存了这张”。
        audit.audit('emoji_curate', '消息' + '、'.join(str(value) for value in messages) +
                    ' 存入表情 ' + added['id'][:12] + '：' + pick['reason'])
    record['stored'] = record['stored'][:MAX_PICKS]
    record['note'] = '看过 %d 张（%d 张送去判定），入库 %d 张。' % (seen, len(offered), len(record['stored']))
    if not picks:
        record['note'] += '没有一张够格当表情，今天一张都没存。'
    audit.audit('emoji_curate', '%s 看过 %d 张，判定 %d 张，入库 %d 张'
                % (day, seen, len(offered), len(record['stored'])))
    return record


def _summary(digest):
    """小结那一段在页面上的样子：正文就在 digests 里，这里只留状态。"""
    if not isinstance(digest, dict) or digest.get('error'):
        return {'ok': False, 'error': (digest or {}).get('error', '没有结果')}
    text = str(digest.get('text') or '')
    return {'ok': True, 'day': digest.get('day'), 'skipped': bool(digest.get('skipped')),
            'chars': len(text), 'text': text[:200], 'model_calls': int(digest.get('model_calls') or 0)}


def run(root, day=None, *, force=False, gateway=None, messages_db=None, now=None):
    """一天一次：清理记忆、写小结、挑表情，然后留下一份可读的结果。"""
    root = Path(root)
    day = _day(day)
    now = time.time() if now is None else float(now)
    path = _memory_path(root)
    with database(path) as db:
        db.execute(TABLE)
        existing = db.execute('SELECT result FROM daily_curate WHERE day=?', (day,)).fetchone()
    if existing is not None and not force:
        try:
            return {**json.loads(existing['result']), 'skipped': True}
        except ValueError:
            pass  # 旧行坏了就重做一次，不把页面卡在一段读不出来的文本上
    from memory_digest import daily as memory_digest_daily
    cleaned = _part(_cleanup, root, now)
    digest = _part(memory_digest_daily, root, day, force=force)
    emoji = _part(_emoji, root, day, gateway=gateway, messages_db=messages_db)
    created = time.time()
    record = {'day': day, 'created': created,
              'created_text': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(created)),
              'cleaned': cleaned, 'digest': _summary(digest), 'emoji': emoji, 'skipped': False}
    with database(path) as db:
        db.execute(TABLE)
        db.execute('INSERT INTO daily_curate(day,result,created) VALUES(?,?,?) '
                   'ON CONFLICT(day) DO UPDATE SET result=excluded.result,created=excluded.created',
                   (day, json.dumps(record, ensure_ascii=False), created))
    return record


def tick(root, now=None):
    """调度入口：凌晨四点以后、当天还没做过才真的跑一次。"""
    now = time.time() if now is None else float(now)
    day = _day(time.strftime('%Y-%m-%d', time.localtime(now)))
    if time.localtime(now).tm_hour < DAY_HOUR:
        return {'day': day, 'skipped': '还没到凌晨四点'}
    path = _memory_path(root)
    with database(path) as db:
        db.execute(TABLE)
        done = db.execute('SELECT 1 FROM daily_curate WHERE day=?', (day,)).fetchone()
    if done:
        return {'day': day, 'skipped': '今天已经整理过'}
    return run(root, day, now=now)


def latest(root):
    """最近一次整理的结果，页面 memory.overview 里的 daily_curate 字段。"""
    path = _memory_path(root)
    if not path.is_file():
        return None
    with database(path) as db:
        db.execute(TABLE)
        row = db.execute('SELECT result FROM daily_curate ORDER BY day DESC LIMIT 1').fetchone()
    if row is None:
        return None
    try:
        return json.loads(row['result'])
    except ValueError:
        return None
