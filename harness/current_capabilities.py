"""Report the current admitted request, rather than remembered tool names."""
from governance import Governance
from request_inspector import RequestInspector


def tool_names(payload):
    """Published names, from the OpenAI wrapper shape or a flat name field."""
    names = []
    for tool in payload.get('tools', []) or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get('function') if isinstance(tool.get('function'), dict) else {}
        name = str(tool.get('name') or function.get('name') or '')
        if name:
            names.append(name)
    return names


def current_capabilities(root, role, identity):
    snapshot = RequestInspector(root).latest(role, identity['session'])
    payload = snapshot.get('payload', {})
    cfg = Governance(root, role).config()
    tools = tool_names(payload)
    def offered(*names):
        return any(name in tools for name in names)
    from multimodal import load_config
    media = load_config(root)
    image_count = sum(1 for message in payload.get('messages', []) for block in message.get('content', [])
        if isinstance(message.get('content'), list) and isinstance(block, dict) and block.get('type') in ('image','image_url'))
    return {'captured': snapshot.get('captured', False), 'as_of': snapshot.get('created'),
        'provider': payload.get('provider'), 'model': payload.get('model'),
        'tools': tools, 'web_search': offered('web_search', 'fish_web_search'),
        'web_fetch': offered('web_fetch', 'fish_web_fetch'),
        'group_history': offered('fish_group_history'), 'group_list': offered('fish_group_list'),
        'owner': bool(identity.get('owner')), 'mode': identity.get('owner_mode', 'chat'),
        'local_file_and_terminal_tools': [name for name in tools if name in ('pwsh','bash','read','write','edit','read_file','write_file','fish_file_read','fish_file_write','fish_file_edit','fish_directory','fish_shell','fish_process')],
        'effort': payload.get('effort', cfg['chat_reasoning_effort']), 'reply_display': cfg['reply_display'],
        'tool_call_limit': cfg['max_tool_calls_per_turn'] or None, 'tool_calls_unlimited': cfg['max_tool_calls_per_turn'] == 0,
        'daily_unlimited': not cfg['daily_usage_limits'][role],
        'image_input': {'enabled':bool(media.get(role+'_enabled') and media['vision']['enabled']), 'images_in_request_snapshot':image_count,
            'character_source_check':offered('fish_image_verify') and __import__('vision_identification').settings(root)['enabled'],
            'character_check_mode':'optional-tool', 'image_views':offered('fish_image_view')},
        'note':'以上工具来自本轮实际模型请求。是否发送成功仍以发送回执为准；图片存在不代表身份已核实。'}


"""What this turn can really do, read live instead of remembered.

The model is handed a tool schema and a persona and nothing else, so a claim about
her own abilities used to be recollection: which groups are listening, whether the
reminder switch is on, which tools exist today. This module answers that claim from
the same stores the executors read, and says 未知 where nothing answers.
"""
import os
from pathlib import Path

LIMIT = 300
UNKNOWN = '未知'
PREFIX = 'fish_'
OWNER = ('owner-private', 'owner-desktop')
HONESTY = '不知道的就说不知道，不要编；工具报错先看原因，不要原样重试'
# One sentence for every surface: the tool table no longer differs, so a capability
# question is a boundary question - and the refusal the tool returns says which one.
UNIFORM = '能力是同一套，能不能做取决于场合与安全边界'
REMINDER_ON = '记忆到期线索自动整理已开启：kind=event+due 会进入 Fish 自主机会；主人明确委托的可靠提醒使用 fish_schedule purpose=reminder，与此开关无关'
REMINDER_OFF = '记忆到期线索自动整理未开启；主人明确委托的可靠提醒仍可通过 fish_schedule 创建并在任务页管理，只有创建成功才承诺到点执行'
REMINDER_NA = '记忆到期线索开关状态未知；可靠提醒以 fish_schedule 实际创建回执为准'


def _kind(scope):
    """The surface a scope names: one of the two owner contexts, or one bound group."""
    text = str(scope or '')
    if text in OWNER:
        return text, ''
    if text.startswith('chat:group:'):
        return 'group', 'group:' + text[len('chat:group:'):]
    if text == 'group':
        return 'group', ''
    if text.startswith('group:'):
        return 'group', text
    return '', ''


def _tools(context, role, root=None):
    """The names she has, taken from the catalogue rather than copied.

    One table for the whole role, so the context no longer narrows it; an answer
    without a bound chat (context '') asks the same table.
    """
    from fish_chat import tool_definitions
    try:
        return [item['function']['name']
                for item in tool_definitions(role, context or 'qq', root=root, emoji_state={})]
    except Exception:  # noqa: BLE001 - an unusable catalogue is reported, not raised
        return None


def _groups(root):
    """The owner's authorised groups; None when the store cannot answer."""
    try:
        from group_read import authorized_groups
        return authorized_groups(Path(root))
    except Exception:  # noqa: BLE001 - a store that cannot answer yields 未知, not a guess
        return None


def _switch_on():
    """The consumer's current process flag; user environment is never a live flag."""
    try:
        from memory_reminder import enabled
        return enabled()
    except Exception:  # noqa: BLE001 - the display must report unknown, not claim a running consumer
        return None


