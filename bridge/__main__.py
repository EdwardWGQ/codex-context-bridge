"""Run with python -m bridge; local command channels never use process-name kills."""

import argparse
import http.client
import json
import os
from pathlib import Path
import signal
import sys
import threading
import webbrowser
from urllib.parse import urlsplit

from . import config
from .application import Application, SettingsServer

ROOT = Path(__file__).resolve().parent.parent


def instance_lock(path):
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            if not handle.read(1):
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except OSError:
        handle.close()
        return None


def contact(state_file, operation="state"):
    record = json.loads(state_file.read_text(encoding="utf-8"))
    target = urlsplit(record["origin"])
    if target.scheme != "http" or target.hostname != "127.0.0.1" or target.path or target.query or target.fragment:
        raise ValueError("本机服务记录无效。")
    connection = http.client.HTTPConnection("127.0.0.1", target.port, timeout=5)
    headers = {"X-Bridge-Token": record["token"]}
    try:
        connection.request("GET", "/api/state", headers=headers)
        response = connection.getresponse()
        state = json.loads(response.read(65536))
        if response.status != 200 or state.get("service") != "codex-context-bridge" or state.get("pid") != record["pid"]:
            raise ValueError("本机服务身份校验失败。")
        if operation == "shutdown":
            connection.request("POST", "/api/shutdown", body=b"{}", headers=headers | {"Content-Type": "application/json"})
            response = connection.getresponse()
            result = json.loads(response.read(65536))
            if response.status != 200:
                raise ValueError(result.get("error", "服务无法停止。"))
        elif operation == "open":
            if not webbrowser.open(record["origin"] + "/#" + record["token"]):
                raise ValueError("系统浏览器无法打开。")
        return state
    finally:
        connection.close()


def main(argv=None):
    # Windows redirected consoles may use a legacy code page. Keep Chinese CLI
    # output from aborting an otherwise healthy background service.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description="Codex Context Bridge：本机上下文兼容代理")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--state-dir", type=Path, default=ROOT / ".runtime")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--start-proxy", action="store_true")
    commands = parser.add_mutually_exclusive_group()
    commands.add_argument("--status", action="store_true")
    commands.add_argument("--stop", action="store_true")
    commands.add_argument("--open-running", action="store_true")
    args = parser.parse_args(argv)
    os.umask(0o077)
    args.state_dir.mkdir(parents=True, exist_ok=True)
    state_file = args.state_dir / "service.json"
    if args.status or args.stop or args.open_running:
        try:
            state = contact(state_file, "shutdown" if args.stop else "open" if args.open_running else "state")
            print("服务已停止。" if args.stop else json.dumps(state, ensure_ascii=False))
            return 0
        except (OSError, ValueError, KeyError, http.client.HTTPException) as failure:
            print("服务未运行或操作失败：" + str(failure), file=sys.stderr)
            return 1
    lock = instance_lock(args.state_dir / "service.lock")
    if lock is None:
        try:
            contact(state_file, "state" if args.no_browser else "open")
            print("服务已在运行。")
            return 0
        except (OSError, ValueError, KeyError, http.client.HTTPException):
            print("服务正在启动，请稍后重试。", file=sys.stderr)
            return 1
    application = server = None
    try:
        application = Application(args.config)
        server = SettingsServer(application)
        quit_event = threading.Event()
        application.on_shutdown = quit_event.set
        signal.signal(signal.SIGINT, lambda *_: quit_event.set())
        signal.signal(signal.SIGTERM, lambda *_: quit_event.set())
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        config.write(state_file, {"origin": server.origin, "token": server.token, "pid": os.getpid()})
        if args.start_proxy:
            application.start()
        if not args.no_browser:
            webbrowser.open(server.origin + "/#" + server.token)
        print("本机设置服务已启动。运行期间不要移动目录；Ctrl+C 可退出。", flush=True)
        quit_event.wait()
        return 0
    except (OSError, ValueError) as failure:
        print("启动失败：" + str(failure), file=sys.stderr)
        return 1
    finally:
        if application:
            application.stop(force=True)
        if server:
            server.shutdown()
            server.server_close()
        state_file.unlink(missing_ok=True)
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
