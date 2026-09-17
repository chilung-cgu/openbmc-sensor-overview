"""Unit tests for Dense Matrix View and HUD (Task 2)."""

import curses
import unittest
from sensor_overview.model import Sensor


class MockCursesWindow:
    """Mock curses window simulating standard curses window behavior."""

    def __init__(self, height: int = 24, width: int = 80):
        self.height = height
        self.width = width
        self.calls = []
        self.buffer = [[" " for _ in range(width)] for _ in range(height)]

    def getmaxyx(self) -> tuple[int, int]:
        return (self.height, self.width)

    def addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        self.calls.append(("addstr", y, x, text, attr))
        if y < 0 or y >= self.height or x < 0 or x >= self.width:
            raise curses.error("Coordinates out of bounds")
        # Standard curses behavior: writing beyond or to bottom-right corner raises curses.error
        if y == self.height - 1 and x + len(text) > self.width:
            raise curses.error("Writing beyond terminal bottom-right raises wrap error")
        if y == self.height - 1 and (x + len(text)) == self.width and len(text) > 0:
            # curses raises error on exact bottom-right character wrap
            raise curses.error("Terminal wrap at bottom right corner")
        for i, ch in enumerate(text):
            if x + i < self.width and y < self.height:
                self.buffer[y][x + i] = ch

    def addch(self, y: int, x: int, ch: int | str, attr: int = 0) -> None:
        self.calls.append(("addch", y, x, ch, attr))
        if y < 0 or y >= self.height or x < 0 or x >= self.width:
            raise curses.error("Coordinates out of bounds")
        if y == self.height - 1 and x == self.width - 1:
            raise curses.error("Bottom-right corner character wrap error")
        self.buffer[y][x] = chr(ch) if isinstance(ch, int) else ch

    def insstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        self.calls.append(("insstr", y, x, text, attr))
        if y < 0 or y >= self.height or x < 0 or x >= self.width:
            raise curses.error("insstr out of bounds")
        for i, ch in enumerate(text):
            if x + i < self.width:
                self.buffer[y][x + i] = ch

    def erase(self) -> None:
        self.calls.append(("erase",))
        self.buffer = [[" " for _ in range(self.width)] for _ in range(self.height)]

    def refresh(self) -> None:
        self.calls.append(("refresh",))


