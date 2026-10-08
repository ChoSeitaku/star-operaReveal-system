"""大模型分析引擎（engine_mode=llm）。

直接以 httpx 调用 Anthropic Messages 协议（不依赖官方 SDK），支持流式增量输出与工具调用。

针对 Anthropic 兼容网关（如 DeepSeek）的实测差异做了适配：
- 响应中会包含 `thinking` 内容块：只累积、不推送、不落库，回放历史时也必须剔除；
- 工具入参以 `input_json_delta` 分片下发，需要按块累积到 content_block_stop 再解析；
- `tool_result.content` 使用纯字符串最稳妥；
- 部分网关不接受 `system` 参数，遇到此类 400 会退化为「把系统提示词作为首条用户消息」。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

import httpx

from backend import config
from backend.analysis import tools as analysis_tools
from backend.analysis.engine import AnalysisEngine, EngineContext, ResultData
from backend.utils import truncate

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}
MAX_RETRIES = 2
TOOL_RESULT_LIMIT = 20000
SUBMIT_RESULT = "submit_result"


class LLMError(RuntimeError):
    """大模型调用失败。"""


# ---------------------------------------------------------------- 请求构造


def _api_key() -> str:
    return config.get("llm.api_key", "").strip()


def _endpoint() -> str:
    return config.get("llm.base_url", "").rstrip("/") + "/v1/messages"


def _headers() -> dict[str, str]:
    key = _api_key()
    return {
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
        "x-api-key": key,
        "authorization": f"Bearer {key}",
    }


def _timeout() -> httpx.Timeout:
    total = float(config.get_int("llm.timeout_seconds", 180))
    return httpx.Timeout(timeout=total, connect=30.0, read=total)


def is_configured() -> bool:
    return bool(_api_key() and config.get("llm.base_url", ""))


def _merge_consecutive(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """合并相邻同角色消息，避免部分网关因角色不交替而报错。"""
    merged: list[dict[str, Any]] = []
    for message in messages:
        if merged and merged[-1]["role"] == message["role"] and isinstance(
            merged[-1]["content"], str
        ) and isinstance(message["content"], str):
            merged[-1]["content"] = f"{merged[-1]['content']}\n\n{message['content']}"
        else:
            merged.append(dict(message))
    return merged


def build_messages(assembled, user_input: str) -> list[dict[str, Any]]:
    """把装配好的上下文转成 Messages 协议的消息数组。"""
    messages: list[dict[str, Any]] = []

    if assembled.summaries:
        blocks = [
            f"[历史对话摘要 {s['start_seq_no']}-{s['end_seq_no']}]\n{s['summary_text']}"
            for s in assembled.summaries
        ]
        messages.append({"role": "user", "content": "以下是本会话此前的对话摘要：\n\n" + "\n\n".join(blocks)})
        messages.append({"role": "assistant", "content": "已了解历史上下文，请继续。"})

    for message in assembled.recent_messages:
        messages.append(
            {
                "role": "user" if message["role"] == "user" else "assistant",
                "content": message["content"],
            }
        )

    if assembled.attachments:
        blocks = [
            f"[附件 {a['file_name']}（{a['file_type']}）]\n{a['preview']}"
            for a in assembled.attachments
        ]
        messages.append(
            {"role": "user", "content": "用户在本会话上传了以下附件，可作为分析依据：\n\n" + "\n\n".join(blocks)}
        )

    messages.append({"role": "user", "content": user_input})
    return _merge_consecutive(messages)


def _tool_schemas() -> list[dict[str, Any]]:
    return analysis_tools.available_tool_schemas() + [analysis_tools.SUBMIT_RESULT_TOOL]


# ---------------------------------------------------------------- 非流式调用


async def complete_once(prompt: str, *, max_tokens: int = 800, system: str = "") -> str:
    """一次性补全（用于历史摘要等辅助场景）。"""
    if not is_configured():
        raise LLMError("未配置 LLM（缺少 api_key 或 base_url）")

    body: dict[str, Any] = {
        "model": config.get("llm.model"),
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        body["system"] = system

    async with httpx.AsyncClient(timeout=_timeout()) as client:
        response = await client.post(_endpoint(), headers=_headers(), json=body)
        if response.status_code != 200:
            raise LLMError(f"LLM 返回 {response.status_code}: {truncate(response.text, 300)}")
        payload = response.json()

    parts = [
        block.get("text", "")
        for block in payload.get("content", [])
        if block.get("type") == "text"
    ]
    return "".join(parts)


# ---------------------------------------------------------------- 流式调用


class StreamOutcome:
    def __init__(self) -> None:
        self.text: str = ""
        self.tool_calls: list[dict[str, Any]] = []
        self.stop_reason: str = ""
        self.input_tokens: int = 0
        self.output_tokens: int = 0

    async def consume_json(self, raw: str, on_text) -> None:
        """处理非流式的整包响应（网关忽略 stream 参数时的回退路径）。"""
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise LLMError(f"响应不是合法 JSON：{truncate(raw, 300)}") from exc

        usage = payload.get("usage") or {}
        self.input_tokens = int(usage.get("input_tokens") or 0)
        self.output_tokens = int(usage.get("output_tokens") or 0)
        self.stop_reason = str(payload.get("stop_reason") or "")

        for block in payload.get("content") or []:
            btype = block.get("type")
            if btype == "text":
                piece = block.get("text") or ""
                self.text += piece
                await on_text(piece)
            elif btype == "tool_use":
                self.tool_calls.append(dict(block))
            # thinking 块同样忽略


async def _stream_once(
    body: dict[str, Any], on_text, use_system: bool = True
) -> StreamOutcome:
    """发起一次流式请求并解析 SSE。on_text 为异步回调，用于即时推送增量文本。"""
    payload = dict(body)
    payload["stream"] = True  # 必须显式声明，否则网关会退回整包 JSON
    if not use_system:
        payload.pop("system", None)

    outcome = StreamOutcome()
    blocks: dict[int, dict[str, Any]] = {}
    last_error: Exception | None = None

    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            await asyncio.sleep(1.5 * attempt)
        try:
            async with httpx.AsyncClient(timeout=_timeout()) as client:
                async with client.stream(
                    "POST", _endpoint(), headers=_headers(), json=payload
                ) as response:
                    if response.status_code != 200:
                        text = (await response.aread()).decode("utf-8", "replace")
                        if response.status_code == 400 and use_system and "system" in text.lower():
                            # 网关不接受 system 参数：退化为把提示词并入消息
                            return await _stream_once(body, on_text, use_system=False)
                        if response.status_code in RETRYABLE_STATUS and attempt < MAX_RETRIES:
                            last_error = LLMError(f"LLM 返回 {response.status_code}: {truncate(text, 200)}")
                            continue
                        raise LLMError(f"LLM 返回 {response.status_code}: {truncate(text, 400)}")

                    # 部分网关会忽略 stream 参数直接返回整包 JSON，这里做兼容
                    if "application/json" in (response.headers.get("content-type") or ""):
                        raw = (await response.aread()).decode("utf-8", "replace")
                        await outcome.consume_json(raw, on_text)
                        return outcome

                    async for event in _iter_sse(response):
                        etype = event.get("type")
                        if etype == "message_start":
                            usage = (event.get("message") or {}).get("usage") or {}
                            outcome.input_tokens = int(usage.get("input_tokens") or 0)
                        elif etype == "content_block_start":
                            index = int(event.get("index", 0))
                            block = dict(event.get("content_block") or {})
                            block["_json"] = ""
                            blocks[index] = block
                        elif etype == "content_block_delta":
                            index = int(event.get("index", 0))
                            delta = event.get("delta") or {}
                            block = blocks.setdefault(index, {"type": "text", "_json": ""})
                            dtype = delta.get("type")
                            if dtype == "text_delta":
                                piece = delta.get("text", "")
                                block["text"] = block.get("text", "") + piece
                                outcome.text += piece
                                await on_text(piece)
                            elif dtype == "thinking_delta":
                                # 思维链只累积不推送（既不落库也不进最终回答）
                                block["thinking"] = block.get("thinking", "") + delta.get("thinking", "")
                            elif dtype == "input_json_delta":
                                block["_json"] = block.get("_json", "") + delta.get("partial_json", "")
                        elif etype == "content_block_stop":
                            index = int(event.get("index", 0))
                            block = blocks.get(index) or {}
                            if block.get("type") == "tool_use":
                                raw = block.pop("_json", "") or "{}"
                                try:
                                    block["input"] = json.loads(raw) if raw.strip() else {}
                                except ValueError:
                                    block["input"] = {}
                                outcome.tool_calls.append(block)
                        elif etype == "message_delta":
                            delta = event.get("delta") or {}
                            if delta.get("stop_reason"):
                                outcome.stop_reason = str(delta["stop_reason"])
                            usage = event.get("usage") or {}
                            if usage.get("output_tokens"):
                                outcome.output_tokens = int(usage["output_tokens"])
                        elif etype == "error":
                            raise LLMError(f"LLM 流式错误: {truncate(json.dumps(event, ensure_ascii=False), 300)}")
                    return outcome
        except (httpx.TransportError, httpx.ReadTimeout) as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                continue
            raise LLMError(f"无法连接大模型服务: {exc}") from exc

    raise LLMError(f"大模型调用失败: {last_error}")


async def _iter_sse(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """解析 Anthropic 风格 SSE：成对的 event: / data: 行。"""
    async for line in response.aiter_lines():
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            payload = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(payload, dict):
            yield payload


def _assistant_content(outcome: StreamOutcome) -> list[dict[str, Any]]:
    """把一次响应重建成可回放的内容块数组：只保留 text 与 tool_use，剔除 thinking。"""
    content: list[dict[str, Any]] = []
    if outcome.text.strip():
        content.append({"type": "text", "text": outcome.text})
    for call in outcome.tool_calls:
        content.append(
            {
                "type": "tool_use",
                "id": call.get("id") or f"toolu_{len(content)}",
                "name": call.get("name") or "",
                "input": call.get("input") or {},
            }
        )
    return content


# ---------------------------------------------------------------- 引擎


class LLMEngine(AnalysisEngine):
    name = "llm"

    def __init__(self) -> None:
        self._assembled = None

    def set_assembled(self, assembled) -> None:
        self._assembled = assembled

    async def run(self, ctx: EngineContext) -> None:
        if not is_configured():
            raise LLMError("未配置 LLM：请在系统配置中设置 llm.api_key / llm.base_url，或切换为 demo 引擎")

        messages = build_messages(self._assembled, ctx.user_input)
        base_body: dict[str, Any] = {
            "model": config.get("llm.model"),
            "max_tokens": config.get_int("llm.max_tokens", 4096),
            "system": ctx.system_prompt,
            "tools": _tool_schemas(),
            "messages": messages,
        }

        max_steps = max(1, config.get_int("llm.max_agent_steps", 10))
        submitted = False

        for step in range(1, max_steps + 1):
            await ctx.raise_if_cancelled()
            await ctx.log("info", "llm", f"第 {step} 轮模型调用，消息数 {len(base_body['messages'])}")

            async def on_text(piece: str) -> None:
                await ctx.text(piece)

            outcome = await _stream_once(base_body, on_text)
            await ctx.log(
                "info",
                "llm",
                f"第 {step} 轮返回：文本 {len(outcome.text)} 字、工具调用 {len(outcome.tool_calls)} 次、"
                f"stop_reason={outcome.stop_reason or '-'}、tokens in/out="
                f"{outcome.input_tokens}/{outcome.output_tokens}",
            )

            # 拦截 submit_result：结构化结果即最终产出
            submit = next(
                (c for c in outcome.tool_calls if c.get("name") == SUBMIT_RESULT), None
            )
            if submit is not None:
                await ctx.finalize(_to_result(submit.get("input") or {}))
                submitted = True
                break

            if not outcome.tool_calls:
                # 模型没有调用任何工具就结束了：追问一次，要求它提交结构化结果
                await ctx.log("warning", "llm", "模型未调用工具即结束，尝试追问一次")
                base_body["messages"] = list(base_body["messages"]) + [
                    {"role": "assistant", "content": _assistant_content(outcome) or [{"type": "text", "text": outcome.text}]},
                    {"role": "user", "content": f"请立即调用 {SUBMIT_RESULT} 工具提交结构化结论，不要再输出其他内容。"},
                ]
                follow = await _stream_once(base_body, on_text)
                submit = next((c for c in follow.tool_calls if c.get("name") == SUBMIT_RESULT), None)
                if submit is not None:
                    await ctx.finalize(_to_result(submit.get("input") or {}))
                    submitted = True
                    break
                raise LLMError("模型未能生成结构化结论")

            # 模型只调工具不说话时，补一句可读的进展说明，避免对话区一片空白
            if not outcome.text.strip() and outcome.tool_calls:
                names = "、".join(dict.fromkeys(
                    str(c.get("name")) for c in outcome.tool_calls if c.get("name")
                ))
                await ctx.text(f"正在调用工具：{names}……\n")

            # 执行工具并把结果回灌给模型
            tool_results: list[dict[str, Any]] = []
            for call in outcome.tool_calls:
                await ctx.raise_if_cancelled()
                name = str(call.get("name") or "")
                arguments = call.get("input") or {}
                if not isinstance(arguments, dict):
                    arguments = {}
                await ctx.log("info", "tool", f"模型请求工具 {name}，参数 {truncate(json.dumps(arguments, ensure_ascii=False), 200)}")
                output = await ctx.call_tool(name, arguments)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call.get("id") or "",
                        "content": truncate(output, TOOL_RESULT_LIMIT),
                    }
                )

            base_body["messages"] = (
                list(base_body["messages"])
                + [{"role": "assistant", "content": _assistant_content(outcome)}]
                + [{"role": "user", "content": tool_results}]
            )

            # 轮次将尽时明确催促收口，避免把预算耗在补充查询上
            remaining = max_steps - step
            if remaining <= 2:
                await ctx.log("warning", "llm", f"剩余 {remaining} 轮，提示模型立即提交结论")
                base_body["messages"].append(
                    {
                        "role": "user",
                        "content": (
                            f"剩余工具调用轮次已不足（还剩 {remaining} 轮）。"
                            f"请立即调用 {SUBMIT_RESULT} 提交当前已有的分析结论，"
                            f"缺失的信息写入「待补充数据」即可，不要再执行新的查询。"
                        ),
                    }
                )

        if not submitted:
            raise LLMError(f"已达到最大工具调用轮次（{max_steps}）仍未产出结构化结论")


def _to_result(payload: dict[str, Any]) -> ResultData:
    return ResultData(
        problem_definition=str(payload.get("problem_definition") or ""),
        key_metrics=payload.get("key_metrics") or [],
        evidence_list=payload.get("evidence_list") or [],
        conclusion_text=str(payload.get("conclusion_text") or ""),
        missing_data_text=str(payload.get("missing_data_text") or ""),
        next_action_text=str(payload.get("next_action_text") or ""),
    )
