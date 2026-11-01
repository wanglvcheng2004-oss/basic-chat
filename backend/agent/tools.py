from __future__ import annotations

import json
from pathlib import Path

from .config import ROOT
from .store import AgentStore
from .types import Task
from ..access import can_read_thread

MAX_READ_CHARS = 8000
MAX_WRITE_CHARS = 20000
BLOCKED_PARTS = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", "tools", ".data", ".state", "data", ".codex", ".review-venv"}
BLOCKED_FILES = {".env"}


class ToolError(ValueError):
    pass


def _json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False)


def _function(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


READ_THREAD = _function(
    "read_thread",
    "读取某个会话线程的消息记录，用于召回上下文。省略 thread_id 时读取当前线程。",
    {
        "thread_id": {"type": "string", "description": "线程 ID，例如 room:main 或 dm:<userId>"},
        "limit": {"type": "integer", "description": "最多返回多少条，默认 20，最大 50"},
    },
    [],
)

READ_MESSAGE = _function(
    "read_message",
    "按消息 ID 读取完整内容，用于展开被截断的消息。",
    {"message_id": {"type": "string", "description": "消息 ID"}},
    ["message_id"],
)

LIST_TASKS = _function(
    "list_tasks",
    "列出最近的任务及状态，便于回忆此前做过什么。",
    {"limit": {"type": "integer", "description": "最多返回多少条，默认 10"}},
    [],
)

READ_FILE = _function(
    "read_file",
    "读取项目工作区内的文本文件。路径必须相对于项目根目录。",
    {
        "path": {"type": "string", "description": "相对路径，例如 backend/main.py"},
        "max_chars": {"type": "integer", "description": "最多读取的字符数，默认 4000，最大 8000"},
    },
    ["path"],
)

WRITE_FILE = _function(
    "write_file",
    "在工作区内创建或覆盖文本文件。仅在确实需要修改代码或文档时使用。",
    {
        "path": {"type": "string", "description": "相对路径，例如 docs/notes.md"},
        "content": {"type": "string", "description": "完整文件内容，最多 20000 字符"},
    },
    ["path", "content"],
)

LIST_FILES = _function(
    "list_files",
    "列出工作区目录结构，帮助定位文件。",
    {
        "path": {"type": "string", "description": "相对目录，默认 ."},
        "depth": {"type": "integer", "description": "递归深度，默认 2，最大 3"},
    },
    [],
)


def tool_schemas(allow_write: bool = True) -> list[dict]:
    schemas = [READ_THREAD, READ_MESSAGE, LIST_TASKS, READ_FILE, LIST_FILES]
    if allow_write:
        schemas.append(WRITE_FILE)
    return schemas


def _safe_path(raw: str) -> Path:
    value = (raw or ".").strip().replace("\\", "/")
    candidate = Path(value)
    resolved = candidate.resolve() if candidate.is_absolute() else (ROOT / candidate).resolve()
    try:
        relative = resolved.relative_to(ROOT)
    except ValueError as exc:
        raise ToolError("只能访问项目工作区内的文件") from exc
    if any(part.casefold() in BLOCKED_PARTS for part in relative.parts):
        raise ToolError("该路径被禁止访问")
    name = relative.name.casefold()
    if name.startswith(".env") or any(suffix in name for suffix in ('.sqlite', '.db', '.pem', '.key')):
        raise ToolError("环境变量文件禁止访问")
    return resolved


async def execute_tool(
    name: str,
    args: dict,
    *,
    store: AgentStore,
    task: Task,
    allow_write: bool = True,
) -> str:
    try:
        if name == "read_thread":
            thread_id = str(args.get("thread_id") or task.thread_id)
            if not can_read_thread(task, thread_id):
                raise ToolError('没有读取该会话的权限')
            limit = max(1, min(int(args.get("limit") or 20), 50))
            messages = await store.history(thread_id, limit=limit)
            related = [item.public() for item in store.tasks.values() if item.thread_id == thread_id][-5:]
            return _json(
                {
                    "threadId": thread_id,
                    "messages": [
                        {
                            "id": message.id,
                            "role": message.role,
                            "nickname": message.nickname,
                            "content": message.content[:800],
                            "createdAt": message.created_at,
                        }
                        for message in messages
                    ],
                    "tasks": related,
                }
            )

        if name == "read_message":
            message = await store.lookup_message(str(args.get("message_id") or ""))
            if message is None:
                raise ToolError("找不到该消息")
            if not can_read_thread(task, message.thread_id):
                raise ToolError('没有读取该消息的权限')
            return _json(message.as_packet())

        if name == "list_tasks":
            limit = max(1, min(int(args.get("limit") or 10), 30))
            tasks = sorted((item for item in store.tasks.values() if can_read_thread(task, item.thread_id)), key=lambda item: item.created_at)[-limit:]
            return _json({"tasks": [item.public() for item in tasks]})

        if name == "read_file":
            path = _safe_path(str(args.get("path") or ""))
            if not path.is_file():
                raise ToolError("文件不存在或不是普通文件")
            limit = max(200, min(int(args.get("max_chars") or 4000), MAX_READ_CHARS))
            with path.open(encoding="utf-8", errors="replace") as file:
                text = file.read(limit + 1)
            return _json(
                {
                    "path": str(path.relative_to(ROOT)).replace("\\", "/"),
                    "content": text[:limit],
                    "truncated": len(text) > limit,
                    "totalChars": len(text),
                }
            )

        if name == "write_file":
            if not allow_write or getattr(task, 'kind', 'interactive') != 'interactive':
                raise ToolError("当前未开启写入权限")
            content = str(args.get("content") or "")
            if len(content) > MAX_WRITE_CHARS:
                raise ToolError(f"内容过长，最多 {MAX_WRITE_CHARS} 字符")
            path = _safe_path(str(args.get("path") or ""))
            if path.is_dir():
                raise ToolError("目标是目录")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return _json(
                {
                    "path": str(path.relative_to(ROOT)).replace("\\", "/"),
                    "bytes": len(content.encode("utf-8")),
                    "ok": True,
                }
            )

        if name == "list_files":
            root = _safe_path(str(args.get("path") or "."))
            if not root.exists():
                raise ToolError("目录不存在")
            depth = max(1, min(int(args.get("depth") or 2), 3))
            entries: list[str] = []
            for item in sorted(root.rglob("*")):
                try:
                    relative = item.relative_to(ROOT)
                except ValueError:
                    continue
                if len(relative.parts) > depth + len(root.relative_to(ROOT).parts):
                    continue
                if any(part in BLOCKED_PARTS for part in relative.parts):
                    continue
                try:
                    _safe_path(str(relative))
                except ToolError:
                    continue
                suffix = "/" if item.is_dir() else ""
                entries.append(str(relative).replace("\\", "/") + suffix)
                if len(entries) >= 200:
                    break
            return _json({"path": str(root.relative_to(ROOT)).replace("\\", "/") or ".", "entries": entries})

        raise ToolError(f"未知工具：{name}")
    except ToolError as exc:
        return _json({"error": str(exc)})
    except (OSError, ValueError) as exc:
        return _json({"error": f"工具执行失败：{exc}"})
