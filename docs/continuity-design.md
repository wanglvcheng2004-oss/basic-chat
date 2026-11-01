# Super-agent 连续性设计方案

> 目标：让用户在网页聊天室、私聊、GitHub PR 评论、IM 话题等不同界面与同一个 Agent 交互时，不需要关心「哪个 Session 知道什么」。同一项目内的上下文、任务和决策由 Router 统一协调，Worker 在独立分区中执行。
>
> 本文只做设计，不包含代码改动。

---

## 1. 目标与验收

### 1.1 要解决的问题

当前实现中：

- 房间消息用 `room:main`，私聊用 `dm:<userId>`，两者历史互不可见；
- Router 只读取当前 Thread 最近 16 条消息，无法跨 Thread 召回；
- 任务、订阅、消息全部在进程内，重启即丢失；
- 只有网页一个信道，没有 GitHub/IM；
- 所有任务都在同一个仓库根目录执行，没有项目/任务级工作区隔离。

### 1.2 验收场景

以文章中的例子作为核心验收：

1. 用户在 IM 里说：「这个功能采用方案 A。」
2. Agent 创建任务，并把方案 A 作为约束写进任务。
3. 开发过程中，用户在 GitHub PR 评论里说：「改成方案 B，不要用 A 里的缓存层。」
4. Agent 应当：
   - 知道这条 PR 评论属于同一个任务；
   - 读到方案 A 的原始上下文；
   - 把执行方向调整为 B；
   - 在 PR 评论里回复调整结果，而不是重新开一个无关任务。

补充验收：

- 同一项目内，房间、私聊、GitHub 三个 Thread 的后续消息都能进入同一个任务；
- 不同项目的上下文不串；
- 服务重启后，项目、Thread、任务、订阅和工作区仍能恢复；
- 一个 Thread 的后续消息不需要再次 @ Agent，也能到达正在执行它的 Worker。

---

## 2. 官方实现调研结论

官方 `deepseek-ai/dsh-superagent-prototype` 已经实现了连续性骨架，核心结论：

| 能力 | 官方原型 | 对我们的含义 |
|---|---|---|
| Router + Worker | ✅ Main Session + 每 Ticket 一个 Executor Session | 双层架构方向正确，不需要重新发明 |
| Thread | ✅ 每个 Source 定义自己的会话线 | 网页房间、私聊、PR、IM 话题都应映射为 Thread |
| Correlation | ✅ 只补充上下文，不负责路由 | 跨信道关联不能替代订阅 |
| Subscription | ✅ Ticket ↔ Thread 多对多 | 后续事件直通 Worker 的关键 |
| Ticket / Assignment | ✅ 授权、执行、报告分离 | 比我们现在的 Task 更完整 |
| 持久化 | ✅ SQLite + effect 队列 + outbox | 重启恢复必须补 |
| 信道 | GitHub + 飞书 | 没有通用 IM，也没有网页聊天 |
| 项目分区 | 一个实例一个仓库；每 Ticket 一个目录/worktree | 任务级隔离强，多项目需要多实例 |
| 跨 Source 身份 | 明确不做映射，只作为提示词文本 | 不能假设同一个人的 IM 和 GitHub 账号自动关联 |
| Main Session 记忆 | 卡片 + 按需读取，有界上下文 | 不是无限全局记忆 |

关键边界：

- 官方原型的「连续」是 **Ticket/Thread 级**，不是所有消息的无限上下文；
- 跨信道消息只有在 **同一个 Ticket 订阅了这些 Thread** 时才自动直通；
- 否则 Main Session 需要显式 `attach`，或者把它当作新任务；
- 一个实例只服务一个 GitHub 仓库；
- 多项目 = 多实例、多 profile、多 stateDir，不是单个 Agent 内的项目命名空间。

因此，我们的方案应当借鉴它的 **Thread / Correlation / Subscription / Ticket** 模型，而不是只做一个更大的聊天记录。

---

## 3. 概念模型

