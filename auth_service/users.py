"""认证中心演示账号与内存态凭据存储。

注意：本服务是用于演示的模拟认证中心（OAuth2 授权码模式），
凭据仅存于内存，重启后失效；生产环境请对接真实认证中心。
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

CODE_TTL_SECONDS = 300
TOKEN_TTL_SECONDS = 3600


@dataclass(frozen=True)
class DemoUser:
    username: str
    display_name: str
    role: str  # 'admin' | 'analyst'
    email: str


# 演示账号：admin = 系统管理员，analyst = 分析用户
DEMO_USERS: dict[str, DemoUser] = {
    "admin": DemoUser("admin", "系统管理员", "admin", "admin@example.com"),
    "analyst": DemoUser("analyst", "数据分析师", "analyst", "analyst@example.com"),
}


@dataclass
class AuthCode:
    code: str
    username: str
    client_id: str
    redirect_uri: str
    expires_at: datetime
    used: bool = False


@dataclass
class AccessToken:
    token: str
    username: str
    expires_at: datetime


# 内存态存储
CODES: dict[str, AuthCode] = {}
TOKENS: dict[str, AccessToken] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def issue_code(username: str, client_id: str, redirect_uri: str) -> AuthCode:
    _purge_expired()
    code = secrets.token_urlsafe(24)
    entry = AuthCode(
        code=code,
        username=username,
        client_id=client_id,
        redirect_uri=redirect_uri,
        expires_at=_now() + timedelta(seconds=CODE_TTL_SECONDS),
    )
    CODES[code] = entry
    return entry


def consume_code(code: str, client_id: str, redirect_uri: str) -> Optional[AuthCode]:
    """校验并消费授权码（一次性）。"""
    entry = CODES.get(code)
    if entry is None:
        return None
    if entry.used or entry.expires_at < _now():
        CODES.pop(code, None)
        return None
    if entry.client_id != client_id or entry.redirect_uri != redirect_uri:
        return None
    entry.used = True
    CODES.pop(code, None)
    return entry


def issue_token(username: str) -> AccessToken:
    _purge_expired()
    token = secrets.token_urlsafe(32)
    entry = AccessToken(
        token=token, username=username, expires_at=_now() + timedelta(seconds=TOKEN_TTL_SECONDS)
    )
    TOKENS[token] = entry
    return entry


def lookup_token(token: str) -> Optional[DemoUser]:
    entry = TOKENS.get(token)
    if entry is None or entry.expires_at < _now():
        TOKENS.pop(token, None)
        return None
    return DEMO_USERS.get(entry.username)


def _purge_expired() -> None:
    now = _now()
    for code in [c for c, v in CODES.items() if v.expires_at < now or v.used]:
        CODES.pop(code, None)
    for token in [t for t, v in TOKENS.items() if v.expires_at < now]:
        TOKENS.pop(token, None)
