from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass

from .schemas import clean_text


@dataclass(frozen=True)
class Member:
    id: str
    nickname: str
    admin: bool = False

    def public(self):
        return {'userId': self.id, 'nickname': self.nickname, 'admin': self.admin}


class IdentityService:
    def __init__(self, db, agent_name='小深'):
        self.db = db
        self.agent_name = agent_name

    async def initialize(self):
        await self.db.execute('INSERT OR IGNORE INTO meta VALUES(?,?)', ('invite', secrets.token_urlsafe(18)))

    async def invite(self):
        return (await self.db.one('SELECT value FROM meta WHERE key=?', ('invite',)))['value']

    async def login(self, nickname, invite, *, local=False, existing=None):
        name = clean_text(nickname, max_length=20, label='昵称')
        if name == self.agent_name or name.lower() == 'agent':
            raise ValueError('该昵称由智能体使用，请换一个。')
        if not local and existing is None and not secrets.compare_digest((invite or '').encode(), (await self.invite()).encode()):
            raise PermissionError('请输入主机提供的有效邀请码。')
        member_id = existing.id if existing else secrets.token_hex(16)
        admin = bool(local or (existing and existing.admin))
        token = secrets.token_urlsafe(32)
        expires = time.time() + 30 * 86400
        await self.db.batch([
            ('INSERT INTO members VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET nickname=excluded.nickname,admin=excluded.admin', (member_id, name, int(admin), time.time())),
            ('INSERT INTO sessions VALUES(?,?,?)', (self.digest(token), member_id, expires)),
        ])
        return Member(member_id, name, admin), token

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    async def resolve(self, token):
        if not token:
            return None
        row = await self.db.one('SELECT m.* FROM sessions s JOIN members m ON m.id=s.member_id WHERE s.token_hash=? AND s.expires>?', (self.digest(token), time.time()))
        return Member(row['id'], row['nickname'], bool(row['admin'])) if row else None

    async def logout(self, token):
        if token:
            await self.db.execute('DELETE FROM sessions WHERE token_hash=?', (self.digest(token),))
