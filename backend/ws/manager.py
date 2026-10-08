"""会话级长连接管理。

一个会话允许多个连接（多标签页），所有推送按会话广播；发送失败或已关闭的连接
自动摘除。WebSocket 事件类型与字段见规格书 1.1.10。
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import WebSocket

# 规格书 1.1.10 定义的 8 种服务端 → 客户端消息类型
SERVER_MESSAGE_TYPES = (
    "message_start",
    "message_delta",
    "tool_start",
    "tool_finish",
    "task_status",
    "result_ready",
    "error",
    "done",
)

# 扩展的客户端 → 服务端消息类型
CLIENT_MESSAGE_TYPES = ("user_message", "cancel", "ping")


class WSManager:
    def __init__(self) -> None:
        self._rooms: dict[int, set[WebSocket]] = {}
        self._guard = asyncio.Lock()

    async def register(self, conversation_id: int, websocket: WebSocket) -> None:
        async with self._guard:
            self._rooms.setdefault(conversation_id, set()).add(websocket)

    async def unregister(self, conversation_id: int, websocket: WebSocket) -> None:
        async with self._guard:
            room = self._rooms.get(conversation_id)
            if room is not None:
                room.discard(websocket)
                if not room:
                    self._rooms.pop(conversation_id, None)

    async def broadcast(self, conversation_id: int, payload: dict[str, Any]) -> int:
        """向会话内所有连接推送，返回成功条数。"""
        async with self._guard:
            targets = list(self._rooms.get(conversation_id, ()))

        if not targets:
            return 0

        delivered = 0
        dead: list[WebSocket] = []
        for websocket in targets:
            try:
                await websocket.send_json(payload)
                delivered += 1
            except Exception:
                dead.append(websocket)

        if dead:
            async with self._guard:
                room = self._rooms.get(conversation_id)
                if room is not None:
                    for websocket in dead:
                        room.discard(websocket)
                    if not room:
                        self._rooms.pop(conversation_id, None)
        return delivered

    async def close_conversation(
        self, conversation_id: int, code: int = 1000, reason: str = ""
    ) -> int:
        """关闭会话内所有连接（会话被删除时使用）。"""
        async with self._guard:
            targets = list(self._rooms.pop(conversation_id, ()))

        for websocket in targets:
            try:
                await websocket.close(code=code, reason=reason)
            except Exception:
                pass
        return len(targets)

    def connection_count(self, conversation_id: int) -> int:
        return len(self._rooms.get(conversation_id, ()))

    def stats(self) -> dict[str, int]:
        return {
            "conversations": len(self._rooms),
            "connections": sum(len(room) for room in self._rooms.values()),
        }


manager = WSManager()
