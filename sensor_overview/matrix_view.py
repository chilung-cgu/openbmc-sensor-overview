"""Dense Matrix View and Layout Engine for OpenBMC Sensor Matrix TUI (Task 2)."""

import curses
import re
from dataclasses import dataclass
from typing import Any, Sequence

__all__ = [
    "SEVERITY_NORMAL",
    "SEVERITY_UNAVAILABLE",
    "SEVERITY_WARNING",
    "SEVERITY_CRITICAL",
    "SEVERITY_NAMES",
    "TOPOLOGY_CATEGORIES",
    "GLYPHS_ASCII",
    "GLYPHS_UNICODE",
    "classify_topology",
    "get_sensor_severity",
    "find_most_severe_sensor",
    "find_next_abnormal_index",
    "navigate_cursor",
    "safe_addstr",
    "safe_addch",
    "format_hud",
    "MatrixCell",
    "MatrixLayoutResult",
    "MatrixLayoutEngine",
    "MatrixState",
    "render_matrix_view",
]


# Severity levels in ascending order of priority: CRITICAL > WARNING > UNAVAILABLE > NORMAL
SEVERITY_NORMAL = 0
SEVERITY_UNAVAILABLE = 1
SEVERITY_WARNING = 2
SEVERITY_CRITICAL = 3

SEVERITY_NAMES = {
    SEVERITY_NORMAL: "NORMAL",
    SEVERITY_UNAVAILABLE: "UNAVAILABLE",
    SEVERITY_WARNING: "WARNING",
    SEVERITY_CRITICAL: "CRITICAL",
}

TOPOLOGY_CATEGORIES: tuple[str, ...] = (
    "Slot 1",
    "Slot 2",
    "Slot 3",
    "Slot 4",
    "Slot 5",
    "Slot 6",
    "Slot 7",
    "Slot 8",
    "Baseboard",
    "Medusa/Power",
    "Fan",
    "Others",
)

GLYPHS_ASCII = {
    SEVERITY_NORMAL: ".",
    SEVERITY_UNAVAILABLE: "?",
    SEVERITY_WARNING: "^",
    SEVERITY_CRITICAL: "X",
}

GLYPHS_UNICODE = {
    SEVERITY_NORMAL: "■",
    SEVERITY_UNAVAILABLE: "○",
    SEVERITY_WARNING: "▲",
    SEVERITY_CRITICAL: "■",
}

_SLOT_RE = re.compile(r'(?:SENTINEL_DOME|WAILUA_FALLS)?_?SLOT_?([1-8])(?:_|$)', re.IGNORECASE)
_FAN_RE = re.compile(r'(?:FANBOARD\d*|VIRTUAL_FANBOARD\d*|FAN|TACH|PWM)', re.IGNORECASE)
_MEDUSA_PWR_RE = re.compile(r'^(?:MEDUSA|PSU|POWER|PWR)', re.IGNORECASE)
_MEDUSA_IN_NAME_RE = re.compile(r'(?:^|_)MEDUSA\d*(?:_|$)', re.IGNORECASE)
_PSU_IN_NAME_RE = re.compile(r'(?:^|_)PSU\d*(?:_|$)', re.IGNORECASE)
_BASEBOARD_RE = re.compile(r'^(?:MGNT|SYSTEM|VIRTUAL_NIC|NIC|bmc/|CPU$|Memory$)', re.IGNORECASE)


def classify_topology(sensor: Any) -> str:
    """Classify a sensor into one of the standard topology categories."""
    name = getattr(sensor, "name", str(sensor))
    group = getattr(sensor, "group", ())

    # 1. Slot 1 ~ 8 checks
    if isinstance(group, (tuple, list)):
        for item in group:
            m = re.match(r'^Slot\s*([1-8])$', str(item), re.IGNORECASE)
            if m:
                return f"Slot {m.group(1)}"

    m_slot = _SLOT_RE.search(name)
    if m_slot:
        return f"Slot {m_slot.group(1)}"

    # 2. Fan checks
    if isinstance(group, (tuple, list)):
        for item in group:
            if "FAN" in str(item).upper():
                return "Fan"
    if _FAN_RE.search(name):
        return "Fan"

    # 3. Medusa / Power checks
    if isinstance(group, (tuple, list)):
        for item in group:
            item_u = str(item).upper()
            if "MEDUSA" in item_u or "PSU" in item_u or "POWER" in item_u:
                return "Medusa/Power"
    if _MEDUSA_PWR_RE.match(name) or _MEDUSA_IN_NAME_RE.search(name) or _PSU_IN_NAME_RE.search(name):
        return "Medusa/Power"

    # 4. Baseboard / Infrastructure checks
    if isinstance(group, (tuple, list)):
        for item in group:
            if item in ("Baseboard", "System", "BMC metrics", "NIC", "MGNT"):
                return "Baseboard"
    if _BASEBOARD_RE.search(name):
        return "Baseboard"

    return "Others"


