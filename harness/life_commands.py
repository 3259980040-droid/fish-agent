"""/生活 面板的命令组：主人在 QQ 私聊里就能看和改她的生活状态。

这些命令和既有的斜杠命令一样跑在模型生成之前：命中就发一条回复并 return True，
省掉一次付费生成；非主人或群聊一律 return False，原样交给后面的分支。

编号 → 记忆 id 的对应只存在本进程的 _LAST_EVENTS 里：重启就没了，重新发 /事件 即可。
"""
from __future__ import annotations
from locations import project_path

import re
import sqlite3
import time
import uuid

# 群开关、引擎、潜水、概率这些读的是 GroupPolicy 那一份配置，和群通道用的是同一条。
ENGINES = {'dsh': '旧群通道', 'shadow': '只算不发', 'fish': '真生成并发送'}
ACTIONS = {'complete': ('complete', '已完成'), 'cancel': ('cancel', '已取消')}
# (键, 类型, 下限, 上限, 单位, 名字, 换算)：换算 60 表示命令里的数字是分钟，存的是秒。
HABITS = {
    '冷却': ('active_cooldown_s', int, 0, 86400, '秒', '主动回复冷却', 1),
    '概率': ('wake_probability', float, 0, 1, '', '唤醒概率', 1),
    '回访': ('followup_s', int, 0, 60, '秒', '回复后回访', 1),
    '上限': ('max_active_hour', int, 0, 120, '条/小时', '每小时主动上限', 1),
    '主动概率': ('proactive_probability', float, 0, 1, '', '主动开口概率', 1),
    '主动安静': ('proactive_idle_s', int, 5, 120, '分钟', '主动开口安静阈值', 60),
    '预筛': ('prescreen_threshold', float, 0, 1, '', '预筛阈值', 1),
    '目标': ('target_replies_per_hour', int, 0, 200, '条/小时', '每小时目标条数', 1),
}
# 主动开口只有开关，没有数字：#/习惯 主动开、/习惯 主动关。
PROACTIVE_SWITCH = {'主动开': True, '主动关': False}
# 精力也只有开关：#/习惯 精力开、/习惯 精力关。
ENERGY_SWITCH = {'精力开': True, '精力关': False}
# 自主回合的六个预算，/群设置 能看能改，和回合本身读的是同一份 GroupPolicy。
# (键, 类型, 下限, 上限, 单位, 名字, 换算)：换算 60 表示命令里的数字是分钟，存的是秒。
# 上下限和 group_policy 的 ROUND_*_RANGE / DIVE_SPAN_RANGE 一致（潜水那两行除以 60）。
GROUP_SETTINGS = {
    '回合': ('round_interval_s', int, 60, 3600, '秒', '自主回合节拍', 1),
    '预算': ('round_budget_chars', int, 200, 4000, '字', '回合读入预算', 1),
    '每小时': ('round_hourly_calls', int, 0, 500, '次', '每小时判断调用上限', 1),
    '每天': ('round_daily_calls', int, 0, 5000, '次', '每天判断调用上限', 1),
    '潜水': ('dive_max_s', int, 0, 1440, '分钟', '单次潜水上限', 60),
    '潜水全天': ('dive_daily_max_s', int, 0, 1440, '分钟', '一天累计潜水上限', 60),
}
GROUP_SETTINGS_USAGE = ('改法：/群设置 回合 180、/群设置 预算 1200、/群设置 每小时 40、'
                        '/群设置 每天 400、/群设置 潜水 240 分钟、/群设置 潜水全天 480 分钟。')
COMMANDS = ('/生活', '/状态', '/群开', '/群关', '/学习开', '/学习关', '/潜水', '/醒来',
            '/事件', '/完成', '/取消', '/习惯', '/群设置', '/彩排', '/歇着', '/回来', '/上线')

# 上一条 /事件 的编号顺序：[(id, text)]。只是进程内存，重启后重新 /事件 就有了。
_LAST_EVENTS: list[tuple[str, str]] = []

HINT = ('下一步：/生活 看面板 · /群开 /群关 听和说 · /学习开 /学习关 学习 · '
        '/潜水 30 · /醒来 · /事件 列事件 · /完成 2 · /取消 2 · '
        '/习惯 看参数（/习惯 冷却 240、/习惯 概率 0.4、/习惯 回访 8、/习惯 上限 2、'
        '/习惯 主动开、/习惯 主动关、/习惯 主动概率 0.15、/习惯 主动安静 20 分钟、'
        '/习惯 预筛 0.5、/习惯 精力开、/习惯 精力关、/习惯 目标 6） · '
        '/群设置 回合参数 · /彩排 排练')


