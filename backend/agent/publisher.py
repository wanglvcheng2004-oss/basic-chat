"""Publish a verified isolated diff without force-updating a branch."""
from __future__ import annotations
import base64
from pathlib import Path
from .tools import _safe_path, ToolError

class PublishError(RuntimeError):
    pass

class ProjectPublisher:
    def __init__(self, github):
        self.github = github

    async def update_existing(self, link, workspace, path, *, verify=None):
        repo, number, branch, head = link['repo'], link['pr'], link['branch'], link['head_sha']
        current = await self.github.pull_request(repo, number)
        if (current['state'] != 'open' or current['merged'] or current['head_repo'] != repo or
                current['branch'] != branch or current['head_sha'] != head):
            raise PublishError('PR 已变化或不再开放，请重新检查后再执行')
        files = workspace.changed_paths(path)
        if not files:
            raise PublishError('任务没有产生文件变更')
        if verify is None or not await verify(path):
            raise PublishError('项目验证未通过，未更新 PR')
        current = await self.github.pull_request(repo, number)
        if current['head_sha'] != head or current['branch'] != branch or current['state'] != 'open':
            raise PublishError('验证期间 PR 已变化，未更新分支')
        commit = await self._build_commit(repo, head, workspace, path, files)
        await self.github.advance_branch(repo, branch, commit)
        return commit

    async def create_new(self, repo, base, head, branch, title, workspace, path, *, verify=None):
        if await self.github.branch_head(repo, base) != head:
            raise PublishError('项目基线已变化，请重新检查后再执行')
        files = workspace.changed_paths(path)
        if not files:
            raise PublishError('任务没有产生文件变更')
        if verify is None or not await verify(path):
            raise PublishError('项目验证未通过，未创建 PR')
        if await self.github.branch_head(repo, base) != head:
            raise PublishError('验证期间项目基线已变化，未创建 PR')
        commit = await self._build_commit(repo, head, workspace, path, files)
        await self.github.create_branch(repo, branch, commit)
        result = await self.github.create_draft(repo, branch, base, title, '自动生成的草稿。验证命令已通过，请审阅。')
        return {**result, 'head_sha': commit, 'branch': branch}

    async def _build_commit(self, repo, head, workspace, path, files):
        entries = []
        root = Path(path).resolve()
        if len(files) > 50:
            raise PublishError('修改文件数量超过上限')
        base_tree = await self.github.commit_tree(repo, head)
        for name in files:
            try:
                file = _safe_path(name, root)
            except ToolError as exc:
                raise PublishError('修改包含不允许的路径') from exc
            if not file.exists():
                entries.append({'path': name, 'mode': workspace.file_mode(root, name), 'type': 'blob', 'sha': None})
                continue
            if not file.is_file() or file.stat().st_size > 1_000_000:
                raise PublishError('修改的文件不可用或过大')
            blob = await self.github.create_blob(repo, base64.b64encode(file.read_bytes()).decode('ascii'))
            entries.append({'path': name, 'mode': workspace.file_mode(root, name), 'type': 'blob', 'sha': blob})
        tree = await self.github.create_tree(repo, base_tree, entries)
        return await self.github.create_commit(repo, tree, head, 'Apply reviewed changes')
