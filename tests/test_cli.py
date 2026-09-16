import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SENSOR_SCRIPT = REPO_ROOT / "sensor-overview.py"
PYTHON_BIN = sys.executable

class TestCLI(unittest.TestCase):
    def run_cli(self, args: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
        full_env = os.environ.copy()
        if env:
            full_env.update(env)
        cmd = [PYTHON_BIN, str(SENSOR_SCRIPT)] + args
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env=full_env,
        )

    def test_once_demo_success(self):
        run = self.run_cli(["--demo", "--once"])
        self.assertEqual(run.returncode, 0, f"stderr: {run.stderr}\nstdout: {run.stdout}")
        self.assertIn("DEMO", run.stdout)
        self.assertIn("normal", run.stdout)

    def test_missing_source_exits_2(self):
        run = self.run_cli(["--once"])
        self.assertEqual(run.returncode, 2)
        self.assertIn("error", run.stderr.lower())

    def test_mutually_exclusive_sources_exits_2(self):
        run = self.run_cli(["--demo", "--local", "--once"])
        self.assertEqual(run.returncode, 2)
        run2 = self.run_cli(["--demo", "--file", "dummy.json", "--once"])
        self.assertEqual(run2.returncode, 2)

    def test_invalid_interval_exits_2(self):
        for bad in ["0", "-1", "NaN", "inf", "-inf", "abc"]:
            with self.subTest(bad=bad):
                run = self.run_cli(["--demo", "--once", "--interval", bad])
                self.assertEqual(run.returncode, 2)

    def test_invalid_timeout_exits_2(self):
        for bad in ["0", "-5", "NaN", "inf"]:
            with self.subTest(bad=bad):
                run = self.run_cli(["--demo", "--once", "--timeout", bad])
                self.assertEqual(run.returncode, 2)

    def test_invalid_stale_after_exits_2(self):
        for bad in ["0", "-2", "NaN", "inf"]:
            with self.subTest(bad=bad):
                run = self.run_cli(["--demo", "--once", "--stale-after", bad])
                self.assertEqual(run.returncode, 2)

    def test_invalid_port_exits_2(self):
        for bad in ["0", "65536", "-1", "abc"]:
            with self.subTest(bad=bad):
                run = self.run_cli(["--host", "root@192.0.2.1", "--once", "--port", bad])
                self.assertEqual(run.returncode, 2)

    def test_collector_constructor_value_error_exits_2(self):
        # Invalid host syntax (e.g. starts with '-') rejected by Collector constructor
        run = self.run_cli(["--host", "-invalidhost", "--once"])
        self.assertEqual(run.returncode, 2)
        self.assertIn("error", run.stderr.lower())

    def test_failed_file_exits_1(self):
        run = self.run_cli(["--file", "/tmp/nonexistent_file_xyz_12345.json", "--once"])
        self.assertEqual(run.returncode, 1)

    def test_once_empty_file_exits_1(self):
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json") as tf:
            tf.write("")
            tf_path = tf.name
        try:
            run = self.run_cli(["--file", tf_path, "--once"])
            self.assertEqual(run.returncode, 1)
            self.assertIn("error", (run.stderr + run.stdout).lower())
        finally:
            if os.path.exists(tf_path):
                os.remove(tf_path)

    def test_once_sanitizes_control_injection(self):
        payload = {
            "\x1b[31mBAD_SENSOR\x1b[0m\r\n": {
                "status": "ok\x1b[0m",
                "value": 12.5
            }
        }
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json") as tf:
            json.dump(payload, tf)
            tf_path = tf.name
        try:
            run = self.run_cli(["--file", tf_path, "--once"])
            self.assertEqual(run.returncode, 0)
            self.assertNotIn("\x1b[", run.stdout)
            self.assertNotIn("\x1b[31m", run.stdout)
            self.assertIn("BAD_SENSOR", run.stdout)
        finally:
            if os.path.exists(tf_path):
                os.remove(tf_path)

    def test_once_does_not_import_curses(self):
        code = (
            "import sys\n"
            "sys.path.insert(0, '')\n"
            "import sensor_overview.cli as cli\n"
            "try:\n"
            "    cli.main(['--demo', '--once'])\n"
            "except SystemExit as e:\n"
            "    sys.exit(0 if ('curses' not in sys.modules and e.code == 0) else 10)\n"
        )
        run = subprocess.run(
            [PYTHON_BIN, "-c", code],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(run.returncode, 0, f"stderr: {run.stderr}\nstdout: {run.stdout}")

    def test_non_tty_prompts_once(self):
        run = self.run_cli(["--demo"])
        self.assertNotEqual(run.returncode, 0)
        combined = (run.stdout + run.stderr).lower()
        self.assertIn("--once", combined)

if __name__ == "__main__":
    unittest.main()