def get_sensor_severity(sensor: Any) -> int:
    """Return integer severity for a sensor (CRITICAL > WARNING > UNAVAILABLE > NORMAL)."""
    status = str(getattr(sensor, "status", "unknown")).strip().lower()
    health = str(getattr(sensor, "health", "unknown")).strip().lower()

    if getattr(sensor, "functional", True) is False:
        return SEVERITY_CRITICAL

    if status in ("critical", "crit", "high", "low") or "critical" in status:
        return SEVERITY_CRITICAL

    if status in ("warning", "warn") or "warn" in status:
        return SEVERITY_WARNING

    if getattr(sensor, "available", True) is False:
        return SEVERITY_UNAVAILABLE

    if status in ("unavailable", "missing", "stale", "invalid value", "unknown"):
        return SEVERITY_UNAVAILABLE

    if health == "attention":
        return SEVERITY_WARNING
    if health == "unknown":
        return SEVERITY_UNAVAILABLE
    if status in ("ok", "normal") or health == "normal":
        return SEVERITY_NORMAL

    return SEVERITY_UNAVAILABLE


def find_most_severe_sensor(
    sensors: Sequence[Any],
    anomalies_only: bool = True
) -> tuple[int | None, Any | None]:
    """Find the sensor with highest severity. Returns (index, sensor) or (None, None)."""
    if not sensors:
        return (None, None)

    best_idx = None
    best_sensor = None
    best_sev = -1

    for idx, s in enumerate(sensors):
        sev = get_sensor_severity(s)
        if anomalies_only and sev == SEVERITY_NORMAL:
            continue
        if sev > best_sev:
            best_sev = sev
            best_idx = idx
            best_sensor = s

    return (best_idx, best_sensor)


def find_next_abnormal_index(
    sensors: Sequence[Any],
    current_index: int,
    reverse: bool = False
) -> int | None:
    """Find next abnormal sensor index with cyclical wraparound."""
    abnormal_indices = [
        i for i, s in enumerate(sensors)
        if get_sensor_severity(s) > SEVERITY_NORMAL
    ]
    if not abnormal_indices:
        return None

    if not reverse:
        for idx in abnormal_indices:
            if idx > current_index:
                return idx
        return abnormal_indices[0]
    else:
        for idx in reversed(abnormal_indices):
            if idx < current_index:
                return idx
        return abnormal_indices[-1]


@dataclass(frozen=True)
class MatrixCell:
    sensor_index: int
    sensor: Any
    x: int
    y: int
    category: str
    severity: int


@dataclass
class MatrixLayoutResult:
    mode: str  # "blocks", "compact", "too_small"
    is_too_small: bool
    width: int
    height: int
    cells: list[MatrixCell]
    blocks: dict[str, tuple[int, int, int, int]]  # category -> (x, y, w, h)
    hud_rect: tuple[int, int, int, int]  # (x, y, w, h)
    header_rect: tuple[int, int, int, int]  # (x, y, w, h)


