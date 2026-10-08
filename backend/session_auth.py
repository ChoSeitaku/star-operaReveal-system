"""会话认证：HMAC 签名 Cookie + 当前用户识别 + 用户自动建档。

登录态不使用服务端 Session 存储，而是把 {用户ID, 角色, 过期时间} 用 HMAC-SHA256
签名后写入 HttpOnly Cookie，服务端无状态校验。签名密钥持久化在 data/secret.key，
保证进程重启后已登录用户不掉线。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Annotated, Any, Optional

from fastapi import Depends, HTTPException, Request, Response, status

from backend import config, database, storage
from backend.utils import utc_now_iso

COOKIE_NAME = "star_session"
_secret_cache: Optional[bytes] = None


# ---------------------------------------------------------------- 签名密钥


def get_secret() -> bytes:
    """获取签名密钥：环境变量 > data/secret.key（不存在则生成并持久化）。"""
    global _secret_cache
    if _secret_cache is not None:
        return _secret_cache

    override = config.server_setting("STAR_SESSION_SECRET", "")
    if override:
        _secret_cache = override.encode("utf-8")
        return _secret_cache

    path = storage.secret_key_path()
    try:
        if path.exists():
            data = path.read_bytes().strip()
            if data:
                _secret_cache = data
                return _secret_cache
        path.parent.mkdir(parents=True, exist_ok=True)
        data = secrets.token_bytes(32)
        path.write_bytes(data)
        _secret_cache = data
    except OSError:
        # 数据目录不可写时退化为进程内随机密钥（重启后需重新登录）
        _secret_cache = secrets.token_bytes(32)
    return _secret_cache


# ---------------------------------------------------------------- 令牌编解码


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def sign_payload(payload: dict[str, Any], purpose: str) -> str:
    """对任意 JSON 载荷做 HMAC 签名。purpose 用于隔离不同用途的签名，防止串用。"""
    body = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(get_secret(), f"{purpose}:{body}".encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def verify_payload(token: str | None, purpose: str) -> Optional[dict[str, Any]]:
    if not token or "." not in token:
        return None
    body, _, signature = token.rpartition(".")
    expected = hmac.new(get_secret(), f"{purpose}:{body}".encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = json.loads(_b64decode(body))
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def sign_session(user_id: int, role: str, ttl_seconds: int | None = None) -> str:
    ttl = ttl_seconds if ttl_seconds is not None else config.get_int("auth.session_ttl_seconds", 86400)
    return sign_payload(
        {"uid": int(user_id), "role": role, "exp": int(time.time()) + int(ttl)}, "session"
    )


def verify_session(token: str | None) -> Optional[dict[str, Any]]:
    payload = verify_payload(token, "session")
    if payload is None or payload.get("exp", 0) < time.time():
        return None
    return payload


# ---------------------------------------------------------------- Cookie 读写


def set_session_cookie(response: Response, token: str) -> None:
    ttl = config.get_int("auth.session_ttl_seconds", 86400)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=ttl,
        httponly=True,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


# ---------------------------------------------------------------- 用户建档


async def provision_user(auth_user: dict[str, Any]) -> dict[str, Any]:
    """按认证中心返回的用户信息建/更新本地用户记录，返回 users 行。"""
    external_id = str(auth_user.get("username") or auth_user.get("id") or "").strip()
    if not external_id:
        raise HTTPException(status_code=400, detail="认证中心未返回用户标识")

    display_name = str(auth_user.get("display_name") or external_id)
    role = str(auth_user.get("role") or "analyst")
    if role not in ("admin", "analyst"):
        role = "analyst"
    now = utc_now_iso()

    row = await database.fetch_one(
        "SELECT * FROM users WHERE external_user_id = ?", (external_id,)
    )
    if row is None:
        user_id, _ = await database.execute(
            "INSERT INTO users (external_user_id, username, display_name, role, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'active', ?, ?)",
            (external_id, external_id, display_name, role, now, now),
        )
        row = await database.fetch_one("SELECT * FROM users WHERE id = ?", (user_id,))
    else:
        await database.execute(
            "UPDATE users SET display_name = ?, role = ?, updated_at = ? WHERE id = ?",
            (display_name, role, now, row["id"]),
        )
        row = await database.fetch_one("SELECT * FROM users WHERE id = ?", (row["id"],))
    return dict(row)


# ---------------------------------------------------------------- 依赖注入


async def get_current_user(request: Request) -> dict[str, Any]:
    """从 Cookie 解析当前用户；未登录或已停用则 401。"""
    payload = verify_session(request.cookies.get(COOKIE_NAME))
    if payload is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

    row = await database.fetch_one(
        "SELECT * FROM users WHERE id = ? AND status = 'active'", (payload["uid"],)
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

    user = dict(row)
    request.state.user = user
    return user


CurrentUser = Annotated[dict[str, Any], Depends(get_current_user)]


async def require_admin(user: CurrentUser) -> dict[str, Any]:
    """管理员依赖：非 admin 角色返回 403。"""
    if user.get("role") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要系统管理员权限")
    return user


AdminUser = Annotated[dict[str, Any], Depends(require_admin)]
