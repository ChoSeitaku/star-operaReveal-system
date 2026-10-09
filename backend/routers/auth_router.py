"""认证接入模块。

职责：跳转认证中心发起授权、处理授权回调、换取访问令牌、建立/清除本地登录态、
识别当前用户。对上游是模拟认证中心，对下游是 HMAC 签名 Cookie，替换真实认证中心
只需保持 /oauth/token 与 /oauth/userinfo 契约。
"""
from __future__ import annotations

import re
import secrets
import time
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from backend import config, database, session_auth
from backend.utils import utc_now_iso

router = APIRouter(tags=["认证"])

STATE_PURPOSE = "oauth-state"
STATE_TTL_SECONDS = 300


def _make_state() -> str:
    return session_auth.sign_payload(
        {"ts": int(time.time()), "nonce": secrets.token_urlsafe(8)}, STATE_PURPOSE
    )


def _verify_state(state: str) -> bool:
    payload = session_auth.verify_payload(state, STATE_PURPOSE)
    if payload is None:
        return False
    return (time.time() - int(payload.get("ts", 0))) <= STATE_TTL_SECONDS


def _callback_url() -> str:
    return f"{config.public_base_url()}/auth/callback"

def _auth_center_url() -> str:
    """认证中心对外地址：未显式配置时跟随 PUBLIC_BASE_URL，仅把端口换成认证中心端口。"""
    configured = config.get("auth.auth_center_url")
    if configured and configured != config.CONFIG_DEFAULTS["auth.auth_center_url"]:
        return configured.rstrip("/")
    base = config.public_base_url()
    if not base:
        return configured.rstrip("/")
    return re.sub(":" + chr(92) + "d+$", "", base) + f":{config.auth_port()}"

@router.get("/auth/login", summary="跳转到认证中心发起授权登录")
async def login() -> RedirectResponse:
    auth_center = _auth_center_url()
    params = {
        "client_id": config.get("auth.client_id"),
        "redirect_uri": _callback_url(),
        "state": _make_state(),
    }
    return RedirectResponse(url=f"{auth_center}/auth/login?{urlencode(params)}", status_code=302)


@router.get("/auth/callback", summary="处理授权回调并建立登录态")
async def callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
) -> RedirectResponse:
    frontend_callback = f"{config.public_base_url()}/callback.html"

    if error:
        return RedirectResponse(
            url=f"{frontend_callback}?{urlencode({'status': 'error', 'message': f'认证中心返回错误: {error}'})}",
            status_code=302,
        )
    if not code:
        return RedirectResponse(
            url=f"{frontend_callback}?{urlencode({'status': 'error', 'message': '缺少授权码 code 参数'})}",
            status_code=302,
        )
    if not state or not _verify_state(state):
        return RedirectResponse(
            url=f"{frontend_callback}?{urlencode({'status': 'error', 'message': 'state 校验失败或已过期，请重新登录'})}",
            status_code=302,
        )

    auth_center = config.get("auth.auth_center_url").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            response = await client.post(
                f"{auth_center}/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": config.get("auth.client_id"),
                    "client_secret": config.get("auth.client_secret"),
                    "redirect_uri": _callback_url(),
                },
            )
    except httpx.HTTPError as exc:
        return RedirectResponse(
            url=f"{frontend_callback}?{urlencode({'status': 'error', 'message': f'无法连接认证中心: {exc}'})}",
            status_code=302,
        )

    if response.status_code != 200:
        detail = "令牌换取失败"
        try:
            detail = response.json().get("detail", detail)
        except ValueError:
            pass
        return RedirectResponse(
            url=f"{frontend_callback}?{urlencode({'status': 'error', 'message': detail})}",
            status_code=302,
        )

    payload = response.json()
    auth_user = payload.get("user") or {}
    user = await session_auth.provision_user(auth_user)

    redirect = RedirectResponse(
        url=(
            f"{frontend_callback}?status=ok"
            f"&display_name={quote(str(user['display_name']))}"
            f"&role={quote(str(user['role']))}"
        ),
        status_code=302,
    )
    session_auth.set_session_cookie(redirect, session_auth.sign_session(user["id"], user["role"]))
    return redirect


@router.post("/auth/logout", summary="退出登录（扩展接口）")
async def logout() -> JSONResponse:
    response = JSONResponse({"status": "ok", "message": "已退出登录"})
    session_auth.clear_session_cookie(response)
    return response


@router.get("/api/session", summary="查询登录态（扩展接口，始终返回 200）")
async def session_state(request: Request) -> dict:
    """公开页（入口页 / 回调页）用它判断是否已登录，避免 401 在控制台留下噪音。"""
    payload = session_auth.verify_session(request.cookies.get(session_auth.COOKIE_NAME))
    if payload is None:
        return {"authenticated": False, "user": None}

    row = await database.fetch_one(
        "SELECT * FROM users WHERE id = ? AND status = 'active'", (payload["uid"],)
    )
    if row is None:
        return {"authenticated": False, "user": None}
    return {
        "authenticated": True,
        "user": {
            "user_id": row["id"],
            "username": row["username"],
            "display_name": row["display_name"],
            "role": row["role"],
        },
    }


@router.get("/api/users/me", summary="获取当前登录用户（扩展接口）")
async def me(user: session_auth.CurrentUser) -> dict:
    return {
        "user_id": user["id"],
        "username": user["username"],
        "display_name": user["display_name"],
        "role": user["role"],
        "status": user["status"],
        "created_at": user["created_at"],
        "server_time": utc_now_iso(),
    }
