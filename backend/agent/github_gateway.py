"""Minimal GitHub API adapter with a fixed API origin and repository allowlist."""
from __future__ import annotations

import re
import httpx


class GitHubError(RuntimeError):
    def __init__(self, message, *, status=None):
        super().__init__(message)
        self.status = status


class GitHubGateway:
    def __init__(self, repositories, token, *, client=None):
        if not isinstance(repositories, dict) or not repositories:
            raise GitHubError('未配置允许的 GitHub 仓库')
        self._designers = {}
        self._projects = {}
        for repo, value in repositories.items():
            settings = value if isinstance(value, dict) else {"designers": value}
            maintainers = settings.get("designers", [])
            if not isinstance(repo, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
                raise GitHubError('GitHub 仓库名称无效')
            if not isinstance(maintainers, list) or any(not isinstance(login, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,39}', login) for login in maintainers):
                raise GitHubError('项目设计维护者列表无效')
            local_path = settings.get('local_path', '')
            verify = settings.get('verify', [])
            if not isinstance(local_path, str) or not isinstance(verify, list) or any(not isinstance(arg, str) or not arg for arg in verify):
                raise GitHubError('项目目录或验证命令配置无效')
            self._designers[repo.lower()] = {login.lower() for login in maintainers}
            self._projects[repo.lower()] = {'local_path': local_path, 'verify': verify}
        if not token:
            raise GitHubError('GitHub 凭据未配置')
        self.repositories = sorted(self._designers)
        self._token = token
        self._client = client or httpx.AsyncClient(timeout=20, follow_redirects=False)
        self._owned = client is None

    def _repo(self, repository):
        repo = (repository or '').lower()
        if repo not in self._designers:
            raise GitHubError('仓库不在允许范围内')
        return repo

    def project(self, repository):
        return dict(self._projects[self._repo(repository)])

    def is_design_maintainer(self, repository, login):
        return (login or '').lower() in self._designers.get((repository or '').lower(), set())

    async def authorized(self, repository, login, association):
        repo = self._repo(repository)
        if not login or not re.fullmatch(r'[A-Za-z0-9-]{1,39}', login):
            return False
        if self.is_design_maintainer(repo, login):
            return True
        if association not in {'OWNER', 'MEMBER', 'COLLABORATOR'}:
            return False
        try:
            result = await self._get(f'/repos/{repo}/collaborators/{login}/permission')
        except GitHubError as exc:
            if exc.status == 404:
                return False
            raise
        return result.json().get('permission') in {'read', 'triage', 'write', 'maintain', 'admin'}

    async def close(self):
        if self._owned:
            await self._client.aclose()

    async def _get(self, path, params=None):
        try:
            response = await self._client.get('https://api.github.com' + path, params=params,
                headers={'Authorization': 'Bearer ' + self._token, 'Accept': 'application/vnd.github+json',
                         'X-GitHub-Api-Version': '2022-11-28'})
        except httpx.HTTPError as exc:
            raise GitHubError('GitHub 连接失败') from exc
        if response.status_code != 200:
            raise GitHubError(f'GitHub 请求失败（HTTP {response.status_code}）', status=response.status_code)
        return response

    async def _post(self, path, payload):
        try:
            response = await self._client.post('https://api.github.com' + path, json=payload,
                headers={'Authorization': 'Bearer ' + self._token, 'Accept': 'application/vnd.github+json',
                         'X-GitHub-Api-Version': '2022-11-28'})
        except httpx.HTTPError as exc:
            raise GitHubError('GitHub 连接失败') from exc
        if response.status_code != 201:
            raise GitHubError(f'GitHub 请求失败（HTTP {response.status_code}）', status=response.status_code)
        return response.json()

    async def create_draft(self, repository, branch, base, title, body):
        repo = self._repo(repository)
        if not branch or not base or not title:
            raise GitHubError('PR 标题、源分支和目标分支不能为空')
        result = await self._post(f'/repos/{repo}/pulls',
            {'head': branch, 'base': base, 'title': title[:200], 'body': body, 'draft': True})
        return {'number': result['number'], 'url': result['html_url']}

    async def comment(self, repository, number, body):
        repo = self._repo(repository)
        if not body.strip():
            raise GitHubError('评论不能为空')
        result = await self._post(f'/repos/{repo}/issues/{int(number)}/comments', {'body': body})
        return {'id': result['id'], 'url': result['html_url']}

    async def default_branch(self, repository):
        repo = self._repo(repository)
        return (await self._get(f'/repos/{repo}')).json()['default_branch']

    async def branch_head(self, repository, branch):
        repo = self._repo(repository)
        if not re.fullmatch(r'[A-Za-z0-9._/-]{1,200}', branch or '') or '..' in branch or branch.startswith('/'):
            raise GitHubError('分支名称无效')
        return (await self._get(f'/repos/{repo}/git/ref/heads/{branch}')).json()['object']['sha']

    async def create_branch(self, repository, branch, commit):
        repo = self._repo(repository)
        if not re.fullmatch(r'agent/[A-Za-z0-9_-]{1,80}', branch or '') or not re.fullmatch(r'[0-9a-fA-F]{40}', commit or ''):
            raise GitHubError('任务分支或提交无效')
        return await self._post(f'/repos/{repo}/git/refs', {'ref': 'refs/heads/' + branch, 'sha': commit})

    async def commit_tree(self, repository, commit):
        repo = self._repo(repository)
        if not re.fullmatch(r'[0-9a-fA-F]{40}', commit or ''):
            raise GitHubError('提交标识无效')
        result = (await self._get(f'/repos/{repo}/git/commits/{commit}')).json()
        return result['tree']['sha']

    async def create_blob(self, repository, content):
        repo = self._repo(repository)
        return (await self._post(f'/repos/{repo}/git/blobs', {'content': content, 'encoding': 'base64'}))['sha']

    async def create_tree(self, repository, base, entries):
        repo = self._repo(repository)
        return (await self._post(f'/repos/{repo}/git/trees', {'base_tree': base, 'tree': entries}))['sha']

    async def create_commit(self, repository, tree, parent, message):
        repo = self._repo(repository)
        return (await self._post(f'/repos/{repo}/git/commits', {'message': message, 'tree': tree, 'parents': [parent]}))['sha']

    async def advance_branch(self, repository, branch, commit):
        repo = self._repo(repository)
        if not re.fullmatch(r'[A-Za-z0-9._/-]{1,200}', branch or '') or '..' in branch or branch.startswith('/'):
            raise GitHubError('分支名称无效')
        if not re.fullmatch(r'[0-9a-fA-F]{40}', commit or ''):
            raise GitHubError('提交标识无效')
        try:
            response = await self._client.patch(f'https://api.github.com/repos/{repo}/git/refs/heads/{branch}',
                json={'sha': commit, 'force': False}, headers={'Authorization': 'Bearer ' + self._token,
                'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'})
        except httpx.HTTPError as exc:
            raise GitHubError('GitHub 连接失败') from exc
        if response.status_code != 200:
            raise GitHubError(f'GitHub 分支更新失败（HTTP {response.status_code}）')
        return response.json()

    async def pull_request(self, repository, number):
        repo = self._repo(repository)
        response = await self._get(f'/repos/{repo}/pulls/{int(number)}')
        data = response.json()
        return {'number': data['number'], 'state': data['state'], 'merged': bool(data.get('merged')),
                'url': data['html_url'], 'branch': data['head']['ref'], 'head_sha': data['head']['sha'],
                'head_repo': (data['head'].get('repo') or {}).get('full_name', '').lower()}

    async def feedback(self, repository, number):
        repo = self._repo(repository)
        events = []
        endpoints = [('issue_comment', f'/repos/{repo}/issues/{int(number)}/comments'),
                     ('review_comment', f'/repos/{repo}/pulls/{int(number)}/comments'),
                     ('review', f'/repos/{repo}/pulls/{int(number)}/reviews')]
        for kind, path in endpoints:
            for page in range(1, 51):
                response = await self._get(path, {'per_page': 100, 'page': page})
                data = response.json()
                if not isinstance(data, list):
                    raise GitHubError('GitHub 事件格式无效')
                for item in data:
                    if kind == 'review' and not item.get('submitted_at'):
                        continue
                    text = (item.get('body') or '').strip()
                    if not text:
                        continue
                    events.append({'kind': kind, 'id': str(item['id']),
                        'version': item.get('updated_at') or item.get('submitted_at') or item.get('created_at') or '',
                        'author': (item.get('user') or {}).get('login', ''),
                        'is_bot': (item.get('user') or {}).get('type') == 'Bot' or (item.get('user') or {}).get('login', '').endswith('[bot]'),
                        'association': item.get('author_association', 'NONE'),
                        'body': text, 'url': item.get('html_url', '')})
                if 'next' not in response.links:
                    break
            else:
                raise GitHubError('GitHub 评论页数超出上限')
        return sorted(events, key=lambda event: (event['version'], event['kind'], event['id']))
