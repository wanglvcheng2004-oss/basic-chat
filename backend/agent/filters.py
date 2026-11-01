from __future__ import annotations

import re

from .types import Trigger, dm_thread, new_id

ROOM_THREAD = "room:main"


class TriggerBuilder:
    """事件过滤中的程序层（Fast Path）：确定性规则在调用模型前完成筛选。"""

    def __init__(self, name: str) -> None:
        self.name = name

    def room(self, user_id: str, nickname: str, content: str, message_id: str) -> Trigger | None:
        text = content.strip()
        if not text or user_id == "agent":
            return None
        marker = f"@{self.name}"
        match = re.search(r'(?<!\S)' + re.escape(marker) + r'(?=$|\s|[，。！？、,.!?;；：:])', text)
        if match is None:
            return None
        cleaned = (text[:match.start()] + ' ' + text[match.end():]).strip()
        return Trigger(
            id=new_id("trg"),
            thread_id=ROOM_THREAD,
            channel="room",
            owner_id=user_id,
            owner_name=nickname,
            content=cleaned or "你好",
            message_id=message_id,
            source="chat",
        )

    def direct(self, user_id: str, nickname: str, content: str) -> Trigger | None:
        text = content.strip()
        if not text or user_id == "agent":
            return None
        return Trigger(
            id=new_id("trg"),
            thread_id=dm_thread(user_id),
            channel="dm",
            owner_id=user_id,
            owner_name=nickname,
            content=text,
            message_id="",
            source="dm",
        )
