"""Pure tests for XtlVacuumDevice status handling (xtl profile)."""
from __future__ import annotations

import pytest

from .helpers import FakeMiotDevice, load_device_module


@pytest.mark.parametrize(
    ("raw_status", "expected_raw", "expected_activity"),
    [
        (5, 5, "cleaning"),   # Sweeping and Mopping
        (6, 6, "paused"),     # Paused
        (999, 999, "idle"),   # unknown code falls back to idle
    ],
)
def test_status_maps_raw_activity(monkeypatch, raw_status, expected_raw, expected_activity):
    device_mod = load_device_module(monkeypatch)
    device = device_mod.XtlVacuumDevice("host", "token", "xtl.vacuum.xm2216")
    status_prop = device.core.status
    FakeMiotDevice.property_values = {(status_prop.siid, status_prop.piid): raw_status}

    status = device.status()

    assert status.raw_status == expected_raw
    assert status.activity == expected_activity


def test_status_raises_when_required_prop_fails(monkeypatch):
    """A failing status read must raise DeviceCommunicationError, not return idle."""
    device_mod = load_device_module(monkeypatch)
    device = device_mod.XtlVacuumDevice("host", "token", "xtl.vacuum.xm2216")
    status_prop = device.core.status
    FakeMiotDevice.property_values = {
        (status_prop.siid, status_prop.piid): RuntimeError("network timeout")
    }

    with pytest.raises(device_mod.DeviceCommunicationError):
        device.status()


def test_status_tolerates_optional_prop_none(monkeypatch):
    """Optional props returning None must not raise; required status must succeed."""
    device_mod = load_device_module(monkeypatch)
    device = device_mod.XtlVacuumDevice("host", "token", "xtl.vacuum.xm2216")
    status_prop = device.core.status
    # Only supply the required status prop; everything else defaults to None via FakeMiotDevice.
    FakeMiotDevice.property_values = {(status_prop.siid, status_prop.piid): 5}

    status = device.status()

    assert status.raw_status == 5
    # Optional fields that have no prop on this model or no value stay None.
    assert status.sweep_type_raw is None or isinstance(status.sweep_type_raw, int)


def test_status_skips_absent_core_props(monkeypatch):
    """Props the profile doesn't declare are never polled (None filter)."""
    device_mod = load_device_module(monkeypatch)
    device = device_mod.XtlVacuumDevice("host", "token", "xtl.vacuum.xm2216")
    status_prop = device.core.status
    FakeMiotDevice.property_values = {(status_prop.siid, status_prop.piid): 5}

    # The xtl core exposes no sweep_type/alarm prop.
    assert device.core.sweep_type is None
    assert device.core.alarm is None

    status = device.status()

    assert status.sweep_type_raw is None
    assert status.alarm_raw is None
    assert all(call[1] is not None and call[2] is not None for call in FakeMiotDevice.instances[-1].calls)


def test_lean_core_fields_stay_parked(monkeypatch):
    device_mod = load_device_module(monkeypatch)
    device = device_mod.XtlVacuumDevice("host", "token", "xtl.vacuum.xm2216")
    status_prop = device.core.status
    FakeMiotDevice.property_values = {(status_prop.siid, status_prop.piid): 5}

    status = device.status()

    assert status.clean_area is None
    assert status.clean_time is None


def test_as_int_coercion(monkeypatch):
    device_mod = load_device_module(monkeypatch)

    assert device_mod._as_int("83") == 83
    assert device_mod._as_int(None) is None
    assert device_mod._as_int("junk") is None


@pytest.mark.parametrize(
    "model",
    [
        "ijai.vacuum.v17",
        "dreame.vacuum.p2008",
        "viomi.vacuum.v12",
        "roborock.vacuum.a01",
    ],
)
def test_device_refuses_pruned_brand_models(monkeypatch, model: str):
    """Every non-xtl brand is out of this fork — building one must refuse."""
    device_mod = load_device_module(monkeypatch)

    with pytest.raises(ValueError):
        device_mod.XtlVacuumDevice("host", "token", model)
