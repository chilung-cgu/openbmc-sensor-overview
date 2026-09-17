import curses
import math
import queue
import re
import threading
import time
from dataclasses import dataclass
from sensor_overview.matrix_view import (
    GLYPHS_ASCII,
    GLYPHS_UNICODE,
    MatrixLayoutEngine,
    MatrixState,
    SEVERITY_CRITICAL,
    SEVERITY_NORMAL,
    SEVERITY_UNAVAILABLE,
    SEVERITY_WARNING,
    find_most_severe_sensor,
    find_next_abnormal_index,
    navigate_cursor,
    render_matrix_view,
)

from typing import Any, Iterable

CONTROL_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")


def strip_control_codes(s: str) -> str:
    """Remove ANSI escape sequences, line breaks, tabs, and all non-printable Unicode characters."""
    if not s:
        return ""
    cleaned = CONTROL_RE.sub("", s)
    return "".join(ch for ch in cleaned if ch.isprintable() and ch not in ("\r", "\n", "\t"))


@dataclass
class TreeNode:
    name: str
    group_path: tuple[str, ...]
    is_group: bool
    sensor: Any = None
    children: list['TreeNode'] = None
    expanded: bool = True
    total_count: int = 0
    normal_count: int = 0
    attention_count: int = 0
    unknown_count: int = 0

    def format_label(self) -> str:
        if not self.is_group:
            status_char = "O" if self.sensor.health == "normal" else ("!" if self.sensor.health == "attention" else "?")
            return f"[{status_char}] {self.name}: {self.sensor.status}"
        exp_marker = "[-]" if self.expanded else "[+]"
        # Group summary: show attention and unknown counts
        counts = []
        if self.attention_count > 0:
            counts.append(f"{self.attention_count} !")
        if self.unknown_count > 0:
            counts.append(f"{self.unknown_count} ?")
        if not counts:
            counts.append(f"{self.normal_count} O")
        count_str = ", ".join(counts)
        return f"{exp_marker} {self.name} ({self.total_count} sensors: {count_str})"


@dataclass
class RenderRow:
    node: TreeNode
    depth: int
    name: str
    is_group: bool
    sensor: Any = None


class _IntermediateGroup:
    def __init__(self, name: str, group_path: tuple[str, ...]):
        self.name = name
        self.group_path = group_path
        self.subgroups: dict[str, _IntermediateGroup] = {}
        self.sensors: list[Any] = []


