from __future__ import annotations

from .context import build_worker_messages
from .dsh import DshEvent
from .llm import LLMResult
from .store import AgentStore
from .tools import execute_tool, tool_schemas
from .types import Task, Trigger, now_iso


class Worker:
    """上下文隔离的执行会话：只处理一个任务，不直接对用户说话。"""

    def __init__(
        self,
        config,
        llm,
        store: AgentStore,
        runtime,
        task: Task,
        trigger: Trigger,
    ) -> None:
        self.config = config
        self.llm = llm
        self.store = store
        self.runtime = runtime
        self.task = task
        self.trigger = trigger
        self.message_id = f"msg_{task.id}_out"
        self.inbox: list[Trigger] = []
        self.finished = False
        self.accepting = True
        self.model = config.worker_model_heavy if task.complexity == "heavy" else config.worker_model
        self._schemas = tool_schemas(config.allow_write)

    def deliver(self, trigger: Trigger) -> bool:
        if self.finished or not self.accepting:
            return False
        self.inbox.append(trigger)
        return True

    def take_pending(self):
        pending, self.inbox = self.inbox, []
        return pending

    def supplements(self):
        pending = self.take_pending()
        for trigger in pending:
            self.store.finish_event(trigger.id, task_id=self.task.id)
        return '\n'.join(f'{item.owner_name}: {item.content}' for item in pending)

    async def run(self) -> None:
        try:
            if self.runtime.harness_allowed():
                await self._run_dsh()
            else:
                await self._run_llm()
        finally:
            self.accepting = False
            self.finished = True

    # ---------- 内置模型 + 本地工具 ----------
    async def _run_llm(self) -> None:
        messages = build_worker_messages(self.config, self.store, self.task, self.trigger)
        for step in range(1, self.config.max_steps + 1):
            self.store.update_task(self.task, steps=step)
            if self.inbox:
                supplement = self.supplements()
                messages.append({"role": "user", "content": f"用户补充了新的消息，请一并考虑：\n{supplement}"})

            result = LLMResult()
            async for event in self.llm.stream(
                messages,
                model=self.model,
                tools=self._schemas,
                temperature=self.config.temperature,
                max_tokens=self.config.worker_max_tokens,
                thinking=self.config.worker_thinking,
                reasoning_effort=self.config.reasoning_effort,
            ):
                self.runtime.signal(self.task)
                if event.kind == "delta" and event.text:
                    await self.runtime.emit_task_delta(self, event.text)
                elif event.kind == "final" and event.result is not None:
                    result = event.result

            if result.tool_calls:
                assistant = {
                    "role": "assistant",
                    "content": result.content or "",
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {"name": call.name, "arguments": call.arguments},
                        }
                        for call in result.tool_calls
                    ],
                }
                # DeepSeek 思考模式下，带 tools 的请求必须回传 reasoning_content。
                if self.config.worker_thinking == "enabled":
                    assistant["reasoning_content"] = result.reasoning
                messages.append(assistant)
                for call in result.tool_calls:
                    args = call.parsed()
                    await self.runtime.emit_trace(self.task, f"tool:{call.name}", _brief(args))
                    output = await execute_tool(
                        call.name,
                        args,
                        store=self.store,
                        task=self.task,
                        allow_write=self.config.allow_write,
                    )
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": output})
                    self.store.update_task(self.task, updated_at=now_iso())
                    self.runtime.signal(self.task, progress=True)
                continue

            content = (result.content or "").strip() or "（任务没有产生文本结果）"
            if self.inbox:
                messages.append({'role': 'assistant', 'content': content, **({'reasoning_content': result.reasoning} if self.config.worker_thinking == 'enabled' else {})})
                continue
            self.accepting = False
            await self.runtime.finish_task(self.task, content)
            return

        await self.runtime.fail_task(self.task, f"超过最大步数（{self.config.max_steps}）")

    # ---------- DeepSeek Harness 后端 ----------
    async def _run_dsh(self) -> None:
        prompt = self._dsh_prompt()
        session_id = self.task.dsh_session_id or None
        for round_index in range(1, self.config.max_steps + 1):
            self.store.update_task(self.task, steps=round_index)
            result = await self.runtime.dsh.run(
                prompt,
                session_id=session_id,
                on_event=self._on_dsh_event,
                model=self.model,
            )
            if result.session_id:
                self.store.update_task(self.task, dsh_session_id=result.session_id)
                session_id = result.session_id

            content = (result.content or "").strip()
            if not content:
                content = "（Harness 没有返回文本结果）"
            if not self.inbox:
                self.accepting = False
                await self.runtime.finish_task(self.task, content)
                return

            followups = self.supplements()
            prompt = f"基于上一次的结果继续处理以下补充消息，并给出更新后的最终答复：\n{followups}"

        await self.runtime.fail_task(self.task, f"补充消息超过最大轮数（{self.config.max_steps}）")

    def _dsh_prompt(self) -> str:
        messages = build_worker_messages(self.config, self.store, self.task, self.trigger)
        return f"{messages[0]['content']}\n\n{messages[1]['content']}\n\n请直接在工作区执行这个任务。"

    async def _on_dsh_event(self, event: DshEvent) -> None:
        self.runtime.signal(self.task, progress=event.kind in {'tool_result', 'final'})
        if event.kind == "text" and event.text:
            await self.runtime.emit_task_delta(self, event.text + "\n")
        elif event.kind == "tool_call":
            await self.runtime.emit_trace(self.task, f"harness:{event.name or 'tool'}", event.detail)
        elif event.kind == "tool_result":
            await self.runtime.emit_trace(self.task, f"result:{event.name or 'tool'}", event.detail)
        elif event.kind == "status" and event.detail:
            await self.runtime.emit_trace(self.task, "harness", event.detail)


def _brief(args: dict) -> str:
    text = ", ".join(f"{key}={str(value)[:60]}" for key, value in args.items())
    return text[:160] or "（无参数）"
