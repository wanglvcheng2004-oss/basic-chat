import asyncio
import ctypes
import json
import os
from pathlib import Path
import sys
import time

import pytest

from backend.agent.dispatcher import Dispatcher
from backend.agent.dsh import DshBackend
from backend.agent.llm import BaseLLM, LLMEvent, LLMResult
from backend.agent.scheduler import latest_boundary, next_boundary
from backend.agent.tools import execute_tool
from backend.database import Database
from backend.tests.test_agent import FakeClient
from backend.tests.test_automation import environment, message, settle


class FailingLLM(BaseLLM):
    def __init__(self):
        self.calls = 0

    async def stream(self, *args, **kwargs):
        self.calls += 1
        raise RuntimeError('simulated provider failure')
        yield LLMEvent('final', result=LLMResult())


def test_stop_pending_retry_is_durable(tmp_path):
    async def scenario():
        model = FailingLLM()
        async with environment(tmp_path, model) as (runtime, service, db):
            now = time.time()
            await service.configure({'preprocess': True}, 'alice', now=now)
            await service.observe(message(runtime, 'Traceback (most recent call last):\nValueError: bad', now), now=now)
            await service.tick(now + 11)
            await settle(runtime)
            task = next(iter(runtime.store.tasks.values()))
            assert task.retry_pending is True
            await runtime.stop_task(task.id, FakeClient(user_id='alice'))
            assert task.status == 'stopped' and not task.retry_pending
            await service.tick(now + 100)
            await settle(runtime)
            assert model.calls == 1
            await service.initialize()
            assert (await db.one('SELECT status FROM jobs'))['status'] == 'stopped'
    asyncio.run(scenario())


