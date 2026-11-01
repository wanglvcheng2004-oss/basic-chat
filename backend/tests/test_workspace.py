import asyncio
import subprocess
from pathlib import Path
import pytest
from backend.agent.workspace import GitWorkspace, WorkspaceError


def git(cwd, *args):
    return subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def test_workspace_isolated_from_uncommitted_source(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    git(source, 'init')
    (source / 'file.txt').write_text('base', encoding='utf-8')
    git(source, 'add', 'file.txt')
    git(source, 'commit', '-m', 'base')
    head = git(source, 'rev-parse', 'HEAD')
    (source / 'file.txt').write_text('user changes', encoding='utf-8')
    workspace = GitWorkspace(source, tmp_path / 'jobs')
    path = workspace.create('task-123', head)
    assert (path / 'file.txt').read_text() == 'base'
    (path / 'file.txt').write_text('agent changes', encoding='utf-8')
    assert (source / 'file.txt').read_text() == 'user changes'
    assert workspace.changed_paths(path) == ['file.txt']
    with pytest.raises(WorkspaceError):
        workspace.create('task-123', head)


def test_workspace_rejects_unknown_base(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    git(source, 'init')
    workspace = GitWorkspace(source, tmp_path / 'jobs')
    with pytest.raises(WorkspaceError):
        workspace.create('task-123', 'not-a-commit')


def test_tools_use_task_workspace_and_block_traversal(tmp_path):
    import json
    from backend.agent.store import AgentStore
    from backend.agent.tools import execute_tool
    root = tmp_path / 'workspace'
    root.mkdir()
    original = tmp_path / 'outside.txt'
    original.write_text('original', encoding='utf-8')
    task = AgentStore().create_task(thread_id='room:main', channel='room', owner_id='alice', title='x', instruction='x', kind='continuity_edit')
    result = json.loads(asyncio.run(execute_tool('write_file', {'path': 'inside.txt', 'content': 'changed'},
        store=AgentStore(), task=task, allow_write=True, workspace_root=root)))
    assert result['ok'] and (root / 'inside.txt').read_text(encoding='utf-8') == 'changed'
    blocked = json.loads(asyncio.run(execute_tool('write_file', {'path': '../outside.txt', 'content': 'bad'},
        store=AgentStore(), task=task, allow_write=True, workspace_root=root)))
    assert 'error' in blocked and original.read_text() == 'original'


def test_workspace_commits_only_its_own_changes(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    git(source, 'init')
    (source / 'file.txt').write_text('base', encoding='utf-8')
    git(source, 'add', 'file.txt')
    git(source, 'commit', '-m', 'base')
    head = git(source, 'rev-parse', 'HEAD')
    workspace = GitWorkspace(source, tmp_path / 'jobs')
    path = workspace.create('task-123', head)
    (path / 'file.txt').write_text('fixed', encoding='utf-8')
    result = workspace.commit(path, '修复问题')
    assert result != head
    assert git(path, 'status', '--porcelain') == ''
    assert (source / 'file.txt').read_text(encoding='utf-8') == 'base'
    assert git(source, 'rev-parse', 'HEAD') == head


def test_worker_writes_only_in_provided_continuity_workspace(tmp_path):
    import json
    from backend.agent.config import AgentConfig
    from backend.agent.llm import LLMResult, ToolCall
    from backend.agent.runtime import AgentRuntime
    from backend.agent.types import Trigger
    from backend.agent.worker import Worker
    from backend.tests.test_agent import FakeManager, ScriptedLLM
    async def run():
        llm = ScriptedLLM([LLMResult(tool_calls=[ToolCall(id='c1', name='write_file', arguments=json.dumps({'path':'change.txt','content':'fix'}))]), LLMResult(content='完成')])
        runtime = AgentRuntime(AgentConfig(allow_write=True, github_write_enabled=True), FakeManager(), llm=llm)
        async def simulated_publish(task, content): return content
        runtime._publish_continuity = simulated_publish
        task = runtime.store.create_task(thread_id='room:main', channel='room', owner_id='alice', title='fix', instruction='fix', kind='continuity_edit')
        trigger = Trigger(id='t', thread_id='room:main', channel='room', owner_id='alice', owner_name='Alice', content='fix')
        worker = Worker(runtime.config, llm, runtime.store, runtime, task, trigger, workspace_root=tmp_path)
        await worker.run()
        assert (tmp_path / 'change.txt').read_text(encoding='utf-8') == 'fix'
        assert not (Path(__file__).resolve().parents[2] / 'change.txt').exists()
    asyncio.run(run())


def test_publisher_refuses_stale_pr_before_remote_write(tmp_path):
    from backend.agent.publisher import ProjectPublisher, PublishError
    class FakeGitHub:
        def __init__(self): self.writes = 0
        async def pull_request(self, repo, pr):
            return {'head_sha':'b' * 40, 'branch':'feature/a', 'head_repo':repo, 'state':'open', 'merged':False}
        async def create_blob(self, *args): self.writes += 1
    class FakeWorkspace:
        def changed_paths(self, path): return ['file.txt']
    async def run():
        gateway = FakeGitHub()
        publisher = ProjectPublisher(gateway)
        with pytest.raises(PublishError):
            await publisher.update_existing({'repo':'team/project','pr':1,'branch':'feature/a','head_sha':'a'*40}, FakeWorkspace(), tmp_path)
        assert gateway.writes == 0
    asyncio.run(run())


def test_publisher_verifies_before_updating_branch(tmp_path):
    from backend.agent.publisher import ProjectPublisher
    class FakeGitHub:
        def __init__(self): self.calls = []
        async def pull_request(self, repo, pr):
            return {'head_sha':'a'*40,'branch':'feature/a','head_repo':repo,'state':'open','merged':False}
        async def commit_tree(self, repo, sha): self.calls.append('tree'); return 'tree-sha'
        async def create_blob(self, repo, content): self.calls.append('blob'); return 'blob-sha'
        async def create_tree(self, repo, base, entries): self.calls.append('create_tree'); return 'new-tree'
        async def create_commit(self, repo, tree, parent, message): self.calls.append('commit'); return 'new-commit'
        async def advance_branch(self, repo, branch, sha): self.calls.append('advance')
    class FakeWorkspace:
        def changed_paths(self, path): return ['file.txt']
        def file_mode(self, path, name): return '100644'
    async def run():
        (tmp_path / 'file.txt').write_text('fixed', encoding='utf-8')
        gateway = FakeGitHub()
        async def verify(path):
            assert not gateway.calls
            return True
        result = await ProjectPublisher(gateway).update_existing(
            {'repo':'team/project','pr':1,'branch':'feature/a','head_sha':'a'*40}, FakeWorkspace(), tmp_path, verify=verify)
        assert result == 'new-commit'
        assert gateway.calls == ['tree','blob','create_tree','commit','advance']
    asyncio.run(run())


def test_workspace_verification_fails_closed_without_command(tmp_path):
    import sys
    from backend.agent.workspace import verify_workspace
    async def run():
        assert not await verify_workspace(tmp_path, [])
        assert await verify_workspace(tmp_path, [sys.executable, '-c', 'raise SystemExit(0)'])
        assert not await verify_workspace(tmp_path, [sys.executable, '-c', 'raise SystemExit(1)'])
    asyncio.run(run())


def test_create_new_draft_only_after_validation(tmp_path):
    from backend.agent.publisher import ProjectPublisher
    class FakeGitHub:
        def __init__(self): self.calls = []
        async def branch_head(self, repo, branch): return 'a'*40
        async def commit_tree(self, repo, sha): self.calls.append('tree'); return 'tree-sha'
        async def create_blob(self, repo, content): self.calls.append('blob'); return 'blob-sha'
        async def create_tree(self, repo, base, entries): self.calls.append('create_tree'); return 'new-tree'
        async def create_commit(self, repo, tree, parent, message): self.calls.append('commit'); return 'b'*40
        async def create_branch(self, repo, branch, sha): self.calls.append('branch')
        async def create_draft(self, repo, branch, base, title, body): self.calls.append('draft'); return {'number':21,'url':'https://github.com/team/project/pull/21'}
    class FakeWorkspace:
        def changed_paths(self, path): return ['file.txt']
        def file_mode(self, path, name): return '100644'
    async def run():
        (tmp_path / 'file.txt').write_text('fixed', encoding='utf-8')
        gateway = FakeGitHub()
        async def verify(path): return True
        result = await ProjectPublisher(gateway).create_new('team/project', 'main', 'a'*40, 'agent/task-1',
            'Fix issue', FakeWorkspace(), tmp_path, verify=verify)
        assert result['number'] == 21
        assert gateway.calls == ['tree','blob','create_tree','commit','branch','draft']
    asyncio.run(run())


def test_auto_issue_creates_verified_draft_in_isolated_project(tmp_path, monkeypatch):
    import json
    import sys
    from backend.agent.automation import Automation
    from backend.agent.config import AgentConfig
    from backend.agent.continuity import ContinuityStore
    from backend.agent.llm import LLMResult, ToolCall
    from backend.agent.runtime import AgentRuntime
    from backend.agent.store import AgentStore
    from backend.database import Database
    from backend.tests.test_agent import FakeManager, ScriptedLLM
    from backend.tests.test_automation import message

    source = tmp_path / 'source'
    source.mkdir()
    git(source, 'init')
    (source / 'file.txt').write_text('base', encoding='utf-8')
    git(source, 'add', 'file.txt')
    git(source, 'commit', '-m', 'base')
    base = git(source, 'rev-parse', 'HEAD')

    class FakeGitHub:
        repositories = ['team/project']
        _token = 'test-secret'
        def __init__(self): self.calls = []
        def project(self, repo): return {'local_path':str(source), 'verify':[sys.executable,'-c','raise SystemExit(0)']}
        async def default_branch(self, repo): return 'main'
        async def branch_head(self, repo, branch): return base
        async def commit_tree(self, repo, sha): self.calls.append('tree'); return 'tree-sha'
        async def create_blob(self, repo, data): self.calls.append('blob'); return 'blob-sha'
        async def create_tree(self, repo, tree, entries): self.calls.append('tree-create'); return 'new-tree'
        async def create_commit(self, repo, tree, parent, message): self.calls.append('commit'); return 'b'*40
        async def create_branch(self, repo, branch, sha): self.calls.append('branch')
        async def create_draft(self, repo, branch, base_branch, title, body):
            self.calls.append('draft'); return {'number':21,'url':'https://github.com/team/project/pull/21'}

    def fake_remote(self, task_id, repository, branch, expected_head, token):
        return self.create(task_id, expected_head)
    monkeypatch.setattr(GitWorkspace, 'create_remote', fake_remote)

    async def run():
        db = Database(tmp_path / 'state' / 'db.sqlite3')
        await db.open()
        llm = ScriptedLLM([
            LLMResult(content='{"reply":true,"needsCodeChange":true}'),
            LLMResult(tool_calls=[ToolCall(id='c1', name='write_file', arguments=json.dumps({'path':'file.txt','content':'fixed'}))]),
            LLMResult(content='已修改并验证。'),
        ])
        config = AgentConfig(data_dir=str(tmp_path / 'state'), allow_write=True, github_write_enabled=True)
        runtime = AgentRuntime(config, FakeManager(), store=AgentStore(db=db), llm=llm)
        runtime.github_gateway = FakeGitHub()
        runtime.continuity_store = ContinuityStore(db)
        await runtime.continuity_store.initialize()
        service = Automation(runtime, db)
        runtime.automation = service
        await service.initialize()
        await service.configure({'autoReply':True}, 'admin')
        original = message(runtime, '这个项目出现问题，请修复代码', 1793520000, owner='alice')
        await service.observe(original)
        await asyncio.gather(*list(service._decisions))
        await asyncio.gather(*list(runtime._workers.values()))
        replies = [p for p in runtime.manager.broadcasted if p.get('type') == 'agent.done']
        assert any('pull/21' in p['content'] and p['replyToUserId'] == 'alice' for p in replies)
        assert runtime.github_gateway.calls == ['tree','blob','tree-create','commit','branch','draft']
        assert (source / 'file.txt').read_text(encoding='utf-8') == 'base'
        task = next(iter(runtime.store.tasks.values()))
        assert (await runtime.continuity_store.get(task.id, 'alice'))['pr'] == 21
        await runtime.stop()
        await db.close()
    asyncio.run(run())


def test_github_feedback_updates_same_pr_after_verified_change(tmp_path, monkeypatch):
    import json
    import sys
    from backend.agent.automation import Automation
    from backend.agent.config import AgentConfig
    from backend.agent.continuity import ContinuityStore, ContinuityService
    from backend.agent.llm import LLMResult, ToolCall
    from backend.agent.runtime import AgentRuntime
    from backend.agent.store import AgentStore
    from backend.database import Database
    from backend.tests.test_agent import FakeManager, ScriptedLLM

    source = tmp_path / 'source'
    source.mkdir()
    git(source, 'init')
    (source / 'file.txt').write_text('A', encoding='utf-8')
    git(source, 'add', 'file.txt')
    git(source, 'commit', '-m', 'base')
    base = git(source, 'rev-parse', 'HEAD')

    class FakeGitHub:
        repositories = ['team/project']
        _token = 'test-secret'
        def __init__(self): self.calls = []
        def project(self, repo): return {'local_path':str(source), 'verify':[sys.executable,'-c','raise SystemExit(0)']}
        async def pull_request(self, repo, number):
            return {'state':'open','merged':False,'head_repo':repo,'branch':'feature/a','head_sha':base}
        async def authorized(self, repo, login, association): return login == 'designer'
        async def feedback(self, repo, number):
            return [{'kind':'issue_comment','id':'22','version':'2026-11-01T09:00:00Z','author':'designer',
                     'association':'COLLABORATOR','body':'改用方案 B','url':'https://github.com/team/project/pull/17#issuecomment-22'}]
        async def commit_tree(self, repo, sha): self.calls.append('tree'); return 'tree'
        async def create_blob(self, repo, data): self.calls.append('blob'); return 'blob'
        async def create_tree(self, repo, base_tree, entries): self.calls.append('tree-create'); return 'new-tree'
        async def create_commit(self, repo, tree, parent, message): self.calls.append('commit'); return 'b'*40
        async def advance_branch(self, repo, branch, sha): self.calls.append('advance')
        async def comment(self, repo, pr, body): self.calls.append('comment'); return {'id':1,'url':''}

    def fake_remote(self, task_id, repository, branch, expected_head, token):
        return self.create(task_id, expected_head)
    monkeypatch.setattr(GitWorkspace, 'create_remote', fake_remote)

    async def run():
        db = Database(tmp_path / 'state' / 'db.sqlite3')
        await db.open()
        llm = ScriptedLLM([
            LLMResult(tool_calls=[ToolCall(id='dispatch', name='dispatch_task', arguments=json.dumps({'title':'采用 B','instruction':'改用方案 B'}))]),
            LLMResult(tool_calls=[ToolCall(id='write', name='write_file', arguments=json.dumps({'path':'file.txt','content':'B'}))]),
            LLMResult(content='已改用 B。'),
        ])
        config = AgentConfig(data_dir=str(tmp_path / 'state'), allow_write=True, github_write_enabled=True)
        runtime = AgentRuntime(config, FakeManager(), store=AgentStore(db=db), llm=llm)
        runtime.github_gateway = FakeGitHub()
        runtime.continuity_store = ContinuityStore(db)
        await runtime.continuity_store.initialize()
        automation = Automation(runtime, db)
        runtime.automation = automation
        await automation.initialize()
        owner_task = runtime.store.create_task(thread_id='room:main', channel='room', owner_id='alice', title='采用 A', instruction='方案 A')
        runtime.store.add_message(thread_id='room:main', channel='room', role='user', nickname='Alice', user_id='alice', content='采用方案 A')
        await runtime.store.flush()
        await runtime.continuity_store.bind(owner_task.id, 'alice', 'team/project', 17, 'feature/a', base)
        await runtime.start()
        service = ContinuityService(runtime.continuity_store, runtime.github_gateway, runtime.on_github_feedback)
        await service.sync(owner_task.id, 'alice')
        for _ in range(300):
            if await db.one('SELECT id FROM continuity_outbox WHERE task_id=?', (owner_task.id,)):
                break
            await asyncio.sleep(.01)
        await service.sync(owner_task.id, 'alice')
        assert runtime.github_gateway.calls == ['tree','blob','tree-create','commit','advance','comment']
        assert (source / 'file.txt').read_text(encoding='utf-8') == 'A'
        assert (await runtime.continuity_store.get(owner_task.id, 'alice'))['head_sha'] == 'b'*40
        assert '方案 A' in str(llm.calls[0]['messages']) and '方案 B' in str(llm.calls[0]['messages'])
        await service.sync(owner_task.id, 'alice')
        assert runtime.github_gateway.calls.count('advance') == 1
        await runtime.stop()
        await db.close()
    asyncio.run(run())


def test_room_followup_continues_linked_pr(tmp_path, monkeypatch):
    import json
    import sys
    from backend.agent.automation import Automation
    from backend.agent.config import AgentConfig
    from backend.agent.continuity import ContinuityStore
    from backend.agent.llm import LLMResult, ToolCall
    from backend.agent.runtime import AgentRuntime
    from backend.agent.store import AgentStore
    from backend.database import Database
    from backend.tests.test_agent import FakeClient, FakeManager, ScriptedLLM

    source = tmp_path / 'source'
    source.mkdir()
    git(source, 'init')
    (source / 'file.txt').write_text('A', encoding='utf-8')
    git(source, 'add', 'file.txt')
    git(source, 'commit', '-m', 'base')
    base = git(source, 'rev-parse', 'HEAD')
    class FakeGitHub:
        _token = 'test-secret'
        def __init__(self): self.calls = []
        def project(self, repo): return {'local_path':str(source), 'verify':[sys.executable,'-c','raise SystemExit(0)']}
        async def pull_request(self, repo, number): return {'state':'open','merged':False,'head_repo':repo,'branch':'feature/a','head_sha':base}
        async def commit_tree(self, repo, sha): self.calls.append('tree'); return 'tree'
        async def create_blob(self, repo, data): self.calls.append('blob'); return 'blob'
        async def create_tree(self, repo, tree, entries): self.calls.append('tree-create'); return 'new-tree'
        async def create_commit(self, repo, tree, parent, message): self.calls.append('commit'); return 'b'*40
        async def advance_branch(self, repo, branch, sha): self.calls.append('advance')
        async def comment(self, repo, pr, body): self.calls.append('comment'); return {'id':1,'url':''}
    monkeypatch.setattr(GitWorkspace, 'create_remote', lambda self, task_id, repo, branch, head, token: self.create(task_id, head))
    FakeGitHub._token = 'test-secret'

    async def run():
        db = Database(tmp_path / 'state' / 'db.sqlite3')
        await db.open()
        llm = ScriptedLLM([
            LLMResult(tool_calls=[ToolCall(id='dispatch', name='dispatch_task', arguments=json.dumps({'title':'采用 C','instruction':'改用方案 C'}))]),
            LLMResult(tool_calls=[ToolCall(id='write', name='write_file', arguments=json.dumps({'path':'file.txt','content':'C'}))]),
            LLMResult(content='已改用 C。'),
        ])
        runtime = AgentRuntime(AgentConfig(data_dir=str(tmp_path / 'state'), allow_write=True, github_write_enabled=True), FakeManager(), store=AgentStore(db=db), llm=llm)
        runtime.github_gateway = FakeGitHub()
        runtime.continuity_store = ContinuityStore(db)
        await runtime.continuity_store.initialize()
        automation = Automation(runtime, db)
        runtime.automation = automation
        await automation.initialize()
        original = runtime.store.create_task(thread_id='room-member:alice', channel='room', owner_id='alice', title='采用 A', instruction='A', kind='continuity_edit')
        runtime.store.update_task(original, status='done', continuity_id=original.id)
        await runtime.store.flush()
        await runtime.continuity_store.bind(original.id, 'alice', 'team/project', 17, 'feature/a', base)
        await runtime.start()
        await runtime.on_followup(FakeClient('alice', 'Alice'), original.id, '再改为方案 C')
        for _ in range(300):
            if await db.one('SELECT id FROM continuity_outbox WHERE task_id=?', (original.id,)): break
            await asyncio.sleep(.01)
        assert 'advance' in runtime.github_gateway.calls
        assert (await db.one('SELECT state FROM continuity_outbox WHERE task_id=?', (original.id,)))['state'] == 'pending'
        assert (await runtime.continuity_store.get(original.id, 'alice'))['head_sha'] == 'b'*40
        assert (source / 'file.txt').read_text(encoding='utf-8') == 'A'
        await runtime.stop()
        await db.close()
    asyncio.run(run())
