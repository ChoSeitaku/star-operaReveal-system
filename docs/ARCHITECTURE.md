# 架构说明

## 1. 整体结构

```
┌────────────────────────┐         ┌──────────────────────────────┐
│  认证中心（8001）       │         │  分析后端（8000）             │
│  auth_service/         │◀────────│  backend/                    │
│  · 授权登录页           │ 服务端   │  · /auth/*      认证接入      │
│  · /auth/authorize     │ 换取令牌 │  · /api/chat/*  会话与消息    │
│  · /oauth/token        │────────▶│  · /api/attachment/*  附件    │
│  · /oauth/userinfo     │         │  · /api/tasks/*  任务         │
└────────────────────────┘         │  · /api/results/* 结果        │
                                   │  · /api/admin/*  系统管理      │
┌────────────────────────┐         │  · WS /api/chat/ws/chat       │
│  浏览器（零构建前端）    │◀───────▶│  · 静态资源（frontend/）      │
│  frontend/             │  HTTP   └──────────────┬───────────────┘
│  · ES Module 模块化     │  + WS                  │
└────────────────────────┘                        │
                                   ┌──────────────▼───────────────┐
                                   │  SQLite                      │
                                   │  · data/system.db  系统库      │
                                   │  · db/demo/*.db    情景库(只读) │
                                   │  · uploads/exports/workspace  │
                                   └──────────────────────────────┘
```

两个服务由 `run.py` 在**同一个进程**中启动（各自一个 uvicorn Server 跑在守护线程里）。
uvicorn 在非主线程会自动跳过信号处理（见 `uvicorn/server.py` 的 `capture_signals`），
因此无需任何 hack；主线程负责就绪探测与 Ctrl+C 优雅退出。

`run.py` 是唯一入口：启动前会检查情景库是否存在，缺失则调用 `db/seed_scenarios.py` 生成，
因此全新环境只需 `python run.py` 一条命令（系统库的建表与默认配置由后端 lifespan 完成）。

### 监听套接字为什么要自己绑定

uvicorn 的 `Config.bind_socket()` 只创建一个套接字，并按 host 字符串里是否含 `:` 决定地址族。
而浏览器解析 `localhost` 时，按 RFC 6724 / Happy Eyeballs 会**优先尝试 IPv6 回环 `::1`**，
若服务只监听了 IPv4 的 `127.0.0.1`，用户访问 `http://localhost:8000` 就会得到「拒绝连接」
（`curl` 往往回退到 IPv4 而察觉不到，所以命令行测试容易漏掉这个问题）。

因此 `run.py` 先用 `_bind_sockets()` 自行绑定套接字，再通过 `Server.run(sockets=[...])`
交给 uvicorn：

| `--host` | 实际监听 |
| --- | --- |
| `127.0.0.1`（默认） | `127.0.0.1` + `::1`（双栈回环，不对外暴露） |
| `0.0.0.0` | `0.0.0.0` + `::`（IPv4/IPv6 全网） |
| 其他显式地址 | 按原样绑定 |

IPv6 绑定失败（系统禁用 IPv6 等）会打印提示并自动降级为仅 IPv4；IPv4 绑定失败
（端口被占用）则直接报错退出。套接字在退出时统一关闭。

## 2. 模块划分

### 2.1 后端

| 模块 | 文件 | 职责 |
| --- | --- | --- |
| 认证接入 | `backend/session_auth.py`、`routers/auth_router.py` | 对接认证中心、令牌校验、Cookie 签发、当前用户识别、用户自动建档 |
| 会话 | `backend/conversations.py`、`routers/chat_router.py` | 会话增删改查、序号分配、级联删除、长连接令牌签发 |
| 消息 | `routers/chat_router.py` | 消息入库、历史查询、顺序控制（`seq_no`） |
| 附件 | `backend/storage.py`、`attachments/parser.py`、`routers/attachment_router.py` | 文件保存/删除/下载、路径校验、后台解析 |
| 长连接 | `backend/ws/manager.py`、`ws/endpoint.py` | 会话级连接管理、连接鉴权、消息推送、广播 |
| 任务 | `backend/tasks/{state,service,runner}.py` | 任务创建、状态流转、取消、日志、并发控制 |
| 分析 | `backend/analysis/*` | 上下文装配、工具执行、引擎调度、结构化结论 |
| 结果 | `analysis/result_builder.py`、`routers/result_router.py` | 结果落库、Markdown 渲染、导出文件管理 |
| 配置 | `backend/config.py`、`routers/admin_router.py` | 配置读取、优先级合成、热更新 |

### 2.2 前端（零构建，原生 ES Module）

