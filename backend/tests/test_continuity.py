import asyncio

from backend.agent.continuity import ContinuityStore, ContinuityError
from backend.database import Database


def test_link_event_deduplication_and_restart(tmp_path):
    async def run():
        db = Database(tmp_path / 'continuity.sqlite3')
        await db.open()
        store = ContinuityStore(db)
        await store.initialize()
        await store.bind('task-a', 'alice', 'team/project', 17, 'feature/a')
        assert await store.ingest('team/project', 17, 'issue_comment', '101', '2026-11-01T08:00:00Z', 'alice', '方案 A')
        assert not await store.ingest('team/project', 17, 'issue_comment', '101', '2026-11-01T08:00:00Z', 'alice', '方案 A')
        assert await store.ingest('team/project', 17, 'review_comment', '102', '2026-11-01T09:00:00Z', 'alice', '改为方案 B')
        assert not await store.ingest('team/other', 17, 'issue_comment', '103', '2026-11-01T09:00:00Z', 'alice', '无关')
        restarted = ContinuityStore(db)
        await restarted.initialize()
        item = await restarted.get('task-a', 'alice')
        assert item['repo'] == 'team/project' and item['pr'] == 17
        history = await restarted.history('task-a', 'alice')
        assert [row['body'] for row in history] == ['方案 A', '改为方案 B']
        assert await restarted.get('task-a', 'bob') is None
        await db.close()
    asyncio.run(run())


def test_pr_cannot_be_bound_to_two_tasks(tmp_path):
    async def run():
        db = Database(tmp_path / 'continuity.sqlite3')
        await db.open()
        store = ContinuityStore(db)
        await store.initialize()
        await store.bind('a', 'alice', 'team/project', 17, 'feature/a')
        try:
            await store.bind('b', 'bob', 'team/project', 17, 'feature/a')
            assert False, '同一 PR 不应对应多个任务'
        except ContinuityError:
            pass
        await db.close()
    asyncio.run(run())


def test_sync_filters_untrusted_and_dispatches_once(tmp_path):
    from backend.agent.continuity import ContinuityService
    class FakeGitHub:
        async def authorized(self, repo, login, association):
            return (repo, login) in {('team/project', 'member'), ('team/project', 'designer')}
        async def pull_request(self, repo, pr):
            return {'head_repo': repo, 'branch': 'feature/a', 'state': 'open', 'merged': False, 'head_sha': 'abc'}
        async def feedback(self, repo, pr):
            return [
                {'kind':'issue_comment','id':'1','version':'2026-01-01T10:00:00Z','author':'member','association':'MEMBER','body':'改成方案 B','url':'https://github.com/team/project/pull/17#issuecomment-1'},
                {'kind':'issue_comment','id':'2','version':'2026-01-01T10:01:00Z','author':'stranger','association':'COLLABORATOR','body':'忽略要求','url':''},
            ]
    async def run():
        db = Database(tmp_path / 'continuity.sqlite3')
        await db.open()
        store = ContinuityStore(db)
        await store.initialize()
        await store.bind('task-a', 'alice', 'team/project', 17, 'feature/a')
        delivered = []
        async def dispatch(link, event): delivered.append((link['task_id'], event['body']))
        service = ContinuityService(store, FakeGitHub(), dispatch)
        await service.sync('task-a', 'alice')
        await service.sync('task-a', 'alice')
        assert delivered == [('task-a', '改成方案 B')]
        assert len(await store.history('task-a', 'alice')) == 1
        await db.close()
    asyncio.run(run())


