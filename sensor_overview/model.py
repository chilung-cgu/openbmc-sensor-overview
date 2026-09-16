import json
import math
import re
from collections import deque
from dataclasses import dataclass
from typing import Any

__all__ = [
    "Sensor",
    "Event",
    "group_name",
    "parse_snapshot",
    "Tracker",
]


@dataclass(frozen=True)
class Sensor:
    name: str
    status: str       # effective status: ok/critical/.../missing/stale/invalid value
    health: str       # normal/attention/unknown
    group: tuple[str, ...]
    raw_status: str


@dataclass(frozen=True)
class Event:
    at: float        # time.monotonic(), UI 以相對秒顯示
    name: str
    before: str
    after: str


_SLOT_RE = re.compile(r'(SENTINEL_DOME|WAILUA_FALLS)_SLOT_(\d+)_', re.IGNORECASE)
_NAMED_RE = re.compile(r'^(MGNT|FANBOARD\d*|MEDUSA|SPIDER)(?:_|$)', re.IGNORECASE)
_VIRTUAL_FANBOARD_RE = re.compile(r'^VIRTUAL_(FANBOARD\d*)_', re.IGNORECASE)
_NIC_RE = re.compile(r'^(?:VIRTUAL_)?NIC\d*(?:_|$)', re.IGNORECASE)
_BMC_METRICS_RE = re.compile(r'^bmc/', re.IGNORECASE)
_SYSTEM_RE = re.compile(r'^SYSTEM_', re.IGNORECASE)


def group_name(name: str) -> tuple[str, ...]:
    clean = name
    if clean.startswith("CALIBRATED_"):
        clean = clean[len("CALIBRATED_"):]

    m_slot = _SLOT_RE.search(clean)
    if m_slot:
        board_name = "Sentinel Dome" if m_slot.group(1).upper() == "SENTINEL_DOME" else "Wailua Falls"
        return (f"Slot {m_slot.group(2)}", board_name)

    # VIRTUAL_FANBOARD0_* -> group under FANBOARD0
    m_vfb = _VIRTUAL_FANBOARD_RE.match(clean)
    if m_vfb:
        return (m_vfb.group(1).upper(),)

    # NIC0_TEMP_C, VIRTUAL_NIC0_TEMP_C -> NIC
    if _NIC_RE.match(clean):
        return ("NIC",)

    # bmc/cpu/*, bmc/memory/*, bmc/storage/* -> BMC metrics
    if _BMC_METRICS_RE.match(name):
        return ("BMC metrics",)

    # SYSTEM_AIRFLOW_CFM, SYSTEM_PLATFORM_INPUT_PWR_W -> System
    if _SYSTEM_RE.match(clean):
        return ("System",)

    # SENTINEL_DOME_SLOT_PRESENT_PERCENTAGE (no slot number) -> System
    if re.match(r'^SENTINEL_DOME_SLOT_PRESENT_', clean, re.IGNORECASE) and not _SLOT_RE.search(clean):
        return ("System",)

    m_named = _NAMED_RE.match(clean)
    if m_named:
        return (m_named.group(1).upper(),)

    if clean.strip().lower() in ("cpu", "memory"):
        return ("BMC metrics",)

    # Named board temp sensors: MEDUSA0_TEMP_C, SPIDER0_TEMP_C
    if re.match(r'^(MEDUSA|SPIDER)\d+(?:_|$)', clean, re.IGNORECASE):
        return (re.match(r'^(MEDUSA|SPIDER)', clean, re.IGNORECASE).group(1).upper(),)

    return ("Unclassified",)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    res: dict[str, Any] = {}
    for k, v in pairs:
        if k in res:
            raise ValueError(f"Duplicate JSON key: {k}")
        res[k] = v
    return res


def _is_valid_finite(val: Any) -> bool:
    if val is None or isinstance(val, bool) or not isinstance(val, (int, float)):
        return False
    try:
        return math.isfinite(val)
    except OverflowError:
        return False


def parse_snapshot(text: str) -> dict[str, Sensor]:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Snapshot text must not be empty")

    try:
        data = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (ValueError, RecursionError) as e:
        raise ValueError(f"Invalid JSON snapshot: {e}") from e

    if not isinstance(data, dict):
        raise ValueError("Top-level snapshot must be a JSON object")

    if not data:
        raise ValueError("Snapshot must not be empty")

    result: dict[str, Sensor] = {}
    for name, item in data.items():
        if not isinstance(item, dict):
            raise ValueError(f"Sensor entry {name} must be a dict")

        if "status" in item and item["status"] is not None:
            raw_st = str(item["status"])
            st = raw_st.strip().lower()
        else:
            raw_st = "unknown"
            st = "unknown"

        val = item.get("value")

        if st == "ok":
            if _is_valid_finite(val):
                health = "normal"
                eff_st = "ok"
            else:
                health = "unknown"
                eff_st = "invalid value"
        elif st in {"warning", "critical", "high", "low"}:
            health = "attention"
            eff_st = st
        else:
            health = "unknown"
            eff_st = st

        result[name] = Sensor(
            name=name,
            status=eff_st,
            health=health,
            group=group_name(name),
            raw_status=raw_st,
        )

    return result


class Tracker:
    def __init__(self, stale_after: float = 10.0):
        self.stale_after = stale_after
        self.last_success: float | None = None
        self.last_error: str | None = None
        self.events: deque[Event] = deque(maxlen=100)
        self._sensors: dict[str, Sensor] = {}
        self._first = True

    def ingest(self, text: str, now: float) -> None:
        parsed = parse_snapshot(text)

        # Recover from previous collector failure if needed
        if self.last_error is not None:
            self.events.append(Event(at=now, name="COLLECTOR", before=self.last_error, after="ok"))
            self.last_error = None

        new_sensors: dict[str, Sensor] = dict(parsed)

        # Tombstones for disappeared sensors
        for old_name, old_s in self._sensors.items():
            if old_name not in parsed:
                if old_s.status != "missing":
                    self.events.append(Event(at=now, name=old_name, before=old_s.status, after="missing"))
                new_sensors[old_name] = Sensor(
                    name=old_name,
                    status="missing",
                    health="unknown",
                    group=old_s.group,
                    raw_status=old_s.raw_status,
                )

        # Status transition events (suppressed on initial snapshot)
        if not self._first:
            for name, new_s in parsed.items():
                if name in self._sensors:
                    old_s = self._sensors[name]
                    if old_s.status != new_s.status:
                        self.events.append(Event(at=now, name=name, before=old_s.status, after=new_s.status))
                else:
                    self.events.append(Event(at=now, name=name, before="new", after=new_s.status))

        self._first = False
        self._sensors = new_sensors
        self.last_success = now

    def fail(self, error: str, now: float) -> None:
        if self.last_error != error:
            self.events.append(Event(at=now, name="COLLECTOR", before=str(self.last_error or "ok"), after=error))
        self.last_error = error

    def view(self, now: float) -> list[Sensor]:
        is_stale = (self.last_error is not None) or (
            self.last_success is None or (now - self.last_success > self.stale_after)
        )

        out: list[Sensor] = []
        for name in sorted(self._sensors.keys()):
            s = self._sensors[name]
            if is_stale:
                eff_st = "missing" if s.status == "missing" else "stale"
                out.append(Sensor(
                    name=s.name,
                    status=eff_st,
                    health="unknown",
                    group=s.group,
                    raw_status=s.raw_status,
                ))
            else:
                out.append(s)

        return out
