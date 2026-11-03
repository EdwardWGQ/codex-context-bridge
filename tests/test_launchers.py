import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent


class TerminalConfigurationTests(unittest.TestCase):
    def test_first_configuration_and_saved_configuration_without_input(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "config.json"
            command = [sys.executable, "-m", "bridge", "--config", str(destination), "--configure"]
            first = subprocess.run(command, cwd=ROOT, input=b"https://example.com/v1\n\n", capture_output=True, timeout=10)
            self.assertEqual(first.returncode, 0, first.stderr)
            saved = json.loads(destination.read_text())
            self.assertEqual(saved["upstream_url"], "https://example.com/v1")
            self.assertEqual(saved["port"], 8787)
            repeat = subprocess.run(command, cwd=ROOT, input=b"", capture_output=True, timeout=10)
            self.assertEqual(repeat.returncode, 0, repeat.stderr)
            self.assertEqual(json.loads(destination.read_text()), saved)

    def test_invalid_terminal_configuration_is_not_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "config.json"
            failed = subprocess.run([sys.executable, "-m", "bridge", "--config", str(destination), "--configure"],
                                    cwd=ROOT, input=b"not-a-url\n\n", capture_output=True, timeout=10)
            self.assertNotEqual(failed.returncode, 0)
            self.assertFalse(destination.exists())


@unittest.skipUnless(os.name == "nt", "Windows command launcher")
class WindowsLauncherTests(unittest.TestCase):
    def test_start_cmd_starts_resumes_and_never_opens_browser(self):
        with tempfile.TemporaryDirectory(prefix="bridge launch ") as directory:
            work = Path(directory)
            shutil.copytree(ROOT / "bridge", work / "bridge", ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copytree(ROOT / "scripts", work / "scripts")
            for entry in ["start.cmd", "stop.cmd", "settings.cmd"]:
                shutil.copyfile(ROOT / entry, work / entry)
            hook = work / "browser_check"
            hook.mkdir()
            (hook / "sitecustomize.py").write_text(
                "import webbrowser\nfrom pathlib import Path\n"
                "def forbidden(*a, **kw):\n"
                "    Path('browser_was_opened').touch()\n"
                "    raise RuntimeError('start must not open browser')\n"
                "webbrowser.open = forbidden\n", encoding="utf-8")
            environment = os.environ | {"PYTHONPATH": str(hook), "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")}
            probe = socket.socket()
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
            probe.close()
            (work / "config.json").write_text(json.dumps({"upstream_url": "https://example.com/v1", "port": port}), encoding="utf-8")

            def launch(name, text=b""):
                # Background processes can inherit launcher pipe handles on
                # Windows. Files verify launcher exit without waiting for EOF
                # from an intentionally long-lived child.
                with tempfile.TemporaryFile() as stdin, tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                    stdin.write(text)
                    stdin.seek(0)
                    result = subprocess.run(["cmd.exe", "/d", "/c", name], cwd=work, env=environment,
                                            stdin=stdin, stdout=stdout, stderr=stderr, timeout=35,
                                            creationflags=subprocess.CREATE_NO_WINDOW)
                    stdout.seek(0)
                    stderr.seek(0)
                    result.stdout, result.stderr = stdout.read(), stderr.read()
                    return result

            def status():
                result = subprocess.run([sys.executable, "-m", "bridge", "--status"], cwd=work,
                                        env=environment, capture_output=True, timeout=10,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)

            def health():
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                try:
                    connection.request("GET", "/health")
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.loads(response.read())["service"], "codex-context-bridge")
                finally:
                    connection.close()

            try:
                first = launch("start.cmd")
                self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
                original = status()
                self.assertTrue(original["running"])
                health()
                self.assertFalse((work / "browser_was_opened").exists())
                repeat = launch("start.cmd")
                self.assertEqual(repeat.returncode, 0, repeat.stdout + repeat.stderr)
                self.assertEqual(status()["pid"], original["pid"])
                record = json.loads((work / ".runtime/service.json").read_text())
                from urllib.parse import urlsplit
                connection = http.client.HTTPConnection("127.0.0.1", urlsplit(record["origin"]).port, timeout=3)
                connection.request("POST", "/api/stop", b"{}", {"X-Bridge-Token": record["token"], "Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
                connection.close()
                self.assertFalse(status()["running"])
                resumed = launch("start.cmd")
                self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
                self.assertTrue(status()["running"])
                self.assertEqual(status()["pid"], original["pid"])
                health()
                self.assertFalse((work / "browser_was_opened").exists())
            finally:
                subprocess.run([sys.executable, "-m", "bridge", "--stop"], cwd=work, env=environment,
                               capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
                for _ in range(100):
                    if not (work / ".runtime/service.json").exists():
                        break
                    time.sleep(.05)


if __name__ == "__main__":
    unittest.main()
