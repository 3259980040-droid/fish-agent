"""A fixed instance view of the shared QQ adapter, with no connection or loop."""
import threading
from governance import Governance
from qq_store import QQStore


class QQInstanceRoute:
    def __init__(self, host, context):
        self.host, self.context = host, context
        self.root, self.owner = context.root, host.owner
        self.governance = Governance(self.root, 'qq')
        self.store = QQStore(self.root)
        self._stop = host._stop
        self.private_cancel = threading.Event()
        self.cancel_event = self.private_cancel

    async def _deliver_reply(self, *args, **kwargs):
        kwargs['instance_root'] = self.root
        return await self.host._deliver_reply(*args, **kwargs)

    async def _fault(self, source, error, *, stage='model', uncertain=False):
        return self.governance.fault(source, error, stage=stage, uncertain=uncertain)

    def watch_private(self, execution, item):
        return self.host.watch_private(execution, item, instance=self.context)