def _text(payload):
    segments = payload.get('segments') or []
    return ''.join(str(s.get('text') or '') for s in segments if s.get('type') == 'text').strip()


def _owner_private(loop, payload, sender):
    """只有主人自己的私聊窗口能发这些命令；群聊和别人的私聊都不认。"""
    owner = str(getattr(loop, 'owner', '') or '')
    if payload.get('chat_type') != 'private' or not owner:
        return False
    if str(sender) != owner or payload.get('chat_id') != 'private:' + owner:
        return False
    try:
        return bool(loop.governance.is_owner(sender))
    except Exception:  # 权限表读不到时以 chat_id 归属为准，不把异常抛进消息循环
        return True


def _group_chat(loop):
    """命令改的是哪一群：通道 allowlist 里的那一群，缺省用当前的群。"""
    for number in sorted(getattr(loop, 'group_ids', ()) or ()):
        number = str(number).strip()
        if number:
            return number if number.startswith('group:') else 'group:' + number
    return 'group:1043230169'


def _policy(loop):
    from group_policy import GroupPolicy
    return GroupPolicy(loop.root)


def _duration(seconds):
    seconds = max(0, int(round(float(seconds))))
    return str(seconds) + ' 秒' if seconds < 120 else '%.1f 分钟' % (seconds / 60)


def _life_state(root):
    """记忆与提醒的读数复用 LifePage 那两个只读查询，桌面端和这里不会各说一套。"""
    from workbench import _life_memory, _life_reminder
    return _life_memory(root), _life_reminder(root)


def _panel(loop):
    chat = _group_chat(loop)
    policy = _policy(loop)
    cfg = policy.config(chat)
    lines = ['【生活面板】']
    lines.append('%s：旁听%s · 发言%s · 学习%s\n引擎 %s（%s）' % (
        chat, '开' if cfg['listen'] else '关', '开' if cfg['reply'] else '关',
        '开' if cfg['learn'] else '关', cfg['engine'], ENGINES.get(cfg['engine'], '未知')))
    now = time.time()
    dive = float(cfg['dive_until'] or 0)
    if dive > now:
        lines.append('潜水：到 %s（还剩 %d 分钟）——到点自己醒，这期间消息仍然会看会记，只是不说话' % (
            time.strftime('%m-%d %H:%M', time.localtime(dive)), max(1, int(round((dive - now) / 60)))))
    else:
        lines.append('潜水：没有在潜水')
    lines.append('唤醒概率 %.2f（1 表示每条群消息都进入判断）· 主动冷却 %s · 回访 %s · 每小时主动上限 %s 条' % (
        float(cfg['wake_probability']), _duration(cfg['active_cooldown_s']),
        '关' if not cfg['followup_s'] else _duration(cfg['followup_s']), cfg['max_active_hour']))
    try:
        from group_proactive import next_check
        check_at = next_check(loop.root, chat) if cfg['proactive_enabled'] else 0.0
        shown = time.strftime('%m-%d %H:%M', time.localtime(check_at)) if check_at else '未排期'
    except Exception:  # 主动开口那张表读不到时只影响这一行，面板其余部分照常
        shown = '读不到'
    lines.append('主动开口：%s · 安静阈值 %s · 概率 %.2f · 下次检查 %s' % (
        '开' if cfg['proactive_enabled'] else '关', _duration(cfg['proactive_idle_s']),
        float(cfg['proactive_probability']), shown))
    # 先判后生成的两道闸：预筛阈值决定"要不要为这条花钱生成"，精力与每小时目标
    # 决定"今天她还有没有主动开口的余量"。两者都只读，读不到就当作没有限制。
    try:
        energy = float(policy.energy(chat))
        spoke = int(policy.replies_last_hour(chat))
        energy_text = '%.2f' % energy if cfg['energy_enabled'] else '已关'
    except Exception:  # 精力表读不到只影响这一行，面板其余部分照常
        energy_text, spoke = '读不到', 0
    lines.append('预筛 %s（阈值 %.2f，判断超时 %d 秒）· 精力 %s（一句扣 %.2f）· 目标 %s 条/小时（近一小时已主动 %d 条）' % (
        '开' if cfg['prescreen_enabled'] else '关', float(cfg['prescreen_threshold']),
        int(cfg['prescreen_timeout_s']), energy_text, float(cfg['energy_cost']),
        cfg['target_replies_per_hour'] or '不限', spoke))
    try:
        life, reminder = _life_state(loop.root)
        kinds, social = life['kinds'], life['social']
        lines.append('她记着：事实 %d · 偏好 %d · 事件 %d · 约定 %d；社交 话题 %d/想说 %d/印象 %d；未完成事件 %d 条%s' % (
            kinds['fact'], kinds['preference'], kinds['event'], kinds['promise'],
            social['topic'], social['thought'], social['impression'], life['open_events'],
            '，最近到期 ' + life['next_due_text'] if life['next_due_text'] else ''))
        ledger = ''
        if reminder['last_state'] or reminder['last_receipt']:
            ledger = '；台账最近一条：%s %s' % (reminder['last_state'] or '-',
                                              (reminder['last_receipt'] or '')[:40])
        lines.append('提醒：%s（台账 %d 条%s）' % (
            '后台已开 · ' + reminder['source'] if reminder['on'] else '未开 · ' + reminder['source'],
            reminder['ledger_count'], ledger))
    except Exception as exc:  # 台账读不到就说明读不到，不要在面板里抛异常
        lines.append('记忆台账暂时读不到（%s: %s）' % (type(exc).__name__, exc))
    try:
        state = loop.governance.config()
        queue = getattr(loop, 'queue', None)
        # /状态 原来是既有管理命令，这里带上它的那几项，换成面板也不会少信息。
        lines.append('回复：%s；等待任务 %s；今日处理轮数 %s；主人权限 %s' % (
            '已暂停' if state['paused'] else '可回复',
            queue.qsize() if queue is not None else '未知', state['usage'].get('calls', 0),
            '、'.join(loop.governance.owner_capabilities().values()) or '无'))
    except Exception:
        pass
    lines.append(HINT)
    return '\n'.join(lines)