| 模块 | 文件 | 职责 |
| --- | --- | --- |
| 认证回调 | `js/auth.js`、`js/callback.js` | 授权跳转、回调参数接收、登录态校验、退出 |
| 会话 | `js/session.js` | 会话列表加载、新建、删除、重命名、切换 |
| 聊天 | `js/chat.js`、`js/ws.js` | 消息发送、流式渲染、取消、历史回放、断线重连 |
| 附件 | `js/attachment.js` | 上传（含进度）、解析状态轮询、删除、下载 |
| 结果 | `js/result.js` | 六段式展示、复制、导出 |
| 配置 | `js/config.js` | 配置查看、修改、重载 |
| 日志 | `js/logs.js` | 实时任务区、步骤条、工具过程、错误与日志查看 |
| 编排 | `js/app.js`、`js/admin.js` | 把上述模块组装成工作台与管理台 |

## 3. 关键流程

### 3.1 授权登录

```
浏览器            后端(8000)                    认证中心(8001)
  │ GET /auth/login │                              │
  │────────────────▶│ 302 (client_id, redirect_uri, HMAC state)
  │                 │─────────────────────────────▶│  展示授权页
  │  选择「数据分析师」                              │
  │─────────────────────────────────────────────▶ │ /auth/authorize
  │                 │                              │ 校验 redirect_uri 白名单
  │                 │                              │ 签发一次性 code
  │◀────────────────────────────────────────────  │ 302 redirect_uri?code=&state=
  │ GET /auth/callback?code=&state=               │
  │────────────────▶│ 1. 校验 state 签名与时效      │
  │                 │ 2. POST /oauth/token ───────▶│（服务端到服务端，含 client_secret）
  │                 │◀──── access_token + user ────│
  │                 │ 3. 按 external_user_id 建档   │
  │                 │ 4. Set-Cookie: star_session  │
  │◀────────────────│ 302 /callback.html?status=ok │
  │  页面校验登录态 → 跳转 /app                     │
```

`client_secret` 只在服务端之间传递，浏览器永远接触不到。
`state` 用 HMAC 签名并带时间戳，认证中心无需保存任何会话状态。

### 3.2 一次分析任务的完整生命周期

```
① 发送            浏览器 ──WS user_message──▶ 后端
② 落库            backend/tasks/service.py
                    · 取得会话锁
                    · 检查该会话是否已有 queued/running 任务（有则拒绝，且不写任何记录）
                    · 同一事务内写入：messages(role=user, seq_no=N) + analysis_tasks(queued)
                    · 广播 user_message_accepted
③ 调度            TaskRunner.run() 在后台 asyncio 任务中执行
                    · 全局信号量放行（analysis.max_concurrent_tasks）
                    · 条件更新 queued → running（WHERE task_status='queued'）
④ 装配上下文       analysis/context.py
                    · 超过阈值时先把较早对话压缩为 context_summaries
                    · 摘要 + 最近 N 条消息 + 附件预览 + 库结构 → 系统提示词与消息数组
⑤ 执行引擎         analysis/engine.py 调度 demo / llm / canned 引擎
                    · 引擎只通过 EngineContext 回调：emit_text / call_tool / finalize
                    · 每次工具调用 → tool_start + tool_finish 事件 + tool 消息落库
⑥ 结构化结果       analysis/result_builder.py
                    · 规范化六段式结构 → 渲染 Markdown → 写 exports/ → 落 analysis_results
                    · 推送 result_ready
⑦ 收尾             写入最终 assistant 消息 → task_status=success → 推送 done
                    · finally：释放信号量与会话锁、清理取消标记
```

### 3.3 序号分配与消息↔任务配对

规则表**不额外增加列**，配对关系靠不变式维持：

- 每条被受理的用户消息，必然**恰好**创建一个任务
- 被单飞拒绝的消息**不写入任何记录**
- 每个任务无论成功、失败还是取消，都必然落一条最终助手消息

因此「第 k 条用户消息（按 `seq_no`）↔ 第 k 个任务（按 `id`）」永远成立，
历史回放接口据此把任务信息挂到对应的用户消息上。

`seq_no` 由 `SELECT COALESCE(MAX(seq_no),0)+1` 在会话锁内分配；
单飞约束保证同一会话同时只有一个任务在写消息，因此序号严格递增且不重复。

## 4. 并发与状态一致性

