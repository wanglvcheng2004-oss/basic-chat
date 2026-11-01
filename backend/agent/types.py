from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

ROOM_THREAD = "room:main"
AGENT_USER_ID = "agent"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def dm_thread(user_id: str) -> str:
    return f"dm:{user_id}"


@dataclass(slots=True)
class Trigger:
    id: str
    thread_id: str
    channel: str
    owner_id: str
    owner_name: str
    content: str
    message_id: str = ""
    source: str = "chat"
    created_at: str = field(default_factory=now_iso)


@dataclass(slots=True)
class Message:
    id: str
    thread_id: str
    channel: str
    role: str
    nickname: str
    content: str
    created_at: str
    user_id: str = ""
    kind: str = 'chat'
    task_id: str = ''

    def as_packet(self) -> dict:
        return {
            "id": self.id,
            "messageId": self.id,
            "threadId": self.thread_id,
            "channel": self.channel,
            "role": self.role,
            "nickname": self.nickname,
            "userId": self.user_id,
            "content": self.content,
            "timestamp": self.created_at,
            "isAgent": self.role == "agent",
            'kind': self.kind,
            'taskId': self.task_id or None,
        }


@dataclass(slots=True)
class Task:
    id: str
    thread_id: str
    channel: str
    owner_id: str
    title: str
    instruction: str
    status: str = "queued"
    complexity: str = "light"
    dsh_session_id: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    steps: int = 0
    result: str = ""
    error: str = ""
    kind: str = 'interactive'
    source_id: str = ''
    reason_code: str = ''
    deadline_at: float = 0
    last_signal_at: float = 0
    last_progress_at: float = 0
    health: str = 'normal'
    health_revision: int = 0
    retry_pending: bool = False

    def public(self) -> dict:
        return {
            "taskId": self.id,
            "threadId": self.thread_id,
            "channel": self.channel,
            "ownerId": self.owner_id,
            "title": self.title,
            "status": self.status,
            "complexity": self.complexity,
            "dshSessionId": self.dsh_session_id,
            "steps": self.steps,
            "result": self.result,
            "error": self.error,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            'kind': self.kind,
            'reasonCode': self.reason_code,
            'health': self.health,
            'retryPending': self.retry_pending,
        }
