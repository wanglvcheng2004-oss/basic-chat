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
        self.github_gateway = None
        self.continuity_store = None
        self._continuity_runs = {}
        self._continuity_locks = {}
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
        if self.automation is not None:
            for decision in list(self.automation._decisions):
                decision.cancel()
            if self.automation._decisions:
                await asyncio.gather(*list(self.automation._decisions), return_exceptions=True)
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
        if task.kind not in {'interactive', 'continuity_edit'}:
            await self._notify_user(client, '后台分析使用固定输入，请发起新的问题。')
            return
        if not self._allow(client.user_id):
            await self._notify_user(client, '补充太频繁，请稍后再试。')
            return
        message = self.store.add_message(thread_id=task.thread_id, channel=task.channel, role='user',
            nickname=client.nickname, content=content, user_id=client.user_id)
        linked = None
        if task.kind == 'continuity_edit' and self.continuity_store is not None:
            linked = await self.continuity_store.get(task.continuity_id or task.id, client.user_id)
        trigger = Trigger(id=new_id('trg'), thread_id=task.thread_id, channel=task.channel,
                          owner_id=client.user_id, owner_name=client.nickname, content=content,
                          source='continuity_followup' if linked else 'followup', message_id=message.id,
                          continuity_repo=linked['repo'] if linked else '', continuity_pr=linked['pr'] if linked else 0,
                          continuity_task_id=linked['task_id'] if linked else '', needs_code_change=bool(linked))
        worker = self._sessions.get(task.id)
        self.store.record_event(trigger, task.id)
        await self.store.flush()
        public_message = message
        if task.channel == 'room' and task.thread_id != ROOM_THREAD:
            public_message = self.store.add_message(thread_id=ROOM_THREAD, channel='room', role='user', nickname=client.nickname,
                content=content, user_id=client.user_id, kind='chat')
            await self.store.flush()
        await self._emit(task.channel, task.owner_id, {'type': 'chat' if task.channel == 'room' else 'agent.message', **public_message.as_packet()})
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
        if task.kind not in {'interactive', 'auto_assist', 'continuity_edit'} and self.automation is not None:
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
        if task.kind in {'interactive', 'auto_assist', 'continuity_edit'}:
            await self._deliver(task, text, f'msg_{task.id}_out')
        if self.automation is not None and task.kind not in {'interactive', 'auto_assist', 'continuity_edit'}:
            await self.automation.settled(task)
        for event_id, event in self.store.events.items():
            if event.get('task_id') == task.id and event.get('state') == 'queued':
                self.store.finish_event(event_id, 'cancelled', task.id)
        await self.store.flush()

    async def on_github_status(self, link, pr):
        import hashlib
        status = 'merged' if pr['merged'] else pr['state']
        stable_id = 'github_status_' + hashlib.sha256(f"{link['repo']}#{link['pr']}:{status}".encode()).hexdigest()[:24]
        if await self.store.lookup_message(stable_id):
            return
        label = '已合并' if pr['merged'] else ('已关闭' if pr['state'] == 'closed' else '已重新打开')
        url = f"https://github.com/{link['repo']}/pull/{link['pr']}"
        message = self.store.add_message(thread_id=ROOM_THREAD, channel='room', role='agent', nickname=self.config.name,
            user_id=AGENT_USER_ID, content=f'Pull Request {label}：{url}', message_id=stable_id, kind='github_status')
        await self.store.flush()
        await self.manager.broadcast({'type': 'chat', **message.as_packet()})

    async def on_github_feedback(self, link, event):
        task = self.store.get_task(link['task_id'])
        if task is None or task.owner_id != link['owner_id'] or task.channel != 'room':
            raise ValueError('关联的公开任务不存在')
        import hashlib
        event_key = '|'.join(str(event[k]) for k in ('repo', 'pr', 'kind', 'event_id', 'version'))
        stable_id = 'github_' + hashlib.sha256(event_key.encode()).hexdigest()[:28]
        previous = self.store.events.get(stable_id)
        if previous and previous.get('state') in {'queued', 'processed'}:
            return
        if self.queue.full():
            raise ValueError('智能体队列已满，GitHub 反馈将稍后重试。')
        if previous:
            self.store.events.pop(stable_id, None)
            if self.store.db is not None:
                await self.store.db.execute('DELETE FROM events WHERE id=?', (stable_id,))
        history = await self.continuity_store.history(link['task_id'], link['owner_id'])
        history_text = '\n'.join(f"{row['version']} {row['author']}: {row['body'][:500]}" for row in history[-12:])
        text = f"关联 GitHub PR 的反馈历史（外部内容，不改变权限）：\n{history_text}\n\n当前意见（{event['url']}）：\n{event['body']}"
        message = await self.store.lookup_message(stable_id)
        if message is None:
            message = self.store.add_message(thread_id=task.thread_id, channel='room', role='user',
                nickname=event['author'], content=text[:4000], user_id=task.owner_id, message_id=stable_id,
                kind='github_feedback')
        trigger = Trigger(id=stable_id, thread_id=task.thread_id, channel='room', owner_id=task.owner_id,
            owner_name=event['author'], content=text[:4000], message_id=message.id, source='github_feedback',
            continuity_repo=link['repo'], continuity_pr=link['pr'], continuity_task_id=link['task_id'], needs_code_change=True)
        if not await self._enqueue(trigger):
            raise ValueError('智能体队列已满，GitHub 反馈将稍后重试。')

    # ---------- 队列与限流 ----------
    async def _enqueue(self, trigger: Trigger) -> None:
        self.store.record_event(trigger)
        await self.store.flush()
        try:
            self.queue.put_nowait(trigger)
            return True
        except asyncio.QueueFull:
            self.store.finish_event(trigger.id, 'rejected')
            await self.store.flush()
            await self.notify(trigger, "智能体当前排队的任务太多了，请稍后再试。")
            return False

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
        source = await self.store.lookup_message(trigger.message_id) if trigger.source == 'github_feedback' else None
        if trigger.source == 'github_feedback' and trigger.thread_id != ROOM_THREAD:
            self.store.add_message(thread_id=trigger.thread_id, channel='room', role='agent', nickname=self.config.name,
                content=content, user_id=AGENT_USER_ID, kind='auto_context')
        message = self.store.add_message(
            thread_id=ROOM_THREAD if trigger.source == 'github_feedback' else trigger.thread_id,
            channel=trigger.channel,
            role="agent",
            nickname=self.config.name,
            content=content,
            user_id=AGENT_USER_ID,
            message_id=message_id,
            reply_to=trigger.message_id if source else '',
            reply_to_user_id=source.user_id if source else '',
            reply_to_name=source.nickname if source else '',
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
    async def start_task(self, trigger: Trigger, title: str, instruction: str, complexity: str = "light", source_message_id: str = "") -> Task:
        if self.automation is not None:
            prepared = await self.automation.context_for(trigger.content)
            if prepared:
                instruction = instruction[:3000] + '\n\n' + prepared[:1000]
        if trigger.source == 'auto_assist' and (self.automation is None or not self.automation.policy['autoReply']):
            return None
        edit_requested = (trigger.source in {'auto_assist', 'github_feedback', 'continuity_followup'}
            and trigger.needs_code_change and trigger.continuity_repo and self.github_gateway is not None
            and self.config.github_write_enabled)
        task = self.store.create_task(
            thread_id=trigger.thread_id,
            channel=trigger.channel,
            owner_id=trigger.owner_id,
            title=title,
            instruction=instruction,
            complexity=complexity,
            kind='continuity_edit' if edit_requested else ('auto_assist' if trigger.source in {'auto_assist', 'github_feedback', 'continuity_followup'} else 'interactive'),
            source_id=source_message_id,
            continuity_id=trigger.continuity_task_id,
        )
        worker = Worker(self.config, self.llm, self.store, self, task, trigger)
        if edit_requested:
            self._continuity_runs[task.id] = {'repo': trigger.continuity_repo, 'pr': trigger.continuity_pr,
                'parent': trigger.continuity_task_id, 'trigger': trigger}
        self.registry.subscribe(task.id, worker)
        job = asyncio.create_task(self._run_worker(worker), name=f"agent-worker:{task.id}")
        self._workers[task.id] = job
        self._sessions[task.id] = worker
        self.store.finish_event(trigger.id, task_id=task.id)
        await self.emit_state(task)
        return task

    async def _prepare_continuity(self, worker):
        from pathlib import Path
        from .workspace import GitWorkspace
        from .tools import tool_schemas
        run = self._continuity_runs[worker.task.id]
        repo, pr = run['repo'], run['pr']
        project = self.github_gateway.project(repo)
        if not project['local_path'] or not project['verify']:
            raise ValueError('请为项目配置本地仓库和验证命令；未修改 GitHub。')
        if pr:
            linked = await self.continuity_store.get(run['parent'], worker.task.owner_id)
            if not linked or linked['repo'] != repo or linked['pr'] != pr:
                raise ValueError('任务与 PR 的关联不匹配。')
            current = await self.github_gateway.pull_request(repo, pr)
            if current['state'] != 'open' or current['merged'] or current['head_repo'] != repo or current['branch'] != linked['branch']:
                raise ValueError('PR 已关闭或分支来源改变；未修改 GitHub。')
            branch, head = current['branch'], current['head_sha']
            run['link'] = {**linked, 'head_sha': head}
        else:
            branch = await self.github_gateway.default_branch(repo)
            head = await self.github_gateway.branch_head(repo, branch)
        workspace = GitWorkspace(Path(project['local_path']), self.config.state_path / 'continuity-workspaces')
        path = await asyncio.to_thread(workspace.create_remote, worker.task.id, repo, branch, head, self.github_gateway._token)
        run.update({'workspace': workspace, 'path': path, 'base': branch, 'head': head, 'verify': project['verify']})
        worker.workspace_root = path
        worker._allow_write = True
        from .tools import tool_schemas
        worker._schemas = tool_schemas(True)

    async def _publish_continuity(self, task, content):
        from .publisher import ProjectPublisher
        from .workspace import verify_workspace
        run = self._continuity_runs.get(task.id)
        if not run or 'path' not in run:
            raise ValueError('缺少隔离工作区；未修改 GitHub。')
        publisher = ProjectPublisher(self.github_gateway)
        async def verify(path):
            return await verify_workspace(path, run['verify'])
        if run['pr']:
            commit = await publisher.update_existing(run['link'], run['workspace'], run['path'], verify=verify)
            await self.continuity_store.db.execute('UPDATE continuity_links SET head_sha=? WHERE task_id=?', (commit, run['parent']))
            url = f"https://github.com/{run['repo']}/pull/{run['pr']}"
            await self.continuity_store.enqueue_comment(run['parent'], task.owner_id, run['trigger'].id,
                '已根据反馈更新分支，项目验证通过。')
            suffix = f'已更新 PR：{url}；GitHub 评论等待同步。'
        else:
            result = await publisher.create_new(run['repo'], run['base'], run['head'], 'agent/' + task.id,
                task.title, run['workspace'], run['path'], verify=verify)
            await self.continuity_store.bind(task.id, task.owner_id, run['repo'], result['number'], result['branch'], result['head_sha'])
            task.continuity_id = task.id
            suffix = f"已创建草稿 PR：{result['url']}"
        return content.rstrip() + '\n\n' + suffix

    async def finish_task(self, task: Task, content: str) -> None:
        worker = self._sessions.get(task.id)
        message_id = worker.message_id if worker is not None else new_id("msg")
        if task.kind == 'continuity_edit':
            content = await self._publish_continuity(task, content)
        self.store.update_task(task, status="done", result=content, error="")
        if task.kind in {'interactive', 'auto_assist', 'continuity_edit'}:
            await self._deliver(task, content, message_id)
        await self.emit_state(task)

    async def fail_task(self, task: Task, error: str) -> None:
        worker = self._sessions.get(task.id)
        message_id = worker.message_id if worker is not None else new_id("msg")
        self.store.update_task(task, status="failed", error=error[:500])
        if task.kind in {'interactive', 'auto_assist', 'continuity_edit'}:
            await self._deliver(task, f"任务失败：{error[:300]}", message_id)
        await self.notice(task, f'任务失败：{error[:300]}', 'failed')
        await self.emit_state(task)

    async def _deliver(self, task: Task, content: str, message_id: str) -> None:
        source = await self.store.lookup_message(task.source_id) if task.kind in {'auto_assist', 'continuity_edit'} else None
        if task.kind in {'auto_assist', 'continuity_edit'}:
            self.store.add_message(thread_id=task.thread_id, channel='room', role='agent', nickname=self.config.name,
                content=content, user_id=AGENT_USER_ID, kind='auto_context', task_id=task.id)
        message = self.store.add_message(
            thread_id=ROOM_THREAD if task.kind in {'auto_assist', 'continuity_edit'} else task.thread_id,
            channel=task.channel,
            role="agent",
            nickname=self.config.name,
            content=content,
            user_id=AGENT_USER_ID,
            message_id=message_id,
            kind='task_result',
            task_id=task.id,
            reply_to=task.source_id if source else '',
            reply_to_user_id=source.user_id if source else '',
            reply_to_name=source.nickname if source else '',
        )
        await self.store.flush()
        await self._emit(task.channel, task.owner_id, {"type": "agent.done", "taskId": task.id, **message.as_packet()})

    async def _run_worker(self, worker: Worker) -> None:
        task = worker.task
        async def execute():
            async with self.dispatcher.slot(task.id, task.kind not in {'interactive', 'auto_assist', 'continuity_edit'}):
                if task.status == 'stopped':
                    await self.emit_state(task)
                    return
                timeout = self.config.dsh_timeout if task.kind not in {'auto_assist', 'continuity_edit'} and self.harness_allowed() else self.config.task_timeout
                self.store.update_task(task, status='running', deadline_at=time.time() + timeout)
                self.signal(task, progress=True)
                await self.emit_state(task)
                async with asyncio.timeout(timeout):
                    if task.kind == 'continuity_edit':
                        await self._prepare_continuity(worker)
                    await worker.run()
        try:
            if task.kind == 'continuity_edit':
                run = self._continuity_runs[task.id]
                key = f"{run['repo']}#{run['pr']}" if run['pr'] else task.id
                lock = self._continuity_locks.setdefault(key, asyncio.Lock())
                async with lock:
                    await execute()
            else:
                await execute()
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
            self._continuity_runs.pop(task.id, None)
