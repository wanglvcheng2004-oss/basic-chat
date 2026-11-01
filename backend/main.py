from __future__ import annotations

import ipaddress
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .agent import AgentConfig, AgentRuntime
from .agent.automation import Automation
from .agent.continuity import ContinuityStore, ContinuityService, ContinuityError
from .agent.github_gateway import GitHubGateway, GitHubError
from .agent.store import AgentStore
from .agent.types import ROOM_THREAD, now_iso
from .connection_manager import ConnectionManager
from .database import Database
from .identity import IdentityService
from .schemas import ClientPacket

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / 'frontend' / 'dist'
COOKIE = 'basic_chat_session'


class Login(BaseModel):
    nickname: str = Field(min_length=1, max_length=20)
    invite: str = Field(default='', max_length=100)


class GitHubBind(BaseModel):
    repo: str = Field(min_length=3, max_length=200)
    pr: int = Field(gt=0)


class PolicyUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    autoReply: bool | None = None
    preprocess: bool | None = None
    watchdog: bool | None = None
    summary: bool | None = None
    hour: int | None = Field(default=None, ge=0, le=23)
    minute: int | None = Field(default=None, ge=0, le=59)
    dailyLimit: int | None = Field(default=None, ge=3, le=300)


def local_access(request):
    host = urlsplit('http://' + request.headers.get('host', '')).hostname
    peer = request.client.host if request.client else ''
    try:
        peer_local = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        peer_local = False
    return peer_local and host in {'localhost', '127.0.0.1', '::1'}


def same_origin(request):
    origin = request.headers.get('origin')
    if request.headers.get('sec-fetch-site') == 'cross-site':
        return False
    if not origin:
        return True
    parsed = urlsplit(origin)
    host = urlsplit('http://' + request.headers.get('host', ''))
    return parsed.scheme in {'http', 'https'} and parsed.netloc == host.netloc


