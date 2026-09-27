"""Pure-tier tests for the card baseline (xtl-only registry)."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

from spec.registry import MODEL_PROFILES, card_baseline_gaps, get_profile, is_supported


def test_is_supported_matches_card_baseline_for_registry():
    mismatches = {
        model: card_baseline_gaps(profile)
        for model, profile in MODEL_PROFILES.items()
        if is_supported(model) != (not card_baseline_gaps(profile))
    }

    assert mismatches == {}


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("xtl.vacuum.xm2216", True),
        ("ijai.vacuum.v17", False),
        ("dreame.vacuum.p2008", False),
        ("viomi.vacuum.v12", False),
        ("roborock.vacuum.a01", False),
    ],
)
def test_support_gate_known_models(model, expected):
    """Only the shipped xtl profile passes; every other brand is gone."""
    assert is_supported(model) is expected


def test_xtl_profile_meets_card_baseline():
    profile = get_profile("xtl.vacuum.xm2216")

    assert profile is not None
    assert card_baseline_gaps(profile) == ()


class _FakeMiotDevice:
    instances: list["_FakeMiotDevice"] = []

    def __init__(self, host, token, mapping=None, timeout=5):
        # device.py holds two MiotDevice handles on the same robot (main +
        # short-timeout soft-read instance); tests model one robot, so every
        # instance writes into the first one's call log.
        if _FakeMiotDevice.instances:
            self.calls = _FakeMiotDevice.instances[0].calls
        else:
            self.calls = []
        self.mapping = mapping
        self.instances.append(self)

    def set_property_by(self, siid, piid, value):
        self.calls.append(("set", siid, piid, value))

    def call_action_by(self, siid, aiid, params):
        self.calls.append(("action", siid, aiid, params))
        return {"out": []}


def _load_device_module(monkeypatch: pytest.MonkeyPatch):
    pkg_root = Path(__file__).resolve().parents[2] / "custom_components" / "jonr_vac"

    miio = ModuleType("miio")
    miio.MiotDevice = _FakeMiotDevice
    monkeypatch.setitem(sys.modules, "miio", miio)

    pkg = ModuleType("jonr_vac")
    pkg.__path__ = [str(pkg_root)]
    monkeypatch.setitem(sys.modules, "jonr_vac", pkg)

    for name in list(sys.modules):
        if name == "jonr_vac.device" or name.startswith("jonr_vac.spec"):
            monkeypatch.delitem(sys.modules, name, raising=False)

    _FakeMiotDevice.instances.clear()
    return importlib.import_module("jonr_vac.device")


def test_device_locate_uses_core_action(monkeypatch):
    device_mod = _load_device_module(monkeypatch)
    device = device_mod.XtlVacuumDevice("host", "token", "xtl.vacuum.xm2216")

    device.locate()

    assert _FakeMiotDevice.instances[-1].calls == [("action", 17, 4, [])]


def test_device_rejects_unsupported_models(monkeypatch):
    device_mod = _load_device_module(monkeypatch)

    with pytest.raises(ValueError):
        device_mod.XtlVacuumDevice("host", "token", "dreame.vacuum.r2235a")
