"""上下文装配与摘要压缩。

多轮追问时上下文会无限增长，因此在「未摘要消息数 ≥ N」或「未摘要字符数 ≥ M」时，
把较早的一段对话压缩成一条 context_summaries 记录，只保留最近若干条消息原文。

摘要窗口的构造严格遵守以下不变式，避免重复或漏摘：
    已有摘要覆盖到 last_end；新窗口取 (last_end, max_seq - recent_n]。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend import config, database
from backend.attachments import parser as attachment_parser
from backend.analysis import tools as analysis_tools
from backend.utils import truncate

SUMMARY_MAX_CHARS = 2000


@dataclass
class AssembledContext:
    """一次分析所需的全部上下文素材。"""

    conversation_title: str
    system_prompt: str
    current_input: str
    recent_messages: list[dict[str, Any]] = field(default_factory=list)
    summaries: list[dict[str, Any]] = field(default_factory=list)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    schema_text: str = ""

    def history_text(self) -> str:
        """把摘要 + 近期消息渲染成纯文本（供规则引擎阅读）。"""
        blocks: list[str] = []
        for item in self.summaries:
            blocks.append(
                f"[历史摘要 {item['start_seq_no']}-{item['end_seq_no']}]\n{item['summary_text']}"
            )
        for message in self.recent_messages:
            speaker = "用户" if message["role"] == "user" else "助手"
            blocks.append(f"{speaker}: {truncate(message['content'], 1200)}")
        return "\n\n".join(blocks)

    def attachments_text(self) -> str:
        if not self.attachments:
            return ""
        blocks = [
            f"[附件: {item['file_name']}（{item['file_type']}，{item['size_text']}）]\n{item['preview']}"
            for item in self.attachments
        ]
        return "\n\n".join(blocks)


def make_system_prompt(*, schema_text: str, enabled_tools: list[str], engine_mode: str) -> str:
    tool_lines = "、".join(enabled_tools) if enabled_tools else "（当前未启用任何工具）"
    return f"""你是「经营归因分析系统」的资深经营分析师，负责回答业务方的经营归因问题。

工作方式：
1. 先理解业务问题，明确要回答什么、需要哪些数据支撑。
2. 用 list_databases 了解可用的数据表结构，再用 db_query 执行只读 SQL 取数；
   必要时用 file_read / text_search 查看用户上传的附件，用 file_write 记录中间分析笔记。
3. 所有结论都必须有数据支撑：引用的数字要能追溯到具体查询或附件内容。
4. 证据要标注来源（source_type / source_name）与置信度（high / medium / low）。
5. 数据不足以支撑判断时，如实写入「待补充数据」，不要编造。

取数纪律（很重要）：
- 聚焦问题直接相关的表，不要为了「看得更全」而遍历所有数据库；
- 单个问题最多做 6 轮工具调用，够用即可，不要把轮次耗尽；
- 每一轮请先用一句话说明你正在做什么、为什么，再调用工具，便于用户看到分析进展；
- 取数完成后必须立刻调用 submit_result 提交结构化结论，不要反复补充查询。

可用工具：{tool_lines}

可查询的数据库结构：
{schema_text or "（当前没有可查询的情景数据库）"}

submit_result 需包含六个部分：
问题定义、关键指标、证据列表、归因结论、待补充数据、下一步建议（至少两条）。
下一步建议必须是可执行的动作，不要写空话。全部使用中文作答。