def _switch(loop, updates):
    chat = _group_chat(loop)
    updates = dict(updates)
    if 'reply' in updates:
        from fish_agent.settings.tools import ToolSettings
        ToolSettings(loop.root).save({'send': {'groups': {chat.removeprefix('group:'): updates.pop('reply')}}})
    return chat, _policy(loop).configure(chat, updates)


def _group_on(loop):
    chat, cfg = _switch(loop, {'listen': True, 'reply': True})
    return '群 %s 已打开：旁听开、发言开，学习保持%s。现在她能在这个群里说话，引擎是 %s（%s）。' % (
        chat.split(':')[-1], '开启' if cfg['learn'] else '原来的关',
        cfg['engine'], ENGINES.get(cfg['engine'], '未知'))


def _group_off(loop):
    chat, cfg = _switch(loop, {'listen': False, 'reply': False})
    return '群 %s 已关掉：旁听关、发言关，学习保持%s。现在她不能在这个群说话，也不会再看那里的消息。' % (
        chat.split(':')[-1], '开启（旁听关着，学不到新消息）' if cfg['learn'] else '原来的关')


def _learn(loop, on):
    chat, cfg = _switch(loop, {'learn': on})
    return '群 %s 学习已%s：%s（听和说照旧%s）。' % (
        chat.split(':')[-1], '打开' if on else '关掉',
        '群里的东西会记进记忆' if on else '群里不再学新东西，已经记住的还在',
        '不受影响' if cfg['listen'] else '，而且现在旁听本来就是关的，学不到新消息')


def _dive(loop, words):
    if len(words) != 1 or not re.fullmatch(r'[0-9]+', words[0]):
        raise ValueError('潜水分钟要写成 5 到 240 的整数，例如 /潜水 30。')
    minutes = int(words[0])
    if not 5 <= minutes <= 240:
        raise ValueError('潜水分钟需在 5 到 240 之间，例如 /潜水 30。')
    until = time.time() + minutes * 60
    _switch(loop, {'dive_until': until})
    return '从现在起潜水 %d 分钟，到 %s 结束。到点自己醒，这期间消息仍然会看会记，只是不说话。' % (
        minutes, time.strftime('%H:%M', time.localtime(until)))


