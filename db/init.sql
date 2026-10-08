-- 经营归因分析系统 — 系统库 DDL（SQLite）
-- 10 张规格表，列名与规格书 1.1.7 一致，不额外增加列。
-- 所有时间字段统一存 UTC ISO-8601 字符串（如 2026-10-08T03:12:05Z），前端负责转本地时区。

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 用户表：由认证中心首次登录时自动建档
CREATE TABLE IF NOT EXISTS users (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    external_user_id TEXT    NOT NULL UNIQUE,              -- 认证中心用户标识（用户名）
    username         TEXT    NOT NULL,
    display_name     TEXT    NOT NULL,
    role             TEXT    NOT NULL DEFAULT 'analyst'    -- 'admin' | 'analyst'
                     CHECK (role IN ('admin', 'analyst')),
    status           TEXT    NOT NULL DEFAULT 'active'     -- 'active' | 'disabled'
                     CHECK (status IN ('active', 'disabled')),
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL
);

-- 会话表：删除为软删除（status='deleted'），子表记录级联物理删除
CREATE TABLE IF NOT EXISTS conversations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    title           TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'active'      -- 'active' | 'archived' | 'deleted'
                    CHECK (status IN ('active', 'archived', 'deleted')),
    last_message_at TEXT,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

-- 消息表：seq_no 为会话内严格递增序号，决定回放顺序
-- role: 'user' | 'assistant' | 'tool'；message_type: 'text' | 'tool_result'
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    role            TEXT    NOT NULL CHECK (role IN ('user', 'assistant', 'tool')),
    message_type    TEXT    NOT NULL CHECK (message_type IN ('text', 'tool_result')),
    content         TEXT    NOT NULL,
    tool_name       TEXT,
    tool_status     TEXT    CHECK (tool_status IS NULL OR tool_status IN ('success', 'error')),
    seq_no          INTEGER NOT NULL,
    created_at      TEXT    NOT NULL
);

-- 附件表：message_id 为空表示仅存在于侧栏、尚未随消息发送
CREATE TABLE IF NOT EXISTS attachments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    message_id      INTEGER REFERENCES messages(id),
    file_name       TEXT    NOT NULL,
    file_path       TEXT    NOT NULL,                      -- uploads/{user_id}/{conversation_id}/ 下的绝对路径
    file_type       TEXT    NOT NULL,
    file_size       INTEGER NOT NULL,
    parse_status    TEXT    NOT NULL DEFAULT 'pending'     -- 'pending' | 'parsing' | 'success' | 'failed'
                    CHECK (parse_status IN ('pending', 'parsing', 'success', 'failed')),
    created_at      TEXT    NOT NULL
);

-- 分析任务表：一条用户消息对应一次分析任务
CREATE TABLE IF NOT EXISTS analysis_tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    user_id         INTEGER NOT NULL REFERENCES users(id),
    input_text      TEXT    NOT NULL,
    task_status     TEXT    NOT NULL DEFAULT 'queued'      -- 'queued' | 'running' | 'success' | 'failed' | 'cancelled'
                    CHECK (task_status IN ('queued', 'running', 'success', 'failed', 'cancelled')),
    current_step    TEXT,
    started_at      TEXT,
    finished_at     TEXT,
    error_message   TEXT
);

-- 分析结果表：每个任务至多一条结构化结果
CREATE TABLE IF NOT EXISTS analysis_results (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id            INTEGER NOT NULL UNIQUE REFERENCES analysis_tasks(id),
    conversation_id    INTEGER NOT NULL REFERENCES conversations(id),
    problem_definition TEXT NOT NULL,
    key_metrics_json   TEXT NOT NULL,                      -- [{metric_name, metric_value, metric_unit, metric_period}]
    evidence_list_json TEXT NOT NULL,                      -- [{source_type, source_name, evidence_text, related_metric, confidence}]
    conclusion_text    TEXT NOT NULL,
    missing_data_text  TEXT NOT NULL,
    next_action_text   TEXT NOT NULL,
    result_markdown    TEXT NOT NULL,
    result_file_path   TEXT,                               -- exports/{user_id}/{conversation_id}/ 下的绝对路径
    created_at         TEXT NOT NULL
);

-- 上下文摘要表：多轮对话历史压缩结果，避免上下文无限增长
CREATE TABLE IF NOT EXISTS context_summaries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    start_seq_no    INTEGER NOT NULL,
    end_seq_no      INTEGER NOT NULL,
    summary_text    TEXT    NOT NULL,
    created_at      TEXT    NOT NULL
);

-- 长连接临时令牌表：单次消费（consumed_at 非空即失效）
CREATE TABLE IF NOT EXISTS websocket_tokens (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    token           TEXT    NOT NULL UNIQUE,
    expires_at      TEXT    NOT NULL,
    consumed_at     TEXT,
    created_at      TEXT    NOT NULL
);

-- 系统配置表：config_group 分组，支持热更新
CREATE TABLE IF NOT EXISTS system_configs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    config_key   TEXT NOT NULL UNIQUE,
    config_value TEXT NOT NULL,
    config_group TEXT NOT NULL                            -- 'llm' | 'auth' | 'analysis' | 'upload'
                 CHECK (config_group IN ('llm', 'auth', 'analysis', 'upload')),
    updated_at   TEXT NOT NULL
);

-- 任务日志表：log_type 区分步骤/工具/模型/系统/错误
-- task_id 可为空：系统级日志（配置重载、工具自检等）不属于任何分析任务
CREATE TABLE IF NOT EXISTS task_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER REFERENCES analysis_tasks(id),
    log_level   TEXT    NOT NULL CHECK (log_level IN ('info', 'warning', 'error')),
    log_type    TEXT    NOT NULL CHECK (log_type IN ('step', 'tool', 'llm', 'system', 'error')),
    log_content TEXT    NOT NULL,
    created_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_conv_seq ON messages(conversation_id, seq_no);
CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id, status, last_message_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_conv_status ON analysis_tasks(conversation_id, task_status);
CREATE INDEX IF NOT EXISTS idx_tasks_conv_id ON analysis_tasks(conversation_id, id);
CREATE INDEX IF NOT EXISTS idx_attachments_conv ON attachments(conversation_id);
CREATE INDEX IF NOT EXISTS idx_summaries_conv ON context_summaries(conversation_id, end_seq_no);
CREATE INDEX IF NOT EXISTS idx_task_logs_task ON task_logs(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_ws_tokens_token ON websocket_tokens(token);
