import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import time

from backend.agent.config import AgentConfig
from backend.agent.automation import Automation
from backend.agent.runtime import AgentRuntime
from backend.agent.store import AgentStore
from backend.agent.types import ROOM_THREAD
from backend.database import Database
from backend.tests.test_agent import FakeManager, ScriptedLLM
from backend.agent.llm import LLMResult


@asynccontextmanager
async def environment(tmp_path, llm=None):
    db = Database(tmp_path / 'state' / 'chat.sqlite3')
    await db.open()
    runtime = AgentRuntime(AgentConfig(allow_write=False), FakeManager(),
                           store=AgentStore(db=db), llm=llm or ScriptedLLM([LLMResult(content='分析结果')]))
    service = Automation(runtime, db)
    runtime.automation = service
    await service.initialize()
    try:
        yield runtime, service, db
    finally:
        await runtime.stop()
        await db.close()


def message(runtime, text, now, *, owner='alice', channel='room', role='user'):
    return runtime.store.add_message(thread_id=ROOM_THREAD if channel == 'room' else 'dm:' + owner,
        channel=channel, role=role, nickname=owner, user_id=owner, content=text,
        created_at=datetime.fromtimestamp(now, timezone.utc).isoformat())


async def settle(runtime):
    await asyncio.wait_for(asyncio.gather(*list(runtime._workers.values())), 5)


def test_preprocess_filter_dedupe_and_cache_invalidation(tmp_path, monkeypatch):
    from backend.agent import tools
    monkeypatch.setattr(tools, 'ROOT', tmp_path)
    source = tmp_path / '测试 文件.py'
    source.write_text('value = 1\n', encoding='utf-8')

    async def scenario():
        async with environment(tmp_path) as (runtime, service, db):
            now = time.time()
            await service.configure({'preprocess': True}, 'admin', now=now)
            for text, role in [('周末去哪玩', 'user'), ('@小深 错误 测试 文件.py', 'user'), ('Traceback Error', 'agent')]:
                await service.observe(message(runtime, text, now, role=role), now=now)
            assert not await db.all('SELECT * FROM jobs')
            text = 'Traceback (most recent call last):\n  File "测试 文件.py", line 1\nValueError: broken'
            first = message(runtime, text, now)
            await service.observe(first, now=now)
            await service.observe(message(runtime, text, now + 5), now=now + 5)
            await service.tick(now + 9)
            assert not runtime.llm.calls
            await service.tick(now + 16)
            await settle(runtime)
            assert len(runtime.llm.calls) == 1
            assert runtime.llm.calls[0]['tools'] is None
            assert source.read_text() == 'value = 1\n'
            assert not any(p['type'] == 'agent.done' for p in runtime.manager.broadcasted)
            assert len((await service.snapshot(now=now + 20))['prepared']) == 1
            await service.observe(message(runtime, text, now + 20), now=now + 20)
            assert len(await db.all('SELECT * FROM jobs')) == 1
            source.write_text('value = 2\n')
            assert not (await service.snapshot(now=now + 21))['prepared']
            await service.observe(message(runtime, text, now + 22), now=now + 22)
            assert len(await db.all('SELECT * FROM jobs')) == 2
    asyncio.run(scenario())


def test_summary_windows_are_durable_private_and_half_open(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (runtime, service, db):
            start = time.time() - 100
            await service.configure({'summary': True}, 'admin', now=start)
            message(runtime, 'INCLUDED', start + 10)
            message(runtime, 'PRIVATE', start + 20, channel='dm')
            message(runtime, 'NEXT_WINDOW', start + 50)
            message(runtime, 'OLD_BOT_NOISE', start + 30, role='agent')
            await runtime.store.flush()
            await service.schedule_summary(now=start + 50, manual=True)
            await service.tick(start + 50)
            await settle(runtime)
            inputs = str(runtime.llm.calls)
            assert 'INCLUDED' in inputs
            assert 'PRIVATE' not in inputs and 'NEXT_WINDOW' not in inputs and 'OLD_BOT_NOISE' not in inputs
            assert service.policy['cursor'] == start + 50
            notices = await db.all('SELECT * FROM notifications')
            assert len(notices) == 1
            restarted = Automation(runtime, db)
            await restarted.initialize()
            assert restarted.policy['cursor'] == start + 50
            assert len((await restarted.snapshot())['summaries']) == 1
            await service.schedule_summary(now=start + 60, manual=True)
            await service.tick(start + 60)
            await settle(runtime)
            assert 'NEXT_WINDOW' in str(runtime.llm.calls[-1])
    asyncio.run(scenario())


def test_empty_summary_and_daily_budget_do_not_call_model(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (runtime, service, db):
            now = time.time()
            await service.configure({'summary': True, 'dailyLimit': 3}, 'admin', now=now)
            await service.schedule_summary(now=now + 10, manual=True)
            await service.tick(now + 10)
            await settle(runtime)
            assert not runtime.llm.calls
            assert service.policy['cursor'] == now + 10
            assert not await db.all('SELECT * FROM notifications')
            assert await service.reserve_call('preprocess', now=now) is False
            assert await service.reserve_call('summary', now=now) is True
    asyncio.run(scenario())


def test_switch_off_cancels_waiting_work_and_survives_reload(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (runtime, service, db):
            now = time.time()
            await service.configure({'preprocess': True}, 'admin', now=now)
            await service.observe(message(runtime, 'Traceback (most recent call last):\nValueError: bad', now), now=now)
            await service.configure({'preprocess': False}, 'admin', now=now + 1)
            await service.tick(now + 40)
            assert not runtime.llm.calls
            assert all(j['status'] == 'stopped' for j in await db.all('SELECT * FROM jobs'))
            restarted = Automation(runtime, db)
            await restarted.initialize()
            assert restarted.policy['preprocess'] is False
    asyncio.run(scenario())


def test_failed_summary_transaction_does_not_advance_window(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (runtime, service, db):
            now = time.time()
            await service.configure({'summary': True}, 'admin', now=now)
            message(runtime, 'COMMIT_BOUNDARY', now + 1)
            await runtime.store.flush()
            await service.schedule_summary(now=now + 10, manual=True)
            original = db.batch
            failed = False
            async def fail_commit_once(statements):
                nonlocal failed
                if not failed and any(sql.startswith('UPDATE summary_runs SET status=') for sql, args in statements):
                    failed = True
                    raise RuntimeError('simulated interrupted transaction')
                await original(statements)
            db.batch = fail_commit_once
            await service.tick(now + 10)
            await settle(runtime)
            assert service.policy['cursor'] == now
            stored = json.loads((await db.one('SELECT payload FROM policies'))['payload'])
            assert stored['cursor'] == now
            assert not await db.all('SELECT * FROM notifications')
            await service.tick(now + 100)
            await settle(runtime)
            assert service.policy['cursor'] == now + 10
            assert len(await db.all('SELECT * FROM notifications')) == 1
    asyncio.run(scenario())
