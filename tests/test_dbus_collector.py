import json
import math
import threading
import unittest
from typing import Any

from sensor_overview.collector import CollectionError, DBusCollector, dbus_ssh_command
from sensor_overview.model import (
    HealthState,
    Sensor,
    group_name,
    parse_dbus_managed_objects,
    parse_dbus_sensor,
    parse_dbus_snapshot,
)


class TestHealthState(unittest.TestCase):
    def test_enum_and_string_equality(self):
        self.assertEqual(HealthState.NORMAL, "normal")
        self.assertEqual(HealthState.NORMAL, "NORMAL")
        self.assertEqual(HealthState.WARNING, "warning")
        self.assertEqual(HealthState.WARNING, "WARNING")
        self.assertEqual(HealthState.CRITICAL, "critical")
        self.assertEqual(HealthState.CRITICAL, "CRITICAL")
        self.assertEqual(HealthState.UNAVAILABLE, "unavailable")
        self.assertEqual(HealthState.UNAVAILABLE, "UNAVAILABLE")


class TestSensorHealthBoundaries(unittest.TestCase):
    def test_normal_sensor(self):
        interfaces = {
            "xyz.openbmc_project.Sensor.Value": {
                "Value": {"type": "d", "data": 25.0},
                "Unit": {"type": "s", "data": "xyz.openbmc_project.Sensor.Value.Unit.DegreesC"},
            },
            "xyz.openbmc_project.State.Decorator.OperationalStatus": {
                "Functional": {"type": "b", "data": True},
            },
            "xyz.openbmc_project.State.Decorator.Availability": {
                "Available": {"type": "b", "data": True},
            },
            "xyz.openbmc_project.Sensor.Threshold.Critical": {
                "CriticalHigh": {"type": "d", "data": 85.0},
                "CriticalLow": {"type": "d", "data": 0.0},
                "CriticalAlarmHigh": {"type": "b", "data": False},
                "CriticalAlarmLow": {"type": "b", "data": False},
            },
            "xyz.openbmc_project.Sensor.Threshold.Warning": {
                "WarningHigh": {"type": "d", "data": 75.0},
                "WarningLow": {"type": "d", "data": 5.0},
                "WarningAlarmHigh": {"type": "b", "data": False},
                "WarningAlarmLow": {"type": "b", "data": False},
            },
        }
        s = parse_dbus_sensor("/xyz/openbmc_project/sensors/temperature/TEMP_AMBIENT", interfaces)
        self.assertEqual(s.name, "TEMP_AMBIENT")
        self.assertEqual(s.health, HealthState.NORMAL)
        self.assertEqual(s.status, "ok")
        self.assertEqual(s.value, 25.0)
        self.assertTrue(s.functional)
        self.assertTrue(s.available)

    def test_warning_threshold_high_exceeded(self):
        interfaces = {
            "xyz.openbmc_project.Sensor.Value": {"Value": 76.0},
            "xyz.openbmc_project.Sensor.Threshold.Critical": {"CriticalHigh": 85.0},
            "xyz.openbmc_project.Sensor.Threshold.Warning": {"WarningHigh": 75.0},
        }
        s = parse_dbus_sensor("/xyz/openbmc_project/sensors/temperature/TEMP_HIGH_WARN", interfaces)
        self.assertEqual(s.health, HealthState.WARNING)
        self.assertIn("warning", s.status.lower())

    def test_warning_threshold_low_exceeded(self):
        interfaces = {
            "xyz.openbmc_project.Sensor.Value": {"Value": 4.5},
            "xyz.openbmc_project.Sensor.Threshold.Warning": {"WarningLow": 5.0},
        }
        s = parse_dbus_sensor("/xyz/openbmc_project/sensors/temperature/TEMP_LOW_WARN", interfaces)
        self.assertEqual(s.health, HealthState.WARNING)

    def test_warning_alarm_high_asserted(self):
        interfaces = {
            "xyz.openbmc_project.Sensor.Value": {"Value": 60.0},
            "xyz.openbmc_project.Sensor.Threshold.Warning": {
                "WarningHigh": 75.0,
                "WarningAlarmHigh": True,
            },
        }
        s = parse_dbus_sensor("/xyz/openbmc_project/sensors/temperature/TEMP_ALARM_WARN", interfaces)
        self.assertEqual(s.health, HealthState.WARNING)

    def test_warning_alarm_low_asserted(self):
        interfaces = {
            "xyz.openbmc_project.Sensor.Value": {"Value": 20.0},
            "xyz.openbmc_project.Sensor.Threshold.Warning": {
                "WarningAlarmLow": True,
            },
        }
        s = parse_dbus_sensor("/xyz/openbmc_project/sensors/temperature/TEMP_ALARM_LOW_WARN", interfaces)
        self.assertEqual(s.health, HealthState.WARNING)

    def test_critical_threshold_high_exceeded(self):
        interfaces = {
            "xyz.openbmc_project.Sensor.Value": {"Value": 90.0},
            "xyz.openbmc_project.Sensor.Threshold.Critical": {"CriticalHigh": 85.0},
            "xyz.openbmc_project.Sensor.Threshold.Warning": {"WarningHigh": 75.0},
        }
        s = parse_dbus_sensor("/xyz/openbmc_project/sensors/temperature/TEMP_CRIT_HIGH", interfaces)
        self.assertEqual(s.health, HealthState.CRITICAL)
        self.assertIn("critical", s.status.lower())

    def test_critical_threshold_low_exceeded(self):
        interfaces = {
            "xyz.openbmc_project.Sensor.Value": {"Value": -5.0},
            "xyz.openbmc_project.Sensor.Threshold.Critical": {"CriticalLow": 0.0},
        }
        s = parse_dbus_sensor("/xyz/openbmc_project/sensors/temperature/TEMP_CRIT_LOW", interfaces)
        self.assertEqual(s.health, HealthState.CRITICAL)

    def test_critical_alarm_high_asserted(self):
        interfaces = {
            "xyz.openbmc_project.Sensor.Value": {"Value": 50.0},
            "xyz.openbmc_project.Sensor.Threshold.Critical": {"CriticalAlarmHigh": True},
        }
        s = parse_dbus_sensor("/xyz/openbmc_project/sensors/temperature/TEMP_CRIT_ALARM", interfaces)
        self.assertEqual(s.health, HealthState.CRITICAL)

    def test_critical_alarm_low_asserted(self):
        interfaces = {
            "xyz.openbmc_project.Sensor.Value": {"Value": 50.0},
            "xyz.openbmc_project.Sensor.Threshold.Critical": {"CriticalAlarmLow": True},
        }
        s = parse_dbus_sensor("/xyz/openbmc_project/sensors/temperature/TEMP_CRIT_ALARM_LOW", interfaces)
        self.assertEqual(s.health, HealthState.CRITICAL)

    def test_critical_functional_false(self):
        interfaces = {
            "xyz.openbmc_project.Sensor.Value": {"Value": 25.0},
            "xyz.openbmc_project.State.Decorator.OperationalStatus": {"Functional": False},
        }
        s = parse_dbus_sensor("/xyz/openbmc_project/sensors/temperature/TEMP_UNFUNCTIONAL", interfaces)
        self.assertEqual(s.health, HealthState.CRITICAL)
        self.assertFalse(s.functional)

    def test_unavailable_available_false(self):
        interfaces = {
            "xyz.openbmc_project.Sensor.Value": {"Value": 25.0},
            "xyz.openbmc_project.State.Decorator.Availability": {"Available": False},
        }
        s = parse_dbus_sensor("/xyz/openbmc_project/sensors/temperature/SLOT1_TEMP", interfaces)
        self.assertEqual(s.health, HealthState.UNAVAILABLE)
        self.assertFalse(s.available)
        self.assertEqual(s.status, "unavailable")

    def test_unavailable_nan_value(self):
        interfaces = {
            "xyz.openbmc_project.Sensor.Value": {"Value": float("nan")},
        }
        s = parse_dbus_sensor("/xyz/openbmc_project/sensors/temperature/TEMP_NAN", interfaces)
        self.assertEqual(s.health, HealthState.UNAVAILABLE)
        self.assertIsNone(s.value)

    def test_unavailable_none_value(self):
        interfaces = {
            "xyz.openbmc_project.Sensor.Value": {"Value": None},
        }
        s = parse_dbus_sensor("/xyz/openbmc_project/sensors/temperature/TEMP_NONE", interfaces)
        self.assertEqual(s.health, HealthState.UNAVAILABLE)
        self.assertIsNone(s.value)

    def test_unavailable_inf_value(self):
        interfaces = {
            "xyz.openbmc_project.Sensor.Value": {"Value": float("inf")},
        }
        s = parse_dbus_sensor("/xyz/openbmc_project/sensors/temperature/TEMP_INF", interfaces)
        self.assertEqual(s.health, HealthState.UNAVAILABLE)

        interfaces_neg = {
            "xyz.openbmc_project.Sensor.Value": {"Value": float("-inf")},
        }
        s_neg = parse_dbus_sensor("/xyz/openbmc_project/sensors/temperature/TEMP_NEGINF", interfaces_neg)
        self.assertEqual(s_neg.health, HealthState.UNAVAILABLE)

    def test_unavailable_invalid_value_type(self):
        interfaces = {
            "xyz.openbmc_project.Sensor.Value": {"Value": "invalid_string"},
        }
        s = parse_dbus_sensor("/xyz/openbmc_project/sensors/temperature/TEMP_STR", interfaces)
        self.assertEqual(s.health, HealthState.UNAVAILABLE)

        interfaces_bool = {
            "xyz.openbmc_project.Sensor.Value": {"Value": True},
        }
        s_bool = parse_dbus_sensor("/xyz/openbmc_project/sensors/temperature/TEMP_BOOL", interfaces_bool)
        self.assertEqual(s_bool.health, HealthState.UNAVAILABLE)

    def test_group_name_integration(self):
        interfaces = {"xyz.openbmc_project.Sensor.Value": {"Value": 45.0}}
        s = parse_dbus_sensor(
            "/xyz/openbmc_project/sensors/temperature/SENTINEL_DOME_SLOT_1_CPU_TEMP",
            interfaces,
        )
        self.assertEqual(s.group, ("Slot 1", "Sentinel Dome"))