| 场景 | 机制 |
| --- | --- |
| 同一会话并发提交 | 会话级 `asyncio.Lock` + 锁内 DB 检查，拒绝时不写入任何记录 |
| 排队中的任务被取消 | `cancel_task` 先置取消标记，再做**条件更新**（`WHERE task_status='queued'`）；运行器的 `_try_start` 同样是条件更新。两者竞争必有且仅有一方成功，`done` 事件只会发一次 |
| 运行中的任务被取消 | 协作式取消：运行器在每个工具调用边界检查取消标记并抛 `TaskCancelled` |
| 全局并发上限 | `asyncio.Semaphore(analysis.max_concurrent_tasks)` 决定 `queued → running` 的放行时机，让 `queued` 状态有实际意义 |
| 服务重启 | 启动时把遗留的 `queued`/`running` 任务标记为 `failed`（"服务重启导致任务中断"），并记录系统日志 |
| 任务运行中会话被删除 | 删除前先取消该会话的任务；运行器在每次落库前复核会话状态，已删除则静默跳过，不会复活数据 |
| 状态流转校验 | `_transition` 每次以**数据库中的当前状态**为准做合法性校验，而不是用内存缓存，避免覆盖并发写入 |
| SQLite 并发 | WAL + `busy_timeout=10000`；每次操作使用独立短连接；写操作遇 "database is locked" 指数退避重试；绝不在 LLM 调用期间持有事务 |

## 5. 分析引擎抽象

```
AnalysisEngine (ABC)
├── DemoEngine    规则化演示引擎（默认，零 API Key）
├── LLMEngine     大模型 Agent 引擎（Anthropic Messages 协议 + 工具调用）
└── CannedEngine  固定输出的自检引擎
```

引擎**不接触数据库、WebSocket 与文件路径**，只通过 `EngineContext` 交互：

```python
await ctx.text("第一步：按一级类目统计……")      # 增量文本
await ctx.call_tool("db_query", {"database": "catalog", "sql": "..."})
await ctx.log("info", "step", "上下文装配完成")
if ctx.cancelled(): ...                          # 协作式取消检查
await ctx.finalize(ResultData(...))              # 提交结构化结果
```

`EngineContext.call_tool` 由运行器实现，内部完成：推送 `tool_start` → 执行工具 →
推送 `tool_finish` → 把工具结果作为 `role='tool'` 的消息落库。
因此**两个引擎在持久化与实时推送链路上完全一致**，界面表现毫无差别。

### 5.1 结构化结果的产出方式

- **LLM 引擎**：把 `submit_result` 注册为一个真实工具（`input_schema` 即六段式结构），
  系统提示词要求模型在最后一步调用它。引擎拦截该工具调用并直接 `finalize`，
  避免对自由文本做脆弱的 JSON 解析。轮次将尽时会追加提示催促模型收口。
- **Demo 引擎**：规则分析器按固定 SQL 序列取数，用真实查询结果计算六段式结论并 `finalize`。
- 两者都会经过 `result_builder.normalize_result` 做容错规范化
  （缺字段补默认值、指标单位去重、建议条目重新编号等）。

## 6. 安全边界

| 面 | 措施 |
| --- | --- |
| 会话 | `star_session` 为 HttpOnly + SameSite=Lax，载荷 HMAC-SHA256 签名；密钥持久化在 `data/secret.key`，重启不掉线 |
| 长连接 | 一次性、60 秒时效、绑定用户与会话的临时令牌；服务端原子消费 |
| 数据隔离 | 所有会话/附件/任务/结果接口都做归属校验，跨用户一律返回 404 |
| SQL 工具 | 只能打开 `analysis.scenario_db_dir` 下的库（`system.db` 在结构上不可达），一律 `mode=ro` + `PRAGMA query_only`；仅允许单条 `SELECT` / `WITH` / `EXPLAIN`，并拒绝写关键字 |
| 文件工具 | `file_read`/`text_search` 限定在**本会话**的 uploads 与 workspace 目录内（`Path.is_relative_to` 校验）；`file_write` 只允许写 workspace，文件名经清洗 |
| 命令执行 | 仅放行白名单可执行文件（`python` → 当前解释器、`sqlite3` 若存在）；工作目录固定为会话 workspace；环境变量白名单化（剔除 `ANTHROPIC_*` 等敏感变量）；30 秒超时 + 64KB 输出上限 |
| 上传 | 扩展名白名单 + 大小上限 + 流式落盘（超限即中止并回滚记录）；文件名清洗（去目录部分与非法字符） |
| 下载 | 下载前校验路径确实位于数据目录之内 |
| 管理接口 | 全部要求 `role=admin`；敏感配置在响应中脱敏 |

> 说明：`run_command` 的设计目标是**演示环境下的能力展示与自检**（例如校验中间产物），
> 不是生产级的沙箱。生产部署时建议改为容器化执行或直接关闭该工具
> （`analysis.tools_enabled` 去掉 `run_command` 即可）。

