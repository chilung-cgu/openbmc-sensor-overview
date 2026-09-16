"""End-to-end workstation checks; no BMC or network access."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "sensor-overview.py"


class AcceptanceTests(unittest.TestCase):
    def run_cli(self, *args, env=None):
        return subprocess.run(
            [sys.executable, str(ENTRY), *args],
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
        )

    def test_ssh_transport_reaches_real_parser_without_network(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fake_ssh = root / "ssh"
            fake_ssh.write_text(
                f"#!{sys.executable}\n"
                "import json, os, sys\n"
                "with open(os.environ['ARGV_RECORD'], 'w') as f:\n"
                "    json.dump(sys.argv[1:], f)\n"
                "print(json.dumps({'SENTINEL_DOME_SLOT_1_CPU_TEMP': "
                "{'status': 'critical', 'value': 100}}))\n",
                encoding="utf-8",
            )
            fake_ssh.chmod(0o755)
            record = root / "argv.json"
            env = dict(os.environ, PATH=f"{folder}:{os.environ.get('PATH', '')}",
                       ARGV_RECORD=str(record))
            result = self.run_cli("--host", "root@192.0.2.1", "--once", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("critical", result.stdout)
            self.assertIn("SENTINEL_DOME_SLOT_1_CPU_TEMP", result.stdout)
            argv = json.loads(record.read_text())
            self.assertEqual(argv[-1], "mfg-tool sensor-display")
            self.assertIn("BatchMode=yes", argv)
            self.assertNotIn("StrictHostKeyChecking=no", argv)

    def test_malformed_file_fails_instead_of_reporting_empty_normal(self):
        with tempfile.TemporaryDirectory() as folder:
            snapshot = Path(folder) / "bad.json"
            for text in ("{}", "[]", "not JSON", '{"A": 12}'):
                with self.subTest(text=text):
                    snapshot.write_text(text)
                    result = self.run_cli("--file", str(snapshot), "--once")
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_thousand_sensor_snapshot_keeps_all_names(self):
        with tempfile.TemporaryDirectory() as folder:
            snapshot = Path(folder) / "large.json"
            readings = {
                f"SENTINEL_DOME_SLOT_1_TEST_{i:04d}": {"status": "ok", "value": 20}
                for i in range(1000)
            }
            readings["SENTINEL_DOME_SLOT_1_TEST_0999"]["status"] = "warning"
            snapshot.write_text(json.dumps(readings))
            result = self.run_cli("--file", str(snapshot), "--once")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("warning", result.stdout)
            for name in readings:
                self.assertIn(name, result.stdout)

    def test_noninteractive_use_explains_once(self):
        result = self.run_cli("--demo")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--once", result.stderr + result.stdout)


if __name__ == "__main__":
    unittest.main()
