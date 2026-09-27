"""Pure tests for the ENUM error sensors' attributes (code/solution/image)."""
from __future__ import annotations

from types import SimpleNamespace

from .helpers import load_sensor_module


def _error_attrs_fn(monkeypatch, key: str):
    """Return the attrs_lang_fn of the named error sensor description."""
    sensor = load_sensor_module(monkeypatch)
    import importlib
    from pathlib import Path
    from types import ModuleType
    import sys

    pkg_root = Path(sensor.__file__).resolve().parent
    pkg = ModuleType("jonr_vac")
    pkg.__path__ = [str(pkg_root)]
    sys.modules.setdefault("jonr_vac", pkg)
    spec_pkg = ModuleType("jonr_vac.spec")
    spec_pkg.__path__ = [str(pkg_root / "spec")]
    sys.modules.setdefault("jonr_vac.spec", spec_pkg)
    profiles = importlib.import_module("jonr_vac.spec.profiles.xtl")

    desc = next(d for d in sensor.build_sensors(profiles.XTL_XM2216)
                if d.key == key)
    return desc.attrs_lang_fn, profiles


def test_error_attrs_carry_official_image(monkeypatch):
    """An error code with an official fault picture exposes its file name.

    Automations build the push URL from this attribute (the companion app
    needs an absolute URL it assembles itself from the HA base URL).
    """
    attrs_fn, profiles = _error_attrs_fn(monkeypatch, "robot_error")
    code = next(iter(profiles.XTL_ERROR_IMAGES))
    status = SimpleNamespace(fault=code)

    out = attrs_fn(status, "en")

    assert out["image"] == profiles.XTL_ERROR_IMAGES[code]
    assert out["image"].endswith(".png")
    assert out["code"] == code
    assert "solution" in out  # every imaged code also has fix-it text


def test_error_attrs_none_while_healthy(monkeypatch):
    """No attributes at all while the robot reports no fault."""
    attrs_fn, _ = _error_attrs_fn(monkeypatch, "station_error")

    assert attrs_fn(SimpleNamespace(station_error_raw=0), "en") is None
