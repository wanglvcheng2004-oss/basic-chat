from __future__ import annotations

import asyncio
import time
import json
from collections import deque

from .config import AgentConfig
from .dsh import DshBackend
from .filters import TriggerBuilder
from .llm import BaseLLM, build_llm
from .registry import WorkerRegistry
from .router import Router
from .store import AgentStore
from .types import AGENT_USER_ID, ROOM_THREAD, Message, Task, Trigger, dm_thread, new_id, now_iso
from .worker import Worker
from .dispatcher import Dispatcher
from .watchdog import Watchdog
from ..database import encode


class AgentRuntime:
    """Super-agent 的进程内运行时：Router + Worker 池 + 订阅表。"""

    def __init__(
        self,
        config: AgentConfig,
        manager,
        *,
        store: AgentStore | None = None,
        llm: BaseLLM | None = None,
    ) -> None:
        self.config = config
        self.manager = manager
        self.store = store or AgentStore()
        self.llm = llm or build_llm(config)
        self.dsh = DshBackend(config)
        self.filter = TriggerBuilder(config.name)
        self.registry = WorkerRegistry()
        self.queue: asyncio.Queue[Trigger] = asyncio.Queue(maxsize=100)
        self.router = Router(self)

        self._router_task: asyncio.Task | None = None
        self._workers: dict[str, asyncio.Task] = {}
        self._sessions: dict[str, Worker] = {}
        self._semaphore = asyncio.Semaphore(config.max_workers)
        self._rate: dict[str, deque[float]] = {}
        self._started = False
        self._stopping = False
        self.dispatcher = Dispatcher(config.max_workers)
        self.watchdog = Watchdog(self)
        self.automation = None
        self._ticker = None
        self.instance_id = new_id('runtime')
        self._last_tick = 0.0
        self._last_lease = 0.0

    # ---------- 生命周期 ----------
    async def start(self) -> None:
        if self._started:
            return
        await self.store.load()
        self._started = True
        self._stopping = False
        self._router_task = asyncio.create_task(self.router.run(), name="agent-router")
        for event_id, event in list(self.store.events.items()):
            if event['state'] == 'queued':
                trigger = event['trigger']
                self.store.finish_event(event_id, 'interrupted')
                await self.publish_notice(trigger['owner_id'], '请求在服务重启时中断，请重新发起。', 'interrupted:' + event_id)
        await self.store.flush()
        self._ticker = asyncio.create_task(self._tick_loop(), name='agent-maintenance')

    async def stop(self) -> None:
        self._started = False
        self._stopping = True
        if self._ticker is not None:
            self._ticker.cancel()
            await asyncio.gather(self._ticker, return_exceptions=True)
            self._ticker = None
        if self._router_task is not None:
            self._router_task.cancel()
            await asyncio.gather(self._router_task, return_exceptions=True)
            self._router_task = None
        jobs = list(self._workers.values())
        for job in jobs:
            job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        await self.llm.aclose()
        await self.store.flush()

    async def _tick_loop(self):
        while True:
            await asyncio.sleep(1)
            now = time.time()
            if self.store.db is not None and now - self._last_lease >= 10:
                if not await self.store.db.lease('runtime', self.instance_id):
                    raise RuntimeError('运行实例租约失效，后台调度已停止。')
                self._last_lease = now
            if now - self._last_tick >= self.config.heartbeat_interval:
                self._last_tick = now
                if self.automation is not None and self.automation.policy.get('watchdog'):
                    await self.watchdog.tick(now)
            if self.automation is not None:
                await self.automation.tick(now)
            await self.store.flush()

    def public_info(self) -> dict:
        return {
            "name": self.config.name,
            "online": self._started,
            "demo": self.llm.demo,
            "allowWrite": self.config.allow_write,
            "maxWorkers": self.config.max_workers,
            "backend": "dsh" if self.harness_allowed() else "builtin",
        }

    # ---------- 入口 ----------
    def record_room_message(
        self,
        *,
        user_id: str,
        nickname: str,
        content: str,
        message_id: str,
        created_at: str | None = None,
    ) -> Message:
        return self.store.add_message(
            thread_id=ROOM_THREAD,
            channel="room",
            role="user",
            nickname=nickname,
            content=content,
            user_id=user_id,
            message_id=message_id,
            created_at=created_at,
        )

    async def on_room_message(self, client, content: str, message_id: str) -> None:
        trigger = self.filter.room(client.user_id, client.nickname, content, message_id)
        if trigger is None:
            return
        if not self._allow(client.user_id):
            await self._notify_user(client, "触发太频繁了，稍等一会儿再试。")
            return
        await self._enqueue(trigger)

    async def on_dm(self, client, content: str) -> None:
        message = self.store.add_message(
            thread_id=dm_thread(client.user_id),
            channel="dm",
            role="user",
            nickname=client.nickname,
            content=content,
            user_id=client.user_id,
            message_id=new_id("msg"),
        )
        await self.store.flush()
        await self.manager.send_to(client.user_id, {"type": "agent.message", **message.as_packet()})

        trigger = self.filter.direct(client.user_id, client.nickname, content)
        if trigger is None:
            return
        if not self._allow(client.user_id):
            await self._notify_user(client, "触发太频繁了，稍等一会儿再试。")
            return
        await self._enqueue(trigger)

    async def on_followup(self, client, task_id: str, content: str) -> None:
        task = self.store.get_task(task_id)
        if task is None or task.owner_id != client.user_id:
            await self._notify_user(client, '只能补充自己发起的任务。')
            return
        if task.kind != 'interactive':
            await self._notify_user(client, '后台分析使用固定输入，请发起新的问题。')
            return
        if not self._allow(client.user_id):
            await self._notify_user(client, '补充太频繁，请稍后再试。')
            return
        message = self.store.add_message(thread_id=task.thread_id, channel=task.channel, role='user',
            nickname=client.nickname, content=content, user_id=client.user_id)
        trigger = Trigger(id=new_id('trg'), thread_id=task.thread_id, channel=task.channel,
                          owner_id=client.user_id, owner_name=client.nickname, content=content,
                          source='followup', message_id=message.id)
        worker = self._sessions.get(task.id)
        self.store.record_event(trigger, task.id)
        await self.store.flush()
        await self._emit(task.channel, task.owner_id, {'type': 'chat' if task.channel == 'room' else 'agent.message', **message.as_packet()})
        if task.status in {'queued', 'running'} and worker is not None and worker.deliver(trigger):
            return
        # The close gate is synchronous: late input becomes a new request.
        await self._enqueue(trigger)

    async def send_history(self, client) -> None:
        messages = await self.store.history(dm_thread(client.user_id), limit=100)
        await self.manager.send_to(
            client.user_id,
            {
                "type": "agent.history",
                "threadId": dm_thread(client.user_id),
                "messages": [
                    {
                        "id": message.id,
                        "role": message.role,
                        "content": message.content,
                        "timestamp": message.created_at,
                    }
                    for message in messages
                ],
            },
        )
        room = await self.store.history(ROOM_THREAD, limit=100)
        await self.manager.send_to(client.user_id, {'type': 'chat.history', 'messages': [m.as_packet() for m in room]})
        visible = [task.public() for task in self.store.tasks.values() if task.channel == 'room' or task.owner_id == client.user_id]
        await self.manager.send_to(client.user_id, {'type': 'agent.tasks', 'tasks': visible[-100:]})
        if self.store.db is not None:
            notices = await self.store.db.all('SELECT n.payload FROM notifications n LEFT JOIN receipts r ON r.notification_id=n.id AND r.member_id=? WHERE r.notification_id IS NULL AND (n.scope=? OR n.owner_id=?) ORDER BY n.created DESC LIMIT 100', (client.user_id, ROOM_THREAD, client.user_id))
            for row in reversed(notices):
                await self.manager.send_to(client.user_id, json.loads(row['payload']))

    async def stop_task(self, task_id: str, client) -> None:
        task = self.store.get_task(task_id)
        if task is None:
            await self._notify_user(client, "找不到这个任务。")
            return
        if task.owner_id != client.user_id:
            await self._notify_user(client, "只能停止自己发起的任务。")
            return
        if task.kind != 'interactive' and self.automation is not None:
            await self.automation.stop_source(task)
            return
        if task.status not in {"queued", "running"}:
            return
        await self.cancel_task(task, 'user_cancelled')

    async def cancel_task(self, task, reason='user_cancelled'):
        if task.status not in {'queued', 'running'}:
            return
        task.reason_code = reason
        job = self._workers.get(task.id)
        if job is not None and not job.done():
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        if task.status in {'queued', 'running'}:
            await self._cancelled(task)
            worker = self._sessions.pop(task.id, None)
            self._workers.pop(task.id, None)
            if worker:
                self.registry.unsubscribe(task.id, worker)
                worker.accepting = False

    async def _cancelled(self, task):
        reason = task.reason_code or ('interrupted' if self._stopping else 'user_cancelled')
        stopped = reason in {'user_cancelled', 'policy_disabled', 'preempted'}
        text = {'timeout': '任务执行超过总时限。', 'interrupted': '服务关闭，任务已中断。', 'policy_disabled': '规则已关闭，后台任务已取消。', 'preempted': '后台任务已让位于用户请求。'}.get(reason, '任务已停止。')
        self.store.update_task(task, status='stopped' if stopped else 'failed', reason_code=reason, error='' if stopped else text, retry_pending=False)
        await self.emit_state(task)
        if reason in {'timeout', 'interrupted'}:
            await self.notice(task, text, 'abnormal:' + reason)
        if task.kind == 'interactive':
            await self._deliver(task, text, f'msg_{task.id}_out')
        if self.automation is not None and task.kind != 'interactive':
            await self.automation.settled(task)
        for event_id, event in self.store.events.items():
            if event.get('task_id') == task.id and event.get('state') == 'queued':
                self.store.finish_event(event_id, 'cancelled', task.id)
        await self.store.flush()

    # ---------- 队列与限流 ----------
    async def _enqueue(self, trigger: Trigger) -> None:
        self.store.record_event(trigger)
        await self.store.flush()
        try:
            self.queue.put_nowait(trigger)
        except asyncio.QueueFull:
            self.store.finish_event(trigger.id, 'rejected')
            await self.notify(trigger, "智能体当前排队的任务太多了，请稍后再试。")

    def _allow(self, user_id: str) -> bool:
        now = time.monotonic()
        bucket = self._rate.setdefault(user_id, deque())
        while bucket and now - bucket[0] > self.config.rate_window:
            bucket.popleft()
        if len(bucket) >= self.config.rate_limit:
            return False
        bucket.append(now)
        return True

    # ---------- 对外输出 ----------
    async def emit_delta(self, trigger: Trigger, message_id: str, text: str) -> None:
        packet = {
            "type": "agent.delta",
            "threadId": trigger.thread_id,
            "messageId": message_id,
            "channel": trigger.channel,
            "taskId": None,
            "content": text,
        }
        await self._emit(trigger.channel, trigger.owner_id, packet)

    async def emit_reply(self, trigger: Trigger, message_id: str, content: str) -> None:
        message = self.store.add_message(
            thread_id=trigger.thread_id,
            channel=trigger.channel,
            role="agent",
            nickname=self.config.name,
            content=content,
            user_id=AGENT_USER_ID,
            message_id=message_id,
        )
        packet = {"type": "agent.done", "taskId": None, **message.as_packet()}
        await self.store.flush()
        await self._emit(trigger.channel, trigger.owner_id, packet)

    async def emit_task_delta(self, worker: Worker, text: str) -> None:
        task = worker.task
        await self._emit(
            task.channel, task.owner_id,
            {
                "type": "agent.delta",
                "threadId": task.thread_id,
                "messageId": worker.message_id,
                "channel": task.channel,
                "taskId": task.id,
                "scope": "task",
                "content": text,
            },
        )

    async def emit_trace(self, task: Task, step: str, detail: str) -> None:
        await self._emit(
            task.channel, task.owner_id,
            {
                "type": "agent.trace",
                "taskId": task.id,
                "step": step,
                "detail": detail,
                "timestamp": now_iso(),
            },
        )

    async def emit_state(self, task: Task) -> None:
        await self.store.flush()
        packet = {"type": "agent.state", **task.public()}
        await self._emit(task.channel, task.owner_id, packet)

    async def publish_notice(self, owner_id, text, notice_id, scope='', task_id=''):
        packet = {'type': 'agent.notice', 'id': notice_id, 'message': text, 'taskId': task_id, 'timestamp': now_iso()}
        if self.store.db is not None:
            count = await self.store.db.execute('INSERT OR IGNORE INTO notifications VALUES(?,?,?,?,?)', (notice_id, scope or dm_thread(owner_id), owner_id, time.time(), encode(packet)))
            if not count:
                return
        await self._emit('room' if scope == ROOM_THREAD else 'dm', owner_id, packet)

    async def notice(self, task, text, key):
        if self.automation is not None and not self.automation.policy.get('watchdog'):
            return
        await self.publish_notice(task.owner_id, text, f'{task.id}:{key}', task_id=task.id)

    def signal(self, task, progress=False):
        now = time.time()
        task.last_signal_at = now
        if progress:
            task.last_progress_at = now

    def harness_allowed(self):
        return self.config.dsh_policy_verified and self.config.allow_write and self.dsh.available()

    async def notify(self, trigger: Trigger, message: str) -> None:
        await self._emit(
            trigger.channel,
            trigger.owner_id,
            {
                "type": "agent.error",
                "threadId": trigger.thread_id,
                "channel": trigger.channel,
                "message": message,
            },
        )

    async def _notify_user(self, client, message: str) -> None:
        await self.manager.send_to(client.user_id, {"type": "agent.error", "message": message})

    async def _emit(self, channel: str, owner_id: str, packet: dict) -> None:
        if channel == "room":
            await self.manager.broadcast(packet)
        else:
            await self.manager.send_to(owner_id, packet)

    # ---------- 任务 ----------
    async def start_task(self, trigger: Trigger, title: str, instruction: str, complexity: str = "light") -> Task:
        if self.automation is not None:
            prepared = await self.automation.context_for(trigger.content)
            if prepared:
                instruction = instruction[:3000] + '\n\n' + prepared[:1000]
        task = self.store.create_task(
            thread_id=trigger.thread_id,
            channel=trigger.channel,
            owner_id=trigger.owner_id,
            title=title,
            instruction=instruction,
            complexity=complexity,
        )
        worker = Worker(self.config, self.llm, self.store, self, task, trigger)
        self.registry.subscribe(task.id, worker)
        job = asyncio.create_task(self._run_worker(worker), name=f"agent-worker:{task.id}")
        self._workers[task.id] = job
        self._sessions[task.id] = worker
        self.store.finish_event(trigger.id, task_id=task.id)
        await self.emit_state(task)
        return task

    async def finish_task(self, task: Task, content: str) -> None:
        worker = self._sessions.get(task.id)
        message_id = worker.message_id if worker is not None else new_id("msg")
        self.store.update_task(task, status="done", result=content, error="")
        if task.kind == 'interactive':
            await self._deliver(task, content, message_id)
        await self.emit_state(task)

    async def fail_task(self, task: Task, error: str) -> None:
        worker = self._sessions.get(task.id)
        message_id = worker.message_id if worker is not None else new_id("msg")
        self.store.update_task(task, status="failed", error=error[:500])
        if task.kind == 'interactive':
            await self._deliver(task, f"任务失败：{error[:300]}", message_id)
        await self.notice(task, f'任务失败：{error[:300]}', 'failed')
        await self.emit_state(task)

    async def _deliver(self, task: Task, content: str, message_id: str) -> None:
        message = self.store.add_message(
            thread_id=task.thread_id,
            channel=task.channel,
            role="agent",
            nickname=self.config.name,
            content=content,
            user_id=AGENT_USER_ID,
            message_id=message_id,
            kind='task_result',
            task_id=task.id,
        )
        await self.store.flush()
        await self._emit(task.channel, task.owner_id, {"type": "agent.done", "taskId": task.id, **message.as_packet()})

    async def _run_worker(self, worker: Worker) -> None:
        task = worker.task
        try:
            async with self.dispatcher.slot(task.id, task.kind != 'interactive'):
                if task.status == "stopped":
                    await self.emit_state(task)
                    return
                timeout = self.config.dsh_timeout if self.harness_allowed() else self.config.task_timeout
                self.store.update_task(task, status="running", deadline_at=time.time() + timeout)
                self.signal(task, progress=True)
                await self.emit_state(task)
                async with asyncio.timeout(timeout):
                    await worker.run()
        except TimeoutError:
            task.reason_code = 'timeout'
            await self._cancelled(task)
        except asyncio.CancelledError:
            await self._cancelled(task)
        except Exception as exc:
            await self.fail_task(task, str(exc))
        finally:
            worker.accepting = False
            # The step budget must not discard already accepted supplements.
            if worker.inbox and task.reason_code not in {'user_cancelled', 'policy_disabled', 'interrupted'}:
                for pending in worker.take_pending():
                    await self._enqueue(pending)
            self.registry.unsubscribe(task.id, worker)
            self._workers.pop(task.id, None)
            self._sessions.pop(task.id, None)
