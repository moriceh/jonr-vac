"""Pure tests for build_sensors() on the xtl profile — no HA install required."""
from __future__ import annotations


from .helpers import load_sensor_module


_DEAD_KEYS = {
    "clean_area",
    "clean_time",
}


def _xtl_profile(monkeypatch):
    """Return the real XTL_XM2216 ModelProfile (no HA needed)."""
    import importlib
    import sys
    from pathlib import Path
    from types import ModuleType

    pkg_root = Path(__file__).resolve().parents[2] / "custom_components" / "jonr_vac"
    pkg = ModuleType("jonr_vac")
    pkg.__path__ = [str(pkg_root)]
    sys.modules.setdefault("jonr_vac", pkg)

    spec_pkg = ModuleType("jonr_vac.spec")
    spec_pkg.__path__ = [str(pkg_root / "spec")]
    sys.modules.setdefault("jonr_vac.spec", spec_pkg)

    for name in list(sys.modules):
        if name.startswith("jonr_vac.spec.") and "profiles" in name:
            monkeypatch.delitem(sys.modules, name, raising=False)

    profiles_mod = importlib.import_module("jonr_vac.spec.profiles.xtl")
    return profiles_mod.XTL_XM2216


def test_build_sensors_xtl_has_status_and_battery(monkeypatch):
    sensor = load_sensor_module(monkeypatch)
    profile = _xtl_profile(monkeypatch)

    sensors = sensor.build_sensors(profile)
    keys = {d.key for d in sensors}

    assert "status" in keys
    assert "battery" in keys


def test_build_sensors_xtl_no_dead_sensors(monkeypatch):
    """xtl must not produce permanently-None sensor entities at launch."""
    sensor = load_sensor_module(monkeypatch)
    profile = _xtl_profile(monkeypatch)

    sensors = sensor.build_sensors(profile)
    keys = {d.key for d in sensors}

    assert keys.isdisjoint(_DEAD_KEYS), (
        f"Dead sensor(s) found for xtl: {keys & _DEAD_KEYS}"
    )


def test_build_sensors_xtl_exact_set(monkeypatch):
    """Return the complete xtl sensor set (xtl-only fork table)."""
    sensor = load_sensor_module(monkeypatch)
    profile = _xtl_profile(monkeypatch)

    sensors = sensor.build_sensors(profile)
    keys = {d.key for d in sensors}

    assert keys == {
        "status",
        "battery",
        "main_brush_life",
        "side_brush_life",
        "filter_life",
        "mop_life",
        "dust_bag_life",
        "mop_trough_life",
        "unit_sensor_life",
        "session_clean_time",
        "session_clean_area",
        "total_clean_time",
        "total_clean_area",
        "total_clean_count",
        "clean_timers",
        "robot_error",
        "station_error",
        "robot_message",
        "station_status",
        "clean_water_tank",
        "drain_water_tank",
        "dust_bag_state",
        "mop_tank_state",
    }


def test_build_sensors_profile_without_battery_omits_battery(monkeypatch):
    """A profile with core.battery=None must not get a battery sensor."""
    from dataclasses import replace

    sensor = load_sensor_module(monkeypatch)
    profile = _xtl_profile(monkeypatch)
    # Strip the battery prop from the core
    coreless_battery = replace(profile.core, battery=None)
    no_battery_profile = replace(profile, core=coreless_battery)

    sensors = sensor.build_sensors(no_battery_profile)
    keys = {d.key for d in sensors}

    assert "battery" not in keys
    assert "status" in keys
