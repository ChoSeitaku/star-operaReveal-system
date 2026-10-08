# 接口说明

- 后端基地址：`http://localhost:8000`
- 认证中心基地址：`http://localhost:8001`
- 交互式文档（FastAPI 自动生成）：<http://localhost:8000/docs>

## 0. 约定

### 0.1 认证方式

除认证相关接口外，所有 `/api/*` 接口都要求携带登录后下发的 **HttpOnly Cookie**：

```
star_session=<base64url(payload)>.<hmac-sha256>
```

Cookie 由后端签发（`backend/session_auth.py`），内容为 `{用户ID, 角色, 过期时间}` 的 HMAC 签名载荷，
服务端无状态校验，密钥持久化在 `data/secret.key`。

未登录或登录态过期时返回：

```json
HTTP/1.1 401 Unauthorized
{"detail": "unauthorized"}
```

管理接口额外要求 `role=admin`，否则返回：

```json
HTTP/1.1 403 Forbidden
{"detail": "需要系统管理员权限"}
```

### 0.2 通用错误格式

所有业务错误统一为 `{"detail": "中文错误描述"}`，常见状态码：

| 状态码 | 含义 |
| --- | --- |
| 400 | 参数错误（如标题为空、文件类型不支持、会话已归档） |
| 401 | 未登录 / 登录态失效 / 认证中心拒绝 |
| 403 | 权限不足（非管理员访问管理接口） |
| 404 | 资源不存在或不属于当前用户（会话、附件、任务、结果） |
| 413 | 上传文件超过大小限制 |
| 500 | 服务器内部错误 |

### 0.3 时间格式

数据库与接口中的时间统一为 **UTC ISO-8601**（如 `2026-10-08T03:10:13Z`），
前端负责转换为本地时区展示。

### 0.4 字段命名

接口响应统一使用 `snake_case`，且会话/消息/任务主键字段名与规格书一致：
`conversation_id`、`message_id`、`task_id`、`attachment_id`。

---

## 1. 认证接口

### `GET /auth/login` — 跳转到认证中心发起授权登录

无参数。返回 `302` 重定向到认证中心授权页，并携带 `client_id`、`redirect_uri` 与
HMAC 签名的 `state`（5 分钟有效，用于防 CSRF）。

```
HTTP/1.1 302 Found
Location: http://localhost:8001/auth/login?client_id=star-demo&redirect_uri=...&state=...
```

### `GET /auth/callback` — 处理授权回调并建立登录态

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `code` | string | 认证中心下发的授权码 |
| `state` | string | 必须与发起时一致且在有效期内 |

后端在**服务端**用授权码换取访问令牌（避免 `client_secret` 暴露给浏览器），
按 `external_user_id` 自动建档，下发会话 Cookie，然后重定向到登录回调页：

```
HTTP/1.1 302 Found
Set-Cookie: star_session=...; HttpOnly; SameSite=Lax; Path=/
Location: http://localhost:8000/callback.html?status=ok&display_name=%E6%95%B0%E6%8D%AE%E5%88%86%E6%9E%90%E5%B8%88&role=analyst
```

失败时同样重定向到回调页并携带 `status=error&message=...`，由页面提示用户重试。

### `POST /auth/logout` — 退出登录（扩展）

清空会话 Cookie。

```json
{"status": "ok", "message": "已退出登录"}
```

### `GET /api/session` — 查询登录态（扩展）

**始终返回 200**，供入口页 / 回调页等公开页面使用，避免未登录时的 401 在控制台产生噪音。

```json
{"authenticated": true, "user": {"user_id": 1, "username": "analyst", "display_name": "数据分析师", "role": "analyst"}}
```

### `GET /api/users/me` — 获取当前登录用户（扩展）

```json
{
  "user_id": 1, "username": "analyst", "display_name": "数据分析师",
  "role": "analyst", "status": "active",
  "created_at": "2026-10-08T02:27:24Z", "server_time": "2026-10-08T03:10:13Z"
}
```

---

## 2. 会话接口

### `POST /api/chat/create` — 新建会话

请求：

```json
{"title": "商品目录优化分析"}
```

`title` 可省略或留空，默认标题为「新会话」。

响应：

```json
{"conversation_id": 1, "title": "商品目录优化分析", "status": "active"}
```

### `POST /api/chat/delete` — 删除会话

请求：

```json
{"conversation_ids": [1, 2]}
```

级联删除该会话下的消息、附件记录、任务记录、结果记录、上下文摘要、任务日志与长连接令牌，
并删除 `uploads/`、`exports/`、`workspace/` 三个目录。会话行本身保留，`status` 置为 `deleted`。
所有权不属于当前用户的会话会被跳过而不是报错。