def test_watchdog_is_model_free_and_notifies_on_each_transition_once(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (runtime, service, db):
            await service.configure({'watchdog': True}, 'alice')
            task = runtime.store.create_task(thread_id='dm:alice', channel='dm', owner_id='alice', title='t', instruction='i')
            now = time.time()
            runtime.store.update_task(task, status='running', last_signal_at=now)
            await runtime.watchdog.tick(now + 1)
            assert not await db.all('SELECT * FROM notifications')
            await runtime.watchdog.tick(now + 200)
            await runtime.watchdog.tick(now + 201)
            assert len(await db.all('SELECT * FROM notifications')) == 1
            task.last_signal_at = now + 202
            await runtime.watchdog.tick(now + 202)
            await runtime.watchdog.tick(now + 500)
            assert len(await db.all('SELECT * FROM notifications')) == 3
            assert not runtime.llm.calls
            assert all(owner == 'alice' for owner, packet in runtime.manager.sent)
            assert not any(p['type'] == 'agent.notice' for p in runtime.manager.broadcasted)
            task.status = 'done'
    asyncio.run(scenario())


def test_dispatcher_prioritizes_user_and_limits_background():
    async def scenario():
        dispatcher = Dispatcher(1)
        order = []
        async def run(key, bg):
            async with dispatcher.slot(key, bg):
                order.append(key)
                await asyncio.sleep(0)
        async with dispatcher.slot('occupied'):
            background = asyncio.create_task(run('background', True))
            foreground = asyncio.create_task(run('user', False))
            await asyncio.sleep(0)
        await asyncio.gather(background, foreground)
        assert order == ['user', 'background']
        dispatcher = Dispatcher(3)
        active, maximum = 0, 0
        async def bg(key):
            nonlocal active, maximum
            async with dispatcher.slot(key, True):
                active += 1
                maximum = max(maximum, active)
                await asyncio.sleep(0)
                active -= 1
        await asyncio.gather(*(bg(str(i)) for i in range(5)))
        assert maximum == 1
    asyncio.run(scenario())


def test_calendar_catchup_uses_last_fixed_boundary(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (runtime, service, db):
            start = 1_790_000_000.
            await service.configure({'summary': True}, 'alice', now=start)
            end = latest_boundary(start + 4 * 86400)
            message(runtime, 'EARLY_SOURCE', start + 1)
            message(runtime, 'LATE_SOURCE', end - 1)
            await runtime.store.flush()
            await service.tick(start + 4 * 86400)
            await settle(runtime)
            await service.tick(start + 4 * 86400)
            assert len(runtime.llm.calls) == 1
            assert len(await db.all('SELECT * FROM summary_runs')) == 1
            assert service.policy['cursor'] == end
            assert service.policy['nextDue'] == next_boundary(end)
            assert (await service.snapshot())['summaries'][0]['catchup']
    asyncio.run(scenario())


def test_database_lease_prevents_duplicate_scheduler(tmp_path):
    async def scenario():
        a, b = Database(tmp_path / 'chat.db'), Database(tmp_path / 'chat.db')
        await a.open()
        await b.open()
        try:
            assert await a.lease('runtime', 'a', now=100)
            assert not await b.lease('runtime', 'b', now=110)
            assert await a.lease('runtime', 'a', now=110)
            assert not await b.lease('runtime', 'b', now=135)
            await a.release('runtime', 'a')
            assert await b.lease('runtime', 'b', now=136)
        finally:
            await a.close()
            await b.close()
    asyncio.run(scenario())


def test_readonly_background_denies_writes_secrets_and_escapes(tmp_path, monkeypatch):
    from backend.agent import tools
    monkeypatch.setattr(tools, 'ROOT', tmp_path)
    source = tmp_path / 'source.py'
    source.write_text('unchanged')
    for name in ('.env', 'chat.sqlite3-wal'):
        (tmp_path / name).write_text('PRIVATE')
    async def scenario():
        async with environment(tmp_path) as (runtime, service, db):
            task = runtime.store.create_task(thread_id='room:main', channel='room', owner_id='alice', title='t', instruction='i', kind='preprocess')
            result = await execute_tool('write_file', {'path': 'source.py', 'content': 'changed'}, store=runtime.store, task=task, allow_write=True)
            assert 'error' in json.loads(result)
            for name in ('.env', 'chat.sqlite3-wal', '../outside.py'):
                result = await execute_tool('read_file', {'path': name}, store=runtime.store, task=task)
                assert 'error' in json.loads(result) and 'PRIVATE' not in result
            assert source.read_text() == 'unchanged'
    asyncio.run(scenario())


@pytest.mark.skipif(os.name != 'nt', reason='Windows process tree contract')
def test_windows_stop_ends_real_child_process_tree(tmp_path):
    folder = tmp_path / '中文 含空格'
    folder.mkdir()
    pidfile = folder / 'child.pid'
    script = folder / 'parent.py'
    script.write_text("import subprocess,sys,time\nfrom pathlib import Path\np=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\nPath(sys.argv[1]).write_text(str(p.pid))\ntime.sleep(60)\n")
    async def scenario():
        process = await asyncio.create_subprocess_exec(sys.executable, str(script), str(pidfile))
        try:
            for _ in range(200):
                if pidfile.exists():
                    break
                await asyncio.sleep(.01)
            assert pidfile.exists()
            child_pid = int(pidfile.read_text())
            await asyncio.wait_for(DshBackend._terminate(process), 5)
            assert process.returncode is not None
            kernel = ctypes.windll.kernel32
            kernel.OpenProcess.restype = ctypes.c_void_p
            handle = kernel.OpenProcess(0x1000, False, child_pid)
            if handle:
                code = ctypes.c_ulong()
                kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
                kernel.CloseHandle.argtypes = [ctypes.c_void_p]
                assert kernel.GetExitCodeProcess(handle, ctypes.byref(code))
                kernel.CloseHandle(handle)
                assert code.value != 259
        finally:
            if process.returncode is None:
                await DshBackend._terminate(process)
    asyncio.run(scenario())
