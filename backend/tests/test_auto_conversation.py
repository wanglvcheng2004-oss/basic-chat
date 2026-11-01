import asyncio
import json
import time

from backend.agent.llm import BaseLLM, LLMEvent, LLMResult
from backend.tests.test_automation import environment, message


class ContextLLM(BaseLLM):
    def __init__(self):
        self.worker_calls = []
        self.running = 0
        self.max_running = 0

    async def stream(self, messages, *, model, tools=None, **kwargs):
        if '群聊介入判断' in messages[0]['content']:
            text = json.loads(messages[-1]['content'])['latest']['content']
            answer = {'reply': '项目问题' in text, 'question': text}
            yield LLMEvent('final', result=LLMResult(content=json.dumps(answer, ensure_ascii=False)))
            return
        self.worker_calls.append({'messages': messages, 'tools': tools})
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        await asyncio.sleep(0.05)
        self.running -= 1
        yield LLMEvent('final', result=LLMResult(content='这是解决建议。'))


def test_model_selects_and_runs_independent_member_tasks(tmp_path):
    async def scenario():
        llm = ContextLLM()
        async with environment(tmp_path, llm) as (runtime, automation, db):
            now = time.time()
            await automation.configure({'autoReply': True}, 'admin', now=now)
            await automation.observe(message(runtime, '休息闲聊', now, owner='alice'), now=now)
            await asyncio.gather(*list(automation._decisions))
            assert not runtime._workers
            first = message(runtime, '项目问题 Alice 怎么处理', now, owner='alice')
            second = message(runtime, '项目问题 Bob 怎么处理', now, owner='bob')
            await automation.observe(first, now=now)
            await automation.observe(second, now=now)
            await asyncio.gather(*list(automation._decisions))
            await asyncio.gather(*list(runtime._workers.values()))
            assert len(llm.worker_calls) == 2
            assert llm.max_running == 2
            assert all(not any(t['function']['name'] == 'write_file' for t in call['tools']) for call in llm.worker_calls)
            replies = [p for p in runtime.manager.broadcasted if p.get('type') == 'agent.done' and p.get('taskId')]
            assert {p['replyTo'] for p in replies} == {first.id, second.id}
            assert {p['replyToUserId'] for p in replies} == {'alice', 'bob'}
            assert len(await runtime.store.history('room:main')) >= 2
            assert all('Bob 怎么处理' not in str(call['messages']) for call in llm.worker_calls if 'Alice 怎么处理' in str(call['messages']))
    asyncio.run(scenario())


def test_disabled_or_targeted_messages_never_call_model(tmp_path):
    async def scenario():
        llm = ContextLLM()
        async with environment(tmp_path, llm) as (runtime, automation, db):
            now = time.time()
            await automation.observe(message(runtime, '项目问题 默认关闭', now), now=now)
            assert not automation._decisions and not llm.worker_calls
            await automation.configure({'autoReply': True}, 'admin', now=now)
            for text in ['@小深 项目问题', '@小雨 项目问题']:
                await automation.observe(message(runtime, text, now), now=now)
            assert not automation._decisions and not llm.worker_calls
    asyncio.run(scenario())


def test_member_session_tool_access_is_scoped():
    from backend.access import can_read_thread
    from backend.agent.types import Task
    task = Task(id='t', thread_id='room-member:alice', channel='room', owner_id='alice', title='x', instruction='x', kind='auto_assist')
    assert can_read_thread(task, 'room-member:alice')
    assert not can_read_thread(task, 'room-member:bob')
    assert not can_read_thread(task, 'dm:bob')
