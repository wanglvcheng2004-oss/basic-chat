"""Durable task-to-PR association and idempotent external event storage."""
from __future__ import annotations

import sqlite3
import hashlib


class ContinuityError(ValueError):
    pass


class ContinuityStore:
    def __init__(self, db):
        self.db = db

    async def initialize(self):
        await self.db.execute('''CREATE TABLE IF NOT EXISTS continuity_links (
            task_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, repo TEXT NOT NULL,
            pr INTEGER NOT NULL, branch TEXT NOT NULL, head_sha TEXT NOT NULL DEFAULT '',
            UNIQUE(repo, pr))''')
        await self.db.execute('''CREATE TABLE IF NOT EXISTS continuity_status (task_id TEXT PRIMARY KEY, state TEXT NOT NULL, merged INTEGER NOT NULL, head_sha TEXT NOT NULL)''')
        await self.db.execute('''CREATE TABLE IF NOT EXISTS continuity_outbox (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL, repo TEXT NOT NULL,
            pr INTEGER NOT NULL, body TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending')''')
        await self.db.execute('''CREATE TABLE IF NOT EXISTS continuity_events (
            repo TEXT NOT NULL, pr INTEGER NOT NULL, kind TEXT NOT NULL, event_id TEXT NOT NULL,
            version TEXT NOT NULL, task_id TEXT NOT NULL, author TEXT NOT NULL, body TEXT NOT NULL,
            url TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'pending', association TEXT NOT NULL DEFAULT 'NONE',
            PRIMARY KEY(repo, pr, kind, event_id, version))''')
        columns = await self.db.all('PRAGMA table_info(continuity_events)')
        if not any(column['name'] == 'association' for column in columns):
            await self.db.execute("ALTER TABLE continuity_events ADD COLUMN association TEXT NOT NULL DEFAULT 'NONE'")

    async def bind(self, task_id, owner_id, repo, pr, branch, head_sha=''):
        if not task_id or not owner_id or not repo or int(pr) <= 0 or not branch:
            raise ContinuityError('任务、仓库、PR 和分支必须有效')
        try:
            await self.db.execute('INSERT INTO continuity_links VALUES(?,?,?,?,?,?)',
                                  (task_id, owner_id, repo.lower(), int(pr), branch, head_sha))
        except sqlite3.IntegrityError as exc:
            raise ContinuityError('任务或 PR 已绑定') from exc

    async def bind_with_history(self, task_id, owner_id, repo, pr, branch, head_sha, events):
        if not task_id or not owner_id or not repo or int(pr) <= 0 or not branch:
            raise ContinuityError('任务、仓库、PR 和分支必须有效')
        async def transaction(conn):
            await conn.execute('INSERT INTO continuity_links VALUES(?,?,?,?,?,?)',
                (task_id, owner_id, repo.lower(), int(pr), branch, head_sha))
            await conn.execute('INSERT INTO continuity_status VALUES(?,?,?,?)', (task_id, 'open', 0, head_sha))
            for event in events:
                if not event.get('id') or not event.get('version'):
                    continue
                await conn.execute("""INSERT OR IGNORE INTO continuity_events
                    (repo,pr,kind,event_id,version,task_id,author,body,url,state,association)
                    VALUES(?,?,?,?,?,?,?,?,?,'historical',?)""",
                    (repo.lower(), int(pr), event['kind'], event['id'], event['version'], task_id,
                     event['author'], event['body'], event.get('url', ''), event.get('association', 'NONE')))
        try:
            await self.db.transaction(transaction)
        except sqlite3.IntegrityError as exc:
            raise ContinuityError('任务或 PR 已绑定') from exc

    async def get(self, task_id, owner_id):
        return await self.db.one('SELECT * FROM continuity_links WHERE task_id=? AND owner_id=?', (task_id, owner_id))

    async def find(self, repo, pr):
        return await self.db.one('SELECT * FROM continuity_links WHERE repo=? AND pr=?', (repo.lower(), int(pr)))

    async def ingest(self, repo, pr, kind, event_id, version, author, body, url='', association='NONE'):
        link = await self.find(repo, pr)
        if not link or not event_id or not version:
            return False
        count = await self.db.execute('''INSERT OR IGNORE INTO continuity_events
            (repo,pr,kind,event_id,version,task_id,author,body,url,association) VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (repo.lower(), int(pr), kind, str(event_id), version, link['task_id'], author, body, url, association))
        return bool(count)

    async def history(self, task_id, owner_id):
        if not await self.get(task_id, owner_id):
            return []
        return await self.db.all('SELECT * FROM continuity_events WHERE task_id=? ORDER BY version, kind, event_id', (task_id,))

    async def enqueue_comment(self, task_id, owner_id, event_id, body):
        link = await self.get(task_id, owner_id)
        if not link or not event_id or not body:
            raise ContinuityError('无法为该任务创建评论')
        key = hashlib.sha256((task_id + '\0' + event_id).encode()).hexdigest()
        marked = f'<!-- continuity-event:{key} -->\n{body}'
        await self.db.execute('INSERT OR IGNORE INTO continuity_outbox(id,task_id,repo,pr,body) VALUES(?,?,?,?,?)',
            (key, task_id, link['repo'], link['pr'], marked))


class ContinuityService:
    """Synchronize trusted feedback for an explicitly associated pull request."""
    def __init__(self, store, github, dispatch, notify=None):
        import asyncio
        self.store = store
        self.github = github
        self.dispatch = dispatch
        self.notify = notify
        self._lock = asyncio.Lock()

    async def sync(self, task_id, owner_id):
        async with self._lock:
            link = await self.store.get(task_id, owner_id)
            if link is None:
                raise ContinuityError('找不到该任务的 PR 关联')
            pr = await self.github.pull_request(link['repo'], link['pr'])
            if pr['state'] == 'open' and (pr['head_repo'] != link['repo'] or pr['branch'] != link['branch']):
                raise ContinuityError('PR 分支来源发生变化，需要人工检查。')
            previous = await self.store.db.one('SELECT * FROM continuity_status WHERE task_id=?', (task_id,))
            if previous and (previous['state'] != pr['state'] or bool(previous['merged']) != pr['merged']) and self.notify:
                await self.notify(link, pr)
            await self.store.db.execute('INSERT OR REPLACE INTO continuity_status VALUES(?,?,?,?)',
                (task_id, pr['state'], int(pr['merged']), pr['head_sha']))
            if pr['state'] != 'open':
                return await self.store.history(task_id, owner_id)
            events = await self.github.feedback(link['repo'], link['pr'])
            pending = await self.store.db.all("SELECT * FROM continuity_outbox WHERE task_id=? AND state='pending'", (task_id,))
            for item in pending:
                marker = f"<!-- continuity-event:{item['id']} -->"
                if not any(marker in event.get('body', '') for event in events):
                    await self.github.comment(item['repo'], item['pr'], item['body'])
                await self.store.db.execute("UPDATE continuity_outbox SET state='sent' WHERE id=?", (item['id'],))
            for event in events:
                if event.get('is_bot') or '<!-- continuity-event:' in event.get('body', '') or not await self.github.authorized(link['repo'], event.get('author', ''), event.get('association', 'NONE')):
                    continue
                await self.store.ingest(link['repo'], link['pr'], event['kind'], event['id'],
                    event['version'], event['author'], event['body'], event.get('url', ''), event.get('association', 'NONE'))
            for event in await self.store.history(task_id, owner_id):
                if event['state'] != 'pending':
                    continue
                if not await self.github.authorized(link['repo'], event['author'], event['association']):
                    continue
                await self.dispatch(link, event)
                await self.store.db.execute('''UPDATE continuity_events SET state='queued'
                    WHERE repo=? AND pr=? AND kind=? AND event_id=? AND version=?''',
                    (event['repo'], event['pr'], event['kind'], event['event_id'], event['version']))
            return await self.store.history(task_id, owner_id)
