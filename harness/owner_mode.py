"""Verified owner's private QQ mode: ordinary chat or native remote work.

The mode is program state, never a persona, a model instruction or an identity
claim. It is persisted to data/social/owner-mode.json and every consumer
(QQ loop, DesktopChat, ToolHost, fish-control) reads the same record, so the
conversation, session, persona and history stay untouched when switching.

Public API (also used by the native UI and ToolHost):

    OwnerMode(root).get() -> {'mode': 'chat'|'work', 'workspace': str, 'updated': ...}
    OwnerMode(root).set(mode, workspace=None) -> same

Identity permission verification stays in owner_computer.authorized(); this
module only answers "which working mode did the verified owner choose".
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from storage import read_json, write_json

MODES = ('chat', 'work')

# Program-level commands. "切换" with no target toggles; otherwise chat/work is
# chosen explicitly. Only whole messages match: a normal sentence that merely
# mentions work mode keeps its raw text and reaches the model.
_WORK = re.compile(r'^(?:/工作模式|切换工作模式|切工作模式)$')
_CHAT = re.compile(r'^(?:/聊天模式|切换聊天模式|切聊天模式)$')
_TOGGLE = re.compile(r'^切换(?:聊天/工作)?模式$')


def mode_command(text) -> str | None:
    """Return 'chat'|'work'|'toggle' for a bare owner command, else None."""
    value = str(text or '').strip()
    if _WORK.fullmatch(value):
        return 'work'
    if _CHAT.fullmatch(value):
        return 'chat'
    if _TOGGLE.fullmatch(value):
        return 'toggle'
    return None


def normalise_workspace(root, workspace=None) -> str:
    """Return an existing work directory; default derives from owner_computer."""
    if workspace is None or not str(workspace).strip():
        from owner_computer import workspace as owner_workspace
        return str(owner_workspace(root).resolve())
    directory = Path(str(workspace).strip().strip('"')).expanduser()
    if not directory.is_absolute():
        directory = Path(root) / directory
    directory = directory.resolve()
    if not directory.is_dir():
        raise ValueError('请选择存在的工作目录')
    return str(directory)


class OwnerMode:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = self.root / 'data/social/owner-mode.json'

    def _stored(self) -> dict:
        try:
            value = read_json(self.path, {})
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def get(self) -> dict:
        value = self._stored()
        mode = value.get('mode') if value.get('mode') in MODES else 'chat'
        workspace = str(value.get('workspace') or '').strip()
        if not workspace:
            workspace = str(self._default_workspace())
        return {'mode': mode, 'workspace': workspace, 'updated': value.get('updated', 0)}

    def set(self, mode, workspace=None) -> dict:
        if mode not in MODES:
            raise ValueError('未知工作模式：' + str(mode))
        current = self._stored()
        now = time.time()
        if workspace is not None and str(workspace).strip():
            target = normalise_workspace(self.root, workspace)
        elif current.get('workspace') and current.get('mode', 'chat') in MODES:
            target = str(current['workspace'])
        else:
            target = str(self._default_workspace())
        record = {'mode': mode, 'workspace': target,
                  'created': current.get('created') or now, 'updated': now}
        write_json(self.path, record)
        return {'mode': mode, 'workspace': target, 'updated': now}

    def set_workspace(self, workspace) -> dict:
        return self.set(self.get()['mode'], workspace)

    def toggle(self, workspace=None) -> dict:
        return self.set('chat' if self.get()['mode'] == 'work' else 'work', workspace)

    def _default_workspace(self) -> Path:
        try:
            from owner_computer import workspace as owner_workspace
            return owner_workspace(self.root).resolve()
        except Exception:
            path = self.root / 'qq-work/owner-files'
            path.mkdir(parents=True, exist_ok=True)
            return path.resolve()

    def status(self) -> dict:
        """Human-readable line for owner QQ replies and the local UI."""
        value = self.get()
        return {'mode': value['mode'],
                'label': '工作模式' if value['mode'] == 'work' else '聊天模式',
                'workspace': value['workspace'], 'updated': value['updated']}

    def summary(self, before=None, *, failed=False) -> str:
        value = self.status()
        head = '' if before is None or before == value['mode'] else f"已从{'工作' if before == 'work' else '聊天'}模式切换。"
        if failed:
            head = '本次未切换：'
        if value['mode'] == 'work':
            return (f"{head}当前：工作模式。使用的仍是同一个私聊会话、人格和历史。"
                    f"工作目录：{value['workspace']}。可处理本机文件与任务；发 /聊天模式 返回聊天模式。")
        return (f"{head}当前：聊天模式。使用的仍是同一个私聊会话、人格和历史。"
                "发 /工作模式 切换到工作模式。")


__all__ = ['OwnerMode', 'modes', 'mode_command', 'normalise_workspace', 'MODES']
modes = MODES
