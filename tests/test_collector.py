import contextlib
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from sensor_overview.collector import (
    CollectionError,
    Collector,
    ssh_command,
)


@contextlib.contextmanager
def fake_mfg_tool(script_body: str):
    """Places a temporary executable named mfg-tool in PATH."""
    with tempfile.TemporaryDirectory() as td:
        tool_path = os.path.join(td, "mfg-tool")
        with open(tool_path, "w", encoding="utf-8") as f:
            f.write(f"#!/usr/bin/env python3\n{script_body}\n")
        os.chmod(tool_path, 0o755)
        orig_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{td}:{orig_path}"
        try:
            yield tool_path
        finally:
            os.environ["PATH"] = orig_path


class TestSshCommand(unittest.TestCase):
    def test_ssh_is_readonly_and_noninteractive(self):
        args = ssh_command("root@192.0.2.1")
        self.assertIn("-T", args)
        self.assertIn("BatchMode=yes", args)
        self.assertEqual(args[-1], "mfg-tool sensor-display")
        self.assertNotIn("StrictHostKeyChecking=no", args)

    def test_ssh_args_port_and_identity(self):
        args = ssh_command("root@192.0.2.1", port=2222, identity="/path/to/key")
        self.assertIn("-p", args)
        self.assertEqual(args[args.index("-p") + 1], "2222")
        self.assertIn("-i", args)
        self.assertEqual(args[args.index("-i") + 1], "/path/to/key")
        self.assertEqual(args[-1], "mfg-tool sensor-display")

    def test_valid_hosts_supported(self):
        valid_hosts = [
            "192.0.2.1",
            "root@192.0.2.1",
            "bmc-slot1",
            "my.bmc.local",
            "2001:db8::1",
            "[2001:db8::1]",
            "root@[2001:db8::1]",
            "root@2001:db8::1",
            "admin_1@host-01.corp",
        ]
        for host in valid_hosts:
            with self.subTest(host=host):
                args = ssh_command(host)
                self.assertEqual(args[-1], "mfg-tool sensor-display")
                self.assertIn(host, args)

    def test_injection_host_rejected(self):
        injections = [
            "root@host;touch /tmp/oops",
            "-oProxyCommand=touch /tmp/pwn",
            "-host",
            "root@-host",
            "-root@host",
            "root@host && ls",
            "root@host|cat",
            "root@host$(id)",
            "root@host`id`",
            "root@host>file",
            "root@host<file",
            "",
            "   ",
            "host with spaces",
            "root@host\ntouch /tmp/oops",
            "root@host:22",
            "host[bracket",
            'root@host"bad',
            "root@host'bad",
        ]
        for bad_host in injections:
            with self.subTest(bad_host=bad_host):
                with self.assertRaises(ValueError):
                    ssh_command(bad_host)

    def test_invalid_ports(self):
        for bad_port in [0, -1, 65536, 70000]:
            with self.subTest(bad_port=bad_port):
                with self.assertRaises(ValueError):
                    ssh_command("root@192.0.2.1", port=bad_port)

    def test_invalid_identity(self):
        for bad_id in ["", "   ", "-oProxyCommand=bad", "-i"]:
            with self.subTest(bad_id=bad_id):
                with self.assertRaises(ValueError):
                    ssh_command("root@192.0.2.1", identity=bad_id)


class TestCollectorConstructor(unittest.TestCase):
    def test_requires_exactly_one_source(self):
        with self.assertRaises(ValueError):
            Collector()

        with self.assertRaises(ValueError):
            Collector(host="root@192.0.2.1", demo=True)

        with self.assertRaises(ValueError):
            Collector(file="test.json", demo=True)

        with self.assertRaises(ValueError):
            Collector(local=True, host="root@192.0.2.1")

        with self.assertRaises(ValueError):
            Collector(local=True, file="test.json")

    def test_timeout_must_be_positive_finite(self):
        for bad_timeout in [0, -1, -0.5, float("nan"), float("inf"), float("-inf"), True, False, "10"]:
            with self.subTest(bad_timeout=bad_timeout):
                with self.assertRaises(ValueError):
                    Collector(demo=True, timeout=bad_timeout)

        c1 = Collector(demo=True, timeout=0.1)
        self.assertEqual(c1.timeout, 0.1)
        c2 = Collector(demo=True, timeout=15)
        self.assertEqual(c2.timeout, 15.0)

    def test_port_validation_in_collector(self):
        with self.assertRaises(ValueError):
            Collector(host="root@192.0.2.1", port=0)
        with self.assertRaises(ValueError):
            Collector(host="root@192.0.2.1", port=65536)