def _natural_sort_key(s: str) -> tuple:
    parts = [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", s)]
    return tuple(parts)


def build_tree(sensors: Iterable[Any], collapsed_groups: set[tuple[str, ...]] | None = None) -> list[TreeNode]:
    if collapsed_groups is None:
        collapsed_groups = set()

    # Build intermediate multi-level hierarchy: e.g. ("Slot 1", "Sentinel Dome")
    root_groups: dict[str, _IntermediateGroup] = {}

    for s in sensors:
        grp = s.group if (s.group and len(s.group) > 0) else ("Unclassified",)
        curr_dict = root_groups
        path_accum: list[str] = []
        curr_node: _IntermediateGroup | None = None

        for seg in grp:
            path_accum.append(seg)
            curr_path = tuple(path_accum)
            if seg not in curr_dict:
                curr_dict[seg] = _IntermediateGroup(name=seg, group_path=curr_path)
            curr_node = curr_dict[seg]
            curr_dict = curr_node.subgroups

        if curr_node is not None:
            curr_node.sensors.append(s)

    def convert_group(ig: _IntermediateGroup) -> TreeNode:
        # Recursively convert subgroups
        sorted_subgroups = sorted(ig.subgroups.values(), key=lambda x: _natural_sort_key(x.name))
        child_nodes: list[TreeNode] = [convert_group(sub) for sub in sorted_subgroups]

        # Add sensor leaves
        sorted_sensors = sorted(ig.sensors, key=lambda x: x.name)
        for s in sorted_sensors:
            is_norm = (s.health == "normal")
            is_att = (s.health == "attention")
            is_unk = not is_norm and not is_att
            child_nodes.append(
                TreeNode(
                    name=s.name,
                    group_path=ig.group_path,
                    is_group=False,
                    sensor=s,
                    children=[],
                    expanded=True,
                    total_count=1,
                    normal_count=1 if is_norm else 0,
                    attention_count=1 if is_att else 0,
                    unknown_count=1 if is_unk else 0,
                )
            )

        # Parent aggregates all descendant counts
        total = sum(c.total_count for c in child_nodes)
        norm = sum(c.normal_count for c in child_nodes)
        att = sum(c.attention_count for c in child_nodes)
        unk = sum(c.unknown_count for c in child_nodes)
        expanded = (ig.group_path not in collapsed_groups)

        return TreeNode(
            name=ig.name,
            group_path=ig.group_path,
            is_group=True,
            sensor=None,
            children=child_nodes,
            expanded=expanded,
            total_count=total,
            normal_count=norm,
            attention_count=att,
            unknown_count=unk,
        )

    sorted_roots = sorted(root_groups.values(), key=lambda x: _natural_sort_key(x.name))
    return [convert_group(r) for r in sorted_roots]


def flatten_tree(
    tree: list[TreeNode],
    filter_attention: bool = False,
    search_query: str = "",
) -> list[RenderRow]:
    query = search_query.strip().lower()
    rows: list[RenderRow] = []

    def _matches_query(node: TreeNode) -> bool:
        if not query:
            return True
        if query in node.name.lower():
            return True
        if node.is_group and node.children:
            return any(_matches_query(child) for child in node.children)
        return False

    def _collect(node: TreeNode, depth: int) -> None:
        if node.is_group:
            # Filter non-normal (attention, unknown, missing, stale)
            if filter_attention and (node.attention_count + node.unknown_count) == 0:
                return
            if query and not _matches_query(node):
                return

            rows.append(RenderRow(node=node, depth=depth, name=node.name, is_group=True, sensor=None))
            if node.expanded and node.children:
                for child in node.children:
                    _collect(child, depth + 1)
        else:
            if filter_attention and node.sensor.health == "normal":
                return
            if query and query not in node.name.lower():
                return
            rows.append(RenderRow(node=node, depth=depth, name=node.name, is_group=False, sensor=node.sensor))

    for root_node in tree:
        _collect(root_node, depth=0)

    return rows


def format_header(
    source_label: str,
    conn_status: str,
    age: float | None,
    duration: float | None,
    stale_after: float,
    total: int,
    norm: int,
    att: int,
    unk: int,
    max_width: int = 120,
) -> tuple[str, str]:
    if age is None:
        age_str = "--"
    elif age > stale_after:
        age_str = f"STALE ({age:.1f}s)"
    else:
        age_str = f"{age:.1f}s"

    dur_str = f"{duration:.2f}s" if duration is not None else "--"
    right_meta = f" [Conn: {conn_status}] [Age: {age_str}] [Query: {dur_str}]"

    # Ensure Age and Query remain visible even with long source
    prefix = "[Source: "
    suffix = "]"
    avail_src_len = max_width - len(right_meta) - len(prefix) - len(suffix)

    if avail_src_len <= 3:
        clean_src = "..."
    elif len(source_label) > avail_src_len:
        clean_src = "..." + source_label[-(avail_src_len - 3):]
    else:
        clean_src = source_label

    top = f"{prefix}{clean_src}{suffix}{right_meta}"
    counts = f"Total: {total} | Normal: {norm} (O) | Attention: {att} (!) | Unknown: {unk} (?)"
    return strip_control_codes(top), strip_control_codes(counts)


def format_selected_detail(row: RenderRow | None, max_width: int) -> tuple[str, str]:
    if row is None:
        return ("No sensors match current filter", "")

    if row.is_group:
        g = row.node
        path_str = "/".join(g.group_path)
        l1 = f"Group: {g.name} (Path: {path_str})"
        l2 = f"Counts: {g.total_count} sensors (Normal: {g.normal_count}, Attention: {g.attention_count}, Unknown: {g.unknown_count})"
        return (strip_control_codes(l1)[:max_width], strip_control_codes(l2)[:max_width])

    s = row.sensor
    grp_str = "/".join(s.group) if s.group else "Unclassified"
    name_prefix = f"Sensor: {s.name}"
    status_suffix = f"Status: {s.status} | Health: {s.health} | Raw: {s.raw_status} | Group: {grp_str}"

    if len(name_prefix) <= max_width:
        l1 = name_prefix
        l2 = status_suffix[:max_width]
    else:
        l1 = name_prefix[:max_width]
        rem_name = name_prefix[max_width:]
        l2 = f"...{rem_name} | {status_suffix}"[:max_width]

    return (strip_control_codes(l1)[:max_width], strip_control_codes(l2)[:max_width])


class TUIState:
    def __init__(self, initial_view: str = "matrix"):
        self.view_mode: str = initial_view  # "matrix" or "tree"
        self.matrix_state: MatrixState = MatrixState()
        self.matrix_engine: MatrixLayoutEngine = MatrixLayoutEngine()
        self.collapsed_groups: set[tuple[str, ...]] = set()
        self.selected_index: int = 0
        self.scroll_offset: int = 0
        self.filter_attention: bool = False
        self.search_active: bool = False
        self.search_query: str = ""
        self.search_buffer: str = ""


def run_tui(
    tracker: Any,
    collector_queue: queue.Queue,
    stop_event: threading.Event,
    source_label: str,
    interval: float,
    initial_view: str = "matrix",
) -> None:
    curses.wrapper(lambda stdscr: _tui_main(stdscr, tracker, collector_queue, stop_event, source_label, interval, initial_view))


def _tui_main(
    stdscr: Any,
    tracker: Any,
    collector_queue: queue.Queue,
    stop_event: threading.Event,
    source_label: str,
    interval: float,
    initial_view: str = "matrix",
) -> None:
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    stdscr.timeout(100)  # 0.1s non-blocking tick

    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_RED, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_CYAN, -1)
        curses.init_pair(5, curses.COLOR_BLACK, curses.COLOR_WHITE)
        curses.init_pair(6, curses.COLOR_RED, curses.COLOR_WHITE)
        curses.init_pair(7, curses.COLOR_YELLOW, curses.COLOR_WHITE)

    color_map = {
        SEVERITY_NORMAL: curses.color_pair(1),
        SEVERITY_UNAVAILABLE: curses.color_pair(3) | curses.A_DIM,
        SEVERITY_WARNING: curses.color_pair(3) | curses.A_BOLD,
        SEVERITY_CRITICAL: curses.color_pair(2) | curses.A_BOLD,
    } if curses.has_colors() else None

    state = TUIState(initial_view=initial_view)
    conn_status = "Initializing..."
    last_duration: float | None = None

    while not stop_event.is_set():
        while not collector_queue.empty():
            try:
                raw_text, err, duration, finished_at = collector_queue.get_nowait()
                last_duration = duration
                if err:
                    tracker.fail(err, finished_at)
                    conn_status = f"ERROR: {strip_control_codes(err)[:30]}"
                elif raw_text is not None:
                    if not raw_text.strip():
                        tracker.fail("Empty snapshot", finished_at)
                        conn_status = "ERROR: Empty snapshot"
                    else:
                        try:
                            tracker.ingest(raw_text, finished_at)
                            conn_status = "Connected"
                        except ValueError as ve:
                            tracker.fail(f"Invalid snapshot: {ve}", finished_at)
                            conn_status = f"Parse error: {strip_control_codes(str(ve))[:20]}"
            except queue.Empty:
                break

        now = time.monotonic()
        sensors = tracker.view(now)

        if tracker.last_error:
            conn_status = f"ERROR: {strip_control_codes(tracker.last_error)[:30]}"
        elif tracker.last_success is not None:
            conn_status = "Connected"

        age = (now - tracker.last_success) if tracker.last_success is not None else None

        total_cnt = len(sensors)
        norm_cnt = sum(1 for s in sensors if s.health == "normal")
        att_cnt = sum(1 for s in sensors if s.health in ("attention", "warning", "critical"))
        unk_cnt = total_cnt - norm_cnt - att_cnt

        tree = build_tree(sensors, collapsed_groups=state.collapsed_groups)
        effective_query = state.search_buffer if state.search_active else state.search_query
        rows = flatten_tree(
            tree,
            filter_attention=state.filter_attention,
            search_query=effective_query,
        )

        max_y, max_x = stdscr.getmaxyx()
        stdscr.erase()

        if max_y < 15 or max_x < 60:
            msg1 = f"Terminal too small ({max_x}x{max_y})"
            msg2 = "Minimum required size: 60x15"
            msg3 = "Press 'q' to quit"
            try:
                stdscr.addstr(max(0, max_y // 2 - 1), max(0, (max_x - len(msg1)) // 2), msg1, curses.A_BOLD)
                stdscr.addstr(max(0, max_y // 2), max(0, (max_x - len(msg2)) // 2), msg2)
                stdscr.addstr(max(0, max_y // 2 + 1), max(0, (max_x - len(msg3)) // 2), msg3)
            except curses.error:
                pass
            stdscr.refresh()
            ch = stdscr.getch()
            if ch in (ord('q'), ord('Q')):
                stop_event.set()
                break
            continue

        if state.view_mode == "matrix":
            render_matrix_view(
                stdscr,
                sensors,
                state=state.matrix_state,
                engine=state.matrix_engine,
                color_map=color_map,
                use_unicode=True,
                source_label=source_label,
                conn_status=conn_status,
                age=age,
                last_duration=last_duration,
                stale_after=tracker.stale_after,
            )
        else:
            # Header lines (0, 1, 2)
            top_hdr, cnt_hdr = format_header(
                source_label,
                conn_status,
                age,
                last_duration,
                tracker.stale_after,
                total_cnt,
                norm_cnt,
                att_cnt,
                unk_cnt,
                max_width=max_x - 1,
            )
            try:
                stdscr.addstr(0, 0, top_hdr[:max_x - 1], curses.A_BOLD | (curses.color_pair(4) if curses.has_colors() else 0))
                stdscr.addstr(1, 0, cnt_hdr[:max_x - 1])
                stdscr.addstr(2, 0, "-" * (max_x - 1))
            except curses.error:
                pass
    
            # Calculate section budgets
            # Header: 3 lines (0, 1, 2)
            # Footer: 4 lines (separator, 2 detail lines, command bar)
            # Issues summary: 2-3 lines
            # Recent events: 2-3 lines if max_y >= 22
            issues_lines = 3 if max_y >= 20 else 2
            events_lines = (3 if max_y >= 25 else 2) if max_y >= 22 else 0
            footer_height = 4
    
            list_top = 3
            list_height = max(1, max_y - list_top - footer_height - issues_lines - (events_lines + 1 if events_lines > 0 else 0))
    
            if not rows:
                state.selected_index = 0
                state.scroll_offset = 0
            else:
                state.selected_index = max(0, min(state.selected_index, len(rows) - 1))
                if state.selected_index < state.scroll_offset:
                    state.scroll_offset = state.selected_index
                elif state.selected_index >= state.scroll_offset + list_height:
                    state.scroll_offset = state.selected_index - list_height + 1
    
            # Render Tree rows
            for idx in range(list_height):
                row_idx = state.scroll_offset + idx
                if row_idx >= len(rows):
                    break
                row = rows[row_idx]
                is_selected = (row_idx == state.selected_index)
                line_y = list_top + idx
                indent = "  " * row.depth
    
                if row.is_group:
                    text_line = indent + row.node.format_label()
                    # Aggregate health color for parent groups
                    if is_selected:
                        if row.node.attention_count > 0:
                            attr = curses.color_pair(6) | curses.A_BOLD if curses.has_colors() else curses.A_REVERSE
                        elif row.node.unknown_count > 0:
                            attr = curses.color_pair(7) | curses.A_BOLD if curses.has_colors() else curses.A_REVERSE
                        else:
                            attr = curses.color_pair(5) | curses.A_BOLD if curses.has_colors() else curses.A_REVERSE
                    else:
                        if row.node.attention_count > 0:
                            attr = curses.color_pair(2) | curses.A_BOLD if curses.has_colors() else curses.A_BOLD
                        elif row.node.unknown_count > 0:
                            attr = curses.color_pair(3) | curses.A_BOLD if curses.has_colors() else 0
                        else:
                            attr = curses.color_pair(1) if curses.has_colors() else 0
    
                    text_line = strip_control_codes(text_line)[:max_x - 1]
                    try:
                        stdscr.addstr(line_y, 0, text_line.ljust(max_x - 1), attr)
                    except curses.error:
                        pass
                else:
                    sensor = row.sensor
                    status_char = "O" if sensor.health == "normal" else ("!" if sensor.health == "attention" else "?")
                    prefix = f"{indent}[{status_char}] {row.name}"
                    suffix = f" {sensor.status}"
                    dots_len = max(2, max_x - 1 - len(prefix) - len(suffix))
                    line_str = prefix + ("." * dots_len) + suffix
                    line_str = strip_control_codes(line_str)[:max_x - 1]
    
                    if is_selected:
                        attr = curses.color_pair(6 if sensor.health == "attention" else 5) if curses.has_colors() else curses.A_REVERSE
                    else:
                        if sensor.health == "normal":
                            attr = curses.color_pair(1) if curses.has_colors() else 0
                        elif sensor.health == "attention":
                            attr = curses.color_pair(2) | curses.A_BOLD if curses.has_colors() else curses.A_BOLD
                        else:
                            attr = curses.color_pair(3) if curses.has_colors() else 0
    
                    try:
                        stdscr.addstr(line_y, 0, line_str.ljust(max_x - 1), attr)
                    except curses.error:
                        pass
    
            cur_y = list_top + list_height
    
            # Fixed Visible Issues Summary (independent of tree viewport)
            non_normal_sensors = [s for s in sensors if s.health != "normal"]
            try:
                stdscr.addstr(cur_y, 0, "-" * (max_x - 1))
                cur_y += 1
                if not non_normal_sensors:
                    issue_header = "Active Issues: (None - all sensors normal)"
                    stdscr.addstr(cur_y, 0, issue_header[:max_x - 1], curses.color_pair(1) if curses.has_colors() else curses.A_DIM)
                    cur_y += 1
                else:
                    issue_header = f"Active Issues ({len(non_normal_sensors)} non-normal):"
                    stdscr.addstr(cur_y, 0, issue_header[:max_x - 1], curses.color_pair(2) | curses.A_BOLD if curses.has_colors() else curses.A_BOLD)
                    cur_y += 1
    
                    max_items = issues_lines - 1
                    for i in range(min(max_items, len(non_normal_sensors))):
                        if cur_y >= max_y - footer_height - (events_lines + 1 if events_lines > 0 else 0):
                            break
                        iss = non_normal_sensors[i]
                        sym = "!" if iss.health == "attention" else "?"
                        more_str = f" (+{len(non_normal_sensors) - i - 1} more)" if (i == max_items - 1 and len(non_normal_sensors) > max_items) else ""
                        iss_line = f"  [{sym}] {iss.name}: {iss.status} ({iss.health}){more_str}"
                        attr = curses.color_pair(2 if iss.health == "attention" else 3) if curses.has_colors() else 0
                        stdscr.addstr(cur_y, 0, strip_control_codes(iss_line)[:max_x - 1], attr)
                        cur_y += 1
            except curses.error:
                pass
    
            # Recent Events Section (if height permits)
            if events_lines > 0 and cur_y < max_y - footer_height:
                try:
                    stdscr.addstr(cur_y, 0, "-" * (max_x - 1))
                    cur_y += 1
                    recent_events = list(tracker.events)[-(events_lines - 1):]
                    if not recent_events:
                        stdscr.addstr(cur_y, 0, "Recent Events: (None)"[:max_x - 1], curses.A_DIM)
                        cur_y += 1
                    else:
                        for ev in recent_events:
                            if cur_y >= max_y - footer_height:
                                break
                            rel_sec = now - ev.at
                            ev_line = f"  [-{rel_sec:.1f}s] {ev.name}: {ev.before} -> {ev.after}"
                            stdscr.addstr(cur_y, 0, strip_control_codes(ev_line)[:max_x - 1], curses.A_DIM)
                            cur_y += 1
                except curses.error:
                    pass
    
            # Footer Area: Separator, 2-line detail, command line
            footer_y = max_y - 3
            try:
                stdscr.addstr(footer_y - 1, 0, "-" * (max_x - 1))
    
                sel_row = rows[state.selected_index] if (rows and 0 <= state.selected_index < len(rows)) else None
                det_l1, det_l2 = format_selected_detail(sel_row, max_x - 1)
    
                stdscr.addstr(footer_y, 0, det_l1, curses.A_BOLD)
                stdscr.addstr(footer_y + 1, 0, det_l2)
    
                cmd_y = max_y - 1
                if state.search_active:
                    search_bar = f"Search: {state.search_buffer}_ (Enter: apply, Esc: cancel)"
                    stdscr.addstr(cmd_y, 0, strip_control_codes(search_bar)[:max_x - 1], curses.A_REVERSE)
                else:
                    filter_hint = " [FILTER: NON-NORMAL]" if state.filter_attention else ""
                    search_hint = f" [SEARCH: {state.search_query}]" if state.search_query else ""
                    bar = f"[q]uit  [a]ttention  [/]search  [Enter/Space]toggle  [j/k]move{filter_hint}{search_hint}"
                    stdscr.addstr(cmd_y, 0, strip_control_codes(bar)[:max_x - 1])
            except curses.error:
                pass
    
        stdscr.refresh()

        try:
            ch = stdscr.getch()
        except curses.error:
            ch = -1

        if ch == -1:
            continue

        if ch == curses.KEY_RESIZE:
            try:
                curses.update_lines_cols()
                stdscr.clear()
            except curses.error:
                pass
            continue

        if state.search_active:
            if ch == 27:  # Esc
                state.search_active = False
                state.search_buffer = ""
            elif ch in (curses.KEY_ENTER, 10, 13):
                state.search_active = False
                state.search_query = state.search_buffer
                state.search_buffer = ""
                state.selected_index = 0
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                state.search_buffer = state.search_buffer[:-1]
            elif 32 <= ch <= 126:
                state.search_buffer += chr(ch)
            continue

        if state.view_mode == "matrix":
            if ch in (ord("q"), ord("Q")):
                stop_event.set()
                break
            elif ch in (ord("m"), ord("M"), ord("v"), ord("V")):
                state.view_mode = "tree"
                stdscr.clear()
            elif ch in (ord("	"), 9):
                state.matrix_state.auto_locked = False
                nxt = find_next_abnormal_index(sensors, state.matrix_state.cursor_index)
                if nxt is not None:
                    state.matrix_state.cursor_index = nxt
            elif ch in (curses.KEY_BTAB,):
                state.matrix_state.auto_locked = False
                nxt = find_next_abnormal_index(sensors, state.matrix_state.cursor_index, reverse=True)
                if nxt is not None:
                    state.matrix_state.cursor_index = nxt
            elif ch in (curses.KEY_LEFT, ord("h"), ord("H")):
                state.matrix_state.auto_locked = False
                layout = state.matrix_engine.compute_layout(sensors, max_x, max_y, preferred_mode=state.matrix_state.preferred_mode)
                state.matrix_state.cursor_index = navigate_cursor("LEFT", state.matrix_state.cursor_index, layout)
            elif ch in (curses.KEY_RIGHT, ord("l"), ord("L")):
                state.matrix_state.auto_locked = False
                layout = state.matrix_engine.compute_layout(sensors, max_x, max_y, preferred_mode=state.matrix_state.preferred_mode)
                state.matrix_state.cursor_index = navigate_cursor("RIGHT", state.matrix_state.cursor_index, layout)
            elif ch in (curses.KEY_UP, ord("k"), ord("K")):
                state.matrix_state.auto_locked = False
                layout = state.matrix_engine.compute_layout(sensors, max_x, max_y, preferred_mode=state.matrix_state.preferred_mode)
                state.matrix_state.cursor_index = navigate_cursor("UP", state.matrix_state.cursor_index, layout)
            elif ch in (curses.KEY_DOWN, ord("j"), ord("J")):
                state.matrix_state.auto_locked = False
                layout = state.matrix_engine.compute_layout(sensors, max_x, max_y, preferred_mode=state.matrix_state.preferred_mode)
                state.matrix_state.cursor_index = navigate_cursor("DOWN", state.matrix_state.cursor_index, layout)
            elif ch in (ord("a"), ord("A")):
                state.matrix_state.auto_locked = not state.matrix_state.auto_locked
                if state.matrix_state.auto_locked:
                    most_sev_idx, _ = find_most_severe_sensor(sensors, anomalies_only=True)
                    if most_sev_idx is not None:
                        state.matrix_state.cursor_index = most_sev_idx
            elif ch in (ord("p"), ord("P")):
                if state.matrix_state.preferred_mode == "blocks":
                    state.matrix_state.preferred_mode = "compact"
                elif state.matrix_state.preferred_mode == "compact":
                    state.matrix_state.preferred_mode = None
                else:
                    state.matrix_state.preferred_mode = "blocks"
                stdscr.clear()
            continue

        # Tree View Keys
        if ch in (ord("m"), ord("M"), ord("v"), ord("V")):
            state.view_mode = "matrix"
            stdscr.clear()
            continue
        if ch in (ord('q'), ord('Q')):
            stop_event.set()
            break
        elif ch in (curses.KEY_UP, ord('k'), ord('K')):
            if rows:
                state.selected_index = max(0, state.selected_index - 1)
        elif ch in (curses.KEY_DOWN, ord('j'), ord('J')):
            if rows:
                state.selected_index = min(len(rows) - 1, state.selected_index + 1)
        elif ch in (curses.KEY_PPAGE,):
            if rows:
                state.selected_index = max(0, state.selected_index - list_height)
        elif ch in (curses.KEY_NPAGE,):
            if rows:
                state.selected_index = min(len(rows) - 1, state.selected_index + list_height)
        elif ch in (curses.KEY_HOME, ord('g')):
            state.selected_index = 0
        elif ch in (curses.KEY_END, ord('G')):
            if rows:
                state.selected_index = len(rows) - 1
        elif ch in (curses.KEY_ENTER, 10, 13, ord(' ')):
            if rows and 0 <= state.selected_index < len(rows):
                cur_node = rows[state.selected_index].node
                if cur_node.is_group:
                    if cur_node.group_path in state.collapsed_groups:
                        state.collapsed_groups.remove(cur_node.group_path)
                    else:
                        state.collapsed_groups.add(cur_node.group_path)
        elif ch in (curses.KEY_LEFT, ord('h')):
            if rows and 0 <= state.selected_index < len(rows):
                row = rows[state.selected_index]
                if row.is_group:
                    state.collapsed_groups.add(row.node.group_path)
                else:
                    for p_idx in range(state.selected_index - 1, -1, -1):
                        if rows[p_idx].is_group and rows[p_idx].node.group_path == row.node.group_path:
                            state.selected_index = p_idx
                            break
        elif ch in (curses.KEY_RIGHT, ord('l')):
            if rows and 0 <= state.selected_index < len(rows):
                row = rows[state.selected_index]
                if row.is_group:
                    state.collapsed_groups.discard(row.node.group_path)
        elif ch in (ord('a'), ord('A')):
            state.filter_attention = not state.filter_attention
            state.selected_index = 0
        elif ch == ord('/'):
            state.search_active = True
            state.search_buffer = state.search_query
        elif ch == 27:
            state.search_query = ""
            state.search_buffer = ""
            state.filter_attention = False