class MatrixLayoutEngine:
    MIN_WIDTH: int = 80
    MIN_HEIGHT: int = 22

    def is_terminal_too_small(self, width: int, height: int) -> bool:
        return width < self.MIN_WIDTH or height < self.MIN_HEIGHT

    def compute_layout(
        self,
        sensors: Sequence[Any],
        width: int,
        height: int,
        preferred_mode: str | None = None,
    ) -> MatrixLayoutResult:
        if self.is_terminal_too_small(width, height):
            return MatrixLayoutResult(
                mode="too_small",
                is_too_small=True,
                width=width,
                height=height,
                cells=[],
                blocks={},
                hud_rect=(0, max(0, height - 3), width, 3),
                header_rect=(0, 0, width, min(height, 2)),
            )

        header_rect = (0, 0, width, 2)
        hud_h = 3
        hud_y = height - hud_h
        hud_rect = (0, hud_y, width, hud_h)

        matrix_top = 2
        matrix_bottom = hud_y

        # Group sensors by category preserving original index
        grouped: dict[str, list[tuple[int, Any]]] = {cat: [] for cat in TOPOLOGY_CATEGORIES}
        for idx, s in enumerate(sensors):
            cat = classify_topology(s)
            if cat not in grouped:
                grouped[cat] = []
            grouped[cat].append((idx, s))

        # Select mode
        if preferred_mode in ("blocks", "compact"):
            mode = preferred_mode
        else:
            mode = "blocks" if (width >= 110 and height >= 32) else "compact"

        if mode == "blocks":
            # 3 rows x 4 columns grid of blocks
            cols = 4
            rows = 3
            col_w = width // cols
            row_h = (matrix_bottom - matrix_top) // rows

            blocks: dict[str, tuple[int, int, int, int]] = {}
            cells: list[MatrixCell] = []

            for idx, cat in enumerate(TOPOLOGY_CATEGORIES):
                r = idx // cols
                c = idx % cols
                bx = c * col_w
                by = matrix_top + r * row_h
                bw = col_w
                bh = row_h
                blocks[cat] = (bx, by, bw, bh)

                cat_sensors = grouped.get(cat, [])
                inner_left = bx + 1
                inner_right = bx + bw - 1
                inner_top = by + 1
                inner_bottom = by + bh - 1

                inner_w = max(1, inner_right - inner_left)
                inner_h = max(1, inner_bottom - inner_top)

                for i, (orig_idx, s) in enumerate(cat_sensors):
                    ix = i % inner_w
                    iy = i // inner_w
                    cx = inner_left + ix
                    cy = min(inner_bottom - 1, inner_top + iy)
                    sev = get_sensor_severity(s)
                    cells.append(MatrixCell(
                        sensor_index=orig_idx,
                        sensor=s,
                        x=cx,
                        y=cy,
                        category=cat,
                        severity=sev,
                    ))

            # Keep cells ordered by layout category/grouping for contiguous navigation
            # (cells were appended category by category, line by line)
            return MatrixLayoutResult(
                mode="blocks",
                is_too_small=False,
                width=width,
                height=height,
                cells=cells,
                blocks=blocks,
                hud_rect=hud_rect,
                header_rect=header_rect,
            )

        # mode == "compact"
        blocks = {}
        cells = []
        current_y = matrix_top

        for cat in TOPOLOGY_CATEGORIES:
            cat_sensors = grouped.get(cat, [])
            if not cat_sensors:
                blocks[cat] = (0, current_y, width, 0)
                continue

            label = f"{cat}: "
            label_len = len(label)
            dot_width = max(1, width - label_len)
            start_y = current_y

            for i, (orig_idx, s) in enumerate(cat_sensors):
                line_offset = i // dot_width
                col_offset = i % dot_width
                cy = current_y + line_offset
                if cy >= matrix_bottom:
                    cy = matrix_bottom - 1
                    cx = min(width - 1, label_len + (i % dot_width))
                else:
                    cx = label_len + col_offset

                sev = get_sensor_severity(s)
                cells.append(MatrixCell(
                    sensor_index=orig_idx,
                    sensor=s,
                    x=cx,
                    y=cy,
                    category=cat,
                    severity=sev,
                ))

            num_lines = (len(cat_sensors) + dot_width - 1) // dot_width
            block_h = max(1, num_lines)
            blocks[cat] = (0, start_y, width, block_h)
            current_y += num_lines

            # Keep cells ordered by layout category/grouping for contiguous navigation
            # (cells were appended category by category, line by line)
        return MatrixLayoutResult(
            mode="compact",
            is_too_small=False,
            width=width,
            height=height,
            cells=cells,
            blocks=blocks,
            hud_rect=hud_rect,
            header_rect=header_rect,
        )