def create_app(config=None, *, llm=None, github=None):
    config = config or AgentConfig.from_env()
    db = Database(config.state_path / 'chat.sqlite3')
    manager = ConnectionManager()
    identity = IdentityService(db, config.name)
    runtime = AgentRuntime(config, manager, store=AgentStore(db=db), llm=llm)
    automation = Automation(runtime, db)
    runtime.automation = automation
    continuity_store = ContinuityStore(db)
    runtime.continuity_store = continuity_store
    if github is None and config.github_projects and config.github_token:
        try:
            github = GitHubGateway(json.loads(config.github_projects), config.github_token)
        except (ValueError, GitHubError) as exc:
            raise RuntimeError('GitHub 项目配置无效，请检查允许仓库与设计维护者列表。') from exc
    runtime.github_gateway = github
    continuity = ContinuityService(continuity_store, github, runtime.on_github_feedback, notify=runtime.on_github_status) if github is not None else None

    async def poll_continuity():
        while True:
            await asyncio.sleep(30)
            links = await db.all('SELECT task_id,owner_id FROM continuity_links')
            for link in links:
                try:
                    await continuity.sync(link['task_id'], link['owner_id'])
                except Exception:
                    # Keep the event pending; the next poll can retry without leaking credentials.
                    continue

    @asynccontextmanager
    async def lifespan(_):
        await db.open()
        claimed = False
        try:
            claimed = await db.lease('runtime', runtime.instance_id)
            if not claimed:
                raise RuntimeError('此数据目录已有运行实例；请关闭另一实例，或使用独立 CHAT_DATA_DIR。异常退出后最多等待 30 秒。')
            await identity.initialize()
            await continuity_store.initialize()
            await automation.initialize()
            await runtime.start()
            poller = asyncio.create_task(poll_continuity()) if continuity else None
            yield
        finally:
            if claimed:
                if 'poller' in locals() and poller is not None:
                    poller.cancel()
                    await asyncio.gather(poller, return_exceptions=True)
                await runtime.stop()
                if github is not None:
                    await github.close()
                await db.release('runtime', runtime.instance_id)
            await db.close()

    app = FastAPI(title='Basic Chat', lifespan=lifespan)
    app.state.runtime, app.state.database = runtime, db
    app.state.identity, app.state.automation = identity, automation

    @app.middleware('http')
    async def check_origin(request, call_next):
        if request.method in {'POST', 'PUT', 'DELETE', 'PATCH'} and not same_origin(request):
            return JSONResponse({'detail': '请求来源与当前站点不一致。'}, status_code=403)
        return await call_next(request)

    async def member(request, admin=False):
        found = await identity.resolve(request.cookies.get(COOKIE))
        if found is None:
            raise HTTPException(401, '请先加入聊天室。')
        if admin and not found.admin:
            raise HTTPException(403, '只有主机管理员可以修改主动规则。')
        return found

    @app.get('/health')
    async def health():
        healthy = runtime._ticker is not None and not runtime._ticker.done()
        return JSONResponse({'status': 'ok' if healthy else 'degraded', 'online': manager.count,
                             'agent': runtime.public_info()}, status_code=200 if healthy else 503)

    @app.get('/api/session')
    async def session(request: Request):
        found = await identity.resolve(request.cookies.get(COOKIE))
        return {'member': found.public() if found else None, 'local': local_access(request), 'agent': runtime.public_info()}

    @app.post('/api/session')
    async def login(payload: Login, request: Request):
        try:
            found, token = await identity.login(payload.nickname, payload.invite,
                local=local_access(request), existing=await identity.resolve(request.cookies.get(COOKIE)))
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        response = JSONResponse({'member': found.public(), 'agent': runtime.public_info()})
        response.set_cookie(COOKIE, token, max_age=30 * 86400, httponly=True,
                            samesite='strict', secure=request.url.scheme == 'https')
        return response

    @app.delete('/api/session')
    async def logout(request: Request):
        await identity.logout(request.cookies.get(COOKIE))
        response = JSONResponse({'ok': True})
        response.delete_cookie(COOKIE)
        return response

    @app.get('/api/automation')
    async def automation_state(request: Request):
        found = await member(request)
        result = await automation.snapshot()
        if found.admin:
            result['invite'] = await identity.invite()
        return result

    @app.put('/api/automation')
    async def update_automation(payload: PolicyUpdate, request: Request):
        found = await member(request, admin=True)
        await automation.configure(payload.model_dump(exclude_none=True), found.id)
        return await automation.snapshot()

    @app.post('/api/automation/summary')
    async def summarize_now(request: Request):
        await member(request, admin=True)
        if not automation.policy['summary']:
            raise HTTPException(409, '请先启用定时摘要。')
        await automation.schedule_summary(manual=True)
        return await automation.snapshot()

    @app.get('/api/github/repositories')
    async def github_repositories(request: Request):
        await member(request)
        return {'repositories': github.repositories if github else []}

    @app.post('/api/tasks/{task_id}/github')
    async def bind_github(task_id: str, payload: GitHubBind, request: Request):
        found = await member(request)
        task = runtime.store.get_task(task_id)
        if task is None or task.owner_id != found.id or task.channel != 'room':
            raise HTTPException(404, '未找到自己的公开任务。')
        if github is None:
            raise HTTPException(503, 'GitHub 仓库或凭据未配置。')
        try:
            pr = await github.pull_request(payload.repo, payload.pr)
            if pr['state'] != 'open' or pr['head_repo'] != payload.repo.lower():
                raise ContinuityError('PR 必须开放且分支位于已配置仓库。')
            existing = await github.feedback(payload.repo, payload.pr)
            authorized_history = []
            for event in existing:
                if event.get('is_bot') or '<!-- continuity-event:' in event.get('body', '') or not await github.authorized(payload.repo, event.get('author', ''), event.get('association', 'NONE')):
                    continue
                authorized_history.append(event)
            async with continuity._lock:
                await continuity_store.bind_with_history(task.id, found.id, payload.repo, payload.pr,
                    pr['branch'], pr['head_sha'], authorized_history)
            return await continuity_store.get(task.id, found.id)
        except (GitHubError, ContinuityError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get('/api/tasks/{task_id}/github')
    async def github_status(task_id: str, request: Request):
        found = await member(request)
        link = await continuity_store.get(task_id, found.id)
        if link is None:
            raise HTTPException(404, '未找到关联的 PR。')
        return {'link': link, 'status': await db.one('SELECT * FROM continuity_status WHERE task_id=?', (task_id,)),
                'events': await continuity_store.history(task_id, found.id),
                'comments': await db.all('SELECT id,state FROM continuity_outbox WHERE task_id=?', (task_id,))}

    @app.post('/api/tasks/{task_id}/github/sync')
    async def github_sync(task_id: str, request: Request):
        found = await member(request)
        if continuity is None:
            raise HTTPException(503, 'GitHub 仓库或凭据未配置。')
        try:
            return {'events': await continuity.sync(task_id, found.id)}
        except ContinuityError as exc:
            raise HTTPException(404, str(exc)) from exc
        except GitHubError as exc:
            raise HTTPException(502, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.websocket('/ws')
    async def websocket_chat(websocket: WebSocket):
        found = await identity.resolve(websocket.cookies.get(COOKIE))
        if found is None or not same_origin(websocket):
            await websocket.close(code=1008, reason='请先加入聊天室，或检查访问来源。')
            return
        await websocket.accept()
        client = manager.add(found.nickname, websocket, found.id, found.admin)
        try:
            await manager.send(client, {'type': 'ready', **found.public(), 'onlineCount': manager.count,
                                        'members': manager.members(), 'agent': runtime.public_info()})
            await manager.broadcast({'type': 'presence', 'onlineCount': manager.count, 'members': manager.members()})
            await runtime.send_history(client)
            await manager.send(client, {'type': 'agent.automation', **await automation.snapshot()})
            while True:
                try:
                    raw = await websocket.receive_text()
                    if len(raw) > 12000:
                        raise ValueError('消息包过大。')
                    packet = ClientPacket.model_validate_json(raw)
                    if await identity.resolve(websocket.cookies.get(COOKIE)) is None:
                        await websocket.close(code=1008, reason='会话已过期。')
                        break
                except WebSocketDisconnect:
                    break
                except (ValidationError, json.JSONDecodeError, ValueError) as exc:
                    message = exc.errors()[0]['msg'] if isinstance(exc, ValidationError) else str(exc)
                    await manager.send(client, {'type': 'error', 'message': message})
                    continue
                if packet.type == 'chat':
                    message = runtime.record_room_message(user_id=client.user_id, nickname=client.nickname,
                        content=packet.content, message_id=str(uuid4()), created_at=now_iso())
                    await runtime.store.flush()
                    await manager.broadcast({'type': 'chat', **message.as_packet()})
                    await runtime.on_room_message(client, packet.content, message.id)
                    await automation.observe(message)
                elif packet.type == 'agent.dm':
                    await runtime.on_dm(client, packet.content)
                elif packet.type == 'agent.stop':
                    await runtime.stop_task(packet.taskId or '', client)
                elif packet.type == 'agent.followup':
                    await runtime.on_followup(client, packet.taskId or '', packet.content)
                elif packet.type == 'notice.ack':
                    await db.execute('INSERT OR IGNORE INTO receipts SELECT ?,id FROM notifications WHERE id=? AND (scope=? OR owner_id=?)',
                        (client.user_id, packet.noticeId, ROOM_THREAD, client.user_id))
        finally:
            manager.remove(client.connection_id)
            await asyncio.shield(manager.broadcast({'type': 'presence', 'onlineCount': manager.count, 'members': manager.members()}))

    if DIST.exists():
        if (DIST / 'assets').exists():
            app.mount('/assets', StaticFiles(directory=DIST / 'assets'), name='assets')

    @app.get('/')
    async def index():
        if (DIST / 'index.html').exists():
            return FileResponse(DIST / 'index.html')
        return JSONResponse({'message': '前端尚未构建，请运行 npm --prefix frontend run build'}, status_code=503)

    return app


app = create_app()
