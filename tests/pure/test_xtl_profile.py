"""Pure tests for the xtl (JONR P20 Pro) profile quirks."""
from __future__ import annotations

import json

from .helpers import FakeMiotDevice, load_device_module


def _load_xtl(monkeypatch):
    device_mod = load_device_module(monkeypatch)
    monkeypatch.setattr(device_mod.time, "sleep", lambda s: None)
    return device_mod.XtlVacuumDevice("host", "token", "xtl.vacuum.xm2216")


def _last_calls():
    return FakeMiotDevice.instances[-1].calls


def _in(piid: int, value):
    """MIoT strict input frame: xtl drops positional raw values."""
    return {"did": "-", "piid": piid, "value": value}


def test_xtl_satisfies_card_baseline(monkeypatch):
    # XtlVacuumDevice refuses to build unless card_baseline_gaps() is empty.
    _load_xtl(monkeypatch)


def test_start_stops_then_uses_typed_full_clean(monkeypatch):
    device = _load_xtl(monkeypatch)

    device.start()

    assert _last_calls() == [
        ("action", 17, 3, []),
        ("action", 17, 1, [_in(8, 1), _in(34, "[]")]),
    ]


def test_start_when_paused_resumes_instead(monkeypatch):
    device = _load_xtl(monkeypatch)

    device.start("paused")

    assert _last_calls() == [("action", 17, 2, [_in(55, 8)])]


def test_pause_uses_robot_set_status(monkeypatch):
    device = _load_xtl(monkeypatch)

    device.pause()

    assert _last_calls() == [("action", 17, 2, [_in(55, 7)])]


def test_gear_changes_use_actions_not_property_writes(monkeypatch):
    device = _load_xtl(monkeypatch)

    device.set_fan_speed("turbo")
    device.set_water_level("high")
    device.set_mode("mop")

    assert _last_calls() == [
        ("action", 17, 26, [_in(13, 3)]),
        ("action", 17, 27, [_in(12, 2)]),
        ("action", 17, 46, [_in(10, 2)]),
    ]


def test_clean_segments_stops_then_sends_room_type(monkeypatch):
    device = _load_xtl(monkeypatch)

    device.clean_segments([3, 4])

    assert _last_calls() == [
        ("action", 17, 3, []),
        ("action", 17, 1, [_in(8, 3), _in(34, "[3, 4]")]),
    ]


def test_status_charged_maps_to_docked(monkeypatch):
    device = _load_xtl(monkeypatch)
    FakeMiotDevice.property_values = {(2, 2): 13, (16, 1): 100}

    status = device.status()

    assert status.activity == "docked"
    assert status.battery == 100


def test_route_and_count_selects_use_their_actions(monkeypatch):
    device = _load_xtl(monkeypatch)

    device.set_route("fine")
    device.set_count("twice")

    assert _last_calls() == [
        ("action", 17, 50, [_in(65, 2)]),
        ("action", 17, 28, [_in(14, 2)]),
    ]


def test_reset_consumable_sends_type_string(monkeypatch):
    device = _load_xtl(monkeypatch)

    device.reset_consumable("engineSensor")

    assert _last_calls() == [("action", 17, 20, [_in(33, "engineSensor")])]


def test_no_input_actions_take_positional_empty_list(monkeypatch):
    # stop/charge/locate declare no input piids -> no wrapping, empty "in".
    device = _load_xtl(monkeypatch)

    device.stop()
    device.return_home()
    device.locate()

    assert _last_calls() == [
        ("action", 17, 3, []),
        ("action", 16, 1, []),
        ("action", 17, 4, []),
    ]


def test_status_unpacks_consumables_json(monkeypatch):
    device = _load_xtl(monkeypatch)
    items = [
        {"type": "sideBrush", "used": 56, "mode": 1},
        {"type": "rollBrush", "used": 28, "mode": 1},
        {"type": "filter", "used": 70, "mode": 1},
        {"type": "mop", "used": 21, "mode": 1},
        {"type": "engineSensor", "used": 100, "mode": 2},
        {"type": "dustbag", "used": 96, "mode": 1},
        {"type": "mopCleaningTrough", "used": 46, "mode": 1},
    ]
    FakeMiotDevice.property_values = {
        (2, 2): 13,
        (16, 1): 100,
        (17, 32): json.dumps(items),
        (17, 14): 2,
        (17, 65): 2,
        (17, 36): 2008,
        (17, 79): 4502,
    }

    status = device.status()

    assert status.side_brush_life == 44
    assert status.main_brush_life == 72
    assert status.filter_life == 30
    assert status.mop_life == 79
    assert status.dust_bag_life == 4
    assert status.extra_consumable_lives == {
        "mop_trough_life": 54,
        "unit_sensor_life": 0,
    }
    assert status.consumable_types == tuple(i["type"] for i in items)
    assert status.count_raw == 2
    assert status.fine_drag_raw == 2
    assert status.message_raw == 2008
    assert status.station_error_raw == 4502


def test_map_capability_wired_on_profile(monkeypatch):
    device_mod = load_device_module(monkeypatch)
    monkeypatch.setattr(device_mod.time, "sleep", lambda s: None)
    device = device_mod.XtlVacuumDevice("host", "token", "xtl.vacuum.xm2216")

    cap = device.profile.map
    assert isinstance(cap, device_mod.XtlMapCapability)
    assert (cap.get_obj_name.siid, cap.get_obj_name.aiid) == (17, 12)
    assert cap.get_obj_name.out_piids == (38,)


def test_map_obj_name_reads_out_piid_38(monkeypatch):
    device = _load_xtl(monkeypatch)
    # Live-verified 17.12 answer: fully-qualified KS3 obj uid/did/slot.
    FakeMiotDevice.action_results[(17, 12)] = {
        "out": [{"did": "-", "piid": 38, "value": "1628932731/1152219162/0"}]
    }

    assert device.map_obj_name() == "1628932731/1152219162/0"
    assert _last_calls() == [("action", 17, 12, [])]


def test_map_obj_name_accepts_positional_out(monkeypatch):
    # Some firmware revisions answer action outs as raw positional values.
    device = _load_xtl(monkeypatch)
    FakeMiotDevice.action_results[(17, 12)] = {"out": ["name/raw"]}

    assert device.map_obj_name() == "name/raw"


def test_map_obj_name_none_when_out_is_empty(monkeypatch):
    device = _load_xtl(monkeypatch)

    assert device.map_obj_name() is None