在现有 Router/Worker 之上增加五个核心概念：

| 概念 | 定义 | 归属 |
|---|---|---|
| **Project** | 一个代码仓库/工作区及其绑定的信道集合 | 顶层隔离边界 |
| **Thread** | 某个信道下的一条会话线，如房间、私聊、PR、IM 话题 | 属于一个 Project |
| **Message** | Thread 内的一条消息 | 属于一个 Thread |
| **Correlation** | 两个 Thread 的关联，只补充上下文，不路由 | 双向关系 |
| **Subscription** | Ticket 订阅 Thread，后续消息扇出给 Worker | 多对多 |
| **Ticket** | 一个被授权的工作单元，替代当前 Task | 属于一个 Project，可订阅多个 Thread |
| **Channel** | 回复写回哪个信道的适配器 | 绑定 Thread |

关系：

```text
Project 1 ── * Thread 1 ── * Message
                 │
                 ├── * Correlation * ── Thread
                 │
                 └── * Subscription * ── Ticket
                                           │
                                           └── Assignment → Worker Session
```

约束：

- 一个 Thread 只属于一个 Project，避免跨项目上下文泄漏；
- 一个 Ticket 只属于一个 Project，但可以订阅同一 Project 下的多个 Thread；
- Correlation 只用于召回上下文，不触发 Worker；
- Subscription 才负责把新事件直通 Worker；
- 所有跨项目操作必须显式声明目标 Project，不能靠模型猜。

---

## 4. 架构选择

### 4.1 推荐：单 Router + 项目命名空间 + 项目内 Worker 池

```text
                    ┌─────────────────────────────┐
网页房间 ─┐         │ Router（单常驻 Session）     │
私聊 ─────┼─Trigger→│ 项目索引 / 全局摘要 / 分派   │
GitHub ───┤         │ attach / read_thread 工具    │
IM ───────┘         └──────────┬──────────────────┘
                               │ Ticket
                 ┌─────────────┼─────────────┐
                 ▼             ▼             ▼
            Worker A       Worker B      Worker C
           Project X      Project X     Project Y
           独立 worktree  独立 worktree 独立 worktree
```

- **一个 Router**：保持「只有一个 Agent」的体验；
- **Project 作为上下文分区**：Router 每次只装载当前 Project 的摘要和 Thread 索引；
- **Worker 按 Project 隔离**：每个 Ticket 一个工作目录，跨项目不可见；
- **信道适配器**只负责把外部事件标准化成 Trigger，以及把回复写回原信道。

### 4.2 物理隔离级别

| 级别 | 做法 | 适用 |
|---|---|---|
| 逻辑分区（推荐起步） | 单进程、SQLite 按 `project_id` 分区、路径守卫 | 个人/小团队 |
| 物理分区 | 每个项目一个实例、独立 profile/stateDir/端口 | 团队、强隔离、不同凭据 |

物理分区等价于官方原型的一仓库一实例，代价是多个项目之间没有共享 Router 记忆。

建议：**先做逻辑分区，保留升级到物理分区的接口**。当某个项目需要独立凭据或强安全边界时，再把该项目迁到独立实例。

---

## 5. 核心流程

### 5.1 IM 提出方案 A

1. IM 适配器收到消息，生成 Trigger：
   - `project_id = p1`
   - `thread_id = im:p1:chat-123:topic-9`
   - `channel = feishu|slack|...`
2. Trigger 写入 `messages`，更新 Thread 摘要。
3. Router 读取：
   - 当前 Project 摘要；
   - 当前 Thread 最近消息；
   - 该 Project 的开放 Ticket 列表；
   - 相关的 Thread 摘要。
4. Router 判断这是新任务，调用 `dispatch_task`：
   - 创建 Ticket；
   - 把方案 A 写入 `instruction` 和 Ticket 约束；
   - 订阅当前 IM Thread；
   - 启动 Worker。
5. Worker 在 `projects/p1/worktrees/task-...` 中执行，回复写回 IM Thread。

