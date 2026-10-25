"""QQ association/private reception; GroupPolicy.listen and ToolSettings stay owners."""
from locations import InstanceCatalog, instance_context, project_path
from storage import database, read_json, write_json, settings_lock


class ChannelSettings:
    def __init__(self, root):
        self.context = instance_context(root)
        self.root = self.context.root
        self.path = project_path(self.root, 'harness/qq-participation.json')

    def get(self):
        if self.path.is_file(): return read_json(self.path)
        with settings_lock(self.root):
            if self.path.is_file(): return read_json(self.path)
            initial = self.context.instance_id == 'main'
            value = {'version': 1, 'connection_id': 'qq' if initial and project_path(self.root, 'channels/fish_qq/config.json').is_file() else '',
                     'receive': initial, 'private_receive': initial}
            if initial:
                from group_policy import GroupPolicy
                policy = GroupPolicy(self.root)
                legacy = read_json(project_path(self.root, 'channels/fish_qq/config.json'), {})
                for group in legacy.get('allowlist', {}).get('group_ids', []):
                    policy.config('group:' + str(group))
            write_json(self.path, value)
            return value

    def group_ids(self):
        self.get()
        from group_policy import GroupPolicy
        with database(GroupPolicy(self.root).path) as db:
            return [str(row['chat']).removeprefix('group:') for row in db.execute('SELECT chat FROM groups ORDER BY chat')]

    def accepts(self, chat_type, chat):
        value = self.get()
        if value['connection_id'] != 'qq' or not value['receive']: return False
        if chat_type == 'private': return value['private_receive']
        if chat_type == 'group' and str(chat).removeprefix('group:') in self.group_ids():
            from group_policy import GroupPolicy
            return GroupPolicy(self.root).config(chat)['listen']
        return False

    def save(self, changes):
        from group_policy import GroupPolicy
        from .tools import ToolSettings
        from conversation_policy import set_community
        if 'connection_id' in changes and changes['connection_id'] not in ('', 'qq'):
            raise ValueError('请选择现有QQ连接；当前宿主只配置了一个QQ连接')
        if changes.get('connection_id') == 'qq' and not project_path(self.root, 'channels/fish_qq/config.json').is_file():
            raise ValueError('宿主尚未配置QQ连接')
        for key in ('receive', 'private_receive', 'listen', 'read', 'reply'):
            if key in changes and type(changes[key]) is not bool: raise ValueError('接收和发送设置必须是开关值')
        group = str(changes.get('group_id') or '').removeprefix('group:')
        if group: GroupPolicy._chat('group:' + group)
        elif any(key in changes for key in ('listen', 'read', 'reply', 'topic')): raise ValueError('请填写群号')
        with settings_lock(self.root):
            value = self.get()
            value.update({key: changes[key] for key in ('connection_id', 'receive', 'private_receive') if key in changes})
            if group:
                policy = GroupPolicy(self.root)
                patch = {key: changes[key] for key in ('listen', 'read') if key in changes}
                if patch: policy.configure('group:' + group, patch)
                else: policy.config('group:' + group)
                if 'reply' in changes: ToolSettings(self.root).save({'send': {'groups': {group: changes['reply']}}})
                if 'topic' in changes: set_community(self.root, group, changes['topic'])
            write_json(self.path, value)
        return self.get()


def account_groups(root, *, receiving=False):
    """Live union of instance choices, never the foreground selection."""
    catalog = InstanceCatalog(root)
    result = set()
    for entry in catalog.records():
        settings = ChannelSettings(catalog.resolve(entry['id']))
        for group in settings.group_ids():
            if not receiving or settings.accepts('group', 'group:' + group): result.add(group)
    return result