def _wake(loop):
    _switch(loop, {'dive_until': 0})
    return '已醒来：潜水时间清零。要不要说话还是看群开关（/群关 只听不说，/群开 能说）。'


def _self_switch(loop, on):
    """她自己的账号开关：和工具 fish_self_control 的 pause/resume 写同一份 Governance。"""
    paused = bool(loop.governance.save({'paused': not on})['paused'])
    if paused:
        return ('好，我下线了：群里和私聊我都不会再说话，消息仍然收着，等你想我。'
                '叫我回来就私聊发 /回来（或 /上线）。')
    return '我回来了，从现在起照常看消息、照常说话。'


def _rows(loop, limit=8):
    path = project_path(loop.root, 'data/qq/memory.db')
    if not path.is_file():
        return []
    conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA query_only=ON')
        return [dict(row) for row in conn.execute(
            "SELECT id,text,action,due FROM memories WHERE kind='event' AND status='active' "
            "ORDER BY CASE action WHEN 'open' THEN 0 ELSE 1 END, updated DESC LIMIT ?", (int(limit),))]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _events(loop):
    rows = _rows(loop)
    _LAST_EVENTS.clear()
    if not rows:
        return '还没有记下事件。' + '\n' + HINT
    lines = ['最近的事件（编号只在这次运行里有效，重启后要重新 /事件）：']
    for index, row in enumerate(rows, 1):
        due = float(row.get('due') or 0)
        lines.append('%d. [%s]%s %s' % (
            index, {'open': '待办', 'completed': '已完成', 'cancelled': '已取消'}.get(row.get('action'), '待办'),
            ' ' + time.strftime('%m-%d %H:%M', time.localtime(due)) if due > 0 else '',
            str(row.get('text') or '')[:40]))
        _LAST_EVENTS.append((str(row['id']), str(row.get('text') or '')))
    lines.append('标记用 /完成 2、/取消 2。')
    return '\n'.join(lines)


def _finish(loop, words, action):
    if not _LAST_EVENTS:
        raise ValueError('还没有列过事件编号，先发 /事件，再按编号回 /完成 或 /取消。')
    if len(words) != 1 or not re.fullmatch(r'[0-9]+', words[0]):
        raise ValueError('编号要写成数字，例如 /完成 2，范围 1 到 %d。' % len(_LAST_EVENTS))
    index = int(words[0])
    if not 1 <= index <= len(_LAST_EVENTS):
        raise ValueError('编号只能 1 到 %d，先发 /事件 看最新一列。' % len(_LAST_EVENTS))
    memory_id, text = _LAST_EVENTS[index - 1]
    from memory_store import MemoryStore
    method, verb = ACTIONS[action]
    result = getattr(MemoryStore(loop.root, 'qq'), method)(
        memory_id, scope='owner', note='主人在 QQ 私聊里发 /' + ('完成' if action == 'complete' else '取消'))
    if not result.get('ok'):
        raise ValueError(str(result.get('reason') or '这条事件没改成'))
    return '%s：%s（编号 %d）' % (verb, text[:20], index)


def _shown(value, scale):
    """存的是秒、说的是分钟：显示按命令的写法来算回去。"""
    return '%g' % (value / scale) if scale != 1 else value


def _habits(loop):
    cfg = _policy(loop).config(_group_chat(loop))
    lines = ['习惯参数：']
    for name, (key, _cast, _lo, _hi, unit, label, scale) in HABITS.items():
        value = cfg[key]
        shown = _shown(value, scale) if isinstance(value, int) and not isinstance(value, bool) else \
            ('%g' % (value / scale) if scale != 1 else '%g' % value)
        lines.append('· %s（%s）：%s%s' % (name, label, shown, unit))
    lines.append('· 主动开/主动关（主动开口）：%s' % ('开' if cfg['proactive_enabled'] else '关'))
    lines.append('· 精力开/精力关（精力）：%s' % ('开' if cfg['energy_enabled'] else '关'))
    lines.append('改法：/习惯 冷却 240、/习惯 概率 0.4、/习惯 回访 8、/习惯 上限 2、'
                 '/习惯 主动开、/习惯 主动关、/习惯 主动概率 0.15、/习惯 主动安静 20 分钟、'
                 '/习惯 预筛 0.5、/习惯 精力开、/习惯 精力关、/习惯 目标 6。')
    # 回合自己的那六个预算不在这张表里，归 /群设置：#/习惯 只提一句，免得两处各说各的。
    lines.append('回合节拍、读入预算、每小时/每天调用上限和潜水上限用 /群设置 看和改'
                 '（例如 /群设置 回合 180、/群设置 预算 1200）。')
    return '\n'.join(lines)


