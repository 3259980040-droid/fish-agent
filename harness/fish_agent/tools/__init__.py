"""Tool execution: one catalogue, one executor, and the reply queue it feeds.

Every tool the model may call is discovered from the tool host's own registry and
executed through the same loopback identity/permission gate. There is no second
list of names on any entry: a surface that publishes no tools simply has none, and
one that publishes them gets the whole catalogue. The reply queue (``chat_says``)
belongs here too because it is the result channel of the sending tools.
"""
from __future__ import annotations

from pathlib import Path

# A call may not outlive its own work; the turn's remaining budget still wins.
TOOL_TIMEOUTS = {'fish_capabilities': 20, 'fish_group_list': 20, 'fish_group_history': 30,
                 'fish_browser': 90, 'fish_computer': 30, 'fish_qq_action': 55,
                 'fish_web_search': 40, 'fish_web_fetch': 40, 'fish_emoji_save': 45,
                 # 一次看图是一次下载加一次视觉模型调用：最多两张，按两张的预算给。
                 'fish_image_look': 90}


def catalog(root, role, *, all_tools=False, session=''):
    """The tool host's current catalogue, including enabled MCP entries."""
    from tool_host import request
    return request(Path(root), '/catalog',{'all_tools':all_tools,'session':session}, role=role, admin=False)['tools']


def definitions(root, role, *, session=''):
    """The catalogue in the chat-completions function shape the model receives."""
    return [{'type': 'function', 'function': {key: item[key] for key in ('name', 'description', 'parameters')}}
            for item in catalog(root, role,session=session)]


def execute(root, role, session, name, arguments, call_id, timeout):
    """Run one published tool through the same loopback identity and permission gate."""
    from tool_host import request
    return request(Path(root), '/execute',
                   {'session': session, 'name': name, 'arguments': arguments, 'call_id': call_id},
                   role=role, admin=False, timeout=timeout)


def surface(root, session, role='qq'):
    """Which real sending surface this session has, or '' when it has no sender."""
    from tool_host import sender_surface
    return sender_surface(Path(root), session, role)


def say_queue(root, session):
    """What this turn has queued to say, oldest first; reading keeps it."""
    from tool_host import peek_says
    return peek_says(Path(root), session)


def take_says(root, session):
    """Hand the turn's queue to its one sender, once."""
    from tool_host import take_says as take
    return take(Path(root), session)


def clear_says(root, session):
    from tool_host import clear_says as clear
    clear(Path(root), session)


def note_preview(root, session, asset):
    from tool_host import note_preview as note
    note(Path(root), session, asset)


class ToolsModule:
    """The module as the assembly holds it: the same catalogue, bound to one root."""

    def __init__(self, runtime):
        self.root = runtime.root
        self.runtime = runtime

    def catalog(self, role='qq'):
        return catalog(self.root, role)

    def execute(self, role, session, name, arguments, call_id, timeout):
        return execute(self.root, role, session, name, arguments, call_id, timeout)

    def delivery_sources(self,execution):
        view=self.runtime.execution.turn_view(str(execution))
        events=(view.get('trace') or {}).get('events',[]) if view else []
        return list(dict.fromkeys(event['value']['name'] for event in events
            if event.get('kind')=='tool_result' and isinstance(event.get('value'),dict)
            and event['value'].get('name') in ('fish_say','fish_send_sticker','fish_chat_reply')
            and isinstance(event['value'].get('result'),dict)
            and event['value']['result'].get('status') in ('staged','prepared')))

    def result_material(self,run):
        """Terminal facts from the original stores, for the next ordinary thought."""
        import json
        import sqlite3
        from contextlib import closing
        from locations import project_path
        from storage import database
        with database(self.runtime.state.path) as db:
            row=db.execute("SELECT MAX(started) FROM agent_runs WHERE status IN ('complete','silent') AND id<>?",(str(run['id']),)).fetchone()
            since=float(row[0] or 0)
        facts=[]
        sources=[('task','data/delegation/tasks.db','tasks',
                  "SELECT id,title,status,result,error,updated FROM tasks WHERE updated>? AND status IN ('completed','failed','cancelled','unknown')"),
                 ('process','data/social/computer-jobs.db','jobs',
                  "SELECT id,status,exit_code,workdir,output,ended FROM jobs WHERE ended>?"),
                 ('delivery','data/social/experience.db','experience_turns',
                  "SELECT reference,outcome,error,receipt FROM experience_turns WHERE receipt>?")]
        for kind,relative,table,sql in sources:
            path=project_path(self.root,relative)
            if not path.is_file():continue
            with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
                db.row_factory=sqlite3.Row
                if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():continue
                facts.extend({'kind':kind,'instance_id':self.runtime.instance_id,**dict(row)} for row in db.execute(sql,(since,)))
        return ('[已受理工具/任务的实际后续结果；不是新的来信，不要求额外发言]\n'+
                json.dumps(facts,ensure_ascii=False)) if facts else ''