def _user_value(name):
    """The stored user environment value: what a service restarted now would inherit."""
    try:
        import winreg
    except ImportError:
        return None
    for hive in ('HKEY_CURRENT_USER', 'HKEY_LOCAL_MACHINE'):
        try:
            with winreg.OpenKey(getattr(winreg, hive), 'Environment') as key:
                return str(winreg.QueryValueEx(key, name)[0]).strip().lower()
        except OSError:
            continue
    return None


def _reminder(on):
    if on is True:
        return REMINDER_ON
    if on is False:
        return REMINDER_OFF
    return REMINDER_NA


def _tools_text(names, mode):
    """The published names in full, under one shared prefix, or as a bare count."""
    if names is None:
        return '工具：' + UNKNOWN + '（本轮工具目录读不到）'
    if not names:
        return '本轮没有可用工具'
    if mode == 'count':
        return '本轮可用工具' + str(len(names)) + '个，名字见本轮工具清单'
    if mode == 'short' and all(name.startswith(PREFIX) for name in names):
        return ('工具' + str(len(names)) + '个，都以 fish_ 开头：'
                + ' '.join(name[len(PREFIX):] for name in names))
    return '工具' + str(len(names)) + '个：' + ' '.join(names)


def _participation(row):
    """One honest line about who decides this group's listen/reply switches."""
    if row is None:
        return '本群参与：' + UNKNOWN + '（本群不在主人授权读取的群里）'
    listen = row.get('listen') is True
    reply = row.get('reply') is True
    source = str(row.get('participation_source') or '')
    engine = str(row.get('engine') or UNKNOWN)
    if source.startswith('space:'):
        who = '群测试窗口 space:' + source[len('space:'):] + ' '
    elif engine == 'fish':
        who = '主决策 engine=fish '
    else:
        who = '主策略 engine=' + engine + ' '
    line = ('本群参与由' + who + '决定，旁听' + ('开' if listen else '关')
            + '、发言' + ('开' if reply else '关'))
    if not listen and not reply:
        return line + '：这个群现在不会自动说话'
    return line


def _owner_tail(root, names):
    """Private-chat facts: memory, the reminder switch, posting into an authorised group."""
    parts = [UNIFORM]
    if names:
        parts.append('记忆可读可写' if any(name.startswith('fish_memory_') for name in names)
                     else '记忆不可用')
        # She used to answer "我只能存公网链接的图" because nothing told her the chat images
        # were reachable; the tool takes latest/message_id now, so say so.
        if 'fish_emoji_save' in names:
            parts.append('表情：聊天里你或群友刚发的图我能直接存进表情库，网上图片给链接也能存')
        if 'fish_doc_organize' in names:
            parts.append('资料：能存网页和文本，能建一层文件夹归类整理')
    parts.append(_reminder(_switch_on()))
    if names and 'fish_group_send' in names:
        groups = _groups(root)
        if groups is None:
            parts.append('能否代发群消息：' + UNKNOWN)
        elif groups:
            parts.append('可代发群消息到' + str(len(groups)) + '个授权群，真发以回执为准')
        else:
            parts.append('现在没有可代发群消息的授权群')
    parts.append(HONESTY)
    return parts


def _group_tail(root, chat, names):
    """Group facts: the same abilities, the boundary this scope draws, who runs the switches."""
    parts = [UNIFORM, '这个群里读写的只有本群自己的记录和资料，看不到主人的私聊，也改不了任何设置']
    rows = _groups(root) if chat else None
    if not chat or rows is None:
        parts.append('本群参与：' + UNKNOWN)
    else:
        parts.append(_participation(next((item for item in rows
                                          if str(item.get('chat')) == chat), None)))
    parts.append(HONESTY)
    return parts


def _unbound_tail():
    """No bound chat: the ability list holds, the chat-shaped facts do not."""
    return [UNIFORM, _reminder(_switch_on()),
            '这次没有绑定任何聊天窗口，所以群授权、当前人格和本轮实际模型要等绑定之后才说得准',
            HONESTY]


def ability_table(root, role='qq'):
    """The names she has, for a caller that asks without a bound chat."""
    return _tools('', role)


def ability_brief(root, role='qq') -> str:
    """What she is, answered without a bound session: the table plus the live switches."""
    return _brief(_tools('', role, root), _unbound_tail())


def capabilities_brief(root, *, scope, role='qq') -> str:
    """One short live statement of this turn's abilities; '' when no tools are published."""
    kind, chat = _kind(scope)
    if not kind:
        return ''
    names = _tools(kind, role, root)
    parts = _group_tail(root, chat, names) if kind == 'group' else _owner_tail(root, names)
    return _brief(names, parts)


def _brief(names, parts) -> str:
    """One brief within the budget: the longest of the three tool shapes that fits."""
    text = ''
    for mode in ('full', 'short', 'count'):
        text = '。'.join([_tools_text(names, mode), *parts]) + '。'
        if len(text) <= LIMIT:
            return text
    return text[:LIMIT - 1] + '。'
