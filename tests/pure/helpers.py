"""Helpers for pure tests that import integration modules without HA."""
from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


class FakeMiotDevice:
    """Small MiotDevice fake with scriptable reads/actions."""

    instances: list["FakeMiotDevice"] = []
    property_values: dict[tuple[int, int], object] = {}
    action_results: dict[tuple[int, int], object] = {}
    info_mac = "AA:BB:CC:DD:EE:FF"

    def __init__(self, host, token, mapping=None, timeout=5):  # noqa: ARG002
        # device.py keeps two MiotDevice handles on the same robot (main +
        # short-timeout soft-read instance). The tests model one robot, so
        # every instance after the first writes into the first one's call
        # log — reads and actions stay observable through `instances[-1]`.
        if FakeMiotDevice.instances:
            self.calls = FakeMiotDevice.instances[0].calls
            self.batch_max_properties = FakeMiotDevice.instances[0].batch_max_properties
        else:
            self.calls = []
            self.batch_max_properties = []
        self.mapping = mapping
        self.instances.append(self)

    def get_property_by(self, siid: int, piid: int):
        self.calls.append(("get", siid, piid))
        value = self.property_values.get((siid, piid))
        if isinstance(value, Exception):
            raise value
        return [{"value": value}]

    def get_properties(self, properties, *, property_getter="get_properties", max_properties=None):  # noqa: ARG002
        self.batch_max_properties.append(max_properties)
        results = []
        for p in properties:
            siid, piid = p["siid"], p["piid"]
            self.calls.append(("get", siid, piid))
            value = self.property_values.get((siid, piid))
            if isinstance(value, Exception):
                raise value
            results.append({"siid": siid, "piid": piid, "code": 0, "value": value})
        return results

    def set_property_by(self, siid: int, piid: int, value):
        self.calls.append(("set", siid, piid, value))

    def call_action_by(self, siid: int, aiid: int, params):
        self.calls.append(("action", siid, aiid, params))
        result = self.action_results.get((siid, aiid), {"out": []})
        if isinstance(result, Exception):
            raise result
        return result

    def info(self):
        return SimpleNamespace(mac_address=self.info_mac)

    @classmethod
    def reset(cls) -> None:
        cls.instances.clear()
        cls.property_values = {}
        cls.action_results = {}
        cls.info_mac = "AA:BB:CC:DD:EE:FF"


def load_device_module(monkeypatch: pytest.MonkeyPatch):
    """Load jonr_vac.device with miio replaced by FakeMiotDevice."""
    pkg_root = Path(__file__).resolve().parents[2] / "custom_components" / "jonr_vac"

    miio = ModuleType("miio")
    miio.MiotDevice = FakeMiotDevice
    monkeypatch.setitem(sys.modules, "miio", miio)

    pkg = ModuleType("jonr_vac")
    pkg.__path__ = [str(pkg_root)]
    monkeypatch.setitem(sys.modules, "jonr_vac", pkg)

    for name in list(sys.modules):
        if name == "jonr_vac.device" or name.startswith("jonr_vac.spec"):
            monkeypatch.delitem(sys.modules, name, raising=False)

    FakeMiotDevice.reset()
    return importlib.import_module("jonr_vac.device")


