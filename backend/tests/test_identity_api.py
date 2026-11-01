import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.agent.config import AgentConfig
from backend.main import create_app


def test_identity_invite_origin_and_reconnect(tmp_path):
    app = create_app(AgentConfig(data_dir=str(tmp_path)))
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 5000)) as host:
        assert host.get('/api/automation').status_code == 401
        with pytest.raises(WebSocketDisconnect):
            with host.websocket_connect('ws://127.0.0.1/ws?nickname=Admin'):
                pass
        assert host.post('/api/session', json={'nickname': 'Host'}, headers={'Origin': 'https://evil.example'}).status_code == 403
        owner = host.post('/api/session', json={'nickname': 'Host'}).json()['member']
        assert owner['admin'] is True
        invite = host.get('/api/automation').json()['invite']
        with host.websocket_connect('ws://127.0.0.1/ws') as socket:
            assert socket.receive_json()['userId'] == owner['userId']
        with host.websocket_connect('ws://127.0.0.1/ws') as socket:
            assert socket.receive_json()['userId'] == owner['userId']
        with pytest.raises(WebSocketDisconnect):
            with host.websocket_connect('ws://127.0.0.1/ws', headers={'Origin': 'https://evil.example'}):
                pass
        assert host.post('http://26.1.1.1/api/session', json={'nickname': 'Host'}).status_code == 403
        assert host.post('http://26.1.1.1/api/session', json={'nickname': 'Guest', 'invite': '错误邀请码'}).status_code == 403
        person = host.post('http://26.1.1.1/api/session', json={'nickname': 'Guest', 'invite': invite}).json()['member']
        assert not person['admin'] and person['userId'] != owner['userId']
        assert host.put('http://26.1.1.1/api/automation', json={'summary': True}).status_code == 403
        assert 'invite' not in host.get('http://26.1.1.1/api/automation').json()
        assert host.post('http://26.1.1.1/api/session', json={'nickname': 'Changed'}).json()['member']['userId'] == person['userId']


def test_service_restart_preserves_session_and_room_history(tmp_path):
    config = AgentConfig(data_dir=str(tmp_path))
    app = create_app(config)
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 5000)) as client:
        member = client.post('/api/session', json={'nickname': 'Host'}).json()['member']
        cookie = client.cookies.get('basic_chat_session')
        with client.websocket_connect('ws://127.0.0.1/ws') as socket:
            socket.send_json({'type': 'chat', 'content': 'PERSISTED_MESSAGE'})
            while socket.receive_json()['type'] != 'chat':
                pass
    with TestClient(create_app(config), base_url='http://127.0.0.1', client=('127.0.0.1', 5000)) as client:
        client.cookies.set('basic_chat_session', cookie)
        assert client.get('/api/session').json()['member']['userId'] == member['userId']
        with client.websocket_connect('ws://127.0.0.1/ws') as socket:
            packet = socket.receive_json()
            while packet['type'] != 'chat.history':
                packet = socket.receive_json()
            assert any(m['content'] == 'PERSISTED_MESSAGE' for m in packet['messages'])