class TestDemoCollector(unittest.TestCase):
    def test_demo_five_stage_cycle_and_anchor(self):
        collector = Collector(demo=True)
        stop = threading.Event()

        rounds = []
        for _ in range(5):
            raw = collector.collect(stop)
            data = json.loads(raw)
            self.assertIsInstance(data, dict)
            self.assertIn("SENTINEL_DOME_SLOT_1_FAN1", data)
            rounds.append(data)

        # Round 1: normal
        self.assertEqual(rounds[0]["SENTINEL_DOME_SLOT_1_CPU_TEMP"]["status"], "ok")
        # Round 2: critical
        self.assertEqual(rounds[1]["SENTINEL_DOME_SLOT_1_CPU_TEMP"]["status"], "critical")
        # Round 3: unavailable
        self.assertEqual(rounds[2]["SENTINEL_DOME_SLOT_1_CPU_TEMP"]["status"], "unavailable")
        # Round 4: missing
        self.assertNotIn("SENTINEL_DOME_SLOT_1_CPU_TEMP", rounds[3])
        # Anchor is still present in round 4
        self.assertIn("SENTINEL_DOME_SLOT_1_FAN1", rounds[3])
        # Round 5: recovered
        self.assertEqual(rounds[4]["SENTINEL_DOME_SLOT_1_CPU_TEMP"]["status"], "ok")

        # Round 6 cycles back to round 1
        round6 = json.loads(collector.collect(stop))
        self.assertEqual(round6["SENTINEL_DOME_SLOT_1_CPU_TEMP"]["status"], "ok")

    def test_demo_cancelled_by_stop_event(self):
        collector = Collector(demo=True)
        stop = threading.Event()
        stop.set()
        with self.assertRaises(CollectionError):
            collector.collect(stop)


class TestFileCollector(unittest.TestCase):
    def test_file_reading_and_reloading(self):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as tf:
            tf.write('{"S1": {"status": "ok", "value": 1}}')
            temp_path = tf.name

        try:
            collector = Collector(file=temp_path)
            stop = threading.Event()

            # First read
            out1 = collector.collect(stop)
            self.assertIn('"S1"', out1)

            # Modify file
            with open(temp_path, "w", encoding="utf-8") as tf:
                tf.write('{"S2": {"status": "critical", "value": 99}}')

            # Second read reflects modification
            out2 = collector.collect(stop)
            self.assertIn('"S2"', out2)
            self.assertNotIn('"S1"', out2)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def test_file_missing_raises_collection_error(self):
        collector = Collector(file="/tmp/non_existent_file_sensor_overview_12345.json")
        stop = threading.Event()
        with self.assertRaises(CollectionError):
            collector.collect(stop)

    def test_file_cancelled_by_stop_event(self):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as tf:
            tf.write('{"S1": 1}')
            temp_path = tf.name

        try:
            collector = Collector(file=temp_path)
            stop = threading.Event()
            stop.set()
            with self.assertRaises(CollectionError):
                collector.collect(stop)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)