USAGE = ('用法：/习惯 冷却 240 · /习惯 概率 0.4 · /习惯 回访 8 · /习惯 上限 2 · '
         '/习惯 主动开 · /习惯 主动关 · /习惯 主动概率 0.15 · /习惯 主动安静 20 分钟 · '
         '/习惯 预筛 0.5 · /习惯 精力开 · /习惯 精力关 · /习惯 目标 6。')


def _habit_set(loop, words):
    # 主动开口的开关只有一个词：/习惯 主动开、/习惯 主动关。
    if len(words) == 1 and words[0] in PROACTIVE_SWITCH:
        on = PROACTIVE_SWITCH[words[0]]
        _switch(loop, {'proactive_enabled': on})
        return ('主动开口已打开：群安静够久时她会自己决定要不要开个头，不想说就什么都不发。'
                '安静阈值、概率用 /习惯 主动安静 20 分钟、/习惯 主动概率 0.15 调。' if on else
                '主动开口已关掉：她只在有人说话时才可能接话，不会自己起头。')
    if len(words) == 1 and words[0] in ENERGY_SWITCH:
        on = ENERGY_SWITCH[words[0]]
        _switch(loop, {'energy_enabled': on})
        return ('精力已打开：每主动说一句就扣掉一点精力，之后随时间自己回满；不够一句的量时她只回 @ 和回复，'
                '不再主动插话。扣多少用配置里的 energy_cost 调。' if on else
                '精力已关掉：主动开口不再受精力限制，冷却、上限、预筛这些门槛照旧。')
    if len(words) not in (2, 3) or words[0] not in HABITS:
        raise ValueError(USAGE)
    name, raw = words[0], words[1]
    key, cast, low, high, unit, label, scale = HABITS[name]
    # 单位是可选的：/习惯 主动安静 20 和 /习惯 主动安静 20 分钟是同一件事。
    if len(words) == 3 and words[2] != unit:
        raise ValueError(USAGE)
    if cast is int:
        if not re.fullmatch(r'[0-9]+', raw):
            raise ValueError('%s要写成 %s 到 %s 的整数%s，例如 /习惯 %s %s。' % (name, low, high, unit, name, low))
        value = int(raw)
    else:
        if not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', raw):
            raise ValueError('%s要写成 0 到 1 之间的小数，例如 /习惯 概率 0.4。' % name)
        value = float(raw)
    if not low <= value <= high:
        raise ValueError('%s需在 %s 到 %s 之间%s。' % (name, low, high, '（' + unit + '）' if unit else ''))
    _switch(loop, {key: value * scale})
    return '已设%s %s%s。' % (label, '%g' % value if cast is float else value, ' ' + unit if unit else '')


def _setting_line(name, cfg):
    """一行读数：中文名、它管什么、现在是多少（按命令的写法换算回去）。"""
    key, _cast, _low, _high, unit, label, scale = GROUP_SETTINGS[name]
    return '· %s（%s）：%s%s' % (name, label, _shown(int(cfg[key]), scale), unit)


def _group_settings(loop):
    """六个回合参数一次看全：读的就是群通道用的那一份 GroupPolicy。"""
    chat = _group_chat(loop)
    cfg = _policy(loop).config(chat)
    lines = ['群 %s 的回合参数：' % chat.split(':')[-1]]
    lines.extend(_setting_line(name, cfg) for name in GROUP_SETTINGS)
    lines.append(GROUP_SETTINGS_USAGE)
    return '\n'.join(lines)


