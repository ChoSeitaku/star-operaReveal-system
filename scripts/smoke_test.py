#!/usr/bin/env python3
"""端到端验收测试：无头驱动完整的 REST + WebSocket 链路。

覆盖规格书 1.1.1–1.1.13 的主要验收点：
    授权登录 → 会话 CRUD → 附件上传/解析/下载/删除 → 长连接鉴权 →
    分析任务全生命周期（8 类实时消息）→ 单会话单任务 → 取消 →
    历史回放与序号 → 六段式结果 → 导出下载 → 管理与配置热更新 → 级联删除

用法：
    python scripts/smoke_test.py                    # 假定服务已在运行
    python scripts/smoke_test.py --start-server     # 自行拉起 run.py（结束后关闭）

退出码 0 表示全部通过。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

BASE = "http://localhost:8000"
AUTH = "http://localhost:8001"
WS_BASE = "ws://localhost:8000"

# 规格书 1.1.10 定义的服务端 → 客户端消息类型
SPEC_MESSAGE_TYPES = {
    "message_start", "message_delta", "tool_start", "tool_finish",
    "task_status", "result_ready", "error", "done",
}
# 规格书 1.1.11 定义的六段式结果
RESULT_FIELDS = (
    "problem_definition", "key_metrics", "evidence_list",
    "conclusion_text", "missing_data_text", "next_action_text",
)

CATALOG_QUESTION = "近30天各品类的搜索曝光到转化漏斗表现如何？哪些品类存在曝光转化不匹配的问题，主要原因是什么？"
CUSTOMER_QUESTION = "各渠道和用户分层在访问→加购→下单漏斗上的转化表现如何？哪个环节流失最严重，主要原因是什么？"

PASSED: list[str] = []
FAILED: list[str] = []


def check(condition: bool, title: str, detail: str = "") -> bool:
    if condition:
        PASSED.append(title)
        print(f"  ✔ {title}" + (f"　{detail}" if detail else ""))
    else:
        FAILED.append(f"{title}　{detail}")
        print(f"  ✘ {title}" + (f"　{detail}" if detail else ""))
    return condition


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


# ---------------------------------------------------------------- 登录

async def login(client: httpx.AsyncClient, user: str) -> bool:
    """完整走一遍授权码流程：后端发起 → 认证中心授权 → 后端回调换取令牌。"""
    response = await client.get(f"{BASE}/auth/login", follow_redirects=False)
    if response.status_code != 302:
        return False
    location = response.headers.get("location", "")
    if "/auth/login" not in location:
        return False
    query = parse_qs(urlparse(location).query)
    if "state" not in query or "redirect_uri" not in query:
        return False

    authorize = await client.get(
        f"{AUTH}/auth/authorize",
        params={
            "user": user,
            "client_id": query["client_id"][0],
            "redirect_uri": query["redirect_uri"][0],
            "state": query["state"][0],
        },
        follow_redirects=False,
    )
    if authorize.status_code != 302:
        return False
    callback = authorize.headers.get("location", "")
    code = parse_qs(urlparse(callback).query).get("code", [""])[0]
    if not code:
        return False

    final = await client.get(callback, follow_redirects=False)
    return final.status_code == 302 and "status=ok" in final.headers.get("location", "")


async def ws_uri(client: httpx.AsyncClient, conversation_id: int) -> str:
    token = await client.post(
        f"{BASE}/api/chat/ws-token", json={"conversation_id": conversation_id}
    )
    payload = token.json()
    return f"{WS_BASE}/api/chat/ws/chat?websocket_token={payload['websocket_token']}&conversation_id={conversation_id}"


async def read_until(ws, wanted: set[str], timeout: float = 180.0):
    seen: list[dict] = []
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise TimeoutError(f"等待 {wanted} 超时，已收到 {[e['type'] for e in seen]}")
        event = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
        if event.get("type") in wanted:
            return event, seen
        seen.append(event)


# ---------------------------------------------------------------- 各验收段

async def test_health() -> None:
    section("① 服务健康检查")
    async with httpx.AsyncClient(timeout=15) as client:
        for url, name in ((f"{BASE}/api/health", "分析后端"), (f"{AUTH}/healthz", "认证中心")):
            try:
                response = await client.get(url)
                check(response.status_code == 200, f"{name} 健康检查", url)
            except httpx.HTTPError as exc:
                check(False, f"{name} 健康检查", str(exc))


async def test_login() -> None:
    section("② 授权登录（认证中心 + 回调 + 登录态）")
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        # 认证中心授权页
        response = await client.get(
            f"{AUTH}/auth/login",
            params={"client_id": "star-demo", "redirect_uri": f"{BASE}/auth/callback", "state": "x"},
        )
        check(
            response.status_code == 200 and "数据分析师" in response.text and "系统管理员" in response.text,
            "认证中心授权页返回两个演示账号",
        )

        # 非法回调地址白名单
        bad = await client.get(
            f"{AUTH}/auth/authorize",
            params={"user": "analyst", "redirect_uri": "http://evil.example/steal", "state": "x"},
        )
        check(bad.status_code == 400, "拒绝白名单外的 redirect_uri", f"HTTP {bad.status_code}")

        # 正常登录
        ok = await login(client, "analyst")
        check(ok, "分析用户授权登录成功并下发会话 Cookie")
        me = await client.get(f"{BASE}/api/users/me")
        check(
            me.status_code == 200 and me.json().get("role") == "analyst",
            "登录态识别当前用户",
            me.json().get("display_name", "") if me.status_code == 200 else "",
        )

    # 未登录访问
    async with httpx.AsyncClient(timeout=15) as anonymous:
        response = await anonymous.get(f"{BASE}/api/users/me")
        check(response.status_code == 401, "未携带 Cookie 访问受保护接口返回 401")
        response = await anonymous.get(f"{BASE}/api/session")
        check(
            response.status_code == 200 and response.json().get("authenticated") is False,
            "/api/session 对未登录返回 200 且 authenticated=false",
        )


async def test_conversation_and_attachment(client: httpx.AsyncClient) -> int:
    section("③ 会话管理与附件（上传 / 解析 / 下载 / 删除 / 越界校验）")
    created = (await client.post(f"{BASE}/api/chat/create", json={"title": "验收会话"})).json()
    cid = created["conversation_id"]
    check(
        created.get("conversation_id") and created.get("status") == "active",
        "新建会话返回 conversation_id / title / status",
        json.dumps(created, ensure_ascii=False),
    )

    listed = (await client.get(f"{BASE}/api/chat/ls")).json()
    check(
        any(c["conversation_id"] == cid for c in listed["conversations"]),
        "会话列表包含新建的会话",
        f"共 {listed['total']} 个",
    )

    renamed = (await client.post(
        f"{BASE}/api/chat/update", json={"conversation_id": cid, "title": "验收会话（已重命名）"}
    )).json()
    check(renamed["title"] == "验收会话（已重命名）", "重命名会话")

    # 附件：UTF-8 与 GBK 双编码
    for name in ("sample_metrics.csv", "sample_metrics_gbk.csv"):
        path = BASE_DIR / "scripts" / "fixtures" / name
        upload = await client.post(
            f"{BASE}/api/attachment/upload",
            data={"conversation_id": str(cid)},
            files={"file": (name, path.read_bytes(), "text/csv")},
        )
        check(
            upload.status_code == 200 and upload.json().get("attachment_id"),
            f"上传附件 {name}",
            upload.json().get("size_text", ""),
        )

    # 等解析完成
    attachments = []
    for _ in range(20):
        await asyncio.sleep(0.6)
        attachments = (await client.get(
            f"{BASE}/api/attachment/ls", params={"conversation_id": cid}
        )).json()["attachments"]
        if attachments and all(a["parse_status"] in ("success", "failed") for a in attachments):
            break
    check(
        attachments and all(a["parse_status"] == "success" for a in attachments),
        "附件解析完成（含 GBK 编码文件）",
        " / ".join(f"{a['file_name']}={a['parse_status']}" for a in attachments),
    )

    # 编码回退确实生效
    gbk = next((a for a in attachments if "gbk" in a["file_name"]), None)
    if gbk:
        meta_path = BASE_DIR / "data" / (gbk["file_path"] + ".meta.json")
        encoding = ""
        if meta_path.exists():
            encoding = json.loads(meta_path.read_text(encoding="utf-8")).get("encoding", "")
        check(encoding in ("gb18030", "gbk"), "GBK 文件按 gb18030 回退解码", f"encoding={encoding}")

    # 下载
    attachment_id = attachments[0]["attachment_id"]
    download = await client.get(f"{BASE}/api/attachment/get", params={"attachment_id": attachment_id})
    check(
        download.status_code == 200 and len(download.content) > 0,
        "下载附件",
        f"{len(download.content)} bytes",
    )

    # 路径穿越
    traversal = await client.post(
        f"{BASE}/api/attachment/upload",
        data={"conversation_id": str(cid)},
        files={"file": ("../../evil.csv", b"a,b\n1,2\n", "text/csv")},
    )
    escaped = list((BASE_DIR / "data").rglob("*evil*"))
    check(
        traversal.status_code == 200 and bool(escaped)
        and all(p.resolve().is_relative_to((BASE_DIR / "data").resolve()) for p in escaped),
        "路径穿越文件名被清洗（文件仍落在数据目录内）",
        str(escaped[0].relative_to(BASE_DIR)) if escaped else "",
    )
    # 通过接口删除，避免留下指向已删文件的孤儿记录
    if traversal.status_code == 200:
        await asyncio.sleep(0.5)
        await client.post(
            f"{BASE}/api/attachment/delete",
            json={"attachment_id": traversal.json()["attachment_id"]},
        )

    # 非法类型与空文件
    bad_type = await client.post(
        f"{BASE}/api/attachment/upload",
        data={"conversation_id": str(cid)},
        files={"file": ("x.exe", b"MZ", "application/octet-stream")},
    )
    check(bad_type.status_code == 400, "拒绝不在白名单内的文件类型", bad_type.json().get("detail", "")[:60])
    empty = await client.post(
        f"{BASE}/api/attachment/upload",
        data={"conversation_id": str(cid)},
        files={"file": ("empty.csv", b"", "text/csv")},
    )
    check(empty.status_code == 400, "拒绝空文件")

    # 删除附件
    removed = await client.post(f"{BASE}/api/attachment/delete", json={"attachment_id": attachment_id})
    check(removed.json().get("deleted") is True, "删除附件")

    return cid


async def test_websocket_auth(client: httpx.AsyncClient, cid: int) -> None:
    section("④ 长连接鉴权（一次性令牌 / 越权 / 伪造）")
    import websockets

    uri = await ws_uri(client, cid)
    async with websockets.connect(uri) as ws:
        hello = json.loads(await ws.recv())
        check(hello.get("type") == "connected", "令牌换取长连接成功")
        check(
            len(hello.get("message_types", [])) == len(SPEC_MESSAGE_TYPES),
            "服务端声明规格书 8 类实时消息",
            "、".join(hello.get("message_types", [])),
        )

        # 同一令牌二次使用
        reused = "未被拒绝"
        try:
            async with websockets.connect(uri) as second:
                await second.recv()
        except Exception as exc:  # noqa: BLE001
            reused = f"code={getattr(exc, 'code', '?')}"
        check("code=4401" in reused, "一次性令牌重复使用被拒绝", reused)

        # 伪造令牌
        forged = "未被拒绝"
        try:
            async with websockets.connect(
                f"{WS_BASE}/api/chat/ws/chat?websocket_token=forged-token&conversation_id={cid}"
            ) as bad:
                await bad.recv()
        except Exception as exc:  # noqa: BLE001
            forged = f"code={getattr(exc, 'code', '?')}"
        check("code=4401" in forged, "伪造令牌被拒绝", forged)

        # ping / pong 与未知类型
        await ws.send(json.dumps({"type": "ping"}))
        pong = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        check(pong.get("type") == "pong", "心跳 ping/pong")
        await ws.send(json.dumps({"type": "unknown_type"}))
        err = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        check(err.get("type") == "error", "未知消息类型返回 error 事件")

    # 越权：用另一个会话的令牌连到本会话
    other = (await client.post(f"{BASE}/api/chat/create", json={"title": "越权测试"})).json()
    token = (await client.post(f"{BASE}/api/chat/ws-token", json={"conversation_id": other["conversation_id"]})).json()
    mismatch = "未被拒绝"
    try:
        async with websockets.connect(
            f"{WS_BASE}/api/chat/ws/chat?websocket_token={token['websocket_token']}&conversation_id={cid}"
        ) as bad:
            await bad.recv()
    except Exception as exc:  # noqa: BLE001
        mismatch = f"code={getattr(exc, 'code', '?')}"
    check("code=4401" in mismatch, "令牌与会话不匹配被拒绝", mismatch)
    await client.post(f"{BASE}/api/chat/delete", json={"conversation_ids": [other["conversation_id"]]})


async def run_analysis(client: httpx.AsyncClient, cid: int, question: str, label: str) -> int:
    """发送一条消息并收完整个事件流，返回 task_id。"""
    import websockets

    uri = await ws_uri(client, cid)
    async with websockets.connect(uri, max_size=8 * 1024 * 1024) as ws:
        await ws.recv()  # connected
        await ws.send(json.dumps({"type": "user_message", "content": question}))
        accepted, _ = await read_until(ws, {"user_message_accepted"}, timeout=20)
        task_id = accepted["task_id"]
        done, seen = await read_until(ws, {"done"}, timeout=240)

        kinds = [e["type"] for e in seen] + ["done"]
        missing = (SPEC_MESSAGE_TYPES - {"error"}) - set(kinds)
        check(not missing, f"[{label}] 实时消息覆盖规格书类型", "、".join(kinds))
        check(
            done.get("task_status") == "success",
            f"[{label}] 任务以 success 结束",
            f"task_id={task_id}",
        )
        tool_events = [e for e in seen if e["type"] == "tool_finish"]
        check(len(tool_events) >= 2, f"[{label}] 工具执行过程可见", f"{len(tool_events)} 次工具调用")

    return task_id


async def test_analysis(client: httpx.AsyncClient, cid: int) -> list[int]:
    section("⑤ 分析任务全生命周期（实时消息 / 结果 / 导出）")
    task_ids = []
    for question, label in ((CATALOG_QUESTION, "商品目录优化"), (CUSTOMER_QUESTION, "客户行为分析")):
        task_id = await run_analysis(client, cid, question, label)
        task_ids.append(task_id)

        task = (await client.get(f"{BASE}/api/tasks/{task_id}")).json()
        check(
            set(task) >= {"task_status", "current_step", "started_at", "finished_at", "error_message"},
            f"[{label}] 任务查询接口字段完整",
        )

        result = (await client.get(f"{BASE}/api/results/{task_id}")).json()
        check(
            all(field in result for field in RESULT_FIELDS),
            f"[{label}] 结果包含六段式字段",
            "、".join(RESULT_FIELDS),
        )
        check(
            len(result["key_metrics"]) >= 3 and len(result["evidence_list"]) >= 2,
            f"[{label}] 关键指标与证据数量充足",
            f"指标 {len(result['key_metrics'])} 项 / 证据 {len(result['evidence_list'])} 条",
        )
        suggestions = [s for s in result["next_action_text"].splitlines() if s.strip()]
        check(len(suggestions) >= 2, f"[{label}] 下一步建议至少两条", f"{len(suggestions)} 条")

        download = await client.get(f"{BASE}/api/results/{task_id}/download")
        check(
            download.status_code == 200 and len(download.content) > 500,
            f"[{label}] 导出结果文件可下载",
            f"{len(download.content)} bytes",
        )
        check(
            result["key_metrics"][0]["metric_name"] != "",
            f"[{label}] 关键指标字段结构正确",
            json.dumps(result["key_metrics"][0], ensure_ascii=False)[:80],
        )

    # 历史回放与配对不变式
    history = (await client.get(f"{BASE}/api/chat/ls/{cid}")).json()
    messages = history["messages"]
    seqs = [m["seq_no"] for m in messages]
    check(seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "历史消息 seq_no 严格递增且唯一")
    check(messages[0]["role"] == "user", "历史首条为用户消息")
    check(messages[-1]["role"] == "assistant", "历史末条为助手消息", messages[-1]["content"][:40])
    user_messages = [m for m in messages if m["role"] == "user"]
    tasks_with_message = [m for m in messages if m.get("task")]
    check(
        len(user_messages) == len(tasks_with_message),
        "用户消息与任务一一对应（配对不变式）",
        f"{len(user_messages)} ↔ {len(tasks_with_message)}",
    )
    check(
        all(m["task"].get("has_result") for m in tasks_with_message),
        "每条用户消息都能关联到结构化结果",
    )
    return task_ids


async def test_concurrency(client: httpx.AsyncClient, cid: int) -> None:
    section("⑥ 单会话单任务与取消")
    import websockets

    uri = await ws_uri(client, cid)
    async with websockets.connect(uri) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "user_message", "content": CATALOG_QUESTION}))
        accepted, _ = await read_until(ws, {"user_message_accepted"}, timeout=20)
        running_task = accepted["task_id"]

        # 单飞：任务运行中再发一条应被拒绝
        await ws.send(json.dumps({"type": "user_message", "content": "并发第二条消息"}))
        busy, _ = await read_until(ws, {"error", "user_message_accepted"}, timeout=20)
        check(busy.get("type") == "error", "同一会话并发任务被拒绝", busy.get("error_message", "")[:60])

        # 取消
        await ws.send(json.dumps({"type": "cancel", "task_id": running_task}))
        done, seen = await read_until(ws, {"done"}, timeout=120)
        check(done.get("task_status") == "cancelled", "取消任务后 done 事件状态为 cancelled")

    # 被拒绝的消息不得落库
    history = (await client.get(f"{BASE}/api/chat/ls/{cid}")).json()
    check(
        all("并发第二条消息" not in m["content"] for m in history["messages"]),
        "被单飞拒绝的消息未写入历史",
    )
    user_messages = [m for m in history["messages"] if m["role"] == "user"]
    tasks_with_message = [m for m in history["messages"] if m.get("task")]
    check(len(user_messages) == len(tasks_with_message), "取消后配对不变式仍成立")


async def test_admin(analyst: httpx.AsyncClient) -> None:
    section("⑦ 系统管理（权限 / 配置热更新 / 日志 / 数据源 / 工具自检）")
    forbidden = await analyst.get(f"{BASE}/api/admin/config")
    check(forbidden.status_code == 403, "分析用户访问管理接口返回 403")

    async with httpx.AsyncClient(timeout=180, follow_redirects=True) as admin:
        await login(admin, "admin")
        me = (await admin.get(f"{BASE}/api/users/me")).json()
        check(me.get("role") == "admin", "系统管理员登录", me.get("display_name", ""))

        config = (await admin.get(f"{BASE}/api/admin/config")).json()
        check(config["total"] >= 30 and len(config["groups"]) == 4, "查看配置分组", "、".join(config["groups"]))
        api_key = next(i for i in config["items"] if i["config_key"] == "llm.api_key")
        check(api_key["config_value"] == "******", "敏感配置在管理接口中脱敏")
        check(
            "overridden_by_env" in api_key and "editable" in api_key,
            "配置项标注是否被环境变量覆盖",
        )

        # 配置热更新：改完后新任务立即使用新引擎
        original = config["engine_mode"]
        target = "canned" if original != "canned" else "demo"
        updated = await admin.post(f"{BASE}/api/admin/config", json={"configs": {"analysis.engine_mode": target}})
        check(updated.json()["status"] == "ok", f"修改配置 analysis.engine_mode → {target}")

        reload_resp = (await admin.post(f"{BASE}/api/admin/reload")).json()
        check(
            reload_resp["status"] == "ok" and reload_resp.get("engine_mode") == target,
            "重载配置后新值即时生效",
            reload_resp["message"],
        )

        unknown = await admin.post(f"{BASE}/api/admin/config", json={"configs": {"nope.key": "1"}})
        check(unknown.status_code == 400, "拒绝未知配置项")

        # 还原
        await admin.post(f"{BASE}/api/admin/config", json={"configs": {"analysis.engine_mode": original}})
        await admin.post(f"{BASE}/api/admin/reload")

        logs = (await admin.get(f"{BASE}/api/admin/logs", params={"limit": 50})).json()
        check(logs["total"] > 0, "查看运行日志", f"{logs['total']} 条")
        check(
            any("重载配置" in l["log_content"] for l in logs["logs"]),
            "配置变更被记入运行日志",
        )

        sources = (await admin.get(f"{BASE}/api/admin/datasources")).json()
        check(sources["total"] == 6, "数据源连接列表", "、".join(s["name"] for s in sources["datasources"]))
        check(
            all(s["table_count"] >= 4 for s in sources["datasources"]),
            "六个情景库表结构完整",
        )

        stats = (await admin.get(f"{BASE}/api/admin/stats")).json()
        check(
            stats["tasks_total"] > 0 and stats["results"] > 0,
            "运行状态概览",
            f"任务 {stats['tasks_total']} / 结果 {stats['results']} / 引擎 {stats['engine_mode']}",
        )

        selftest = (await admin.post(f"{BASE}/api/admin/selftest")).json()
        check(
            selftest["passed"] == selftest["total"],
            "六个分析工具自检全部通过",
            "、".join(f"{r['tool']}{'✔' if r['ok'] else '✘'}" for r in selftest["results"]),
        )


async def test_cascade_delete(client: httpx.AsyncClient) -> None:
    section("⑧ 会话级联删除（子表记录 + 文件目录）")
    import sqlite3

    created = (await client.post(f"{BASE}/api/chat/create", json={"title": "待删除会话"})).json()
    cid = created["conversation_id"]
    path = BASE_DIR / "scripts" / "fixtures" / "sample_metrics.csv"
    await client.post(
        f"{BASE}/api/attachment/upload",
        data={"conversation_id": str(cid)},
        files={"file": ("keep.csv", path.read_bytes(), "text/csv")},
    )
    await asyncio.sleep(1.5)

    db_path = BASE_DIR / "data" / "system.db"
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys=ON")
    user_id = connection.execute(
        "SELECT user_id FROM conversations WHERE id = ?", (cid,)
    ).fetchone()[0]
    now = "2026-10-08T00:00:00Z"
    connection.execute(
        "INSERT INTO messages (conversation_id, role, message_type, content, seq_no, created_at)"
        " VALUES (?, 'user', 'text', '测试', 1, ?)", (cid, now),
    )
    message_id = connection.execute(
        "SELECT id FROM messages WHERE conversation_id = ?", (cid,)
    ).fetchone()[0]
    connection.execute("UPDATE attachments SET message_id = ? WHERE conversation_id = ?", (message_id, cid))
    connection.execute(
        "INSERT INTO analysis_tasks (conversation_id, user_id, input_text, task_status)"
        " VALUES (?, ?, '测试', 'success')", (cid, user_id),
    )
    task_id = connection.execute(
        "SELECT id FROM analysis_tasks WHERE conversation_id = ?", (cid,)
    ).fetchone()[0]
    connection.execute(
        "INSERT INTO task_logs (task_id, log_level, log_type, log_content, created_at)"
        " VALUES (?, 'info', 'step', 'x', ?)", (task_id, now),
    )
    connection.execute(
        "INSERT INTO analysis_results (task_id, conversation_id, problem_definition, key_metrics_json,"
        " evidence_list_json, conclusion_text, missing_data_text, next_action_text, result_markdown, created_at)"
        " VALUES (?, ?, 'p', '[]', '[]', 'c', 'm', 'n', '# r', ?)", (task_id, cid, now),
    )
    connection.execute(
        "INSERT INTO context_summaries (conversation_id, start_seq_no, end_seq_no, summary_text, created_at)"
        " VALUES (?, 1, 1, 's', ?)", (cid, now),
    )
    connection.execute(
        "INSERT INTO websocket_tokens (user_id, conversation_id, token, expires_at, created_at)"
        " VALUES (?, ?, 'tok-del', ?, ?)", (user_id, cid, now, now),
    )
    connection.commit()
    connection.close()

    directories = [BASE_DIR / "data" / kind / str(user_id) / str(cid)
                   for kind in ("uploads", "exports", "workspace")]
    check(
        all(d.exists() for d in directories),
        "删除前 uploads / exports / workspace 三个目录均存在",
    )

    deleted = (await client.post(f"{BASE}/api/chat/delete", json={"conversation_ids": [cid]})).json()
    check(deleted["deleted_count"] == 1, "删除会话成功")

    connection = sqlite3.connect(db_path)
    remaining = {
        "messages": connection.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (cid,)).fetchone()[0],
        "attachments": connection.execute("SELECT COUNT(*) FROM attachments WHERE conversation_id = ?", (cid,)).fetchone()[0],
        "analysis_tasks": connection.execute("SELECT COUNT(*) FROM analysis_tasks WHERE conversation_id = ?", (cid,)).fetchone()[0],
        "analysis_results": connection.execute("SELECT COUNT(*) FROM analysis_results WHERE conversation_id = ?", (cid,)).fetchone()[0],
        "context_summaries": connection.execute("SELECT COUNT(*) FROM context_summaries WHERE conversation_id = ?", (cid,)).fetchone()[0],
        "websocket_tokens": connection.execute("SELECT COUNT(*) FROM websocket_tokens WHERE conversation_id = ?", (cid,)).fetchone()[0],
        "task_logs": connection.execute("SELECT COUNT(*) FROM task_logs WHERE task_id = ?", (task_id,)).fetchone()[0],
    }
    status = connection.execute("SELECT status FROM conversations WHERE id = ?", (cid,)).fetchone()
    connection.close()

    check(all(v == 0 for v in remaining.values()), "子表记录全部级联删除", json.dumps(remaining, ensure_ascii=False))
    check(status is not None and status[0] == "deleted", "会话行保留且状态为 deleted（软删除）")
    check(not any(d.exists() for d in directories), "附件 / 导出 / 临时目录已随会话删除")

    listed = (await client.get(f"{BASE}/api/chat/ls")).json()
    check(
        all(c["conversation_id"] != cid for c in listed["conversations"]),
        "已删除会话不再出现在列表中",
    )


async def test_static_pages() -> None:
    section("⑨ 前端页面与静态资源")
    async with httpx.AsyncClient(timeout=15) as client:
        pages = {
            "/": "经营归因分析系统",
            "/callback.html": "登录回调",
            "/app": "聊天工作台",
            "/admin": "系统管理",
        }
        for path, keyword in pages.items():
            response = await client.get(f"{BASE}{path}")
            check(
                response.status_code == 200 and keyword in response.text,
                f"页面可访问 {path}",
                keyword,
            )
        for asset in ("/css/app.css", "/js/app.js", "/js/ws.js", "/favicon.ico"):
            response = await client.get(f"{BASE}{asset}")
            check(response.status_code == 200 and len(response.content) > 50, f"静态资源可访问 {asset}")

        for module in ("api", "auth", "session", "chat", "attachment", "result", "config", "logs", "ws", "components"):
            response = await client.get(f"{BASE}/js/{module}.js")
            check(response.status_code == 200, f"前端模块 {module}.js")


# ---------------------------------------------------------------- 运行器


def wait_health(url: str, timeout: float = 45.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.4)
    return False


async def main_async() -> int:
    print("=" * 72)
    print("  经营归因分析系统 —— 端到端验收测试")
    print("=" * 72)

    await test_health()
    if FAILED:
        print("\n服务未就绪，请先执行：python run.py")
        return 1

    await test_login()

    async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
        await login(client, "analyst")
        cid = await test_conversation_and_attachment(client)
        await test_websocket_auth(client, cid)
        await test_analysis(client, cid)
        await test_concurrency(client, cid)
        await test_cascade_delete(client)

    async with httpx.AsyncClient(timeout=300, follow_redirects=True) as analyst:
        await login(analyst, "analyst")
        await test_admin(analyst)

    await test_static_pages()

    section("验收汇总")
    total = len(PASSED) + len(FAILED)
    print(f"  通过 {len(PASSED)} / {total}")
    if FAILED:
        print("\n失败项：")
        for item in FAILED:
            print(f"  ✘ {item}")
        return 1
    print("\n  全部验收项通过 ✔")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="端到端验收测试")
    parser.add_argument("--start-server", action="store_true", help="自动拉起 run.py 并在结束后关闭")
    args = parser.parse_args()

    process = None
    if args.start_server:
        print("[smoke] 启动服务…")
        process = subprocess.Popen(
            [sys.executable, str(BASE_DIR / "run.py")],
            cwd=str(BASE_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if not wait_health(f"{BASE}/api/health"):
            print("[smoke] 服务启动失败")
            process.terminate()
            return 1

    try:
        return asyncio.run(main_async())
    finally:
        if process is not None:
            print("\n[smoke] 关闭服务…")
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    sys.exit(main())