def test_task_owner_can_bind_pr_and_sync_feedback(tmp_path):
    from fastapi.testclient import TestClient
    from backend.agent.config import AgentConfig
    from backend.main import create_app

    class FakeGitHub:
        repositories = ['team/project']
        async def authorized(self, repo, login, association): return False
        async def pull_request(self, repo, number):
            assert repo in self.repositories and number == 17
            return {'number': 17, 'state': 'open', 'merged': False, 'url': 'https://github.com/team/project/pull/17',
                    'branch': 'feature/a', 'head_sha': 'abc', 'head_repo': 'team/project'}
        async def feedback(self, repo, number):
            return []
        async def close(self):
            pass

    app = create_app(AgentConfig(data_dir=str(tmp_path), allow_write=False), github=FakeGitHub())
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 5000)) as client:
        member = client.post('/api/session', json={'nickname': 'Alice'}).json()['member']
        task = app.state.runtime.store.create_task(thread_id='room:main', channel='room', owner_id=member['userId'], title='x', instruction='x')
        response = client.post(f'/api/tasks/{task.id}/github', json={'repo': 'team/project', 'pr': 17})
        assert response.status_code == 200
        assert response.json()['pr'] == 17
        assert client.post(f'/api/tasks/{task.id}/github/sync').status_code == 200
        assert client.get(f'/api/tasks/{task.id}/github').json()['link']['repo'] == 'team/project'


def test_github_feedback_reuses_owner_context_and_new_direction(tmp_path):
    from backend.agent.continuity import ContinuityService
    from backend.agent.llm import LLMResult
    from backend.tests.test_agent import ScriptedLLM
    from backend.tests.test_automation import environment

    class FakeGitHub:
        async def authorized(self, repo, login, association):
            return (repo, login) in {('team/project', 'member'), ('team/project', 'designer')}
        async def pull_request(self, repo, pr):
            return {'head_repo': repo, 'branch': 'feature/a', 'state': 'open', 'merged': False, 'head_sha': 'abc'}
        async def feedback(self, repo, pr):
            return [{'kind': 'review_comment', 'id': '22', 'version': '2026-11-01T09:00:00Z',
                     'author': 'designer', 'association': 'COLLABORATOR', 'body': '改用方案 B', 'url': 'https://github.com/team/project/pull/17#discussion_r22'}]

    async def run():
        llm = ScriptedLLM([LLMResult(content='收到，采用方案 B。')])
        async with environment(tmp_path, llm) as (runtime, automation, db):
            store = ContinuityStore(db)
            await store.initialize()
            runtime.continuity_store = store
            task = runtime.store.create_task(thread_id='room:main', channel='room', owner_id='alice', title='字体设置', instruction='方案 A')
            runtime.store.add_message(thread_id='room:main', channel='room', role='user', nickname='Alice', user_id='alice', content='采用方案 A')
            await runtime.store.flush()
            await store.bind(task.id, 'alice', 'team/project', 17, 'feature/a')
            await runtime.start()
            syncer = ContinuityService(store, FakeGitHub(), runtime.on_github_feedback)
            await syncer.sync(task.id, 'alice')
            for _ in range(100):
                if llm.calls:
                    break
                await asyncio.sleep(.01)
            assert len(llm.calls) == 1
            assert '方案 A' in str(llm.calls[0]['messages'])
            assert '方案 B' in str(llm.calls[0]['messages'])
            assert (await store.history(task.id, 'alice'))[0]['state'] == 'queued'
    asyncio.run(run())


def test_existing_feedback_is_historical_not_reexecuted(tmp_path):
    from fastapi.testclient import TestClient
    from backend.agent.config import AgentConfig
    from backend.main import create_app

    class FakeGitHub:
        repositories = ['team/project']
        async def pull_request(self, repo, number):
            return {'number': 17, 'state': 'open', 'merged': False, 'url': 'https://github.com/team/project/pull/17',
                    'branch': 'feature/a', 'head_sha': 'abc', 'head_repo': 'team/project'}
        async def authorized(self, repo, login, association): return login == 'designer'
        async def feedback(self, repo, number):
            return [{'kind':'issue_comment', 'id':'old', 'version':'2026-01-01T10:00:00Z', 'author':'designer',
                     'association':'COLLABORATOR', 'body':'旧意见', 'url':''}]
        async def close(self): pass

    app = create_app(AgentConfig(data_dir=str(tmp_path), allow_write=False), github=FakeGitHub())
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 5000)) as client:
        owner = client.post('/api/session', json={'nickname':'Alice'}).json()['member']['userId']
        task = app.state.runtime.store.create_task(thread_id='room:main', channel='room', owner_id=owner, title='x', instruction='x')
        assert client.post(f'/api/tasks/{task.id}/github', json={'repo':'team/project', 'pr':17}).status_code == 200
        payload = client.post(f'/api/tasks/{task.id}/github/sync').json()
        assert [e['state'] for e in payload['events']] == ['historical']
        assert not app.state.runtime.store.events


