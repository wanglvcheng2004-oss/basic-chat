import asyncio
import httpx
import pytest
from backend.agent.github_gateway import GitHubGateway, GitHubError


def test_only_configured_repo_and_paginated_feedback():
    async def run():
        calls = []
        def handle(request):
            calls.append(str(request.url))
            assert request.headers['Authorization'].startswith('Bearer ')
            if request.url.path.endswith('/issues/17/comments'):
                if request.url.params.get('page') == '1':
                    return httpx.Response(200, json=[{'id': 11, 'body': '改为 B', 'updated_at': '2026-11-01T09:00:00Z', 'author_association': 'MEMBER', 'user': {'login': 'alice'}}], headers={'Link': '<https://api.github.com/repos/team/project/issues/17/comments?page=2>; rel="next"'})
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            gateway = GitHubGateway({'team/project': ['alice'], 'team/second': ['bob']}, 'test-secret', client=client)
            events = await gateway.feedback('team/project', 17)
            assert len(events) == 1 and events[0]['body'] == '改为 B'
            assert any('page=2' in call for call in calls)
            with pytest.raises(GitHubError):
                await gateway.feedback('team/other', 17)
    asyncio.run(run())


def test_create_draft_pr_and_comment_use_configured_repo():
    async def run():
        calls = []
        def handle(request):
            calls.append((request.method, request.url.path, request.content))
            if request.url.path.endswith('/pulls'):
                return httpx.Response(201, json={'number': 21, 'html_url': 'https://github.com/team/project/pull/21'})
            return httpx.Response(201, json={'id': 42, 'html_url': 'https://github.com/team/project/pull/21#issuecomment-42'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            gateway = GitHubGateway({'team/project': ['alice'], 'team/second': ['bob']}, 'test-secret', client=client)
            pr = await gateway.create_draft('team/project', 'agent/task-1', 'main', '修复问题', '测试说明')
            assert pr['number'] == 21 and b'"draft":true' in calls[0][2]
            assert (await gateway.comment('team/project', 21, '已更新'))['id'] == 42
            with pytest.raises(GitHubError):
                await gateway.create_draft('other/project', 'agent/task-1', 'main', 'x', 'x')
    asyncio.run(run())


def test_design_maintainers_are_specific_to_each_repository():
    gateway = GitHubGateway({'team/project': ['Alice'], 'team/second': ['Bob']}, 'test-secret')
    assert gateway.is_design_maintainer('team/project', 'alice')
    assert not gateway.is_design_maintainer('team/second', 'alice')
    assert not gateway.is_design_maintainer('team/project', 'bob')
    assert not gateway.is_design_maintainer('team/unknown', 'alice')

def test_collaborator_requires_current_repository_access():
    async def run():
        def handle(request):
            login = request.url.path.split('/')[-2]
            return httpx.Response(200, json={'permission': 'write' if login == 'invited' else 'none'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            gateway = GitHubGateway({'team/project': ['designer'], 'team/second': ['other']}, 'test-secret', client=client)
            assert await gateway.authorized('team/project', 'designer', 'NONE')
            assert await gateway.authorized('team/project', 'invited', 'COLLABORATOR')
            assert not await gateway.authorized('team/project', 'random', 'COLLABORATOR')
            assert not await gateway.authorized('team/project', 'invited', 'NONE')
            assert not await gateway.authorized('team/second', 'designer', 'NONE')
    asyncio.run(run())


def test_advance_branch_never_forces_and_uses_configured_repo():
    async def run():
        import json
        observed = []
        def handle(request):
            observed.append(request)
            return httpx.Response(200, json={'ref':'refs/heads/feature/a'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            gateway = GitHubGateway({'team/project':['designer']}, 'test-secret', client=client)
            await gateway.advance_branch('team/project','feature/a','a'*40)
            assert json.loads(observed[0].content)['force'] is False
            with pytest.raises(GitHubError):
                await gateway.advance_branch('other/project','feature/a','a'*40)
    asyncio.run(run())

def test_project_settings_include_independent_checkout_and_verification():
    gateway = GitHubGateway({'team/project': {'designers':['alice'], 'local_path':'C:/work/a', 'verify':['python','-m','pytest']},
                             'team/second': {'designers':['bob'], 'local_path':'C:/work/b', 'verify':['npm','test']}}, 'test-secret')
    assert gateway.project('team/project')['local_path'] == 'C:/work/a'
    assert gateway.project('team/second')['verify'] == ['npm','test']
    assert not gateway.is_design_maintainer('team/second', 'alice')


def test_missing_collaborator_is_denied_without_blocking_other_comments():
    async def run():
        def handle(request): return httpx.Response(404, json={'message':'Not Found'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            gateway = GitHubGateway({'team/project':[]}, 'test-secret', client=client)
            assert not await gateway.authorized('team/project', 'removed', 'COLLABORATOR')
    asyncio.run(run())