class TestMatrixView(unittest.TestCase):
    def setUp(self):
        from sensor_overview.matrix_view import (
            MatrixLayoutEngine,
            MatrixState,
            classify_topology,
            find_most_severe_sensor,
            find_next_abnormal_index,
            format_hud,
            get_sensor_severity,
            navigate_cursor,
            render_matrix_view,
            safe_addch,
            safe_addstr,
            SEVERITY_CRITICAL,
            SEVERITY_NORMAL,
            SEVERITY_UNAVAILABLE,
            SEVERITY_WARNING,
            TOPOLOGY_CATEGORIES,
        )
        self.engine = MatrixLayoutEngine()
        self.classify_topology = classify_topology
        self.find_most_severe_sensor = find_most_severe_sensor
        self.find_next_abnormal_index = find_next_abnormal_index
        self.format_hud = format_hud
        self.get_sensor_severity = get_sensor_severity
        self.navigate_cursor = navigate_cursor
        self.render_matrix_view = render_matrix_view
        self.safe_addstr = safe_addstr
        self.safe_addch = safe_addch
        self.MatrixState = MatrixState
        self.SEVERITY_CRITICAL = SEVERITY_CRITICAL
        self.SEVERITY_WARNING = SEVERITY_WARNING
        self.SEVERITY_UNAVAILABLE = SEVERITY_UNAVAILABLE
        self.SEVERITY_NORMAL = SEVERITY_NORMAL
        self.TOPOLOGY_CATEGORIES = TOPOLOGY_CATEGORIES

    def _make_sensor(self, name: str, status: str = "ok", health: str = "normal", group: tuple = ("Unclassified",), **kwargs):
        s = Sensor(
            name=name,
            status=status,
            health=health,
            group=group,
            raw_status=status,
        )
        for k, v in kwargs.items():
            object.__setattr__(s, k, v)
        return s

    def test_700_sensors_layout_80x24_compact_strips(self):
        """Test coordinate calculation for 700+ sensors in standard 80x24 terminal."""
        sensors = []
        for i in range(1, 9):
            for j in range(70):
                sensors.append(self._make_sensor(
                    f"SENTINEL_DOME_SLOT_{i}_TEMP_{j}",
                    group=(f"Slot {i}", "Sentinel Dome")
                ))
        for j in range(60):
            sensors.append(self._make_sensor(f"MGNT_TEMP_{j}", group=("Baseboard",)))
        for j in range(40):
            sensors.append(self._make_sensor(f"MEDUSA_PWR_{j}", group=("Medusa/Power",)))
        for j in range(30):
            sensors.append(self._make_sensor(f"FANBOARD0_TACH_{j}", group=("Fan",)))
        for j in range(30):
            sensors.append(self._make_sensor(f"SPIDER_MISC_{j}", group=("Others",)))

        self.assertGreaterEqual(len(sensors), 700)
        result = self.engine.compute_layout(sensors, width=80, height=24)

        self.assertFalse(result.is_too_small)
        self.assertEqual(result.mode, "compact")
        self.assertEqual(len(result.cells), len(sensors))

        hud_y = result.hud_rect[1]
        self.assertGreaterEqual(hud_y, 20)

        coords_set = set()
        for cell in result.cells:
            self.assertGreaterEqual(cell.x, 0)
            self.assertLess(cell.x, 80)
            self.assertGreaterEqual(cell.y, 2, "Cell must not overlap header (lines 0-1)")
            self.assertLess(cell.y, hud_y, "Cell must not overlap HUD area")
            coords_set.add((cell.x, cell.y))

        self.assertGreater(len(coords_set), 600)

    def test_700_sensors_layout_120x36_blocks(self):
        """Test coordinate calculation for 700+ sensors in large 120x36 terminal with independent blocks."""
        sensors = []
        for i in range(1, 9):
            for j in range(65):
                sensors.append(self._make_sensor(
                    f"WAILUA_FALLS_SLOT_{i}_SENSOR_{j}",
                    group=(f"Slot {i}", "Wailua Falls")
                ))
        for j in range(70):
            sensors.append(self._make_sensor(f"SYSTEM_PWR_{j}", group=("Baseboard",)))
        for j in range(50):
            sensors.append(self._make_sensor(f"MEDUSA_VOLT_{j}", group=("Medusa/Power",)))
        for j in range(40):
            sensors.append(self._make_sensor(f"FANBOARD1_PWM_{j}", group=("Fan",)))
        for j in range(40):
            sensors.append(self._make_sensor(f"OTHER_TEMP_{j}", group=("Others",)))

        self.assertGreaterEqual(len(sensors), 700)
        result = self.engine.compute_layout(sensors, width=120, height=36)

        self.assertFalse(result.is_too_small)
        self.assertEqual(result.mode, "blocks")
        self.assertEqual(len(result.cells), len(sensors))

        hud_y = result.hud_rect[1]
        self.assertGreaterEqual(hud_y, 30)

        for cat in self.TOPOLOGY_CATEGORIES:
            self.assertIn(cat, result.blocks)
            bx, by, bw, bh = result.blocks[cat]
            self.assertGreaterEqual(bx, 0)
            self.assertLessEqual(bx + bw, 120)
            self.assertGreaterEqual(by, 2)
            self.assertLessEqual(by + bh, hud_y)

        for cell in result.cells:
            bx, by, bw, bh = result.blocks[cell.category]
            self.assertGreaterEqual(cell.x, bx, f"Cell x {cell.x} must be >= block left {bx}")
            self.assertLess(cell.x, bx + bw, f"Cell x {cell.x} must be inside block right {bx + bw}")
            self.assertGreater(cell.y, by, f"Cell y {cell.y} must be > block top {by}")
            self.assertLess(cell.y, by + bh, f"Cell y {cell.y} must be inside block bottom {by + bh}")

    def test_topology_grouping_classification(self):
        """Test topology category classification for all required categories."""
        for i in range(1, 9):
            s = self._make_sensor(f"SENTINEL_DOME_SLOT_{i}_TEMP", group=(f"Slot {i}", "Sentinel Dome"))
            self.assertEqual(self.classify_topology(s), f"Slot {i}")
            s2 = self._make_sensor(f"WAILUA_FALLS_SLOT_{i}_VOLT")
            self.assertEqual(self.classify_topology(s2), f"Slot {i}")

        self.assertEqual(self.classify_topology(self._make_sensor("NIC0_TEMP_C")), "Baseboard")
        self.assertEqual(self.classify_topology(self._make_sensor("MGNT_TEMP")), "Baseboard")
        self.assertEqual(self.classify_topology(self._make_sensor("SYSTEM_AIRFLOW")), "Baseboard")
        self.assertEqual(self.classify_topology(self._make_sensor("bmc/cpu/kernel")), "Baseboard")

        self.assertEqual(self.classify_topology(self._make_sensor("MEDUSA_MB1_PWR")), "Medusa/Power")
        self.assertEqual(self.classify_topology(self._make_sensor("PSU0_INPUT_PWR_W")), "Medusa/Power")

        self.assertEqual(self.classify_topology(self._make_sensor("FANBOARD0_TACH")), "Fan")
        self.assertEqual(self.classify_topology(self._make_sensor("VIRTUAL_FANBOARD0_FAN0_48V_PWR_W")), "Fan")

        self.assertEqual(self.classify_topology(self._make_sensor("SPIDER_TEMP")), "Others")
        self.assertEqual(self.classify_topology(self._make_sensor("UNKNOWN_TEMP")), "Others")

    def test_too_small_protection_mode(self):
        """Test minimum terminal dimension protection (< 80x22)."""
        self.assertTrue(self.engine.is_terminal_too_small(79, 24))
        self.assertTrue(self.engine.is_terminal_too_small(80, 21))
        self.assertTrue(self.engine.is_terminal_too_small(60, 15))
        self.assertTrue(self.engine.is_terminal_too_small(40, 10))

        self.assertFalse(self.engine.is_terminal_too_small(80, 22))
        self.assertFalse(self.engine.is_terminal_too_small(80, 24))
        self.assertFalse(self.engine.is_terminal_too_small(120, 36))

        res = self.engine.compute_layout([], width=70, height=20)
        self.assertTrue(res.is_too_small)
        self.assertEqual(res.mode, "too_small")
        self.assertEqual(len(res.cells), 0)

        win = MockCursesWindow(height=18, width=70)
        sensors = [self._make_sensor("TEST")]
        try:
            self.render_matrix_view(win, sensors)
        except Exception as e:
            self.fail(f"render_matrix_view raised unexpected exception on small terminal: {e}")

        rendered_text = "".join("".join(row) for row in win.buffer)
        self.assertIn("Terminal too small", rendered_text)
        self.assertIn("80x22", rendered_text)

    def test_safe_addstr_bottom_right_corner_protection(self):
        """Test writing to bottom-right corner does not raise curses wrap error."""
        win = MockCursesWindow(height=24, width=80)

        with self.assertRaises(curses.error):
            win.addstr(23, 79, "X")

        try:
            self.safe_addstr(win, 23, 79, "X")
        except curses.error as e:
            self.fail(f"safe_addstr raised curses.error at bottom right: {e}")

        try:
            self.safe_addstr(win, 23, 0, "A" * 80)
        except curses.error as e:
            self.fail(f"safe_addstr raised curses.error on full bottom line: {e}")

        self.assertFalse(self.safe_addstr(win, -1, 0, "TEST"))
        self.assertFalse(self.safe_addstr(win, 24, 0, "TEST"))
        self.assertFalse(self.safe_addstr(win, 0, 80, "TEST"))
        self.assertFalse(self.safe_addstr(win, 0, -5, "TEST"))

    def test_safe_addch_bottom_right_corner_protection(self):
        """Test safe_addch at bottom-right corner."""
        win = MockCursesWindow(height=24, width=80)
        with self.assertRaises(curses.error):
            win.addch(23, 79, ord("Z"))

        try:
            self.safe_addch(win, 23, 79, ord("Z"))
        except curses.error as e:
            self.fail(f"safe_addch raised curses.error at bottom-right: {e}")

    def test_hud_auto_lock_severity_hierarchy(self):
        """Test HUD auto-lock prioritizes CRITICAL > WARNING > UNAVAILABLE > NORMAL."""
        s_normal = self._make_sensor("NORM_S", status="ok", health="normal")
        s_unavail = self._make_sensor("UNAV_S", status="missing", health="unknown")
        s_warn = self._make_sensor("WARN_S", status="warning", health="attention")
        s_crit = self._make_sensor("CRIT_S", status="critical", health="attention")

        # 1. CRITICAL beats everything
        pool1 = [s_normal, s_unavail, s_warn, s_crit]
        idx, chosen = self.find_most_severe_sensor(pool1)
        self.assertEqual(idx, 3)
        self.assertEqual(chosen.name, "CRIT_S")

        # 2. WARNING beats UNAVAILABLE and NORMAL
        pool2 = [s_normal, s_unavail, s_warn]
        idx, chosen = self.find_most_severe_sensor(pool2)
        self.assertEqual(idx, 2)
        self.assertEqual(chosen.name, "WARN_S")

        # 3. UNAVAILABLE beats NORMAL
        pool3 = [s_normal, s_unavail]
        idx, chosen = self.find_most_severe_sensor(pool3)
        self.assertEqual(idx, 1)
        self.assertEqual(chosen.name, "UNAV_S")

        # 4. All NORMAL sensors
        pool4 = [s_normal, self._make_sensor("NORM_2", status="ok", health="normal")]
        idx, chosen = self.find_most_severe_sensor(pool4, anomalies_only=False)
        self.assertEqual(chosen.name, "NORM_S")

        idx_anomaly, chosen_anomaly = self.find_most_severe_sensor(pool4, anomalies_only=True)
        self.assertIsNone(chosen_anomaly)

        # 5. Multiple CRITICAL sensors: select first encountered
        s_crit_2 = self._make_sensor("CRIT_2", status="critical", health="attention")
        pool5 = [s_normal, s_crit, s_crit_2]
        idx, chosen = self.find_most_severe_sensor(pool5)
        self.assertEqual(idx, 1)
        self.assertEqual(chosen.name, "CRIT_S")

    def test_hud_format_lines(self):
        """Test HUD formatting produces valid, clean lines bounded by terminal width."""
        s = self._make_sensor(
            "SENTINEL_DOME_SLOT_1_TEMP",
            status="critical",
            health="attention",
            group=("Slot 1", "Sentinel Dome")
        )
        lines = self.format_hud(s, width=80)
        self.assertGreaterEqual(len(lines), 3)
        for line in lines:
            self.assertLessEqual(len(line), 80)
            self.assertNotIn("", line, "HUD lines must not contain ANSI escape codes")

        content = " ".join(lines)
        self.assertIn("SENTINEL_DOME_SLOT_1_TEMP", content)
        self.assertIn("CRITICAL", content)

    def test_tab_jump_to_next_abnormal(self):
        """Test Tab jump calculation to next abnormal sensor with cyclical wraparound."""
        sensors = [
            self._make_sensor("S0", status="ok", health="normal"),
            self._make_sensor("S1_WARN", status="warning", health="attention"),
            self._make_sensor("S2", status="ok", health="normal"),
            self._make_sensor("S3_CRIT", status="critical", health="attention"),
            self._make_sensor("S4", status="ok", health="normal"),
            self._make_sensor("S5_UNAV", status="missing", health="unknown"),
            self._make_sensor("S6", status="ok", health="normal"),
        ]

        self.assertEqual(self.find_next_abnormal_index(sensors, current_index=0), 1)
        self.assertEqual(self.find_next_abnormal_index(sensors, current_index=1), 3)
        self.assertEqual(self.find_next_abnormal_index(sensors, current_index=2), 3)
        self.assertEqual(self.find_next_abnormal_index(sensors, current_index=3), 5)
        self.assertEqual(self.find_next_abnormal_index(sensors, current_index=4), 5)
        self.assertEqual(self.find_next_abnormal_index(sensors, current_index=5), 1)
        self.assertEqual(self.find_next_abnormal_index(sensors, current_index=6), 1)

        self.assertEqual(self.find_next_abnormal_index(sensors, current_index=5, reverse=True), 3)
        self.assertEqual(self.find_next_abnormal_index(sensors, current_index=3, reverse=True), 1)
        self.assertEqual(self.find_next_abnormal_index(sensors, current_index=1, reverse=True), 5)
        self.assertEqual(self.find_next_abnormal_index(sensors, current_index=0, reverse=True), 5)

        all_ok = [self._make_sensor(f"OK_{i}", status="ok", health="normal") for i in range(5)]
        self.assertIsNone(self.find_next_abnormal_index(all_ok, current_index=2))

        # Single abnormal sensor jumps back to itself
        one_bad = [self._make_sensor("OK_0"), self._make_sensor("BAD_1", status="critical")]
        self.assertEqual(self.find_next_abnormal_index(one_bad, current_index=0), 1)
        self.assertEqual(self.find_next_abnormal_index(one_bad, current_index=1), 1)
        self.assertEqual(self.find_next_abnormal_index(one_bad, current_index=1, reverse=True), 1)

    def test_cursor_navigation_2d(self):
        """Test cursor 2D spatial navigation (LEFT, RIGHT, UP, DOWN)."""
        sensors = [self._make_sensor(f"S_{i}") for i in range(100)]
        layout = self.engine.compute_layout(sensors, width=80, height=24)

        self.assertEqual(self.navigate_cursor("LEFT", 0, layout), 0)
        self.assertEqual(self.navigate_cursor("RIGHT", 0, layout), 1)
        last_idx = len(sensors) - 1
        self.assertEqual(self.navigate_cursor("RIGHT", last_idx, layout), last_idx)
        self.assertEqual(self.navigate_cursor("LEFT", last_idx, layout), last_idx - 1)

        cur_cell = layout.cells[10]
        down_idx = self.navigate_cursor("DOWN", 10, layout)
        down_cell = layout.cells[down_idx]
        self.assertGreater(down_cell.y, cur_cell.y)

        up_idx = self.navigate_cursor("UP", down_idx, layout)
        up_cell = layout.cells[up_idx]
        self.assertLess(up_cell.y, down_cell.y)

    def test_render_matrix_view_full_render(self):
        """Test full render_matrix_view pipeline executes without error and draws expected content."""
        sensors = [
            self._make_sensor("SENTINEL_DOME_SLOT_1_TEMP", status="ok"),
            self._make_sensor("SENTINEL_DOME_SLOT_2_TEMP", status="critical"),
            self._make_sensor("MGNT_TEMP", status="warning"),
        ]
        win = MockCursesWindow(height=24, width=80)
        state = self.MatrixState()
        self.render_matrix_view(win, sensors, state=state)

        # Verify auto-lock locked onto SLOT_2 (CRITICAL)
        self.assertEqual(state.cursor_index, 1)

        # Check that header and HUD were drawn into the buffer
        buffer_str = chr(10).join("".join(row) for row in win.buffer)
        self.assertIn("SENSOR MATRIX", buffer_str)
        self.assertIn("HUD: [CRITICAL] SENTINEL_DOME_SLOT_2_TEMP", buffer_str)

    def test_empty_sensors_render(self):
        """Test rendering with empty sensor list does not raise exception."""
        win = MockCursesWindow(height=24, width=80)
        state = self.MatrixState()
        self.render_matrix_view(win, [], state=state)
        buffer_str = chr(10).join("".join(row) for row in win.buffer)
        self.assertIn("SENSOR MATRIX", buffer_str)
        self.assertIn("All Sensors Normal", buffer_str)


    def test_render_matrix_view_with_connection_metadata(self):
        """Test render_matrix_view correctly formats Conn, Age, and Query in header."""
        win = MockCursesWindow(height=30, width=140)
        sensors = [
            self._make_sensor("TEMP_1", status="ok", health="normal"),
            self._make_sensor("TEMP_2", status="critical", health="critical"),
        ]
        state = self.MatrixState()
        self.render_matrix_view(
            win,
            sensors,
            state=state,
            source_label="SSH:root@192.0.2.1",
            conn_status="Connected",
            age=1.5,
            last_duration=2.75,
            stale_after=30.0,
        )
        first_line = "".join(win.buffer[0])
        self.assertIn("Conn: Connected", first_line)
        self.assertIn("Age: 1.5s", first_line)
        self.assertIn("Query: 2.75s", first_line)


if __name__ == "__main__":
    unittest.main()