### 5.2 GitHub PR 评论改成方案 B

1. GitHub 适配器轮询/接收 PR 评论，生成 Trigger：
   - `project_id = p1`
   - `thread_id = github:p1:repo#42`
2. 如果该 Thread 已被 Ticket T1 订阅：
   - 直接扇出到 T1 的 Worker；
   - Worker 上下文包含原 IM Thread 的摘要和 T1 的约束；
   - Worker 调整方案为 B，并在 PR 评论中回复。
3. 如果未订阅：
   - Trigger 进入 Router；
   - Router 通过 `list_tickets` + Thread 内容判断它属于 T1；
   - 调用 `attach_thread(ticket_id=T1, thread_id=github:...#42)`；
   - 之后的消息自动直通。
4. 如果无法判断：
   - Router 在 PR 评论中提一个澄清问题，或创建新 Ticket；
   - 不会静默合并两个无关任务。

### 5.3 跨信道关联的三种来源

| 来源 | 例子 | 可靠性 |
|---|---|---|
| 显式 | 用户在消息里附上任务 ID/PR 链接 | 高 |
| 结构 | Ticket 自己创建了 PR，自动订阅 PR Thread | 高 |
| 语义 | Router 根据标题、文件、分支名判断 | 中，需要置信度阈值 |

`Correlation` 表只记录前两种和语义关联，用于上下文召回；真正让消息到达 Worker 的仍然是 `Subscription`。

---

## 6. Router 的上下文与记忆

### 6.1 Router 看什么

每次决策只装载一个 Project 的切片：

1. **全局层**：
   - Agent 身份、当前时间、策略；
   - Project 列表（名称、仓库、状态），不包含其他项目的消息内容。
2. **项目层**：
   - Project 摘要（最近目标、进行中的 Ticket、关键决策）；
   - 开放/最近关闭的 Ticket 卡片；
   - 当前 Thread 与相关 Thread 的摘要。
3. **线程层**：
   - 最近 N 条完整消息；
   - 更早消息截断为摘要 + 消息 ID；
   - 需要时通过 `read_thread` / `read_message` 逐跳展开。

### 6.2 新增 Router 工具

| 工具 | 作用 |
|---|---|
| `list_threads(project_id?)` | 列出项目内的 Thread 及摘要 |
| `read_thread(thread_id, limit)` | 读取某个 Thread 的消息 |
| `read_message(message_id)` | 读取完整消息 |
| `list_tickets(project_id, status?)` | 查看开放/历史任务 |
| `attach_thread(ticket_id, thread_id, note?)` | 把 Thread 关联到 Ticket 并订阅 |
| `link_threads(thread_a, thread_b, relation)` | 建立 Correlation，仅补充上下文 |
| `dispatch_task(title, instruction, project_id, thread_id, complexity)` | 创建 Ticket |
| `update_ticket(ticket_id, instruction)` | 调整正在执行的任务方向 |

### 6.3 记忆层级

| 层级 | 存储 | 更新时机 |
|---|---|---|
| 消息原文 | `messages` | 每条消息 |
| Thread 摘要 | `thread_summaries` | 每 N 条或每轮任务结束 |
| Project 摘要 | `project_memories` | 任务完成、关键决策、定时 |
| 任务约束 | `tickets.instruction` + `ticket_constraints` | 创建/调整时 |
| 长期记忆 | 可选：`memory_entries` | 人工或定时沉淀 |

首版不做向量检索；用「摘要 + ID + 按需读取」的链式召回即可，和官方原型的 `orchestrator_read` 思路一致。

### 6.4 Token 预算

建议配置：

```ini
ROUTER_CONTEXT_BUDGET=24000
ROUTER_INLINE_MESSAGES=12
ROUTER_SPILL_CHARS=300
THREAD_SUMMARY_MAX_CHARS=800
PROJECT_SUMMARY_MAX_CHARS=1500
```

超出预算时按优先级裁剪：其他项目摘要 > 当前项目旧消息 > 相关 Thread 摘要。当前 Thread 最近消息永远保留。

