"""Isolated local Git workspace for a continuity task."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess


class WorkspaceError(RuntimeError):
    pass


class GitWorkspace:
    def __init__(self, source, root):
        self.source = Path(source).resolve()
        self.root = Path(root).resolve()

    @staticmethod
    def _git(cwd, *args):
        env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
        try:
            result = subprocess.run(['git', *args], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkspaceError('无法运行 Git') from exc
        if result.returncode:
            raise WorkspaceError('Git 操作失败，请检查仓库和基线')
        return result.stdout

    def create(self, task_id, expected_head):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', task_id or ''):
            raise WorkspaceError('任务标识无效')
        if not re.fullmatch(r'[0-9a-fA-F]{40,64}', expected_head or ''):
            raise WorkspaceError('基线提交无效')
        self._git(self.source, 'cat-file', '-e', expected_head + '^{commit}')
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / task_id
        if target.exists():
            raise WorkspaceError('任务工作区已存在，需要先检查其状态')
        self._git(self.root, 'clone', '--no-hardlinks', '--no-checkout', '--', str(self.source), str(target))
        self._git(target, 'switch', '-c', 'agent/' + task_id, expected_head)
        return target

    def create_remote(self, task_id, repository, branch, expected_head, token):
        import base64
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', task_id or ''):
            raise WorkspaceError('任务标识无效')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository or ''):
            raise WorkspaceError('项目名称无效')
        if not re.fullmatch(r'[A-Za-z0-9._/-]{1,200}', branch or '') or '..' in branch or branch.startswith('/'):
            raise WorkspaceError('分支名称无效')
        if not re.fullmatch(r'[0-9a-fA-F]{40}', expected_head or '') or not token:
            raise WorkspaceError('项目基线或凭据未配置')
        self._git(self.source, 'rev-parse', '--is-inside-work-tree')
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / task_id
        if target.exists():
            raise WorkspaceError('任务工作区已存在，需要先检查其状态')
        self._git(self.root, 'clone', '--no-hardlinks', '--no-checkout', '--', str(self.source), str(target))
        # The access token stays in this subprocess environment, never in a remote URL or command argument.
        auth = base64.b64encode(('x-access-token:' + token).encode()).decode('ascii')
        environment = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_CONFIG_COUNT': '1',
                       'GIT_CONFIG_KEY_0': 'http.https://github.com/.extraheader',
                       'GIT_CONFIG_VALUE_0': 'AUTHORIZATION: basic ' + auth}
        url = 'https://github.com/' + repository + '.git'
        try:
            result = subprocess.run(['git', 'fetch', '--no-tags', '--depth=1', '--', url, 'refs/heads/' + branch],
                cwd=target, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkspaceError('无法获取项目分支') from exc
        if result.returncode:
            raise WorkspaceError('无法获取项目分支，请检查仓库访问权限')
        fetched = self._git(target, 'rev-parse', 'FETCH_HEAD').decode('ascii').strip()
        if fetched.lower() != expected_head.lower():
            raise WorkspaceError('项目分支已变化，请重新检查')
        self._git(target, 'switch', '-c', 'agent/' + task_id, 'FETCH_HEAD')
        return target

    def changed_paths(self, path):
        path = Path(path).resolve()
        if path.parent != self.root or not path.is_dir():
            raise WorkspaceError('工作区不属于当前任务目录')
        tracked = self._git(path, 'diff', '--name-only', '-z', 'HEAD')
        untracked = self._git(path, 'ls-files', '--others', '--exclude-standard', '-z')
        return sorted({entry.decode('utf-8') for entry in (tracked + untracked).split(b'\0') if entry})

    def commit(self, path, message):
        from .tools import _safe_path, ToolError
        path = Path(path).resolve()
        files = self.changed_paths(path)
        if not files:
            raise WorkspaceError('任务没有可提交的文件修改')
        for file in files:
            try:
                _safe_path(file, path)
            except ToolError as exc:
                raise WorkspaceError('修改包含不允许提交的路径') from exc
        self._git(path, 'add', '--', *files)
        self._git(path, '-c', 'user.name=Super-agent', '-c', 'user.email=agent@localhost',
                  '-c', 'core.hooksPath=', 'commit', '--no-verify', '-m', message[:200])
        return self._git(path, 'rev-parse', 'HEAD').decode('ascii').strip()

    def file_mode(self, path, name):
        path = Path(path).resolve()
        if path.parent != self.root:
            raise WorkspaceError('工作区不属于当前任务目录')
        entry = self._git(path, 'ls-tree', 'HEAD', '--', name).decode('utf-8').strip()
        if not entry:
            return '100644'
        mode = entry.split(' ', 1)[0]
        if mode not in {'100644', '100755'}:
            raise WorkspaceError('不支持修改该文件类型')
        return mode


async def verify_workspace(path, command, timeout=120):
    """Run an explicitly configured argv without a shell or model credentials."""
    import asyncio
    if not command or not isinstance(command, list) or any(not isinstance(x, str) or not x for x in command):
        return False
    safe_keys = ('PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP', 'LANG')
    environment = {key: os.environ[key] for key in safe_keys if key in os.environ}
    process = None
    try:
        process = await asyncio.create_subprocess_exec(*command, cwd=str(path), env=environment,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
        return await asyncio.wait_for(process.wait(), timeout) == 0
    except (OSError, asyncio.TimeoutError):
        if process is not None and process.returncode is None:
            from .dsh import DshBackend
            await DshBackend._terminate(process)
        return False
    except asyncio.CancelledError:
        if process is not None and process.returncode is None:
            from .dsh import DshBackend
            await DshBackend._terminate(process)
        raise
