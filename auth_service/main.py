"""认证中心（模拟 OAuth2 授权码模式），独立服务，默认端口 8001。

流程：
    GET  /auth/login                     授权登录页（展示演示账号）
    GET  /auth/authorize                 签发授权码并 302 回 redirect_uri?code=&state=
    POST /oauth/token                    授权码换取访问令牌（服务端到服务端）
    GET  /oauth/userinfo                 由访问令牌获取用户信息
    GET  /healthz                        健康检查

说明：本服务为演示用模拟认证中心，账号与凭据均存于内存；生产请替换为真实认证中心
（只需保证 /oauth/token 与 /oauth/userinfo 两个接口契约一致）。
"""
from __future__ import annotations

import html
import os
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auth_service import users as store  # noqa: E402

app = FastAPI(title="认证中心（模拟）", version="0.1.0")

CLIENT_ID = os.environ.get("OAUTH_CLIENT_ID", "star-demo")
CLIENT_SECRET = os.environ.get("OAUTH_CLIENT_SECRET", "star-demo-secret")
BACKEND_PORT = os.environ.get("BACKEND_PORT", "8000")


def _allowed_redirects() -> set[str]:
    """回调地址白名单。默认只允许本机后端的 /auth/callback。"""
    raw = os.environ.get("OAUTH_ALLOWED_REDIRECTS", "")
    if raw:
        return {item.strip() for item in raw.split(",") if item.strip()}
    return {
        f"http://localhost:{BACKEND_PORT}/auth/callback",
        f"http://127.0.0.1:{BACKEND_PORT}/auth/callback",
    }


# 内联 SVG 图标：认证中心保持零静态资源依赖，同时避免浏览器请求 /favicon.ico 产生 404
FAVICON_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
    "<rect width='64' height='64' rx='14' fill='#4f8cff'/>"
    "<text x='32' y='45' font-size='36' font-family='sans-serif' font-weight='700'"
    " fill='white' text-anchor='middle'>星</text></svg>"
)
FAVICON_DATA_URI = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E"
    "%3Crect width='64' height='64' rx='14' fill='%234f8cff'/%3E"
    "%3Ctext x='32' y='45' font-size='36' font-family='sans-serif' font-weight='700'"
    " fill='white' text-anchor='middle'%3E%E6%98%9F%3C/text%3E%3C/svg%3E"
)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "service": "auth-center", "client_id": CLIENT_ID}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    return Response(content=FAVICON_SVG, media_type="image/svg+xml")


# ---------------------------------------------------------------- 授权登录页