---

## 7. 数据模型（SQLite）

当前 `AgentStore` 是内存 `deque`，需要替换为 SQLite。建议表结构：

```sql
CREATE TABLE projects (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  repo_url      TEXT,
  root_path     TEXT NOT NULL,          -- 主 clone
  workspace_root TEXT NOT NULL,         -- 每任务 worktree 的父目录
  default_branch TEXT DEFAULT 'main',
  created_at    TEXT NOT NULL
);

CREATE TABLE threads (
  id            TEXT PRIMARY KEY,       -- project:channel:external
  project_id    TEXT NOT NULL REFERENCES projects(id),
  channel       TEXT NOT NULL,          -- web-room | web-dm | github | feishu | slack
  external_id   TEXT NOT NULL,
  title         TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  UNIQUE(project_id, channel, external_id)
);

CREATE TABLE messages (
  id            TEXT PRIMARY KEY,
  thread_id     TEXT NOT NULL REFERENCES threads(id),
  role          TEXT NOT NULL,          -- user | agent | system
  author_id     TEXT,
  author_name   TEXT,
  content       TEXT NOT NULL,
  created_at    TEXT NOT NULL
);
CREATE INDEX idx_messages_thread_time ON messages(thread_id, created_at);

CREATE TABLE thread_links (
  id                TEXT PRIMARY KEY,
  thread_id         TEXT NOT NULL REFERENCES threads(id),
  related_thread_id TEXT NOT NULL REFERENCES threads(id),
  relation          TEXT NOT NULL,      -- same_task | mentions | manual
  confidence        REAL DEFAULT 1.0,
  created_by        TEXT,
  created_at        TEXT NOT NULL,
  UNIQUE(thread_id, related_thread_id)
);

CREATE TABLE tickets (
  id              TEXT PRIMARY KEY,
  project_id      TEXT NOT NULL REFERENCES projects(id),
  thread_id       TEXT NOT NULL REFERENCES threads(id),  -- 发起线程
  title           TEXT NOT NULL,
  instruction     TEXT NOT NULL,
  constraints     TEXT,                 -- 方案 A/B 等关键约束
  status          TEXT NOT NULL,        -- queued | running | blocked | done | failed | stopped
  owner_id        TEXT,
  workspace       TEXT,                 -- 本任务 worktree
  branch          TEXT,
  dsh_session_id  TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);

CREATE TABLE subscriptions (
  ticket_id   TEXT NOT NULL REFERENCES tickets(id),
  thread_id   TEXT NOT NULL REFERENCES threads(id),
  created_at  TEXT NOT NULL,
  PRIMARY KEY (ticket_id, thread_id)
);

CREATE TABLE ticket_events (
  id          TEXT PRIMARY KEY,
  ticket_id   TEXT NOT NULL REFERENCES tickets(id),
  kind        TEXT NOT NULL,            -- tool | report | state | error
  payload     TEXT NOT NULL,
  created_at  TEXT NOT NULL
);

CREATE TABLE thread_summaries (
  thread_id     TEXT PRIMARY KEY REFERENCES threads(id),
  summary       TEXT NOT NULL,
  last_message_id TEXT,
  updated_at    TEXT NOT NULL
);

CREATE TABLE project_memories (
  project_id  TEXT PRIMARY KEY REFERENCES projects(id),
  summary     TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE identities (
  id           TEXT PRIMARY KEY,
  channel      TEXT NOT NULL,           -- github | feishu | web
  external_id  TEXT NOT NULL,
  display_name TEXT,
  alias        TEXT,                    -- 仅用于提示词/展示，不做认证
  UNIQUE(channel, external_id)
);

CREATE TABLE channel_bindings (
  id          TEXT PRIMARY KEY,
  project_id  TEXT NOT NULL REFERENCES projects(id),
  channel     TEXT NOT NULL,
  config_json TEXT NOT NULL,
  enabled     INTEGER NOT NULL DEFAULT 1
);
```

