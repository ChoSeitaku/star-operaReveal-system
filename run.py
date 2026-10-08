#!/usr/bin/env python3
"""单进程启动器：同时拉起「分析后端」与「认证中心」两个服务。

两个 uvicorn.Server 分别运行在守护线程中（uvicorn 在非主线程会自动跳过信号处理，
见 uvicorn/server.py capture_signals），主线程负责就绪探测与 Ctrl+C 优雅退出。

用法：
    python run.py                 # 默认 127.0.0.1:8000（后端） + 127.0.0.1:8001（认证中心）
    python run.py --host 0.0.0.0  # 对外暴露（注意 Windows 防火墙提示）
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import uvicorn  # noqa: E402

from backend import config  # noqa: E402


def _reconfigure_console() -> None:
    """Windows 控制台默认 GBK，重设为 UTF-8 以免中文日志乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def _bind_sockets(host: str, port: int) -> list[socket.socket]:
    """预先绑定监听套接字。

    浏览器把 `localhost` 解析为 IPv6 回环 `::1` 的优先级高于 IPv4 `127.0.0.1`
    （RFC 6724 / Happy Eyeballs），而 uvicorn 只会按 host 字符串是否含 ":" 绑定
    **单个**地址族。若只监听 127.0.0.1，用户访问 http://localhost:port 就会
    「拒绝连接」。这里对回环地址同时绑定两族，使两种写法都可用且仍不对外暴露。
    """
    targets: list[tuple[int, tuple]] = []

    if host in ("127.0.0.1", "localhost"):
        targets.append((socket.AF_INET, ("127.0.0.1", port)))
        targets.append((socket.AF_INET6, ("::1", port)))
    elif host == "::1":
        targets.append((socket.AF_INET6, ("::1", port)))
    elif host == "0.0.0.0":
        # 对外暴露：IPv4 全网 + IPv6 全网都监听，否则局域网内用 IPv6 地址访问会失败
        targets.append((socket.AF_INET, ("0.0.0.0", port)))
        targets.append((socket.AF_INET6, ("::", port)))
    else:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        targets.append((family, (host, port)))

    bound: list[socket.socket] = []
    for family, address in targets:
        try:
            sock = socket.socket(family, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                # 必须显式 V6ONLY：否则在部分系统上会与 IPv4 套接字抢同一端口
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            sock.bind(address)
            sock.set_inheritable(True)
            sock.listen(2048)
            bound.append(sock)
        except OSError as exc:
            if family == socket.AF_INET6:
                print(f"  · 跳过 IPv6 绑定 {address}（{exc.strerror or exc}）")
                continue
            for opened in bound:
                opened.close()
            raise

    return bound


def _ensure_sample_data() -> None:
    """首次运行时生成情景演示库，使 `python run.py` 一条命令即可跑起来。

    系统库（system.db）的建表与默认配置由后端启动时的 lifespan 完成；
    这里只负责 6 个业务情景库 —— 它们是纯只读的演示数据，生成过程确定性可复现。
    """
    from backend import config

    demo_dir = config.scenario_db_dir()
    if demo_dir.exists() and any(demo_dir.glob("*.db")):
        return

    print("  首次运行：正在生成示例数据（6 个情景库）…")
    from db import seed_scenarios

    seed_scenarios.build_all(verbose=False)
    print(f"  示例数据已就绪：{demo_dir}")


def _wait_health(url: str, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(0.3)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="启动经营归因分析系统")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--log-level", default="info", help="日志级别（默认 info）")
    args = parser.parse_args()

    _reconfigure_console()

    backend_port = config.backend_port()
    auth_port = config.auth_port()
    if backend_port == auth_port:
        print(f"[run] 端口冲突：后端与认证中心都配置为 {backend_port}", file=sys.stderr)
        return 1

    services = [
        {
            "name": "分析后端",
            "app": "backend.main:app",
            "port": backend_port,
            "health": f"http://127.0.0.1:{backend_port}/api/health",
            "url": f"http://localhost:{backend_port}",
        },
        {
            "name": "认证中心",
            "app": "auth_service.main:app",
            "port": auth_port,
            "health": f"http://127.0.0.1:{auth_port}/healthz",
            "url": f"http://localhost:{auth_port}",
        },
    ]

    print("=" * 68)
    print("  经营归因分析系统 — 启动中")
    print("=" * 68)

    try:
        _ensure_sample_data()
    except Exception as exc:
        print(f"  ✘ 示例数据生成失败：{exc}", file=sys.stderr)
        print("    可手动执行：python scripts/init_db.py", file=sys.stderr)
        return 1

    servers = []
    for svc in services:
        try:
            socks = _bind_sockets(args.host, svc["port"])
        except OSError as exc:
            print(
                f"  ✘ {svc['name']} 无法绑定 {args.host}:{svc['port']}：{exc.strerror or exc}\n"
                f"    端口可能已被占用，可用 BACKEND_PORT / AUTH_PORT 更换端口",
                file=sys.stderr,
            )
            for _svc, _server, _thread, _socks in servers:
                _server.should_exit = True
                for opened in _socks:
                    opened.close()
            return 1

        server = uvicorn.Server(
            uvicorn.Config(svc["app"], host=args.host, port=svc["port"], log_level=args.log_level)
        )
        # 传入预绑定套接字（IPv4 + IPv6 回环），使 localhost / 127.0.0.1 两种写法都可用
        thread = threading.Thread(
            target=server.run, kwargs={"sockets": socks}, name=svc["name"], daemon=True
        )
        thread.start()
        servers.append((svc, server, thread, socks))
        print(f"  · {svc['name']:<6s} 启动中 -> {svc['url']}")

    failed = False
    for svc, _server, _thread, _socks in servers:
        if _wait_health(svc["health"]):
            print(f"  ✔ {svc['name']:<6s} 就绪  {svc['url']}")
        else:
            failed = True
            print(f"  ✘ {svc['name']:<6s} 未在超时时间内就绪，请检查端口占用", file=sys.stderr)

    if failed:
        for _svc, server, _thread, _socks in servers:
            server.should_exit = True
        return 1

    print("-" * 68)
    print(f"  浏览器访问：{services[0]['url']}   （认证中心：{services[1]['url']}）")
    print("  演示账号：analyst（分析用户） / admin（系统管理员）")
    print("  按 Ctrl+C 停止服务")
    print("=" * 68)

    stop_event = threading.Event()
    try:
        while not stop_event.wait(0.5):
            if any(not thread.is_alive() for _svc, _server, thread, _socks in servers):
                print("[run] 检测到服务线程退出，正在关闭…", file=sys.stderr)
                break
    except KeyboardInterrupt:
        print("\n[run] 收到中断信号，正在关闭…")

    for _svc, server, _thread, _socks in servers:
        server.should_exit = True
    for _svc, _server, thread, _socks in servers:
        thread.join(timeout=10)
    for _svc, _server, _thread, socks in servers:
        for opened in socks:
            try:
                opened.close()
            except OSError:
                pass
    print("[run] 已全部停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