_LOGIN_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>认证中心 · 授权登录</title>
<link rel="icon" href="{favicon}">
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
    background: radial-gradient(1200px 600px at 50% -10%, #1e3a5f 0%, #0b1220 55%, #05070d 100%);
    font-family: "Segoe UI", "Microsoft YaHei", system-ui, sans-serif; color: #e6edf7;
  }}
  .card {{
    width: min(460px, 92vw); background: rgba(17,26,44,.92); border: 1px solid #22314f;
    border-radius: 18px; padding: 34px 30px; box-shadow: 0 24px 60px rgba(0,0,0,.55);
  }}
  .brand {{ display: flex; align-items: center; gap: 12px; margin-bottom: 6px; }}
  .logo {{
    width: 38px; height: 38px; border-radius: 11px; display: grid; place-items: center;
    background: linear-gradient(135deg, #4f8cff, #7b5cff); font-weight: 700; font-size: 18px;
  }}
  h1 {{ font-size: 19px; margin: 0; letter-spacing: .3px; }}
  .sub {{ color: #8fa3c4; font-size: 13px; margin: 10px 0 22px; line-height: 1.7; }}
  .hint {{ background: #0e1729; border: 1px solid #1d2a44; border-radius: 10px; padding: 10px 13px;
           font-size: 12.5px; color: #9db0d0; margin-bottom: 20px; line-height: 1.6; }}
  .user {{
    display: flex; align-items: center; gap: 14px; width: 100%; text-align: left; cursor: pointer;
    background: #131f36; border: 1px solid #24344f; border-radius: 13px; padding: 15px 16px;
    color: inherit; font: inherit; margin-bottom: 12px; transition: .18s;
  }}
  .user:hover {{ border-color: #4f8cff; background: #17253f; transform: translateY(-1px); }}
  .avatar {{ width: 42px; height: 42px; border-radius: 50%; display: grid; place-items: center;
             font-size: 17px; font-weight: 600; flex: none; }}
  .a-admin {{ background: linear-gradient(135deg,#f2994a,#eb5757); }}
  .a-analyst {{ background: linear-gradient(135deg,#4f8cff,#56ccf2); }}
  .name {{ font-size: 15px; font-weight: 600; }}
  .meta {{ font-size: 12.5px; color: #8fa3c4; margin-top: 3px; }}
  .chev {{ margin-left: auto; color: #5c7297; font-size: 18px; }}
  footer {{ margin-top: 22px; font-size: 12px; color: #6b7f9e; text-align: center; line-height: 1.7; }}
</style>
</head>
<body>
  <main class="card">
    <div class="brand">
      <div class="logo">星</div>
      <h1>认证中心</h1>
    </div>
    <p class="sub">应用 <b>经营归因分析系统</b> 请求访问你的账号，请选择一个演示身份完成授权。</p>
    <div class="hint">client_id: <code>{client_id}</code><br>本页面为演示用模拟认证中心，凭据仅存于内存。</div>
    {options}
    <footer>授权后将跳转回 {redirect_display}</footer>
  </main>
</body>
</html>
"""

_USER_OPTION = """<form method="get" action="/auth/authorize">
  <input type="hidden" name="user" value="{username}">
  <input type="hidden" name="client_id" value="{client_id}">
  <input type="hidden" name="redirect_uri" value="{redirect_uri}">
  <input type="hidden" name="state" value="{state}">
  <button class="user" type="submit">
    <span class="avatar a-{username}">{initial}</span>
    <span>
      <span class="name">{display_name}</span>
      <span class="meta">{username} · {role_label}</span>
    </span>
    <span class="chev">›</span>
  </button>
</form>"""


@app.get("/auth/login", response_class=HTMLResponse)
async def login_page(
    client_id: str = "",
    redirect_uri: str = "",
    state: str = "",
) -> HTMLResponse:
    options = []
    for username, user in store.DEMO_USERS.items():
        options.append(
            _USER_OPTION.format(
                username=html.escape(username),
                client_id=html.escape(client_id),
                redirect_uri=html.escape(redirect_uri, quote=True),
                state=html.escape(state, quote=True),
                initial=html.escape(user.display_name[0]),
                display_name=html.escape(user.display_name),
                role_label="系统管理员" if user.role == "admin" else "分析用户",
            )
        )
    return HTMLResponse(
        _LOGIN_PAGE.format(
            favicon=FAVICON_DATA_URI,
            client_id=html.escape(client_id or CLIENT_ID),
            options="\n".join(options),
            redirect_display=html.escape(redirect_uri or "本机应用"),
        )
    )


@app.get("/auth/authorize")
async def authorize(
    user: str,
    client_id: str = CLIENT_ID,
    redirect_uri: str = "",
    state: str = "",
) -> RedirectResponse:
    if redirect_uri not in _allowed_redirects():
        raise HTTPException(status_code=400, detail=f"redirect_uri 不在白名单内: {redirect_uri}")
    if user not in store.DEMO_USERS:
        raise HTTPException(status_code=400, detail=f"未知的演示账号: {user}")

    entry = store.issue_code(user, client_id, redirect_uri)
    params = {"code": entry.code}
    if state:
        params["state"] = state
    separator = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(url=f"{redirect_uri}{separator}{urlencode(params)}", status_code=302)


# ---------------------------------------------------------------- 令牌接口


@app.post("/oauth/token")
async def token(
    grant_type: str = Form("authorization_code"),
    code: str = Form(""),
    client_id: str = Form(""),
    client_secret: str = Form(""),
    redirect_uri: str = Form(""),
) -> JSONResponse:
    if grant_type != "authorization_code":
        raise HTTPException(status_code=400, detail="仅支持 authorization_code 模式")
    if client_id != CLIENT_ID or client_secret != CLIENT_SECRET:
        raise HTTPException(status_code=401, detail="client_id 或 client_secret 无效")

    entry = store.consume_code(code, client_id, redirect_uri)
    if entry is None:
        raise HTTPException(status_code=400, detail="授权码无效、已使用或已过期")

    user = store.DEMO_USERS[entry.username]
    access = store.issue_token(entry.username)
    return JSONResponse(
        {
            "access_token": access.token,
            "token_type": "Bearer",
            "expires_in": store.TOKEN_TTL_SECONDS,
            "user": {
                "id": user.username,
                "username": user.username,
                "display_name": user.display_name,
                "role": user.role,
                "email": user.email,
            },
        }
    )


@app.get("/oauth/userinfo")
async def userinfo(request: Request) -> JSONResponse:
    header = request.headers.get("authorization", "")
    token_value = header[7:].strip() if header.lower().startswith("bearer ") else ""
    user = store.lookup_token(token_value) if token_value else None
    if user is None:
        raise HTTPException(status_code=401, detail="访问令牌无效或已过期")
    return JSONResponse(
        {
            "id": user.username,
            "username": user.username,
            "display_name": user.display_name,
            "role": user.role,
            "email": user.email,
        }
    )
