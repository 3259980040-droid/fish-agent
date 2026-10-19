"""Explicit owner-private execution route for the original Tulpa MCP."""
from pathlib import Path
import os
import subprocess
import time
from storage import read_json

PLUGIN_ID = 'tulpa_owner'
PREFIX = 'fishp__tulpa_owner__'


def configuration(root):
    return read_json(Path(root) / 'data/integrations/tulpa/owner-route.json', {})


def matches(root, space):
    config = configuration(root)
    return bool(config.get('enabled') and space['origin'] == 'owner-qq'
                and (space['id']==config.get('space_id') or space['revision'] == config.get('persona_revision')))


def ensure_service(root):
    """Restore the original MCP after Fish restarts, without opening a window."""
    import requests
    root = Path(root)
    if not configuration(root).get('enabled'):
        return
    def healthy():
        try:
            reply = requests.get('http://127.0.0.1:3120/health', timeout=1,
                                 proxies={'http': None, 'https': None, 'all': None}).json()
            return reply.get('source') == str(root / 'vendor/integrations/tulpa/source')
        except (requests.RequestException, ValueError):
            return False
    if healthy():
        return
    parent = int(os.environ.get('FISH_LOOP_PARENT_PID', str(os.getpid())))
    with (root / 'data/integrations/tulpa/service.log').open('a', encoding='utf-8') as output:
        subprocess.Popen([str(root / 'vendor/integrations/tulpa/venv/Scripts/pythonw.exe'),
                          str(root / 'harness/tulpa_service.py'), '--parent', str(parent)],
                         cwd=str(root), stdout=output, stderr=subprocess.STDOUT,
                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    for _ in range(60):
        if healthy():
            return
        time.sleep(.25)
    raise RuntimeError('原版 Tulpa MCP 尚未连接 QQ，请查看独立服务记录')


def require_route(root, identity):
    from social_context import SocialContext
    from governance import Governance
    config = configuration(root)
    owner = str(config.get('owner', ''))
    store = SocialContext(root)
    selected = store.owner_space(owner) if owner else None
    if not (identity['role'] == 'qq' and identity['owner'] and not identity['desktop']
            and identity['sender'] == owner and Governance(root).is_owner(owner)
            and selected and matches(root, selected)
            and identity['scope'] == selected['scope'] and identity['session'] == selected['session']):
        raise PermissionError('原版 Tulpa 发送仅用于当前选中的主人私聊测试窗口')
    return config


def prompt(root):
    config = configuration(root)
    return ('[实际聊天接入：原版 Tulpa MCP。]\n'
            f"QQ 会话为 {config['conversation_id']}，原版聊天 session_id 为 {config['chat_session_id']}。\n"
            '每轮先调用 fishp__tulpa_owner__get_chat_session 取得最新 input_batch，然后按原版协议 '
            'plan_chat_reply → send_chat_reply。工具返回的 next_call 工具名需要加前缀 fishp__tulpa_owner__。'
            '正常发言必须由 send_chat_reply 实际发送；最终普通模型文本不会另外发到 QQ，避免重复。'
            '可以按上下文搜索、看图、选表情和读取学习库，采用原版返回的人格和行为资料。'
            '收到定时唤醒时，若任务明确要求主动联系，即使没有新消息也可以对当前空批次计划主动发言，'
            '不编造提问或事件编号。使用原生 schedule_create 创建持久定时任务；到期仍在本会话执行，'
            '任务内容必须写清实际要做的事，调度成功不等于 QQ 已送达。'
            '发送成功以原版工具回执为准；结果不明先查回执，不能直接重发。'
            'Fish 已负责新消息触发：处理完一批后可调用一次 wait_chat_messages，'
            '以 acknowledge_through_id 确认 read_through_id；宿主将等待设为1秒。'
            '返回 idle 时结束本轮，让 Fish 等下一条消息，不在同一模型回合持续轮询。'
            '若有 learning_ready，可领取并用下一次模型调用完成一个学习阶段后提交；不连续领取。'
            '日常短接话自然简短，解释问题按需要展开；括号动作只在有表达作用时使用。')
