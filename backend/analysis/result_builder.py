"""分析结果处理：结构规范化 → Markdown 渲染 → 结果文件落盘 → 结果落库。

六段式输出（规格书 1.1.11）：
    问题定义 / 关键指标 / 证据列表 / 归因结论 / 待补充数据 / 下一步建议
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from backend import database, storage
from backend.analysis.engine import ResultData
from backend.utils import json_dumps, truncate, utc_now_iso

# 下一步建议的兜底条目：当产出不足两条时补齐（基于「待补充数据」的诚实建议，非编造结论）
FALLBACK_ACTION = "补充上述缺失数据后重新运行本分析，以复核结论的稳健性。"


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return "\n".join(_as_text(v) for v in value if _as_text(v))
    if isinstance(value, dict):
        return json_dumps(value)
    return str(value).strip()


_LEADING_INDEX = re.compile(r"^\s*(?:\d+\s*[.、)）]|[-•*])\s*")


def _split_actions(text: str) -> list[str]:
    """把建议文本拆成条目：优先按行，其次按分号；去掉模型自带的行首序号。"""
    raw = _as_text(text)
    if not raw:
        return []
    items = [_LEADING_INDEX.sub("", line).strip() for line in raw.splitlines()]
    items = [item for item in items if item]
    if len(items) <= 1 and "；" in raw:
        items = [_LEADING_INDEX.sub("", part).strip() for part in raw.split("；") if part.strip()]
    if len(items) <= 1 and ";" in raw:
        items = [_LEADING_INDEX.sub("", part).strip() for part in raw.split(";") if part.strip()]
    return items


def _normalize_metrics(raw: Any) -> list[dict[str, Any]]:
    metrics: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return metrics
    for item in raw:
        if isinstance(item, str):
            metrics.append(
                {"metric_name": item, "metric_value": "", "metric_unit": "", "metric_period": ""}
            )
            continue
        if not isinstance(item, dict):
            continue
        name = _as_text(item.get("metric_name") or item.get("name"))
        if not name:
            continue
        value = _as_text(item.get("metric_value") or item.get("value"))
        unit = _as_text(item.get("metric_unit") or item.get("unit"))
        # 模型常把单位同时写进数值和单位字段（如 "37.2%" + "%"），去重避免出现 "37.2% %"
        if unit and unit in value:
            unit = ""
        metrics.append(
            {
                "metric_name": name,
                "metric_value": value,
                "metric_unit": unit,
                "metric_period": _as_text(item.get("metric_period") or item.get("period")),
            }
        )
    return metrics


def _normalize_evidence(raw: Any) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return evidence
    for item in raw:
        if isinstance(item, str):
            evidence.append(
                {
                    "source_type": "分析过程",
                    "source_name": "未知来源",
                    "evidence_text": item,
                    "related_metric": "",
                    "confidence": "medium",
                }
            )
            continue
        if not isinstance(item, dict):
            continue
        text = _as_text(item.get("evidence_text") or item.get("text") or item.get("detail"))
        if not text:
            continue
        confidence = _as_text(item.get("confidence") or "medium").lower()
        if confidence not in ("high", "medium", "low"):
            confidence = "medium"
        evidence.append(
            {
                "source_type": _as_text(item.get("source_type")) or "分析过程",
                "source_name": _as_text(item.get("source_name")) or "未标注",
                "evidence_text": text,
                "related_metric": _as_text(item.get("related_metric")),
                "confidence": confidence,
            }
        )
    return evidence


def normalize_result(raw: dict[str, Any] | ResultData) -> ResultData:
    """把引擎产出规范化为标准六段式结构，容忍字段缺失与类型偏差。"""
    data = raw.to_dict() if isinstance(raw, ResultData) else dict(raw or {})

    actions = _split_actions(data.get("next_action_text"))
    if len(actions) < 2:
        actions.append(FALLBACK_ACTION)

    return ResultData(
        problem_definition=_as_text(data.get("problem_definition")) or "未明确问题定义",
        key_metrics=_normalize_metrics(data.get("key_metrics")),
        evidence_list=_normalize_evidence(data.get("evidence_list")),
        conclusion_text=_as_text(data.get("conclusion_text")) or "未得出结论",
        missing_data_text=_as_text(data.get("missing_data_text")) or "未标注缺失数据",
        next_action_text="\n".join(f"{i}. {item}" for i, item in enumerate(actions, start=1)),
    )


def result_summary(result: ResultData) -> str:
    """给 WS tool_finish / 日志用的一句话摘要。"""
    return (
        f"问题定义：{truncate(result.problem_definition, 60)}｜"
        f"关键指标 {len(result.key_metrics)} 项｜证据 {len(result.evidence_list)} 条"
    )


def next_actions(result: ResultData) -> list[str]:
    return _split_actions(result.next_action_text)


CONFIDENCE_LABEL = {"high": "高", "medium": "中", "low": "低"}


def render_markdown(
    result: ResultData,
    *,
    task_id: int,
    conversation_id: int,
    conversation_title: str,
    user_input: str,
    engine_mode: str,
    created_at: str | None = None,
) -> str:
    """渲染导出用 Markdown 报告。"""
    stamp = created_at or utc_now_iso()
    lines: list[str] = [
        "# 经营归因分析报告",
        "",
        f"- **会话**：{conversation_title}（conversation_id={conversation_id}）",
        f"- **任务**：task_id={task_id}",
        f"- **分析引擎**：{engine_mode}",
        f"- **生成时间**：{stamp}",
        "",
        "## 一、问题定义",
        "",
        result.problem_definition,
        "",
        "## 二、关键指标",
        "",
    ]

    if result.key_metrics:
        lines += ["| 指标 | 数值 | 单位 | 周期 |", "| --- | --- | --- | --- |"]
        for metric in result.key_metrics:
            lines.append(
                f"| {metric['metric_name']} | {metric['metric_value']} | "
                f"{metric['metric_unit'] or '-'} | {metric['metric_period'] or '-'} |"
            )
    else:
        lines.append("（本次分析未产出关键指标）")
    lines.append("")

    lines += ["## 三、证据列表", ""]
    if result.evidence_list:
        for index, item in enumerate(result.evidence_list, start=1):
            lines += [
                f"**证据 {index}**　来源类型：{item['source_type']}　来源：{item['source_name']}　"
                f"置信度：{CONFIDENCE_LABEL.get(item['confidence'], item['confidence'])}"
                + (f"　关联指标：{item['related_metric']}" if item["related_metric"] else ""),
                "",
                f"> {item['evidence_text']}",
                "",
            ]
    else:
        lines.append("（本次分析未产出证据）")
        lines.append("")

    lines += [
        "## 四、归因结论",
        "",
        result.conclusion_text,
        "",
        "## 五、待补充数据",
        "",
        result.missing_data_text,
        "",
        "## 六、下一步建议",
        "",
    ]
    for action in next_actions(result):
        lines.append(f"- {action}")
    lines += ["", "---", "", f"原始问题：{user_input}", ""]

    return "\n".join(lines)


def save_result_file(user_id: int, conversation_id: int, task_id: int, markdown: str) -> Path:
    """结果文件写入 exports/{user_id}/{conversation_id}/，返回绝对路径。"""
    directory = storage.export_dir(user_id, conversation_id)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = utc_now_iso().replace(":", "").replace("-", "")
    path = storage.safe_join(directory, f"归因分析_report_task{task_id}_{stamp}.md")
    path.write_text(markdown, encoding="utf-8")
    return path


async def persist_result(
    *,
    task_id: int,
    conversation_id: int,
    user_id: int,
    conversation_title: str,
    user_input: str,
    engine_mode: str,
    raw_result: dict[str, Any] | ResultData,
    write_file: bool = True,
) -> dict[str, Any]:
    """规范化 → 渲染 → 落盘 → 落库，返回结果行字典。"""
    result = normalize_result(raw_result)
    created_at = utc_now_iso()

    markdown = render_markdown(
        result,
        task_id=task_id,
        conversation_id=conversation_id,
        conversation_title=conversation_title,
        user_input=user_input,
        engine_mode=engine_mode,
        created_at=created_at,
    )

    file_path: str | None = None
    if write_file:
        try:
            file_path = str(save_result_file(user_id, conversation_id, task_id, markdown))
        except OSError:
            file_path = None

    result_id, _ = await database.execute(
        "INSERT INTO analysis_results (task_id, conversation_id, problem_definition,"
        " key_metrics_json, evidence_list_json, conclusion_text, missing_data_text,"
        " next_action_text, result_markdown, result_file_path, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            conversation_id,
            result.problem_definition,
            json_dumps(result.key_metrics),
            json_dumps(result.evidence_list),
            result.conclusion_text,
            result.missing_data_text,
            result.next_action_text,
            markdown,
            file_path,
            created_at,
        ),
    )

    return {
        "result_id": result_id,
        "task_id": task_id,
        "conversation_id": conversation_id,
        "result": result,
        "result_markdown": markdown,
        "result_file_path": file_path,
        "created_at": created_at,
    }


def result_payload(row: Any, *, include_markdown: bool = True) -> dict[str, Any]:
    """把 analysis_results 行转为接口响应（六个必需字段 + 扩展字段）。"""
    from backend.utils import json_loads

    payload = {
        "result_id": row["id"],
        "task_id": row["task_id"],
        "conversation_id": row["conversation_id"],
        "problem_definition": row["problem_definition"],
        "key_metrics": json_loads(row["key_metrics_json"], []),
        "evidence_list": json_loads(row["evidence_list_json"], []),
        "conclusion_text": row["conclusion_text"],
        "missing_data_text": row["missing_data_text"],
        "next_action_text": row["next_action_text"],
        "result_file_path": row["result_file_path"],
        "created_at": row["created_at"],
        "has_file": bool(row["result_file_path"] and Path(row["result_file_path"]).exists()),
    }
    if include_markdown:
        payload["result_markdown"] = row["result_markdown"]
    return payload
