"""规则化演示引擎（engine_mode=demo，默认）。

零外部依赖：不使用任何大模型，靠「情景识别 + 固定 SQL 序列 + 据实计算」产出结论。
它同样会走真实的工具链路（db_query / file_write / file_read / text_search / run_command），
因此 WS 上的 tool_start / tool_finish 事件与 LLM 引擎完全一致。
"""
from __future__ import annotations

import json
from typing import Any

from backend.analysis import tools as analysis_tools
from backend.analysis.demo_analyzers import (
    ALL_ANALYZERS,
    DemoAnalyzer,
    Finding,
    GenericAnalyzer,
)
from backend.analysis.engine import AnalysisEngine, EngineContext
from backend.utils import truncate

NOTES_FILENAME = "分析笔记.md"
FUNNEL_CSV = "funnel_data.csv"
VERIFY_SCRIPT = "verify_funnel.py"

VERIFY_CODE = '''"""校验中间产物 funnel_data.csv 的结构与汇总值（由分析引擎自动生成）。"""
import csv

with open("funnel_data.csv", encoding="utf-8") as handle:
    rows = list(csv.reader(handle))

header, body = rows[0], rows[1:]
total_exposure = sum(int(row[1]) for row in body)

print("表头:", ",".join(header))
print("数据行数:", len(body))
print("曝光量合计:", total_exposure)
print("校验结果:", "通过" if len(body) > 0 and total_exposure > 0 else "失败")
'''


