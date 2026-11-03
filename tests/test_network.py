import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest

from bridge.application import Application, SettingsServer
from bridge.config import DEFAULTS
from bridge.proxy import BridgeServer


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.server.received = {"path": self.path, "headers": dict(self.headers),
                                "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))}
        mode = self.server.mode
        if mode == "sse":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"data: first\n\n")
            self.wfile.flush()
            self.server.first_sent.set()
            self.server.release.wait(5)
            self.wfile.write(b"data: last\n\n")
        elif mode == "redirect":
            self.send_response(307)
            self.send_header("Location", "http://127.0.0.1:1/do-not-follow")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            data = b'{"error":{"message":"invalid key"}}' if mode == "error" else b'{"output_text":"ok"}'
            self.send_response(401 if mode == "error" else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.upstream.daemon_threads = True
        self.upstream.mode = "json"
        self.upstream.received = None
        self.upstream.first_sent = threading.Event()
        self.upstream.release = threading.Event()
        self.upstream_thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.upstream_thread.start()
        self.bridge = BridgeServer(DEFAULTS | {"port": 0, "upstream_url": f"http://127.0.0.1:{self.upstream.server_port}/v1"})
        self.bridge_thread = threading.Thread(target=self.bridge.serve_forever, daemon=True)
        self.bridge_thread.start()

    def tearDown(self):
        self.upstream.release.set()
        for server, worker in [(self.bridge, self.bridge_thread), (self.upstream, self.upstream_thread)]:
            server.shutdown()
            server.server_close()
            worker.join(3)

    def post(self, payload, *, route="/v1/responses", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.bridge.server_port, timeout=3)
        connection.request("POST", route, json.dumps(payload).encode(), headers=headers or {"Content-Type": "application/json"})
        return connection, connection.getresponse()

    def test_json_header_forwarding_and_compact_route(self):
        connection, response = self.post({"input": [{"type": "reasoning"}, {"role": "user", "id": "msg_1", "content": "hello"}]},
                                         route="/v1/responses/compact", headers={"Authorization": "Bearer test-only", "Cookie": "ignored", "Connection": "X-Private", "X-Private": "ignored", "Content-Type": "application/json"})
        try:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["output_text"], "ok")
            received = self.upstream.received
            self.assertEqual(received["path"], "/v1/responses/compact")
            fields = {key.lower(): value for key, value in received["headers"].items()}
            self.assertEqual(fields["authorization"], "Bearer test-only")
            self.assertNotIn("cookie", fields)
            self.assertNotIn("x-private", fields)
            self.assertEqual(received["body"]["input"], [{"role": "user", "content": "hello"}])
        finally:
            connection.close()

    def test_sse_first_event_arrives_before_upstream_finishes(self):
        self.upstream.mode = "sse"
        connection, response = self.post({"input": "hello", "stream": True})
        try:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.readline(), b"data: first\n")
            self.assertFalse(self.upstream.release.is_set())
            self.upstream.release.set()
            self.assertIn(b"data: last", response.read())
        finally:
            connection.close()

    def test_upstream_error_status_and_body_preserved(self):
        self.upstream.mode = "error"
        connection, response = self.post({"input": "hello"})
        try:
            self.assertEqual(response.status, 401)
            self.assertEqual(json.loads(response.read())["error"]["message"], "invalid key")
        finally:
            connection.close()

    def test_redirect_is_returned_without_following(self):
        self.upstream.mode = "redirect"
        connection, response = self.post({"input": "hello"})
        try:
            self.assertEqual(response.status, 307)
            response.read()
        finally:
            connection.close()

    def test_remote_history_never_reaches_upstream(self):
        connection, response = self.post({"input": "latest only", "previous_response_id": "resp_1"})
        try:
            self.assertEqual(response.status, 400)
            response.read()
            self.assertIsNone(self.upstream.received)
        finally:
            connection.close()

    def test_cross_origin_requests_never_reach_upstream(self):
        connection, response = self.post({"input": "hello"}, headers={"Origin": "https://example.com", "Content-Type": "application/json"})
        try:
            self.assertEqual(response.status, 403)
            response.read()
            self.assertIsNone(self.upstream.received)
        finally:
            connection.close()


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = Application(Path(self.directory.name) / "config.json")
        self.server = SettingsServer(self.app)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def tearDown(self):
        self.app.stop(force=True)
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(3)
        self.directory.cleanup()

    def call(self, path, data=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        supplied = {"X-Bridge-Token": self.server.token, "Content-Type": "application/json"} | (headers or {})
        connection.request("GET" if data is None else "POST", path, None if data is None else json.dumps(data), supplied)
        response = connection.getresponse()
        status, content = response.status, response.read()
        connection.close()
        return status, content

    def test_settings_token_origin_and_host_are_checked(self):
        for headers in [{"X-Bridge-Token": "incorrect"}, {"Origin": "https://example.com"}, {"Host": "example.com"}]:
            with self.subTest(headers=headers):
                status, _ = self.call("/api/state", headers=headers)
                self.assertEqual(status, 403)
        self.assertEqual(self.call("/api/state")[0], 200)

    def test_assets_and_save_without_credentials(self):
        self.assertEqual(self.call("/")[0], 200)
        status, _ = self.call("/api/config", DEFAULTS | {"upstream_url": "https://example.com/v1", "port": 9812})
        self.assertEqual(status, 200)
        saved = json.loads(self.app.config_path.read_text())
        self.assertEqual(saved["port"], 9812)
        self.assertNotIn("api_key", saved)

    def test_active_request_blocks_stop_and_configuration_changes(self):
        from unittest.mock import patch
        with patch("bridge.application.config.validate", side_effect=lambda values, **_: values):
            self.app.settings = DEFAULTS | {"port": 0, "upstream_url": "https://example.com/v1"}
            self.app.start()
        with self.app.proxy.guard:
            self.app.proxy.counters["active"] = 1
        with self.assertRaises(ValueError):
            self.app.stop()
        with self.assertRaises(ValueError):
            self.app.save(DEFAULTS | {"port": 9813, "upstream_url": "https://example.com/v1"})
        with self.app.proxy.guard:
            self.app.proxy.counters["active"] = 0
        self.app.stop()
        self.assertFalse(self.app.state()["running"])

    def test_failed_restart_restores_config(self):
        import socket
        occupied = socket.socket()
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        spare = socket.socket()
        spare.bind(("127.0.0.1", 0))
        original_port = spare.getsockname()[1]
        spare.close()
        original = DEFAULTS | {"upstream_url": "https://example.com/v1", "port": original_port}
        self.app.save(original)
        self.app.start()
        try:
            with self.assertRaises(ValueError):
                self.app.save(original | {"port": occupied.getsockname()[1]})
            self.assertEqual(self.app.settings, original)
            self.assertTrue(self.app.state()["running"])
            self.assertEqual(json.loads(self.app.config_path.read_text()), original)
        finally:
            occupied.close()


if __name__ == "__main__":
    unittest.main()