def load_sensor_module(monkeypatch: pytest.MonkeyPatch):
    """Load jonr_vac.sensor with HA replaced by thin stubs.

    Only the symbols actually consumed by sensor.py are stubbed — enough to
    import the module and exercise build_sensors() without a real HA install.
    """

    # --- minimal HA dataclass stubs ---

    @dataclass(frozen=True, kw_only=True)
    class _SensorEntityDescription:
        key: str = ""
        translation_key: str | None = None
        device_class: str | None = None
        options: list | None = None
        native_unit_of_measurement: str | None = None
        state_class: str | None = None
        entity_category: str | None = None
        icon: str | None = None

    class _SensorDeviceClass(SimpleNamespace):
        ENUM = "enum"
        BATTERY = "battery"
        DURATION = "duration"
        AREA = "area"

    class _SensorStateClass(SimpleNamespace):
        MEASUREMENT = "measurement"
        TOTAL_INCREASING = "total_increasing"

    class _EntityCategory(SimpleNamespace):
        DIAGNOSTIC = "diagnostic"
        CONFIG = "config"

    class _UnitOfArea(SimpleNamespace):
        SQUARE_METERS = "m²"

    class _UnitOfTime(SimpleNamespace):
        MINUTES = "min"
        SECONDS = "s"

    # Stub modules sensor.py imports from HA
    def _make(name: str, **attrs) -> ModuleType:
        m = ModuleType(name)
        m.__dict__.update(attrs)
        return m

    sensor_mod = _make(
        "homeassistant.components.sensor",
        SensorDeviceClass=_SensorDeviceClass,
        SensorEntity=object,
        SensorEntityDescription=_SensorEntityDescription,
        SensorStateClass=_SensorStateClass,
    )
    const_mod = _make(
        "homeassistant.const",
        PERCENTAGE="%",
        EntityCategory=_EntityCategory,
        UnitOfArea=_UnitOfArea,
        UnitOfTime=_UnitOfTime,
    )
    for mod_name, mod in [
        ("homeassistant", ModuleType("homeassistant")),
        ("homeassistant.components", ModuleType("homeassistant.components")),
        ("homeassistant.components.sensor", sensor_mod),
        ("homeassistant.const", const_mod),
        ("homeassistant.core", _make("homeassistant.core", HomeAssistant=object)),
        ("homeassistant.helpers", ModuleType("homeassistant.helpers")),
        ("homeassistant.helpers.device_registry", _make("homeassistant.helpers.device_registry", DeviceInfo=object)),
        ("homeassistant.helpers.entity_platform", _make("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)),
        ("homeassistant.helpers.update_coordinator", _make(
            "homeassistant.helpers.update_coordinator",
            CoordinatorEntity=type("CoordinatorEntity", (), {"__class_getitem__": classmethod(lambda cls, _: cls)}),
            DataUpdateCoordinator=object,
            UpdateFailed=Exception,
        )),
    ]:
        monkeypatch.setitem(sys.modules, mod_name, mod)

    # Stub the integration's own modules that sensor.py depends on
    pkg_root = Path(__file__).resolve().parents[2] / "custom_components" / "jonr_vac"
    pkg = ModuleType("jonr_vac")
    pkg.__path__ = [str(pkg_root)]
    monkeypatch.setitem(sys.modules, "jonr_vac", pkg)

    # __init__ stub (just JonrConfigEntry)
    init_stub = ModuleType("jonr_vac.__init__")
    init_stub.JonrConfigEntry = object
    monkeypatch.setitem(sys.modules, "jonr_vac.__init__", init_stub)
    # The relative `. import JonrConfigEntry` resolves via the package __init__
    pkg.JonrConfigEntry = object  # type: ignore[attr-defined]

    const_stub = _make("jonr_vac.const", DOMAIN="jonr_vac")
    monkeypatch.setitem(sys.modules, "jonr_vac.const", const_stub)

    coordinator_stub = _make(
        "jonr_vac.coordinator",
        JonrVacuumCoordinator=object,
    )
    monkeypatch.setitem(sys.modules, "jonr_vac.coordinator", coordinator_stub)

    # device stub — only VacuumStatus is needed by sensor.py
    device_stub = _make("jonr_vac.device", VacuumStatus=object)
    monkeypatch.setitem(sys.modules, "jonr_vac.device", device_stub)

    # Ensure spec subpackage stubs are present so relative imports resolve
    spec_pkg = ModuleType("jonr_vac.spec")
    spec_pkg.__path__ = [str(pkg_root / "spec")]
    monkeypatch.setitem(sys.modules, "jonr_vac.spec", spec_pkg)

    # spec.types is imported directly by sensor.py; import the real thing.
    # Profiles must go too — sensor.py's XtlCoreCapability isinstance gates
    # compare against the freshly reloaded spec.types classes, so a cached
    # jonr_vac.spec.profiles.xtl (built against an older generation of
    # those classes) would make every gate fail.
    for name in list(sys.modules):
        if (name == "jonr_vac.spec.types" or name == "jonr_vac.sensor"
                or name.startswith("jonr_vac.spec.profiles")):
            monkeypatch.delitem(sys.modules, name, raising=False)

    # Import real spec.types so ModelProfile etc. are genuine objects
    spec_types = importlib.import_module("jonr_vac.spec.types")
    monkeypatch.setitem(sys.modules, "jonr_vac.spec.types", spec_types)

    return importlib.import_module("jonr_vac.sensor")


class FakePersistentNotification:
    """Stand-in for homeassistant.components.persistent_notification.

    Class-level records so a test can assert create/dismiss calls across the
    notifier's lazy ``from homeassistant.components import …`` lookups."""

    created: list[dict] = []
    dismissed: list[str] = []

    @classmethod
    def reset(cls) -> None:
        cls.created = []
        cls.dismissed = []


class FakeCoordinator:
    """DataUpdateCoordinator stand-in: a listener list + a data snapshot."""

    def __init__(self) -> None:
        self.data = None
        self.subs: list = []
        self.unsubbed: list = []

    def async_add_listener(self, cb):
        self.subs.append(cb)

        def unsub() -> None:
            if cb in self.subs:
                self.subs.remove(cb)
                self.unsubbed.append(cb)

        return unsub

    def push(self) -> None:
        """Simulate a coordinator snapshot update."""
        for cb in list(self.subs):
            cb()


def _make_module(name: str, **attrs) -> ModuleType:
    m = ModuleType(name)
    m.__dict__.update(attrs)
    return m


def _bootstrap_jonr_pkg(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Mount jonr_vac + .spec + .spec.profiles as synthetic packages (their
    real __init__ imports HA, which the pure tier must not execute) and drop
    any cached submodule so the next import re-reads the shipped files."""
    pkg_root = Path(__file__).resolve().parents[2] / "custom_components" / "jonr_vac"
    for name, sub in (
        ("jonr_vac", ""),
        ("jonr_vac.spec", "spec"),
        ("jonr_vac.spec.profiles", "spec/profiles"),
    ):
        pkg = ModuleType(name)
        pkg.__path__ = [str(pkg_root / sub if sub else pkg_root)]
        monkeypatch.setitem(sys.modules, name, pkg)
    for name in list(sys.modules):
        if name == "jonr_vac.const" or name.startswith("jonr_vac.spec"):
            monkeypatch.delitem(sys.modules, name, raising=False)
    return pkg_root


def load_xtl_module(monkeypatch: pytest.MonkeyPatch):
    """Load the real jonr_vac.spec.profiles.xtl (pure, no HA, no miio)."""
    _bootstrap_jonr_pkg(monkeypatch)
    return importlib.import_module("jonr_vac.spec.profiles.xtl")


def load_error_events_module(monkeypatch: pytest.MonkeyPatch):
    """Load jonr_vac.error_events with its two lazy HA touchpoints stubbed.

    The module imports no HA at load time; the lazy imports inside methods
    (persistent_notification, entity_registry) hit these stubs. const and the
    xtl tables are imported for real — the tables notifications quote are
    the shipped ones."""
    FakePersistentNotification.reset()

    # Signatures mirror the real API: hass arrives as the first argument.
    def _create(hass, message, title=None, notification_id=None):
        assert hass is not None
        FakePersistentNotification.created.append(
            {"message": message, "title": title, "notification_id": notification_id}
        )

    def _dismiss(hass, notification_id):
        assert hass is not None
        FakePersistentNotification.dismissed.append(notification_id)

    components = ModuleType("homeassistant.components")
    components.persistent_notification = _make_module(  # type: ignore[attr-defined]
        "homeassistant.components.persistent_notification",
        async_create=_create,
        async_dismiss=_dismiss,
    )
    for mod_name, mod in [
        ("homeassistant", ModuleType("homeassistant")),
        ("homeassistant.core", _make_module(
            "homeassistant.core", callback=lambda func: func, HomeAssistant=object)),
        ("homeassistant.components", components),
    ]:
        monkeypatch.setitem(sys.modules, mod_name, mod)

    _bootstrap_jonr_pkg(monkeypatch)
    monkeypatch.delitem(sys.modules, "jonr_vac.error_events", raising=False)
    return importlib.import_module("jonr_vac.error_events")


class FakeStore:
    """Stand-in for homeassistant.helpers.storage.Store.

    Backed by a shared class-level dict keyed by storage key, so a test can
    simulate a fresh load, a persisted round-trip, or a corrupt file by
    poking `FakeStore.backing` directly before constructing MapCache.
    """

    backing: dict[str, object] = {}

    def __init__(self, hass, version: int, key: str) -> None:  # noqa: ARG002
        self.key = key

    async def async_load(self):
        return self.backing.get(self.key)

    async def async_save(self, data) -> None:
        self.backing[self.key] = data

    @classmethod
    def reset(cls) -> None:
        cls.backing = {}


def load_map_cache_module(monkeypatch: pytest.MonkeyPatch):
    """Load jonr_vac.map_cache with HA's Store replaced by FakeStore."""

    def _make(name: str, **attrs) -> ModuleType:
        m = ModuleType(name)
        m.__dict__.update(attrs)
        return m

    FakeStore.reset()
    for mod_name, mod in [
        ("homeassistant", ModuleType("homeassistant")),
        ("homeassistant.core", _make("homeassistant.core", HomeAssistant=object)),
        ("homeassistant.helpers", ModuleType("homeassistant.helpers")),
        ("homeassistant.helpers.storage", _make("homeassistant.helpers.storage", Store=FakeStore)),
    ]:
        monkeypatch.setitem(sys.modules, mod_name, mod)

    pkg_root = Path(__file__).resolve().parents[2] / "custom_components" / "jonr_vac"
    pkg = ModuleType("jonr_vac")
    pkg.__path__ = [str(pkg_root)]
    monkeypatch.setitem(sys.modules, "jonr_vac", pkg)

    const_stub = _make("jonr_vac.const", DOMAIN="jonr_vac")
    monkeypatch.setitem(sys.modules, "jonr_vac.const", const_stub)

    for name in list(sys.modules):
        if name == "jonr_vac.map_cache":
            monkeypatch.delitem(sys.modules, name, raising=False)

    module = importlib.import_module("jonr_vac.map_cache")
    return module, FakeStore
