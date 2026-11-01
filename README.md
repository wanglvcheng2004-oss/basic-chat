# 基础聊天室与主动助手

Vue 3 / Vite + FastAPI / WebSocket，单房间、单主机、单服务进程。成员身份、消息、任务、主动规则、摘要与通知保存在本机 SQLite 中。页面刷新或服务重启后可恢复历史。

本地开发分支：`feat/local-proactive-agent`，基于 `5b946cde211a4f719e919780f84ecc9b7c1c4c0d`。实现与验收范围见 [开发记录](docs/proactive-development.md)。

## 本机启动

需要 Python 3.11+、Node.js 18+ 和 npm。本次实际验证环境为 Windows、Python 3.14.3、Node.js 24.14.1。

```powershell
.\scripts\setup.ps1
.\scripts\start.ps1
```

打开 `http://127.0.0.1:8000`，输入昵称加入。从此本机入口加入的成员可管理主动规则、查看队友邀请码。会话由服务端校验，昵称相同不会合并身份。会话有效期 30 天；清除浏览器 Cookie 会失去原会话入口，本版没有账号找回功能。

开发模式：

```powershell
.\scripts\dev.ps1
```

打开 `http://127.0.0.1:5173`。开发服务只监听本机，前端热更新；后端代码修改后需重启开发脚本。不要另开第二个后端或使用多个 Uvicorn worker。

## Radmin 联调

主机与队友加入同一个 Radmin 网络后，在主机运行：

```powershell
.\scripts\start.ps1 -Radmin
# 或显式指定本机已分配的地址：
.\scripts\start.ps1 -ListenAddress '26.x.x.x' -Port 8000
```

脚本保留 `127.0.0.1` 管理入口，并用同一个进程额外监听选中的 IPv4 地址。主机从本机入口查看邀请码，队友访问打印的 Radmin 地址并输入邀请码。脚本拒绝 `0.0.0.0`，不添加或放宽防火墙规则。

本轮只验证了本机双地址监听、真实 TCP/WebSocket 多成员通信，尚未由另一台 Radmin 设备验证连通性。公网部署、反向代理转发和完整账号系统不在本版验收范围内。

## 使用方式

- 左侧是共享房间；`@小深` 发起明确的 AI 请求。
- 右侧“私聊”只属于当前成员；公开任务的工具也不能读取该成员的私聊。
- “任务”展示进度、结果和异常。只能停止自己的任务；“补充这个任务”明确关联任务 ID，已结束时作为新请求处理。
- “主动能力”中分别开启静默预处理、任务异常监控、定时摘要。**三项默认关闭**，只有主机管理员可以修改规则。
- 通知保留到点击“已读”；断线重发使用同一 ID。准备结果和摘要保存在侧栏，预处理不会自动向群聊发言。

### 静默预处理

只识别人类在共享房间中发出的明确堆栈，或“错误描述 + 项目文件路径”。普通聊天、智能体输出与已明确 `@小深` 的请求不会重复触发。

等待 10 秒合并相关消息，最长 30 秒；按问题和文件内容去重 30 分钟。最多读取 6 个安全的项目文本文件，单文件不超过 256 KB，传给模型的源码摘录最多 16000 字符。后台没有写文件、执行命令或访问私聊的工具，始终不使用 Harness。

每次执行最多 1 个模型请求，失败最多重试 1 次，单次总时限 90 秒。结果保留 30 分钟，引用文件内容改变后失效；后续相关问题会复用仍有效的线索。只会给出有来源的材料与待验证推测，不会声称已运行项目或修复代码。

### 任务异常监控

每 10 秒进行程序检查，不调用模型。执行信号与实际进展分别记录；180 秒没有新信号时标记“疑似停滞”。失败、停滞、恢复或超时只在状态变化时通知任务所有者。普通任务默认总时限 600 秒。

停止会取消正在执行和等待重试的任务；用户停止后不会自动恢复。只有后台只读任务允许最多一次自动重试。服务重启后，普通执行任务标为中断；后台只读任务按持久化的次数限制恢复。不会整项重放可能写文件的用户任务。

### 定时摘要

默认每天 19:00，时区固定为 `Asia/Shanghai`，小时和分钟可在界面修改。启用时开始记录，按 `[起点, 终点)` 划分固定窗口。仅包含该房间的人类消息和已完成的用户任务结果，排除私聊和自动通知。

