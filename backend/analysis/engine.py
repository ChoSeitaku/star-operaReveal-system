"""分析引擎抽象层。

引擎（demo / llm / canned）只通过 EngineContext 与外界交互：发增量文本、报工具调用、
写日志、检查取消、提交结构化结果。引擎自身不碰数据库、WebSocket 与文件系统路径，
因此运行器（tasks/runner.py）可以用同一套落库与推送逻辑承载所有引擎。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

# 结构化结果的六个部分（规格书 1.1.11）
RESULT_SECTIONS = (
    "problem_definition",
    "key_metrics",
    "evidence_list",
    "conclusion_text",
    "missing_data_text",
    "next_action_text",
)


@dataclass
class ResultData:
    """引擎产出的结构化结果。"""

    problem_definition: str = ""
    key_metrics: list[dict[str, Any]] = field(default_factory=list)
    evidence_list: list[dict[str, Any]] = field(default_factory=list)
    conclusion_text: str = ""
    missing_data_text: str = ""
    next_action_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in RESULT_SECTIONS}


class EngineContext:
    """引擎运行上下文：所有对外副作用的唯一出口。

    由 TaskRunner 构造，回调均已绑定到具体任务（WS 推送、消息落库、任务日志）。
    """

    def __init__(
        self,
        *,
        task_id: int,
        conversation_id: int,
        user_id: int,
        user_input: str,
        system_prompt: str,
        available_tools: list[str],
        callbacks: "EngineCallbacks",
    ) -> None:
        self.task_id = task_id
        self.conversation_id = conversation_id
        self.user_id = user_id
        self.user_input = user_input
        self.system_prompt = system_prompt
        self.available_tools = available_tools
        self._cb = callbacks

    # ---- 输出 ----

    async def text(self, delta: str) -> None:
        """推送一段模型增量文本。"""
        if delta:
            await self._cb.emit_text(delta)

    async def tool_start(self, tool_name: str) -> None:
        await self._cb.emit_tool_start(tool_name)

    async def tool_finish(self, tool_name: str, summary: str, result: str, status: str = "success") -> None:
        await self._cb.emit_tool_finish(tool_name, summary, result, status)

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """统一工具调用入口：自动推送 tool_start / tool_finish 并落地工具消息。"""
        return await self._cb.call_tool(tool_name, arguments)

    async def log(self, level: str, log_type: str, content: str) -> None:
        await self._cb.log(level, log_type, content)

    async def step(self, current_step: str) -> None:
        """更新任务当前步骤（推 task_status）。"""
        await self._cb.set_step(current_step)

    def cancelled(self) -> bool:
        return self._cb.is_cancelled()

    async def raise_if_cancelled(self) -> None:
        if self._cb.is_cancelled():
            from backend.tasks.state import TaskCancelled

            raise TaskCancelled()

    async def finalize(self, result: ResultData) -> None:
        await self._cb.finalize(result)


class EngineCallbacks:
    """EngineContext 依赖的回调集合（由 TaskRunner 实现）。"""

    async def emit_text(self, delta: str) -> None:  # pragma: no cover - 抽象
        raise NotImplementedError

    async def emit_tool_start(self, tool_name: str) -> None:  # pragma: no cover
        raise NotImplementedError

    async def emit_tool_finish(self, tool_name: str, summary: str, result: str, status: str) -> None:  # pragma: no cover
        raise NotImplementedError

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:  # pragma: no cover
        raise NotImplementedError

    async def log(self, level: str, log_type: str, content: str) -> None:  # pragma: no cover
        raise NotImplementedError

    async def set_step(self, current_step: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def is_cancelled(self) -> bool:  # pragma: no cover
        raise NotImplementedError

    async def finalize(self, result: ResultData) -> None:  # pragma: no cover
        raise NotImplementedError


class AnalysisEngine(ABC):
    """分析引擎接口。"""

    name = "base"

    @abstractmethod
    async def run(self, ctx: EngineContext) -> None:
        """执行分析。结果通过 ctx.finalize() 提交，不得直接返回。"""


def build_engine(mode: str) -> AnalysisEngine:
    """按配置构造引擎。

    demo   —— 规则化演示引擎（默认，无需 API Key）
    llm    —— 大模型 Agent 引擎
    canned —— 固定输出的最小引擎，仅用于链路自检
    """
    normalized = (mode or "demo").strip().lower()
    if normalized == "llm":
        from backend.analysis.llm_engine import LLMEngine

        return LLMEngine()
    if normalized == "canned":
        from backend.analysis.canned_engine import CannedEngine

        return CannedEngine()
    from backend.analysis.demo_engine import DemoEngine

    return DemoEngine()
