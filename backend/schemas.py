from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, model_validator


def clean_text(value: str, *, max_length: int, label: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{label}不能为空")
    if len(value) > max_length:
        raise ValueError(f"{label}不能超过{max_length}个字符")
    return value


class ClientPacket(BaseModel):
    type: Literal["chat", "agent.dm", "agent.stop", "agent.followup", "notice.ack"]
    content: str = ""
    threadId: str | None = None
    taskId: str | None = None
    noticeId: str | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> "ClientPacket":
        if self.type == 'notice.ack':
            if not self.noticeId or len(self.noticeId) > 150:
                raise ValueError('缺少有效 noticeId')
            return self
        if self.type in {"agent.stop", "agent.followup"}:
            if not (self.taskId or "").strip():
                raise ValueError("缺少 taskId")
            if self.type == 'agent.stop':
                return self
        self.content = clean_text(self.content, max_length=2000, label="消息")
        return self
