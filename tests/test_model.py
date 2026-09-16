import json
import math
from collections import deque
from dataclasses import FrozenInstanceError
import unittest

from sensor_overview.model import (
    Event,
    Sensor,
    Tracker,
    group_name,
    parse_snapshot,
)


class TestModel(unittest.TestCase):
    # --- Step 1 Required Tests from Implementation Plan ---

    def test_missing_recovers(self):
        t = Tracker()
        t.ingest('{"A":{"status":"ok","value":1}}', 1)
        t.ingest('{"B":{"status":"ok","value":2}}', 2)
        self.assertEqual({s.name: s.status for s in t.view(2)}['A'], 'missing')
        t.ingest('{"A":{"status":"ok","value":1}}', 3)
        self.assertEqual({s.name: s.health for s in t.view(3)}['A'], 'normal')

    def test_failure_never_leaves_green(self):
        t = Tracker()
        t.ingest('{"A":{"status":"ok","value":1}}', 1)
        t.fail('SSH disconnected', 2)
        self.assertTrue(all(s.health == 'unknown' for s in t.view(2)))
        self.assertEqual(t.last_success, 1)

    def test_empty_is_not_success(self):
        with self.assertRaises(ValueError):
            parse_snapshot('{}')

    # --- Step 4 Extended Tests & Regression Tests ---

    def test_seven_raw_statuses(self):
        # 1. ok (with finite value) -> normal, effective status ok
        s_ok = parse_snapshot('{"S_OK": {"status": "ok", "value": 25.0}}')["S_OK"]
        self.assertEqual(s_ok.status, "ok")
        self.assertEqual(s_ok.raw_status, "ok")
        self.assertEqual(s_ok.health, "normal")

        # 2. warning -> attention
        s_warn = parse_snapshot('{"S_WARN": {"status": "warning", "value": 85.0}}')["S_WARN"]
        self.assertEqual(s_warn.status, "warning")
        self.assertEqual(s_warn.raw_status, "warning")
        self.assertEqual(s_warn.health, "attention")

        # 3. critical -> attention
        s_crit = parse_snapshot('{"S_CRIT": {"status": "critical", "value": 99.0}}')["S_CRIT"]
        self.assertEqual(s_crit.status, "critical")
        self.assertEqual(s_crit.raw_status, "critical")
        self.assertEqual(s_crit.health, "attention")

        # 4. high -> attention
        s_high = parse_snapshot('{"S_HIGH": {"status": "high", "value": 90.0}}')["S_HIGH"]
        self.assertEqual(s_high.status, "high")
        self.assertEqual(s_high.raw_status, "high")
        self.assertEqual(s_high.health, "attention")

        # 5. low -> attention
        s_low = parse_snapshot('{"S_LOW": {"status": "low", "value": 5.0}}')["S_LOW"]
        self.assertEqual(s_low.status, "low")
        self.assertEqual(s_low.raw_status, "low")
        self.assertEqual(s_low.health, "attention")

        # 6. unavailable -> unknown
        s_unavail = parse_snapshot('{"S_UN": {"status": "unavailable", "value": null}}')["S_UN"]
        self.assertEqual(s_unavail.status, "unavailable")
        self.assertEqual(s_unavail.raw_status, "unavailable")
        self.assertEqual(s_unavail.health, "unknown")

        # 7. dbus error -> unknown
        s_dbus = parse_snapshot('{"S_DBUS": {"status": "dbus error"}}')["S_DBUS"]
        self.assertEqual(s_dbus.status, "dbus error")
        self.assertEqual(s_dbus.raw_status, "dbus error")
        self.assertEqual(s_dbus.health, "unknown")

    def test_unknown_status_string(self):
        s_unknown = parse_snapshot('{"S_STR": {"status": "some_random_status", "value": 10}}')["S_STR"]
        self.assertEqual(s_unknown.status, "some_random_status")
        self.assertEqual(s_unknown.raw_status, "some_random_status")
        self.assertEqual(s_unknown.health, "unknown")

    def test_missing_fields_in_entry(self):
        # Missing status field defaults to unknown
        s_no_status = parse_snapshot('{"S_NO_ST": {"value": 10.0}}')["S_NO_ST"]
        self.assertEqual(s_no_status.status, "unknown")
        self.assertEqual(s_no_status.raw_status, "unknown")
        self.assertEqual(s_no_status.health, "unknown")

        # Missing value field with ok status -> effective status is 'invalid value', health unknown
        s_no_val = parse_snapshot('{"S_NO_VAL": {"status": "ok"}}')["S_NO_VAL"]
        self.assertEqual(s_no_val.status, "invalid value")
        self.assertEqual(s_no_val.raw_status, "ok")
        self.assertEqual(s_no_val.health, "unknown")

    def test_non_finite_and_boolean_and_null_values(self):
        # Bool value must not be treated as int; effective status 'invalid value'
        s_bool_t = parse_snapshot('{"S_BOOL_T": {"status": "ok", "value": true}}')["S_BOOL_T"]
        self.assertEqual(s_bool_t.status, "invalid value")
        self.assertEqual(s_bool_t.raw_status, "ok")
        self.assertEqual(s_bool_t.health, "unknown")

        s_bool_f = parse_snapshot('{"S_BOOL_F": {"status": "ok", "value": false}}')["S_BOOL_F"]
        self.assertEqual(s_bool_f.status, "invalid value")
        self.assertEqual(s_bool_f.raw_status, "ok")
        self.assertEqual(s_bool_f.health, "unknown")

        # Null value -> effective status 'invalid value'
        s_null = parse_snapshot('{"S_NULL": {"status": "ok", "value": null}}')["S_NULL"]
        self.assertEqual(s_null.status, "invalid value")
        self.assertEqual(s_null.raw_status, "ok")
        self.assertEqual(s_null.health, "unknown")

        # NaN / Infinity / -Infinity -> effective status 'invalid value'
        s_nan = parse_snapshot('{"S_NAN": {"status": "ok", "value": NaN}}')["S_NAN"]
        self.assertEqual(s_nan.status, "invalid value")
        self.assertEqual(s_nan.raw_status, "ok")
        self.assertEqual(s_nan.health, "unknown")

        s_inf = parse_snapshot('{"S_INF": {"status": "ok", "value": Infinity}}')["S_INF"]
        self.assertEqual(s_inf.status, "invalid value")
        self.assertEqual(s_inf.raw_status, "ok")
        self.assertEqual(s_inf.health, "unknown")

        s_ninf = parse_snapshot('{"S_NINF": {"status": "ok", "value": -Infinity}}')["S_NINF"]
        self.assertEqual(s_ninf.status, "invalid value")
        self.assertEqual(s_ninf.raw_status, "ok")
        self.assertEqual(s_ninf.health, "unknown")

    def test_large_integer_overflow_guarded(self):
        # Extremely large integer that overflows float conversion
        big_int = "1" + "0" * 400
        snap_text = f'{{"S_BIG": {{"status": "ok", "value": {big_int}}}}}'
        s_big = parse_snapshot(snap_text)["S_BIG"]
        self.assertEqual(s_big.status, "invalid value")
        self.assertEqual(s_big.raw_status, "ok")
        self.assertEqual(s_big.health, "unknown")

    def test_duplicate_json_key_rejection(self):
        # Duplicate top-level key
        dup_top = '{"A": {"status": "ok", "value": 1}, "A": {"status": "ok", "value": 2}}'
        with self.assertRaises(ValueError):
            parse_snapshot(dup_top)

        # Duplicate key in entry object
        dup_nested = '{"A": {"status": "ok", "status": "ok", "value": 1}}'
        with self.assertRaises(ValueError):
            parse_snapshot(dup_nested)

    def test_invalid_snapshot_structures(self):
        # Empty text / whitespace
        with self.assertRaises(ValueError):
            parse_snapshot("")
        with self.assertRaises(ValueError):
            parse_snapshot("   \n\t  ")

        # Malformed JSON
        with self.assertRaises(ValueError):
            parse_snapshot("{not valid json")

        # Top-level array
        with self.assertRaises(ValueError):
            parse_snapshot('[{"status": "ok", "value": 1}]')

        # Top-level primitive
        with self.assertRaises(ValueError):
            parse_snapshot('"just a string"')
        with self.assertRaises(ValueError):
            parse_snapshot('12345')

        # Non-dict sensor entry
        with self.assertRaises(ValueError):
            parse_snapshot('{"A": 123}')
        with self.assertRaises(ValueError):
            parse_snapshot('{"A": [1, 2, 3]}')
        with self.assertRaises(ValueError):
            parse_snapshot('{"A": null}')
        with self.assertRaises(ValueError):
            parse_snapshot('{"A": "bad_entry"}')

    def test_ingest_validation_failure_leaves_state_unchanged(self):
        t = Tracker()
        t.ingest('{"A": {"status": "ok", "value": 1}}', 1.0)
        self.assertEqual(len(t.view(1.0)), 1)
        self.assertEqual(t.last_success, 1.0)

        # Ingest invalid payload
        with self.assertRaises(ValueError):
            t.ingest('{"A": {"status": "ok"}, "A": {"status": "bad"}}', 2.0)

        # State must remain from t=1.0
        self.assertEqual(t.last_success, 1.0)
        self.assertIsNone(t.last_error)
        views = t.view(1.0)
        self.assertEqual(len(views), 1)
        self.assertEqual(views[0].name, "A")
        self.assertEqual(views[0].status, "ok")
        self.assertEqual(len(t.events), 0)

    def test_no_events_when_statuses_unchanged(self):
        t = Tracker()
        t.ingest('{"A": {"status": "ok", "value": 1}, "B": {"status": "critical", "value": 90}}', 1.0)
        self.assertEqual(len(t.events), 0)  # First snapshot produces no events

        t.ingest('{"A": {"status": "ok", "value": 1}, "B": {"status": "critical", "value": 92}}', 2.0)
        self.assertEqual(len(t.events), 0)  # No status changes

        # Change A to warning
        t.ingest('{"A": {"status": "warning", "value": 80}, "B": {"status": "critical", "value": 92}}', 3.0)
        self.assertEqual(len(t.events), 1)
        ev = t.events[-1]
        self.assertEqual(ev.name, "A")
        self.assertEqual(ev.before, "ok")
        self.assertEqual(ev.after, "warning")
        self.assertEqual(ev.at, 3.0)

    def test_validity_transitions_ok_to_invalid_value_to_ok(self):
        t = Tracker()
        # Round 1: normal ok
        t.ingest('{"A": {"status": "ok", "value": 10.0}}', 1.0)
        self.assertEqual(len(t.events), 0)

        # Round 2: raw status stays 'ok', but value turns null -> effective status 'invalid value'
        t.ingest('{"A": {"status": "ok", "value": null}}', 2.0)
        self.assertEqual(len(t.events), 1)
        ev1 = t.events[-1]
        self.assertEqual(ev1.name, "A")
        self.assertEqual(ev1.before, "ok")
        self.assertEqual(ev1.after, "invalid value")

        # Round 3: recovers to valid value
        t.ingest('{"A": {"status": "ok", "value": 15.0}}', 3.0)
        self.assertEqual(len(t.events), 2)
        ev2 = t.events[-1]
        self.assertEqual(ev2.name, "A")
        self.assertEqual(ev2.before, "invalid value")
        self.assertEqual(ev2.after, "ok")

    def test_discovery_of_new_sensor_after_first_snapshot(self):
        t = Tracker()
        # First snapshot: no events
        t.ingest('{"A": {"status": "ok", "value": 1.0}}', 1.0)
        self.assertEqual(len(t.events), 0)

        # Second snapshot: A unchanged, B is discovered
        t.ingest('{"A": {"status": "ok", "value": 1.0}, "B": {"status": "ok", "value": 2.0}}', 2.0)
        self.assertEqual(len(t.events), 1)
        ev = t.events[-1]
        self.assertEqual(ev.name, "B")
        self.assertEqual(ev.before, "new")
        self.assertEqual(ev.after, "ok")

    def test_missing_tombstone_preserves_raw_status_and_appends_single_event(self):
        t = Tracker()
        t.ingest('{"A": {"status": "ok", "value": 1.0}}', 1.0)
        self.assertEqual(len(t.events), 0)

        # A goes missing in round 2
        t.ingest('{"B": {"status": "ok", "value": 2.0}}', 2.0)
        # Events should contain exactly 2: B ('new' -> 'ok') and A ('ok' -> 'missing')
        self.assertEqual(len(t.events), 2)
        ev_missing = [e for e in t.events if e.name == "A"]
        self.assertEqual(len(ev_missing), 1)
        self.assertEqual(ev_missing[0].before, "ok")
        self.assertEqual(ev_missing[0].after, "missing")

        views = {s.name: s for s in t.view(2.0)}
        self.assertEqual(views["A"].status, "missing")
        self.assertEqual(views["A"].raw_status, "ok")  # preserves actual source raw status

        # Round 3: A still missing, B unchanged -> NO duplicate missing events
        t.ingest('{"B": {"status": "ok", "value": 2.0}}', 3.0)
        self.assertEqual(len(t.events), 2)

    def test_stale_boundary_and_effective_views(self):
        t = Tracker(stale_after=10.0)
        t.ingest('{"A": {"status": "ok", "value": 1.0}}', 100.0)

        # Exactly at 10.0s elapsed: not stale (> 10.0 required)
        views_edge = t.view(110.0)
        self.assertEqual(views_edge[0].status, "ok")
        self.assertEqual(views_edge[0].health, "normal")

        # 10.001s elapsed: stale
        views_stale = t.view(110.001)
        self.assertEqual(views_stale[0].status, "stale")
        self.assertEqual(views_stale[0].health, "unknown")
        # raw_status must be preserved
        self.assertEqual(views_stale[0].raw_status, "ok")

    def test_fail_deduplication_and_recovery(self):
        t = Tracker()
        t.ingest('{"A": {"status": "ok", "value": 1.0}}', 1.0)

        # First failure
        t.fail("Connection refused", 2.0)
        self.assertEqual(len(t.events), 1)
        self.assertEqual(t.events[-1].name, "COLLECTOR")
        self.assertEqual(t.events[-1].after, "Connection refused")
        self.assertEqual(t.last_error, "Connection refused")
        self.assertEqual(t.view(2.0)[0].status, "stale")
        self.assertEqual(t.view(2.0)[0].health, "unknown")

        # Identical failure does not spam events
        t.fail("Connection refused", 3.0)
        self.assertEqual(len(t.events), 1)

        # Different failure creates new event
        t.fail("Timeout", 4.0)
        self.assertEqual(len(t.events), 2)
        self.assertEqual(t.events[-1].after, "Timeout")

        # Success recovers tracker and emits recovery event
        t.ingest('{"A": {"status": "ok", "value": 1.0}}', 5.0)
        self.assertIsNone(t.last_error)
        self.assertEqual(t.last_success, 5.0)
        self.assertEqual(t.view(5.0)[0].status, "ok")
        self.assertEqual(t.view(5.0)[0].health, "normal")
        self.assertEqual(len(t.events), 3)
        self.assertEqual(t.events[-1].name, "COLLECTOR")
        self.assertEqual(t.events[-1].before, "Timeout")
        self.assertEqual(t.events[-1].after, "ok")

    def test_event_ring_buffer_caps_at_100(self):
        t = Tracker()
        t.ingest('{"A": {"status": "ok", "value": 1}}', 0.0)

        for i in range(1, 125):
            st = "critical" if i % 2 == 1 else "ok"
            t.ingest(f'{{"A": {{"status": "{st}", "value": 1}}}}', float(i))

        self.assertEqual(len(t.events), 100)
        self.assertEqual(t.events.maxlen, 100)
        # Oldest events (i < 25) discarded, newest (i=124) present
        self.assertEqual(t.events[-1].at, 124.0)

    def test_group_name_rules_and_no_suffix_guessing(self):
        # 1. Explicit Sentinel Dome slot -> ('Slot N', 'Sentinel Dome')
        self.assertEqual(group_name("SENTINEL_DOME_SLOT_1_TEMP"), ("Slot 1", "Sentinel Dome"))
        # 2. Explicit Wailua Falls slot -> ('Slot N', 'Wailua Falls')
        self.assertEqual(group_name("WAILUA_FALLS_SLOT_8_VOLT"), ("Slot 8", "Wailua Falls"))
        # 3. CALIBRATED_ prefix stripped only for grouping, sensor retains board
        self.assertEqual(group_name("CALIBRATED_SENTINEL_DOME_SLOT_3_CURR"), ("Slot 3", "Sentinel Dome"))
        self.assertEqual(group_name("CALIBRATED_WAILUA_FALLS_SLOT_2_TEMP"), ("Slot 2", "Wailua Falls"))
        # 4. MEDUSA_MB1 must NOT be guessed as Slot 1
        self.assertEqual(group_name("MEDUSA_MB1_PWR"), ("MEDUSA",))
        self.assertEqual(group_name("MEDUSA_VOLT"), ("MEDUSA",))
        # 5. SPIDER board
        self.assertEqual(group_name("SPIDER_TEMP"), ("SPIDER",))
        # 6. MGNT board
        self.assertEqual(group_name("MGNT_TEMP"), ("MGNT",))
        # 7. FANBOARDn and FANBOARD
        self.assertEqual(group_name("FANBOARD0_TEMP"), ("FANBOARD0",))
        self.assertEqual(group_name("FANBOARD3_PWM"), ("FANBOARD3",))
        self.assertEqual(group_name("FANBOARD_TACH"), ("FANBOARD",))
        # 8. BMC metrics: ONLY exact CPU or Memory (case insensitive)
        self.assertEqual(group_name("CPU"), ("BMC metrics",))
        self.assertEqual(group_name("cpu"), ("BMC metrics",))
        self.assertEqual(group_name("Memory"), ("BMC metrics",))
        self.assertEqual(group_name("MEMORY"), ("BMC metrics",))
        # Other names containing CPU/MEM must be Unclassified
        self.assertEqual(group_name("MB_CPU_TEMP"), ("Unclassified",))
        self.assertEqual(group_name("CPU_TEMP"), ("Unclassified",))
        self.assertEqual(group_name("BMC_CPU_USAGE"), ("Unclassified",))
        self.assertEqual(group_name("MEMORY_UTIL"), ("Unclassified",))
        # 9. Fuzzy suffixes like _38_40 must NOT guess Slot
        self.assertEqual(group_name("TEMP_38_40"), ("Unclassified",))
        # 10. VIRTUAL_FANBOARD* -> corresponding FANBOARD group
        self.assertEqual(group_name("VIRTUAL_FANBOARD0_FAN0_48V_PWR_W"), ("FANBOARD0",))
        self.assertEqual(group_name("VIRTUAL_FANBOARD1_FAN10_48V_PWR_W"), ("FANBOARD1",))
        # 11. NIC sensors -> NIC group
        self.assertEqual(group_name("NIC0_TEMP_C"), ("NIC",))
        self.assertEqual(group_name("NIC2_TEMP_C"), ("NIC",))
        self.assertEqual(group_name("VIRTUAL_NIC0_TEMP_C"), ("NIC",))
        self.assertEqual(group_name("VIRTUAL_NIC3_TEMP_C"), ("NIC",))
        # 12. bmc/* -> BMC metrics
        self.assertEqual(group_name("bmc/cpu/kernel"), ("BMC metrics",))
        self.assertEqual(group_name("bmc/memory/available"), ("BMC metrics",))
        self.assertEqual(group_name("bmc/storage/rw"), ("BMC metrics",))
        # 13. SYSTEM_* -> System
        self.assertEqual(group_name("SYSTEM_AIRFLOW_CFM"), ("System",))
        self.assertEqual(group_name("SYSTEM_PLATFORM_INPUT_PWR_W"), ("System",))
        # 14. MEDUSA0_TEMP_C, SPIDER0_TEMP_C -> parent board group
        self.assertEqual(group_name("MEDUSA0_TEMP_C"), ("MEDUSA",))
        self.assertEqual(group_name("MEDUSA1_TEMP_C"), ("MEDUSA",))
        self.assertEqual(group_name("SPIDER0_TEMP_C"), ("SPIDER",))
        self.assertEqual(group_name("SPIDER1_TEMP_C"), ("SPIDER",))
        # 15. SENTINEL_DOME_SLOT_PRESENT_PERCENTAGE (no slot number) -> Unclassified
        self.assertEqual(group_name("SENTINEL_DOME_SLOT_PRESENT_PERCENTAGE"), ("System",))
        self.assertEqual(group_name("UNKNOWN_SENSOR_MB1"), ("Unclassified",))
        # 10. Sensor with Slot pattern not matching SENTINEL_DOME or WAILUA_FALLS
        self.assertEqual(group_name("OTHER_SLOT_1_TEMP"), ("Unclassified",))

    def test_immutable_views_and_ordering(self):
        t = Tracker()
        t.ingest('{"Z": {"status": "ok", "value": 1}, "A": {"status": "ok", "value": 2}}', 1.0)
        views = t.view(1.0)

        # Sorted by name
        self.assertEqual([s.name for s in views], ["A", "Z"])

        # Sensor dataclass is frozen
        s = views[0]
        with self.assertRaises(FrozenInstanceError):
            s.status = "stale"  # type: ignore

        # Mutating returned list does not affect tracker view
        views.pop()
        self.assertEqual(len(t.view(1.0)), 2)


if __name__ == "__main__":
    unittest.main()