def navigate_cursor(
    direction: str,
    current_sensor_index: int,
    layout: MatrixLayoutResult,
) -> int:
    """Navigate cursor in 2D space across matrix cells.

    Takes current_sensor_index (pointing to the original sensor list),
    maps it to the cell in the layout, navigates along layout order (LEFT/RIGHT)
    or spatial 2D coordinates (UP/DOWN), and returns the new sensor_index.
    """
    if not layout.cells:
        return 0

    # Locate current cell index in layout.cells by sensor_index
    cell_idx = 0
    for idx, c in enumerate(layout.cells):
        if c.sensor_index == current_sensor_index:
            cell_idx = idx
            break
    else:
        cell_idx = max(0, min(len(layout.cells) - 1, current_sensor_index))

    cur_cell = layout.cells[cell_idx]

    if direction == "LEFT":
        next_cell_idx = max(0, cell_idx - 1)
        return layout.cells[next_cell_idx].sensor_index
    elif direction == "RIGHT":
        next_cell_idx = min(len(layout.cells) - 1, cell_idx + 1)
        return layout.cells[next_cell_idx].sensor_index
    elif direction == "UP":
        cur_y = cur_cell.y
        cur_x = cur_cell.x
        cells_above = [c for c in layout.cells if c.y < cur_y]
        if not cells_above:
            return cur_cell.sensor_index
        max_prev_y = max(c.y for c in cells_above)
        row_cells = [c for c in cells_above if c.y == max_prev_y]
        best = min(row_cells, key=lambda c: (abs(c.x - cur_x), c.sensor_index))
        return best.sensor_index
    elif direction == "DOWN":
        cur_y = cur_cell.y
        cur_x = cur_cell.x
        cells_below = [c for c in layout.cells if c.y > cur_y]
        if not cells_below:
            return cur_cell.sensor_index
        min_next_y = min(c.y for c in cells_below)
        row_cells = [c for c in cells_below if c.y == min_next_y]
        best = min(row_cells, key=lambda c: (abs(c.x - cur_x), c.sensor_index))
        return best.sensor_index

    return cur_cell.sensor_index


def safe_addstr(win: Any, y: int, x: int, text: str, attr: int = 0) -> bool:
    """Safely write string to curses window avoiding bottom-right corner wrap errors."""
    max_y, max_x = win.getmaxyx()
    if y < 0 or y >= max_y or x < 0 or x >= max_x or not text:
        return False

    available = max_x - x
    if len(text) > available:
        text = text[:available]

    # Special handling for bottom row
    if y == max_y - 1 and (x + len(text)) >= max_x:
        normal_part = text[:max_x - x - 1]
        last_char = text[max_x - x - 1:]
        if normal_part:
            try:
                win.addstr(y, x, normal_part, attr)
            except curses.error:
                pass
        if last_char:
            last_x = max_x - 1
            try:
                if hasattr(win, "insstr"):
                    win.insstr(y, last_x, last_char[0], attr)
                else:
                    win.addch(y, last_x, ord(last_char[0]), attr)
            except curses.error:
                pass
        return True

    try:
        win.addstr(y, x, text, attr)
        return True
    except curses.error:
        return False


def safe_addch(win: Any, y: int, x: int, ch: int | str, attr: int = 0) -> bool:
    """Safely write character to curses window avoiding bottom-right corner wrap errors."""
    max_y, max_x = win.getmaxyx()
    if y < 0 or y >= max_y or x < 0 or x >= max_x:
        return False

    ch_str = ch if isinstance(ch, str) else chr(ch)
    if y == max_y - 1 and x == max_x - 1:
        try:
            if hasattr(win, "insstr"):
                win.insstr(y, x, ch_str, attr)
            else:
                win.addch(y, x, ch, attr)
        except curses.error:
            pass
        return True

    try:
        win.addch(y, x, ch, attr)
        return True
    except curses.error:
        return False


def format_hud(
    sensor: Any | None,
    width: int,
    total_counts: dict | None = None,
) -> list[str]:
    """Format HUD lines bounded by width."""
    if sensor is None:
        l1 = "── HUD: All Sensors Normal (Healthy) "
        l1 = (l1 + "─" * max(0, width - len(l1)))[:width]
        l2 = "   System operational | No anomalies detected"[:width]
        l3 = "   [Tab] Next Anomaly  [Arrows] Move Cursor  [r] Refresh  [q] Quit"[:width]
        return [l1, l2, l3]

    name = getattr(sensor, "name", "UNKNOWN")
    sev = get_sensor_severity(sensor)
    sev_name = SEVERITY_NAMES.get(sev, "UNKNOWN")
    hdr = f"── HUD: [{sev_name}] {name} "
    l1 = (hdr + "─" * max(0, width - len(hdr)))[:width]

    status = str(getattr(sensor, "status", "unknown")).upper()
    group = getattr(sensor, "group", ())
    group_str = " > ".join(group) if isinstance(group, (tuple, list)) else str(group)

    val = getattr(sensor, "value", None)
    val_str = f" | Value: {val}" if val is not None else ""
    unit = getattr(sensor, "unit", None)
    unit_str = f" {unit}" if unit else ""

    l2 = f"   Status: {status}{val_str}{unit_str} | Group: {group_str}"[:width]
    l3 = "   [Tab] Next Anomaly  [Arrows] Move Cursor  [r] Refresh  [q] Quit"[:width]
    return [l1, l2, l3]


