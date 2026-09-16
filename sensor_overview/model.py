import json
import math
import re
from collections import deque
from dataclasses import dataclass
from typing import Any

__all__ = [
    "HealthState",
    "Sensor",
    "Event",
    "group_name",
    "parse_snapshot",
    "parse_dbus_sensor",
    "parse_dbus_managed_objects",
    "parse_dbus_snapshot",
    "Tracker",
]


class HealthState(str):
    NORMAL: "HealthState"
    WARNING: "HealthState"
    CRITICAL: "HealthState"
    UNAVAILABLE: "HealthState"

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, str):
            return self.lower() == other.lower()
        return super().__eq__(other)

    def __hash__(self) -> int:
        return hash(self.lower())


HealthState.NORMAL = HealthState("normal")
HealthState.WARNING = HealthState("warning")
HealthState.CRITICAL = HealthState("critical")
HealthState.UNAVAILABLE = HealthState("unavailable")


@dataclass(frozen=True)
class Sensor:
    name: str
    status: str       # effective status: ok/critical/.../missing/stale/invalid value
    health: str       # normal/attention/unknown or HealthState
    group: tuple[str, ...]
    raw_status: str
    value: float | None = None
    functional: bool = True
    available: bool = True
    unit: str = ""
    path: str = ""
    thresholds: dict[str, float] | None = None


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


def _unwrap_dbus_val(val: Any) -> Any:
    if isinstance(val, dict) and "data" in val:
        return val["data"]
    return val


def _get_prop(interfaces: dict[str, Any], iface: str, prop: str) -> Any:
    if iface in interfaces and isinstance(interfaces[iface], dict):
        if prop in interfaces[iface]:
            return _unwrap_dbus_val(interfaces[iface][prop])
    if prop in interfaces:
        return _unwrap_dbus_val(interfaces[prop])
    return None


def parse_dbus_sensor(path: str, interfaces: dict[str, Any]) -> Sensor:
    name = path.rstrip("/").split("/")[-1]
    raw_val = _get_prop(interfaces, "xyz.openbmc_project.Sensor.Value", "Value")
    unit = _get_prop(interfaces, "xyz.openbmc_project.Sensor.Value", "Unit") or ""

    avail_val = _get_prop(interfaces, "xyz.openbmc_project.State.Decorator.Availability", "Available")
    if avail_val is None:
        avail_val = _get_prop(interfaces, "xyz.openbmc_project.Sensor.Value", "Available")
    available = True if avail_val is None else bool(avail_val)

    func_val = _get_prop(interfaces, "xyz.openbmc_project.State.Decorator.OperationalStatus", "Functional")
    if func_val is None:
        func_val = _get_prop(interfaces, "xyz.openbmc_project.Sensor.Value", "Functional")
    functional = True if func_val is None else bool(func_val)

    crit_high = _get_prop(interfaces, "xyz.openbmc_project.Sensor.Threshold.Critical", "CriticalHigh")
    crit_low = _get_prop(interfaces, "xyz.openbmc_project.Sensor.Threshold.Critical", "CriticalLow")
    crit_alarm_high = _get_prop(interfaces, "xyz.openbmc_project.Sensor.Threshold.Critical", "CriticalAlarmHigh")
    crit_alarm_low = _get_prop(interfaces, "xyz.openbmc_project.Sensor.Threshold.Critical", "CriticalAlarmLow")

    warn_high = _get_prop(interfaces, "xyz.openbmc_project.Sensor.Threshold.Warning", "WarningHigh")
    warn_low = _get_prop(interfaces, "xyz.openbmc_project.Sensor.Threshold.Warning", "WarningLow")
    warn_alarm_high = _get_prop(interfaces, "xyz.openbmc_project.Sensor.Threshold.Warning", "WarningAlarmHigh")
    warn_alarm_low = _get_prop(interfaces, "xyz.openbmc_project.Sensor.Threshold.Warning", "WarningAlarmLow")

    thresholds: dict[str, float] = {}
    if _is_valid_finite(crit_high):
        thresholds["CriticalHigh"] = float(crit_high)
    if _is_valid_finite(crit_low):
        thresholds["CriticalLow"] = float(crit_low)
    if _is_valid_finite(warn_high):
        thresholds["WarningHigh"] = float(warn_high)
    if _is_valid_finite(warn_low):
        thresholds["WarningLow"] = float(warn_low)

    val_is_finite = _is_valid_finite(raw_val)
    val_num = float(raw_val) if val_is_finite else None

    # Determine HealthState and status
    if not available:
        health = HealthState.UNAVAILABLE
        status = "unavailable"
    elif not val_is_finite:
        health = HealthState.UNAVAILABLE
        status = "unavailable"
    elif not functional:
        health = HealthState.CRITICAL
        status = "critical"
    elif bool(crit_alarm_high) or bool(crit_alarm_low):
        health = HealthState.CRITICAL
        status = "critical"
    elif "CriticalHigh" in thresholds and val_num is not None and val_num >= thresholds["CriticalHigh"]:
        health = HealthState.CRITICAL
        status = "critical"
    elif "CriticalLow" in thresholds and val_num is not None and val_num <= thresholds["CriticalLow"]:
        health = HealthState.CRITICAL
        status = "critical"
    elif bool(warn_alarm_high) or bool(warn_alarm_low):
        health = HealthState.WARNING
        status = "warning"
    elif "WarningHigh" in thresholds and val_num is not None and val_num >= thresholds["WarningHigh"]:
        health = HealthState.WARNING
        status = "warning"
    elif "WarningLow" in thresholds and val_num is not None and val_num <= thresholds["WarningLow"]:
        health = HealthState.WARNING
        status = "warning"
    else:
        health = HealthState.NORMAL
        status = "ok"

    return Sensor(
        name=name,
        status=status,
        health=health,
        group=group_name(name),
        raw_status=status,
        value=val_num,
        functional=functional,
        available=available,
        unit=str(unit),
        path=path,
        thresholds=thresholds if thresholds else None,
    )


