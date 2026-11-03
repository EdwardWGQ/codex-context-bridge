"""HTTP forwarding using standard TLS/proxy support and incremental SSE reads."""

import json
from http.client import HTTPException
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .cleaning import clean_request

BODY_LIMIT = 32 * 1024 * 1024
HOP_FIELDS = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
                        "transfer-encoding", "te", "trailer", "upgrade", "host",
                        "content-length", "proxy-connection"})


def excluded_headers(headers):
    excluded = set(HOP_FIELDS)
    for field, value in headers:
        if field.lower() == "connection":
            excluded.update(part.strip().lower() for part in value.split(","))
    return excluded


def read_json(handler, limit=BODY_LIMIT):
    if handler.headers.get("Transfer-Encoding") or handler.headers.get("Content-Encoding", "identity") != "identity":
        raise ValueError("仅接受带 Content-Length 的未压缩 JSON。")
    sizes = handler.headers.get_all("Content-Length", [])
    if len(sizes) != 1:
        raise ValueError("必须提供唯一的 Content-Length。")
    size = int(sizes[0])
    if not 0 < size <= limit:
        raise ValueError("请求体为空或超过大小限制。")
    raw = handler.rfile.read(size)
    if len(raw) != size:
        raise ValueError("请求体不完整。")
    return json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("JSON 数值无效。")))


def send_json(handler, status, data):
    body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.wfile.write(body)
    handler.close_connection = True


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # Binding a loopback address does not require reverse DNS.
        from socketserver import TCPServer
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address


class BridgeServer(LocalServer):
    def __init__(self, settings):
        super().__init__(("127.0.0.1", settings["port"]), Forwarder)
        self.settings = settings.copy()
        self.guard = threading.Lock()
        self.counters = {"active": 0, "requests": 0, "reasoning_removed": 0, "ids_removed": 0}
        self.accepting = True

    def snapshot(self):
        with self.guard:
            return self.counters.copy()


class Forwarder(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(self.server.settings["timeout_seconds"])

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/health":
            send_json(self, 200, {"service": "codex-context-bridge", "status": "ok"})
        else:
            send_json(self, 405, {"error": {"message": "使用 HTTP POST Responses；不支持 WebSocket。"}})

    def do_POST(self):
        with self.server.guard:
            admitted = self.server.accepting
            if admitted:
                self.server.counters["active"] += 1
                self.server.counters["requests"] += 1
        if not admitted:
            send_json(self, 503, {"error": {"message": "代理正在停止，请稍后重试。"}})
            return
        try:
            self.forward()
        finally:
            self.close_connection = True
            with self.server.guard:
                self.server.counters["active"] -= 1

    def forward(self):
        sent_headers = False
        response = None
        try:
            if self.headers.get("Origin"):
                send_json(self, 403, {"error": {"message": "不接受网页跨域调用。"}})
                return
            routes = {"/responses": "/responses", "/v1/responses": "/responses",
                      "/responses/compact": "/responses/compact", "/v1/responses/compact": "/responses/compact"}
            if self.path not in routes:
                send_json(self, 404, {"error": {"message": "此 API 路径不受支持。"}})
                return
            settings = self.server.settings
            outgoing, report = clean_request(read_json(self), allow_response_link_removal=settings["allow_response_link_removal"])
            with self.server.guard:
                self.server.counters["reasoning_removed"] += report.reasoning_items
                self.server.counters["ids_removed"] += report.item_ids
            omit = excluded_headers(self.headers.items()) | {"cookie", "content-type", "accept-encoding", "expect"}
            headers = {field: value for field, value in self.headers.items() if field.lower() not in omit}
            headers.update({"Content-Type": "application/json", "Accept-Encoding": "identity"})
            request = Request(settings["upstream_url"] + routes[self.path],
                              data=json.dumps(outgoing, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                              headers=headers, method="POST")
            # urllib uses system/environment proxies and validates TLS certificates.
            # Never follow redirects carrying client authentication to another host.
            try:
                response = build_opener(NoRedirect()).open(request, timeout=settings["timeout_seconds"])
            except HTTPError as upstream_error:
                response = upstream_error
            self.send_response(response.code)
            omit = excluded_headers(response.headers.items())
            for field, value in response.headers.items():
                if field.lower() not in omit:
                    self.send_header(field, value)
            self.send_header("Connection", "close")
            self.end_headers()
            sent_headers = True
            while True:
                packet = response.read1(16 * 1024)
                if not packet:
                    break
                self.wfile.write(packet)
                self.wfile.flush()
        except (ValueError, UnicodeError):
            if not sent_headers:
                # Parse errors can contain request fragments; only report safe validation messages.
                send_json(self, 400, {"error": {"message": "请求或历史无效。远端引用、压缩历史、非完整历史链接无法安全清洗。"}})
        except (OSError, URLError, socket.timeout, HTTPException):
            if not sent_headers:
                send_json(self, 502, {"error": {"message": "上游连接失败或超时，请检查服务地址与网络。请求未自动重试。"}})
        finally:
            if response is not None:
                response.close()