class MatrixState:
    def __init__(self):
        self.cursor_index: int = 0
        self.auto_locked: bool = True
        self.preferred_mode: str | None = None


def render_matrix_view(
    win: Any,
    sensors: Sequence[Any],
    state: MatrixState | None = None,
    engine: MatrixLayoutEngine | None = None,
    color_map: dict[int, int] | None = None,
    use_unicode: bool = True,
) -> None:
    """Render high-density matrix view and HUD to a curses window."""
    max_y, max_x = win.getmaxyx()
    if engine is None:
        engine = MatrixLayoutEngine()
    if state is None:
        state = MatrixState()

    if engine.is_terminal_too_small(max_x, max_y):
        msg1 = f"Terminal too small ({max_x}x{max_y})"
        msg2 = "Minimum required: 80x22"
        safe_addstr(win, max(0, max_y // 2 - 1), max(0, (max_x - len(msg1)) // 2), msg1)
        safe_addstr(win, max(0, max_y // 2), max(0, (max_x - len(msg2)) // 2), msg2)
        return

    layout = engine.compute_layout(sensors, width=max_x, height=max_y, preferred_mode=state.preferred_mode)

    # Auto-lock to most severe anomaly if enabled
    if state.auto_locked and sensors:
        most_sev_idx, _ = find_most_severe_sensor(sensors, anomalies_only=True)
        if most_sev_idx is not None:
            state.cursor_index = most_sev_idx

    if sensors:
        state.cursor_index = max(0, min(len(sensors) - 1, state.cursor_index))
    else:
        state.cursor_index = 0

    # Render header
    tot = len(sensors)
    n_norm = sum(1 for s in sensors if get_sensor_severity(s) == SEVERITY_NORMAL)
    n_warn = sum(1 for s in sensors if get_sensor_severity(s) == SEVERITY_WARNING)
    n_crit = sum(1 for s in sensors if get_sensor_severity(s) == SEVERITY_CRITICAL)
    n_unav = sum(1 for s in sensors if get_sensor_severity(s) == SEVERITY_UNAVAILABLE)

    h0 = f" SENSOR MATRIX [{layout.mode.upper()}] "
    safe_addstr(win, 0, 0, (h0 + "=" * max(0, max_x - len(h0)))[:max_x])
    h1 = f" Total: {tot} | OK: {n_norm} | Warn: {n_warn} | Crit: {n_crit} | Unavail: {n_unav}"
    safe_addstr(win, 1, 0, h1[:max_x])

    # Render category labels
    if layout.mode == "blocks":
        for cat, (bx, by, bw, bh) in layout.blocks.items():
            safe_addstr(win, by, bx, f"[{cat}]"[:bw])
    elif layout.mode == "compact":
        for cat, (bx, by, bw, bh) in layout.blocks.items():
            if bh > 0:
                safe_addstr(win, by, bx, f"{cat}: "[:bw])

    # Render cells
    glyphs = GLYPHS_UNICODE if use_unicode else GLYPHS_ASCII
    for cell in layout.cells:
        ch = glyphs.get(cell.severity, ".")
        attr = 0
        if color_map and cell.severity in color_map:
            attr |= color_map[cell.severity]
        if cell.sensor_index == state.cursor_index:
            if hasattr(curses, "A_REVERSE"):
                attr |= curses.A_REVERSE
            if hasattr(curses, "A_BOLD"):
                attr |= curses.A_BOLD
        safe_addstr(win, cell.y, cell.x, ch, attr)

    # Render HUD
    selected_sensor = sensors[state.cursor_index] if (sensors and state.cursor_index < len(sensors)) else None
    hud_lines = format_hud(selected_sensor, width=max_x)
    hud_y = layout.hud_rect[1]
    for i, line in enumerate(hud_lines):
        safe_addstr(win, hud_y + i, 0, line)
