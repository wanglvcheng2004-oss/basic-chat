import pytest
from fastapi import WebSocket

from backend.connection_manager import ConnectionManager
from backend.schemas import clean_text


def test_clean_text():
    assert clean_text("  小雨 ", max_length=20, label="昵称") == "小雨"
    with pytest.raises(ValueError):
        clean_text(" ", max_length=20, label="昵称")
    with pytest.raises(ValueError):
        clean_text("a" * 21, max_length=20, label="昵称")


def test_connection_manager_count():
    manager = ConnectionManager()
    class Socket(WebSocket):
        pass
    socket = object.__new__(Socket)
    client = manager.add("小雨", socket)
    assert manager.count == 1
    manager.remove(client.user_id)
    assert manager.count == 0


def test_presence_lists_members_on_join_and_leave(tmp_path):
    from fastapi.testclient import TestClient
    from backend.main import create_app
    from backend.agent.config import AgentConfig
    app = create_app(AgentConfig(data_dir=str(tmp_path)))

    def receive_presence(socket):
        while True:
            packet = socket.receive_json()
            if packet['type'] == 'presence':
                return packet

    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 5000)) as client:
        client.post('/api/session', json={'nickname': 'Alice'})
        with client.websocket_connect('ws://127.0.0.1/ws') as first:
            first_ready = first.receive_json()
            assert first_ready['members'] == [{'userId': first_ready['userId'], 'nickname': 'Alice'}]
            receive_presence(first)
            client.cookies.clear()
            client.post('/api/session', json={'nickname': 'Bob'})
            with client.websocket_connect('ws://127.0.0.1/ws') as second:
                second_ready = second.receive_json()
                assert {x['nickname'] for x in second_ready['members']} == {'Alice', 'Bob'}
                assert {x['nickname'] for x in receive_presence(first)['members']} == {'Alice', 'Bob'}
                second.close()
                assert [x['nickname'] for x in receive_presence(first)['members']] == ['Alice']
