import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent


class ProcessTests(unittest.TestCase):
    def test_headless_start_duplicate_instance_and_authenticated_shutdown(self):
        flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        environment = os.environ | {"PYTHONIOENCODING": "ascii"}
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            command = [sys.executable, "-m", "bridge", "--state-dir", str(state_dir)]
            process = subprocess.Popen(command + ["--config", str(Path(directory) / "config.json"), "--no-browser"],
                                       cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment, **flags)
            try:
                for _ in range(100):
                    if (state_dir / "service.json").exists():
                        break
                    if process.poll() is not None:
                        self.fail(process.communicate()[1].decode(errors="replace"))
                    time.sleep(.05)
                self.assertTrue((state_dir / "service.json").exists())
                status = subprocess.run(command + ["--status"], cwd=ROOT, capture_output=True, timeout=10, env=environment, **flags)
                self.assertEqual(status.returncode, 0, status.stderr)
                self.assertFalse(json.loads(status.stdout)["running"])
                duplicate = subprocess.run(command + ["--no-browser"], cwd=ROOT, capture_output=True, timeout=10, **flags)
                self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
                stopped = subprocess.run(command + ["--stop"], cwd=ROOT, capture_output=True, timeout=10, **flags)
                self.assertEqual(stopped.returncode, 0, stopped.stderr)
                process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0)
                self.assertFalse((state_dir / "service.json").exists())
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()
