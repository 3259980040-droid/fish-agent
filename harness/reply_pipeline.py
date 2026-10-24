"""Shared delivery feedback and independent response-display settings."""
from governance import Governance


def display_mode(root, persona_mode='final'):
    selected = Governance(root).config()['reply_display']
    return persona_mode if selected == 'persona' else selected


def failure_text(error):
    message = str(error).lower()
    if '工具' in message and ('上限' in message or '次数' in message):
        reason = '本轮工具调用被限额中止'
    elif 'timeout' in message or '超时' in message:
        reason = '本轮处理超时'
    elif '读取' in message or '连接' in message or 'transport' in message:
        reason = '模型或工具连接失败'
    elif 'permission' in message or '阻断' in message or '权限' in message:
        reason = '本轮操作被当前设置拦截'
    else:
        reason = '本轮生成没能完成'
    return reason + '，这条请求没有正常交付。聊天记录已保留，可以继续补充或让我重试。'
