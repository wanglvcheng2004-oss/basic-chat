"""Real TCP/WebSocket test, both addresses confined to local loopback."""
import asyncio
import json
import os
import socket
import subprocess
import sys
import time

import httpx
import websockets

from backend.run import addresses


def test_dual_listener_member_sync_and_private_history(tmp_path):
    assert addresses('127.0.0.2') == ['127.0.0.1', '127.0.0.2']
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    env = {**os.environ, 'CHAT_DATA_DIR': str(tmp_path / '中文 状态'), 'LLM_API_KEY': '', 'HARNESS_ENABLED': 'false'}
    process = subprocess.Popen([sys.executable, '-m', 'backend.run', '--listen', '127.0.0.2', '--port', str(port)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    try:
        base = f'http://127.0.0.1:{port}'
        remote = f'http://127.0.0.2:{port}'
        with httpx.Client(trust_env=False, timeout=2) as host, httpx.Client(trust_env=False, timeout=2) as guest:
            for _ in range(100):
                try:
                    if host.get(base + '/health').status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                assert process.poll() is None, 'Server exited during startup'
                time.sleep(.05)
            assert host.get(base + '/health').status_code == 200
            assert guest.get(remote + '/health').status_code == 200
            host_id = host.post(base + '/api/session', json={'nickname': 'Host'}).json()['member']['userId']
            invite = host.get(base + '/api/automation').json()['invite']
            guest_id = guest.post(remote + '/api/session', json={'nickname': 'Guest', 'invite': invite}).json()['member']['userId']
            assert host_id != guest_id
            host_cookie = host.cookies.get('basic_chat_session')
            guest_cookie = guest.cookies.get('basic_chat_session')

        async def packet(ws, kind):
            async with asyncio.timeout(4):
                while True:
                    value = json.loads(await ws.recv())
                    if value['type'] == kind:
                        return value

        async def scenario():
            async with websockets.connect(f'ws://127.0.0.1:{port}/ws', additional_headers={'Cookie': 'basic_chat_session=' + host_cookie}, proxy=None) as a:
                assert (await packet(a, 'ready'))['userId'] == host_id
                await packet(a, 'agent.automation')
                async with websockets.connect(f'ws://127.0.0.2:{port}/ws', additional_headers={'Cookie': 'basic_chat_session=' + guest_cookie}, proxy=None) as b:
                    assert (await packet(b, 'ready'))['onlineCount'] == 2
                    await packet(b, 'agent.automation')
                    await a.send(json.dumps({'type': 'chat', 'content': 'SHARED'}))
                    assert (await packet(a, 'chat'))['content'] == 'SHARED'
                    assert (await packet(b, 'chat'))['content'] == 'SHARED'
                    await a.send(json.dumps({'type': 'agent.dm', 'content': 'PRIVATE_MARKER'}))
                    assert (await packet(a, 'agent.message'))['content'] == 'PRIVATE_MARKER'
                    await packet(a, 'agent.done')
                async with websockets.connect(f'ws://127.0.0.2:{port}/ws', additional_headers={'Cookie': 'basic_chat_session=' + guest_cookie}, proxy=None) as b:
                    assert (await packet(b, 'ready'))['userId'] == guest_id
                    assert 'PRIVATE_MARKER' not in str(await packet(b, 'agent.history'))
                    assert any(m['content'] == 'SHARED' for m in (await packet(b, 'chat.history'))['messages'])
        asyncio.run(scenario())
    finally:
        process.terminate()
        process.wait(timeout=5)
