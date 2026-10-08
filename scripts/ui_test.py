#!/usr/bin/env python3
"""浏览器端到端测试：用真实浏览器（Edge/Chromium）走通完整用户链路。

覆盖：入口页 → 认证中心授权 → 工作台 → 新建会话 → 上传附件 → 提问 → 实时过程 →
六段式结果 → 复制/导出 → 切换会话 → 管理台。
同时收集浏览器控制台错误与页面异常。
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://localhost:8000"
SHOTS = Path("data/_shots")
QUESTION = "近30天各品类的搜索曝光到转化漏斗表现如何？哪些品类存在曝光转化不匹配的问题，主要原因是什么？"

console_errors: list[str] = []
page_errors: list[str] = []
failed_requests: list[str] = []


def run() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1680, "height": 950})
        page = context.new_page()
        page.on("console", lambda m: console_errors.append(f"[{m.type}] {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: page_errors.append(str(e)))
        page.on("response", lambda r: failed_requests.append(f"{r.status} {r.request.method} {r.url}")
                if r.status >= 400 else None)

        # ---- 1) 入口页 ----
        page.goto(f"{BASE}/", wait_until="networkidle")
        assert "经营归因分析系统" in page.title(), page.title()
        print("1) 入口页 OK:", page.title())
        page.screenshot(path=str(SHOTS / "01-index.png"))

        # ---- 2) 授权登录 ----
        page.click("#login-btn")
        page.wait_for_load_state("networkidle")
        assert "认证中心" in page.title(), page.title()
        assert "数据分析师" in page.content()
        print("2) 认证中心授权页 OK:", page.title())
        page.screenshot(path=str(SHOTS / "02-auth-center.png"))

        page.click("form:has(input[value='analyst']) button")
        page.wait_for_url("**/app", timeout=20000)
        page.wait_for_load_state("networkidle")
        print("3) 回调跳转工作台 OK:", page.url)

        # 工作台骨架
        page.wait_for_selector("#conv-list", timeout=10000)
        page.wait_for_function("() => document.querySelector('#user-box')?.textContent?.includes('数据分析师')", timeout=10000)
        print("   顶部用户信息:", page.locator("#user-box").inner_text().replace("\n", " "))
        page.screenshot(path=str(SHOTS / "03-workspace-empty.png"))

        # ---- 4) 新建会话 ----
        page.click("#new-conv")
        page.wait_for_function("() => document.querySelectorAll('.conv-item').length >= 1", timeout=15000)
        page.wait_for_function("() => document.querySelector('#conv-title')?.textContent !== '未选择会话'", timeout=10000)
        conv_count = page.locator(".conv-item").count()
        print(f"4) 新建会话 OK，会话数 {conv_count}")

        # ---- 5) 上传附件（验证进度与解析状态）----
        page.click(".tab[data-tab='attach']")
        page.set_input_files("#file-input", "scripts/fixtures/sample_metrics.csv")
        page.wait_for_function(
            "() => Array.from(document.querySelectorAll('#attach-list .tag')).some(e => e.textContent.includes('已完成'))",
            timeout=30000,
        )
        attach_text = page.locator("#attach-list").inner_text().replace("\n", " | ")
        print("5) 附件上传并解析 OK:", attach_text[:110])
        page.screenshot(path=str(SHOTS / "04-attachment.png"))

        # ---- 6) 提问并观察实时过程 ----
        page.click(".tab[data-tab='task']")
        page.fill("#composer-input", QUESTION)
        page.click("#send-btn")

        page.wait_for_function(
            "() => Array.from(document.querySelectorAll('#task-panel .lc')).some(e => e.textContent.includes('db_query'))",
            timeout=60000,
        )
        print("6) 实时任务区已出现工具执行记录")
        page.screenshot(path=str(SHOTS / "05-running.png"))

        # 等待分析完成
        page.wait_for_function(
            "() => Array.from(document.querySelectorAll('#task-panel .tag')).some(e => e.textContent.includes('已完成'))",
            timeout=180000,
        )
        print("   任务状态：已完成")

        # ---- 7) 六段式结果 ----
        page.click(".tab[data-tab='result']")
        page.wait_for_selector("#result-panel .sec", timeout=20000)
        sections = page.locator("#result-panel .sec .sh").all_inner_texts()
        print("7) 结果区六段:", " / ".join(s.replace("\n", " ")[:14] for s in sections))
        assert len(sections) >= 6, f"结果段数不足：{sections}"

        metrics = page.locator("#result-panel table.metrics tbody tr").count()
        evidence = page.locator("#result-panel .evidence").count()
        print(f"   关键指标 {metrics} 项，证据 {evidence} 条")
        assert metrics >= 3 and evidence >= 2, "指标或证据过少"
        page.screenshot(path=str(SHOTS / "06-result.png"))
        page.screenshot(path=str(SHOTS / "07-result-full.png"), full_page=True)

        # ---- 8) 导出与复制 ----
        with page.expect_download(timeout=30000) as dl:
            page.click("#result-panel .result-actions button:has-text('导出文件')")
        download = dl.value
        path = download.path()
        size = path.stat().st_size if path else 0
        print(f"8) 导出文件 OK: {download.suggested_filename} ({size} bytes)")
        assert size > 500

        page.click("#result-panel .result-actions button:has-text('复制 Markdown')")
        page.wait_for_selector(".toast", timeout=8000)
        print("   复制反馈:", page.locator(".toast").first.inner_text())

        # ---- 9) 历史回放与多轮追问 ----
        page.reload(wait_until="networkidle")
        page.wait_for_selector("#chat-scroll .msg", timeout=20000)
        msgs = page.locator("#chat-scroll .msg").count()
        tools = page.locator("#chat-scroll .msg-tool").count()
        print(f"9) 刷新后历史回放 OK：消息 {msgs} 条（其中工具卡片 {tools} 个）")
        assert msgs >= 2

        # 第二轮追问
        page.fill("#composer-input", "家居品类的主要问题出在哪几个关键词上？")
        page.click("#send-btn")
        page.wait_for_function(
            "() => document.querySelectorAll('#chat-scroll .msg-user').length >= 2",
            timeout=30000,
        )
        page.wait_for_function(
            "() => Array.from(document.querySelectorAll('#task-panel .tag')).some(e => e.textContent.includes('已完成'))",
            timeout=180000,
        )
        print("   第二轮追问完成")
        page.screenshot(path=str(SHOTS / "08-followup.png"))

        # ---- 10) 管理台（分析用户应无权限）----
        page.goto(f"{BASE}/admin", wait_until="networkidle")
        body = page.locator("#admin-body").inner_text()
        assert "需要系统管理员权限" in body, body[:200]
        print("10) 分析用户访问管理台被正确拒绝")

        # ---- 11) 以管理员登录并操作管理台 ----
        context2 = browser.new_context(viewport={"width": 1680, "height": 950})
        admin = context2.new_page()
        admin.on("console", lambda m: console_errors.append(f"[admin:{m.type}] {m.text}") if m.type == "error" else None)
        admin.on("pageerror", lambda e: page_errors.append(f"[admin] {e}"))
        admin.on("response", lambda r: failed_requests.append(f"[admin] {r.status} {r.request.method} {r.url}")
                 if r.status >= 400 else None)

        admin.goto(f"{BASE}/auth/login", wait_until="networkidle")
        admin.click("form:has(input[value='admin']) button")
        admin.wait_for_url("**/app", timeout=20000)
        admin.goto(f"{BASE}/admin", wait_until="networkidle")
        admin.wait_for_selector("#config-list .cfg-row", timeout=20000)
        rows = admin.locator("#config-list .cfg-row").count()
        locked = admin.locator("#config-list .tag-warning").count()
        print(f"11) 管理台配置项 {rows} 行，其中环境变量覆盖（只读）{locked} 项")
        admin.screenshot(path=str(SHOTS / "09-admin.png"), full_page=True)

        admin.click("#run-selftest")
        admin.wait_for_selector("#selftest-result .log-line", timeout=90000)
        admin.wait_for_function(
            "() => Array.from(document.querySelectorAll('#selftest-result .lc')).some(e => e.textContent.includes('通过'))",
            timeout=90000,
        )
        print("   工具自检结果:", admin.locator("#selftest-result .log-line").first.inner_text()[:80])

        admin.click("#refresh-logs")
        admin.wait_for_selector("#log-list .log-line", timeout=20000)
        print("   运行日志条数:", admin.locator("#log-list .log-line").count())
        admin.screenshot(path=str(SHOTS / "10-admin-selftest.png"), full_page=True)

        browser.close()

    print()
    if failed_requests:
        print("失败请求：")
        for line in failed_requests[:20]:
            print("  -", line)
    if console_errors:
        print("浏览器控制台错误：")
        for line in console_errors[:20]:
            print("  -", line[:180])
    if page_errors:
        print("页面异常：")
        for line in page_errors[:20]:
            print("  -", line[:180])
    if console_errors or page_errors:
        print("存在前端错误 ✘")
        return 1

    print("浏览器端到端测试通过 ✔（截图见 data/_shots/）")
    return 0


if __name__ == "__main__":
    sys.exit(run())