响应：

```json
{
  "deleted_count": 1,
  "deleted": [{"conversation_id": 1, "title": "商品目录优化分析"}],
  "skipped": [{"conversation_id": 999, "reason": "会话不存在"}]
}
```

### `POST /api/chat/update` — 重命名 / 归档会话

请求：

```json
{"conversation_id": 1, "title": "新标题", "status": "archived"}
```

`title` 与 `status` 至少提供一个；`status` 仅接受 `active` 或 `archived`（扩展字段）。

响应：

```json
{
  "conversation_id": 1, "title": "新标题", "status": "archived",
  "last_message_at": "2026-10-08T03:10:13Z",
  "created_at": "...", "updated_at": "...", "previous_title": "商品目录优化分析"
}
```

### `GET /api/chat/ls` — 会话列表

| 查询参数 | 说明 |
| --- | --- |
| `include_archived` | 是否包含已归档会话，默认 `true` |

响应（按最近消息时间倒序，不含已删除会话）：

```json
{
  "total": 2,
  "conversations": [
    {
      "conversation_id": 2, "title": "客户行为分析", "status": "active",
      "last_message_at": "2026-10-08T03:10:13Z",
      "created_at": "...", "updated_at": "...",
      "message_count": 24, "attachment_count": 1
    }
  ]
}
```

### `GET /api/chat/ls/{conversation_id}` — 会话历史消息

按 `seq_no` 升序返回全部消息（含工具消息），附件内联在所属消息上。

```json
{
  "conversation_id": 1,
  "total": 12,
  "messages": [
    {
      "message_id": 1, "role": "user", "message_type": "text",
      "content": "近30天各品类的搜索曝光到转化漏斗表现如何？",
      "tool_name": null, "tool_status": null, "seq_no": 1,
      "created_at": "2026-10-08T03:10:13Z",
      "attachments": [],
      "task": {"task_id": 1, "task_status": "success", "current_step": "完成", "has_result": true}
    }
  ]
}
```

- `role`：`user` / `assistant` / `tool`
- `message_type`：`text` / `tool_result`
- `task`：仅用户消息携带，指向该消息对应的分析任务（**第 k 条用户消息 ↔ 第 k 个任务**）
- `has_result`：该任务是否已产出结构化结果（前端据此决定是否请求结果接口）

### `POST /api/chat/ws-token` — 签发长连接临时令牌

请求：

```json
{"conversation_id": 1}
```

响应：

```json
{
  "websocket_token": "A1b2C3...",
  "expires_in": 60,
  "conversation_id": 1,
  "ws_url": "/api/chat/ws/chat?websocket_token=...&conversation_id=1"
}
```

令牌特性：**短时效**（默认 60 秒，`auth.ws_token_ttl_seconds` 可配）、
**一次性消费**、**绑定到具体会话与用户**。因此每次重连都必须重新签发。

---

## 3. 附件接口

### `POST /api/attachment/upload` — 上传附件

`multipart/form-data`：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `conversation_id` | int | 目标会话 |
| `file` | file | 文件对象 |

限制：扩展名必须在 `upload.allowed_extensions` 内（默认 `csv,txt,md,json,log,xlsx`），
大小不超过 `upload.max_file_size_mb`（默认 20 MB）。

响应：

```json
{
  "attachment_id": 1,
  "file_name": "sample_metrics.csv",
  "file_path": "uploads/1/1/1_sample_metrics.csv",
  "file_type": "csv",
  "file_size": 1236,
  "parse_status": "pending",
  "size_text": "1.2 KB"
}
```

`file_path` 为**相对于数据目录**的路径（不暴露服务器绝对路径）。
上传后立即返回，解析在后台进行，`parse_status` 会依次变为 `parsing` → `success` / `failed`。

### `POST /api/attachment/delete` — 删除附件

```json
{"attachment_id": 1}
```

同时删除磁盘文件与解析元数据 sidecar。

### `GET /api/attachment/get` — 下载附件

| 查询参数 | 说明 |
| --- | --- |
| `attachment_id` | 附件 ID |

返回文件流，带 `Content-Disposition: attachment; filename="..."`。

### `GET /api/attachment/ls` — 会话附件列表（扩展）

| 查询参数 | 说明 |
| --- | --- |
| `conversation_id` | 会话 ID |

