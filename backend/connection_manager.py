from __future__ import annotations

from dataclasses import dataclass
import asyncio
from uuid import uuid4

from fastapi import WebSocket


@dataclass
class Client:
    user_id: str
    nickname: str
    socket: WebSocket
    connection_id: str = ''
    admin: bool = False


class ConnectionManager:
    def __init__(self) -> None:
        self.connections: dict[str, Client] = {}

    @property
    def count(self) -> int:
        return len({client.user_id for client in self.connections.values()})

    def members(self) -> list[dict[str, str]]:
        unique = {client.user_id: client for client in self.connections.values()}
        return [{"userId": client.user_id, "nickname": client.nickname} for client in unique.values()]

    def add(self, nickname: str, socket: WebSocket, user_id=None, admin=False) -> Client:
        connection_id = str(uuid4())
        client = Client(user_id or connection_id, nickname, socket, connection_id, admin)
        self.connections[connection_id] = client
        return client

    def remove(self, connection_id: str) -> None:
        self.connections.pop(connection_id, None)

    async def send(self, client: Client, packet: dict) -> None:
        await asyncio.wait_for(client.socket.send_json(packet), 3)

    async def send_to(self, user_id: str, packet: dict) -> bool:
        clients = [c for c in list(self.connections.values()) if c.user_id == user_id]
        results = await asyncio.gather(*(self._try_send(c, packet) for c in clients))
        return any(results)

    async def _try_send(self, client, packet):
        try:
            await self.send(client, packet)
            return True
        except Exception:
            self.remove(client.connection_id)
            return False

    async def broadcast(self, packet: dict) -> None:
        await asyncio.gather(*(self._try_send(c, packet) for c in list(self.connections.values())))
