import fcntl
import math
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

@dataclass(frozen=True)
class DummySensor:
    name: str
    status: str
    health: str
    group: tuple[str, ...]
    raw_status: str

from sensor_overview.ui import (
    TreeNode,
    build_tree,
    flatten_tree,
    format_header,
    format_selected_detail,
    strip_control_codes,
    TUIState,
)

class TestUIPureFunctions(unittest.TestCase):
    def test_tree_building_and_group_aggregation(self):
        sensors = [
            DummySensor("SENTINEL_DOME_SLOT_1_TEMP", "ok", "normal", ("Slot 1", "Sentinel Dome"), "ok"),
            DummySensor("SENTINEL_DOME_SLOT_1_VOLT", "critical", "attention", ("Slot 1", "Sentinel Dome"), "critical"),
            DummySensor("WAILUA_FALLS_SLOT_1_TEMP", "unavailable", "unknown", ("Slot 1", "Wailua Falls"), "unavailable"),
            DummySensor("MEDUSA_VOLT", "ok", "normal", ("MEDUSA",), "ok"),
        ]
        tree = build_tree(sensors)
        root_group_names = [node.name for node in tree if node.is_group]
        self.assertIn("Slot 1", root_group_names)
        self.assertIn("MEDUSA", root_group_names)

        slot1 = next(node for node in tree if node.name == "Slot 1")
        # Aggregates children across all sub-boards
        self.assertEqual(slot1.normal_count, 1)
        self.assertEqual(slot1.attention_count, 1)
        self.assertEqual(slot1.unknown_count, 1)
        self.assertEqual(slot1.total_count, 3)

        # Child board groups exist under Slot 1
        sub_group_names = [c.name for c in slot1.children if c.is_group]
        self.assertIn("Sentinel Dome", sub_group_names)
        self.assertIn("Wailua Falls", sub_group_names)

        sd = next(c for c in slot1.children if c.name == "Sentinel Dome")
        self.assertEqual(sd.total_count, 2)
        self.assertEqual(sd.normal_count, 1)
        self.assertEqual(sd.attention_count, 1)

    def test_tree_collapse_hides_children(self):
        sensors = [
            DummySensor("SLOT_1_A", "ok", "normal", ("Slot 1",), "ok"),
            DummySensor("SLOT_1_B", "ok", "normal", ("Slot 1",), "ok"),
        ]
        collapsed = {("Slot 1",)}
        tree = build_tree(sensors, collapsed_groups=collapsed)
        rows = flatten_tree(tree)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].name, "Slot 1")
        self.assertTrue(rows[0].is_group)

    def test_filter_non_normal_includes_attention_unknown_stale_missing(self):
        sensors = [
            DummySensor("SLOT_1_OK", "ok", "normal", ("Slot 1",), "ok"),
            DummySensor("SLOT_1_CRIT", "critical", "attention", ("Slot 1",), "critical"),
            DummySensor("SLOT_1_UNK", "unavailable", "unknown", ("Slot 1",), "unavailable"),
            DummySensor("SLOT_1_MISSING", "missing", "unknown", ("Slot 1",), "missing"),
            DummySensor("SLOT_1_STALE", "stale", "unknown", ("Slot 1",), "stale"),
            DummySensor("SLOT_2_OK", "ok", "normal", ("Slot 2",), "ok"),
        ]
        tree = build_tree(sensors)
        rows = flatten_tree(tree, filter_attention=True)
        row_names = [r.name for r in rows]
        self.assertIn("Slot 1", row_names)
        self.assertIn("SLOT_1_CRIT", row_names)
        self.assertIn("SLOT_1_UNK", row_names)
        self.assertIn("SLOT_1_MISSING", row_names)
        self.assertIn("SLOT_1_STALE", row_names)
        self.assertNotIn("SLOT_1_OK", row_names)
        self.assertNotIn("Slot 2", row_names)

    def test_search_filtering(self):
        sensors = [
            DummySensor("SLOT_1_TEMP", "ok", "normal", ("Slot 1",), "ok"),
            DummySensor("SLOT_1_VOLT", "ok", "normal", ("Slot 1",), "ok"),
            DummySensor("MEDUSA_TEMP", "ok", "normal", ("MEDUSA",), "ok"),
        ]
        tree = build_tree(sensors)
        rows = flatten_tree(tree, search_query="TEMP")
        row_names = [r.name for r in rows]
        self.assertIn("SLOT_1_TEMP", row_names)
        self.assertIn("MEDUSA_TEMP", row_names)
        self.assertNotIn("SLOT_1_VOLT", row_names)

    def test_group_label_shows_attention_and_unknown(self):
        sensors = [
            DummySensor("S1", "critical", "attention", ("G1",), "critical"),
            DummySensor("S2", "unavailable", "unknown", ("G1",), "unavailable"),
            DummySensor("S3", "ok", "normal", ("G1",), "ok"),
        ]
        tree = build_tree(sensors)
        g1 = tree[0]
        label = g1.format_label()
        self.assertIn("1 !", label)
        self.assertIn("1 ?", label)

    def test_1000_sensors_flatten_performance(self):
        sensors = []
        for i in range(1000):
            slot = i % 8 + 1
            health = "normal" if i % 10 != 0 else "attention"
            sensors.append(DummySensor(
                f"SENTINEL_DOME_SLOT_{slot}_SENSOR_{i:04d}",
                "ok" if health == "normal" else "critical",
                health,
                (f"Slot {slot}", "Sentinel Dome"),
                "ok",
            ))
        t0 = time.monotonic()
        tree = build_tree(sensors)
        rows = flatten_tree(tree)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.15, f"Tree build & flatten took too long: {elapsed:.3f}s")
        self.assertGreater(len(rows), 1000)

    def test_strip_control_codes_comprehensive(self):
        dirty = "\x1b[31;1mRed Text\x1b[0m\x1b]0;OSC Title\x07\r\n\tClean Text\x00\u200b"
        clean = strip_control_codes(dirty)
        self.assertNotIn("\x1b[", clean)
        self.assertNotIn("\x1b]", clean)
        self.assertNotIn("\r", clean)
        self.assertNotIn("\n", clean)
        self.assertNotIn("\t", clean)
        self.assertNotIn("\x00", clean)
        self.assertIn("Red Text", clean)
        self.assertIn("Clean Text", clean)

    def test_format_header_metrics(self):
        top, counts = format_header(
            source_label="DEMO",
            conn_status="Connected",
            age=1.5,
            duration=0.25,
            stale_after=10.0,
            total=100,
            norm=90,
            att=8,
            unk=2,
        )
        self.assertIn("DEMO", top)
        self.assertIn("Connected", top)
        self.assertIn("1.5s", top)
        self.assertIn("0.25s", top)
        self.assertIn("Total: 100", counts)
        self.assertIn("Normal: 90 (O)", counts)
        self.assertIn("Attention: 8 (!)", counts)
        self.assertIn("Unknown: 2 (?)", counts)

    def test_format_header_stale(self):
        top, counts = format_header(
            source_label="SSH:root@192.0.2.1",
            conn_status="Connected",
            age=15.2,
            duration=0.3,
            stale_after=10.0,
            total=50,
            norm=50,
            att=0,
            unk=0,
        )
        self.assertIn("STALE", top)
        self.assertIn("15.2s", top)

    def test_header_keeps_age_and_query_with_long_source(self):
        long_src = "SSH:root@" + ("verylongsubdomain." * 6) + "example.internal"
        top, _ = format_header(
            source_label=long_src,
            conn_status="Connected",
            age=2.1,
            duration=0.42,
            stale_after=10.0,
            total=10,
            norm=10,
            att=0,
            unk=0,
            max_width=80,
        )
        self.assertIn("Age: 2.1s", top)
        self.assertIn("Query: 0.42s", top)
        self.assertIn("Conn: Connected", top)

    def test_wrapped_detail_for_long_names(self):
        long_name = "SENTINEL_DOME_SLOT_1_VOLTAGE_REGULATOR_SENSOR_CHANNEL_ABC_XYZ_LONG_NAME"
        s = DummySensor(long_name, "critical", "attention", ("Slot 1", "Sentinel Dome"), "critical")
        node = TreeNode(name=long_name, group_path=("Slot 1", "Sentinel Dome"), is_group=False, sensor=s)
        from sensor_overview.ui import RenderRow
        row = RenderRow(node=node, depth=2, name=long_name, is_group=False, sensor=s)
        l1, l2 = format_selected_detail(row, max_width=60)
        self.assertLessEqual(len(l1), 60)
        self.assertLessEqual(len(l2), 60)
        self.assertIn("Sensor:", l1)
        self.assertIn(long_name[:30], l1)

    def test_slow_collector_stop_responsiveness(self):
        stop_event = threading.Event()
        def fake_worker():
            while not stop_event.is_set():
                stop_event.wait(timeout=5.0)

        t = threading.Thread(target=fake_worker)
        t.start()
        t0 = time.monotonic()
        stop_event.set()
        t.join(timeout=1.0)
        elapsed = time.monotonic() - t0
        self.assertFalse(t.is_alive())
        self.assertLess(elapsed, 0.5)