```json
{
  "total": 1,
  "attachments": [
    {
      "attachment_id": 1, "conversation_id": 1, "message_id": null,
      "file_name": "sample_metrics.csv", "file_path": "uploads/1/1/1_sample_metrics.csv",
      "file_type": "csv", "file_size": 1236, "parse_status": "success",
      "created_at": "2026-10-08T03:10:13Z", "size_text": "1.2 KB"
    }
  ]
}
```

---

## 4. 任务与结果接口

### `GET /api/tasks/{task_id}` — 查询任务状态

```json
{
  "task_id": 1, "conversation_id": 1,
  "input_text": "近30天各品类的搜索曝光到转化漏斗表现如何？",
  "task_status": "success",
  "current_step": "完成",
  "started_at": "2026-10-08T03:10:13Z",
  "finished_at": "2026-10-08T03:10:26Z",
  "error_message": null
}
```

`task_status` 取值：`queued` / `running` / `success` / `failed` / `cancelled`。
`current_step` 取值：`排队中` / `上下文准备` / `执行分析` / `生成结果` / `完成` / `已取消` / `已失败`。

### `GET /api/tasks/{task_id}/logs` — 任务执行日志（扩展）

| 查询参数 | 默认 | 说明 |
| --- | --- | --- |
| `limit` | 200 | 最多返回条数（上限 1000） |

```json
{
  "task_id": 1, "total": 12,
  "logs": [
    {"log_id": 1, "log_level": "info", "log_type": "step",
     "log_content": "上下文装配完成：历史 0 条、摘要 0 段、附件 0 个",
     "created_at": "2026-10-08T03:10:13Z"}
  ]
}
```

`log_level`：`info` / `warning` / `error`；
`log_type`：`step` / `tool` / `llm` / `system` / `error`。

### `POST /api/tasks/{task_id}/cancel` — 取消分析任务（扩展）

排队中的任务立即置为 `cancelled`；运行中的任务设置协作式取消标记，
引擎会在下一个检查点（工具调用边界）退出。

```json
{"task_id": 1, "task_status": "running", "cancelled": true,
 "message": "已发出取消请求，任务将在当前步骤结束后停止"}
```

### `GET /api/results/{task_id}` — 查询结构化分析结果

返回六段式结果（`key_metrics` 与 `evidence_list` 已完成 JSON 解析）：

```json
{
  "result_id": 1, "task_id": 1, "conversation_id": 1,
  "problem_definition": "近30天各品类的搜索曝光到转化漏斗表现如何？……",
  "key_metrics": [
    {"metric_name": "搜索曝光总量", "metric_value": "12,000", "metric_unit": "次", "metric_period": "近 30 天"}
  ],
  "evidence_list": [
    {"source_type": "数据库查询", "source_name": "catalog.db · 一级类目漏斗统计",
     "evidence_text": "家居拿到全站 37.2% 的搜索曝光……", "related_metric": "家居曝光→转化率",
     "confidence": "high"}
  ],
  "conclusion_text": "主要原因是搜索曝光资源的类目分配与转化能力严重不匹配。……",
  "missing_data_text": "1. 缺少各渠道的广告花费与 ROI 数据……",
  "next_action_text": "1. 重配搜索曝光：……\n2. 修正关键词映射：……",
  "result_markdown": "# 经营归因分析报告\n……",
  "result_file_path": "F:\\...\\data\\exports\\1\\1\\归因分析_report_task1_20261008T031013Z.md",
  "has_file": true,
  "created_at": "2026-10-08T03:10:26Z"
}
```

任务尚未产出结果时返回 `404 {"detail": "该任务尚未生成结构化结果"}`。

### `GET /api/results/{task_id}/download` — 下载结果文件（扩展）

返回 Markdown 报告文件（`Content-Type: text/markdown; charset=utf-8`）。

---

## 5. 系统管理接口

全部接口要求 `role=admin`。

### `POST /api/admin/reload` — 重载系统配置

重新读取 `system_configs` 并重建生效配置，同时失效数据源结构缓存。
**下一个分析任务立即使用新配置，无需重启服务。**

```json
{"status": "ok", "message": "已重载 31 项配置，分析引擎：llm",
 "engine_mode": "llm", "config_count": 31}
```

### `GET /api/admin/config` — 查看当前生效配置（扩展）

```json
{
  "total": 31,
  "engine_mode": "demo",
  "groups": {"llm": [...], "auth": [...], "analysis": [...], "upload": [...]},
  "items": [
    {"config_key": "llm.api_key", "config_value": "******", "config_group": "llm",
     "is_default": false, "overridden_by_env": false, "editable": true}
  ]
}
```

- 敏感项（`api_key` / `client_secret` / `token`）在响应中脱敏为 `******`
- `overridden_by_env=true` 表示该项被**进程环境变量**强制覆盖，
  写入数据库也不会生效，前端将该项渲染为只读

