"""QQ intake and delivery for the single Fish Agent; inputs never wait on thought."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import uuid
import threading
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from core import HarnessConfig, RuntimeManager
from dsh_web import DSHWebClient
from qq_store import QQStore
from multimodal import MediaService
from governance import Governance, control_token


# Housekeeping is independent of the Fish-owned wake queue.
MAINTENANCE_S = 300.0

# Why a group message went unanswered, read off the candidate's own reason text
# instead of guessed: the reservation refuses for one of these four, and the audit
# has to say which. A dive is also its own audit event, because "she was diving"
# is the one skip that is her own decision and not a limit of the group's.
SKIP_REASONS = (('潜水到', 'dive'), ('本次未被唤醒', 'not-woken'))


def skip_reason(candidate: dict) -> str:
    """'dive', 'not-woken', 'cursor-moved' or 'limits' for one refused reservation."""
    candidate = candidate if isinstance(candidate, dict) else {}
    reason = str(candidate.get('reason') or '')
    for prefix, label in SKIP_REASONS:
        if reason.startswith(prefix):
            return label
    # Still authorised for some cursor but not the one this turn held: a newer
    # message moved it, or another worker already holds the chat.
    if candidate.get('action') == 'candidate':
        return 'cursor-moved'
    return 'limits'


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


class ChannelClient:
    def __init__(self, url: str, token: str, on_event, checkpoint=None, on_health=None):
        self.url = url
        self.token = token
        self.on_event = on_event
        self.on_health = on_health
        self.ws = None
        self.epoch = None
        self.last_seq = 0
        self.checkpoint = Path(checkpoint) if checkpoint else None
        if self.checkpoint and self.checkpoint.exists():
            saved=json.loads(self.checkpoint.read_text(encoding='utf-8'))
            self.epoch=saved.get('epoch');self.last_seq=int(saved.get('seq',0))
        self.bot = None
        self.futures: dict[str, asyncio.Future] = {}
        self._seq = 0
        self._task: asyncio.Task | None = None
        self._event_task: asyncio.Task | None = None
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._processing = False
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._event_task = asyncio.create_task(self._dispatch_events(), name="fish-qq-core-events")
        self._task = asyncio.create_task(self._run(), name="fish-qq-core-client")

    async def stop(self) -> None:
        self._stop.set()
        if self.ws is not None:
            await self.ws.close()
        tasks = [task for task in (self._task, self._event_task) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _dispatch_events(self) -> None:
        # Keep event order while the receiver continues resolving action receipts.
        # An event handler may itself send a message and await its receipt.
        while not self._stop.is_set():
            frame = await self._events.get()
            self._processing = True
            try:
                await self.on_event(frame)
                if self.checkpoint and frame.get('epoch') and frame.get('seq'):
                    from storage import write_json
                    write_json(self.checkpoint,{'epoch':frame['epoch'],'seq':int(frame['seq'])})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log(f"fish-qq event failed: {type(exc).__name__}: {exc}")
                if self.on_health:
                    await self.on_health('receive', exc)
            finally:
                self._processing = False
                self._events.task_done()

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                async with connect(
                    self.url,
                    additional_headers={"Authorization": f"Bearer {self.token}"},
                    max_size=1024 * 1024,
                    open_timeout=5,
                ) as ws:
                    self.ws = ws
                    await ws.send(json.dumps({
                        "v": 1, "type": "hello", "id": f"core-{uuid.uuid4().hex[:8]}", "ts": "",
                        "payload": {"client": "fish-core", "last_seq": self.last_seq,
                                    "epoch": self.epoch if self.last_seq else None},
                    }))
                    hello = json.loads(await asyncio.wait_for(ws.recv(), 10))
                    if hello.get("type") != "hello.ok":
                        raise RuntimeError(f"hello failed: {hello}")
                    self.epoch = hello["payload"].get("epoch")
                    self.last_seq = int(hello["payload"].get("seq", 0))
                    self.bot = hello["payload"].get("bot")
                    log(f"fish-qq connected epoch={self.epoch} seq={self.last_seq}")
                    if self.on_health:
                        await self.on_health('connection', None)
                    async for raw in ws:
                        try:
                            frame = json.loads(raw)
                        except (TypeError, ValueError):
                            continue
                        if frame.get("type") == "event":
                            self.last_seq = max(self.last_seq, int(frame.get("seq", 0)))
                            self._events.put_nowait(frame)
                        elif frame.get("type") == "action.result" and frame.get("id") in self.futures:
                            future = self.futures.pop(frame["id"])
                            if not future.done():
                                future.set_result(frame)
                    if not self._stop.is_set():
                        raise ConnectionError('QQ 通道连接已关闭')
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log(f"fish-qq disconnected: {type(exc).__name__}: {exc}")
                self.ws = None
                if self.on_health:
                    await self.on_health('connection', exc)
            finally:
                self.ws = None
                for future in self.futures.values():
                    if not future.done():
                        future.set_exception(RuntimeError("fish-qq disconnected"))
                self.futures.clear()
            if not self._stop.is_set():
                await asyncio.sleep(1)

    async def action(self, action: str, payload: dict[str, Any], timeout: float = 30) -> dict[str, Any]:
        if self.ws is None:
            raise RuntimeError("fish-qq not connected")
        self._seq += 1
        action_id = f"core-{self._seq}-{uuid.uuid4().hex[:4]}"
        future = asyncio.get_running_loop().create_future()
        self.futures[action_id] = future
        try:
            await self.ws.send(json.dumps({
                "v": 1, "type": "action", "id": action_id, "action": action,
                "ts": "", "payload": payload,
            }))
            return await asyncio.wait_for(future, timeout)
        finally:
            self.futures.pop(action_id, None)


class QQAgentLoop:
    def __init__(self, root: Path, owner: str | None = None, agent_id: str = "qq"):
        self.root = root
        self.governance = Governance(root, agent_id)
        self.owner = str(owner) if owner is not None else self.governance.owner_id()
        self.agent_id = agent_id
        self.config = HarnessConfig(root)
        self.runtime = RuntimeManager(self.config)
        self.store = QQStore(root)
        self.channel = ChannelClient(
            os.environ.get("FISH_QQ_URL", "ws://127.0.0.1:3101/ws"),
            os.environ.get("FISH_QQ_TOKEN", ""),
            self.on_event,
            checkpoint=root/'harness/runtime/qq-channel-cursor.json',
            on_health=self._channel_health,
        )
        self.dsh: DSHWebClient | None = None
        self.session_id: str | None = None
        self.conversation: dict[str, Any] | None = None
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.worker: asyncio.Task | None = None
        self.cancel_event = asyncio.Event()
        self.active_turn: str | None = None
        self.current_text = ""
        self.control_server = None
        self._stop = asyncio.Event()
        self.group_ids: set[str] = set()
        self.group_mode = os.environ.get("FISH_QQ_GROUP_REPLY", "off").lower()
        try:
            cfg = json.loads((root / "channels" / "fish_qq" / "config.json").read_text(encoding="utf-8"))
            self.group_ids = {str(x) for x in (cfg.get("allowlist", {}).get("group_ids", []) or [])}
        except Exception:
            self.governance.fault('qq-channel', ValueError('QQ 通道配置无法读取'), stage='configuration')
        self.group_locks: dict[str, asyncio.Lock] = {}
        self.group_tasks: set[asyncio.Task] = set()
        self.group_inbox: dict[str, dict] = {}
        self.group_pumps: dict[str, asyncio.Task] = {}
        self.control_token = control_token(root)
        self._runtime_lock = asyncio.Lock()
        self.active_test_session = None
        self.group_send_locks = {}
        self.maintenance_task = None
        self.private_cancel = threading.Event()
        self.delivery_task = None
        self._notice_tasks: set[asyncio.Task] = set()
        # 群里有人说话时立刻叫醒维护循环里的回合：要不要开口仍由回合自己的闸门判定，
        # 这个事件只负责"现在去看一眼"，不等十几秒的切片。

    async def start(self) -> None:
        if not self.owner:
            self.governance.fault('qq-channel', ValueError('尚未配置主人账号'), stage='configuration')
            raise ValueError("尚未配置主人账号，请在系统设置中填写")
        self.control_server = await asyncio.start_server(self._control, "127.0.0.1", 3102)
        from group_policy import GroupPolicy
        GroupPolicy(self.root).recover_abandoned()
        self.conversation = self.store.get_or_create_conversation(f"private:{self.owner}", self.agent_id)
        # Keep the old native session in storage for history inspection only.
        # Owner private chat no longer resumes or mutates it.
        self.session_id = None
        await self.channel.start()
        self.worker = asyncio.create_task(self._worker(), name="qq-agent-worker")
        self.maintenance_task = asyncio.create_task(self._maintenance(), name='qq-local-data-maintenance')
        from owner_private import deliveries
        self.delivery_task = asyncio.create_task(deliveries(self), name='qq-owner-task-deliveries')
        log(f"QQ agent loop ready conversation={self.conversation['id']} session={self.session_id}")

    def _silent(self, source, *, stage='model'):
        self.governance.fault_outcome(source, 'silent', stage=stage, reason='她想过了，但选择不开口；不算故障')

    async def _fault(self, source, error, *, stage='model', uncertain=False):
        row = self.governance.fault(source, error, stage=stage, uncertain=uncertain)
        if row is not None:
            await self._fault_notice(row)
        return row

    async def _recovered(self, source, *, stage='model'):
        rows = self.governance.fault_recovered(source, stage=stage)
        for row in rows:
            await self._fault_notice(row, recovered=True)

    async def _fault_notice(self, row, *, recovered=False):
        # System receipts use owner_private.reply, not fish_say or a group sender.
        # An unavailable channel is shown in the UI; reconnect never replays a notice.
        if not self.owner or self.governance.config()['paused'] or getattr(self.channel, 'ws', None) is None:
            return
        token = self.governance.claim_fault_notice(row['source'], row['code'], stage=row['stage'], recovered=recovered)
        if not token:
            return
        result = {'sent': False, 'uncertain': False}
        try:
            from owner_private import fault_notice
            result = await fault_notice(self, row, token, recovered=recovered)
        except Exception as exc:
            # A storage error after delivery can lose the receipt too. Only a
            # definite local denial/cancellation proves the notice was not sent.
            result['uncertain'] = not isinstance(exc, (PermissionError, InterruptedError))
            log('system fault notice unavailable: ' + type(exc).__name__)
        finally:
            self.governance.finish_fault_notice(token, sent=bool(result.get('sent')),
                                                uncertain=bool(result.get('uncertain')))

    async def _channel_health(self, stage, error):
        if error is None:
            rows = self.governance.fault_recovered('qq-channel', stage=stage)
            # The websocket reader must resume before a notice can await its ACK.
            for row in rows:
                task = asyncio.create_task(self._fault_notice(row, recovered=True), name='qq-system-recovery')
                self._notice_tasks.add(task)
                task.add_done_callback(self._notice_tasks.discard)
        else:
            await self._fault('qq-channel', error, stage=stage)

    async def stop(self) -> None:
        self._stop.set()
        self.cancel_event.set()
        self.private_cancel.set()
        if self.delivery_task:
            self.delivery_task.cancel()
            await asyncio.gather(self.delivery_task, return_exceptions=True)
        if self.maintenance_task:
            self.maintenance_task.cancel()
            await asyncio.gather(self.maintenance_task,return_exceptions=True)
        tasks = list(self.group_tasks | self._notice_tasks)
        for task in tasks: task.cancel()
        if tasks: await asyncio.gather(*tasks, return_exceptions=True)
        if self.control_server is not None:
            self.control_server.close()
            await self.control_server.wait_closed()
        if self.worker is not None:
            self.worker.cancel()
            try:
                await self.worker
            except (asyncio.CancelledError, Exception):
                pass
        await self.channel.stop()
        self.runtime.stop(self.agent_id)

    def _maintenance_seconds(self):
        """The outer wait is what actually paces the autonomous round."""
        from storage import read_json
        settings = dict(read_json(self.root / 'harness/control.json'))
        settings.update(read_json(self.root / 'harness/context-settings.json', {}) or {})
        try:
            value = float(settings.get('maintenance_interval_s', 60))
        except (TypeError, ValueError):
            value = 60.0
        return max(20.0, min(MAINTENANCE_S, value))

    async def _maintenance(self):
        from data_retention import DataRetention
        from group_language import GroupLanguage
        from daily_curate import tick as daily_curate_tick
        GroupLanguage(self.root)
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(DataRetention(self.root).run)
            except Exception as exc:
                self.governance.audit('data_maintenance_failed',type(exc).__name__)
            try:
                # 自主回合：原来没人调用（重构后断了线），这里接回同一个维护循环。
                # 节拍与退避由 group_proactive 自己按 context-settings.json 判断。
                from group_proactive import tick as autonomous_round
                await autonomous_round(self)
            except Exception as exc:
                self.governance.audit('autonomous_round_failed', type(exc).__name__)
            try:
                # 每日整理唯一入口：清理记忆 + 当天小结 + 挑表情；
                # 小结由它内部调用 memory_digest.daily，这里不再单独调一遍，
                # 否则同一份小结有两个触发方，各自记账、各自调模型。
                await asyncio.to_thread(daily_curate_tick, self.root)
            except Exception as exc:
                self.governance.audit('daily_curate_failed', type(exc).__name__)
            # All reminders and autonomous opportunities are owned by AgentScheduler.
            # This maintenance pass only handles non-social housekeeping and fault notices.
            for fault in self.governance.fault_snapshot()['items']:
                if fault['consecutive'] > 0:
                    await self._fault_notice(fault)
            try:
                await asyncio.wait_for(self._stop.wait(), self._maintenance_seconds())
            except TimeoutError:
                pass

    async def on_event(self, frame: dict[str, Any]) -> None:
        if frame.get("event") != "message.received":
            return
        payload = frame.get("payload") or {}
        sender = str((payload.get("sender") or {}).get("id", ""))
        if await self._mode_command(payload, sender):
            return
        from group_language import command as language_command
        if await language_command(self,payload,sender):return
        from group_tests import command as group_test_command
        if await group_test_command(self, payload, sender):
            return
        from owner_tests import command
        if await command(self, payload, sender):
            return
        from life_commands import handle as life_command
        if await life_command(self, payload, sender):
            return
        if await self._admin_command(payload, sender):
            return
        if payload.get("chat_type") == "group":
            await self._on_group(payload)
            return
        if payload.get("chat_type") != "private":
            return
        if sender != self.owner or payload.get("chat_id") != f"private:{self.owner}":
            return
        text = "".join(
            seg.get("text", "") for seg in payload.get("segments", [])
            if seg.get("type") == "text"
        ).strip()
        if not text:
            text = "".join({"image":"[图片]", "video":"[视频]", "audio":"[语音]", "file":"[文件]"}.get(s.get("type"), "")
                           for s in payload.get("segments", [])) or "[附件消息]"
        conversation = self.conversation
        paused = self.governance.config()['paused']
        try:
            message_id = self.store.record_message(
                conversation["id"], payload["chat_id"], self.owner, "inbound",
                str(payload.get("message_id", "")), payload.get("reply_to"),
                payload.get("segments", []), text, "paused" if paused else "queued",
            )
        except Exception as exc:  # duplicate channel_message_id or DB error
            log(f"inbound duplicate/error: {type(exc).__name__}: {exc}")
            return
        if paused:
            self.store.update_message(message_id, error="主人已暂停回复，消息已跳过")
            log(f"owner inbound skipped while paused message_id={message_id}")
            return
        await self.queue.put({"message_id": message_id, "payload": payload, "text": text,
                              'space_id': '', 'conversation': conversation})
        log(f"owner inbound queued message_id={message_id} text_len={len(text)}")

    async def _on_group(self, payload: dict[str, Any]) -> None:
        from group_policy import GroupPolicy
        chat = str(payload.get('chat_id', ''))
        sender = str((payload.get('sender') or {}).get('id') or '')
        if not sender or sender == str((self.channel.bot or {}).get('id') or ''):
            return
        seen = GroupPolicy(self.root).observe(payload)
        if not seen.get('accepted'):
            return
        text = ''.join(str(part.get('text') or '') for part in payload.get('segments') or [] if part.get('type') == 'text')
        self._fish_record(chat, payload, text=text, direction='inbound', status='observed', reply_to=str(payload.get('reply_to') or ''))
        self._fish_start({**payload, '_fish_observed': seen}, chat)

    def _fish_start(self, payload: dict[str, Any], chat: str) -> None:
        # All records are saved on arrival. The next thought snapshots their actual
        # prefix, so a busy group never needs a silence window to make progress.
        self.group_inbox[chat] = payload
        current = self.group_pumps.get(chat)
        if current is not None and not current.done():
            return
        async def drain():
            while chat in self.group_inbox and not self._stop.is_set():
                latest = self.group_inbox.pop(chat)
                await self._fish_reply(latest, chat)
        task = asyncio.create_task(drain(), name='fish-group-inbox:' + chat)
        self.group_pumps[chat] = task
        self.group_tasks.add(task)
        task.add_done_callback(self._group_task_done)

    def _group_task_done(self, task: asyncio.Task) -> None:
        self.group_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            # Reading it keeps asyncio from reporting an exception nobody awaits.
            log('group fish task ended: ' + type(task.exception()).__name__)

    def _fish_live(self, policy, token) -> bool:
        """Still allowed to send: the owner has not paused and the reservation is mine.

        Deliberately not policy.valid(): a message that arrived while this reply was
        generating would fail that check, and no reply would ever leave. The single
        sender is still guaranteed - no other task can reserve this chat meanwhile.

        policy.live() re-reads the chat's authorisation as well (engine=fish, listen,
        reply), so this one predicate also answers 'was the engine rolled back or a
        switch withdrawn while I was generating?'. Every send point asks it again:
        before the reply is released and once per segment inside _deliver_reply.
        """
        return not self.governance.config()['paused'] and bool(policy.live(token))

    def _fish_record(self, chat, payload, *, text, direction, status, remote='', reply_to=''):
        """What came in and what the channel accepted, in the group's own ledger.

        Only a row the group really saw may become history ('sent'), so an outbound
        whose receipt is unknown is stored as pending and never enters the next view.
        """
        try:
            conversation = self.store.get_or_create_conversation(chat, 'qq')
            if direction == 'outbound':
                sender = 'bot'
                channel_id = remote
                segments = [{'type': 'text', 'text': text}] if text else []
            else:
                sender = str((payload.get('sender') or {}).get('id') or '')
                channel_id = str(payload.get('message_id') or '')
                segments = [s for s in (payload.get('segments') or []) if isinstance(s, dict)]
            self.store.record_message(conversation['id'], chat, sender, direction, channel_id,
                                      reply_to or None, segments, text, status)
        except Exception as exc:  # a ledger miss must not undo a reply that already happened
            log('group fish ledger skipped: ' + type(exc).__name__)

    def _fish_remember(self, chat: str, payload: dict[str, Any], text: str) -> dict:
        """Queue one observed group message for learning, in this chat's own scope.

        The switch is the chat's own learn flag, read here and nowhere else: a group
        that is not learning writes one row less, and nothing about the bot's own
        outbound is ever learned (what Fish said is not a fact about the group; the
        ledger row _fish_record already keeps it as history).

        The visible range is this chat and only this chat: the scope is derived from
        chat_id alone and is never the owner's, never another group's and never a
        caller-supplied value, so a group's experience stays recallable by that group
        (MemoryStore 'chat:'+chat) and invisible to the owner's private recall.

        One message, one source. The platform message id is preferred; when the
        transport carries none, the payload's own local row id stands in, written
        with its own prefix and reported back as id_kind. A message with neither is
        skipped and audited: two unlabelled messages would otherwise share one source
        string, the second would be deduplicated as a redelivery, and its learning
        would silently never happen.

        Returns what happened, so that "nothing was queued" can never be read as
        "it was saved": {'learned': bool, 'reason': str, ...}. No return value here is
        a failure; the caller's reply path is unaffected either way.
        """
        from group_policy import GroupPolicy
        if not text.strip():
            return {'learned': False, 'reason': 'empty-text'}
        if not GroupPolicy(self.root).config(chat)['learn']:
            return {'learned': False, 'reason': 'learn-off'}
        from memory_store import MemoryStore
        mid = str(payload.get('message_id') or '').strip()
        ident, kind = '', ''
        if 0 < len(mid) <= 200:
            ident, kind = mid, 'message_id'
        else:
            # The qq ledger's own auto-increment row id is unique per message and is
            # what a transport without platform ids can still offer.
            for key in ('local_id', 'row_id'):
                value = str(payload.get(key) or '').strip()
                if 0 < len(value) <= 200:
                    ident, kind = value, key
                    break
        if not ident:
            self.governance.audit('group_fish_memory_skipped', '消息缺少编号，未学习 ' + chat)
            return {'learned': False, 'reason': 'missing-message-id'}
        store = MemoryStore(self.root, 'qq')
        if not store.options()['auto_learning']:
            # The group is learning but the owner switched automatic learning off:
            # enqueue would write nothing and say nothing. Record the reason instead
            # of leaving silence that a caller can mistake for a save.
            self.governance.audit('group_fish_memory_off', '自动学习已关闭，未学习 ' + chat)
            return {'learned': False, 'reason': 'auto-learning-off'}
        # scope = 'chat:' + this chat_id, the same string MemoryStore recalls and
        # group_decide._group_memory queries with. No other scope may be passed here.
        # A local id keeps its own prefix so it can never collide with a platform
        # message id that happens to carry the same digits.
        source = ('qq-group:' + chat + ':' + ident if kind == 'message_id'
                  else 'qq-group:' + chat + ':' + kind + ':' + ident)
        stored = store.enqueue(
            source, text, 'chat:' + chat,
            subject='qq:' + str((payload.get('sender') or {}).get('id') or ''))
        outcome = {'learned': True, 'reason': '', 'source': source, 'id_kind': kind}
        if isinstance(stored, dict) and stored.get('duplicate'):
            outcome.update(learned=False, reason='duplicate')
        elif isinstance(stored, dict) and stored.get('skipped'):
            outcome.update(learned=False, reason=str(stored['skipped']))
        return outcome

    async def _fish_reply(self, payload: dict[str, Any], chat: str, cancelled=None) -> None:
        """engine=fish: one generated reply for one group cursor, sent once.

        The single sender is GroupPolicy.reserve/finish: one live reservation per
        chat and one per cursor, so of two inbound messages in one window, a
        redelivered copy or a competing worker, exactly one wins and the others
        return before any model call. The text leaves through _deliver_reply, the
        group's only send path, quoted to the inbound message_id, and a receipt
        that may have landed is finished as uncertain and never retried.
        """
        from group_policy import GroupPolicy
        # 接力点名的放行由群策略判定，但它要留痕在群审计里：把记账口交进去，判定和
        # 记账就发生在同一个决定上，不需要在主循环里把同一条规则再算一遍。
        policy = GroupPolicy(self.root, audit=self.governance.audit)
        cancel = cancelled if cancelled is not None else threading.Event()

        def audit(kind, reason):
            self.governance.audit(kind, reason[:250] + ' ' + chat)

        def stopped():
            return bool(cancel.is_set() or self.cancel_event.is_set())

        if self.governance.config()['paused']:
            return audit('group_fish_skipped', 'paused')
        sender = str((payload.get('sender') or {}).get('id', ''))
        if not sender or sender == str((self.channel.bot or {}).get('id', '')):
            return audit('group_fish_skipped', 'own-or-missing-sender')
        if stopped():
            return audit('group_fish_skipped', 'cancelled')
        seen = payload.get('_fish_observed') or policy.observe(payload)
        if not seen.get('accepted'):
            return  # duplicate copy, or this group is not listening: nothing to answer
        cursor = int(seen['cursor'])
        kind = 'direct' if (payload.get('mentions_bot') or payload.get('reply_to_bot')) else 'active'
        inbound_text = ''.join(str(s.get('text') or '') for s in (payload.get('segments') or [])
                               if isinstance(s, dict) and s.get('type') == 'text')
        if not payload.get('_fish_observed'):
            self._fish_record(chat, payload, text=inbound_text, direction='inbound',
                              status='observed', reply_to=str(payload.get('reply_to') or ''))
        # After this group accepted the message and before any generation: what the
        # group said becomes this group's memory when it is learning. A memory miss
        # is audited and costs the group nothing it was otherwise going to get.
        try:
            self._fish_remember(chat, payload, inbound_text)
        except Exception as exc:  # noqa: BLE001 - a memory miss never costs a reply
            audit('group_fish_memory_failed', type(exc).__name__ + ': ' + str(exc)[:200])
        # 没有字不等于没有内容：纯图/纯表情/语音这一条的字是空的，图还在段里。她"看
        # 不见"的正是这种消息，所以先问一句这条到底有没有带东西，再决定要不要把"只 @
        # 了你，没有写正文"交下去——带了图就不是"没写正文"。
        from group_decide import media_line
        media = media_line([s for s in (payload.get('segments') or []) if isinstance(s, dict)],
                           str((payload.get('sender') or {}).get('display_name')
                               or (payload.get('sender') or {}).get('name') or ''))
        if kind == 'direct' and not inbound_text.strip() and not media:
            # A bare @ carries no words, so the model used to see an empty turn and
            # answer nothing: being called out was treated as having nothing to say.
            # Tell it what actually happened instead of leaving the turn empty.
            payload = {**payload, 'segments': [*(payload.get('segments') or []),
                                               {'type': 'text', 'text': '[对方只 @ 了你，没有写正文]'}]}
        token = policy.reserve(chat, cursor, kind)
        refused: dict = {}
        if not token:
            # A newer message took the cursor during the merge window. The old DSH
            # path had a later tick that answered the newest message; the fish path
            # runs once per inbound, so without this retarget a burst is answered by
            # nobody and the group goes silent. Answer the newest message instead.
            snapshot = policy.snapshot(chat, limit=1)
            candidate = policy.candidate(chat)
            refused = candidate
            if candidate.get('action') == 'candidate':
                token = policy.reserve(chat, int(candidate['cursor']), candidate['kind'])
                if token:
                    cursor, kind = int(candidate['cursor']), candidate['kind']
                    newest = (snapshot.get('messages') or [None])[-1]
                    if isinstance(newest, dict) and newest.get('message_id'):
                        payload = {
                            'chat_id': chat, 'message_id': newest.get('message_id'),
                            'sender': newest.get('sender') or {},
                            'segments': newest.get('segments') or [],
                            'reply_to': newest.get('reply_to') or '',
                            'mentions_bot': bool(newest.get('direct')),
                            'reply_to_bot': False,
                        }
        if not token:
            reason = skip_reason(refused)
            if reason == 'dive':
                self.governance.audit('group_fish_dived', '潜水期间被叫到也没开口 ' + chat)
            return audit('group_fish_skipped', reason)
        sent = uncertain = False
        try:
            outcome = await self._fish_turn(policy, chat, payload, token, kind, cancel, sender, audit)
            sent, uncertain = outcome['sent'], outcome['uncertain']
        finally:
            policy.finish(token, sent, uncertain=uncertain)
    async def _fish_turn(self, policy, chat: str, payload: dict[str, Any], token: str, kind: str,
                         cancel, sender: str, audit) -> dict:
        """Generate, judge and deliver the reply one live reservation allows.

        Shared by the first answer and by the single catch-up answer of the followup
        window, so both go through the same _fish_live rechecks and worth_speaking:
        the window adds a turn, never a bypass.
        """
        from group_decide import STICKER_ONLY, fish_candidate

        def stopped():
            return bool(cancel.is_set() or self.cancel_event.is_set())

        def outcome(sent=False, uncertain=False):
            return {'sent': sent, 'uncertain': uncertain}

        source, stage = 'group-fish:' + chat, 'binding'
        try:
            if stopped() or self.governance.config()['paused']:
                audit('group_fish_skipped', 'paused-or-cancelled'); return outcome()
            # The group's own tool identity, bound before the model runs: /execute then
            # answers for this chat's scope only. A binding that cannot be made means
            # no tools and no reply, never a silent fall back to an owner identity.
            from tool_host import bind_session
            try:
                from social_context import SocialContext
                SocialContext(self.root).active_persona('qq')
                bind_session(self.root, 'qq', 'group-fish:' + chat, 'chat:' + chat, sender)
            except Exception as exc:  # noqa: BLE001 - fail closed, the group goes unanswered
                await self._fault(source, exc, stage='binding')
                audit('group_fish_skipped',
                      'no-tool-identity: ' + type(exc).__name__ + ': ' + str(exc)[:120]); return outcome()
            await self._recovered(source, stage='binding')
            stage = 'model'
            self.governance.consume('calls')  # the same paid-call accounting the DSH group path uses
            candidate = await asyncio.to_thread(fish_candidate, self.root, chat, payload=payload, cancelled=cancel)
            candidate = candidate if isinstance(candidate, dict) else {}
            text = str(candidate.get('text') or '').strip()
            # 这一轮要发的是候选自己组好的段：文字在前，表情在后。没给段就退回纯文字。
            segments = [s for s in (candidate.get('segments') or []) if isinstance(s, dict)]
            if str(candidate.get('error') or '').startswith('TurnSilent'):
                # 她这一轮没有调用发送工具：turn_no_send 已经由决策层写进审计，这里
                # 只结束这次预留——不算失败，也不算发过，更不会把她的正文送出去。
                audit('group_fish_no_send', '她这一轮选择不开口，没有发出任何消息')
                self._silent(source)
                return outcome()
            if candidate.get('error') or candidate.get('skipped') or not (text or segments):
                if candidate.get('error') or not candidate.get('skipped'):
                    await self._fault(source, candidate.get('error') or ValueError('模型没有返回可用结果'))
                audit('group_fish_failed',
                      str(candidate.get('error') or candidate.get('skipped') or 'empty-reply')[:200])
                return outcome()
            await self._recovered(source, stage='model')
            if stopped() or not self._fish_live(policy, token):
                audit('group_fish_skipped', 'cancelled-or-invalid-before-speaking'); return outcome()
            audit('group_fish_decided', '同一 Fish 回合决定发送，未经过第二次模型打分或否决')
            if stopped() or not self._fish_live(policy, token):
                audit('group_fish_skipped', 'cancelled-or-invalid-before-deliver'); return outcome()
            key = 'group-fish:' + str(payload.get('message_id') or uuid.uuid4().hex)
            # 她要说的每一句都是她自己交出来的一条气泡：按顺序一条一条发，不把几条
            # 拼回去——她分成两句说的，群里看到的就该是两句。
            from chat_output import delivery_plan
            bubbles = [bubble for bubble in (candidate.get('bubbles') or []) if isinstance(bubble, dict)]
            plan = delivery_plan(bubbles) if bubbles else None
            stage = 'delivery'
            from experience import delivery
            reference = candidate.get('source_reference', '')
            if reference:
                delivery(self.root, reference, sending=True)
            delivered = await self._deliver_reply(
                chat, segments or [{'type': 'text', 'text': text}], key, cancelled=cancel,
                validate=lambda: self._fish_live(policy, token),
                reply_to=str(payload.get('message_id') or ''), bubble_plan=plan)
            sent = bool(delivered.get('ok'))
            error = delivered.get('error') or {}
            uncertain = not sent and bool(error.get('may_have_sent'))
            if reference:
                delivery(self.root, reference, outcome='sent' if sent else 'uncertain' if uncertain else 'failed')
            self._fish_record(chat, payload, text=text, direction='outbound',
                              status='sent' if sent else 'uncertain' if uncertain else 'failed',
                              remote=str((delivered.get('payload') or {}).get('message_id') or ''),
                              reply_to=str(payload.get('message_id') or ''))
            if not sent:  # an unknown receipt is recorded here and never resent
                await self._fault(source, error, stage='delivery', uncertain=uncertain)
                audit('group_fish_uncertain' if uncertain else 'group_fish_failed',
                      str(error.get('code') or 'send-failed')[:200])
            else:
                await self._recovered(source, stage='delivery')
            if sent and kind == 'active':
                # Only a line the channel really accepted costs energy: a generation
                # that was judged not worth saying, or a send that failed, is free.
                try:
                    cfg = policy.config(chat)
                    if cfg['energy_enabled']:
                        policy.spend_energy(chat, float(cfg['energy_cost']))
                except Exception as exc:  # noqa: BLE001 - the bubble already left; a ledger miss must not undo it
                    audit('group_energy_failed', type(exc).__name__ + ': ' + str(exc)[:160])
            return outcome(sent, uncertain)
        except TimeoutError:
            await self._fault(source, TimeoutError('本轮等待超时'), stage=stage)
            audit('group_fish_timeout', '实际模型请求或工具等待超时，未冒充沉默'); return outcome()
        except Exception as exc:  # noqa: BLE001 - one failed reply never breaks the group path
            await self._fault(source, exc, stage=stage)
            audit('group_fish_failed', type(exc).__name__ + ': ' + str(exc)[:200]); return outcome()

    async def _worker(self) -> None:
        while not self._stop.is_set():
            item = await self.queue.get()
            try:
                from experience import was_considered
                if was_considered(self.root, 'qq:' + str(item['message_id']), session='qq-private:' + item['conversation']['id']):
                    self.store.update_message(item['message_id'], status='considered')
                    continue
                await self._process(item)
            except Exception as exc:  # noqa: BLE001
                log(f"turn error: {type(exc).__name__}: {exc}")
                from dsh_web import safe_runtime_error
                self.store.update_message(item['message_id'], status='blocked' if isinstance(exc, PermissionError) else 'failed',
                                          error=safe_runtime_error(str(exc) or type(exc).__name__))
                from owner_private import window_current
                await self._failure_notice(item, exc, validate=lambda: window_current(self, item))
            finally:
                self.queue.task_done()

    async def _process(self, item: dict[str, Any]) -> None:
        if self.governance.config()["paused"]:
            self.store.update_message(item["message_id"], status="paused", error="主人已暂停回复")
            return
        from owner_private import process
        await process(self, item)
        from storage import database
        with database(self.store.db_path) as db:
            row = db.execute('SELECT status,error,runtime_turn_id FROM messages WHERE id=?', (item['message_id'],)).fetchone()
            turn = db.execute('SELECT status FROM runtime_turns WHERE id=?', (row['runtime_turn_id'],)).fetchone() if row and row['runtime_turn_id'] else None
        if turn and turn['status'] == 'silent':
            self._silent('owner-private')
        elif row and row['status'] in ('processed', 'delegated'):
            if turn and turn['status'] == 'done':
                await self._recovered('owner-private', stage='model')
            await self._recovered('owner-private', stage='delivery')
        elif row and row['status'] == 'uncertain':
            await self._fault('owner-private', {'code': row['error'], 'may_have_sent': True}, stage='delivery', uncertain=True)
        elif row and row['status'] in ('failed', 'blocked'):
            # Generation failures have already passed _failure_notice. A completed
            # model turn with send_failed is the ordinary sender's own receipt.
            if turn and turn['status'] == 'send_failed':
                await self._fault('owner-private', row['error'], stage='delivery')

    def _finish_cancelled(self, message_id, turn_id, session, error):
        self.store.update_message(message_id, status="cancelled", cancelled=True,
                                  runtime_session_id=session or '', runtime_turn_id=turn_id)
        self.store.finish_turn(turn_id, "cancelled", error=error)
        self.active_turn = None

    def _finish_failed(self, message_id, turn_id, session, exc):
        from dsh_web import safe_runtime_error
        status = 'cancelled' if isinstance(exc, InterruptedError) or self.cancel_event.is_set() else 'failed'
        error = safe_runtime_error(str(exc) or type(exc).__name__)
        self.store.update_message(message_id, status=status, error=error, cancelled=status == 'cancelled',
                                  runtime_session_id=session or '', runtime_turn_id=turn_id)
        self.store.finish_turn(turn_id, status, error=error)
        self.active_turn = None
        log(f"owner turn {turn_id} {status}: {error}")

    async def cancel(self) -> dict[str, Any]:
        self.cancel_event.set()
        self.private_cancel.set()
        for task in list(self.group_tasks):
            task.cancel()
        if self.active_turn and self.active_turn.startswith("fish-private-"):
            return {"ok": True, "turn": self.active_turn, "note": "本地已取消等待；不保证服务商立即停止计算"}
        if self.active_turn:
            result = await asyncio.to_thread(self.runtime.cancel, self.agent_id, self.active_test_session or self.session_id)
            log(f"cancel requested turn={self.active_turn} result={result.get('ok')}")
            return {"ok": result.get("ok", False), "turn": self.active_turn, "result": result}
        return {"ok": False, "error": "no active turn"}

    async def _deliver_reply(self, chat_id, segments, key, cancelled=None, validate=None, reply_to=None, *,
                             validation_error='群配置、人格发布或频率已改变，未继续发送', bubble_plan=None):
        if chat_id == 'private:' + self.owner and not self.governance.config()['send_policy']['owner_private']['owner_reply']:
            raise PermissionError('主人已关闭私聊自动回复发送')
        from chat_output import transport_parts
        group = str(chat_id).startswith('group:')
        plan = bubble_plan if bubble_plan is not None else [{'segments': segments, 'delay': 0.}]
        text = ''.join(str(s.get('text') or '') for item in plan for s in item['segments'] if s.get('type') == 'text')
        self.governance.check_output(text)
        bubbles = [{'index': index, 'state': 'not_sent', 'message_ids': []} for index in range(len(plan))]
        parts = [{**part, 'key': key + part['key_suffix']} for part in transport_parts(plan=plan, group=group)]
        sent = []

        def stopped():
            if validate is not None and not validate():
                return {'code': validation_error, 'validation_failed': True}
            cfg = self.governance.config()
            if cfg['paused'] or (cancelled and cancelled.is_set()):
                return {'code': f'已暂停或取消，已发送 {len(sent)}/{len(parts)} 段', 'cancelled': True}
            if chat_id == 'private:' + self.owner and not cfg['send_policy']['owner_private']['owner_reply']:
                return {'code': '主人已关闭私聊自动回复发送', 'blocked': True}
            return None

        def failed(part, detail):
            bubble = bubbles[part['bubble']]
            bubble['state'] = ('partial' if bubble['message_ids'] else 'uncertain' if detail.get('may_have_sent')
                else 'cancelled' if detail.get('validation_failed') or detail.get('cancelled')
                else 'blocked' if detail.get('blocked') else 'failed')
            return {'ok': False, 'payload': {'message_ids': sent, 'bubbles': bubbles},
                    'error': {**detail, 'may_have_sent': bool(sent) or bool(detail.get('may_have_sent')),
                              'sent_parts': len(sent), 'total_parts': len(parts)}}

        def emoji_reason(part):
            """The store's own words for why this sticker could not go out now.

            通道对所有表情拒绝只回一个 emoji_unavailable，那句话读起来像"这张图
            不能用"，而真相往往是这张刚在这个群里发过、还在冷却，或者今天的额度
            用完了。库里同一张表情私聊和群里都认，所以把真实原因记下来，别让她和
            主人都以为表情本身发不出去。
            """
            asset = next((str(s.get('asset_id') or '') for s in part.get('segments') or []
                          if isinstance(s, dict) and s.get('asset_id')), '')
            if not asset:
                return ''
            try:
                from emoji_store import EmojiStore
                return EmojiStore(self.root).send_problem(asset, str(chat_id))
            except Exception as exc:  # noqa: BLE001 - a diagnosis miss never changes the send outcome
                return ''

        def emoji_failed(part, detail):
            """表情段失败只记账：文字已经送达，这一条不重发，也不把整条报成没发。"""
            if not (group and part.get('emoji') and sent):
                return False
            reason = emoji_reason(part)
            code = str(detail.get('code') or 'send-failed')
            self.governance.audit('emoji_failed_in_group',
                (str(chat_id) + ' ' + code + ('：' + reason if reason else ''))[:250])
            bubbles[part['bubble']]['state'] = 'partial'
            return True

        for index, part in enumerate(parts):
            until = time.monotonic() + part['delay']
            while True:
                stop = stopped()
                if stop:
                    return failed(part, stop)
                remaining = until - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(.1, remaining))
            try:
                result = await self.channel.action('send_message', {'chat_id': chat_id, 'segments': part['segments'],
                    **({'reply_to': str(reply_to)} if reply_to and index == 0 else {}),
                    'idempotency_key': part['key']}, timeout=30)
            except Exception:
                detail = {'code': f'第 {index+1}/{len(parts)} 段回执未取得；已确认送达 {len(sent)} 段，未自动重发',
                          'may_have_sent': True}
                if emoji_failed(part, detail):
                    continue
                if not stopped():
                    await self._delivery_notice(chat_id,key,index+1,len(parts),len(sent),reply_to,uncertain=True)
                return failed(part, detail)
            if not result.get('ok'):
                detail = result.get('error') or {}
                code = f"第 {index+1}/{len(parts)} 段发送失败：{detail.get('code','未知')}；已发送 {len(sent)} 段，未自动重试"
                if emoji_failed(part, {**detail, 'code': code}):
                    continue
                if not stopped():
                    await self._delivery_notice(chat_id,key,index+1,len(parts),len(sent),reply_to,uncertain=bool(detail.get('may_have_sent')))
                return failed(part, {**detail, 'code': code})
            sent.append(str((result.get('payload') or {}).get('message_id', '')))
            bubble = bubbles[part['bubble']]
            bubble['message_ids'].append(sent[-1])
            bubble['state'] = 'sent' if part['last'] else 'partial'
        return {'ok': True, 'payload': {'message_id': sent[-1], 'message_ids': sent, 'bubbles': bubbles}}

    async def _delivery_notice(self, chat, key, part, total, sent, quote, *, uncertain=False):
        message = (f'第{part}/{total}段没有取得发送回执' if uncertain else f'第{part}/{total}段发送失败')
        message += f'，已确认送达{sent}段。原回复已保留，没有自动重发整条。'
        try:
            await self.channel.action('send_message', {'chat_id':chat,'segments':[{'type':'text','text':message}],
                **({'reply_to':str(quote)} if quote else {}), 'idempotency_key':key+':delivery-notice'},timeout=10)
        except Exception as exc:
            log('delivery notice unavailable: ' + type(exc).__name__)

    async def _failure_notice(self, item, error, key='', *, validate=None):
        if isinstance(error, InterruptedError) or self.cancel_event.is_set():
            return
        await self._fault('owner-private', error, stage='model')

    def _reply_segments(self, session, text, reasoning=""):
        from tool_host import request
        from chat_output import segments as bubble_segments
        draft = request(self.root, '/chat/take', {'session': session}, role='qq')
        asset = request(self.root, '/emoji/take', {'session': session}, role='qq').get('asset')
        from reply_pipeline import display_mode
        from owner_reply import format_owner_reply
        mode = display_mode(self.root)
        text = format_owner_reply(text, reasoning, mode)
        if draft.get('bubbles'):
            if mode != "final":
                first = next((b for b in draft["bubbles"] if b.get("kind") == "text"), None)
                if first: first["text"] = format_owner_reply(first["text"], reasoning, mode)
            # Ordinary owner chat/work uses the same prepared output as an
            # isolated window. Do not replace a real media draft with final prose.
            parts = [segment for bubble in draft['bubbles'] for segment in bubble_segments(bubble)]
            texts = [part['text'] for part in parts if part['type'] == 'text']
            media = [part for part in parts if part['type'] != 'text']
            return ([{'type': 'text', 'text': '\n'.join(texts)}] if texts else []) + media
        return ([{'type': 'text', 'text': text}] if text else []) + ([{'type': 'image', 'asset_id': asset}] if asset else [])

    def owner_mode(self):
        from owner_mode import OwnerMode
        return OwnerMode(self.root)

    async def _mode_command(self, payload: dict, sender: str) -> bool:
        """Explain the retired private mode command without changing old state."""
        from owner_mode import mode_command
        text = ''.join(s.get('text', '') for s in (payload.get('segments') or []) if s.get('type') == 'text').strip()
        action = mode_command(text)
        if not action:
            return False
        if (payload.get('chat_type') != 'private' or not self.owner or sender != self.owner
                or payload.get('chat_id') != f'private:{self.owner}' or not self.governance.is_owner(sender)):
            return False
        await self._deliver_reply(f'private:{self.owner}', [{'type': 'text', 'text':
            '主人私聊已统一为独立Fish聊天，不再切换旧工作模式。明确的新文件交办会自动委托DSH；也可以用 /电脑任务 内容。'
            '查看进度用 /任务状态，停止用 /取消任务。'}], 'owner-mode-retired:' + str(payload.get('message_id', uuid.uuid4().hex)))
        return True

    async def _admin_command(self, payload: dict, sender: str) -> bool:
        text = "".join(s.get("text", "") for s in payload.get("segments", []) if s.get("type") == "text").strip()
        commands = {"/状态": "status", "/暂停": "pause", "/继续": "resume", "/取消": "cancel",
                    "停": "pause", "别说了": "pause", "闭嘴": "pause", "安静": "pause", "继续": "resume", "出来吧": "resume"}
        capability = commands.get(text)
        if not capability or payload.get("chat_type") != "private" or sender != self.owner or not self.governance.is_owner(sender):
            return False
        # An explicit test owner still uses the same permission allow-list.
        if capability not in self.governance.config()["owner_permissions"]:
            self.governance.audit("denied", "主人账号未启用权限：" + capability)
            return True
        if capability in ("pause", "resume"):
            self.governance.save({"paused": capability == "pause"})
            if capability == "pause":
                while not self.queue.empty():
                    item = self.queue.get_nowait()
                    self.store.update_message(item['message_id'], status='paused', error='主人已暂停回复，消息已跳过')
                    self.queue.task_done()
                await self.cancel()
            response = "已暂停回复。发 /继续 恢复。" if capability == "pause" else "已恢复回复。"
        elif capability == "cancel":
            await self.cancel()
            response = "已取消当前任务。"
        else:
            cfg = self.governance.snapshot()
            permissions = "、".join(self.governance.owner_capabilities().values()) or "无"
            response = f"主人权限已核验。当前 Fish 管理权限：{permissions}。{'已暂停' if cfg['config']['paused'] else '可回复'}；等待任务 {self.queue.qsize()}；今日处理轮数 {cfg['usage'].get('calls', 0)}。"
        self.governance.audit("owner_command", capability)
        await self.channel.action("send_message", {"chat_id": f"private:{self.owner}",
            "segments": [{"type": "text", "text": response}], "idempotency_key": "admin:" + str(payload.get("message_id", uuid.uuid4().hex))})
        return True

    async def _control(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.read(4096), 5)
            request_line = raw.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
            method, path = (request_line.split(" ") + ["", ""])[:2]
            headers = {}
            for line in raw.split(b"\r\n")[1:]:
                if b":" in line:
                    key, value = line.split(b":", 1)
                    headers[key.strip().lower()] = value.strip().decode("latin-1")
            import hmac
            if not hmac.compare_digest(headers.get(b"x-fish-token", ""), self.control_token):
                body, status = b'{"ok":false,"error":"unauthorized"}', "401 Unauthorized"
            elif path == "/cancel" and method == "POST":
                body = json.dumps(await self.cancel(), ensure_ascii=False).encode()
                status = "200 OK"
            elif path == "/status":
                body = json.dumps({
                    "conversation": self.conversation["id"] if self.conversation else None,
                    "session_id": self.session_id,
                    "active_turn": self.active_turn,
                    "owner_mode": self.owner_mode().get(),
                    "queue": self.queue.qsize(),
                    "group_tasks": sum(not task.done() for task in self.group_tasks),
                    "source_mode": not getattr(sys, 'frozen', False),
                    "language_layer_version": 1,
                    "group_reply_style_version": 2,
                    "group_pipeline_version": 5,
                    "group_focus_version": 3,
                    "owner_emoji_version": 1,
                    "group_history_execution": "fresh-focused-view-v1",
                    "chat_actions_version": 1,
                    "channel_epoch": self.channel.epoch,
                    "channel_seq": self.channel.last_seq,
                    "event_queue": self.channel._events.qsize(),
                    "event_busy": self.channel._processing,
                    "runtime_follow_max_bytes": getattr(self.dsh, 'follow_max_bytes', None),
                    "group_topic_jobs": len(self.group_tasks),
                    "bot": self.channel.bot,
                    "counts": self.store.counts(),
                }, ensure_ascii=False).encode()
                status = "200 OK"
            elif path == "/shutdown" and method == "POST":
                body = b'{"ok":true}'
                status = "200 OK"
                self._stop.set()
            else:
                body = b'{"ok":false}'
                status = "404 Not Found"
            writer.write(f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


async def main_async() -> int:
    root = Path(os.environ.get("FISH_HARNESS_HOME", str(Path(__file__).resolve().parent.parent)))
    loop = QQAgentLoop(root)
    await loop.start()
    async def watch_desktop():
        parent=int(os.environ.get('FISH_LOOP_PARENT_PID','0'))
        if not parent or os.name!='nt':return
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.OpenProcess(0x00100000,False,parent)
        if not handle:loop._stop.set();return
        try:
            while not loop._stop.is_set():
                if kernel.WaitForSingleObject(handle,0)!=258:loop._stop.set();return
                await asyncio.sleep(2)
        finally:kernel.CloseHandle(handle)
    desktop_watch=asyncio.create_task(watch_desktop())
    try:
        await loop._stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        desktop_watch.cancel()
        await asyncio.gather(desktop_watch,return_exceptions=True)
        await loop.stop()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="QQ private reactive agent loop")
    parser.parse_args()
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
