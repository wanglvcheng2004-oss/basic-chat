from __future__ import annotations

from collections import deque
from dataclasses import asdict, fields
import asyncio
import json

from ..database import encode

from .types import Message, Task, new_id, now_iso


class AgentStore:
    """Bounded message cache backed by serialized SQLite writes."""

    def __init__(self, max_messages: int = 2000, max_thread_messages: int = 400, db=None) -> None:
        self.db = db
        self._pending = []
        self._flush_lock = asyncio.Lock()
        self.events = {}
        self._messages: deque[Message] = deque(maxlen=max_messages)
        self._by_id: dict[str, Message] = {}
        self._threads: dict[str, deque[str]] = {}
        self._max_thread_messages = max_thread_messages
        self.tasks: dict[str, Task] = {}

    def add_message(
        self,
        *,
        thread_id: str,
        channel: str,
        role: str,
        nickname: str,
        content: str,
        user_id: str = "",
        message_id: str | None = None,
        created_at: str | None = None,
        kind: str = 'chat',
        task_id: str = '',
    ) -> Message:
        message = Message(
            id=message_id or new_id("msg"),
            thread_id=thread_id,
            channel=channel,
            role=role,
            nickname=nickname,
            content=content,
            created_at=created_at or now_iso(),
            user_id=user_id,
            kind=kind,
            task_id=task_id,
        )
        if len(self._messages) == self._messages.maxlen:
            self._by_id.pop(self._messages[0].id, None)
        self._messages.append(message)
        self._by_id[message.id] = message
        thread = self._threads.setdefault(thread_id, deque(maxlen=self._max_thread_messages))
        thread.append(message.id)
        self._pending.append(('INSERT OR IGNORE INTO messages VALUES(?,?,?,?)', (message.id, thread_id, message.created_at, encode(asdict(message)))))
        return message

    def get_message(self, message_id: str) -> Message | None:
        return self._by_id.get(message_id)

    def thread_messages(self, thread_id: str, limit: int = 20) -> list[Message]:
        ids = self._threads.get(thread_id)
        if not ids:
            return []
        selected = list(ids)[-max(1, limit):]
        return [self._by_id[item] for item in selected if item in self._by_id]

    def create_task(
        self,
        *,
        thread_id: str,
        channel: str,
        owner_id: str,
        title: str,
        instruction: str,
        complexity: str = "light",
        kind: str = 'interactive',
        source_id: str = '',
    ) -> Task:
        task = Task(
            id=new_id("task"),
            thread_id=thread_id,
            channel=channel,
            owner_id=owner_id,
            title=title[:80],
            instruction=instruction[:4000],
            complexity="heavy" if complexity == "heavy" else "light",
            kind=kind,
            source_id=source_id,
        )
        self.tasks[task.id] = task
        self._save_task(task)
        return task

    def get_task(self, task_id: str) -> Task | None:
        return self.tasks.get(task_id)

    def update_task(self, task: Task, **fields) -> Task:
        for key, value in fields.items():
            if hasattr(task, key):
                setattr(task, key, value)
        task.updated_at = now_iso()
        self._save_task(task)
        return task

    def _save_task(self, task):
        self._pending.append(('INSERT OR REPLACE INTO tasks VALUES(?,?,?,?,?,?,?)',
                              (task.id, task.thread_id, task.owner_id, task.status, task.kind, task.updated_at, encode(asdict(task)))))

    def record_event(self, trigger, task_id=''):
        if trigger.id not in self.events:
            self.events[trigger.id] = {'trigger': asdict(trigger), 'state': 'queued', 'task_id': task_id}
            self._pending.append(('INSERT OR IGNORE INTO events VALUES(?,?,?,?,?)',
                                  (trigger.id, trigger.owner_id, task_id, 'queued', encode(asdict(trigger)))))

    def finish_event(self, event_id, state='processed', task_id=''):
        if event_id in self.events:
            self.events[event_id]['state'] = state
            if task_id:
                self.events[event_id]['task_id'] = task_id
        self._pending.append(('UPDATE events SET state=?,task_id=CASE WHEN ?=\'\' THEN task_id ELSE ? END WHERE id=?', (state, task_id, task_id, event_id)))

    async def flush(self, extra=()):
        async with self._flush_lock:
            batch, self._pending = self._pending, []
            if self.db is not None and (batch or extra):
                try:
                    await self.db.batch(batch + list(extra))
                except BaseException:
                    self._pending = batch + self._pending
                    raise

    async def load(self):
        if self.db is None:
            return
        rows = await self.db.all('SELECT payload FROM messages ORDER BY created_at DESC LIMIT 2000')
        for row in reversed(rows):
            data = json.loads(row['payload'])
            self.add_message(**{k: data[k] for k in ('thread_id','channel','role','nickname','content','user_id','created_at','kind','task_id') if k in data}, message_id=data['id'])
        self._pending.clear()
        keys = {field.name for field in fields(Task)}
        for row in await self.db.all('SELECT payload FROM tasks'):
            task = Task(**{k: v for k, v in json.loads(row['payload']).items() if k in keys})
            self.tasks[task.id] = task
            if task.status in {'running', 'queued'}:
                self.update_task(task, status='failed', reason_code='interrupted', error='服务已重启，原执行已中断。')
        for row in await self.db.all("SELECT * FROM events WHERE state='queued'"):
            self.events[row['id']] = {'trigger': json.loads(row['payload']), 'state': 'queued', 'task_id': row['task_id']}
        await self.flush()

    async def history(self, thread_id, limit=100):
        if self.db is None:
            return self.thread_messages(thread_id, limit)
        await self.flush()
        rows = await self.db.all('SELECT payload FROM messages WHERE thread_id=? ORDER BY created_at DESC LIMIT ?', (thread_id, max(1, min(limit, 200))))
        return [Message(**json.loads(row['payload'])) for row in reversed(rows)]

    async def lookup_message(self, message_id):
        message = self.get_message(message_id)
        if message is not None or self.db is None:
            return message
        row = await self.db.one('SELECT payload FROM messages WHERE id=?', (message_id,))
        return Message(**json.loads(row['payload'])) if row else None
