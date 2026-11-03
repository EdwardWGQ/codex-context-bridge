"""Single-process settings service and independently controlled proxy listener."""

import json
import os
from pathlib import Path
import secrets
import threading
from http.server import BaseHTTPRequestHandler

from . import __version__
from . import config
from .proxy import BridgeServer, LocalServer, read_json, send_json

ASSETS = Path(__file__).parent / "web"


class Application:
    def __init__(self, config_path):
        self.config_path = Path(config_path)
        self.settings = config.read(config_path)
        self.lock = threading.RLock()
        self.proxy = None
        self.worker = None
        self.last_counts = {"active": 0, "requests": 0, "reasoning_removed": 0, "ids_removed": 0}
        self.on_shutdown = lambda: None

    def state(self):
        with self.lock:
            return {"version": __version__, "config": self.settings.copy(),
                    "running": self.proxy is not None,
                    "client_url": f"http://127.0.0.1:{self.settings['port']}/v1",
                    "counts": self.proxy.snapshot() if self.proxy else self.last_counts.copy()}

    def start(self):
        with self.lock:
            if self.proxy:
                return self.state()
            settings = config.validate(self.settings, require_upstream=True)
            try:
                proxy = BridgeServer(settings)
            except OSError:
                raise ValueError("本机端口无法监听，请在设置中更换端口。") from None
            worker = threading.Thread(target=proxy.serve_forever, daemon=True)
            worker.start()
            self.proxy, self.worker = proxy, worker
            return self.state()

    def stop(self, *, force=False):
        with self.lock:
            if self.proxy:
                with self.proxy.guard:
                    if self.proxy.counters["active"] and not force:
                        raise ValueError("当前还有请求正在处理，请等回复结束后再操作。")
                    self.proxy.accepting = False
                self.proxy.shutdown()
                self.proxy.server_close()
                self.worker.join(timeout=3)
                self.last_counts = self.proxy.snapshot()
                self.proxy = self.worker = None
            return self.state()

    def save(self, values):
        updated = config.validate(values)
        with self.lock:
            before = self.settings.copy()
            was_running = self.proxy is not None
            if updated == before:
                return self.state()
            if was_running:
                config.validate(updated, require_upstream=True)
                self.stop()
            try:
                config.write(self.config_path, updated)
                self.settings = updated
                if was_running:
                    self.start()
            except (OSError, ValueError) as failure:
                self.settings = before
                recovered = True
                try:
                    config.write(self.config_path, before)
                    if was_running:
                        self.start()
                except (OSError, ValueError):
                    recovered = False
                message = "新配置应用失败，已恢复原配置。" if recovered else "新配置应用失败，原服务也未能恢复，请检查端口和文件权限。"
                raise ValueError(message) from failure
            return self.state()


class SettingsServer(LocalServer):
    def __init__(self, application):
        super().__init__(("127.0.0.1", 0), SettingsHandler)
        self.application = application
        self.token = secrets.token_urlsafe(32)
        self.origin = f"http://127.0.0.1:{self.server_port}"


class SettingsHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *args):
        pass

    def end_headers(self):
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def permitted(self, *, api=False):
        if self.headers.get_all("Host") != [f"127.0.0.1:{self.server.server_port}"]:
            send_json(self, 403, {"error": "设置页仅允许本机访问。"})
            return False
        if self.headers.get("Origin") not in {None, self.server.origin}:
            send_json(self, 403, {"error": "请求来源无效。"})
            return False
        if api and not secrets.compare_digest(self.headers.get("X-Bridge-Token", "").encode(), self.server.token.encode()):
            send_json(self, 403, {"error": "设置会话无效，请重新打开设置页。"})
            return False
        return True

    def do_GET(self):
        api = self.path.startswith("/api/")
        if not self.permitted(api=api):
            return
        if self.path == "/api/state":
            send_json(self, 200, self.server.application.state() | {"service": "codex-context-bridge", "pid": os.getpid()})
            return
        files = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css")}
        asset = files.get(self.path)
        if not asset:
            send_json(self, 404, {"error": "页面不存在。"})
            return
        data = (ASSETS / asset[0]).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", asset[1] + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if not self.permitted(api=True):
            return
        try:
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ValueError("操作请求必须是 JSON。")
            values = read_json(self, limit=16 * 1024)
            app = self.server.application
            if self.path == "/api/config":
                state = app.save(values)
            elif self.path in {"/api/start", "/api/stop", "/api/shutdown"} and values == {}:
                state = app.start() if self.path == "/api/start" else app.stop()
            else:
                send_json(self, 404, {"error": "操作不存在。"})
                return
            send_json(self, 200, state)
            if self.path == "/api/shutdown":
                app.on_shutdown()
        except json.JSONDecodeError:
            send_json(self, 400, {"error": "JSON 格式无效。"})
        except (ValueError, OSError, UnicodeError) as failure:
            message = str(failure) if isinstance(failure, ValueError) else "无法保存设置，请检查文件权限。"
            send_json(self, 400, {"error": message})