class TestDBusCollectorParsing(unittest.TestCase):
    def test_parse_dbus_managed_objects(self):
        managed_objects = {
            "/xyz/openbmc_project/sensors/temperature/TEMP_A": {
                "xyz.openbmc_project.Sensor.Value": {"Value": {"type": "d", "data": 30.0}},
                "xyz.openbmc_project.State.Decorator.OperationalStatus": {"Functional": {"type": "b", "data": True}},
            },
            "/xyz/openbmc_project/sensors/temperature/TEMP_B": {
                "xyz.openbmc_project.Sensor.Value": {"Value": {"type": "d", "data": 99.0}},
                "xyz.openbmc_project.Sensor.Threshold.Critical": {"CriticalHigh": {"type": "d", "data": 80.0}},
            },
        }
        sensors = parse_dbus_managed_objects(managed_objects)
        self.assertEqual(len(sensors), 2)
        self.assertEqual(sensors["TEMP_A"].health, HealthState.NORMAL)
        self.assertEqual(sensors["TEMP_B"].health, HealthState.CRITICAL)

    def test_busctl_json_envelope_parsing(self):
        busctl_json = json.dumps({
            "type": "a{oa{sa{sv}}}",
            "data": [
                {
                    "/xyz/openbmc_project/sensors/voltage/VOLT_12V": {
                        "xyz.openbmc_project.Sensor.Value": {
                            "Value": {"type": "d", "data": 12.1},
                            "Unit": {"type": "s", "data": "xyz.openbmc_project.Sensor.Value.Unit.Volts"},
                        }
                    }
                }
            ],
        })
        sensors = parse_dbus_snapshot(busctl_json)
        self.assertIn("VOLT_12V", sensors)
        self.assertEqual(sensors["VOLT_12V"].value, 12.1)
        self.assertEqual(sensors["VOLT_12V"].health, HealthState.NORMAL)


