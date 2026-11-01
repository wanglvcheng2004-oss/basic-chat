from __future__ import annotations

import asyncio

from .context import build_router_messages
from .llm import LLMError, LLMResult
from .types import Trigger, new_id

DISPATCH_TOOL = {
    "type": "function",
    "function": {
        "name": "dispatch_task",
        "description": "把需要多步执行、读写文件或较长分析的独立任务交给 Worker 执行。",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "任务标题，20 字以内"},
                "instruction": {"type": "string", "description": "完整、可独立执行的任务说明"},
                "complexity": {
                    "type": "string",
                    "enum": ["light", "heavy"],
                    "description": "light：单文件或小改动；heavy：重构、多文件、调试或长链路任务",
                },
            },
            "required": ["title", "instruction"],
            "additionalProperties": False,
        },
    },
}


class Router:
    """常驻的全局路由层：只做对话、分派与汇总，不直接执行具体任务。"""

    def __init__(self, runtime) -> None:
        self.runtime = runtime
        self.config = runtime.config
        self.store = runtime.store
        self.llm = runtime.llm

    async def run(self) -> None:
        while True:
            first = await self.runtime.queue.get()
            batch = [first]
            while len(batch) < self.config.batch_size:
                try:
                    batch.append(self.runtime.queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            for trigger in batch:
                try:
                    await self.route(trigger)
                except asyncio.CancelledError:
                    raise
                except LLMError as exc:
                    await self.runtime.notify(trigger, f"模型调用失败：{exc}")
                except Exception as exc:  # 单条事件失败不应终止 Router
                    await self.runtime.notify(trigger, f"处理失败：{exc}")
                finally:
                    self.store.finish_event(trigger.id)
                    await self.store.flush()

    async def route(self, trigger: Trigger) -> None:
        # A room is a visibility scope, never the identity of a running task.
        async with self.runtime.dispatcher.slot('router:' + trigger.id):
            await self.decide(trigger)

    async def decide(self, trigger: Trigger) -> None:
        messages = build_router_messages(self.config, self.store, trigger)
        message_id = new_id("msg")
        streamed = False
        result = LLMResult()
        async for event in self.llm.stream(
            messages,
            model=self.config.router_model,
            tools=[DISPATCH_TOOL],
            temperature=self.config.temperature,
            max_tokens=self.config.router_max_tokens,
            thinking=self.config.router_thinking,
            reasoning_effort=self.config.reasoning_effort,
        ):
            if event.kind == "delta" and event.text:
                streamed = True
                await self.runtime.emit_delta(trigger, message_id, event.text)
            elif event.kind == "final" and event.result is not None:
                result = event.result

        dispatch = next((call for call in result.tool_calls if call.name == "dispatch_task"), None)
        if dispatch is not None:
            args = dispatch.parsed()
            title = str(args.get("title") or "未命名任务").strip()[:80]
            instruction = str(args.get("instruction") or trigger.content).strip()[:4000]
            complexity = "heavy" if str(args.get("complexity") or "").lower() == "heavy" else "light"
            content = (result.content or "").strip()
            if not streamed and not content:
                content = f"收到，我来处理：{title}"
            if content or streamed:
                await self.runtime.emit_reply(trigger, message_id, content or "收到，我来处理。")
            await self.runtime.start_task(trigger, title, instruction, complexity)
            return

        content = (result.content or "").strip()
        if content:
            await self.runtime.emit_reply(trigger, message_id, content)
        elif not streamed:
            await self.runtime.emit_reply(trigger, message_id, "我在。告诉我具体想做什么就可以。")