迁移策略：

- 首版新建 `store.db`，启动时执行 schema 版本迁移；
- 旧的内存数据不做迁移，直接以新库为准；
- `AgentStore` 保留为内存实现用于测试，生产用 `SqliteStore`；
- 所有写操作走事务；消息先落库再广播。

---

## 8. 接口与前端

### 8.1 WebSocket 扩展

```jsonc
// 客户端
{ "type": "chat", "projectId": "p1", "content": "@小深 ..." }
{ "type": "agent.dm", "projectId": "p1", "content": "..." }
{ "type": "agent.stop", "taskId": "..." }
{ "type": "project.select", "projectId": "p1" }

// 服务端
{ "type": "project.state", "projects": [...] }
{ "type": "thread.linked", "ticketId": "...", "threadId": "...", "relation": "same_task" }
{ "type": "agent.delta", "threadId": "...", ... }
{ "type": "agent.done", "threadId": "...", ... }
```

首版可以保持单项目：服务端配置默认 `project_id`，前端暂不显示项目切换；数据模型仍保留多项目字段。

### 8.2 前端

- 顶部增加 Project 选择器（首版可隐藏）；
- 房间消息按项目隔离；
- Agent 面板增加「关联会话」区域，显示当前 Ticket 订阅的 Thread；
- 任务卡显示所属 Project 和 worktree 分支；
- 重启后从服务端恢复历史和任务。

---

## 9. 项目分区与工作区

### 9.1 目录结构

```text
<stateDir>/
  orchestrator.sqlite
  projects/
    p1/
      repo/                 # 主 clone
      worktrees/
        task-abc/           # 每个 Ticket 一个独立 worktree
      logs/
```

### 9.2 隔离规则

- 每个 Ticket 一个 worktree + 分支 `agent/<ticket-id>`；
- Worker 的 `cwd` 只指向自己的 worktree；
- 文件工具路径守卫拒绝跨项目路径；
- 项目级 git 锁，避免并发 worktree 操作冲突；
- DSH 权限模式优先 `workspace-write`；需要 shell 时显式开启；
- 不同项目可以使用不同的凭据/环境变量。

### 9.3 多项目

- 逻辑分区：一个进程、一个 SQLite，`project_id` 隔离；
- 物理分区：一个项目一个实例，独立 stateDir/profile/端口；
- 迁移路径：先逻辑分区，后续把高风险项目迁出。

---

## 10. 多信道适配

统一接口：

```python
class ChannelAdapter:
    name: str

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send(self, thread: Thread, message: Message) -> None: ...
    def normalize(self, raw_event) -> Trigger: ...
```

首版优先级：

| 信道 | 方式 | 说明 |
|---|---|---|
| Web 房间 | 现有 WebSocket | 映射到 `web-room` Thread |
| Web 私聊 | 现有 WebSocket | 映射到 `web-dm` Thread |
| GitHub | 轮询通知/Issue/PR 评论 | 先用 `gh api` 轮询，后续可换 Webhook |
| 飞书 | 事件订阅或轮询 | 参考官方原型，用 bot 身份 |
| 其他 IM | 后续适配器 | 先不定协议 |

GitHub 事件最小字段：

```json
{
  "project": "p1",
  "thread": "github:p1:repo#42",
  "author": "alice",
  "content": "改成方案 B",
  "kind": "pull_request_review_comment",
  "external_id": "123456"
}
```

回复时使用 Channel 的 `send`，不要直接写 WebSocket。

---

## 11. 安全与权限

- 每个信道独立的 allowlist；网页聊天默认只允许登录用户；
- 公网部署必须加访问控制，不能直接暴露无鉴权的 Agent；
- Project 级凭据隔离，避免 A 项目的 Token 被 B 项目 Worker 读取；
- 文件工具只允许访问当前 Project 的 worktree；
- DSH Worker 默认 `workspace-write`，禁止跨项目目录；
- 审计 `ticket_events`：谁触发、哪个 Thread、用了什么工具、改了哪些文件；
- 跨 Source 身份映射只用于展示/提示词，不作为授权依据。

