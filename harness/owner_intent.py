"""Explicit owner task controls; ordinary requests always reach the same Fish."""
import re


def route(text):
    value = str(text).strip()
    command = re.fullmatch(r'/(任务状态|取消任务|重发成果)(?:\s+([a-f0-9]{32}))?', value)
    if command:
        return {'kind':'FISH_TASK','action':{'任务状态':'get','取消任务':'cancel','重发成果':'deliver'}[command[1]],'task':command[2] or ''}
    if value in ('/电脑任务','/任务'):
        return {'kind':'CLARIFY','text':'直接告诉 Fish 要做什么即可；可操作已有工程，也可让她安排后台子任务。/任务状态、/取消任务、/重发成果 可管理已有任务。'}
    return {'kind':'CHAT'}
