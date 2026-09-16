import argparse
import math
import os
import queue
import re
import sys
import threading
import time
from typing import Any

from sensor_overview.collector import CollectionError, Collector
from sensor_overview.model import Event, Sensor, Tracker

CONTROL_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")


def strip_control_codes(s: str) -> str:
    """Remove ANSI escape sequences, line breaks, tabs, and all non-printable Unicode characters."""
    if not s:
        return ""
    cleaned = CONTROL_RE.sub("", s)
    return "".join(ch for ch in cleaned if ch.isprintable() and ch not in ("\r", "\n", "\t"))


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Yosemite4 Sensor Dynamic Health Overview",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    src_group = parser.add_mutually_exclusive_group(required=True)
    src_group.add_argument("--demo", action="store_true", help="Run in synthetic demo mode")
    src_group.add_argument("--host", type=str, help="SSH target BMC (e.g. root@192.0.2.1)")
    src_group.add_argument("--file", type=str, help="Path to sensor JSON file")
    src_group.add_argument("--local", action="store_true", help="Run locally using mfg-tool")

    parser.add_argument("--interval", type=float, default=2.0, help="Polling interval in seconds")
    parser.add_argument("--timeout", type=float, default=15.0, help="Query timeout in seconds")
    parser.add_argument("--stale-after", type=float, default=10.0, help="Stale threshold in seconds")
    parser.add_argument("--port", type=int, default=22, help="SSH port")
    parser.add_argument("--identity", type=str, default=None, help="SSH identity key file")
    parser.add_argument("--once", action="store_true", help="Print single snapshot to stdout and exit")
    return parser


def validate_args(args: argparse.Namespace) -> str | None:
    if math.isnan(args.interval) or math.isinf(args.interval) or args.interval <= 0:
        return "--interval must be a finite positive number"
    if math.isnan(args.timeout) or math.isinf(args.timeout) or args.timeout <= 0:
        return "--timeout must be a finite positive number"
    if math.isnan(args.stale_after) or math.isinf(args.stale_after) or args.stale_after <= 0:
        return "--stale-after must be a finite positive number"
    if args.port < 1 or args.port > 65535:
        return "--port must be between 1 and 65535"
    return None


def run_once(collector: Collector, tracker: Tracker, source_label: str) -> int:
    stop_event = threading.Event()
    now = time.monotonic()
    try:
        raw_text = collector.collect(stop_event)
        if not raw_text or not raw_text.strip():
            sys.stderr.write("Error: Empty snapshot\n")
            return 1
        tracker.ingest(raw_text, now)
    except CollectionError as ce:
        sys.stderr.write(f"Collection error: {strip_control_codes(str(ce))}\n")
        return 1
    except ValueError as ve:
        sys.stderr.write(f"Invalid snapshot data: {strip_control_codes(str(ve))}\n")
        return 1
    except Exception as e:
        sys.stderr.write(f"Error: {strip_control_codes(str(e))}\n")
        return 1

    sensors = tracker.view(now)
    total_cnt = len(sensors)
    norm_cnt = sum(1 for s in sensors if s.health == "normal")
    att_cnt = sum(1 for s in sensors if s.health == "attention")
    unk_cnt = sum(1 for s in sensors if s.health == "unknown")

    clean_source = strip_control_codes(source_label)
    print(f"[Source: {clean_source}] [Age: 0.0s]")
    print(f"Total: {total_cnt} | Normal: {norm_cnt} (O) | Attention: {att_cnt} (!) | Unknown: {unk_cnt} (?)")
    print("-" * 60)

    groups: dict[tuple[str, ...], list[Sensor]] = {}
    for s in sensors:
        grp = s.group if s.group else ("Unclassified",)
        groups.setdefault(grp, []).append(s)

    for grp_key in sorted(groups.keys()):
        grp_name = strip_control_codes("/".join(grp_key))
        s_list = sorted(groups[grp_key], key=lambda x: x.name)
        att_in_grp = sum(1 for s in s_list if s.health == "attention")
        unk_in_grp = sum(1 for s in s_list if s.health == "unknown")
        cnt_info = []
        if att_in_grp > 0:
            cnt_info.append(f"{att_in_grp} attention")
        if unk_in_grp > 0:
            cnt_info.append(f"{unk_in_grp} unknown")
        info_str = f" ({', '.join(cnt_info)})" if cnt_info else ""
        print(f"[{grp_name}] ({len(s_list)} sensors){info_str}:")
        for s in s_list:
            symbol = "O" if s.health == "normal" else ("!" if s.health == "attention" else "?")
            c_name = strip_control_codes(s.name)
            c_status = strip_control_codes(s.status)
            c_health = strip_control_codes(s.health)
            print(f"  [{symbol}] {c_name}: {c_status} ({c_health})")

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = create_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2

    err = validate_args(args)
    if err:
        sys.stderr.write(f"Error: {err}\n")
        return 2

    # Check TTY and TERM when not in --once mode
    if not args.once:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            sys.stderr.write("Error: Interactive TUI requires a TTY terminal. Use --once for non-interactive output.\n")
            return 2
        term = os.environ.get("TERM", "").strip()
        if not term or term == "dumb":
            sys.stderr.write("Error: TERM environment variable is not set or unsupported. Use --once for non-interactive output.\n")
            return 2

    # Determine source label
    if args.demo:
        source_label = "DEMO"
    elif args.host:
        source_label = f"SSH:{args.host}"
    elif args.file:
        source_label = f"FILE:{args.file}"
    elif args.local:
        source_label = "LOCAL"
    else:
        source_label = "UNKNOWN"

    try:
        collector = Collector(
            host=args.host,
            file=args.file,
            demo=args.demo,
            local=args.local,
            port=args.port,
            identity=args.identity,
            timeout=args.timeout,
        )
    except ValueError as ve:
        sys.stderr.write(f"Error: {strip_control_codes(str(ve))}\n")
        return 2

    try:
        tracker = Tracker(stale_after=args.stale_after)
    except ValueError as ve:
        sys.stderr.write(f"Error: {strip_control_codes(str(ve))}\n")
        return 2

    if args.once:
        return run_once(collector, tracker, source_label)

    # Interactive TUI mode (curses imported only here)
    try:
        import curses
        from sensor_overview.ui import run_tui
    except (ImportError, Exception) as e:
        sys.stderr.write(f"Error: Curses UI is not available: {strip_control_codes(str(e))}\n")
        return 1

    stop_event = threading.Event()
    col_queue: queue.Queue = queue.Queue()

    def background_worker():
        while not stop_event.is_set():
            started = time.monotonic()
            raw_text = None
            err_msg = None
            try:
                raw_text = collector.collect(stop_event)
            except CollectionError as ce:
                err_msg = str(ce)
            except Exception as e:
                err_msg = str(e)
            finished_at = time.monotonic()
            duration = finished_at - started
            if not stop_event.is_set():
                col_queue.put((raw_text, err_msg, duration, finished_at))
            delay = max(0.2, args.interval - duration)
            stop_event.wait(delay)

    worker_thread = threading.Thread(target=background_worker, daemon=True)
    try:
        worker_thread.start()
        run_tui(tracker, col_queue, stop_event, source_label, args.interval)
    except curses.error as ce:
        sys.stderr.write(f"Terminal error: {strip_control_codes(str(ce))}\n")
        return 1
    except Exception as e:
        sys.stderr.write(f"TUI error: {strip_control_codes(str(e))}\n")
        return 1
    finally:
        stop_event.set()
        if worker_thread.is_alive():
            worker_thread.join(timeout=1.5)

    return 0