### `POST /api/admin/config` — 修改配置（扩展）

```json
{"configs": {"analysis.engine_mode": "llm", "analysis.max_concurrent_tasks": "3"},
 "auto_reload": true}
```

配置优先级：**进程环境变量 > system_configs 表 > .env 文件 > 内置默认值**；
某一层取到空值视为「该层未设置」，继续向下层回退。

响应：

```json
{"status": "ok", "message": "已更新 2 项配置并即时生效",
 "updated": ["analysis.engine_mode", "analysis.max_concurrent_tasks"],
 "reloaded": true, "overridden_by_env": []}
```

若修改了被环境变量覆盖的项，`message` 会明确提示该值不会生效。

### `GET /api/admin/logs` — 查看运行日志（扩展）

| 查询参数 | 默认 | 说明 |
| --- | --- | --- |
| `limit` | 200 | 最多返回条数（上限 2000） |
| `log_level` | 空 | 按级别过滤：`info` / `warning` / `error` |
| `task_id` | 空 | 按任务过滤；不传则返回系统级与任务级全部日志 |

### `GET /api/admin/datasources` — 数据源连接列表（扩展）

列出可查询的情景数据库及其表结构（`list_databases` 工具的同一数据源）：

```json
{
  "scenario_db_dir": "...\\db\\demo", "directory_exists": true, "total": 6,
  "datasources": [
    {"name": "catalog", "file": "catalog.db", "table_count": 6, "row_total": 14818,
     "tables": [{"name": "products", "columns": ["product_id", "..."], "row_count": 500}],
     "error": null}
  ]
}
```

### `GET /api/admin/stats` — 运行状态概览（扩展）

```json
{
  "server_time": "...", "users": 2, "conversations_total": 3, "conversations_active": 3,
  "messages": 48, "attachments": 2, "tasks_total": 3, "tasks_running": 0, "tasks_failed": 0,
  "results": 2, "summaries": 0, "cleanup_interrupted": 0, "cancelling_tasks": [],
  "websocket": {"conversations": 1, "connections": 1},
  "engine_mode": "demo", "data_dir": "...\\data"
}
```

### `POST /api/admin/selftest` — 分析工具自检（扩展）

逐个执行六个分析工具（数据库查询、文件读写、文本检索、命令执行、库结构查看），
验证分析能力可用。会在 `data/selftest/` 下产生临时文件。

```json
{
  "status": "ok", "message": "工具自检完成：6/6 项通过", "passed": 6, "total": 6,
  "results": [
    {"tool": "db_query", "enabled": true, "ok": true,
     "summary": "{\"database\": \"catalog\", \"columns\": [\"行数\"], \"rows\": [[30]]...}"}
  ]
}
```

### `GET /api/admin/files` — 查看存储目录占用（扩展）

```json
{"directories": [{"kind": "uploads", "path": "...", "files": 2, "bytes": 2359}]}
```

---

## 6. 系统接口

### `GET /api/health` — 健康检查（扩展）

```json
{"status": "ok", "service": "backend", "server_time": "...",
 "engine_mode": "demo", "scenario_db_dir": "...\\db\\demo"}
```

---

## 7. 长连接协议

```
WS /api/chat/ws/chat?websocket_token=<令牌>&conversation_id=<会话ID>
```

### 7.1 连接鉴权

1. 先用已登录的 Cookie 调用 `POST /api/chat/ws-token` 取得一次性令牌
2. 用该令牌建立连接
3. 服务端原子消费令牌（`UPDATE ... WHERE consumed_at IS NULL`），
   并校验令牌绑定的用户与会话是否匹配

失败时服务端**先 accept 再 close**，以便客户端收到有意义的关闭码：

| 关闭码 | 含义 |
| --- | --- |
| 4401 | 令牌无效、已被使用或已过期 |
| 4403 | 令牌对应的用户无权访问该会话 |
| 1011 | 服务端内部异常 |

### 7.2 服务端 → 客户端消息（规格书 1.1.10）

| type | 字段 | 说明 |
| --- | --- | --- |
| `message_start` | `task_id`、`conversation_id` | 本轮分析开始输出 |
| `message_delta` | `task_id`、`delta_text` | 模型增量文本 |
| `tool_start` | `task_id`、`tool_name` | 工具开始执行 |
| `tool_finish` | `task_id`、`tool_name`、`tool_result_summary` | 工具执行完成 |
| `task_status` | `task_id`、`task_status`、`current_step` | 任务状态变化 |
| `result_ready` | `task_id`、`result_id` | 结构化结果已生成 |
| `error` | `task_id`、`error_message` | 本轮分析失败 |
| `done` | `task_id`、`finished_at`（另附 `task_status`） | 本轮分析结束 |

