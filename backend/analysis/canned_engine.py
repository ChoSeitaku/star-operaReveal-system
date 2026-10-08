"""链路自检引擎（engine_mode=canned）。

不依赖外部模型、也不依赖情景数据，输出固定的增量文本与结构化结果，
用于验证「引擎 → WS 实时事件 → 消息落库 → 结果落盘」整条链路是否通畅。
"""
from __future__ import annotations

import asyncio

from backend.analysis.engine import AnalysisEngine, EngineContext, ResultData

_SCRIPT = [
    "收到分析请求，开始梳理问题。\n",
    "正在查看可用的数据源……\n",
    "正在核对上下文与附件……\n",
    "已完成链路自检，下面给出结构化结果。\n",
]

# 单步延时：让任务持续足够长，便于验证并发控制与取消（协作式取消在检查点生效）
_STEP_DELAY = 0.15


class CannedEngine(AnalysisEngine):
    name = "canned"

    async def run(self, ctx: EngineContext) -> None:
        for line in _SCRIPT:
            await ctx.raise_if_cancelled()
            await ctx.text(line)
            await asyncio.sleep(_STEP_DELAY)

        # 走一次真实工具调用，验证工具链路与 tool_start/tool_finish 事件
        await ctx.raise_if_cancelled()
        tool_output = await ctx.call_tool("list_databases", {})
        await ctx.log("info", "llm", f"自检工具返回 {len(tool_output)} 字符")

        await ctx.raise_if_cancelled()
        await ctx.finalize(
            ResultData(
                problem_definition="链路自检：确认分析任务可以完整跑通并产出结构化结果。",
                key_metrics=[
                    {
                        "metric_name": "自检工具返回字符数",
                        "metric_value": str(len(tool_output)),
                        "metric_unit": "字符",
                        "metric_period": "本次运行",
                    }
                ],
                evidence_list=[
                    {
                        "source_type": "工具调用",
                        "source_name": "list_databases",
                        "evidence_text": f"工具正常返回，内容长度 {len(tool_output)} 字符。",
                        "related_metric": "自检工具返回字符数",
                        "confidence": "high",
                    }
                ],
                conclusion_text="分析链路自检通过：上下文装配、工具执行、结果落库与文件导出均正常。",
                missing_data_text="自检模式不涉及真实业务数据，如需业务结论请切换为 demo 或 llm 引擎。",
                next_action_text="1. 将 analysis.engine_mode 切换为 demo 或 llm 以获得真实归因结论。\n"
                "2. 在管理台执行「工具自检」验证全部六个分析工具。",
            )
        )