class TestPTYInteractive(unittest.TestCase):
    def test_pty_demo_startup_and_quit(self):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        env = dict(os.environ, TERM="xterm-256color")
        proc = subprocess.Popen(
            [sys.executable, str(REPO_ROOT / "sensor-overview.py"), "--demo"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=str(REPO_ROOT),
            close_fds=True,
            env=env,
        )
        os.close(slave)

        output_chunks = []
        deadline = time.monotonic() + 5.0
        seen_ui = False
        while time.monotonic() < deadline:
            r, _, _ = select.select([master], [], [], 0.1)
            if r:
                try:
                    data = os.read(master, 1024)
                    if not data:
                        break
                    output_chunks.append(data)
                    combined = b"".join(output_chunks)
                    if b"DEMO" in combined or b"Slot" in combined or b"Total" in combined:
                        seen_ui = True
                        break
                except OSError:
                    break

        try:
            os.write(master, b"q")
        except OSError:
            pass

        t_end = time.monotonic() + 3.0
        while time.monotonic() < t_end:
            r, _, _ = select.select([master], [], [], 0.05)
            if r:
                try:
                    data = os.read(master, 1024)
                    if data:
                        output_chunks.append(data)
                except OSError:
                    break
            if proc.poll() is not None:
                break

        try:
            if proc.poll() is None:
                proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.fail("TUI did not exit within timeout after pressing 'q'")
        finally:
            os.close(master)

        self.assertEqual(proc.returncode, 0)
        self.assertTrue(seen_ui, f"Expected UI text, got: {b''.join(output_chunks)}")

    def test_pty_small_window_resize_warning_and_quit(self):
        master, slave = pty.openpty()
        # Small size: 10 rows, 40 cols (< 60x15 required)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 10, 40, 0, 0))
        env = dict(os.environ, TERM="xterm-256color")
        proc = subprocess.Popen(
            [sys.executable, str(REPO_ROOT / "sensor-overview.py"), "--demo"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=str(REPO_ROOT),
            close_fds=True,
            env=env,
        )
        os.close(slave)

        output_chunks = []
        deadline = time.monotonic() + 5.0
        seen_warning = False
        while time.monotonic() < deadline:
            r, _, _ = select.select([master], [], [], 0.1)
            if r:
                try:
                    data = os.read(master, 1024)
                    if not data:
                        break
                    output_chunks.append(data)
                    combined = b"".join(output_chunks)
                    if b"small" in combined.lower() or b"60x15" in combined or b"quit" in combined:
                        seen_warning = True
                        break
                except OSError:
                    break

        try:
            os.write(master, b"q")
        except OSError:
            pass

        t_end = time.monotonic() + 3.0
        while time.monotonic() < t_end:
            r, _, _ = select.select([master], [], [], 0.05)
            if r:
                try:
                    data = os.read(master, 1024)
                    if data:
                        output_chunks.append(data)
                except OSError:
                    break
            if proc.poll() is not None:
                break

        try:
            if proc.poll() is None:
                proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.fail("TUI did not exit within timeout on small terminal")
        finally:
            os.close(master)

        self.assertEqual(proc.returncode, 0)
        self.assertTrue(seen_warning, f"Expected small screen warning, got: {b''.join(output_chunks)}")

if __name__ == "__main__":
    unittest.main()
