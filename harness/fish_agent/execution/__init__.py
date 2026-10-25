"""The main execution: one consumer, one turn at a time, real outcomes.

``submit`` registers a standard input and its turn in the unified order and returns
the stable execution reference. One consumer thread claims the next turn and runs it
through the same model/tool loop every entry shares. ``status`` answers with the
turn ledger's own row; ``cancel`` cancels the named real work and reports what it
could actually stop. New inputs registered while a turn runs stay for the next turn.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from . import state as st
from .state import AgentState
from .wakes import Wakes

_ACTIVE = ('queued', 'running')
_JOB_STATUS = {st.QUEUED: 'queued', st.RUNNING: 'running', st.COMPLETE: 'complete',
               st.SILENT: 'complete', st.FAILED: 'failed', st.CANCELLED: 'cancelled'}


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

    # ------------------------------------------------------------ lifecycle
    def start(self):
        """Start the one consumer; the background host owns this lifecycle."""
        with self._cv:
            if self._thread is not None and self._thread.is_alive():
                return False
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
               place='', reference='', payload=None):
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
                                       request=request, input_reference=event['reference'])
        self.start()
        with self._cv:
            self._cv.notify_all()
        return {'execution': run_id, 'input': event['reference'], 'status': 'queued'}

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
                'elapsed_ms': row['elapsed_ms']}

    def cancel(self, conversation):
        """Cancel this conversation's queued submissions and the turn it is running."""
        info = self.state.cancel_conversation(conversation)
        stopped = False
        with self._cancels_lock:
            for run_id in info['running']:
                event = self._cancels.get(run_id)
                if event is not None:
                    event.set()
                    stopped = True
        return {'queued_cancelled': info['queued'], 'running': len(info['running']),
                'running_stopped': stopped, 'note': ('已请求取消；未确认服务商是否停止计算' if stopped or info['running']
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

    def _consume(self):
        while not self._stop.is_set():
            try:
                claimed = self.state.claim_next()
            except Exception:
                # A broken claim must not spin: back off, keep the process serving.
                time.sleep(1)
                continue
            if claimed is None:
                with self._cv:
                    self._cv.wait(0.5)
                continue
            run, inputs = claimed
            try:
                self._execute(run, inputs)
            except Exception:
                # _execute records its own outcome; a failure of the recorder itself
                # still leaves the run out of the queued/running set.
                self.state.finish_run(run['id'], st.FAILED, error='执行记录写入失败，本轮未完成',
                                      error_kind='RuntimeError')

    def _execute(self, run, inputs):
        from chat_output import TurnSilent
        from .runner import execute
        runtime = self.runtime
        cancel = threading.Event()
        with self._cancels_lock:
            self._cancels[run['id']] = cancel
        self._active = run['id']
        try:
            if not inputs:
                runtime.experience.record_result(run, st.CANCELLED)
                self.state.finish_run(run['id'], st.CANCELLED, error_kind='InterruptedError',
                                      error='这次提交附带的输入已取消，没有开始生成')
                return
            if self._stop.is_set():
                self.state.finish_run(run['id'], st.CANCELLED, error_kind='InterruptedError',
                                      error='后台正在关闭，这次提交没有开始')
                return
            answer = execute(
                self.root, 'qq',
                prepare=lambda: runtime.experience.prepare(run, inputs, cancel),
                origin='desktop-chat:' + str(run['conversation']), cancelled=cancel,
                output='bubbles', experience=None)
            delivered = runtime.delivery.submit_desktop(run, inputs, answer, cancelled=cancel)
            runtime.experience.record_result(run, st.COMPLETE)
            self.state.finish_run(run['id'], st.COMPLETE, result={'delivery': delivered})
        except TurnSilent as exc:
            try:
                runtime.experience.record_result(run, st.SILENT)
            except Exception:
                pass
            self.state.finish_run(run['id'], st.SILENT, result={'silent': True, 'note': str(exc)})
        except InterruptedError as exc:
            error = str(exc) or '本轮已取消'
            try:
                runtime.experience.record_result(run, st.CANCELLED, error)
            except Exception:
                pass
            self.state.finish_run(run['id'], st.CANCELLED, error=error, error_kind='InterruptedError')
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            try:
                runtime.experience.record_result(run, st.FAILED, error)
            except Exception:
                pass
            self.state.finish_run(run['id'], st.FAILED, error=error, error_kind=type(exc).__name__)
        finally:
            self._active = ''
            with self._cancels_lock:
                self._cancels.pop(run['id'], None)


__all__ = ['ExecutionModule', 'AgentState']