---

## 12. 分阶段路线

### P2.1 领域模型与 SQLite（约 1–2 天）

- [ ] `Project` / `Thread` / `Ticket` / `Subscription` 数据类
- [ ] `SqliteStore` 与迁移
- [ ] 消息、任务、订阅落库
- [ ] 启动恢复未完成任务
- [ ] 现有 `Task` 迁移为 `Ticket`

验收：重启后消息、任务、订阅仍在；现有测试全绿。

### P2.2 Router 连续性（约 1–2 天）

- [ ] Project 摘要 / Thread 摘要
- [ ] Router 上下文构建器（跨 Thread）
- [ ] `read_thread` / `read_message` / `attach_thread` / `list_tickets` 工具
- [ ] Router 批量合并（batch 内一次 LLM 调用）
- [ ] Thread 级串行化

验收：同一项目内，房间 → 私聊 → 房间的消息能被 Router 正确关联；不同项目不串。

### P2.3 项目分区与 GitHub（约 2 天）

- [ ] Project 工作区与 worktree 管理
- [ ] Worker `cwd` 隔离、路径守卫
- [ ] GitHub 轮询适配器
- [ ] PR 创建后自动订阅 PR Thread
- [ ] 回复写回 PR 评论

验收：完成文章中的 A→B 场景（GitHub PR 评论改变方案）。

### P2.4 IM 与跨信道（约 2 天）

- [ ] 飞书/其他 IM 适配器
- [ ] Identity 别名表（仅展示）
- [ ] Correlation 表与自动关联规则
- [ ] 多信道回复路由

验收：IM 发起任务，GitHub PR 评论调整，Agent 在原信道回复。

### P2.5 持久在线与自愈（约 1–2 天）

- [ ] Watchdog：心跳、超时、无进展
- [ ] 任务重试与恢复
- [ ] 健康指标 `/health` 扩展
- [ ] 可选：独立 daemon/云端部署

---

## 13. 测试计划

- **单元**：Thread ID 生成、Subscription 扇出、Correlation 只读、Project 隔离、上下文裁剪。
- **集成**：房间 + 私聊 + GitHub 三个 Thread 关联到同一 Ticket；A→B 调整。
- **恢复**：进程重启后任务继续；worktree 存在；DSH session 可续。
- **安全**：跨项目路径拒绝；未授权信道拒绝；公网未登录拒绝。
- **回归**：现有 14 个测试保持通过。

---

## 14. 风险与开放问题

| 风险 | 应对 |
|---|---|
| 单 Router 上下文过载 | 项目切片 + 摘要 + 预算裁剪；必要时按项目分片 |
| 自动关联误判 | 只自动关联高置信度场景（Ticket 自建 PR）；其他要求显式 attach |
| 多项目共用凭据 | 逻辑分区阶段用 Project 级凭据；高风险项目物理隔离 |
| DSH Worker 权限过大 | 独立 OS 账号、workspace-write、路径守卫、审计 |
| 跨 Source 身份不一致 | 只做展示别名，不作为授权；每个信道独立 allowlist |
| 研究原型不可直接生产 | 参考其领域模型，核心逻辑自研；或先在 WSL2 中验证原型 |

开放问题：

1. 首版是单项目还是直接多项目？
2. GitHub 用轮询还是 Webhook？
3. IM 先接飞书还是企业微信/Slack？
4. 每个项目是否需要独立的模型/密钥？

---

## 15. 推荐落地顺序

1. **先做 P2.1 + P2.2**：SQLite、Thread、Subscription、Router 跨 Thread 记忆；
2. 用网页房间 + 私聊验证「一个 Agent」的连续感；
3. 再接 GitHub，完成 A→B 验收；
4. 最后接 IM 和多项目隔离。

这样不必一开始就重写整个官方原型，也能逐步逼近文章里的连续性。
