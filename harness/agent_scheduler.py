"""One Fish wake queue for reliable plans and discretionary social opportunities."""
import time

from storage import database


def autonomous_enabled(root):
    path = root / 'data/social/owner-control.db'
    if not path.is_file():
        return False
    import sqlite3
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        columns = {r[1] for r in db.execute('PRAGMA table_info(scheduled)')}
        return bool('purpose' in columns and db.execute("SELECT 1 FROM scheduled WHERE engine='fish' AND purpose='autonomy' AND state='active'").fetchone())


class AgentScheduler:
    def __init__(self, root, stop):
        from bot_control import BotControl
        self.root, self.stop = root, stop
        self.control = BotControl(root)
        self.plans = self.control.plans
        self.runtime = None

    def _runtime(self):
        if self.runtime is None:
            from fish_agent import get_runtime
            self.runtime = get_runtime(self.root)
        return self.runtime

    def initialise(self):
        # A terminated execution might have acted before its final write. Never replay it.
        with database(self.control.path) as db:
            db.execute("UPDATE occurrences SET state='uncertain',note='上次后台在执行中退出，结果未确认，没有自动重做',updated=? WHERE state='running'", (time.time(),))
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='active_occurrences'").fetchone():
                db.execute('DELETE FROM active_occurrences')
            db.execute('CREATE TABLE IF NOT EXISTS automation_bootstrap(name TEXT PRIMARY KEY,created REAL)')
            seeded = db.execute("SELECT 1 FROM automation_bootstrap WHERE name='unified-wakes-v1'").fetchone()
        if seeded:
            return
        seeds = [
            # 两小时巡查由主执行的 45 分钟默认触发取代（06-01）：新的自主机会不再
            # 另建计划、另起一次生成；早晚问候仍是主人可见的明确计划。
            {'title': '早晨自主问候机会', 'daily': {'time': '07:00', 'time_zone': 'Asia/Shanghai'},
             'configuration_key': 'autonomy-morning', 'prompt': '现在是早晨，结合共同经历与真实近况，自己决定是否找主人或去有人的群问候、接话，或先做别的事。不要固定话术，休息中的群不打扰。'},
            {'title': '晚间自主问候机会', 'daily': {'time': '22:00', 'time_zone': 'Asia/Shanghai'},
             'configuration_key': 'autonomy-evening', 'prompt': '现在是晚上，结合共同经历与真实近况，自己决定是否关心主人、在仍有人交流的群聊两句或道晚安，或不行动。不要固定话术，也不把晚安当成要求别人停聊。'},
        ]
        for params in seeds:
            with database(self.control.path) as db:
                present = db.execute('SELECT 1 FROM scheduled WHERE configuration_key=?', (params['configuration_key'],)).fetchone()
            if not present:
                self.control.create({'target': 'owner', 'purpose': 'autonomy', **params})
        with database(self.control.path) as db:
            db.execute("INSERT OR IGNORE INTO automation_bootstrap VALUES('unified-wakes-v1',?)", (time.time(),))

    def loop(self):
        """One time wheel: detect due occurrences and submit them to the one execution.

        It never calls a model, never delivers, and never keeps a second queue: the
        occurrence reference is the deduplication key of the submitted turn.
        """
        from governance import Governance
        policy = Governance(self.root, 'qq')
        try:
            self.initialise()
            while not self.stop.is_set():
                try:
                    if not policy.config()['paused']:
                        from memory_reminder import discover
                        discover(self.control)
                        self.control.detect()
                        self._runtime().plans.dispatch()
                except Exception as exc:
                    policy.fault('fish-scheduler', exc, stage='scheduler')
                self.stop.wait(2)
        except Exception as exc:
            policy.fault('fish-scheduler', exc, stage='scheduler')