class DemoEngine(AnalysisEngine):
    name = "demo"

    def __init__(self) -> None:
        self._assembled = None

    def set_assembled(self, assembled) -> None:
        self._assembled = assembled

    # ------------------------------------------------------------ 情景识别

    @staticmethod
    def _table_index() -> dict[str, set[str]]:
        index: dict[str, set[str]] = {}
        for info in analysis_tools.describe_databases():
            index[info["name"]] = {t["name"] for t in info["tables"]}
        return index

    @classmethod
    def detect(cls, question: str) -> DemoAnalyzer:
        """先按表结构匹配可用场景，再按关键词打分选最贴近的一个。"""
        table_index = cls._table_index()
        text = (question or "").lower()

        best: tuple[int, DemoAnalyzer] | None = None
        for analyzer_cls in ALL_ANALYZERS:
            if not analyzer_cls.required_tables:
                continue
            has_data = any(
                analyzer_cls.required_tables.issubset(tables) for tables in table_index.values()
            )
            if not has_data:
                continue
            score = sum(1 for word in analyzer_cls.keywords if word.lower() in text)
            if score == 0:
                continue
            if best is None or score > best[0]:
                best = (score, analyzer_cls())

        if best is not None:
            return best[1]
        return GenericAnalyzer()

    # ------------------------------------------------------------ 主流程

    async def run(self, ctx: EngineContext) -> None:
        analyzer = self.detect(ctx.user_input)
        await ctx.log("info", "step", f"情景识别结果：{analyzer.title}（{analyzer.key}）")
        await ctx.text(analyzer.intro(ctx.user_input))

        findings: dict[str, Finding] = {}

        if isinstance(analyzer, GenericAnalyzer):
            findings["inventory"] = await self._inventory(ctx)
        else:
            findings = await self._run_steps(ctx, analyzer)

        await ctx.raise_if_cancelled()

        # 中间产物：笔记 + 结构化数据文件 + 独立校验
        await self._write_notes(ctx, analyzer, findings)
        if await self._write_funnel_csv(ctx, findings):
            await self._verify_with_command(ctx)

        # 若用户上传了附件，检索其中与该结论相关的线索作为补充证据
        attachment_evidence = await self._scan_attachments(ctx, analyzer, findings)

        result = analyzer.build(ctx.user_input, findings)
        if attachment_evidence:
            result.evidence_list.append(attachment_evidence)

        await ctx.text("\n\n分析完成，正在汇总结构化结论……\n")
        await ctx.finalize(result)

    # ------------------------------------------------------------ 取数

    async def _run_steps(self, ctx: EngineContext, analyzer: DemoAnalyzer) -> dict[str, Finding]:
        findings: dict[str, Finding] = {}
        steps = analyzer.steps()

        for index, step in enumerate(steps, start=1):
            await ctx.raise_if_cancelled()
            await ctx.text(step.narration)

            raw = await ctx.call_tool("db_query", {"database": step.database, "sql": step.sql})
            finding = Finding(label=step.label, database=step.database, sql=step.sql)

            if raw.startswith("错误："):
                finding.error = raw
                findings[step.label] = finding
                await ctx.log("warning", "tool", f"步骤 {index} 查询失败：{raw}")
                await ctx.text(f"  该步骤取数失败：{raw}\n")
                continue

            try:
                finding.payload = json.loads(raw)
            except ValueError:
                finding.error = "查询结果解析失败"
                findings[step.label] = finding
                continue

            findings[step.label] = finding
            await ctx.text(self._render_finding(finding) + "\n")

        return findings

    @staticmethod
    def _render_finding(finding: Finding) -> str:
        columns = finding.columns
        rows = finding.rows
        if not rows:
            return "  （无数据）"

        lines = ["  " + " | ".join(columns)]
        for row in rows[:8]:
            lines.append("  " + " | ".join("-" if cell is None else str(cell) for cell in row))
        if len(rows) > 8:
            lines.append(f"  …（共 {len(rows)} 行，已省略 {len(rows) - 8} 行）")
        return "\n".join(lines)

    async def _inventory(self, ctx: EngineContext) -> Finding:
        """通用场景：盘点可用数据源（不依赖 SQL，直接读取库结构）。"""
        infos = analysis_tools.describe_databases()
        rows = [
            {
                "数据库": info["name"],
                "表数量": len(info["tables"]),
                "表名列表": "、".join(t["name"] for t in info["tables"]),
            }
            for info in infos
        ]
        payload = {
            "columns": ["数据库", "表数量", "表名列表"],
            "rows": [[r["数据库"], r["表数量"], r["表名列表"]] for r in rows],
            "row_count": len(rows),
            "truncated": False,
        }
        finding = Finding(label="inventory", database="(结构盘点)", sql="(无需 SQL)", payload=payload)
        await ctx.text(self._render_finding(finding) + "\n")
        return finding

    # ------------------------------------------------------------ 中间产物与工具演示

    async def _write_notes(
        self, ctx: EngineContext, analyzer: DemoAnalyzer, findings: dict[str, Finding]
    ) -> None:
        lines = [f"# {analyzer.title} · 分析笔记", "", f"原始问题：{ctx.user_input}", ""]
        for label, finding in findings.items():
            lines.append(f"## {label}")
            lines.append(f"SQL：`{truncate(' '.join(finding.sql.split()), 300)}`")
            if finding.error:
                lines.append(f"错误：{finding.error}")
            else:
                lines.append(self._render_finding(finding))
            lines.append("")

        content = "\n".join(lines)
        await ctx.call_tool("file_write", {"filename": NOTES_FILENAME, "content": content})
        await ctx.text(f"\n已将取数过程记录到工作区文件 {NOTES_FILENAME}。\n")

    async def _write_funnel_csv(self, ctx: EngineContext, findings: dict[str, Finding]) -> bool:
        """把漏斗结果导出为 CSV，作为后续独立校验的输入。无漏斗数据时返回 False。"""
        funnel = findings.get("funnel") or findings.get("channel_funnel")
        if funnel is None or not funnel.rows:
            return False
        header = funnel.columns
        body = "\n".join(",".join("" if c is None else str(c) for c in row) for row in funnel.rows)
        await ctx.call_tool(
            "file_write",
            {"filename": FUNNEL_CSV, "content": "\n".join([",".join(header), body]) + "\n"},
        )
        return True

    async def _verify_with_command(self, ctx: EngineContext) -> None:
        """用一次受限命令执行独立校验中间产物，确保落盘数据完整可解析。"""
        await ctx.raise_if_cancelled()
        await ctx.call_tool("file_write", {"filename": VERIFY_SCRIPT, "content": VERIFY_CODE})
        await ctx.text("\n正在对导出的中间数据做独立校验……\n")
        raw = await ctx.call_tool("run_command", {"command": f"python {VERIFY_SCRIPT}"})

        try:
            payload = json.loads(raw)
            output = (payload.get("stdout") or "").strip()
            detail = output.splitlines()[-1] if output else raw
        except ValueError:
            detail = raw
        await ctx.text(f"  校验输出：{truncate(detail, 160)}\n")

    async def _scan_attachments(
        self, ctx: EngineContext, analyzer: DemoAnalyzer, findings: dict[str, Finding]
    ) -> dict[str, Any] | None:
        """在用户上传的附件中检索与关键发现相关的线索。"""
        if self._assembled is None or not self._assembled.attachments:
            return None

        await ctx.raise_if_cancelled()
        newest = self._assembled.attachments[0]
        await ctx.text(f"\n正在检查用户上传的附件「{newest['file_name']}」……\n")

        await ctx.call_tool("file_read", {"path": newest["file_name"]})

        keyword = self._evidence_keyword(findings)
        if not keyword:
            return None

        raw = await ctx.call_tool("text_search", {"query": keyword})
        try:
            payload = json.loads(raw)
        except ValueError:
            return None

        hits = payload.get("hits") or []
        if not hits:
            return None

        first = hits[0]
        return {
            "source_type": "用户附件",
            "source_name": f"{first['file_name']}（第 {first['line_no']} 行）",
            "evidence_text": f"在附件中检索到与「{keyword}」相关的记录：{first['line']}",
            "related_metric": keyword,
            "confidence": "medium",
        }

    @staticmethod
    def _evidence_keyword(findings: dict[str, Finding]) -> str:
        """挑一个用于附件检索的关键词：优先漏斗里曝光最高但转化最差的类目/渠道。"""
        funnel = findings.get("funnel")
        if funnel is not None and funnel.rows:
            column = funnel.columns[0]
            worst_name, worst_rate = "", None
            for row in funnel.dicts():
                exposure, conv = int(row.get("曝光量") or 0), int(row.get("转化量") or 0)
                rate = conv / exposure if exposure else 0.0
                if worst_rate is None or rate < worst_rate:
                    worst_rate, worst_name = rate, str(row.get(column) or "")
            if worst_name:
                return worst_name

        channel = findings.get("channel_funnel")
        if channel is not None and channel.rows:
            best_name, best_visits = "", -1
            for row in channel.dicts():
                visits = int(row.get("访问量") or 0)
                if visits > best_visits:
                    best_visits, best_name = visits, str(row.get("渠道") or "")
            if best_name:
                return best_name
        return ""