## 7. 配置体系

优先级（由低到高）：

```
内置默认值  →  .env 文件  →  system_configs 表  →  进程环境变量
```

- 某一层取到**空值**视为「该层未设置」，继续向下层回退 ——
  否则播种进数据库的空默认值会把 `.env` 层永久遮蔽。
- 被环境变量强制覆盖的键，在管理台标注为只读（`overridden_by_env` / `editable`），
  写入数据库时会明确提示不会生效。
- 配置分 4 组：`llm` / `auth` / `analysis` / `upload`，共 31 项。
- 热更新：`POST /api/admin/reload` 重新读取数据库并重建配置，
  同时失效数据源结构缓存；**下一个任务立即生效**，无需重启。

功能开关同样通过配置表达，例如：

| 开关 | 配置键 | 效果 |
| --- | --- | --- |
| 分析引擎 | `analysis.engine_mode` | `demo` / `llm` / `canned` |
| 工具启停 | `analysis.tools_enabled` | 逗号分隔的工具名列表，去掉即停用 |
| 全局并发 | `analysis.max_concurrent_tasks` | 同时运行的分析任务上限 |

## 8. 文件存储

```
data/
├── system.db                    系统库（10 张规格表）
├── secret.key                   会话签名密钥
├── logs/                        预留日志目录
├── uploads/{user_id}/{conversation_id}/     附件
│   └── {attachment_id}_{原文件名}
│       └── {同上}.meta.json                 解析元数据（编码、预览、状态）
├── exports/{user_id}/{conversation_id}/     导出结果
│   └── 归因分析_report_task{N}_{时间戳}.md
└── workspace/{user_id}/{conversation_id}/   分析中间产物
    ├── 分析笔记.md
    ├── funnel_data.csv
    └── verify_funnel.py
```

删除会话时三个目录一并删除，并清理空的用户目录。

## 9. 数据库

### 9.1 系统库（10 张规格表）

`users`、`conversations`、`messages`、`attachments`、`analysis_tasks`、
`analysis_results`、`context_summaries`、`websocket_tokens`、`system_configs`、`task_logs`
—— 列名与规格书 1.1.7 完全一致。DDL 见 `db/init.sql`。

设计取舍：

- `conversations.status` 删除时置为 `deleted`（软删除，保留行），
  子表记录与文件目录则物理删除。
- `task_logs.task_id` 可为空：配置重载、工具自检等**系统级日志**不属于任何分析任务。
- 状态字段都用 `CHECK` 约束，非法枚举值在数据库层就被拒绝。

### 9.2 情景库（只读）

`db/demo/` 下的 6 个业务库由 `db/seed_scenarios.py` 生成，
使用固定随机种子 `random.Random(20261008)`，**可重复构建且结果完全一致**。

| 库 | 场景 | 表数据规模 |
| --- | --- | --- |
| `catalog.db` | 商品目录优化（完整演示） | 14,818 行 / 6 表 |
| `customer_behavior.db` | 客户行为分析（完整演示） | 27,264 行 / 4 表 |
| `inventory.db` | 库存异常分析 | 12,819 行 / 6 表 |
| `reviews.db` | 评论反馈分析 | 2,300 行 / 4 表 |
| `market.db` | 市场表现分析 | 3,124 行 / 5 表 |
| `refunds.db` | 退款模式分析 | 3,583 行 / 5 表 |

## 10. 上下文压缩

多轮对话会让上下文无限增长，因此在满足阈值时触发压缩：

- 触发条件：未摘要消息数 ≥ `analysis.summarize_after_messages`（默认 12）
  或未摘要字符数 ≥ `analysis.summarize_after_chars`（默认 24000）
- 压缩窗口：`(已有摘要的 end_seq_no, 最新序号 - context_recent_messages]`，
  即**始终保留最近 10 条消息原文**，只压缩更早的部分
- 摘要方式：LLM 引擎下调用大模型生成要点摘要；demo/canned 引擎下退化为信息抽取式摘要
  （保留每轮提问与结论的要点），两者写入同一张 `context_summaries` 表

这样旧会话继续追问时，既不会丢失关键结论，也不会让 token 消耗线性增长。

## 11. 验收与测试

| 脚本 | 作用 | 依赖 |
| --- | --- | --- |
| `scripts/smoke_test.py` | 端到端验收：REST + WebSocket 全链路，92 项断言 | httpx、websockets |
| `scripts/ui_test.py` | 浏览器端到端：真实浏览器走完整用户链路并截图，同时收集控制台错误 | playwright |
| `POST /api/admin/selftest` | 逐个执行六个分析工具的能力自检 | 无 |
