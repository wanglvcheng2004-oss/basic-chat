from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path

import aiosqlite

SCHEMA = '''
CREATE TABLE IF NOT EXISTS members(id TEXT PRIMARY KEY, nickname TEXT NOT NULL, admin INTEGER NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, member_id TEXT NOT NULL REFERENCES members(id), expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS leases(name TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS message_thread ON messages(thread_id,created_at);
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, owner_id TEXT NOT NULL, status TEXT NOT NULL, kind TEXT NOT NULL, updated_at TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, task_id TEXT, state TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notifications(id TEXT PRIMARY KEY, scope TEXT NOT NULL, owner_id TEXT NOT NULL, created REAL NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS receipts(member_id TEXT NOT NULL, notification_id TEXT NOT NULL, PRIMARY KEY(member_id,notification_id));
CREATE TABLE IF NOT EXISTS policies(scope TEXT PRIMARY KEY, owner_id TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, kind TEXT NOT NULL, scope TEXT NOT NULL, owner_id TEXT NOT NULL, status TEXT NOT NULL, fingerprint TEXT NOT NULL, due REAL NOT NULL, expires REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL, result TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS job_due ON jobs(status,due);
CREATE TABLE IF NOT EXISTS cache(id TEXT PRIMARY KEY, scope TEXT NOT NULL, expires REAL NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS summary_runs(id TEXT PRIMARY KEY, scope TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL, status TEXT NOT NULL, message_id TEXT);
CREATE TABLE IF NOT EXISTS budgets(scope TEXT NOT NULL, day TEXT NOT NULL, used INTEGER NOT NULL, PRIMARY KEY(scope,day));
'''


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


class Database:
    """One async connection and serialized transactions on a local disk."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.connection = None
        self.lock = asyncio.Lock()

    async def open(self):
        existed = self.path.is_file()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = await aiosqlite.connect(self.path, isolation_level=None)
        self.connection.row_factory = aiosqlite.Row
        await self.connection.execute('PRAGMA journal_mode=WAL')
        await self.connection.execute('PRAGMA foreign_keys=ON')
        await self.connection.execute('PRAGMA busy_timeout=5000')
        cursor = await self.connection.execute('PRAGMA user_version')
        version = (await cursor.fetchone())[0]
        if version > 1:
            await self.close()
            raise RuntimeError('数据库版本高于当前程序，请使用匹配版本，未尝试降级。')
        if existed and version < 1:
            backup = sqlite3.connect(str(self.path) + f'.v{version}.{time.time_ns()}.bak')
            try:
                await self.connection.backup(backup)
            finally:
                backup.close()
        await self.connection.executescript(SCHEMA)
        await self.connection.execute('PRAGMA user_version=1')

    async def close(self):
        if self.connection is not None:
            await self.connection.close()
            self.connection = None

    async def execute(self, sql, params=()):
        async with self.lock:
            cursor = await self.connection.execute(sql, params)
            return cursor.rowcount

    async def one(self, sql, params=()):
        async with self.lock:
            cursor = await self.connection.execute(sql, params)
            row = await cursor.fetchone()
            return dict(row) if row is not None else None

    async def all(self, sql, params=()):
        async with self.lock:
            cursor = await self.connection.execute(sql, params)
            return [dict(row) for row in await cursor.fetchall()]

    async def batch(self, statements):
        async def operation(conn):
            for sql, args in statements:
                await conn.execute(sql, args)
        await self.transaction(operation)

    async def transaction(self, operation):
        async with self.lock:
            await self.connection.execute('BEGIN IMMEDIATE')
            try:
                result = await operation(self.connection)
                await self.connection.commit()
                return result
            except BaseException:
                await self.connection.rollback()
                raise

    async def lease(self, name, owner, now=None, seconds=30):
        now = time.time() if now is None else now
        async def claim(conn):
            cursor = await conn.execute('SELECT owner,expires FROM leases WHERE name=?', (name,))
            row = await cursor.fetchone()
            if row and row['owner'] != owner and row['expires'] > now:
                return False
            await conn.execute('INSERT OR REPLACE INTO leases VALUES(?,?,?)', (name, owner, now + seconds))
            return True
        return await self.transaction(claim)

    async def release(self, name, owner):
        await self.execute('DELETE FROM leases WHERE name=? AND owner=?', (name, owner))
