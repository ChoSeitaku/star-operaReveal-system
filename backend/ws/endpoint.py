"""长连接端点。

连接鉴权：客户端先用已登录的 Cookie 调 POST /api/chat/ws-token 取得一次性、
短时效（默认 60 秒）、且绑定到具体会话的临时令牌，再以
    WS /api/chat/ws/chat?websocket_token=...&conversation_id=...
建立连接。令牌在服务端原子消费（UPDATE ... WHERE consumed_at IS NULL），
重复使用会被拒绝 —— 因此重连必须重新签发令牌。

客户端 → 服务端消息（扩展协议，规格书仅定义了服务端 → 客户端方向）：
    {"type": "user_message", "content": "...", "attachment_ids": [1, 2]}
    {"type": "cancel", "task_id": 12}
    {"type": "ping"}
"""
from __future__ import annotations

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from backend import database
from backend.tasks import service as task_service
from backend.tasks.state import TaskStatus
from backend.utils import utc_now_iso
from backend.ws.manager import SERVER_MESSAGE_TYPES
from backend.ws.manager import manager as ws_manager

router = APIRouter()

WS_CLOSE_UNAUTHORIZED = 4401
WS_CLOSE_FORBIDDEN = 4403
WS_CLOSE_INTERNAL = 1011


async def _consume_token(token: str, conversation_id: int) -> dict | None:
    """原子消费一次性令牌；已被消费或已过期返回 None。"""
    if not token:
        return None
    now = utc_now_iso()
    _rowid, affected = await database.execute(
        "UPDATE websocket_tokens SET consumed_at = ?"
        " WHERE token = ? AND consumed_at IS NULL AND expires_at > ?",
        (now, token, now),
    )
    if affected != 1:
        return None
    row = await database.fetch_one("SELECT * FROM websocket_tokens WHERE token = ?", (token,))
    if row is None or row["conversation_id"] != conversation_id:
        return None
    return dict(row)


async def _safe_send(websocket: WebSocket, payload: dict) -> None:
    if websocket.client_state == WebSocketState.CONNECTED:
        try:
            await websocket.send_json(payload)
        except Exception:
            pass


@router.websocket("/api/chat/ws/chat")
async def chat_websocket(
    websocket: WebSocket,
    websocket_token: str = Query(default=""),
    conversation_id: int = Query(default=0),
) -> None:
    # 先 accept 再校验，客户端才能收到有意义的关闭码（而非 HTTP 403）
    await websocket.accept()

    token_row = await _consume_token(websocket_token, conversation_id)
    if token_row is None:
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED, reason="令牌无效、已使用或已过期")
        return

    user_id = token_row["user_id"]
    conversation = await database.fetch_one(
        "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
    )
    if (
        conversation is None
        or conversation["user_id"] != user_id
        or conversation["status"] == "deleted"
    ):
        await websocket.close(code=WS_CLOSE_FORBIDDEN, reason="无权访问该会话")
        return

    await ws_manager.register(conversation_id, websocket)

    try:
        await _safe_send(
            websocket,
            {
                "type": "connected",
                "conversation_id": conversation_id,
                "user_id": user_id,
                "server_time": utc_now_iso(),
                "message_types": list(SERVER_MESSAGE_TYPES),
            },
        )

        # 断线重连：下发当前任务状态快照
        snapshot = await task_service.conversation_snapshot(conversation_id)
        if snapshot is not None:
            await _safe_send(
                websocket,
                {
                    "type": "task_status",
                    "task_id": snapshot["task_id"],
                    "task_status": snapshot["task_status"],
                    "current_step": snapshot["current_step"],
                },
            )

        while True:
            message = await websocket.receive_json()
            if not isinstance(message, dict):
                await _safe_send(
                    websocket,
                    {"type": "error", "task_id": None, "error_message": "消息格式必须是 JSON 对象"},
                )
                continue
            await _handle_message(websocket, conversation_id, user_id, message)

    except WebSocketDisconnect:
        pass
    except Exception as exc:  # 连接层异常：尽量告知客户端后关闭
        await _safe_send(
            websocket, {"type": "error", "task_id": None, "error_message": f"连接异常：{exc}"}
        )
        try:
            await websocket.close(code=WS_CLOSE_INTERNAL)
        except Exception:
            pass
    finally:
        await ws_manager.unregister(conversation_id, websocket)


async def _handle_message(
    websocket: WebSocket, conversation_id: int, user_id: int, message: dict
) -> None:
    msg_type = str(message.get("type") or "")

    if msg_type == "ping":
        await _safe_send(websocket, {"type": "pong", "server_time": utc_now_iso()})
        return

    if msg_type == "user_message":
        content = str(message.get("content") or "")
        raw_ids = message.get("attachment_ids") or []
        attachment_ids = [int(x) for x in raw_ids if isinstance(x, (int, str)) and str(x).isdigit()]
        try:
            result = await task_service.create_task_from_message(
                user_id=user_id,
                conversation_id=conversation_id,
                content=content,
                attachment_ids=attachment_ids,
            )
        except task_service.ConversationBusy as exc:
            await _safe_send(
                websocket, {"type": "error", "task_id": None, "error_message": str(exc)}
            )
        except Exception as exc:
            detail = getattr(exc, "detail", None) or str(exc)
            await _safe_send(
                websocket, {"type": "error", "task_id": None, "error_message": str(detail)}
            )
        else:
            # 接受确认由 service 广播给会话内全部连接，这里只补充当前连接的即时反馈
            await _safe_send(
                websocket,
                {
                    "type": "task_status",
                    "task_id": result["task_id"],
                    "task_status": TaskStatus.QUEUED.value,
                    "current_step": "排队中",
                },
            )
        return

    if msg_type == "cancel":
        raw_task_id = message.get("task_id")
        if not isinstance(raw_task_id, (int, str)) or not str(raw_task_id).isdigit():
            await _safe_send(
                websocket,
                {"type": "error", "task_id": None, "error_message": "cancel 必须携带合法的 task_id"},
            )
            return
        try:
            payload = await task_service.cancel_task(int(raw_task_id), user_id)
        except Exception as exc:
            detail = getattr(exc, "detail", None) or str(exc)
            await _safe_send(
                websocket,
                {"type": "error", "task_id": int(raw_task_id), "error_message": str(detail)},
            )
        else:
            await _safe_send(
                websocket,
                {
                    "type": "cancel_ack",
                    "task_id": payload["task_id"],
                    "cancelled": payload["cancelled"],
                    "message": payload["message"],
                },
            )
        return

    await _safe_send(
        websocket,
        {
            "type": "error",
            "task_id": None,
            "error_message": f"不支持的消息类型：{msg_type or '(空)'}",
        },
    )
