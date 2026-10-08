"""任务运行器。

职责：推进状态机、把引擎产出推送为规格书 1.1.10 定义的实时消息、把消息与结果落库、
处理协作式取消、生成导出文件。引擎只通过 EngineContext 与这里交互。

不变式（务必保持）：
- 一条用户消息对应一个任务；无论成功、失败还是取消，任务都会落一条最终助手消息，
  因此「第 k 条用户消息 ↔ 第 k 个任务」的配对关系永不错位。
- 序号 seq_no 在会话内严格递增，分配发生在会话锁内，保证顺序确定。
- 任务运行中若会话被删除，所有后续落库动作都会静默跳过，不会复活数据。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from backend import config, conversations, database, storage
from backend.analysis import context as ctx_builder
from backend.analysis import result_builder, tools as analysis_tools
from backend.analysis.engine import EngineCallbacks, EngineContext, ResultData, build_engine
from backend.tasks.state import (
    STEP_ANALYZING,
    STEP_CANCELLED,
    STEP_CONTEXT,
    STEP_DONE,
    STEP_FAILED,
    STEP_RESULT,
    TaskCancelled,
    TaskStatus,
    cancel_registry,
    can_transition,
)
from backend.utils import truncate, utc_now_iso
from backend.ws.manager import manager as ws_manager

TOOL_SUMMARY_LIMIT = 300
MAX_IDLE_TEXT = "（本轮分析未产生文本输出）"

_semaphore: asyncio.Semaphore | None = None

# 最近一次启动时被标记为中断的任务数（供管理台展示）
LAST_CLEANUP_COUNT = 0


def task_semaphore() -> asyncio.Semaphore:
    """全局并发闸门：决定 queued → running 的放行时机，让 queued 状态有实际意义。"""
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(max(1, config.get_int("analysis.max_concurrent_tasks", 2)))
    return _semaphore


def reset_semaphore() -> None:
    """配置重载后重建闸门（仅在无任务运行时应调用）。"""
    global _semaphore
    _semaphore = None


class TaskRunner(EngineCallbacks):
    def __init__(self, task_id: int) -> None:
        self.task_id = task_id
        self.task: dict[str, Any] | None = None
        self.conversation: dict[str, Any] | None = None

        self._text_parts: list[str] = []
        self._message_started = False
        self._result_row: dict[str, Any] | None = None
        self._tool_ctx: analysis_tools.ToolContext | None = None

    # ------------------------------------------------------------ 主流程

    async def run(self) -> None:
        try:
            task = await database.fetch_one("SELECT * FROM analysis_tasks WHERE id = ?", (self.task_id,))
            if task is None:
                return
            self.task = dict(task)

            conversation = await database.fetch_one(
                "SELECT * FROM conversations WHERE id = ?", (self.task["conversation_id"],)
            )
            if conversation is None or conversation["status"] == "deleted":
                cancel_registry.clear(self.task_id)
                return
            self.conversation = dict(conversation)

            async with task_semaphore():
                # 排队期间任务可能已被取消。cancel_task 会先置取消标记再尝试改库，
                # _try_start 又是「仅当仍为 queued 才置 running」的条件更新，
                # 因此两者竞争的结果必然是其中一方成功，done 事件只会有一次。
                if cancel_registry.is_requested(self.task_id):
                    return
                if not await self._try_start():
                    return
                try:
                    await self._execute()
                except TaskCancelled:
                    await self._finish(TaskStatus.CANCELLED, STEP_CANCELLED, "任务已取消")
                except Exception as exc:
                    await self._finish(TaskStatus.FAILED, STEP_FAILED, f"{type(exc).__name__}: {exc}")
        finally:
            cancel_registry.clear(self.task_id)

    async def _execute(self) -> None:
        assert self.task is not None and self.conversation is not None
        conversation_id = self.conversation["id"]
        user_id = self.task["user_id"]
        engine_mode = config.get("analysis.engine_mode", "demo")

        # 1) 摘要压缩 + 上下文装配
        try:
            summary_id = await ctx_builder.maybe_summarize(conversation_id, engine_mode)
            if summary_id:
                await self.log("info", "system", f"已生成历史摘要 #{summary_id}")
        except Exception as exc:
            await self.log("warning", "system", f"历史摘要生成失败（已跳过）：{exc}")

        assembled = await ctx_builder.assemble_context(
            conversation_id=conversation_id,
            conversation_title=self.conversation["title"],
            current_input=self.task["input_text"],
            before_seq_no=await self._current_seq_no(),
            engine_mode=engine_mode,
        )

        dirs = storage.ensure_conv_dirs(user_id, conversation_id)
        self._tool_ctx = analysis_tools.ToolContext(
            workspace_dir=dirs["workspace"],
            upload_dir=dirs["uploads"],
            enabled=analysis_tools.enabled_tool_names(),
        )

        await self.log("info", "step", f"上下文装配完成：历史 {len(assembled.recent_messages)} 条、"
                                      f"摘要 {len(assembled.summaries)} 段、附件 {len(assembled.attachments)} 个")

        await self.raise_if_cancelled()
        await self.set_step(STEP_ANALYZING)

        # 2) 执行引擎
        engine = build_engine(engine_mode)
        engine_ctx = EngineContext(
            task_id=self.task_id,
            conversation_id=conversation_id,
            user_id=user_id,
            user_input=self.task["input_text"],
            system_prompt=assembled.system_prompt,
            available_tools=analysis_tools.enabled_tool_names(),
            callbacks=self,
        )
        engine_dispatch = getattr(engine, "set_assembled", None)
        if callable(engine_dispatch):
            engine_dispatch(assembled)
        await self.log("info", "step", f"分析引擎启动：{engine.name}")
        await engine.run(engine_ctx)

        if self._result_row is None:
            raise RuntimeError("分析引擎未提交结构化结果")

        await self.raise_if_cancelled()

        # 3) 落最终助手消息
        content = "".join(self._text_parts).strip()
        if not content:
            content = self._result_fallback_text()
        await self._append_message(role="assistant", content=content, message_type="text")

        await self._finish(TaskStatus.SUCCESS, STEP_DONE, None)

    # ------------------------------------------------------------ 状态流转

    async def _try_start(self) -> bool:
        """原子地把任务从 queued 推进到 running。

        条件更新保证与「排队中取消」互斥：谁先改成功谁负责收尾，
        另一方拿到 0 行受影响后直接退出，避免重复的 done 事件与状态回退。
        """
        assert self.task is not None
        now = utc_now_iso()
        _rowid, affected = await database.execute(
            "UPDATE analysis_tasks SET task_status = 'running', current_step = ?,"
            " started_at = COALESCE(started_at, ?), error_message = NULL"
            " WHERE id = ? AND task_status = 'queued'",
            (STEP_CONTEXT, now, self.task_id),
        )
        if affected != 1:
            return False

        self.task["task_status"] = TaskStatus.RUNNING.value
        self.task["current_step"] = STEP_CONTEXT
        self.task["started_at"] = self.task.get("started_at") or now
        await self._push(
            {
                "type": "task_status",
                "task_id": self.task_id,
                "task_status": TaskStatus.RUNNING.value,
                "current_step": STEP_CONTEXT,
            }
        )
        return True

    async def _transition(
        self,
        target: TaskStatus,
        step: str,
        *,
        error_message: str | None = None,
        finished: bool = False,
    ) -> None:
        assert self.task is not None
        # 以数据库中的当前状态为准做流转校验，避免用缓存状态覆盖掉并发写入
        # （例如排队中被取消后，运行器不得再把状态改回 running）
        row = await database.fetch_one(
            "SELECT task_status FROM analysis_tasks WHERE id = ?", (self.task_id,)
        )
        current = row["task_status"] if row else self.task["task_status"]
        if current == target.value:
            return
        if not can_transition(current, target.value):
            raise RuntimeError(f"非法状态流转：{current} → {target.value}")

        now = utc_now_iso()
        assignments = ["task_status = ?", "current_step = ?", "error_message = ?"]
        params: list[Any] = [target.value, step, error_message]
        if target is TaskStatus.RUNNING:
            assignments.append("started_at = COALESCE(started_at, ?)")
            params.append(now)
        if finished:
            assignments.append("finished_at = ?")
            params.append(now)
        params.append(self.task_id)

        await database.execute(
            f"UPDATE analysis_tasks SET {', '.join(assignments)} WHERE id = ?", params
        )
        self.task["task_status"] = target.value
        self.task["current_step"] = step

        await self._push(
            {
                "type": "task_status",
                "task_id": self.task_id,
                "task_status": target.value,
                "current_step": step,
            }
        )

    async def _finish(self, status: TaskStatus, step: str, error_message: str | None) -> None:
        await self._transition(status, step, error_message=error_message, finished=True)

        if status is TaskStatus.FAILED:
            await self.log("error", "error", error_message or "任务失败")
            await self._push(
                {
                    "type": "error",
                    "task_id": self.task_id,
                    "error_message": error_message or "任务失败",
                }
            )
            # 失败也要落一条助手消息，保持「用户消息 ↔ 任务」配对不变
            await self._append_message(
                role="assistant",
                content=f"本轮分析未能完成：{error_message or '未知错误'}",
                message_type="text",
            )
        elif status is TaskStatus.CANCELLED:
            await self.log("warning", "step", "任务被用户取消")
            await self._append_message(
                role="assistant", content="本轮分析已取消。", message_type="text"
            )

        assert self.task is not None
        await self._push(
            {
                "type": "done",
                "task_id": self.task_id,
                "finished_at": utc_now_iso(),
                "task_status": status.value,
            }
        )

    # ------------------------------------------------------------ 落库

    async def _conversation_alive(self) -> bool:
        if self.conversation is None:
            return False
        row = await database.fetch_one(
            "SELECT status FROM conversations WHERE id = ?", (self.conversation["id"],)
        )
        return bool(row and row["status"] != "deleted")

    async def _current_seq_no(self) -> int | None:
        row = await database.fetch_one(
            "SELECT id FROM messages WHERE conversation_id = ? AND role = 'user'"
            " ORDER BY seq_no DESC LIMIT 1",
            (self.conversation["id"],),
        )
        if row is None:
            return None
        seq = await database.fetch_value("SELECT seq_no FROM messages WHERE id = ?", (row["id"],))
        return int(seq) if seq is not None else None

    async def _append_message(
        self,
        *,
        role: str,
        content: str,
        message_type: str = "text",
        tool_name: str | None = None,
        tool_status: str | None = None,
    ) -> int | None:
        """在会话锁保护下分配 seq_no 并写入消息；会话已删除则跳过。"""
        if not await self._conversation_alive():
            return None
        conversation_id = self.conversation["id"]
        lock = await conversations_lock(conversation_id)
        async with lock:
            async with database.transaction() as conn:
                cursor = await conn.execute(
                    "SELECT status FROM conversations WHERE id = ?", (conversation_id,)
                )
                row = await cursor.fetchone()
                await cursor.close()
                if row is None or row["status"] == "deleted":
                    return None

                seq_no = await conversations.allocate_seq_no(conn, conversation_id)
                cursor = await conn.execute(
                    "INSERT INTO messages (conversation_id, role, message_type, content,"
                    " tool_name, tool_status, seq_no, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        conversation_id,
                        role,
                        message_type,
                        content,
                        tool_name,
                        tool_status,
                        seq_no,
                        utc_now_iso(),
                    ),
                )
                message_id = cursor.lastrowid
                await cursor.close()
        return message_id

    # ------------------------------------------------------------ EngineCallbacks

    async def emit_text(self, delta: str) -> None:
        if not self._message_started:
            self._message_started = True
            await self._push(
                {
                    "type": "message_start",
                    "task_id": self.task_id,
                    "conversation_id": self.conversation["id"] if self.conversation else 0,
                }
            )
        self._text_parts.append(delta)
        await self._push({"type": "message_delta", "task_id": self.task_id, "delta_text": delta})

    async def emit_tool_start(self, tool_name: str) -> None:
        await self._push({"type": "tool_start", "task_id": self.task_id, "tool_name": tool_name})

    async def emit_tool_finish(self, tool_name: str, summary: str, result: str, status: str) -> None:
        await self._push(
            {
                "type": "tool_finish",
                "task_id": self.task_id,
                "tool_name": tool_name,
                "tool_result_summary": summary,
            }
        )
        await self._append_message(
            role="tool",
            content=truncate(result, 20000),
            message_type="tool_result",
            tool_name=tool_name,
            tool_status=status,
        )

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        if self._tool_ctx is None:
            return "错误：工具上下文未初始化"

        await self.emit_tool_start(tool_name)
        started = time.perf_counter()
        result = await analysis_tools.execute_tool(tool_name, arguments, self._tool_ctx)
        elapsed = time.perf_counter() - started

        status = "error" if result.startswith("错误：") else "success"
        summary = self._tool_summary(tool_name, arguments, result, elapsed)
        await self.emit_tool_finish(tool_name, summary, result, status)
        await self.log(
            "info" if status == "success" else "warning",
            "tool",
            f"{tool_name} 用时 {elapsed:.2f}s：{summary}",
        )
        return result

    async def log(self, level: str, log_type: str, content: str) -> None:
        try:
            await database.execute(
                "INSERT INTO task_logs (task_id, log_level, log_type, log_content, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (self.task_id, level, log_type, truncate(content, 4000), utc_now_iso()),
            )
        except Exception:
            pass  # 日志失败不应影响分析

    async def set_step(self, current_step: str) -> None:
        if self.task is None:
            return
        await database.execute(
            "UPDATE analysis_tasks SET current_step = ? WHERE id = ?",
            (current_step, self.task_id),
        )
        self.task["current_step"] = current_step
        await self._push(
            {
                "type": "task_status",
                "task_id": self.task_id,
                "task_status": self.task["task_status"],
                "current_step": current_step,
            }
        )

    def is_cancelled(self) -> bool:
        return cancel_registry.is_requested(self.task_id)

    async def raise_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise TaskCancelled()

    async def finalize(self, result: ResultData) -> None:
        assert self.task is not None and self.conversation is not None
        if self._result_row is not None:
            return  # 幂等：引擎重复提交只取第一次

        await self.set_step(STEP_RESULT)
        if not await self._conversation_alive():
            raise TaskCancelled()

        self._result_row = await result_builder.persist_result(
            task_id=self.task_id,
            conversation_id=self.conversation["id"],
            user_id=self.task["user_id"],
            conversation_title=self.conversation["title"],
            user_input=self.task["input_text"],
            engine_mode=config.get("analysis.engine_mode", "demo"),
            raw_result=result,
        )
        await self.log("info", "step", f"结构化结果已生成：{result_builder.result_summary(self._result_row['result'])}")
        await self._push(
            {
                "type": "result_ready",
                "task_id": self.task_id,
                "result_id": self._result_row["result_id"],
            }
        )

    # ------------------------------------------------------------ 辅助

    async def _push(self, payload: dict[str, Any]) -> None:
        if self.conversation is None:
            return
        try:
            await ws_manager.broadcast(self.conversation["id"], payload)
        except Exception:
            pass  # 推送失败不影响任务推进

    def _tool_summary(
        self, tool_name: str, arguments: dict[str, Any], result: str, elapsed: float
    ) -> str:
        if result.startswith("错误："):
            return truncate(result, TOOL_SUMMARY_LIMIT)
        if tool_name == "db_query":
            try:
                import json

                payload = json.loads(result)
                suffix = "（已截断）" if payload.get("truncated") else ""
                return (
                    f"查询 {payload.get('database')} 返回 {payload.get('row_count')} 行"
                    f"{suffix}，用时 {elapsed:.2f}s"
                )
            except Exception:
                pass
        if tool_name == "list_databases":
            try:
                import json

                return f"发现 {len(json.loads(result))} 个数据库"
            except Exception:
                pass
        if tool_name == "file_write":
            try:
                import json

                return f"已写入 {json.loads(result).get('relative_path')}"
            except Exception:
                pass
        if tool_name == "file_read":
            try:
                import json

                payload = json.loads(result)
                return f"已读取 {payload.get('file_name')}（{payload.get('char_count')} 字符）"
            except Exception:
                pass
        if tool_name == "text_search":
            try:
                import json

                payload = json.loads(result)
                return (
                    f"在 {len(payload.get('searched_files') or [])} 个文件中命中 "
                    f"{payload.get('hit_count')} 处"
                )
            except Exception:
                pass
        if tool_name == "run_command":
            try:
                import json

                payload = json.loads(result)
                return f"退出码 {payload.get('exit_code')}，用时 {elapsed:.2f}s"
            except Exception:
                pass
        return truncate(result.replace("\n", " "), TOOL_SUMMARY_LIMIT)

    def _result_fallback_text(self) -> str:
        if self._result_row is not None:
            result: ResultData = self._result_row["result"]
            parts = [result.problem_definition]
            if result.conclusion_text:
                parts.append(result.conclusion_text)
            return "\n\n".join(parts)
        return MAX_IDLE_TEXT


async def conversations_lock(conversation_id: int) -> asyncio.Lock:
    from backend.tasks.state import conversation_locks

    return await conversation_locks.get(conversation_id)
