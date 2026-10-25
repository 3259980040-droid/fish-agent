"""Per-instance effective tools: one saved value for discovery, calls and send."""
from __future__ import annotations
from pathlib import Path
import re
from locations import instance_context, project_path
from storage import read_json, write_json, settings_lock


def call_target(name, arguments=None, identity=None):
    arguments=arguments or {};identity=identity or {}
    nested=arguments.get('params') if isinstance(arguments.get('params'),dict) else arguments
    action=str(arguments.get('action') or name)
    outgoing=(name in ('fish_say','fish_send_sticker','fish_chat_reply','fish_group_send','fish_group_send_sticker')
              or any(token in action.lower() for token in ('send','upload','forward','publish','reply'))
              or '__send_' in name or '__forward_' in name)
    if not outgoing:return ''
    for key in ('target','chat_id','peer','conversation_id','conversation'):
        value=str(nested.get(key) or arguments.get(key) or '')
        if value.startswith(('private:','group:','desktop:')):return value
        if value=='owner':return 'private:owner'
    if nested.get('group_id'):return 'group:'+str(nested['group_id'])
    if nested.get('user_id'):return 'private:'+str(nested['user_id'])
    if name in ('fish_group_send','fish_group_send_sticker'):return 'group:'
    # A generic sender can target several places: do not guess its destination in a catalogue.
    if not arguments and name not in ('fish_say','fish_send_sticker','fish_chat_reply','fish_owner_send'):return ''
    scope=str(identity.get('scope') or '')
    if scope.startswith('chat:group:'):return scope[5:]
    if identity.get('desktop'):return 'desktop'
    if identity.get('owner'):return 'private:owner'
    return ''


class ToolSettings:
    def __init__(self,root):
        self.context=instance_context(root);self.root=self.context.root
        self.path=project_path(self.root,'harness/tool-settings.json')

    def get(self):
        if self.path.is_file():return read_json(self.path)
        with settings_lock(self.root):
            if self.path.is_file():return read_json(self.path)
            legacy=read_json(project_path(self.root,'harness/control.json'),{}).get('send_policy',{})
            value={'version':1,'tools':{},'plugins':{},'send':{
                'private':legacy.get('owner_private',{}).get('owner_reply',True) is not False,
                'group':legacy.get('group',{}).get('owner_request',True) is not False,'groups':{}}}
            write_json(self.path,value)
            return value

    def save(self,patch):
        if not isinstance(patch,dict) or set(patch)-{'tools','plugins','send'}:
            raise ValueError('工具设置应为tools、plugins或send对象')
        with settings_lock(self.root):
            value=self.get()
            for section in ('tools','plugins'):
                if section not in patch:continue
                if not isinstance(patch[section],dict):raise ValueError('工具开关必须按名称保存')
                for key,enabled in patch[section].items():
                    if not isinstance(key,str) or not key or type(enabled) is not bool:
                        raise ValueError('工具和扩展开关必须为名称与布尔值')
                    value[section][key]=enabled
            if 'send' in patch:
                send=patch['send']
                if not isinstance(send,dict) or set(send)-{'private','group','groups'}:raise ValueError('未知发送开关')
                for target in ('private','group'):
                    if target in send:
                        if type(send[target]) is not bool:raise ValueError('发送开关必须为布尔值')
                        value['send'][target]=send[target]
                if 'groups' in send:
                    if not isinstance(send['groups'],dict):raise ValueError('群发送开关应按群号保存')
                    for gid,enabled in send['groups'].items():
                        if not re.fullmatch(r'[0-9]{5,15}',str(gid)) or type(enabled) is not bool:raise ValueError('群号或发送开关不合法')
                        value['send']['groups'][str(gid)]=enabled
            write_json(self.path,value)
            return value

    def plugin_enabled(self,plugin):
        return self.get()['plugins'].get(plugin['id'],bool(plugin.get('enabled')))

    def effective_capability(self,name='',arguments=None,identity=None,target='',plugin=None):
        settings=self.get();reason=''
        if name and settings['tools'].get(name) is False:reason='已关闭工具：'+name
        if not reason and plugin is not None and name not in settings['tools']:
            selected=plugin.get('allowed_tools')
            if selected is not None and name.split('__',2)[-1] not in selected:
                reason='此工具未在本窗口选择：'+name
        if not reason and plugin is not None and not self.plugin_enabled(plugin):reason='已关闭扩展：'+plugin.get('name',plugin['id'])
        destination=target or call_target(name,arguments,identity)
        if not reason and destination.startswith('private:') and not settings['send']['private']:
            reason='已关闭私聊发送'
        if not reason and destination.startswith('group:'):
            if not settings['send']['group']:reason='已关闭群发送'
            elif settings['send']['groups'].get(destination[6:]) is False:reason='已关闭此群发送：'+destination[6:]
        return {'enabled':not reason,'reason':('窗口 '+self.context.instance_id+'：'+reason) if reason else '',
                'instance_id':self.context.instance_id,'target':destination}

    def require(self,name='',arguments=None,identity=None,target='',plugin=None):
        result=self.effective_capability(name,arguments,identity,target,plugin)
        if not result['enabled']:raise PermissionError(result['reason'])
        return result

    def legacy_policy(self):
        send=self.get()['send']
        return {'version':2,'owner_private':{'owner_reply':send['private'],'scheduled_result':send['private']},
                'group':{'owner_request':send['group'],'public_auto_reply':False},'require_explicit_owner':True}

    def update_legacy(self,changes):
        patch={};owner=changes.get('owner_private',{});group=changes.get('group',{})
        if 'owner_reply' in owner:patch['private']=owner['owner_reply']
        elif 'scheduled_result' in owner:patch['private']=owner['scheduled_result']
        if 'owner_request' in group:patch['group']=group['owner_request']
        return self.save({'send':patch})

    def require_delivery(self,target,source_tools=()):
        self.require(target=target)
        for name in source_tools:self.require(str(name),target=target)
        return True