class TestDBusCollectorRunner(unittest.TestCase):
    def test_ssh_command_building(self):
        cmd = dbus_ssh_command("root@192.0.2.1", "busctl --json=pretty call ...", port=2222, identity="/path/to/key")
        self.assertIn("ssh", cmd[0])
        self.assertIn("-p", cmd)
        self.assertIn("2222", cmd)
        self.assertIn("-i", cmd)
        self.assertIn("/path/to/key", cmd)
        self.assertEqual(cmd[-2], "root@192.0.2.1")
        self.assertEqual(cmd[-1], "busctl --json=pretty call ...")

    def test_successful_dbus_collection_flow(self):
        # 1. GetSubTree mock
        subtree_data = {
            "type": "a{sa{sas}}",
            "data": [
                {
                    "/xyz/openbmc_project/sensors/temperature/CPU_TEMP": {
                        "xyz.openbmc_project.HwmonTemp": ["xyz.openbmc_project.Sensor.Value"]
                    },
                    "/xyz/openbmc_project/sensors/voltage/P12V": {
                        "xyz.openbmc_project.PSUSensor": ["xyz.openbmc_project.Sensor.Value"]
                    },
                }
            ],
        }

        # 2. GetManagedObjects mock for HwmonTemp
        hwmon_data = {
            "type": "a{oa{sa{sv}}}",
            "data": [
                {
                    "/xyz/openbmc_project/sensors/temperature/CPU_TEMP": {
                        "xyz.openbmc_project.Sensor.Value": {"Value": {"type": "d", "data": 42.0}},
                        "xyz.openbmc_project.State.Decorator.OperationalStatus": {"Functional": {"type": "b", "data": True}},
                    }
                }
            ],
        }

        # 3. GetManagedObjects mock for PSUSensor
        psu_data = {
            "type": "a{oa{sa{sv}}}",
            "data": [
                {
                    "/xyz/openbmc_project/sensors/voltage/P12V": {
                        "xyz.openbmc_project.Sensor.Value": {"Value": {"type": "d", "data": 12.0}},
                        "xyz.openbmc_project.Sensor.Threshold.Warning": {
                            "WarningHigh": {"type": "d", "data": 13.0},
                            "WarningAlarmHigh": {"type": "b", "data": True},
                        },
                    }
                }
            ],
        }

        def mock_runner(cmd: list[str], timeout: float) -> tuple[int, str, str]:
            cmd_str = " ".join(cmd)
            if "ObjectMapper" in cmd_str and "GetSubTree" in cmd_str:
                return (0, json.dumps(subtree_data), "")
            elif "HwmonTemp" in cmd_str and "GetManagedObjects" in cmd_str:
                return (0, json.dumps(hwmon_data), "")
            elif "PSUSensor" in cmd_str and "GetManagedObjects" in cmd_str:
                return (0, json.dumps(psu_data), "")
            return (1, "", "Unknown call")

        collector = DBusCollector(local=True, cmd_runner=mock_runner)
        stop = threading.Event()
        sensors = collector.collect_sensors(stop)

        self.assertEqual(len(sensors), 2)
        self.assertEqual(sensors["CPU_TEMP"].health, HealthState.NORMAL)
        self.assertEqual(sensors["CPU_TEMP"].value, 42.0)
        self.assertEqual(sensors["P12V"].health, HealthState.WARNING)
        self.assertEqual(sensors["P12V"].value, 12.0)

        raw_json = collector.collect(stop)
        self.assertTrue(isinstance(raw_json, str))
        self.assertIn("CPU_TEMP", raw_json)

    def test_object_manager_fallback_to_properties_get_all(self):
        # When a daemon does not implement ObjectManager, GetManagedObjects fails,
        # and collector falls back to Properties.GetAll on the sensor paths.
        subtree_data = {
            "type": "a{sa{sas}}",
            "data": [
                {
                    "/xyz/openbmc_project/sensors/temperature/FAN_TEMP": {
                        "xyz.openbmc_project.LegacyDaemon": ["xyz.openbmc_project.Sensor.Value"]
                    }
                }
            ],
        }

        get_all_data = {
            "type": "a{sv}",
            "data": [
                {
                    "Value": {"type": "d", "data": 35.0},
                    "Unit": {"type": "s", "data": "xyz.openbmc_project.Sensor.Value.Unit.DegreesC"},
                    "Functional": {"type": "b", "data": True},
                    "Available": {"type": "b", "data": True},
                }
            ],
        }

        def mock_runner(cmd: list[str], timeout: float) -> tuple[int, str, str]:
            cmd_str = " ".join(cmd)
            if "GetSubTree" in cmd_str:
                return (0, json.dumps(subtree_data), "")
            elif "LegacyDaemon" in cmd_str and "GetManagedObjects" in cmd_str:
                # Return UnknownMethod or non-zero exit code
                return (1, "", "org.freedesktop.DBus.Error.UnknownMethod")
            elif "LegacyDaemon" in cmd_str and "GetAll" in cmd_str:
                return (0, json.dumps(get_all_data), "")
            return (1, "", "Unknown call")

        collector = DBusCollector(local=True, cmd_runner=mock_runner)
        stop = threading.Event()
        sensors = collector.collect_sensors(stop)

        self.assertIn("FAN_TEMP", sensors)
        self.assertEqual(sensors["FAN_TEMP"].health, HealthState.NORMAL)
        self.assertEqual(sensors["FAN_TEMP"].value, 35.0)

    def test_single_daemon_timeout_isolated(self):
        # 1 service succeeds, 1 service hangs/times out
        subtree_data = {
            "type": "a{sa{sas}}",
            "data": [
                {
                    "/xyz/openbmc_project/sensors/temperature/HEALTHY_TEMP": {
                        "xyz.openbmc_project.HealthyService": ["xyz.openbmc_project.Sensor.Value"]
                    },
                    "/xyz/openbmc_project/sensors/temperature/HANGING_TEMP": {
                        "xyz.openbmc_project.HangingService": ["xyz.openbmc_project.Sensor.Value"]
                    },
                }
            ],
        }

        healthy_data = {
            "type": "a{oa{sa{sv}}}",
            "data": [
                {
                    "/xyz/openbmc_project/sensors/temperature/HEALTHY_TEMP": {
                        "xyz.openbmc_project.Sensor.Value": {"Value": {"type": "d", "data": 28.0}},
                    }
                }
            ],
        }

        def mock_runner(cmd: list[str], timeout: float) -> tuple[int, str, str]:
            cmd_str = " ".join(cmd)
            if "GetSubTree" in cmd_str:
                return (0, json.dumps(subtree_data), "")
            elif "HealthyService" in cmd_str:
                return (0, json.dumps(healthy_data), "")
            elif "HangingService" in cmd_str:
                raise TimeoutError("DBus call timed out after 3.0s")
            return (1, "", "error")

        collector = DBusCollector(local=True, cmd_runner=mock_runner)
        stop = threading.Event()
        sensors = collector.collect_sensors(stop)

        # HEALTHY_TEMP is collected normally
        self.assertIn("HEALTHY_TEMP", sensors)
        self.assertEqual(sensors["HEALTHY_TEMP"].health, HealthState.NORMAL)
        self.assertEqual(sensors["HEALTHY_TEMP"].value, 28.0)

        # HANGING_TEMP is isolated and marked UNAVAILABLE without crashing!
        self.assertIn("HANGING_TEMP", sensors)
        self.assertEqual(sensors["HANGING_TEMP"].health, HealthState.UNAVAILABLE)
        self.assertIn("timeout", sensors["HANGING_TEMP"].raw_status.lower())

    def test_single_daemon_error_isolated(self):
        subtree_data = {
            "type": "a{sa{sas}}",
            "data": [
                {
                    "/xyz/openbmc_project/sensors/temperature/OK_TEMP": {
                        "xyz.openbmc_project.OkService": ["xyz.openbmc_project.Sensor.Value"]
                    },
                    "/xyz/openbmc_project/sensors/temperature/FAIL_TEMP": {
                        "xyz.openbmc_project.FailService": ["xyz.openbmc_project.Sensor.Value"]
                    },
                }
            ],
        }

        ok_data = {
            "type": "a{oa{sa{sv}}}",
            "data": [
                {
                    "/xyz/openbmc_project/sensors/temperature/OK_TEMP": {
                        "xyz.openbmc_project.Sensor.Value": {"Value": {"type": "d", "data": 22.0}},
                    }
                }
            ],
        }

        def mock_runner(cmd: list[str], timeout: float) -> tuple[int, str, str]:
            cmd_str = " ".join(cmd)
            if "GetSubTree" in cmd_str:
                return (0, json.dumps(subtree_data), "")
            elif "OkService" in cmd_str:
                return (0, json.dumps(ok_data), "")
            elif "FailService" in cmd_str:
                return (1, "corrupted { json", "D-Bus connection reset by peer")
            return (1, "", "error")

        collector = DBusCollector(local=True, cmd_runner=mock_runner)
        stop = threading.Event()
        sensors = collector.collect_sensors(stop)

        self.assertIn("OK_TEMP", sensors)
        self.assertEqual(sensors["OK_TEMP"].health, HealthState.NORMAL)
        self.assertIn("FAIL_TEMP", sensors)
        self.assertEqual(sensors["FAIL_TEMP"].health, HealthState.UNAVAILABLE)

    def test_subtree_empty_raises_collection_error(self):
        def mock_runner(cmd: list[str], timeout: float) -> tuple[int, str, str]:
            return (0, json.dumps({"type": "a{sa{sas}}", "data": [{}]}), "")

        collector = DBusCollector(local=True, cmd_runner=mock_runner)
        stop = threading.Event()
        with self.assertRaises(CollectionError):
            collector.collect_sensors(stop)

    def test_stop_event_cancels_early(self):
        stop = threading.Event()
        stop.set()
        collector = DBusCollector(local=True)
        with self.assertRaises(CollectionError) as ctx:
            collector.collect_sensors(stop)
        self.assertIn("cancelled", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()