def _group_setting(loop, words):
    """改一个参数：/群设置 回合 180，读回来确认，并把改前改成一起回执。"""
    if not 1 <= len(words) <= 3 or words[0] not in GROUP_SETTINGS:
        raise ValueError(GROUP_SETTINGS_USAGE)
    name = words[0]
    key, _cast, low, high, unit, label, scale = GROUP_SETTINGS[name]
    if len(words) == 1:  # 只看这一项
        chat = _group_chat(loop)
        return '群 %s 的回合参数：\n%s' % (chat.split(':')[-1],
                                          _setting_line(name, _policy(loop).config(chat)))
    raw = words[1]
    # 单位可选：/群设置 潜水 240 和 /群设置 潜水 240 分钟是同一件事。
    if len(words) == 3 and words[2] != unit:
        raise ValueError(GROUP_SETTINGS_USAGE)
    if not re.fullmatch(r'[0-9]+', raw):
        raise ValueError('%s要写成 %s 到 %s 的整数（%s），例如 /群设置 %s %s。'
                         % (name, low, high, unit, name, low or 1))
    value = int(raw)
    if not low <= value <= high:
        raise ValueError('%s需在 %s 到 %s 之间（%s），例如 /群设置 %s %s。'
                         % (name, low, high, unit, name, low or 1))
    chat = _group_chat(loop)
    policy = _policy(loop)
    before = int(policy.config(chat)[key])
    policy.configure(chat, {key: value * scale})
    after = int(policy.config(chat)[key])
    return '已设%s为 %s %s（原来 %s %s）。' % (
        label, _shown(after, scale), unit, _shown(before, scale), unit)


def _rehearsal():
    return ('彩排较慢（要跑几分钟的模型调用），私聊里我不真跑，免得把这几分钟的对话堵住。'
            '要彩排请在命令行执行 harness 里的彩排脚本，跑完我再把结论贴回来。')


def _command(loop, name, words):
    """命令主体：返回要发回去的文本，出错就抛带中文说明的 ValueError。"""
    if name in ('/生活', '/状态'):
        return _panel(loop)
    if name == '/群开':
        return _group_on(loop)
    if name == '/群关':
        return _group_off(loop)
    if name in ('/学习开', '/学习关'):
        return _learn(loop, name == '/学习开')
    if name == '/潜水':
        return _dive(loop, words)
    if name == '/醒来':
        return _wake(loop)
    if name == '/歇着':
        return _self_switch(loop, False)
    if name in ('/回来', '/上线'):
        return _self_switch(loop, True)
    if name == '/事件':
        return _events(loop)
    if name == '/完成':
        return _finish(loop, words, 'complete')
    if name == '/取消':
        return _finish(loop, words, 'cancel')
    if name == '/习惯':
        return _habits(loop) if not words else _habit_set(loop, words)
    if name == '/群设置':
        return _group_settings(loop) if not words else _group_setting(loop, words)
    return _rehearsal()


async def handle(loop, payload, sender) -> bool:
    """命中一条生活命令就发回复并返回 True；否则返回 False 交给后面的分支。"""
    words = _text(payload).split()
    if not words or words[0] not in COMMANDS:
        return False
    if len(words) > 1 and words[0] in ('/生活', '/状态', '/群开', '/群关', '/学习开', '/学习关', '/醒来',
                                       '/事件', '/彩排', '/歇着', '/回来', '/上线'):
        return False  # 这些不带参数：多打了字就不是这条命令，交给后面的分支
    name = words[0]
    if name == '/取消' and (len(words) != 2 or not words[1].isdigit()):
        return False  # 裸 /取消 是既有的取消当前任务，只有带编号的才归这里
    if not _owner_private(loop, payload, sender):
        return False
    try:
        response = _command(loop, name, words[1:])
    except ValueError as exc:
        response = str(exc)
    except Exception as exc:  # 任何失败都回一句中文，不把异常抛进消息循环
        response = '这条命令没执行成功：%s: %s' % (type(exc).__name__, exc)
        _audit(loop, 'life_command_failed', '%s %s' % (name, response))
    _audit(loop, 'life_command', name)
    try:
        await loop._deliver_reply(
            'private:' + str(loop.owner), [{'type': 'text', 'text': response[:4000]}],
            'life-command:' + str(payload.get('message_id') or uuid.uuid4().hex),
            reply_to=str(payload.get('message_id') or '') or None)
    except Exception as exc:  # 发送失败只记账：这里已经没有第二条回复通道可用
        _audit(loop, 'life_command_send_failed', '%s %s: %s' % (name, type(exc).__name__, exc))
    return True


def _audit(loop, kind, reason):
    try:
        loop.governance.audit(kind, str(reason)[:250])
    except Exception:
        pass
