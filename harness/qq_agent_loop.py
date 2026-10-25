"""QQ intake and delivery for the single Fish Agent; inputs never wait on thought."""

from __future__ import annotations
from locations import project_path

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
from storage import database


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
            checkpoint=project_path(root, 'harness/runtime/qq-channel-cursor.json'),
            on_health=self._channel_health,
        )
        self.dsh: DSHWebClient | None = None
        self.session_id: str | None = None
        self.conversation: dict[str, Any] | None = None
        self._private_watches: set[asyncio.Task] = set()
        self.cancel_event = asyncio.Event()
        self.active_turn: str | None = None
        self.current_text = ""
        self.control_server = None
        self._stop = asyncio.Event()
        self.group_ids: set[str] = set()
        self.group_mode = os.environ.get("FISH_QQ_GROUP_REPLY", "off").lower()
        try:
            cfg = json.loads((project_path(root, "channels", "fish_qq", "config.json")).read_text(encoding="utf-8"))
            self.group_ids = {str(x) for x in (cfg.get("allowlist", {}).get("group_ids", []) or [])}
        except Exception:
            self.governance.fault('qq-channel', ValueError('QQ 通道配置无法读取'), stage='configuration')
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
        tasks = list(self._notice_tasks)
        for task in tasks: task.cancel()
        if tasks: await asyncio.gather(*tasks, return_exceptions=True)
        if self.control_server is not None:
            self.control_server.close()
            await self.control_server.wait_closed()
        watches = list(self._private_watches)
        for task in watches:
            task.cancel()
        if watches:
            await asyncio.gather(*watches, return_exceptions=True)
        await self.channel.stop()
        self.runtime.stop(self.agent_id)

    def _maintenance_seconds(self):
        """The outer wait is what actually paces the autonomous round."""
        from storage import read_json
        settings = dict(read_json(project_path(self.root, 'harness/control.json')))
        settings.update(read_json(project_path(self.root, 'harness/context-settings.json'), {}) or {})
        try:
            value = float(settings.get('maintenance_interval_s', 60))
        except (TypeError, ValueError):
            value = 60.0
        return max(20.0, min(MAINTENANCE_S, value))

    async def _maintenance(self):
        from data_retention import DataRetention
        from group_language import GroupLanguage
        GroupLanguage(self.root)
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(DataRetention(self.root).run)
            except Exception as exc:
                self.governance.audit('data_maintenance_failed',type(exc).__name__)
            # 自主开口的旧节拍已退出：群消息的默认触发放在唯一主执行里，
            # 45 分钟定时与三秒自唤醒由 06-01 接回同一条思考（不再另建生成路径）。
            # 每日记忆整理也不再从这里启动：宿主后台的唯一 curation.tick
            # 与 Fish 工具、客户端按钮共用同一整理程序和同一条执行记录。
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
        item = {"message_id": message_id, "payload": payload, "text": text,
                'space_id': '', 'conversation': conversation}
        # The stored event is all that crosses to the host: commands stay here, an
        # ordinary message is handed to the one main execution and watched for its
        # real outcome (delivery itself happens over the channel adapter).
        try:
            from owner_private import handle
            await handle(self, item)
        except Exception as exc:  # noqa: BLE001 - one intake failure never breaks intake
            log(f"owner private submit failed: {type(exc).__name__}: {exc}")
            from dsh_web import safe_runtime_error
            from owner_private import window_current
            self.store.update_message(message_id, status='blocked' if isinstance(exc, PermissionError) else 'failed',
                                      error=safe_runtime_error(str(exc) or type(exc).__name__))
            await self._failure_notice(item, exc, validate=lambda: window_current(self, item))
            return
        log(f"owner inbound submitted message_id={message_id} text_len={len(text)}")

    async def _on_group(self, payload: dict[str, Any]) -> None:
        from group_policy import GroupPolicy
        chat = str(payload.get('chat_id', ''))
        sender = str((payload.get('sender') or {}).get('id') or '')
        if not sender or sender == str((self.channel.bot or {}).get('id') or ''):
            return
        policy = GroupPolicy(self.root)
        seen = policy.observe(payload)
        if not seen.get('accepted'):
            return
        text = ''.join(str(part.get('text') or '') for part in payload.get('segments') or [] if part.get('type') == 'text')
        message = self._fish_record(chat, payload, text=text, direction='inbound', status='observed',
                                    reply_to=str(payload.get('reply_to') or ''))
        if not message:
            return
        try:
            self._fish_remember(chat, payload, text)
        except Exception as exc:  # noqa: BLE001 - a memory miss never costs a group reply
            self.governance.audit('group_fish_memory_failed', type(exc).__name__ + ': ' + str(exc)[:200])
        # The group's own settings decide whether she may speak here at all. A group
        # with reply switched off still records and learns, but nothing is submitted
        # for a turn; a group that may reply hands the stored event to the one main
        # execution exactly like the private and client entries do.
        try:
            config = policy.config(chat)
        except Exception as exc:  # noqa: BLE001 - an unreadable policy proves nothing
            self.governance.audit('group_fish_config_unreadable', type(exc).__name__ + ' ' + chat)
            return
        if not (config.get('listen') and config.get('reply') and str(config.get('engine')) == 'fish'):
            return
        await self._submit_group(payload, chat, message, text)

    async def _submit_group(self, payload: dict[str, Any], chat: str, message: int, text: str = '') -> None:
        """Hand one stored group message to the host's one main execution."""
        from tool_host import request
        body = {'source': 'qq-group',
                'conversation': self._group_conversation(chat),
                'message': message, 'text': text, 'place': chat,
                'reference': 'qq:' + str(message), 'kind': 'message',
                'payload': {key: payload.get(key) for key in
                            ('chat_id', 'message_id', 'sender', 'segments', 'reply_to', 'text',
                             'mentions_bot', 'reply_to_bot')}}
        try:
            submission = await asyncio.to_thread(request, self.root, '/submit', body,
                                                 role='qq', admin=True, timeout=30)
        except Exception as exc:  # noqa: BLE001 - the ledger keeps the real reason
            self.store.update_message(message, status='failed', error='未能提交给 Fish 主执行：' + str(exc)[:300])
            await self._fault('group-fish:' + chat, exc, stage='binding')
            return
        self.store.update_message(message, status='submitted',
                                  runtime_turn_id=submission.get('execution', ''))
        # A successful hand-off is the real recovery of a binding fault: the host
        # accepted this group's input, so the old permission refusal is no longer the
        # current state. Without this the stale binding record stayed "active" forever.
        await self._recovered('group-fish:' + chat, stage='binding')
        self._watch_turn(submission.get('execution', ''), source='group-fish:' + chat, chat=chat)

    def _group_conversation(self, chat: str) -> str:
        return str(self.store.get_or_create_conversation(chat, 'qq')['id'])

    def watch_turn(self, execution: str, *, source: str, chat: str = '') -> None:
        if not execution:
            return
        task = asyncio.create_task(self._watch_turn(execution, source=source, chat=chat),
                                   name='fish-turn:' + str(execution))
        self._private_watches.add(task)
        task.add_done_callback(self._private_watches.discard)

    async def _watch_turn(self, execution: str, *, source: str, chat: str = '') -> None:
        """Report what actually happened to one submitted turn; never invent a result.

        The turn, its tools and its reply run in the background host. This watcher
        only reads the real ledger, so silence, failure and an unconfirmed send stay
        distinguishable and still reach the owner through the existing fault notices.
        """
        row: dict[str, Any] = {}
        deadline = time.monotonic() + 900
        self.active_turn = execution
        while not self._stop.is_set() and time.monotonic() < deadline:
            try:
                row = await self._turn_row(execution)
            except Exception as exc:  # noqa: BLE001 - a transient read never invents a state
                self.governance.audit('owner_turn_status_unavailable', type(exc).__name__)
                await asyncio.sleep(1)
                continue
            if str(row.get('status') or '') not in ('queued', 'running'):
                break
            await asyncio.sleep(0.5)
        if self.active_turn == execution:
            self.active_turn = None
        status = str(row.get('status') or '')
        result = row.get('result') or ''
        if isinstance(result, str):
            try:
                result = json.loads(result or '{}')
            except ValueError:
                result = {}
        result = result if isinstance(result, dict) else {}
        delivery = result.get('delivery') if isinstance(result.get('delivery'), dict) else {}
        if status == 'complete':
            await self._recovered(source, stage='model')
            state = str(delivery.get('state') or '')
            if state == 'uncertain':
                await self._fault(source, {'code': delivery.get('error') or '投递结果未确认',
                                           'may_have_sent': True}, stage='delivery', uncertain=True)
            elif state in ('failed', 'blocked'):
                await self._fault(source, delivery.get('error') or '投递失败', stage='delivery')
            else:
                await self._recovered(source, stage='delivery')
        elif status == 'silent':
            self._silent(source)
        elif status == 'failed':
            await self._fault(source, row.get('error') or '这一轮没有完成', stage='model')
        elif status == 'cancelled':
            self.governance.audit('turn_cancelled', str(row.get('error') or '') + ' ' + chat)
        else:
            await self._fault(source, '等待本轮结果超时，记录保留，未冒充完成', stage='model')

    def _fish_record(self, chat, payload, *, text, direction, status, remote='', reply_to=''):
        """What came in and what the channel accepted, in the group's own ledger.

        Only a row the group really saw may become history ('sent'), so an outbound
        whose receipt is unknown is stored as pending and never enters the next view.
        Returns the ledger row id, which is the reference a turn is submitted with.
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
            return int(self.store.record_message(conversation['id'], chat, sender, direction, channel_id,
                                                 reply_to or None, segments, text, status))
        except Exception as exc:  # a ledger miss must not undo a reply that already happened
            log('group fish ledger skipped: ' + type(exc).__name__)
            return 0

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

    # ------------------------------------------------------- owner private wiring
    def watch_private(self, execution: str, item: dict[str, Any]) -> None:
        """Follow one submitted turn to its real outcome; the host owns the turn."""
        if not execution:
            return
        task = asyncio.create_task(self._watch_private(str(execution), item),
                                   name='qq-private-turn:' + str(execution))
        self._private_watches.add(task)
        task.add_done_callback(self._private_watches.discard)

    async def _turn_row(self, execution: str) -> dict[str, Any]:
        from tool_host import request
        return await asyncio.to_thread(request, self.root, '/turn/status',
                                       {'execution': execution}, role='qq', admin=True, timeout=20)

    async def _watch_private(self, execution: str, item: dict[str, Any]) -> None:
        """Report what actually happened to one private turn; never invent a result.

        The turn, its tools and its reply run in the background host. This watcher
        only reads the real ledger, so silence, failure and an unconfirmed send stay
        distinguishable and still reach the owner through the existing fault notices.
        """
        row: dict[str, Any] = {}
        deadline = time.monotonic() + 900
        self.active_turn = execution
        while not self._stop.is_set() and time.monotonic() < deadline:
            try:
                row = await self._turn_row(execution)
            except Exception as exc:  # noqa: BLE001 - a transient read never invents a state
                self.governance.audit('owner_turn_status_unavailable', type(exc).__name__)
                await asyncio.sleep(1)
                continue
            if str(row.get('status') or '') not in ('queued', 'running'):
                break
            await asyncio.sleep(0.5)
        if self.active_turn == execution:
            self.active_turn = None
        status = str(row.get('status') or '')
        result = row.get('result') or ''
        if isinstance(result, str):
            try:
                result = json.loads(result or '{}')
            except ValueError:
                result = {}
        result = result if isinstance(result, dict) else {}
        delivery = result.get('delivery') if isinstance(result.get('delivery'), dict) else {}
        if status == 'complete':
            await self._recovered('owner-private', stage='model')
            state = str(delivery.get('state') or '')
            if state == 'uncertain':
                await self._fault('owner-private', {'code': delivery.get('error') or '私聊投递结果未确认',
                                                    'may_have_sent': True}, stage='delivery', uncertain=True)
            elif state in ('failed', 'blocked'):
                await self._fault('owner-private', delivery.get('error') or '私聊投递失败', stage='delivery')
            else:
                await self._recovered('owner-private', stage='delivery')
        elif status == 'silent':
            self._silent('owner-private')
        elif status == 'failed':
            await self._fault('owner-private', row.get('error') or '这一轮没有完成', stage='model')
        elif status == 'cancelled':
            self.governance.audit('owner_turn_cancelled', str(row.get('error') or ''))
        else:
            await self._fault('owner-private', '等待本轮结果超时，记录保留，未冒充完成', stage='model')

    async def group_deliver(self, data: dict[str, Any]) -> dict[str, Any]:
        """Send decided bubbles into one group; the group's own settings own the gate.

        This is the transport end of the delivery module for groups. The group policy
        still decides whether this chat may receive a reply at all (listen/reply/
        engine/dive/quiet/hourly limit); the turn itself was already made.
        """
        from chat_output import delivery_plan
        from chat_output import segments as bubble_segments
        from group_policy import GroupPolicy
        chat = str(data.get('target') or '')
        bubbles = data.get('bubbles') or []
        if not chat.startswith('group:') or not isinstance(bubbles, list) or not bubbles:
            return {'ok': False, 'error': '群投递目标或气泡无效'}
        policy = GroupPolicy(self.root, audit=self.governance.audit)
        direct = bool(data.get('direct'))
        blocked = self._group_blocked(policy, chat, direct=direct)
        if blocked:
            return {'ok': True, 'sent': False, 'state': 'blocked', 'error': blocked, 'receipt': {}}
        plan = delivery_plan(bubbles)
        parts = [segment for bubble in bubbles for segment in bubble_segments(bubble)]
        text = ''.join(str(part.get('text') or '') for part in parts if part.get('type') == 'text')
        key = str(data.get('key') or ('group-fish:' + uuid.uuid4().hex))
        reply_to = str(data.get('reply_to') or '')
        reference = str(data.get('reference') or '')
        from experience import delivery
        if reference:
            delivery(self.root, reference, sending=True)
        delivered = await self._deliver_reply(
            chat, parts, key, validate=lambda: self._group_blocked(policy, chat, direct=direct) is None,
            validation_error='群设置已改变，未继续发送', reply_to=reply_to, bubble_plan=plan)
        sent = bool(delivered.get('ok'))
        error = delivered.get('error') or {}
        interrupted = bool(error.get('validation_failed') or error.get('cancelled'))
        state = ('sent' if sent else 'uncertain' if error.get('may_have_sent')
                 else 'cancelled' if interrupted else 'blocked' if error.get('blocked') else 'failed')
        if reference:
            delivery(self.root, reference, outcome=state, error=str(error.get('code') or ''))
        self._fish_record(chat, {'message_id': reply_to}, text=text, direction='outbound',
                          status=state, remote=str((delivered.get('payload') or {}).get('message_id') or ''),
                          reply_to=reply_to)
        if not sent:
            await self._fault('group-fish:' + chat,
                              {'code': error.get('code') or '群投递失败',
                               'may_have_sent': bool(error.get('may_have_sent'))},
                              stage='delivery', uncertain=state == 'uncertain')
        return {'ok': True, 'sent': sent, 'state': state, 'error': str(error.get('code') or ''),
                'receipt': {'message_id': str((delivered.get('payload') or {}).get('message_id') or ''),
                            'message_ids': (delivered.get('payload') or {}).get('message_ids') or []}}

    def _group_blocked(self, policy, chat: str, *, direct: bool = False):
        """The real reason this group cannot receive a reply now, or None."""
        try:
            if self.governance.config()['paused']:
                return '主人已暂停回复'
            config = policy.config(chat)
        except Exception as exc:  # noqa: BLE001 - an unreadable policy refuses, never guesses
            return '群设置无法读取：' + type(exc).__name__
        if not config.get('listen'):
            return '这个群没有开启接收'
        if not config.get('reply'):
            return '这个群没有开启 Fish 回复'
        if str(config.get('engine')) != 'fish':
            return '这个群当前不是 Fish 回复引擎'
        if float(config.get('dive_until') or 0) > time.time():
            return '潜水期间不发送'
        try:
            if policy._quiet({**config}, time.time()):
                return '免打扰时段不发送'
        except Exception:  # noqa: BLE001 - a quiet-hours miss must not block a reply
            pass
        if not direct and config.get('reply_limits'):
            limit = int(config.get('max_active_hour') or 0)
            if limit > 0 and self._active_last_hour(chat) >= limit:
                return '这个群每小时的主动消息已达到上限'
        return None

    def _active_last_hour(self, chat: str) -> int:
        since = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time() - 3600))
        try:
            with database(self.store.db_path) as db:
                return int(db.execute(
                    "SELECT COUNT(*) FROM messages WHERE chat_id=? AND direction='outbound' "
                    "AND status IN ('sent','done') AND ts>=?", (chat, since)).fetchone()[0])
        except Exception:  # noqa: BLE001 - a count that cannot be read is not proof of a limit
            return 0

    async def private_deliver(self, data: dict[str, Any]) -> dict[str, Any]:
        """Send what the host's turn decided to say; the channel owns the receipt.

        This is the transport end of the delivery module: it accepts only the
        verified owner's private chat and reports exactly what the websocket said.
        """
        target = str(data.get('target') or '')
        bubbles = data.get('bubbles') or []
        if not self.owner or target != 'private:' + self.owner:
            return {'ok': False, 'error': '私聊投递目标不是已核验的主人私聊'}
        if not isinstance(bubbles, list) or not bubbles:
            return {'ok': False, 'error': '没有要投递的气泡'}
        from owner_private import reply
        self.private_cancel = threading.Event()
        self.cancel_event = self.private_cancel
        item = {'conversation': self.conversation, 'payload': {'chat_id': target},
                'message_id': int(data.get('message_id') or 0), 'text': '', 'space_id': ''}
        try:
            result = await reply(self, item, bubbles, str(data.get('key') or ''),
                                 turn=str(data.get('turn') or ''))
        except InterruptedError as exc:
            return {'ok': True, 'sent': False, 'state': 'cancelled', 'error': str(exc)[:300], 'receipt': {}}
        except Exception as exc:  # noqa: BLE001 - the caller keeps the real reason
            return {'ok': True, 'sent': False,
                    'state': 'failed' if isinstance(exc, (PermissionError, ValueError)) else 'uncertain',
                    'error': str(exc)[:300], 'receipt': {}}
        return {'ok': True, 'sent': bool(result.get('ok')), 'state': str(result.get('state') or ''),
                'error': str(result.get('error') or ''), 'receipt': result.get('receipt') or {}}

    async def cancel(self) -> dict[str, Any]:
        self.cancel_event.set()
        self.private_cancel.set()
        info: dict[str, Any] = {}
        if self.conversation:
            try:
                from tool_host import request
                # The owner's explicit stop covers every place, not only the
                # conversation the command arrived on.
                info = await asyncio.to_thread(request, self.root, '/turn/cancel',
                                               {'conversation': ''},
                                               role='qq', admin=True, timeout=20)
            except Exception as exc:  # noqa: BLE001 - report the real failure below
                self.governance.audit('owner_turn_cancel_unavailable', type(exc).__name__)
        if self.active_turn and self.active_turn.startswith("fish-private-"):
            return {"ok": True, "turn": self.active_turn, "note": "本地已取消等待；不保证服务商立即停止计算"}
        if info:
            return {"ok": True, "turn": self.active_turn or '', **info}
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
                await self.cancel()
            response = "已暂停回复。发 /继续 恢复。" if capability == "pause" else "已恢复回复。"
        elif capability == "cancel":
            await self.cancel()
            response = "已取消当前任务。"
        else:
            cfg = self.governance.snapshot()
            permissions = "、".join(self.governance.owner_capabilities().values()) or "无"
            response = f"主人权限已核验。当前 Fish 管理权限：{permissions}。{'已暂停' if cfg['config']['paused'] else '可回复'}；等待回合 {len(self._private_watches)}；今日处理轮数 {cfg['usage'].get('calls', 0)}。"
        self.governance.audit("owner_command", capability)
        await self.channel.action("send_message", {"chat_id": f"private:{self.owner}",
            "segments": [{"type": "text", "text": response}], "idempotency_key": "admin:" + str(payload.get("message_id", uuid.uuid4().hex))})
        return True

    async def _control(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = b''
            while b"\r\n\r\n" not in raw and len(raw) < 262144:
                chunk = await asyncio.wait_for(reader.read(65536), 5)
                if not chunk:
                    break
                raw += chunk
            head, _, body = raw.partition(b"\r\n\r\n")
            request_line = head.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
            method, path = (request_line.split(" ") + ["", ""])[:2]
            headers = {}
            for line in head.split(b"\r\n")[1:]:
                if b":" in line:
                    key, value = line.split(b":", 1)
                    headers[key.strip().lower()] = value.strip().decode("latin-1")
            length = int(headers.get("content-length", "0") or 0)
            while len(body) < length:
                chunk = await asyncio.wait_for(reader.read(65536), 5)
                if not chunk:
                    break
                body += chunk
            import hmac
            if not hmac.compare_digest(headers.get(b"x-fish-token", ""), self.control_token):
                body, status = b'{"ok":false,"error":"unauthorized"}', "401 Unauthorized"
            elif path == "/cancel" and method == "POST":
                body = json.dumps(await self.cancel(), ensure_ascii=False).encode()
                status = "200 OK"
            elif path == "/deliver" and method == "POST":
                try:
                    payload = json.loads(body or b'{}')
                except ValueError:
                    payload = None
                if not isinstance(payload, dict):
                    body, status = b'{"ok":false,"error":"deliver body is not JSON"}', "400 Bad Request"
                else:
                    if str(payload.get('target') or '').startswith('group:'):
                        result = await self.group_deliver(payload)
                    else:
                        result = await self.private_deliver(payload)
                    body = json.dumps(result, ensure_ascii=False).encode()
                    status = "200 OK"
            elif path == "/status":
                body = json.dumps({
                    "conversation": self.conversation["id"] if self.conversation else None,
                    "session_id": self.session_id,
                    "active_turn": self.active_turn,
                    "owner_mode": self.owner_mode().get(),
                    "private_turns": len(self._private_watches),
                    "group_tasks": 0,
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
                    "group_topic_jobs": 0,
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