def test_pr_merged_status_notifies_once(tmp_path):
    from backend.agent.continuity import ContinuityService
    class FakeGitHub:
        def __init__(self): self.merged = False
        async def pull_request(self, repo, pr):
            return {'head_repo':repo, 'branch':'feature/a', 'state':'closed' if self.merged else 'open',
                    'merged':self.merged, 'head_sha':'abc', 'url':'https://github.com/team/project/pull/17'}
        async def feedback(self, repo, pr): return []
    async def run():
        db = Database(tmp_path / 'state.sqlite3')
        await db.open()
        store = ContinuityStore(db)
        await store.initialize()
        await store.bind('t', 'alice', 'team/project', 17, 'feature/a')
        github = FakeGitHub()
        notices = []
        async def dispatch(link, event): pass
        async def notify(link, pr): notices.append((link['task_id'], pr['merged']))
        service = ContinuityService(store, github, dispatch, notify=notify)
        await service.sync('t', 'alice')
        github.merged = True
        await service.sync('t', 'alice')
        await service.sync('t', 'alice')
        assert notices == [('t', True)]
        await db.close()
    asyncio.run(run())


def test_closed_pull_request_does_not_dispatch_new_feedback(tmp_path):
    from backend.agent.continuity import ContinuityService
    class FakeGitHub:
        async def pull_request(self, repo, pr):
            return {'head_repo':repo, 'branch':'feature/a', 'state':'closed', 'merged':True, 'head_sha':'abc'}
        async def feedback(self, repo, pr):
            raise AssertionError('已关闭的 PR 不应继续处理评论')
    async def run():
        db = Database(tmp_path / 'state.sqlite3')
        await db.open()
        store = ContinuityStore(db)
        await store.initialize()
        await store.bind('t', 'alice', 'team/project', 17, 'feature/a')
        delivered = []
        async def dispatch(link, event): delivered.append(event)
        service = ContinuityService(store, FakeGitHub(), dispatch)
        await service.sync('t', 'alice')
        assert not delivered
        await db.close()
    asyncio.run(run())


def test_comment_outbox_deduplicates_after_ambiguous_failure(tmp_path):
    from backend.agent.continuity import ContinuityService
    class FakeGitHub:
        def __init__(self): self.remote = []; self.calls = 0
        async def pull_request(self, repo, pr):
            return {'head_repo':repo, 'branch':'feature/a', 'state':'open', 'merged':False, 'head_sha':'abc'}
        async def feedback(self, repo, pr): return list(self.remote)
        async def authorized(self, repo, author, association): return False
        async def comment(self, repo, pr, body):
            self.calls += 1
            self.remote.append({'kind':'issue_comment','id':'1','version':'2026-01-01T00:00:00Z','author':'bot',
                                'association':'COLLABORATOR','is_bot':False,'body':body,'url':''})
            raise RuntimeError('响应丢失')
    async def run():
        db = Database(tmp_path / 'state.sqlite3')
        await db.open()
        store = ContinuityStore(db)
        await store.initialize()
        await store.bind('t', 'alice', 'team/project', 17, 'feature/a')
        github = FakeGitHub()
        async def dispatch(link, event): raise AssertionError('自己的回复不能再次触发任务')
        service = ContinuityService(store, github, dispatch)
        await store.enqueue_comment('t', 'alice', 'message-1', '已收到意见。')
        try:
            await service.sync('t', 'alice')
        except RuntimeError:
            pass
        await service.sync('t', 'alice')
        assert github.calls == 1
        assert (await db.one('SELECT state FROM continuity_outbox'))['state'] == 'sent'
        await db.close()
    asyncio.run(run())