class TestSubprocessCollector(unittest.TestCase):
    def test_successful_command_execution(self):
        script = 'import json\nprint(json.dumps({"A": {"status": "ok", "value": 10}}))'
        with fake_mfg_tool(script):
            collector = Collector(local=True)
            stop = threading.Event()
            raw = collector.collect(stop)
            data = json.loads(raw)
            self.assertEqual(data["A"]["status"], "ok")

    def test_nonzero_exit_raises_collection_error_with_stderr_tail(self):
        long_err = "X" * 3000 + "important_tail_message"
        script = f'import sys\nsys.stderr.write({repr(long_err)})\nsys.exit(42)'
        with fake_mfg_tool(script):
            collector = Collector(local=True)
            stop = threading.Event()
            with self.assertRaises(CollectionError) as cm:
                collector.collect(stop)

            msg = str(cm.exception)
            self.assertIn("42", msg)
            self.assertIn("important_tail_message", msg)
            self.assertNotIn("X" * 2500, msg)

    def test_missing_executable_raises_collection_error(self):
        with tempfile.TemporaryDirectory() as empty_dir:
            orig_path = os.environ.get("PATH", "")
            os.environ["PATH"] = empty_dir
            try:
                collector = Collector(local=True)
                stop = threading.Event()
                with self.assertRaises(CollectionError) as cm:
                    collector.collect(stop)
                self.assertIn("not found", str(cm.exception).lower())
            finally:
                os.environ["PATH"] = orig_path

    def test_timeout_kills_process_group_no_zombies(self):
        script = 'import subprocess, sys, time\np = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])\np.wait()'
        with fake_mfg_tool(script):
            collector = Collector(local=True, timeout=0.2)
            stop = threading.Event()
            start = time.monotonic()
            with self.assertRaises(CollectionError) as cm:
                collector.collect(stop)
            elapsed = time.monotonic() - start

            self.assertLess(elapsed, 2.0, "Timeout should exit promptly")
            self.assertIn("timed out", str(cm.exception).lower())

    def test_stop_event_cancels_running_subprocess_promptly(self):
        script = 'import time\ntime.sleep(60)'
        with fake_mfg_tool(script):
            collector = Collector(local=True, timeout=10.0)
            stop = threading.Event()

            timer = threading.Timer(0.05, stop.set)
            timer.start()

            start = time.monotonic()
            with self.assertRaises(CollectionError) as cm:
                collector.collect(stop)
            elapsed = time.monotonic() - start
            timer.cancel()

            self.assertLess(elapsed, 2.0, "Stop should abort promptly")
            self.assertIn("cancel", str(cm.exception).lower())

    def test_kill_process_group_directly_escalates_and_reaps_child_ignoring_sigterm(self):
        from sensor_overview.collector import _kill_process_group

        child_code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print(12345, flush=True); time.sleep(60)"
        proc = subprocess.Popen(
            [sys.executable, "-u", "-c", child_code],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        line = proc.stdout.readline()
        self.assertEqual(line.strip(), b"12345")

        _kill_process_group(proc)
        self.assertIn(proc.returncode, (-signal.SIGKILL, -9))
        self.assertTrue(proc.stdout.closed)
        self.assertTrue(proc.stderr.closed)

    def test_sigkill_escalation_when_sigterm_ignored(self):
        # Process ignores SIGTERM; collector must escalate to SIGKILL and reap
        script = 'import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)'
        with fake_mfg_tool(script):
            collector = Collector(local=True, timeout=0.2)
            stop = threading.Event()
            start = time.monotonic()
            with self.assertRaises(CollectionError) as cm:
                collector.collect(stop)
            elapsed = time.monotonic() - start

            self.assertLess(elapsed, 2.5, "SIGKILL escalation should terminate and reap promptly")
            self.assertIn("timed out", str(cm.exception).lower())

    def test_decode_failure_kills_and_reaps_process(self):
        # Subprocess writes invalid UTF-8 bytes to stdout; reaches EOF and triggers decode error
        script = 'import sys\nsys.stdout.buffer.write(bytes([255, 254]))\nsys.stdout.buffer.flush()\nsys.exit(0)'
        with fake_mfg_tool(script):
            collector = Collector(local=True, timeout=5.0)
            stop = threading.Event()
            with self.assertRaises(CollectionError) as cm:
                collector.collect(stop)
            self.assertIn("decode", str(cm.exception).lower())


if __name__ == "__main__":
    unittest.main()