空窗口不调用模型、不发通知。服务恢复后，把漏跑时段合并成一份补摘要。结果、通知和窗口推进在同一事务中保存；失败不会提前推进窗口。用户主动停止当前摘要会把该窗口记为已停止，之后不自动补回它。

每次摘要使用 1 个模型请求，失败最多重试 1 次。输入最多 16000 字符；长消息或过大窗口会明确标记覆盖不足。额度耗尽时只保存注明“未进行模型归纳”的原文摘录。“生成当前摘要”用于演示或主动提前收尾，成功后下一窗口从此刻开始。

## 资源与模型

默认总执行并发 3（包括 Router 与 Worker），后台最多 1；调度时用户请求优先，已经运行的请求不会被粗暴中断。默认每天每房间 30 次后台调用，其中为摘要保留 3 次。界面在演示模式下显示的次数是模拟调用计数，不代表费用。

没有密钥时可以运行完整界面和控制流程，所有自动结果明确标注为演示。启用真实模型：

```powershell
# 仅当 .env 尚不存在时复制，避免覆盖已有配置。
Copy-Item .env.example .env
# 编辑 .env，填写 LLM_API_KEY 后重启。
```

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `LLM_API_KEY` | 空 | 模型密钥，仅服务端使用 |
| `LLM_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容接口地址 |
| `AGENT_NAME` | `小深` | 智能体名称与提及词 |
| `ROUTER_MODEL` / `WORKER_MODEL` | `deepseek-flash` | 沿用仓库基线的模型名，真实可用性需联调 |
| `WORKER_MODEL_HEAVY` | `deepseek-v4-pro` | 复杂用户任务模型 |
| `MAX_WORKERS` | `3` | 总执行并发，环境变量最小值 2 |
| `MAX_STEPS` | `8` | 用户任务最大执行轮数 |
| `AGENT_ALLOW_WRITE` | `false` | 显式开启后，内置用户任务可写项目文件 |
| `TASK_TIMEOUT` | `600` | 内置用户任务总时限，秒 |
| `HEARTBEAT_INTERVAL` | `10` | 异常检查间隔，秒 |
| `STALL_SECONDS` | `180` | 疑似停滞阈值，秒 |
| `CHAT_DATA_DIR` | 本机应用数据目录 | 持久化文件所在目录 |
| `HARNESS_ENABLED` | `false` | 请求使用可选 Harness 后端 |
| `HARNESS_POLICY_VERIFIED` | `false` | 管理者已独立验证 Harness 权限边界的声明 |
| `HARNESS_COMMAND` / `HARNESS_PROFILE` | `dsh` / `headless` | 可选 CLI 与 profile |
| `HARNESS_TIMEOUT` | `1800` | Harness 任务总时限，秒 |

Harness 只有在已安装、开启写入且设置 `HARNESS_POLICY_VERIFIED=true` 时才用于显式用户任务。这一配置是管理者声明，不是程序对 Harness 隔离的自动证明。后台三项能力始终使用内置受限流程。本轮未安装或真实调用 Harness，也未调用收费模型。

## 保存、备份与回退

默认数据库位于 `%LOCALAPPDATA%/basic-chat/<工作区标识>/chat.sqlite3`，不进入仓库。可用 `CHAT_DATA_DIR` 指定另一独立目录；不要多个运行实例共用一个目录。数据库迁移前会保存备份，版本高于当前程序时拒绝降级。

手动备份时先停止服务，再复制整个数据目录（包含 SQLite 的配套文件）。要回退代码，可切回基线，并保留当前数据备份；旧基线不支持新会话和持久化结构。日常停用只需关闭三项主动规则，基础聊天仍可使用。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests -q
npm --prefix frontend ci --no-audit --no-fund
npm --prefix frontend run build
node --test frontend/tests/*.test.js
```

测试使用模拟模型、可控时间和临时数据库；包含真实本机 TCP/WebSocket 与 Windows 父子进程终止测试。真实模型效果、真实 Harness 权限以及另一台设备的 Radmin 访问需要单独验收。

## 接口兼容

先通过 `POST /api/session` 建立成员会话，再凭 HttpOnly Cookie 连接 `/ws`。旧的仅昵称 WebSocket 入口已停止接受匿名连接。其他原有聊天和智能体消息类型保留。

新增：`agent.followup {taskId, content}`、`notice.ack {noticeId}`；服务端提供 `chat.history`、`agent.tasks`、`agent.notice`、`agent.automation`。`GET/PUT /api/automation` 查询或修改规则，`POST /api/automation/summary` 生成当前窗口摘要。所有修改与私有数据访问在服务端校验。
