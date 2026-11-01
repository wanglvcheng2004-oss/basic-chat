import asyncio
import json

from backend.agent.config import AgentConfig
from backend.agent.llm import BaseLLM, LLMEvent, LLMResult
from backend.agent.runtime import AgentRuntime
from backend.agent.store import AgentStore
from backend.agent.tools import execute_tool
from backend.agent.types import Trigger
from backend.tests.test_agent import FakeClient, FakeManager


def test_private_tools_never_cross_owner_or_public_output():
    async def scenario():
        store = AgentStore()
        msg = store.add_message(thread_id='dm:alice', channel='dm', role='user',
                                nickname='Alice', content='PRIVATE', user_id='alice')
        private = store.create_task(thread_id='dm:alice', channel='dm', owner_id='alice', title='private', instruction='private')
        store.update_task(private, result='PRIVATE RESULT')
        for thread, channel, owner in [('dm:bob', 'dm', 'bob'), ('room:main', 'room', 'alice')]:
            task = store.create_task(thread_id=thread, channel=channel, owner_id=owner, title='t', instruction='i')
            for name, args in [('read_thread', {'thread_id': 'dm:alice'}), ('read_message', {'message_id': msg.id})]:
                result = json.loads(await execute_tool(name, args, store=store, task=task))
                assert 'error' in result
            listed = json.loads(await execute_tool('list_tasks', {}, store=store, task=task))
            assert private.id not in [item['taskId'] for item in listed['tasks']]
    asyncio.run(scenario())


class GatedLLM(BaseLLM):
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.inputs = []

    async def stream(self, messages, **kwargs):
        self.inputs.append(str(messages))
        self.started.set()
        await self.release.wait()
        yield LLMEvent('final', result=LLMResult(content='completed'))


def test_followup_arriving_during_final_round_is_consumed():
    async def scenario():
        llm = GatedLLM()
        runtime = AgentRuntime(AgentConfig(), FakeManager(), llm=llm)
        trigger = Trigger(id='first', thread_id='room:main', channel='room', owner_id='u1', owner_name='A', content='first')
        task = await runtime.start_task(trigger, 'first', 'first')
        await asyncio.wait_for(llm.started.wait(), 2)
        await runtime.on_followup(FakeClient(), task.id, 'FOLLOWUP_MARKER')
        llm.release.set()
        await asyncio.wait_for(runtime._workers[task.id], 2)
        assert any('FOLLOWUP_MARKER' in item for item in llm.inputs)
        assert task.status == 'done'
        await runtime.stop()
    asyncio.run(scenario())


def test_room_request_does_not_hijack_another_members_worker():
    async def scenario():
        llm = GatedLLM()
        runtime = AgentRuntime(AgentConfig(), FakeManager(), llm=llm)
        first = Trigger(id='first', thread_id='room:main', channel='room', owner_id='u1', owner_name='A', content='first')
        task = await runtime.start_task(first, 'first', 'first')
        await asyncio.wait_for(llm.started.wait(), 2)
        second = Trigger(id='second', thread_id='room:main', channel='room', owner_id='u2', owner_name='B', content='independent')
        routed = []
        async def decide(trigger):
            routed.append(trigger.id)
        runtime.router.decide = decide
        await runtime.router.route(second)
        assert routed == ['second']
        assert not runtime._sessions[task.id].inbox
        llm.release.set()
        await asyncio.wait_for(runtime._workers[task.id], 2)
        await runtime.stop()
    asyncio.run(scenario())


def test_harness_final_round_consumes_followup_before_finishing():
    from backend.agent.dsh import DshResult
    class GatedHarness:
        def __init__(self):
            self.inputs = []
            self.started = asyncio.Event()
            self.release = asyncio.Event()
        def available(self):
            return True
        async def run(self, prompt, **kwargs):
            self.inputs.append(prompt)
            self.started.set()
            await self.release.wait()
            return DshResult(content='harness done', session_id='test-session', exit_code=0)
    async def scenario():
        runtime = AgentRuntime(AgentConfig(allow_write=True, dsh_policy_verified=True), FakeManager())
        runtime.dsh = GatedHarness()
        trigger = Trigger(id='first', thread_id='room:main', channel='room', owner_id='u1', owner_name='A', content='first')
        task = await runtime.start_task(trigger, 'first', 'first')
        await asyncio.wait_for(runtime.dsh.started.wait(), 2)
        await runtime.on_followup(FakeClient(), task.id, 'HARNESS_FOLLOWUP')
        runtime.dsh.release.set()
        await asyncio.wait_for(runtime._workers[task.id], 2)
        assert len(runtime.dsh.inputs) == 2
        assert 'HARNESS_FOLLOWUP' in runtime.dsh.inputs[-1]
        assert task.status == 'done'
        await runtime.stop()
    asyncio.run(scenario())