扩展消息类型：

| type | 字段 | 说明 |
| --- | --- | --- |
| `connected` | `conversation_id`、`user_id`、`server_time`、`message_types` | 连接建立后的握手信息 |
| `user_message_accepted` | `message_id`、`seq_no`、`task_id`、`created_at` | 用户消息已受理并创建任务 |
| `cancel_ack` | `task_id`、`cancelled`、`message` | 取消请求回执 |
| `pong` | `server_time` | 心跳响应 |

### 7.3 客户端 → 服务端消息（扩展协议）

规格书只定义了服务端 → 客户端方向，且未提供「发送消息」的 REST 接口，
因此用户消息与取消操作通过长连接上行：

```json
{"type": "user_message", "content": "近30天各品类的搜索曝光到转化漏斗表现如何？", "attachment_ids": [1, 2]}
{"type": "cancel", "task_id": 12}
{"type": "ping"}
```

- `user_message`：创建用户消息 + 分析任务，服务端广播 `user_message_accepted`。
  若该会话已有 `queued` / `running` 任务，则返回 `error` 事件且**不写入任何记录**
  （保证「第 k 条用户消息 ↔ 第 k 个任务」的配对关系不错位）。
- `cancel`：等价于 `POST /api/tasks/{task_id}/cancel`
- `ping`：保活，服务端回 `pong`（客户端每 25 秒发送一次）

### 7.4 断线重连

令牌一次性，因此重连必须重新签发。连接建立后服务端会下发当前任务状态快照：

```json
{"type": "task_status", "task_id": 12, "task_status": "running", "current_step": "执行分析"}
```

断线期间错过的增量文本无法补发，客户端重连后通过 `GET /api/chat/ls/{conversation_id}`
回放历史并与服务端对齐最终状态。

### 7.5 完整事件序列示例

```
→ {"type":"user_message","content":"近30天各品类……"}
← {"type":"user_message_accepted","message_id":1,"seq_no":1,"task_id":1,"created_at":"..."}
← {"type":"task_status","task_id":1,"task_status":"queued","current_step":"排队中"}
← {"type":"task_status","task_id":1,"task_status":"running","current_step":"上下文准备"}
← {"type":"task_status","task_id":1,"task_status":"running","current_step":"执行分析"}
← {"type":"message_start","task_id":1,"conversation_id":1}
← {"type":"message_delta","task_id":1,"delta_text":"第一步：按一级类目统计……"}
← {"type":"tool_start","task_id":1,"tool_name":"db_query"}
← {"type":"tool_finish","task_id":1,"tool_name":"db_query","tool_result_summary":"查询 catalog 返回 5 行，用时 0.02s"}
   …（后续工具调用重复）
← {"type":"task_status","task_id":1,"task_status":"running","current_step":"生成结果"}
← {"type":"result_ready","task_id":1,"result_id":1}
← {"type":"task_status","task_id":1,"task_status":"success","current_step":"完成"}
← {"type":"done","task_id":1,"finished_at":"2026-10-08T03:10:26Z","task_status":"success"}
```

---

## 8. 页面路由

| 路径 | 说明 |
| --- | --- |
| `/` | 授权登录入口页 |
| `/callback.html` | 登录回调页（接收授权结果并跳转工作台） |
| `/app`（别名 `/login`） | 聊天工作台 |
| `/admin` | 系统管理台（仅 `role=admin`） |
| `/css/*`、`/js/*`、`/favicon.ico` | 前端静态资源 |

---

## 9. 认证中心接口（模拟 OAuth2）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/auth/login` | 授权登录页（展示两个演示账号） |
| GET | `/auth/authorize` | 校验 `redirect_uri` 白名单后签发授权码并 302 回调 |
| POST | `/oauth/token` | 授权码换访问令牌（校验 `client_id` / `client_secret`，授权码一次性） |
| GET | `/oauth/userinfo` | 由 `Bearer` 令牌获取用户信息 |
| GET | `/healthz` | 健康检查 |

演示账号：`analyst`（分析用户）、`admin`（系统管理员）。

> 认证中心为演示用实现，账号与凭据存于内存，重启后失效。
> 对接真实认证中心时只需保持 `/oauth/token` 与 `/oauth/userinfo` 两个接口的契约一致，
> 并调整 `auth.auth_center_url`、`auth.client_id`、`auth.client_secret` 与回调地址白名单。
