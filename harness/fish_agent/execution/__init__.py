"""The main execution: one consumer, one turn at a time, real outcomes.

``submit`` registers a standard input and its turn in the unified order and returns
the stable execution reference. One consumer thread claims the next turn and runs it
through the same model/tool loop every entry shares. ``status`` answers with the
turn ledger's own row; ``cancel`` cancels the named real work and reports what it
could actually stop. New inputs registered while a turn runs stay for the next turn.

The agent's own clock lives here too: an external trigger (a message, @, the eighth
group line or the 45-minute opportunity) and the model's accepted 3-second follow-up
all become the same kind of wake, so they merge into one thought instead of starting
a second one.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from . import state as st
from .state import AgentState
from .wakes import Wakes

_ACTIVE = ('queued', 'running')
_JOB_STATUS = {st.QUEUED: 'queued', st.RUNNING: 'running', st.COMPLETE: 'complete',
               st.SILENT: 'complete', st.FAILED: 'failed', st.CANCELLED: 'cancelled'}

# 45 minutes after a turn ends; the owner confirmed this default.
AUTO_INTERVAL_S = 45 * 60
# The built-in follow-up waits three seconds after the turn ends, at most three times.
FOLLOWUP_DELAY_S = 3.0
FOLLOWUP_LIMIT = 3
FOLLOWUP_MIN_INTEREST = 0.7


class ExecutionModule:
    def __init__(self, runtime):
        self.runtime = runtime
        self.root = Path(runtime.root)
        self.state = runtime.state
        self.wakes = Wakes(runtime)
        self._cv = threading.Condition()
        self._stop = threading.Event()
        self._thread = None
        self._cancels = {}
        self._cancels_lock = threading.Lock()
        self._active = ''
        self._followup_lock = threading.Lock()

    # ------------------------------------------------------------ lifecycle
    def start(self):
        """Start the one consumer; the background host owns this lifecycle."""
        with self._cv:
            if self._thread is not None and self._thread.is_alive():
                return False
            if not self.state.get_state('auto_deadline'):
                self.state.set_state('auto_deadline', str(time.time() + AUTO_INTERVAL_S))
            self._stop.clear()
            self._thread = threading.Thread(target=self._consume, name='fish-main-execution', daemon=True)
            self._thread.start()
        return True

    def stop(self, timeout=10):
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        self._thread = None

    # -------------------------------------------------------- public Interface
    def submit(self, *, source, conversation='', message='', text='', request='', kind='message',
               place='', reference='', payload=None, reason=''):
        """Register one received event and, when it triggers, its turn.

        Returns the stable execution reference for this submission. Submitting the
        same original record twice returns the same input and never starts a second
        generation for it.
        """
        reference = reference or ('%s:%s' % (source, message))
        event = self.runtime.experience.record(
            reference=reference, kind=kind, source=source, conversation=conversation,
            message=message, place=place or source, text=text, payload=payload)
        if not self.wakes.can_wake(event):
            return {'execution': '', 'input': event['reference'], 'status': 'stored'}
        existing = self._run_for_input(event['reference'])
        if existing:
            return {'execution': existing, 'input': event['reference'], 'status': 'queued'}
        run_id = self.state.create_run(kind=kind, source=source, conversation=conversation,
                                       request=request, input_reference=event['reference'],
                                       place=place or source, reason=reason)
        self.start()
        with self._cv:
            self._cv.notify_all()
        return {'execution': run_id, 'input': event['reference'], 'status': 'queued'}

    def request_followup(self, interest, topic='', call_id=''):
        """Accept one built-in 3-second follow-up; the program checks the threshold.

        The count is the agent's own state, not a row that disappears with the next
        task: it survives until an external trigger starts a new turn. A repeated
        call id is the same call and does not count twice.
        """
        if isinstance(interest, bool) or not isinstance(interest, (int, float)):
            raise ValueError('interest 应为 0 到 1 的数字')
        interest = float(interest)
        if not 0 <= interest <= 1:
            raise ValueError('interest 应为 0 到 1 的数字')
        if interest < FOLLOWUP_MIN_INTEREST:
            raise ValueError('interest 低于 %.1f，未受理这次续醒' % FOLLOWUP_MIN_INTEREST)
        with self._followup_lock:
            call = str(call_id or '')
            if call and call == self.state.get_state('followup_call'):
                return {'accepted': True, 'duplicate': True,
                        'consecutive': int(self.state.get_state('followup_count') or 0),
                        'delay_s': FOLLOWUP_DELAY_S,
                        'note': '同一调用已受理，不重复计数；本轮结束后 3 秒再看一眼'}
            count = int(self.state.get_state('followup_count') or 0)
            if count >= FOLLOWUP_LIMIT:
                raise ValueError('连续自唤醒已达 %d 次；等新的消息、@ 或 45 分钟触发后才会解除' % FOLLOWUP_LIMIT)
            count += 1
            self.state.set_state('followup_call', call)
            self.state.set_state('followup_count', str(count))
            self.state.set_state('followup_pending', '1')
            self.state.set_state('followup_topic', str(topic or '')[:200])
            if self._active:
                self.state.set_state('followup_from', self._active)
            return {'accepted': True, 'duplicate': False, 'consecutive': count,
                    'delay_s': FOLLOWUP_DELAY_S,
                    'note': '已受理：本轮结束后 3 秒再看一眼（连续第 %d 次，最多 %d 次）'
                            % (count, FOLLOWUP_LIMIT)}

    def status(self, run_id):
        return self.state.run(run_id) if run_id else None

    def job_status(self, run_id):
        """One turn as the client's job status: queued/running/complete/failed/cancelled."""
        row = self.state.run(run_id)
        if row is None:
            return {'status': 'failed', 'error': '执行记录不存在，未生成内容', 'execution': str(run_id or '')}
        return {'job_id': row['request'], 'execution': row['id'],
                'status': _JOB_STATUS.get(row['status'], 'failed'),
                'error': row['error'], 'started': row['started'], 'finished': row['finished'],
                'elapsed_ms': row['elapsed_ms'], 'trigger': row.get('kind', ''),
                'reason': row.get('reason', ''), 'place': row.get('place', '')}

    def turn_view(self, run_id):
        """One turn's real record, linked by references instead of a second status.

        The client can follow a submission to what actually happened: its trigger
        (kind/reason/place), the stored provider thought and tool events, the files
        that belonged to its batches, the plan occurrence it served, and the real
        delivery receipt, all by the same execution reference.
        """
        row = self.state.run(run_id)
        if row is None:
            return None
        reference = ''
        try:
            inputs = json.loads(row.get('inputs') or '[]')
        except (TypeError, ValueError):
            inputs = []
        reference = str(inputs[0]) if inputs else ('self:' + str(row['id']))
        view = {'execution': row['id'], 'status': _JOB_STATUS.get(row['status'], 'failed'),
                'raw_status': row['status'], 'error': row['error'], 'error_kind': row['error_kind'],
                'trigger': {'kind': row.get('kind', ''), 'reason': row.get('reason', ''),
                            'place': row.get('place', ''), 'source': row.get('source', ''),
                            'reference': reference},
                'started': row['started'], 'finished': row['finished'], 'elapsed_ms': row['elapsed_ms'],
                'inputs': inputs, 'reference': reference}
        try:
            result = json.loads(row.get('result') or '{}')
        except (TypeError, ValueError):
            result = {}
        view['delivery'] = result.get('delivery') if isinstance(result, dict) else None
        view['silent'] = bool(result.get('silent')) if isinstance(result, dict) else False
        view['merged'] = bool(result.get('merged')) if isinstance(result, dict) else False
        view['trace'] = self._turn_trace(reference)
        view['batches'] = self._turn_batches(row['id'])
        view['delivery_times'] = self._delivery_times(reference)
        view['plan'] = self._plan_occurrence(row)
        return view

    def _turn_trace(self, reference):
        import sqlite3
        from contextlib import closing
        path = self.root / 'data/social/experience.db'
        if not reference or not path.is_file():
            return {'reference': reference, 'phase': '', 'reasoning': '', 'events': []}
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='turn_traces'").fetchone():
                return {'reference': reference, 'phase': '', 'reasoning': '', 'events': []}
            row = db.execute('SELECT phase,reasoning,events,error FROM turn_traces WHERE reference=? '
                             'ORDER BY updated DESC LIMIT 1', (reference,)).fetchone()
        if row is None:
            return {'reference': reference, 'phase': '', 'reasoning': '', 'events': []}
        try:
            events = json.loads(row['events'] or '[]')
        except (TypeError, ValueError):
            events = []
        return {'reference': reference, 'phase': str(row['phase'] or ''),
                'reasoning': str(row['reasoning'] or ''), 'error': str(row['error'] or ''),
                'events': events}

    def _turn_batches(self, run_id):
        from storage import database
        with database(self.state.path) as db:
            rows = [dict(row) for row in db.execute(
                'SELECT id,place,opens,closes,state FROM agent_batches WHERE run=? ORDER BY opens', (str(run_id),))]
            for batch in rows:
                batch['items'] = [dict(item) for item in db.execute(
                    'SELECT seq,reference,resource,received FROM agent_batch_items WHERE batch=? ORDER BY seq',
                    (batch['id'],))]
        return rows

    def _delivery_times(self, reference):
        import sqlite3
        from contextlib import closing
        path = self.root / 'data/social/experience.db'
        if not reference or not path.is_file():
            return None
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='experience_turns'").fetchone():
                return None
            row = db.execute('SELECT arrival,started,first_output,sending,receipt,outcome,error '
                             'FROM experience_turns WHERE reference=? ORDER BY rowid DESC LIMIT 1',
                             (reference,)).fetchone()
        return dict(row) if row else None

    def _plan_occurrence(self, row):
        if str(row.get('source')) != 'plan':
            return None
        payload = self.runtime.plans._payload(row)
        schedule_id, slot = str(payload.get('schedule_id') or ''), str(payload.get('slot') or '')
        if not schedule_id or not slot:
            return None
        try:
            plans = self.runtime.plans.store()
            return {'schedule_id': schedule_id, 'slot': slot,
                    'occurrence': plans.occurrence(schedule_id, slot)}
        except Exception:  # noqa: BLE001 - a plan read miss leaves the turn view readable
            return {'schedule_id': schedule_id, 'slot': slot, 'occurrence': None}

    def cancel(self, conversation=None):
        """Cancel this conversation's queued submissions and the turn it is running.

        An empty conversation is the owner's explicit global stop (QQ /取消): every
        place's queued work and the one running turn are named in the result.
        """
        info = self.state.cancel_conversation(conversation or None)
        stopped = False
        with self._cancels_lock:
            for run_id in info['running']:
                event = self._cancels.get(run_id)
                if event is not None:
                    event.set()
                    stopped = True
        return {'queued_cancelled': info['queued'], 'running': len(info['running']),
                'running_stopped': stopped, 'requests': info['requests'],
                'note': ('已请求取消；未确认服务商是否停止计算' if stopped or info['running']
                         else '没有正在进行的回合；已取消排队的提交')}

    def wait(self, run_id, timeout=None, cancelled=None):
        """Block until the turn settles; used only by the legacy blocking client call."""
        deadline = time.monotonic() + (timeout or 900)
        row = self.state.run(run_id)
        while row is not None and row['status'] in _ACTIVE:
            if cancelled is not None and cancelled.is_set():
                self.cancel(row['conversation'])
            if time.monotonic() > deadline:
                raise TimeoutError('等待本轮执行超时；记录保留，未声称已发送')
            time.sleep(.05)
            row = self.state.run(run_id)
        if row is None:
            raise ValueError('执行记录不存在')
        return row

    def active_conversation(self):
        run_id = self._active
        if not run_id:
            return ''
        row = self.state.run(run_id)
        return str(row['conversation']) if row else ''

    def pending(self):
        return self.state.pending_inputs()

    # -------------------------------------------------------------- consumer
    def _run_for_input(self, reference):
        from storage import database
        with database(self.state.path) as db:
            row = db.execute('SELECT run FROM agent_inputs WHERE reference=?', (reference,)).fetchone()
        return str(row['run'] or '') if row else ''

    def _paused(self):
        try:
            from governance import Governance
            return bool(Governance(self.root, 'qq').config()['paused'])
        except Exception:  # noqa: BLE001 - an unreadable switch never starts paid work
            return True

    def _due_wake(self):
        """A due clock or accepted follow-up becomes the next turn when idle."""
        if self._paused() or self.state.queued_or_running() or not self._thread_is_current():
            return False
        now = time.time()
        due = float(self.state.get_state('followup_due') or 0)
        if due and due <= now:
            place = self._stored_place('followup')
            self.state.set_state('followup_due', '')
            self.state.create_run(kind='followup', source=place['source'], conversation=place['conversation'],
                                  reason=self.state.get_state('followup_topic') or '续醒',
                                  place=place['place'])
            return True
        deadline = float(self.state.get_state('auto_deadline') or 0)
        if deadline and deadline <= now:
            place = self._stored_place('auto')
            self.state.set_state('auto_deadline', '')
            self.state.create_run(kind='timer', source=place['source'], conversation=place['conversation'],
                                  reason='45分钟到了', place=place['place'])
            return True
        return False

    def _thread_is_current(self):
        return threading.current_thread() is self._thread

    def _stored_place(self, kind):
        """Where the clock/follow-up thinks: the requesting turn, else the latest input."""
        if kind == 'followup':
            source = self.state.get_state('followup_source')
            conversation = self.state.get_state('followup_conversation')
            place = self.state.get_state('followup_place')
            if source:
                return {'source': source, 'conversation': conversation, 'place': place or source}
        latest = self.state.latest_place()
        if latest and latest.get('source'):
            return {'source': str(latest['source']), 'conversation': str(latest.get('conversation') or ''),
                    'place': str(latest.get('place') or latest['source'])}
        # Nothing has ever been said: the owner's private chat is still the home place.
        from governance import Governance
        from qq_store import QQStore
        owner = str(Governance(self.root, 'qq').owner_id())
        if not owner:
            return {'source': '', 'conversation': '', 'place': ''}
        conversation = QQStore(self.root).get_or_create_conversation('private:' + owner, 'qq')['id']
        return {'source': 'qq-private', 'conversation': conversation, 'place': 'private:' + owner}

    def _consume(self):
        while not self._stop.is_set():
            try:
                if not self._due_wake():
                    with self._cv:
                        self._cv.wait(0.5)
            except Exception:  # noqa: BLE001 - a broken clock must not stop the consumer
                time.sleep(1)
            try:
                claimed = self.state.claim_next()
            except Exception:
                # A broken claim must not spin: back off, keep the process serving.
                time.sleep(1)
                continue
            if claimed is None:
                continue
            run, inputs = claimed
            try:
                self._claimed(run)
                self._execute(run, inputs)
            except Exception:
                # _execute records its own outcome; a failure of the recorder itself
                # still leaves the run out of the queued/running set.
                self.state.finish_run(run['id'], st.FAILED, error='执行记录写入失败，本轮未完成',
                                      error_kind='RuntimeError')

    def _claimed(self, run):
        """One short bookkeeping step on claim: an external trigger resets the limit."""
        if run['kind'] in ('message', 'timer'):
            self.state.set_state('followup_count', '0')
            self.state.set_state('followup_due', '')

    def _execute(self, run, inputs):
        from chat_output import TurnSilent
        from .runner import execute
        runtime = self.runtime
        cancel = threading.Event()
        with self._cancels_lock:
            self._cancels[run['id']] = cancel
        self._active = run['id']
        self_run = str(run['kind']) in ('timer', 'followup')
        materials = {}
        outcome_ok = False
        try:
            if not inputs and not self_run:
                runtime.experience.record_result(run, st.CANCELLED)
                self._finish(run, st.CANCELLED, error_kind='InterruptedError',
                             error='这次提交附带的输入已取消，没有开始生成')
                return
            if self._stop.is_set():
                self._finish(run, st.CANCELLED, error_kind='InterruptedError',
                             error='后台正在关闭，这次提交没有开始')
                return
            references = [str(row.get('reference') or '') for row in inputs]
            if references and all(runtime.experience.batches.covered([ref]) for ref in references):
                # The files of this input were already handed to the turn that owned
                # their 3-second batch; the batch is handled once, not re-generated.
                runtime.experience.record_result(run, st.SILENT)
                self._finish(run, st.SILENT, result={'merged': True,
                                                     'note': '同批文件已由上一回合统一处理'})
                return
            def prepare():
                materials.update(runtime.experience.prepare(run, inputs, cancel))
                return materials

            source = str(run['source'])
            if self_run:
                origin = 'self-wake:' + str(run['kind']) + ':' + str(run['place'])
            elif source == 'desktop':
                origin = 'desktop-chat:' + str(run['conversation'])
            elif source == 'qq-private':
                origin = 'qq-private-chat:' + str(run['conversation'])
            elif source == 'qq-group':
                payload = json.loads((inputs[-1].get('payload') or '{}'))
                origin = 'group-fish:' + str(payload.get('chat_id') or '')
            elif source == 'plan':
                payload = json.loads((inputs[-1].get('payload') or '{}'))
                origin = 'fish-schedule:' + str(payload.get('schedule_id') or '')
            else:
                raise ValueError('未知输入来源，未生成内容：' + source)
            answer = execute(self.root, 'qq', prepare=prepare, origin=origin,
                             cancelled=cancel, output='bubbles', experience=None)
            previewed = (materials.get('emoji_state') or {}).get('previewed') or ()
            if self_run:
                delivered = runtime.delivery.submit_self(run, answer, previewed=previewed, cancelled=cancel)
            elif source == 'desktop':
                delivered = runtime.delivery.submit_desktop(run, answer,
                                                            message_id=str(inputs[-1]['message']),
                                                            cancelled=cancel)
            elif source == 'plan':
                payload = json.loads((inputs[-1].get('payload') or '{}'))
                delivered = runtime.delivery.submit_plan(
                    run, answer, schedule_id=str(payload.get('schedule_id') or ''),
                    slot=str(payload.get('slot') or ''), target=str(payload.get('target') or ''),
                    purpose=str(payload.get('purpose') or ''), cancelled=cancel)
            elif source == 'qq-group':
                payload = json.loads((inputs[-1].get('payload') or '{}'))
                delivered = runtime.delivery.submit_group(
                    run, answer, chat=str(payload.get('chat_id') or ''),
                    reply_to=str(payload.get('message_id') or ''), message_id=str(inputs[-1]['message']),
                    previewed=previewed,
                    direct=bool(payload.get('mentions_bot') or payload.get('reply_to_bot')),
                    cancelled=cancel)
            else:
                delivered = runtime.delivery.submit_qq(run, answer, message_id=str(inputs[-1]['message']),
                                                       previewed=previewed, cancelled=cancel)
            runtime.experience.record_result(run, st.COMPLETE)
            outcome_ok = True
            self._finish(run, st.COMPLETE, result={'delivery': delivered}, delivered=delivered)
        except TurnSilent as exc:
            try:
                runtime.experience.record_result(run, st.SILENT)
            except Exception:
                pass
            # A deliberate silence is a completed turn that already read its material:
            # its files belong to it and must not be re-read in a later unrelated turn.
            outcome_ok = True
            self._finish(run, st.SILENT, result={'silent': True, 'note': str(exc)})
        except InterruptedError as exc:
            error = str(exc) or '本轮已取消'
            try:
                runtime.experience.record_result(run, st.CANCELLED, error)
            except Exception:
                pass
            self._finish(run, st.CANCELLED, error=error, error_kind='InterruptedError')
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            try:
                runtime.experience.record_result(run, st.FAILED, error)
            except Exception:
                pass
            self._finish(run, st.FAILED, error=error, error_kind=type(exc).__name__)
        finally:
            try:
                self.runtime.experience.batches.finish(materials.get('batch_ids') or [], ok=outcome_ok)
            except Exception:  # noqa: BLE001 - a batch bookkeeping miss cannot undo the turn
                pass
            self._finish_clock(run)
            self._active = ''
            with self._cancels_lock:
                self._cancels.pop(run['id'], None)

    def _finish(self, run, status, *, error='', error_kind='', result=None, delivered=None):
        """Write the turn's real outcome, then let its own place record it.

        A plan occurrence is closed from the delivery receipts and this same status,
        so the task page reads one result instead of a page-local guess.
        """
        self.state.finish_run(run['id'], status, error=error, error_kind=error_kind, result=result)
        if str(run.get('source')) == 'plan':
            try:
                self.runtime.plans.record_result(run, status, error=error, delivered=delivered or {})
            except Exception as exc:  # noqa: BLE001 - the turn's own record stays readable
                try:
                    from governance import Governance
                    Governance(self.root, 'qq').audit('plan_result_failed', type(exc).__name__ + ': ' + str(exc)[:200])
                except Exception:
                    pass

    def _finish_clock(self, run):
        """A turn ending restarts the 45-minute clock and arms an accepted follow-up."""
        with self._followup_lock:
            if (self.state.get_state('followup_pending') == '1'
                    and self.state.get_state('followup_from') == run['id']):
                self.state.set_state('followup_due', str(time.time() + FOLLOWUP_DELAY_S))
                self.state.set_state('followup_source', str(run['source']))
                self.state.set_state('followup_conversation', str(run['conversation']))
                self.state.set_state('followup_place', str(run.get('place') or ''))
            self.state.set_state('followup_pending', '0')
            self.state.set_state('followup_from', '')
        self.state.set_state('auto_deadline', str(time.time() + AUTO_INTERVAL_S))
        with self._cv:
            self._cv.notify_all()


__all__ = ['ExecutionModule', 'AgentState']