def parse_dbus_managed_objects(managed_objects: dict[str, Any]) -> dict[str, Sensor]:
    result: dict[str, Sensor] = {}
    if not isinstance(managed_objects, dict):
        return result
    for path, interfaces in managed_objects.items():
        if not isinstance(interfaces, dict):
            continue
        try:
            sensor = parse_dbus_sensor(path, interfaces)
            result[sensor.name] = sensor
        except Exception:
            continue
    return result


def parse_dbus_snapshot(text: str) -> dict[str, Sensor]:
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

    if "type" in data and "data" in data and isinstance(data["data"], list) and len(data["data"]) > 0:
        managed = data["data"][0]
        if isinstance(managed, dict):
            return parse_dbus_managed_objects(managed)

    first_key = next(iter(data.keys()))
    if first_key.startswith("/"):
        return parse_dbus_managed_objects(data)

    return parse_snapshot(text)


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

    if "type" in data and "data" in data and isinstance(data["data"], list):
        return parse_dbus_snapshot(text)

    first_key = next(iter(data.keys())) if data else ""
    if first_key.startswith("/"):
        return parse_dbus_managed_objects(data)

    result: dict[str, Sensor] = {}
    for name, item in data.items():
        if not isinstance(item, dict):
            raise ValueError(f"Sensor entry {name} must be a dict")

        if "health" in item:
            health = HealthState(str(item["health"]).lower())
            raw_st = str(item.get("raw_status", item.get("status", "unknown")))
            st = str(item.get("status", "unknown"))
            val = item.get("value")
            val_num = float(val) if _is_valid_finite(val) else None
            result[name] = Sensor(
                name=name,
                status=st,
                health=health,
                group=tuple(item["group"]) if "group" in item and isinstance(item["group"], (list, tuple)) else group_name(name),
                raw_status=raw_st,
                value=val_num,
                functional=bool(item.get("functional", True)),
                available=bool(item.get("available", True)),
                unit=str(item.get("unit", "")),
                path=str(item.get("path", "")),
                thresholds=item.get("thresholds"),
            )
            continue

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
            value=float(val) if _is_valid_finite(val) else None,
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
                    value=s.value,
                    functional=s.functional,
                    available=s.available,
                    unit=s.unit,
                    path=s.path,
                    thresholds=s.thresholds,
                ))
            else:
                out.append(s)

        return out
