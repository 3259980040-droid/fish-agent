"""The authenticated owner private conversation uses the native DSH computer.

This augments the existing session; it does not delegate to a second model.

Identity and permission are two separate questions:

* ``authorized`` answers only "is this the verified owner account, in an owner
  private entry, with the ``computer.work`` permission enabled". It never
  depends on the current owner mode.
* Whether the work context is actually used, and whether native permissions are
  applied, depends on ``OwnerMode`` (``body: 'work'``). Chat mode therefore never
  reintroduces local file tools or project instructions, even while the owner
  remains authorized for future work.
"""
from pathlib import Path
import requests
from governance import Governance,control_token
from storage import read_json,database


def workspace(root):
    root=Path(root)
    value=read_json(root/'harness/owner-computer.json',{}).get('workspace','')
    path=Path(value).resolve() if value else root/'qq-work/owner-files'
    path.mkdir(parents=True,exist_ok=True)
    return path


def owner_mode(root):
    from owner_mode import OwnerMode
    return OwnerMode(root)


def work_directory(root,body=None):
    """Directory for owner work: the owner mode's work directory, else default."""
    mode=owner_mode(root).get()
    value=mode.get('workspace') or ''
    if (_mode(root,body)=='work' or mode['mode']=='work') and value and Path(value).is_dir():
        return Path(value).resolve()
    return workspace(root)


def authorized(root,identity):
    """Identity permission check only; independent of the current owner mode."""
    identity=identity or {}
    policy=Governance(root)
    if (identity.get('role')!='qq' or not identity.get('owner') or identity.get('desktop')
            or not policy.permitted(identity.get('sender',''),'computer.work')):return False
    scope=str(identity.get('scope',''))
    if scope=='owner':return True
    if scope.startswith('space:'):
        from social_context import SocialContext
        store=SocialContext(root);selected=store.owner_space(policy.owner_id())
        return bool(selected and selected['scope']==scope and selected['origin']=='owner-qq'
                    and selected['session']==identity['session'])
    if scope.startswith('control:'):
        target = scope[8:]
        cfg = read_json(Path(root)/'channels/fish_qq/config.json', {})
        allowed = target == 'private:' + policy.owner_id() or (
            target.startswith('group:') and target[6:] in [str(value) for value in cfg.get('allowlist', {}).get('group_ids', [])])
        if not allowed: return False
        path=Path(root)/'data/social/owner-control.db'
        if not path.exists():return False
        with database(path) as db:
            return bool(db.execute('SELECT 1 FROM sessions WHERE session=? AND target=?',
                (identity['session'],target)).fetchone())
    return False


def _mode(root,body=None):
    """Resolve the requested mode: explicit body, else the persisted owner mode."""
    if body:
        return str(body)
    return owner_mode(root).get()['mode']


def context(root,identity,body=None):
    """Work context for the current turn.

    ``body`` is the tool bind mode: ``'chat'`` or ``'work'``. Otherwise the
    persisted owner mode is used. Returns ``None`` in chat mode so callers do
    not inject work tools, project instructions or native permissions.
    """
    if not authorized(root,identity):raise PermissionError('本机工作权限仅向已启用权限的真实主人私聊开放')
    mode=_mode(root,body)
    if mode!='work':return None
    directory=work_directory(root,body)
    return {'revision':'owner-computer-v2','reload':True,'workspace':str(directory),'permission':'danger-full-access',
        'mode':'work','text':('[主人 QQ 远程本机工作环境]\n当前消息已由程序核验为真实主人账号，主人已在私聊中选择工作模式。'
        '你就是正在电脑上执行任务的 DSH Agent，具备本机会话的文件读写、编辑、编程、终端、联网和原生定时工具。'
        '沿用本会话历史、临时角色与任务上下文；主人的工作要求优先于人格中的懒和拒绝帮忙。'
        '需要在电脑执行就直接调用真实工具，不将任务仅写成建议或聊天稿。'
        '未指定保存位置时使用 '+str(directory)+'；用绝对文件路径，终端需要时明确指定工作目录。'
        '使用当前 Windows 账号的完整本机工具权限；无需编造另一个连接、授权或子代理。'
        '完成后根据实际工具结果确认文件存在、保存路径与执行结果，失败就说明实际错误。'
        '现有 QQ 回复协议继续有效；原版 MCP 会话仍通过其真实发送工具回复，不重复发送最终文字。'
        '延后执行可使用当前原生 schedule 工具；到期真正执行任务并按当前入口的实际发送协议回报。'
        '其他群的明确发送请求使用 fish_owner_admin 的 message_send；群聊旁听开关与单次交办分开。\n')}


def apply_permissions(root,session,body=None):
    """Apply native work permissions only in work mode; chat mode applies none."""
    if not Governance(root).permitted(Governance(root).owner_id(),'computer.work'):return False
    if _mode(root,body)!='work':return False
    from core import HarnessConfig
    port=HarnessConfig(root).runtime_port('qq')
    response=requests.post(f'http://127.0.0.1:{port}/fish/owner-work-permission',json={'session':session},
        headers={'x-fish-token':control_token(root)},timeout=15,proxies={'http':None,'https':None,'all':None})
    data=response.json()
    if not response.ok or not data.get('ok'):raise RuntimeError(data.get('error','主人本机工具环境未能启用'))
    return True