当前分析引擎：{engine_mode}
"""


# ---------------------------------------------------------------- 摘要压缩


def _extractive_summary(messages: list[dict[str, Any]]) -> str:
    """不依赖模型的信息抽取式摘要：保留每轮问与答的要点。"""
    lines: list[str] = []
    for message in messages:
        content = " ".join((message["content"] or "").split())
        if not content:
            continue
        if message["role"] == "user":
            lines.append(f"- 用户提问：{truncate(content, 80)}")
        else:
            lines.append(f"- 分析结论：{truncate(content, 160)}")
    text = "\n".join(lines)
    return truncate(text, SUMMARY_MAX_CHARS) or "（无有效内容）"


async def _llm_summary(messages: list[dict[str, Any]]) -> str:
    """用大模型生成摘要；任何失败都由调用方回退到抽取式摘要。"""
    from backend.analysis.llm_engine import complete_once  # 延迟导入，避免循环依赖

    transcript = "\n".join(
        f"{'用户' if m['role'] == 'user' else '助手'}: {truncate(m['content'] or '', 800)}"
        for m in messages
    )
    prompt = (
        "请把下面这段经营分析对话压缩成要点摘要，保留：用户的业务问题、已得出的结论、"
        "关键数字与口径、以及尚未解决的问题。使用中文，控制在 400 字以内，不要添加新结论。\n\n"
        f"{transcript}"
    )
    text = await complete_once(prompt, max_tokens=800)
    return truncate(text.strip(), SUMMARY_MAX_CHARS) or _extractive_summary(messages)


async def maybe_summarize(conversation_id: int, engine_mode: str) -> int | None:
    """按阈值触发摘要压缩，返回新建摘要的 id（未触发返回 None）。"""
    last_end = await database.fetch_value(
        "SELECT COALESCE(MAX(end_seq_no), 0) FROM context_summaries WHERE conversation_id = ?",
        (conversation_id,),
    )
    last_end = int(last_end or 0)

    rows = await database.fetch_all(
        "SELECT seq_no, role, content FROM messages"
        " WHERE conversation_id = ? AND seq_no > ? AND role IN ('user', 'assistant')"
        " ORDER BY seq_no ASC",
        (conversation_id, last_end),
    )
    if not rows:
        return None

    messages = [dict(r) for r in rows]
    if len(messages) < 2:
        return None

    threshold_count = max(2, config.get_int("analysis.summarize_after_messages", 12))
    threshold_chars = config.get_int("analysis.summarize_after_chars", 24000)
    total_chars = sum(len(m["content"] or "") for m in messages)
    if len(messages) < threshold_count and total_chars < threshold_chars:
        return None

    recent_n = config.get_int("analysis.context_recent_messages", 10)
    window = messages[: max(1, len(messages) - recent_n)]
    if not window:
        return None

    if engine_mode == "llm" and config.get("llm.api_key"):
        try:
            summary_text = await _llm_summary(window)
        except Exception:
            summary_text = _extractive_summary(window)
    else:
        summary_text = _extractive_summary(window)

    from backend.utils import utc_now_iso

    summary_id, _ = await database.execute(
        "INSERT INTO context_summaries (conversation_id, start_seq_no, end_seq_no, summary_text, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            conversation_id,
            window[0]["seq_no"],
            window[-1]["seq_no"],
            summary_text,
            utc_now_iso(),
        ),
    )
    return summary_id


# ---------------------------------------------------------------- 上下文装配


async def _load_attachments(conversation_id: int) -> list[dict[str, Any]]:
    max_count = config.get_int("analysis.max_attachments_in_context", 5)
    max_chars = config.get_int("analysis.max_attachment_chars", 4000)

    rows = await database.fetch_all(
        "SELECT * FROM attachments WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
        (conversation_id, max_count * 3),
    )
    from backend.storage import human_size

    collected: list[dict[str, Any]] = []
    for row in rows:
        if len(collected) >= max_count:
            break
        preview, status = attachment_parser.load_preview(row["file_path"], max_chars)
        if status != "success" or not preview:
            continue
        collected.append(
            {
                "attachment_id": row["id"],
                "file_name": row["file_name"],
                "file_type": row["file_type"],
                "preview": preview,
                "size_text": human_size(row["file_size"]),
            }
        )
    return collected


async def assemble_context(
    *,
    conversation_id: int,
    conversation_title: str,
    current_input: str,
    before_seq_no: int | None = None,
    engine_mode: str,
) -> AssembledContext:
    """装配本次分析的上下文。before_seq_no 用于排除当前这条用户消息。"""
    recent_n = config.get_int("analysis.context_recent_messages", 10)

    summary_rows = await database.fetch_all(
        "SELECT start_seq_no, end_seq_no, summary_text FROM context_summaries"
        " WHERE conversation_id = ? ORDER BY end_seq_no ASC",
        (conversation_id,),
    )
    summaries = [dict(r) for r in summary_rows]

    if before_seq_no is None:
        recent_rows = await database.fetch_all(
            "SELECT seq_no, role, content FROM messages"
            " WHERE conversation_id = ? AND role IN ('user', 'assistant')"
            " ORDER BY seq_no DESC LIMIT ?",
            (conversation_id, recent_n),
        )
        recent = [dict(r) for r in reversed(recent_rows)]
    else:
        recent_rows = await database.fetch_all(
            "SELECT seq_no, role, content FROM messages"
            " WHERE conversation_id = ? AND seq_no < ? AND role IN ('user', 'assistant')"
            " ORDER BY seq_no DESC LIMIT ?",
            (conversation_id, before_seq_no, recent_n),
        )
        recent = [dict(r) for r in reversed(recent_rows)]

    attachments = await _load_attachments(conversation_id)
    schema_text = await _schema_text()
    enabled = analysis_tools.enabled_tool_names()

    return AssembledContext(
        conversation_title=conversation_title,
        system_prompt=make_system_prompt(
            schema_text=schema_text, enabled_tools=enabled, engine_mode=engine_mode
        ),
        current_input=current_input,
        recent_messages=recent,
        summaries=summaries,
        attachments=attachments,
        schema_text=schema_text,
    )


async def _schema_text() -> str:
    import asyncio

    return await asyncio.to_thread(analysis_tools.schema_text)
